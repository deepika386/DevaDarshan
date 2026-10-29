"""Crowd simulator: plays out a temple day minute-by-minute against the real engine.

Used by (a) the admin "Demo controls" in the web app and (b) evaluate.py, which
compares a day with dynamic crowd control OFF vs ON. Simulated devotees are
bookings with source='sim'; their behaviour (when they reach the gate, how long
they stay) is a deterministic function of the booking id, so the simulation is
reproducible and needs no hidden state.
"""
import random
from datetime import date as Date, datetime, timedelta

import config
import db
import engine
import footfall

_plans = {}
_mult = {}          # per-booking dwell multiplier fixed at entry (crowding slows darshan)
SEED = 11


def _dwell_multiplier(date_s):
    """Assumption: the fuller the temple, the slower the darshan queue inside moves."""
    inside = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND status='inside'",
                   (date_s,))["n"]
    return 0.8 + 0.9 * min(1.3, inside / config.TEMPLE["max_inside"])


def plan(b):
    """(arrival_dt or None if no-show, dwell_minutes) for a simulated booking row."""
    p = _plans.get(b["id"])
    if p is None:
        r = random.Random(b["id"] * 7919 + SEED)
        u = r.random()
        if u < 0.05:
            off = None                                  # no-show
        elif u < 0.60:
            off = r.uniform(-5, 10)                     # punctual
        elif u < 0.90:
            off = r.uniform(0, config.SLOT_MIN)         # spread across the slot
        elif u < 0.95:
            off = r.uniform(-9, -3)                     # early
        else:
            off = r.uniform(10, 40)                     # late
        ss = engine.dt_of(b["visit_date"], b["slot_start"])
        arrival = None if off is None else ss + timedelta(minutes=off)
        p = (arrival, r.uniform(25, 60))
        _plans[b["id"]] = p
    return p


def seed_bookings(date_s, demand=1.0, now=None, seed=3):
    """Create simulated online bookings per slot from predicted demand * `demand`."""
    now = now or engine.get_now()
    rnd = random.Random(seed)
    d = Date.fromisoformat(date_s)
    pred = footfall.predict_day(d)
    n_people = n_book = 0
    for s in engine.slot_starts():
        if engine.slot_end_dt(date_s, s) <= now:
            continue
        target = min(engine.slot_cap(date_s, s), round(pred.get(int(s[:2]), 0) * config.SLOT_MIN / 60 * demand))
        got = 0
        while got < target:
            party = min(rnd.choices([1, 2, 3, 4, 5, 6], [18, 30, 22, 18, 7, 5])[0], target - got)
            try:
                engine.create_booking(f"Devotee {n_book + 1}", f"9{rnd.randint(0, 999999999):09d}", date_s, s, party,
                                      assist=rnd.random() < 0.06, source="sim", now=now)
            except engine.BookingError:
                break
            got += party
            n_people += party
            n_book += 1
    return {"bookings": n_book, "devotees": n_people}


def step_minute(now):
    """Advance simulated devotees by one minute at time `now` (clock must already be set)."""
    date_s = now.date().isoformat()
    # exits
    for r in db.q("SELECT * FROM bookings WHERE visit_date=? AND source='sim' AND status='inside'", (date_s,)):
        m = _mult.setdefault(r["id"], _dwell_multiplier(date_s))
        if datetime.strptime(r["entry_at"], engine.FMT) + timedelta(minutes=plan(r)[1] * m) <= now:
            engine.verify_exit(r["token"], now)
    # arrivals queue at the gate in FIFO order
    lo = (now - timedelta(minutes=120)).strftime("%H:%M")
    hi = (now + timedelta(minutes=12)).strftime("%H:%M")
    cand = []
    for r in db.q("SELECT * FROM bookings WHERE visit_date=? AND source='sim' AND status='booked' "
                  "AND slot_start>=? AND slot_start<=?", (date_s, lo, hi)):
        arr = plan(r)[0]
        if arr is not None and arr <= now:
            cand.append((arr, r["id"], r))
    cand.sort(key=lambda t: (t[0], t[1]))
    for arr, _, r in cand:
        if not r["arrived_at"]:
            db.x("UPDATE bookings SET arrived_at=? WHERE id=?", (engine.iso(arr), r["id"]))   # "I've reached the gate"
    for arr, _, r in cand:
        res = engine.verify_entry(r["token"], now)
        if res["decision"] == "WAIT":
            break


def advance(minutes, auto_tick=True):
    """Advance the simulated clock `minutes` (creating a sim clock from live time if needed)."""
    with db.LOCK:        # one coherent block: other requests wait instead of seeing half-simulated state
        now = engine.get_now()
        if not db.meta_get("sim_now"):
            engine.set_clock(now)
        for _ in range(int(minutes)):
            now += timedelta(minutes=1)
            engine.set_clock(now)
            step_minute(now)
            if auto_tick and now.minute % config.MONITOR_INTERVAL_MIN == 0:
                engine.tick(now)
        return now


def run_day(date_s, control_on=True, demand=1.0, seed=3, quiet=True):
    """Full-day simulation on a clean database. Returns metrics dict."""
    db.wipe_operational()
    engine.set_control_enabled(control_on)
    _plans.clear()
    _mult.clear()
    d = Date.fromisoformat(date_s)
    start = datetime.combine(d, datetime.min.time()) + timedelta(hours=5, minutes=30)
    engine.set_clock(start)
    seeded = seed_bookings(date_s, demand, now=start, seed=seed)
    now = start
    end = datetime.combine(d, datetime.min.time()) + timedelta(hours=int(config.TEMPLE["close"][:2]) + 2)
    cap = config.TEMPLE["max_inside"]
    peak = over = 0
    samples = []
    while now < end:
        now += timedelta(minutes=1)
        engine.set_clock(now)
        step_minute(now)
        if now.minute % config.MONITOR_INTERVAL_MIN == 0:
            engine.tick(now)
        inside = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND status='inside'",
                       (date_s,))["n"]
        samples.append(inside)
        peak = max(peak, inside)
        if inside > cap:
            over += 1
    # metrics
    holds, entered = [], 0
    for r in db.q("SELECT * FROM bookings WHERE visit_date=? AND source='sim' AND entry_at IS NOT NULL", (date_s,)):
        arr = plan(r)[0]
        holds.append(max(0.0, (datetime.strptime(r["entry_at"], engine.FMT) - arr).total_seconds() / 60))
        entered += r["party_size"]
    missed = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND status='no_show' "
                   "AND source='sim'", (date_s,))["n"]
    holds.sort()
    mean_hold = sum(holds) / len(holds) if holds else 0
    p95 = holds[int(len(holds) * .95) - 1] if holds else 0
    levels = db.q("SELECT level, COUNT(*) n FROM crowd_log WHERE visit_date=? GROUP BY level", (date_s,))
    return {
        "date": date_s, "control_on": control_on, "booked_devotees": seeded["devotees"], "entered": entered,
        "peak_inside": peak, "peak_pct_of_safe_capacity": round(100 * peak / cap),
        "minutes_over_safe_capacity": over, "mean_gate_hold_min": round(mean_hold, 1), "p95_gate_hold_min": round(p95, 1),
        "max_gate_hold_min": round(holds[-1], 1) if holds else 0, "no_show_devotees": missed,
        "control_levels_5min_samples": {r["level"]: r["n"] for r in levels},
    }
