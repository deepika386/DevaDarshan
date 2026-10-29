"""DevaDarshan REST API + web app.   python app.py  ->  http://localhost:5000

Existing routes are unchanged. Added in this version (see README "What changed"):
  GET  /api/clock                 clock + demo flag + whether an admin key is required
  GET  /api/ticket?token=         query-style alias of /api/ticket/<token>
  GET  /api/qr?token=             query-style alias of /api/qr/<token>.png
  GET  /api/panchanga?date=       tithi + festival context (computed, no external API)
  GET  /api/auth/status           is an admin key configured?
  GET  /api/auth/check            200 only with a valid admin key
  POST /api/sim/mode              enter / leave the separate demo database
Simulation routes now refuse to run unless demo mode is ON, so they can never touch real bookings.
"""
import threading
import time
from datetime import date as Date, datetime, timedelta
from functools import wraps
from io import BytesIO

import qrcode
from flask import Flask, jsonify, request, send_file, send_from_directory

import config
import db
import engine
import footfall
import panchanga
import services
import simulator

app = Flask(__name__, static_folder="static", static_url_path="/static")


def api(fn):
    """JSON error handling for every API route."""
    @wraps(fn)
    def wrapper(*a, **kw):
        try:
            return jsonify(fn(*a, **kw))
        except engine.BookingError as e:
            return jsonify({"error": e.msg}), e.code
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    return wrapper


def admin_only(fn):
    """If ADMIN_KEY is set on the server, require it in the X-Admin-Key header."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if config.ADMIN_KEY and request.headers.get("X-Admin-Key") != config.ADMIN_KEY:
            return jsonify({"error": "Admin key required or incorrect"}), 401
        return fn(*a, **kw)
    return wrapper


def need_demo():
    """Simulation actions are only allowed on the separate demo database."""
    if not (db.is_demo() or db.current_path() == ":memory:"):
        raise engine.BookingError("Demo mode is OFF. Turn it on first; simulations never run on real bookings.", 409)


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


# ------------------------------------------------------------ clock / auth --
@app.get("/api/clock")
@api
def clock():
    return {**engine.clock_info(), "demo": db.is_demo(), "admin_key_required": bool(config.ADMIN_KEY)}


@app.get("/api/auth/status")
@api
def auth_status():
    return {"admin_key_required": bool(config.ADMIN_KEY)}


@app.get("/api/auth/check")
@admin_only
@api
def auth_check():
    return {"ok": True}


# ------------------------------------------------------------ devotee APIs --
@app.get("/api/temple")
@api
def temple():
    now = engine.get_now()
    return {"temple": config.TEMPLE, "clock": engine.clock_info(), "slot_minutes": config.SLOT_MIN,
            "fee_per_person": config.FEE_PER_PERSON, "max_party": config.MAX_PARTY,
            "today": now.date().isoformat(), "max_date": (now.date() + timedelta(days=14)).isoformat(),
            "festival_today": services.day_info(now.date())["festival"], "demo": db.is_demo()}


@app.get("/api/availability")
@api
def availability():
    return engine.availability(request.args.get("date") or engine.get_now().date().isoformat())


@app.post("/api/book")
@api
def book():
    j = request.get_json(force=True, silent=True) or {}
    return engine.create_booking(j.get("name"), j.get("phone"), j.get("date"), j.get("slot"),
                                 j.get("party", 1), j.get("email"), j.get("assist", False))


@app.get("/api/ticket/<token>")
@api
def ticket(token):
    return engine.ticket(token)


@app.get("/api/ticket")
@api
def ticket_q():
    return engine.ticket(request.args.get("token", ""))


@app.post("/api/cancel/<token>")
@api
def cancel(token):
    return engine.cancel_booking(token)


@app.get("/api/lookup")
@api
def lookup():
    return {"tickets": engine.lookup_by_phone(request.args.get("phone", ""), request.args.get("name", ""))}


@app.post("/api/arrive/<token>")
@api
def arrive(token):
    return engine.mark_arrived(token)


def _qr_png(token):
    if not engine.parse_token(token):
        return jsonify({"error": "Ticket not found"}), 404
    img = qrcode.make(token, box_size=8, border=2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.get("/api/qr/<token>.png")
def qr(token):
    return _qr_png(token)


@app.get("/api/qr")
def qr_q():
    return _qr_png(request.args.get("token", ""))


# -------------------------------------------------------- gate / admin APIs --
@app.post("/api/gate/scan")
@admin_only
@api
def gate_scan():
    j = request.get_json(force=True, silent=True) or {}
    mode = j.get("mode", "entry")
    token = (j.get("token") or "").strip()
    return engine.verify_exit(token) if mode == "exit" else engine.verify_entry(token)


@app.get("/api/monitor")
@api
def monitor():
    return engine.tick(force_log=False)["monitor"]


@app.get("/api/admin/dashboard")
@admin_only
@api
def dashboard():
    engine.tick()
    return engine.dashboard(request.args.get("date"))


@app.get("/api/forecast")
@api
def forecast():
    try:
        days = max(1, min(14, int(request.args.get("days", 14))))
    except ValueError:
        days = 14
    rows = footfall.forecast(engine.get_now().date(), days)
    for r in rows:                      # add tithi; a Panchanga failure must never break the forecast
        try:
            t = panchanga.compute(Date.fromisoformat(r["date"]))
            r["tithi"], r["tithi_special"] = t["label"], t["special"]
        except Exception:
            r["tithi"], r["tithi_special"] = None, False
    return {"forecast": rows, "model": footfall.model_info()}


@app.get("/api/panchanga")
@api
def panchanga_route():
    d = request.args.get("date") or engine.get_now().date().isoformat()
    try:
        day = Date.fromisoformat(d)
    except ValueError:
        raise engine.BookingError("Invalid date (use YYYY-MM-DD)")
    if abs((day - engine.get_now().date()).days) > 400:
        raise engine.BookingError("Date out of supported range")
    return panchanga.describe(day)


@app.post("/api/admin/control")
@admin_only
@api
def control():
    engine.set_control_enabled(bool((request.get_json(force=True, silent=True) or {}).get("enabled", True)))
    engine.tick(force_log=True)
    return {"enabled": engine.control_enabled()}


@app.post("/api/admin/retrain")
@admin_only
@api
def retrain():
    if db.is_demo():
        raise engine.BookingError("Retraining is disabled in demo mode, so simulated data never trains the real model.", 409)
    return engine.retrain()


@app.get("/api/notifications")
@admin_only
@api
def notifications():
    rows = db.q("SELECT n.ts, n.channel, n.message, b.name, b.queue_no FROM notifications n "
                "LEFT JOIN bookings b ON b.id=n.booking_id ORDER BY n.id DESC LIMIT 30")
    return {"notifications": [dict(r) for r in rows]}


# ------------------------------------------------------------- demo / sim ----
def _clear_sim_caches():
    simulator._plans.clear()
    simulator._mult.clear()


@app.post("/api/sim/mode")
@admin_only
@api
def sim_mode():
    """Switch between the REAL database and the separate DEMO database (affects every visitor of this server)."""
    enabled = bool((request.get_json(force=True, silent=True) or {}).get("enabled"))
    if enabled and not db.is_demo():
        db.use(config.DEMO_DB_PATH, demo=True)
        db.wipe_operational()            # allowed: demo DB only. Always starts clean.
        _clear_sim_caches()
        engine.set_control_enabled(True)
        engine.set_clock(None)
    elif not enabled and db.is_demo():
        db.use(config.DB_PATH, demo=False)
        _clear_sim_caches()
    return {"demo": db.is_demo(), **engine.clock_info()}


@app.post("/api/sim/clock")
@admin_only
@api
def sim_clock():
    need_demo()
    j = request.get_json(force=True, silent=True) or {}
    if j.get("mode") == "live":
        engine.set_clock(None)
    else:
        engine.set_clock(datetime.strptime(j["time"], "%Y-%m-%d %H:%M"))
    return engine.clock_info()


@app.post("/api/sim/seed")
@admin_only
@api
def sim_seed():
    need_demo()
    j = request.get_json(force=True, silent=True) or {}
    date_s = j.get("date") or engine.get_now().date().isoformat()
    return {"seeded": simulator.seed_bookings(date_s, float(j.get("demand", 1.0))), "date": date_s}


@app.post("/api/sim/advance")
@admin_only
@api
def sim_advance():
    need_demo()
    minutes = max(1, min(180, int((request.get_json(force=True, silent=True) or {}).get("minutes", 10))))
    now = simulator.advance(minutes)
    return {"now": engine.iso(now)}


@app.post("/api/sim/reset")
@admin_only
@api
def sim_reset():
    need_demo()
    db.wipe_operational()                # also guarded inside db.py
    _clear_sim_caches()
    engine.set_control_enabled(True)
    engine.set_clock(None)
    return {"ok": True}


# live clock: monitoring + control run every few minutes even with nobody looking
def _background():
    while True:
        time.sleep(config.MONITOR_INTERVAL_MIN * 60)
        try:
            if not db.meta_get("sim_now"):
                engine.tick()
        except Exception as e:  # keep the loop alive
            print("tick error:", e)


if __name__ == "__main__":
    db.use()
    footfall.model()
    threading.Thread(target=_background, daemon=True).start()
    print(f"DevaDarshan for {config.TEMPLE['name']} -> http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
