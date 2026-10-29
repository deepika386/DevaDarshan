"""External-service layer: calendar, weather, payment, notifications.

Weather uses the free Open-Meteo API when reachable and silently falls back to a
deterministic seasonal estimate (so the prototype also runs offline).
Payment and SMS/e-mail/push are mocked behind small functions -- swap in a real
gateway (Razorpay etc.) and SMS provider without touching the engine.
"""
import hashlib
import json
import math
import random
from datetime import date as Date, datetime
from zoneinfo import ZoneInfo

import config
import db

# ---------------------------------------------------------------- calendar --
_fest = None


def festivals():
    global _fest
    if _fest is None:
        with open(config.FESTIVALS_PATH) as f:
            _fest = {r["date"]: r for r in json.load(f)["festivals"]}
    return _fest


def day_info(d):
    """Calendar facts for date `d` (datetime.date)."""
    f = festivals().get(d.isoformat())
    nearest = None
    for ds in festivals():
        delta = (Date.fromisoformat(ds) - d).days
        if -2 <= delta <= 30 and (nearest is None or abs(delta) < abs(nearest)):
            nearest = delta
    return {
        "festival": f["name"] if f else None,
        "level": f["level"] if f else 0,
        "days_to_festival": 30 if nearest is None else max(nearest, -2),
        "weekday": d.strftime("%A"),
        "is_weekend": d.weekday() >= 5,
        "special_day": d.weekday() in (1, 4),   # Tuesdays & Fridays are traditionally busy at Amma temples
    }


# ----------------------------------------------------------------- weather --
def _fallback_weather(d):
    """Deterministic seasonal Bengaluru estimate (no network)."""
    rnd = random.Random(int(hashlib.md5(d.isoformat().encode()).hexdigest()[:8], 16))
    m = d.month
    rain_p = {1: .03, 2: .03, 3: .06, 4: .18, 5: .35, 6: .40, 7: .45, 8: .50, 9: .55, 10: .50, 11: .30, 12: .08}[m]
    rain = round(rnd.expovariate(1 / 8), 1) if rnd.random() < rain_p else 0.0
    temp = 27 + 5 * math.sin((m - 2) / 12 * 2 * math.pi) + rnd.uniform(-2, 2)
    return {"rain_mm": min(rain, 40.0), "temp_c": round(temp, 1), "source": "seasonal-estimate"}


_wcache = {}


def weather(d):
    key = d.isoformat()
    if key in _wcache:
        return _wcache[key]
    out = _fallback_weather(d)
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    if 0 <= (d - today).days <= 14:
        try:
            import requests
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": config.TEMPLE["lat"], "longitude": config.TEMPLE["lon"],
                        "daily": "precipitation_sum,temperature_2m_max", "timezone": config.TIMEZONE,
                        "start_date": key, "end_date": key},
                timeout=3)
            j = r.json()["daily"]
            out = {"rain_mm": float(j["precipitation_sum"][0] or 0), "temp_c": float(j["temperature_2m_max"][0]),
                   "source": "open-meteo"}
        except Exception:
            pass
    _wcache[key] = out
    return out


def training_weather(d):
    """Always the deterministic estimate, so training data is reproducible."""
    return _fallback_weather(d)


# ----------------------------------------------------------------- payment --
def charge(amount, name):
    """Mock payment gateway. Returns (ok, reference)."""
    if amount <= 0:
        return True, "FREE"
    ref = "PAY-" + hashlib.sha1(f"{name}{amount}{random.random()}".encode()).hexdigest()[:10].upper()
    return True, ref


# ----------------------------------------------------------- notifications --
def notify(booking_id, message, channels=("sms", "push"), ts=None):
    """Mock SMS / e-mail / push: stored in DB (visible in the app) instead of sent."""
    ts = ts or datetime.now().isoformat(timespec="seconds")
    for ch in channels:
        db.x("INSERT INTO notifications(booking_id,ts,channel,message) VALUES(?,?,?,?)",
             (booking_id, ts, ch, message))
