"""Panchanga (Hindu calendar) facts, computed from astronomy -- no external API, no keys.

Tithi = which 12-degree step the Moon is ahead of the Sun (30 tithis per lunar month).
The tithi *of a day* is the one running at local sunrise (fixed here at ~06:15 IST for Bengaluru).

ACCURACY: low-precision sun/moon formulas (Meeus-style truncated series, moon error ~0.3 degrees,
i.e. boundaries can be off by roughly an hour). A tithi that ends within ~1h of sunrise may be
reported as the neighbouring tithi. Always confirm festival dates with the temple's published
panchanga. Tithi is shown for information only: the footfall model does NOT use it as an input.
"""
import math
from datetime import date as Date, datetime, timedelta, timezone

import services

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
SUNRISE = (6, 15)
J2000 = datetime(2000, 1, 1, 12, tzinfo=UTC)

NAMES = ["Prathama", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashti", "Saptami", "Ashtami", "Navami",
         "Dashami", "Ekadashi", "Dwadashi", "Trayodashi", "Chaturdashi"]
# General tradition at Devi temples (not temple-specific): these tithis draw extra devotees.
SPECIAL = {"Ashtami", "Navami", "Purnima", "Amavasya"}


def _sin(deg):
    return math.sin(math.radians(deg))


def elongation(when_utc):
    """Moon longitude minus Sun longitude, degrees 0-360."""
    d = (when_utc - J2000).total_seconds() / 86400.0
    L = 218.316 + 13.176396 * d          # moon mean longitude
    Mm = 134.963 + 13.064993 * d         # moon mean anomaly
    M = 357.529 + 0.98560028 * d         # sun mean anomaly
    D = 297.850 + 12.190749 * d          # mean elongation
    F = 93.272 + 13.229350 * d           # argument of latitude
    moon = (L + 6.289 * _sin(Mm) + 1.274 * _sin(2 * D - Mm) + 0.658 * _sin(2 * D) + 0.214 * _sin(2 * Mm)
            - 0.186 * _sin(M) - 0.114 * _sin(2 * F) + 0.058 * _sin(2 * D - 2 * Mm) + 0.057 * _sin(2 * D - M - Mm)
            + 0.053 * _sin(2 * D + Mm) + 0.046 * _sin(2 * D - M) + 0.041 * _sin(Mm - M) - 0.035 * _sin(D)
            - 0.031 * _sin(Mm + M))
    sun = 280.460 + 0.9856474 * d + 1.915 * _sin(M) + 0.020 * _sin(2 * M)
    return (moon - sun) % 360


def tithi_number(when_utc):
    return int(elongation(when_utc) // 12) + 1      # 1..30


def _label(n):
    paksha = "Shukla" if n <= 15 else "Krishna"
    k = (n - 1) % 15 + 1
    name = NAMES[k - 1] if k < 15 else ("Purnima" if n == 15 else "Amavasya")
    return paksha, name, k


def compute(d):
    """Tithi running at sunrise on date `d`, plus approximate end time."""
    t0 = datetime(d.year, d.month, d.day, SUNRISE[0], SUNRISE[1], tzinfo=IST).astimezone(UTC)
    n = tithi_number(t0)
    paksha, name, k = _label(n)
    end = None
    t = t0
    for _ in range(40 * 6):                         # scan up to 40 h in 10-minute steps
        t2 = t + timedelta(minutes=10)
        if tithi_number(t2) != n:
            lo, hi = t, t2
            for _ in range(10):                     # bisect to ~1 s
                mid = lo + (hi - lo) / 2
                lo, hi = (mid, hi) if tithi_number(mid) == n else (lo, mid)
            end = hi.astimezone(IST)
            break
        t = t2
    nxt = _label(n % 30 + 1)
    return {"number": n, "paksha": paksha, "name": name, "number_in_paksha": k,
            "label": f"{paksha} {name}", "special": name in SPECIAL,
            "next": f"{nxt[0]} {nxt[1]}",
            "ends_at": end.strftime("%Y-%m-%d %H:%M") if end else None,
            "evaluated_at": f"{d.isoformat()} {SUNRISE[0]:02d}:{SUNRISE[1]:02d} IST (sunrise, approx.)"}


def describe(d):
    """Everything the UI shows for one date: tithi + festival context + honest crowd reasons."""
    info = services.day_info(d)
    w = services.weather(d)
    t = compute(d)
    reasons = []
    if info["festival"]:
        reasons.append(("Major festival" if info["level"] == 2 else "Festival") + f": {info['festival']}")
    elif 1 <= info["days_to_festival"] <= 2:
        reasons.append(f"Festival in {info['days_to_festival']} day(s): the model expects a pre-festival rise")
    if info["is_weekend"]:
        reasons.append(f"{info['weekday']}: weekend (model: higher footfall)")
    elif info["special_day"]:
        reasons.append(f"{info['weekday']}: traditionally busy at Amma temples (model: higher footfall)")
    if w["rain_mm"] >= 4:
        reasons.append(f"Rain expected ({w['rain_mm']:.0f} mm): model lowers footfall slightly")
    if d.month in (10, 11, 12, 1):
        reasons.append("Oct-Jan pilgrimage season (model: slightly higher footfall)")
    if not reasons:
        reasons.append("Ordinary day: no festival, weekend or special-day factor")
    note = None
    if t["special"]:
        note = f"{t['name']} is commonly observed as a special day at Devi temples (general tradition, not an input to the model)."
    return {"date": d.isoformat(), "weekday": info["weekday"], "tithi": t, "festival": info["festival"],
            "festival_level": info["level"], "days_to_festival": info["days_to_festival"],
            "special_temple_day": info["special_day"], "tithi_note": note, "crowd_factors": reasons,
            "weather": w,
            "accuracy_note": "Tithi is computed from sun/moon positions (approximate, +/- ~1 hour at boundaries). "
                             "Confirm festival dates with the temple."}
