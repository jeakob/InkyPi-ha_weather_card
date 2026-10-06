import json
import logging
import os
from datetime import datetime

import requests

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

# HA weather condition -> e-ink SVG icon basename (day/night variants), matching the
# E-Ink-Weather-Card icon sets.
ICON_DAY = {
    "clear-night": "clear-night", "cloudy": "cloudy", "exceptional": "exceptional",
    "fog": "fog", "hail": "hail", "lightning": "lightning", "lightning-rainy": "lightning-rain",
    "partlycloudy": "partlycloudy-day", "pouring": "pouring", "rainy": "rain",
    "snowy": "snow", "snowy-rainy": "sleet", "sunny": "clear-day",
    "windy": "wind", "windy-variant": "wind",
}
ICON_NIGHT = dict(ICON_DAY, **{"sunny": "clear-night", "partlycloudy": "partlycloudy-night"})

CONDITION_KEYS = list(ICON_DAY.keys())

# Wind speed conversions from m/s (HA's base for many providers is already the entity unit,
# so we only convert when the user explicitly picks a wind unit different from the source).
WIND_FACTORS = {"km/h": 3.6, "m/s": 1.0, "mph": 2.23694, "kn": 1.94384}


def _truthy(v, default=False):
    if v is None:
        return default
    return str(v).lower() in ("true", "1", "yes", "on")


class HAWeatherCard(BasePlugin):
    """Render an e-ink weather card from Home Assistant data.

    A port of the E-Ink-Weather-Card: pulls a weather entity (and optional per-stat
    override sensors) from the HA REST API and renders its own localized visuals.
    """

    def __init__(self, config, **deps):
        super().__init__(config, **deps)
        with open(self.get_plugin_dir("locale.json"), encoding="utf-8") as f:
            self.locales = json.load(f)

    # --- entry point ----------------------------------------------------------

    def generate_image(self, settings, device_config):
        base_url = (settings.get("ha_url") or "").strip().rstrip("/")
        # Kept in .env via the API Keys page (masked there) rather than in plugin settings,
        # which the settings form would echo back in plain text.
        token = (device_config.load_env_key("HA_ACCESS_TOKEN") or "").strip()
        entity_id = (settings.get("weather_entity") or "").strip()
        if not base_url:
            raise RuntimeError("Home Assistant URL is required.")
        if not token:
            raise RuntimeError("HA_ACCESS_TOKEN is not set. Add your long-lived access token on the API Keys page.")
        if not entity_id:
            raise RuntimeError("A weather entity (e.g. weather.home) is required.")

        verify_ssl = not _truthy(settings.get("insecure"))
        if not verify_ssl:
            requests.packages.urllib3.disable_warnings()
        self._headers = {"Authorization": f"Bearer {token}"}
        self._base_url = base_url
        self._verify = verify_ssl

        weather = self._get_state(entity_id)
        self._sun = self._get_sun(settings)
        forecast_type = settings.get("forecast_type") or "daily"
        forecast = self._get_forecast(entity_id, forecast_type)

        # Resolve override sensors (only fetch the ones configured).
        overrides = self._resolve_overrides(settings)
        custom_text = self._resolve_custom_text(settings)
        title = self._resolve_title(settings, weather)

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        params = self._build_params(settings, weather, forecast, forecast_type, overrides, custom_text, title)
        params["plugin_settings"] = settings

        image = self.render_image(dimensions, "ha_weather_card.html", "ha_weather_card.css", params)
        if not image:
            raise RuntimeError("Failed to render weather card, please check logs.")
        return image

    # --- HA API ---------------------------------------------------------------

    def _get_state(self, entity_id):
        url = f"{self._base_url}/api/states/{entity_id}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=20, verify=self._verify)
        except requests.RequestException as e:
            raise RuntimeError(f"Could not reach Home Assistant: {e}")
        if resp.status_code == 401:
            raise RuntimeError("Home Assistant rejected the access token (401).")
        if resp.status_code == 404:
            raise RuntimeError(f"Entity '{entity_id}' not found in Home Assistant.")
        if not resp.ok:
            raise RuntimeError(f"Home Assistant returned {resp.status_code} for '{entity_id}'.")
        return resp.json()

    def _get_forecast(self, entity_id, forecast_type):
        url = f"{self._base_url}/api/services/weather/get_forecasts?return_response"
        try:
            resp = requests.post(url, headers=self._headers,
                                 json={"entity_id": entity_id, "type": forecast_type},
                                 timeout=20, verify=self._verify)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Could not fetch forecast for {entity_id}: {e}")
            return []
        return data.get("service_response", {}).get(entity_id, {}).get("forecast", []) or []

    def _resolve_overrides(self, settings):
        """Fetch the optional per-stat override sensors that are configured."""
        keys = ["temp", "feels_like", "description", "press", "humid", "uv", "winddir",
                "windspeed", "dew_point", "wind_gust_speed", "visibility", "cloud_coverage"]
        out = {}
        for key in keys:
            ent = (settings.get(key) or "").strip()
            if not ent:
                continue
            try:
                out[key] = self._get_state(ent).get("state")
            except RuntimeError as e:
                logger.warning(f"Override sensor '{ent}' for {key} failed: {e}")
        return out

    def _resolve_custom_text(self, s):
        """Custom text: literal value if set, otherwise an entity's state/attribute."""
        return self._resolve_text(s, "custom_text_sensor_value", "custom_text_sensor",
                                  "custom_text_sensor_attribute")

    def _resolve_title(self, s, weather):
        """Title: literal value or a sensor/calendar's text. Falls back to the entity name
        ONLY when no title source is configured, so a calendar that is off shows no title."""
        resolved = self._resolve_text(s, "title", "title_sensor", "title_sensor_attribute")
        if resolved:
            return resolved
        if (s.get("title") or "").strip() or (s.get("title_sensor") or "").strip():
            return ""  # a configured source resolved empty (e.g. calendar off) -> no title
        return weather.get("attributes", {}).get("friendly_name") or "Weather"

    def _resolve_text(self, s, value_key, sensor_key, attr_key):
        """Shared resolver: a literal value, or a sensor/calendar's state/attribute."""
        text = (s.get(value_key) or "").strip()
        if text:
            return text
        ent = (s.get(sensor_key) or "").strip()
        if not ent:
            return ""
        try:
            state = self._get_state(ent)
        except RuntimeError as e:
            logger.warning(f"Sensor '{ent}' failed: {e}")
            return ""
        attrs = state.get("attributes", {})
        attr = (s.get(attr_key) or "").strip()

        # Calendar entities: only show while an event is active (state 'on'); use the event
        # message, and for timed (non all-day) events append the start time.
        if ent.startswith("calendar."):
            if state.get("state") != "on":
                return ""
            val = attrs.get(attr) if attr else attrs.get("message")
            val = "" if val in (None, "unknown", "unavailable") else str(val)
            if not attrs.get("all_day") and attrs.get("start_time"):
                start = self._format_event_time(attrs["start_time"])
                if start:
                    val = f"{val} {start}".strip()
            return val

        val = attrs.get(attr) if attr else state.get("state")
        return "" if val in (None, "unknown", "unavailable") else str(val)

    @staticmethod
    def _format_event_time(value):
        """Parse a calendar start_time ('YYYY-MM-DD HH:MM:SS') to a short local time."""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(value, fmt).strftime("%H:%M")
            except (ValueError, TypeError):
                continue
        return ""

    # --- params ---------------------------------------------------------------

    def _build_params(self, s, weather, forecast, forecast_type, ov, custom_text, title):
        attrs = weather.get("attributes", {})
        condition = weather.get("state", "")
        t = self.locales.get(s.get("locale") or "en", self.locales["en"])
        temp_unit = attrs.get("temperature_unit", "°C")

        def attr(name, ov_key=None):
            if ov_key and ov_key in ov and ov[ov_key] not in (None, "unknown", "unavailable"):
                return ov[ov_key]
            return attrs.get(name)

        is_day = self._is_day_now()
        bold_text = _truthy(s.get("bold_text"), True)
        p = {
            "t": t,
            "title": title,
            "bold_text": bold_text,
            # Per-text-field bold controls (default on); on -> the strong weight tier,
            # off -> the regular tier. Each editable text field gets its own toggle.
            "feels_like_bold": _truthy(s.get("feels_like_bold"), True),
            "title_bold": _truthy(s.get("title_bold"), True),
            "summary_bold": _truthy(s.get("summary_bold"), True),
            # Two font-weight tiers so the card keeps its visual hierarchy in either mode:
            # bold (default) runs heavier throughout, off falls back to normal weights.
            "fw_strong": 700 if bold_text else 600,
            "fw_regular": 600 if bold_text else 400,
            "text_color": s.get("textColor") or "#000000",
            "title_color": s.get("title_color") or s.get("textColor") or "#000000",
            "accent_color": s.get("accentColor") or "#d35400",
            "temp_unit": temp_unit,
            # display toggles (defaults follow the card: most on, a few off)
            "show": {k: _truthy(s.get(f"show_{k}"), dflt) for k, dflt in {
                "main": True, "current_condition": True, "temperature": True,
                "feels_like": True, "description": False, "attributes": True,
                "attribute_labels": True, "humidity": True, "pressure": True,
                "wind_speed": True, "wind_direction": True, "wind_gust_speed": False,
                "dew_point": False, "visibility": False, "sun": True, "uv": False,
                "date": True, "day": True, "time": False, "daily_summary": False,
            }.items()},
            # current conditions
            "condition": t.get(condition, condition.replace("-", " ").title()),
            "condition_icon": self._icon(condition, is_day, s),
            "temperature": self._round(attr("temperature", "temp")),
            "feels_like": self._round(attr("apparent_temperature", "feels_like")),
            "description": attr(None, "description"),
        }

        # attributes (icon, label, value), honoring per-stat toggles + overrides
        p["data_points"] = self._data_points(s, t, attrs, attr)
        p["clock"] = self._clock(s, t)
        p["custom_text"] = self._custom_text(s, custom_text)
        p["forecast"] = self._parse_forecast(forecast, forecast_type, t, s)
        p["forecast_type"] = forecast_type
        # Chance-of-rain bars are hidden below this probability (%), so the chart only shows
        # a bar when rain is actually likely rather than for every hour.
        try:
            p["rain_threshold"] = max(0, min(100, int(s.get("rain_bar_threshold") or 30)))
        except (TypeError, ValueError):
            p["rain_threshold"] = 30
        p["daily_summary"] = self._daily_summary(s, t)
        sizes = self._sizes(s)
        p["sizes"] = sizes
        # Height (px) to reserve at the bottom for the forecast + summary, which are pinned
        # there with absolute positioning (flex/grid distribution is unreliable in the
        # base's quirks-mode document).
        bh = 4
        if p["forecast"]:
            bh += sizes["forecast_icon"] + 6
            bh += (sizes["forecast_icon"] + 36) if forecast_type == "daily" else sizes["chart_height"]
        if p["daily_summary"]:
            bh += sizes["summary"] + 28
        p["bottom_height"] = bh
        return p

    def _daily_summary(self, s, t):
        """The 'Tomorrow' / 'In 2 days' summary, from the daily forecast. The two labels
        default to the language pack but can be overridden in settings."""
        if not _truthy(s.get("show_daily_summary"), False):
            return []
        from datetime import timedelta
        daily = self._get_forecast(s.get("weather_entity"), "daily")
        today = datetime.now().date()
        tomorrow = (s.get("summary_tomorrow_label") or "").strip() or t.get("tomorrow", "Tomorrow")
        in2days = (s.get("summary_in2days_label") or "").strip() or t.get("in2days", "In 2 days")
        targets = [(today + timedelta(days=1), tomorrow),
                   (today + timedelta(days=2), in2days)]
        out = []
        for target_date, label in targets:
            for e in daily:
                dt = self._parse_dt(e.get("datetime"))
                if dt and dt.date() == target_date:
                    cond = e.get("condition", "")
                    out.append({
                        "label": label,
                        "icon": self._icon(cond, True, s),
                        "condition": t.get(cond, cond.replace("-", " ").title()),
                        "high": self._round(e.get("temperature")),
                        "low": self._round(e.get("templow")),
                    })
                    break
        return out

    def _data_points(self, s, t, attrs, attr):
        dp = []

        def add(show_default, show_key, icon, label, value):
            if value in (None, "", "unknown", "unavailable"):
                return
            if not _truthy(s.get(show_key), show_default):
                return
            dp.append({"icon": self.get_plugin_dir(f"icons/{icon}.png"), "label": label, "value": value})

        humid = attr("humidity", "humid")
        add(True, "show_humidity", "humidity", t["humidity"], f"{self._round(humid)}%" if humid is not None else None)
        press = attr("pressure", "press")
        if press is not None:
            add(True, "show_pressure", "pressure", t["pressure"], f"{self._round(press)} {attrs.get('pressure_unit', t['units'].get('hPa', 'hPa'))}")
        ws = attr("wind_speed", "windspeed")
        if ws is not None:
            wd = self._wind_dir(attr("wind_bearing", "winddir"), t)
            ws_val = f"{self._round(ws)} {attrs.get('wind_speed_unit', 'km/h')}"
            if wd and _truthy(s.get("show_wind_direction"), True):
                ws_val = f"{wd} {ws_val}"
            add(True, "show_wind_speed", "wind", t["windSpeed"], ws_val)
        add(False, "show_wind_gust_speed", "wind", t["windGust"], self._fmt(attr("wind_gust_speed", "wind_gust_speed")))
        add(False, "show_dew_point", "humidity", t["dewPoint"], self._fmt(attr("dew_point", "dew_point"), attrs.get("temperature_unit", "")))
        add(False, "show_visibility", "visibility", t["visibility"], self._fmt(attr("visibility", "visibility")))
        add(False, "show_uv", "uvi", "UV", self._fmt(attr("uv_index", "uv")))
        return dp

    def _clock(self, s, t):
        now = datetime.now()
        twelve = _truthy(s.get("use_12hour_format"))
        time_fmt = "%I:%M %p" if twelve else "%H:%M"
        months = t.get("monthsGen") or []
        days = t.get("days") or []
        return {
            "show_day": _truthy(s.get("show_day"), True),
            "show_date": _truthy(s.get("show_date"), True),
            "show_time": _truthy(s.get("show_time"), False),
            "day": days[now.weekday()] if days else now.strftime("%A"),
            "date": f"{now.day} {months[now.month - 1]}" if months else now.strftime("%d %B"),
            "time": now.strftime(time_fmt),
        }

    def _custom_text(self, s, text):
        if not text:
            return None
        return {
            "value": text,
            "bold": _truthy(s.get("custom_text_sensor_bold"), True),
            "color": s.get("custom_text_sensor_color") or s.get("textColor") or "#000000",
            "size": s.get("custom_text_sensor_text_size") or "18",
        }

    def _parse_forecast(self, forecast, forecast_type, t, s):
        try:
            count = int(s.get("forecast_count") or (5 if forecast_type == "daily" else 12))
        except (TypeError, ValueError):
            count = 5 if forecast_type == "daily" else 12
        days_short = t.get("daysShort") or []
        out = []
        for e in forecast[:count]:
            dt = self._parse_dt(e.get("datetime"))
            if forecast_type == "daily":
                label = (days_short[dt.weekday()] if days_short else dt.strftime("%a")) if dt else ""
                is_day = True
            else:
                label = dt.strftime("%H:%M") if dt else ""
                is_day = self._is_day_at(dt)
            out.append({
                "time": label,
                "temperature": self._num(e.get("temperature")),
                "templow": self._num(e.get("templow")),
                "precipitation": self._num(e.get("precipitation")) or 0,
                "precipitation_probability": self._num(e.get("precipitation_probability")) or 0,
                "icon": self._icon(e.get("condition", ""), is_day, s),
            })
        return out

    def _sizes(self, s):
        """Per-element font sizes (px), with the card's e-ink-friendly defaults."""
        defaults = {
            "current_temp": 64, "condition": 34, "feels_like": 22, "description": 20,
            "attributes": 18, "title": 30, "day_date": 20, "time": 40,
            "forecast_icon": 36, "forecast_text": 16, "summary": 18,
        }
        out = {}
        for key, d in defaults.items():
            try:
                out[key] = int(s.get(f"{key}_size") or d)
            except (TypeError, ValueError):
                out[key] = d
        # Chart height grows with the forecast text/icon size so enlarging the forecast
        # gives the line and precipitation bars more room. The conditions now sit beside
        # the temperature (no centred band competes for height), so the forecast can be
        # tall: more vertical room for the temperature line and the precipitation bars.
        out["chart_height"] = max(130, (out["forecast_icon"] + out["forecast_text"]) * 2)
        return out

    # --- helpers --------------------------------------------------------------

    def _icon(self, condition, is_day, s):
        base = (ICON_NIGHT if not is_day else ICON_DAY).get(condition, "clear-day")
        svg = self.get_plugin_dir(f"wicons/{base}.svg")
        return svg if os.path.exists(svg) else self.get_plugin_dir("wicons/clear-day.svg")

    def _wind_dir(self, bearing, t):
        if bearing is None:
            return ""
        try:
            dirs = t.get("cardinalDirections")
            idx = round(float(bearing) / 22.5) % 16
            return dirs[idx] if dirs else ""
        except (TypeError, ValueError, IndexError):
            return ""

    def _get_sun(self, settings):
        """Day/night from HA's sun entity: its current state plus today's sunrise/sunset
        times (local), so icons follow the real sun rather than a fixed clock. Returns
        None if the entity is unavailable, and callers fall back to an hour heuristic."""
        ent = (settings.get("sun_entity") or "sun.sun").strip()
        try:
            st = self._get_state(ent)
        except RuntimeError as e:
            logger.warning(f"Sun entity '{ent}' unavailable, using hour heuristic: {e}")
            return None
        a = st.get("attributes", {})
        rising = self._parse_dt(a.get("next_rising"))
        setting = self._parse_dt(a.get("next_setting"))
        return {
            "up": st.get("state") == "above_horizon",
            "sunrise": rising.time() if rising else None,
            "sunset": setting.time() if setting else None,
        }

    def _is_day_now(self):
        """Is the sun up right now? Trust the sun entity's state when we have it."""
        sun = getattr(self, "_sun", None)
        if sun is not None:
            return sun["up"]
        return 6 <= datetime.now().hour < 19

    def _is_day_at(self, dt):
        """Is the given (local) datetime during daylight? Uses today's sunrise/sunset
        times when available, else the 6–19 fallback."""
        sun = getattr(self, "_sun", None)
        if sun and sun.get("sunrise") and sun.get("sunset") and dt:
            return sun["sunrise"] <= dt.time() < sun["sunset"]
        return 6 <= dt.hour < 19 if dt else True

    @staticmethod
    def _parse_dt(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _round(v):
        try:
            return round(float(v))
        except (TypeError, ValueError):
            return v if v is not None else "—"

    @staticmethod
    def _fmt(v, unit=""):
        if v in (None, "", "unknown", "unavailable"):
            return None
        try:
            return f"{round(float(v))}{unit}"
        except (TypeError, ValueError):
            return f"{v}{unit}"

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
