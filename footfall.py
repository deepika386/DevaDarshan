"""Footfall prediction (stage 3): hourly arrivals forecast with a Gradient Boosting model.

The synopsis notes that linear models struggle with non-linear festival spikes, so
this uses gradient-boosted trees over calendar + weather features.

Training data: no real attendance records exist for this prototype, so a SYNTHETIC
history (two years, generated from a documented demand pattern + noise) bootstraps
the model. The system then appends ACTUAL hourly arrivals recorded at the gate
(stage 8 feedback loop) and `retrain()` blends them in.
"""
import math
import os
import random
from datetime import date as Date, timedelta

import joblib
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score

import config
import db
import services

HOURS = list(range(int(config.TEMPLE["open"][:2]), int(config.TEMPLE["close"][:2])))  # 6..19
BASE_ARRIVALS = 250
# relative demand by hour (morning & evening puja peaks)
PROFILE = {6: .45, 7: .8, 8: 1.0, 9: .9, 10: .7, 11: .5, 12: .4, 13: .45, 14: .55,
           15: .7, 16: .9, 17: 1.0, 18: .95, 19: .55}
DOW = [0.8, 1.5, 0.8, 0.85, 1.5, 1.1, 1.35]  # Mon..Sun


def _true_demand(d, hour, rng=None):
    """Synthetic ground-truth demand used to build the bootstrap history."""
    info = services.day_info(d)
    w = services.training_weather(d)
    f = {0: 1.0, 1: 2.2, 2: 2.9}[info["level"]]
    if info["level"] == 0 and 0 <= info["days_to_festival"] <= 2:
        f = 1.25 if info["days_to_festival"] > 0 else 1.1
    rain = 1 - 0.05 * min(w["rain_mm"], 12) / 2
    season = 1.1 if d.month in (10, 11, 12, 1) else 1.0
    val = BASE_ARRIVALS * PROFILE[hour] * DOW[d.weekday()] * f * rain * season
    if rng:
        val *= math.exp(rng.gauss(0, 0.08))
    return max(0, round(val))


def features(d, hour, w=None):
    info = services.day_info(d)
    w = w or services.weather(d)
    return [hour, d.weekday(), d.month, int(info["is_weekend"]), info["level"],
            info["days_to_festival"], w["rain_mm"], w["temp_c"]]


FEATURE_NAMES = ["hour", "weekday", "month", "is_weekend", "festival_level", "days_to_festival", "rain_mm", "temp_c"]


def _synthetic_rows(end, days=730, seed=7):
    rng = random.Random(seed)
    X, y, meta = [], [], []
    for i in range(days):
        d = end - timedelta(days=days - i)
        for h in HOURS:
            X.append(features(d, h, services.training_weather(d)))
            y.append(_true_demand(d, h, rng))
    return X, y


_model = None
_info = {}


def train(include_actual=True):
    global _model, _info
    X, y = _synthetic_rows(Date(2026, 10, 1))
    n_syn = len(y)
    n_act = 0
    if include_actual:
        for r in db.q("SELECT visit_date,hour,arrivals FROM footfall_history WHERE source='actual'"):
            d = Date.fromisoformat(r["visit_date"])
            row = features(d, r["hour"], services.training_weather(d))
            for _ in range(5):               # real observations count more than synthetic ones
                X.append(row)
                y.append(r["arrivals"])
            n_act += 1
    X, y = np.array(X, float), np.array(y, float)
    idx = np.random.RandomState(0).permutation(len(y))
    cut = int(len(y) * 0.8)
    tr, te = idx[:cut], idx[cut:]
    m = GradientBoostingRegressor(n_estimators=250, max_depth=3, learning_rate=0.08, random_state=0)
    m.fit(X[tr], y[tr])
    pred = m.predict(X[te])
    _info = {"algorithm": "GradientBoostingRegressor", "train_rows": int(len(tr)),
             "synthetic_rows": n_syn, "actual_hours_used": n_act,
             "holdout_r2": round(float(r2_score(y[te], pred)), 3),
             "holdout_mae_devotees_per_hour": round(float(mean_absolute_error(y[te], pred)), 1),
             "note": "Bootstrapped on SYNTHETIC history; improves as real gate data accumulates."}
    m.fit(X, y)                              # final fit on everything
    _model = m
    joblib.dump((m, _info), config.MODEL_PATH)
    _cache.clear()
    return _info


def model():
    global _model, _info
    if _model is None:
        loaded = False
        if os.path.exists(config.MODEL_PATH):
            try:
                _model, _info = joblib.load(config.MODEL_PATH)
                loaded = True
            except Exception as e:   # e.g. saved with a different scikit-learn/numpy version on another machine
                print("Saved model unreadable here, retraining:", e)
        if not loaded:
            train(include_actual=False)
    return _model


def model_info():
    model()
    return _info


_cache = {}


def predict_day(d):
    """{hour: predicted arrivals} for date d."""
    key = d.isoformat()
    if key not in _cache:
        w = services.weather(d)
        X = np.array([features(d, h, w) for h in HOURS], float)
        p = model().predict(X)
        _cache[key] = {h: max(0, int(round(v))) for h, v in zip(HOURS, p)}
    return _cache[key]


def predict_hour(d, hour):
    return predict_day(d).get(hour, 0)


def forecast(d, days=14):
    """Daily summaries for the next `days` days (plan staffing / slot capacity)."""
    out = []
    for i in range(days):
        day = d + timedelta(days=i)
        hourly = predict_day(day)
        total = sum(hourly.values())
        peak_h = max(hourly, key=hourly.get)
        info = services.day_info(day)
        hourly_cap = config.SLOT_CAPACITY * 60 // config.SLOT_MIN
        out.append({
            "date": day.isoformat(), "weekday": info["weekday"], "festival": info["festival"],
            "predicted_total": total, "peak_hour": f"{peak_h:02d}:00", "peak_arrivals": hourly[peak_h],
            "overflow_hours": [f"{h:02d}:00" for h, v in hourly.items() if v > hourly_cap],
            "staff_needed_at_peak": math.ceil(hourly[peak_h] / 40),   # 1 volunteer per ~40 devotees/hour
            "demand_level": "very high" if hourly[peak_h] > hourly_cap else
                            "high" if hourly[peak_h] > 0.7 * hourly_cap else
                            "moderate" if hourly[peak_h] > 0.4 * hourly_cap else "low",
        })
    return out


def record_actuals(visit_date):
    """Stage 8 feedback: store real hourly arrivals counted at the gate."""
    rows = db.q("""SELECT substr(entry_at,12,2) AS h, SUM(party_size) AS n FROM bookings
                   WHERE visit_date=? AND entry_at IS NOT NULL GROUP BY h""", (visit_date,))
    for r in rows:
        db.x("INSERT INTO footfall_history VALUES(?,?,?,?) ON CONFLICT(visit_date,hour,source) "
             "DO UPDATE SET arrivals=excluded.arrivals", (visit_date, int(r["h"]), r["n"], "actual"))
    return len(rows)
