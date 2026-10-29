"""DevaDarshan core engine: the eight stages of the workflow.

 1 User booking              -> create_booking(), availability()
 2 Smart queue allocation    -> queue number + est_entry()
 3 Footfall prediction       -> footfall.py (used in score, alerts, availability)
 4 Real-time monitoring      -> monitor()
 5 Dynamic crowd control     -> compute_score(), control_tick()
 6 QR verification           -> verify_entry()
 7 Smart queue update        -> queue_status(), notifications, tick()
 8 Darshan & exit            -> verify_exit(), footfall.record_actuals()
"""
import hashlib
import hmac
import math
from datetime import date as Date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db
import footfall
import services

T = config.TEMPLE
TOTAL_SLOT_HOURS = [int(T["open"][:2]), int(T["close"][:2])]
FMT = "%Y-%m-%dT%H:%M:%S"


class BookingError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg, self.code = msg, code


# ------------------------------------------------------------------- clock --
def real_now():
    return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None, microsecond=0)


def get_now():
    s = db.meta_get("sim_now")
    return datetime.strptime(s, FMT) if s else real_now()


def set_clock(dt=None):
    """dt=None -> live clock; else simulated clock at dt."""
    db.meta_set("sim_now", dt.strftime(FMT) if dt else None)


def clock_info():
    return {"now": get_now().strftime(FMT), "mode": "simulated" if db.meta_get("sim_now") else "live"}


def control_enabled():
    return db.meta_get("control_enabled", "1") == "1"


def set_control_enabled(flag):
    db.meta_set("control_enabled", "1" if flag else "0")


# ----------------------------------------------------------------- helpers --
def dt_of(date_s, hhmm):
    return datetime.strptime(f"{date_s} {hhmm}", "%Y-%m-%d %H:%M")


def iso(d):
    return d.strftime(FMT) if d else None


def hhmm(d):
    return d.strftime("%H:%M")


def slot_starts():
    h0, h1 = TOTAL_SLOT_HOURS
    out, t = [], h0 * 60
    while t < h1 * 60:
        out.append(f"{t // 60:02d}:{t % 60:02d}")
        t += config.SLOT_MIN
    return out


def slot_end_dt(date_s, slot):
    return dt_of(date_s, slot) + timedelta(minutes=config.SLOT_MIN)


def _sign(bid, visit_date, slot, phone):
    msg = f"{bid}|{visit_date}|{slot}|{phone}".encode()
    return hmac.new(config.SECRET, msg, hashlib.sha256).hexdigest()[:12]


def parse_token(token):
    """Return the booking row if the token is well-formed and its signature is valid."""
    try:
        bid_s, sig = str(token).strip().split("-", 1)
        b = db.q1("SELECT * FROM bookings WHERE id=?", (int(bid_s),))
    except Exception:
        return None
    if not b:
        return None
    ok = hmac.compare_digest(sig, _sign(b["id"], b["visit_date"], b["slot_start"], b["phone"]))
    return b if ok and b["token"] == token else None


def _booking_dict(b, now=None, with_queue=False):
    d = dict(b)
    d["slot_end"] = hhmm(slot_end_dt(b["visit_date"], b["slot_start"]))
    if with_queue:
        d["queue"] = queue_status(b, now)
    return d


# ---------------------------------------------------------- control state ---
def get_cs(date_s, now=None):
    r = db.q1("SELECT * FROM control_state WHERE visit_date=?", (date_s,))
    if not r:
        db.x("INSERT INTO control_state(visit_date,tokens,tokens_ts) VALUES(?,?,?)",
             (date_s, config.BATCH_SIZE, iso(now or get_now())))
        r = db.q1("SELECT * FROM control_state WHERE visit_date=?", (date_s,))
    return r


def _gate_rate(cs):
    """Devotees admitted per minute under the current control decision."""
    if not control_enabled():
        return float("inf")
    if cs["paused"]:
        return 0.0
    return config.BATCH_SIZE / (config.BATCH_INTERVAL_MIN + cs["extra_min"])


def _refill(date_s, now):
    cs = get_cs(date_s, now)
    last = datetime.strptime(cs["tokens_ts"], FMT) if cs["tokens_ts"] else now
    mins = max(0.0, (now - last).total_seconds() / 60)
    rate = _gate_rate(cs)
    tokens = config.BATCH_SIZE if rate == float("inf") else min(config.BATCH_SIZE, cs["tokens"] + mins * rate)
    db.x("UPDATE control_state SET tokens=?, tokens_ts=? WHERE visit_date=?", (tokens, iso(now), date_s))
    return tokens, rate


# ------------------------------------------------- stage 1: availability ----
def _valid_date(date_s, now):
    try:
        d = Date.fromisoformat(date_s)
    except Exception:
        raise BookingError("Invalid date")
    if d < now.date():
        raise BookingError("That date has passed")
    if d > now.date() + timedelta(days=14):
        raise BookingError("Bookings open up to 14 days ahead")
    return d


def slot_cap(date_s, slot):
    """Bookable devotees for a slot. The footfall forecast lowers it on predicted-overflow hours
    so total inflow stays near what the premises can safely hold (stage 3: capacity planning)."""
    pred = footfall.predict_hour(Date.fromisoformat(date_s), int(slot[:2])) * config.SLOT_MIN / 60
    ratio = pred / config.SLOT_CAPACITY
    for lim, factor in config.CAPACITY_TIERS:
        if ratio >= lim:
            return round(config.SLOT_CAPACITY * factor)
    return config.SLOT_CAPACITY


def availability(date_s, now=None):
    now = now or get_now()
    d = _valid_date(date_s, now)
    pred = footfall.predict_day(d)
    info = services.day_info(d)
    booked = {r["slot_start"]: r["n"] for r in db.q(
        "SELECT slot_start, SUM(party_size) n FROM bookings WHERE visit_date=? AND status!='cancelled' "
        "GROUP BY slot_start", (date_s,))}
    slots = []
    for s in slot_starts():
        n = booked.get(s, 0)
        cap_s = slot_cap(date_s, s)
        remaining = max(0, cap_s - n)
        ended = slot_end_dt(date_s, s) <= now
        expected = pred.get(int(s[:2]), 0) * config.SLOT_MIN / 60
        ratio = expected / config.SLOT_CAPACITY
        busy = "very busy" if ratio >= 0.9 else "busy" if ratio >= 0.6 else "moderate" if ratio >= 0.35 else "quiet"
        slots.append({"start": s, "end": hhmm(slot_end_dt(date_s, s)), "capacity": cap_s,
                      "reduced": cap_s < config.SLOT_CAPACITY, "booked": n, "remaining": remaining,
                      "status": "closed" if ended else "full" if remaining == 0 else "open",
                      "expected_demand": round(expected), "busy": busy})
    quiet = sorted([s for s in slots if s["status"] == "open"], key=lambda s: s["expected_demand"])[:3]
    return {"date": date_s, "temple": T["name"], "festival": info["festival"], "weekday": info["weekday"],
            "weather": services.weather(d), "slots": slots,
            "suggested_quiet_slots": [s["start"] for s in quiet],
            "fee_per_person": config.FEE_PER_PERSON, "max_party": config.MAX_PARTY}


# ---------------------------------------------------- stage 1+2: booking ----
def create_booking(name, phone, date_s, slot, party=1, email=None, assist=False, source="app", now=None):
    now = now or get_now()
    name, phone = (name or "").strip(), "".join(c for c in str(phone or "") if c.isdigit())
    if not name:
        raise BookingError("Name is required")
    if len(phone) != 10:
        raise BookingError("Enter a valid 10-digit mobile number")
    try:
        party = int(party)
    except Exception:
        raise BookingError("Invalid party size")
    if not 1 <= party <= config.MAX_PARTY:
        raise BookingError(f"Party size must be 1-{config.MAX_PARTY}")
    _valid_date(date_s, now)
    if slot not in slot_starts():
        raise BookingError("Invalid slot")
    if slot_end_dt(date_s, slot) <= now:
        raise BookingError("That slot has already ended")

    with db.LOCK:
        db.x("BEGIN IMMEDIATE")
        try:
            used = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND slot_start=? "
                         "AND status!='cancelled'", (date_s, slot))["n"]
            cap_s = slot_cap(date_s, slot)
            if used + party > cap_s:
                raise BookingError(f"Only {max(0, cap_s - used)} places left in this slot", 409)
            amount = config.FEE_PER_PERSON * party
            ok, ref = services.charge(amount, name)
            if not ok:
                raise BookingError("Payment failed", 402)
            # Queue numbers follow serving order: each slot owns a block of SLOT_CAPACITY numbers, and a
            # booking takes the next numbers in its slot's block (so "now serving #N" always moves forward).
            used_all = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND slot_start=?",
                             (date_s, slot))["n"]
            if used_all + party > config.SLOT_CAPACITY:
                raise BookingError("This slot is closed to new bookings", 409)
            qn = slot_starts().index(slot) * config.SLOT_CAPACITY + used_all + 1
            cur = db.x("""INSERT INTO bookings(name,phone,email,visit_date,slot_start,party_size,assist,queue_no,token,
                          amount,payment_ref,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (name, phone, email, date_s, slot, party, int(bool(assist)), qn, "", amount, ref, source,
                        iso(now)))
            bid = cur.lastrowid
            token = f"{bid}-{_sign(bid, date_s, slot, phone)}"
            db.x("UPDATE bookings SET token=? WHERE id=?", (token, bid))
            db.x("COMMIT")
        except BaseException:
            db.x("ROLLBACK")
            raise
    b = db.q1("SELECT * FROM bookings WHERE id=?", (bid,))
    est = est_entry(b, now)
    if source == "app":
        services.notify(bid, f"Booking confirmed for {date_s}, slot {slot}-{hhmm(slot_end_dt(date_s, slot))}. "
                             f"Queue #{qn}, estimated entry {hhmm(est)}. Show your QR at the gate.", ts=iso(now))
    return _booking_dict(b, now, with_queue=True)


def cancel_booking(token, now=None):
    now = now or get_now()
    b = parse_token(token)
    if not b:
        raise BookingError("Ticket not found", 404)
    if b["status"] != "booked" or dt_of(b["visit_date"], b["slot_start"]) <= now:
        raise BookingError("This booking can no longer be cancelled", 409)
    db.x("UPDATE bookings SET status='cancelled' WHERE id=?", (b["id"],))
    services.notify(b["id"], "Your booking was cancelled.", ts=iso(now))
    return {"ok": True}


# -------------------------------------------- stage 2/7: queue + estimate ---
def _still_valid_sql():
    return "status='booked'"


def est_entry(b, now=None):
    """Estimated gate-entry time for booking row `b`, given the live gate pace."""
    now = now or get_now()
    cs = get_cs(b["visit_date"], now)
    ss = dt_of(b["visit_date"], b["slot_start"])
    extra = cs["extra_min"] if control_enabled() else 0
    paused = bool(cs["paused"]) and control_enabled()
    is_today = b["visit_date"] == now.date().isoformat()
    if is_today and ss <= now:
        if b["status"] != "booked":
            return now
        rows = db.q("SELECT slot_start, queue_no, party_size, arrived_at FROM bookings WHERE visit_date=? "
                    "AND status='booked' AND (slot_start<? OR (slot_start=? AND queue_no<?))",
                    (b["visit_date"], b["slot_start"], b["slot_start"], b["queue_no"]))
        ahead = 0
        for r in rows:  # ignore people whose window has already lapsed (unless already waiting at the gate)
            if r["arrived_at"] or slot_end_dt(b["visit_date"], r["slot_start"]) + timedelta(
                    minutes=config.GRACE_LATE_MIN) >= now:
                ahead += r["party_size"]
        rate = config.BATCH_SIZE / (config.BATCH_INTERVAL_MIN + extra)
        wait = ahead / rate
        if paused:
            wait = max(wait, 15)
        return now + timedelta(minutes=wait)
    cum = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND slot_start=? "
                "AND queue_no<=? AND status!='cancelled'", (b["visit_date"], b["slot_start"], b["queue_no"]))["n"]
    idx = max(0, math.ceil(cum / config.BATCH_SIZE) - 1)
    soon = is_today and (ss - now) <= timedelta(minutes=60)
    return ss + timedelta(minutes=idx * (config.BATCH_INTERVAL_MIN + (extra if soon else 0)))


def queue_status(b, now=None):
    """Stage 7: what a devotee sees -- serving number, position, ETA, alerts."""
    now = now or get_now()
    b = db.q1("SELECT * FROM bookings WHERE id=?", (b["id"],))
    date_s = b["visit_date"]
    ss = dt_of(date_s, b["slot_start"])
    se = ss + timedelta(minutes=config.SLOT_MIN)
    est = est_entry(b, now)
    cs = get_cs(date_s, now)
    last = db.q1("SELECT queue_no FROM bookings WHERE visit_date=? AND entry_at IS NOT NULL "
                 "ORDER BY entry_at DESC, id DESC LIMIT 1", (date_s,))
    waiting = db.q("SELECT queue_no, slot_start, party_size FROM bookings WHERE visit_date=? AND status='booked' "
                   "ORDER BY slot_start, queue_no", (date_s,))
    mine_idx = next((i for i, r in enumerate(waiting) if r["queue_no"] == b["queue_no"]), None)
    serving = last["queue_no"] if last else 0
    nxt = next((r["queue_no"] for r in waiting if r["queue_no"] > serving), waiting[0]["queue_no"] if waiting else None)
    ahead_people = sum(r["party_size"] for r in waiting[:mine_idx]) if mine_idx is not None else 0
    ctrl_on = control_enabled()
    held = ctrl_on and (cs["paused"] or cs["extra_min"] > 0) and date_s == now.date().isoformat()
    window_open = ss - timedelta(minutes=config.GRACE_EARLY_MIN)
    window_close = se + timedelta(minutes=config.GRACE_LATE_MIN)

    if b["status"] == "inside":
        state, msg = "inside", "You are inside. Enjoy your darshan. Scan your QR at the gate when leaving."
    elif b["status"] == "exited":
        state, msg = "done", "Darshan complete. Thank you! Your visit helps us predict crowds better."
    elif b["status"] == "cancelled":
        state, msg = "cancelled", "This booking was cancelled."
    elif b["status"] == "no_show" or (date_s < now.date().isoformat()) or now > window_close:
        state, msg = "missed", "Your time window has passed. Please book a new slot."
    elif now < ss - timedelta(minutes=45) or date_s > now.date().isoformat():
        state, msg = "upcoming", f"Your slot starts at {hhmm(ss)}. We'll remind you before it's time."
    elif cs["paused"] and ctrl_on:
        state, msg = "held", "Entry is briefly paused to keep the temple safe. Please wait nearby; we will alert you."
    elif est <= now + timedelta(minutes=2) and now >= window_open:
        state, msg = "proceed", "It's your turn! Please proceed to the gate and scan your QR."
    elif now < window_open:
        state, msg = "get_ready", f"Please start towards the temple. Gate opens for you at {hhmm(window_open)}."
    else:
        state, msg = "waiting", f"Estimated entry {hhmm(est)}. " + \
            (f"Crowd control is adding about {cs['extra_min']} min between batches." if held else "")
    return {
        "state": state, "message": msg.strip(), "queue_no": b["queue_no"],
        "currently_serving": last["queue_no"] if last else None,
        "next_in_queue": nxt, "people_ahead": ahead_people,
        "est_entry": iso(est), "est_entry_text": hhmm(est),
        "mins_to_entry": max(0, round((est - now).total_seconds() / 60)),
        "window_open": hhmm(window_open), "window_close": hhmm(window_close),
        "crowd_level": cs["level"], "control_action": cs["action"] if ctrl_on else "Monitoring only",
        "delay_minutes": cs["extra_min"] if ctrl_on else 0,
    }


def ticket(token, now=None):
    b = parse_token(token)
    if not b:
        raise BookingError("Ticket not found", 404)
    notes = db.q("SELECT ts, channel, message FROM notifications WHERE booking_id=? AND channel='push' "
                 "ORDER BY id DESC LIMIT 8", (b["id"],))
    d = _booking_dict(b, now, with_queue=True)
    d["notifications"] = [dict(n) for n in notes]
    # privacy: a token holder does not need the e-mail, payment reference or full phone number
    for k in ("email", "payment_ref", "reminded"):
        d.pop(k, None)
    d["phone"] = "XXXXXX" + str(b["phone"])[-4:]
    return d


def lookup_by_phone(phone, name):
    """Find tickets by mobile number AND name (no OTP in this prototype, so both must match)."""
    phone = "".join(c for c in str(phone) if c.isdigit())
    name = (name or "").strip().lower()
    if len(phone) != 10 or not name:
        raise BookingError("Enter both the 10-digit mobile number and the name used for booking")
    rows = db.q("SELECT * FROM bookings WHERE phone=? AND lower(name)=? AND status!='cancelled' "
                "ORDER BY visit_date DESC, slot_start DESC LIMIT 10", (phone, name))
    return [{"token": r["token"], "visit_date": r["visit_date"], "slot_start": r["slot_start"],
             "queue_no": r["queue_no"], "status": r["status"], "name": r["name"]} for r in rows]


# ----------------------------------------------- stage 4: live monitoring ---
def _level_for_pct(p):
    return "Comfortable" if p < .5 else "Busy" if p < .75 else "Crowded" if p < .9 else "Overcrowded"


def monitor(date_s=None, now=None):
    now = now or get_now()
    date_s = date_s or now.date().isoformat()
    rows = db.q("SELECT party_size, entry_at FROM bookings WHERE visit_date=? AND status='inside'", (date_s,))
    inside = sum(r["party_size"] for r in rows)
    cap = T["max_inside"]
    zones = []
    for name, lo, hi, share in config.ZONES:
        n = 0
        for r in rows:
            mins = (now - datetime.strptime(r["entry_at"], FMT)).total_seconds() / 60
            if lo <= mins < hi:
                n += r["party_size"]
        zc = max(1, round(cap * share))
        zones.append({"zone": name, "count": n, "capacity": zc, "pct": round(n / zc, 3),
                      "level": _level_for_pct(n / zc)})
    t15 = iso(now - timedelta(minutes=15))
    ent = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND entry_at>=?", (date_s, t15))["n"]
    ex = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND exit_at>=?", (date_s, t15))["n"]
    totals = db.q1("SELECT COALESCE(SUM(CASE WHEN entry_at IS NOT NULL THEN party_size END),0) entered, "
                   "COALESCE(SUM(CASE WHEN exit_at IS NOT NULL THEN party_size END),0) exited FROM bookings "
                   "WHERE visit_date=?", (date_s,))
    return {"as_of": iso(now), "inside": inside, "capacity": cap, "pct": round(inside / cap, 3),
            "level": _level_for_pct(inside / cap), "entries_15m": ent, "exits_15m": ex,
            "entered_today": totals["entered"], "exited_today": totals["exited"], "zones": zones}


# ------------------------------------------- stage 5: dynamic crowd control --
def _mean_dwell(date_s):
    rows = db.q("SELECT entry_at, exit_at FROM bookings WHERE visit_date=? AND exit_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 200", (date_s,))
    if len(rows) < 20:
        return config.DEFAULT_DWELL_MIN
    tot = sum((datetime.strptime(r["exit_at"], FMT) - datetime.strptime(r["entry_at"], FMT)).total_seconds() / 60
              for r in rows)
    return max(15.0, tot / len(rows))


def compute_score(date_s, now=None):
    """Crowd Load Score 0-100 = weighted blend of three load ratios (stage 5):
         current    occupancy now / safe capacity
         booked     projected occupancy in 30 min from live bookings
         predicted  projected occupancy in 30 min from the ML footfall forecast
       'Projected' = people still expected inside + people expected to arrive, so the score
       reacts *before* the temple fills, but is not inflated when it is still mostly empty."""
    now = now or get_now()
    d = Date.fromisoformat(date_s)
    cap = T["max_inside"]
    inside = db.q1("SELECT COALESCE(SUM(party_size),0) n FROM bookings WHERE visit_date=? AND status='inside'",
                   (date_s,))["n"]
    stay = inside * (1 - min(1.0, 30 / _mean_dwell(date_s)))     # those still inside in 30 min
    arriving = 0
    for r in db.q("SELECT slot_start, party_size, arrived_at FROM bookings WHERE visit_date=? AND status='booked'",
                  (date_s,)):
        ss = dt_of(date_s, r["slot_start"])
        due = ss <= now + timedelta(minutes=30)
        live = r["arrived_at"] or ss + timedelta(minutes=config.SLOT_MIN + config.GRACE_LATE_MIN) >= now
        if due and live:
            arriving += r["party_size"]
    frac = now.minute / 60
    pred_hr = footfall.predict_hour(d, now.hour) * (1 - frac) + footfall.predict_hour(d, now.hour + 1) * frac
    gate_30 = 30 * config.BATCH_SIZE / config.BATCH_INTERVAL_MIN        # most the gate can pass in 30 min
    comp = {"current": min(1.5, inside / cap),
            "booked": min(1.5, (stay + min(0.95 * arriving, gate_30)) / cap),
            "predicted": min(1.5, (stay + min(pred_hr / 2, gate_30)) / cap)}
    w = config.SCORE_WEIGHTS
    score = 100 * min(1.0, sum(w[k] * comp[k] for k in comp))
    return round(score, 1), comp, {"inside": inside, "booked_next_30min": arriving,
                                   "predicted_per_hour": round(pred_hr)}


def _level_for_score(score):
    for ub, name, extra, paused in config.CONTROL_LEVELS:
        if score < ub:
            return name, extra, paused
    return config.CONTROL_LEVELS[-1][1:]


ACTIONS = {"Low": "Open normally", "Moderate": "Slow down: +3 min between batches",
           "High": "Delay next batch by 10 minutes", "Critical": "Pause new entries until the crowd eases"}
ORDER = ["Low", "Moderate", "High", "Critical"]


def control_tick(date_s, now=None):
    now = now or get_now()
    cs = get_cs(date_s, now)
    score, comp, raw = compute_score(date_s, now)
    level, extra, paused = _level_for_score(score)
    # hysteresis: step down only when clearly below the boundary (avoids flapping)
    cur = cs["level"]
    if ORDER.index(level) < ORDER.index(cur):
        bound = next(ub for ub, n, _, _ in config.CONTROL_LEVELS if n == cur)
        lower = [ub for ub, n, _, _ in config.CONTROL_LEVELS][ORDER.index(cur) - 1]
        if score > lower - 4:
            level, extra, paused = next((n, e, p) for _, n, e, p in config.CONTROL_LEVELS if n == cur)
    _refill(date_s, now)  # settle tokens at the old pace before changing it
    db.x("UPDATE control_state SET extra_min=?, paused=?, level=?, score=?, action=?, updated_at=? WHERE visit_date=?",
         (extra, int(paused), level, score, ACTIONS[level], iso(now), date_s))
    if ORDER.index(level) > ORDER.index(cs["notified_level"]) and control_enabled() and level != "Low":
        _notify_delay(date_s, level, extra, paused, now)
    if level == "Low" or ORDER.index(level) < ORDER.index(cs["notified_level"]):
        db.x("UPDATE control_state SET notified_level=? WHERE visit_date=?", (level, date_s))
    return {"score": score, "components": comp, "raw": raw, "level": level, "extra_min": extra,
            "paused": bool(paused), "action": ACTIONS[level] if control_enabled() else "Monitoring only"}


def _notify_delay(date_s, level, extra, paused, now):
    db.x("UPDATE control_state SET notified_level=? WHERE visit_date=?", (level, date_s))
    msg_tail = "Entry is paused briefly." if paused else f"Entry may be delayed by about {extra} minutes per batch."
    for r in db.q("SELECT id, slot_start FROM bookings WHERE visit_date=? AND status='booked'", (date_s,)):
        ss = dt_of(date_s, r["slot_start"])
        if now - timedelta(minutes=config.SLOT_MIN) <= ss <= now + timedelta(minutes=90):
            services.notify(r["id"], f"Crowd is {level.lower()} right now. {msg_tail} Your new estimate is in the app.",
                            ts=iso(now))


# ------------------------------------------------------------------- tick ----
def tick(now=None, force_log=False):
    """Run on a timer (every few minutes): expire no-shows, update control, remind, log snapshot."""
    now = now or get_now()
    date_s = now.date().isoformat()
    with db.LOCK:
        for r in db.q("SELECT id, visit_date, slot_start FROM bookings WHERE status='booked' AND visit_date<=? "
                      "AND arrived_at IS NULL", (date_s,)):
            if slot_end_dt(r["visit_date"], r["slot_start"]) + timedelta(minutes=config.GRACE_LATE_MIN) < now:
                db.x("UPDATE bookings SET status='no_show' WHERE id=?", (r["id"],))
        ctrl = control_tick(date_s, now)
        for r in db.q("SELECT id, slot_start FROM bookings WHERE visit_date=? AND status='booked' AND reminded=0",
                      (date_s,)):
            ss = dt_of(date_s, r["slot_start"])
            if timedelta(0) <= ss - now <= timedelta(minutes=30):
                services.notify(r["id"], f"Reminder: your darshan slot starts at {hhmm(ss)}. Please head to the gate.",
                                ts=iso(now))
                db.x("UPDATE bookings SET reminded=1 WHERE id=?", (r["id"],))
        mon = monitor(date_s, now)
        last = db.q1("SELECT ts FROM crowd_log WHERE visit_date=? ORDER BY id DESC LIMIT 1", (date_s,))
        if force_log or not last or (now - datetime.strptime(last["ts"], FMT)).total_seconds() / 60 >= \
                config.MONITOR_INTERVAL_MIN - 0.01:
            c = ctrl["components"]
            db.x("INSERT INTO crowd_log(ts,visit_date,inside,capacity,pct,c_current,c_booked,c_predicted,score,level,"
                 "action,extra_min) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (iso(now), date_s, mon["inside"], mon["capacity"], mon["pct"], c["current"], c["booked"],
                  c["predicted"], ctrl["score"], ctrl["level"], ctrl["action"], ctrl["extra_min"]))
    return {"monitor": mon, "control": ctrl}


# ------------------------------------------------ stage 6: QR verification ---
def _gate_log(b, kind, detail, now):
    db.x("INSERT INTO gate_events(ts,booking_id,kind,detail) VALUES(?,?,?,?)", (iso(now), b["id"] if b else None, kind, detail))


def verify_entry(token, now=None):
    now = now or get_now()
    with db.LOCK:
        b = parse_token(token)
        if not b:
            _gate_log(None, "deny", "invalid QR", now)
            return {"ok": False, "decision": "DENY", "reason": "Invalid or tampered QR code"}
        info = {"name": b["name"], "party_size": b["party_size"], "queue_no": b["queue_no"],
                "slot": f"{b['slot_start']}-{hhmm(slot_end_dt(b['visit_date'], b['slot_start']))}", "assist": bool(b["assist"])}
        if b["status"] == "inside":
            _gate_log(b, "deny", "already inside", now)
            return {"ok": False, "decision": "DENY", "reason": "Already admitted (QR already used for entry)", "booking": info}
        if b["status"] in ("exited",):
            return {"ok": False, "decision": "DENY", "reason": "Visit already completed", "booking": info}
        if b["status"] == "cancelled":
            return {"ok": False, "decision": "DENY", "reason": "Booking was cancelled", "booking": info}
        if b["visit_date"] != now.date().isoformat():
            return {"ok": False, "decision": "DENY", "reason": f"Booking is for {b['visit_date']}", "booking": info}
        ss = dt_of(b["visit_date"], b["slot_start"])
        opens = ss - timedelta(minutes=config.GRACE_EARLY_MIN)
        closes = ss + timedelta(minutes=config.GRACE_LATE_MIN + config.SLOT_MIN)
        if now < opens:
            return {"ok": False, "decision": "DENY", "reason": f"Too early. Gate opens for your slot at {hhmm(opens)}",
                    "booking": info}
        waited_at_gate = b["arrived_at"] and datetime.strptime(b["arrived_at"], FMT) <= closes
        if now > closes and not waited_at_gate:
            _gate_log(b, "deny", "slot expired", now)
            return {"ok": False, "decision": "DENY", "reason": "Your time window has expired. Please rebook.", "booking": info}
        # dynamic control: token bucket at the current batch pace
        tokens, rate = _refill(b["visit_date"], now)
        if rate != float("inf") and tokens <= 0:
            retry = 5 if rate == 0 else max(1, math.ceil((1 - tokens) / rate))
            if not b["arrived_at"]:
                db.x("UPDATE bookings SET arrived_at=? WHERE id=?", (iso(now), b["id"]))
            _gate_log(b, "wait", f"held {retry}m", now)
            return {"ok": False, "decision": "WAIT", "retry_in_min": retry,
                    "reason": "Entry paused to keep the temple safe" if rate == 0 else
                    f"Next batch in about {retry} min (crowd control active)", "booking": info}
        if rate != float("inf"):
            db.x("UPDATE control_state SET tokens=tokens-? WHERE visit_date=?", (b["party_size"], b["visit_date"]))
        db.x("UPDATE bookings SET status='inside', entry_at=? WHERE id=?", (iso(now), b["id"]))
        _gate_log(b, "entry", f"party {b['party_size']}", now)
        services.notify(b["id"], "Welcome! Entry recorded. Scan this QR again at the gate when you leave.",
                        channels=("push",), ts=iso(now))
        return {"ok": True, "decision": "ADMIT", "reason": "Verified. Entry allowed.", "booking": info}


def mark_arrived(token, now=None):
    """Devotee taps 'I've reached the gate' (or gate volunteer logs them): joins the gate wait list.
    Their window is then protected while crowd control holds the batch."""
    now = now or get_now()
    b = parse_token(token)
    if not b:
        return {"ok": False, "reason": "Invalid QR"}
    if b["status"] != "booked" or b["visit_date"] != now.date().isoformat():
        return {"ok": False, "reason": "No active booking for today"}
    ss = dt_of(b["visit_date"], b["slot_start"])
    if now < ss - timedelta(minutes=config.GRACE_EARLY_MIN):
        return {"ok": False, "reason": f"Too early. Gate opens for your slot at {hhmm(ss - timedelta(minutes=config.GRACE_EARLY_MIN))}"}
    if now > ss + timedelta(minutes=config.SLOT_MIN + config.GRACE_LATE_MIN) and not b["arrived_at"]:
        return {"ok": False, "reason": "Your time window has expired"}
    if not b["arrived_at"]:
        db.x("UPDATE bookings SET arrived_at=? WHERE id=?", (iso(now), b["id"]))
    return {"ok": True, "reason": "Check-in noted. Please wait near the gate; scan your QR when called."}


# ---------------------------------------------------- stage 8: exit + loop ---
def verify_exit(token, now=None):
    now = now or get_now()
    with db.LOCK:
        b = parse_token(token)
        if not b:
            return {"ok": False, "reason": "Invalid QR"}
        if b["status"] != "inside":
            return {"ok": False, "reason": "Not currently inside (already exited or never entered)"}
        db.x("UPDATE bookings SET status='exited', exit_at=? WHERE id=?", (iso(now), b["id"]))
        _gate_log(b, "exit", f"party {b['party_size']}", now)
        footfall.record_actuals(b["visit_date"])
        mins = round((now - datetime.strptime(b["entry_at"], FMT)).total_seconds() / 60)
        return {"ok": True, "reason": f"Exit recorded. Time inside: {mins} min.", "minutes_inside": mins,
                "booking": {"name": b["name"], "party_size": b["party_size"]}}


def retrain():
    """Feed the day's real gate data back into the prediction model."""
    for r in db.q("SELECT DISTINCT visit_date FROM bookings WHERE entry_at IS NOT NULL"):
        footfall.record_actuals(r["visit_date"])
    return footfall.train(include_actual=True)


# ------------------------------------------------------- admin dashboard ----
def dashboard(date_s=None, now=None):
    now = now or get_now()
    date_s = date_s or now.date().isoformat()
    d = Date.fromisoformat(date_s)
    mon = monitor(date_s, now)
    score, comp, raw = compute_score(date_s, now)
    cs = get_cs(date_s, now)
    pred = footfall.predict_day(d)
    cap_hour = config.SLOT_CAPACITY * 60 // config.SLOT_MIN

    slot_rows = db.q("""SELECT slot_start,
        COALESCE(SUM(CASE WHEN status!='cancelled' THEN party_size END),0) booked,
        COALESCE(SUM(CASE WHEN entry_at IS NOT NULL THEN party_size END),0) entered,
        COALESCE(SUM(CASE WHEN status='no_show' THEN party_size END),0) no_show
        FROM bookings WHERE visit_date=? GROUP BY slot_start""", (date_s,))
    sl = {r["slot_start"]: r for r in slot_rows}
    slots = []
    for s in slot_starts():
        r = sl.get(s)
        booked = r["booked"] if r else 0
        cap_s = slot_cap(date_s, s)
        slots.append({"slot": s, "capacity": cap_s, "booked": booked,
                      "entered": r["entered"] if r else 0, "no_show": r["no_show"] if r else 0,
                      "utilization": round(booked / cap_s, 3)})
    hourly = []
    actual_rows = {int(r["h"]): r["n"] for r in db.q(
        "SELECT substr(entry_at,12,2) h, SUM(party_size) n FROM bookings WHERE visit_date=? AND entry_at IS NOT NULL "
        "GROUP BY h", (date_s,))}
    for h in footfall.HOURS:
        booked_h = sum(s["booked"] for s in slots if int(s["slot"][:2]) == h)
        hourly.append({"hour": f"{h:02d}:00", "predicted": pred.get(h, 0), "booked": booked_h,
                       "actual": actual_rows.get(h, 0)})

    alerts = []
    ctrl_on = control_enabled()
    if score >= 60:
        alerts.append({"level": "high" if score >= 80 else "medium",
                       "text": f"Crowd load score {score:.0f}/100 ({cs['level']}). " +
                               (ACTIONS[cs["level"]] if ctrl_on else "Dynamic control is OFF (monitoring only).")})
    if mon["pct"] >= .9:
        alerts.append({"level": "high", "text": f"Occupancy at {mon['pct']*100:.0f}% of safe capacity ({mon['inside']}/{mon['capacity']})."})
    start_h = now.hour if date_s == now.date().isoformat() else footfall.HOURS[0]
    over = [f"{h:02d}:00" for h in footfall.HOURS if h >= start_h and pred.get(h, 0) > cap_hour * 0.9]
    if over:
        alerts.append({"level": "medium", "text": "Predicted demand above gate/slot capacity at " + ", ".join(over) +
                                                  ". Consider more volunteers and keeping dynamic control ON."})
    full = [s["slot"] for s in slots if s["utilization"] >= .95]
    if full:
        alerts.append({"level": "info", "text": f"{len(full)} slot(s) are ~full: " + ", ".join(full[:6]) + ("..." if len(full) > 6 else "")})
    if date_s == now.date().isoformat():
        assisted = db.q1("SELECT COUNT(*) n FROM bookings WHERE visit_date=? AND assist=1 AND status='booked'", (date_s,))["n"]
        if assisted:
            alerts.append({"level": "info", "text": f"{assisted} booking(s) flagged for elderly/accessibility assistance."})

    series = [dict(r) for r in db.q("SELECT ts,inside,pct,score,extra_min,level FROM crowd_log WHERE visit_date=? "
                                    "ORDER BY id", (date_s,))]
    stats = db.q1("""SELECT COUNT(*) bookings, COALESCE(SUM(party_size),0) devotees,
        COALESCE(SUM(CASE WHEN status='no_show' THEN party_size END),0) no_show,
        COALESCE(SUM(amount),0) revenue FROM bookings WHERE visit_date=? AND status!='cancelled'""", (date_s,))
    gate = db.q("SELECT ts, kind, detail FROM gate_events ORDER BY id DESC LIMIT 12")
    return {
        "date": date_s, "clock": clock_info(), "temple": T, "festival": services.day_info(d)["festival"],
        "monitor": mon,
        "control": {"enabled": ctrl_on, "score": score, "components": comp, "raw": raw, "level": cs["level"],
                    "extra_min": cs["extra_min"] if ctrl_on else 0, "paused": bool(cs["paused"]) and ctrl_on,
                    "action": ACTIONS[cs["level"]] if ctrl_on else "Monitoring only (control OFF)"},
        "slots": slots, "hourly": hourly, "alerts": alerts, "series": series,
        "stats": dict(stats), "gate_events": [dict(g) for g in gate],
        "model": footfall.model_info(),
    }
