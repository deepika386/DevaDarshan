"""End-to-end tests through the real HTTP API (Flask test client, temporary DB files).  python tests.py"""
import os
import shutil
import tempfile

import config
import db
import engine

# keep the shipped model file untouched: the retrain test writes to a temp copy
_tmp = os.path.join(tempfile.mkdtemp(), "model.joblib")
if os.path.exists(config.MODEL_PATH):
    shutil.copy(config.MODEL_PATH, _tmp)
config.MODEL_PATH = _tmp

# Real and demo databases are separate temp FILES, so the real-vs-demo isolation is genuinely exercised.
_dbdir = tempfile.mkdtemp()
config.DB_PATH = os.path.join(_dbdir, "real.db")
config.DEMO_DB_PATH = os.path.join(_dbdir, "demo.db")
db.use(config.DB_PATH)
import app as appmod  # noqa: E402

c = appmod.app.test_client()
DAY = "2026-11-03"   # a Tuesday
passed = 0


def check(name, cond, extra=""):
    global passed
    assert cond, f"FAIL: {name} {extra}"
    passed += 1
    print(f"  ok  {name}")


def post(url, body=None):
    r = c.post(url, json=body or {})
    return r.status_code, r.get_json()


def clock(t):
    post("/api/sim/clock", {"mode": "set", "time": t})


print("Safety - simulation can never touch real bookings")
real_day = (engine.get_now().date() + __import__("datetime").timedelta(days=1)).isoformat()
code, rb = post("/api/book", {"name": "Real Person", "phone": "9000000001", "date": real_day, "slot": "10:00", "party": 2})
check("a real booking is created on the real database", code == 200, str(rb))
code, r = post("/api/sim/reset")
check("sim/reset is refused while demo mode is OFF", code == 409, str(r))
code, r = post("/api/sim/seed", {"date": real_day})
check("sim/seed is refused while demo mode is OFF", code == 409)
try:
    db.wipe_operational()
    refused = False
except PermissionError:
    refused = True
check("db.wipe_operational() refuses to run on the real database", refused)
check("clock reports demo OFF", c.get("/api/clock").get_json()["demo"] is False)
code, r = post("/api/sim/mode", {"enabled": True})
check("demo mode switches to the separate demo database", code == 200 and r["demo"] is True and db.current_path() == config.DEMO_DB_PATH)
check("demo database starts without the real booking", len(c.get("/api/lookup?phone=9000000001&name=Real Person").get_json()["tickets"]) == 0)

print("Stage 1 - booking & availability")
clock(f"{DAY} 07:00")
r = c.get(f"/api/availability?date={DAY}").get_json()
check("availability lists slots", len(r["slots"]) == 28)
check("slot has capacity + demand label", r["slots"][0]["capacity"] > 0 and "busy" in r["slots"][0])
code, e = post("/api/book", {"name": "", "phone": "123", "date": DAY, "slot": "07:30", "party": 2})
check("validation rejects bad input", code == 400)
code, b = post("/api/book", {"name": "Asha Rao", "phone": "9876543210", "date": DAY, "slot": "07:30", "party": 2})
check("booking created", code == 200 and b["queue_no"] == 3 * config.SLOT_CAPACITY + 1, str(b))
tok = b["token"]
check("payment recorded (demo)", b["amount"] == 2 * config.FEE_PER_PERSON and b["payment_ref"].startswith("PAY-"))
code, e = post("/api/book", {"name": "Late", "phone": "9876543211", "date": DAY, "slot": "06:00", "party": 1})
check("cannot book a slot that already ended", code == 400)
check("QR image served", c.get(f"/api/qr/{tok}.png").mimetype == "image/png")
check("forged token has no QR", c.get("/api/qr/1-deadbeefdead.png").status_code == 404)

print("Stage 2/7 - queue number, estimate, live updates")
q = c.get(f"/api/ticket/{tok}").get_json()["queue"]
check("queue number + estimated entry given", q["queue_no"] == 901 and q["est_entry_text"] >= "07:30")
code, b2 = post("/api/book", {"name": "Ravi K", "phone": "9876543212", "date": DAY, "slot": "07:30", "party": 6})
check("next devotee gets next number block position", b2["queue_no"] == 903)
q2 = c.get(f"/api/ticket/{b2['token']}").get_json()["queue"]
check("people ahead counted", q2["people_ahead"] == 2, str(q2))
check("lookup by phone + name", len(c.get("/api/lookup?phone=9876543210&name=asha rao").get_json()["tickets"]) == 1)
check("lookup by phone alone is refused", c.get("/api/lookup?phone=9876543210").status_code == 400)
check("lookup with wrong name finds nothing", len(c.get("/api/lookup?phone=9876543210&name=Someone Else").get_json()["tickets"]) == 0)
tk = c.get(f"/api/ticket/{tok}").get_json()
check("public ticket hides e-mail, payment ref and full phone", "email" not in tk and "payment_ref" not in tk and tk["phone"] == "XXXXXX3210")
check("query-style ticket alias matches", c.get(f"/api/ticket?token={tok}").get_json()["queue_no"] == q["queue_no"])
check("query-style QR alias works", c.get(f"/api/qr?token={tok}").mimetype == "image/png")
check("/api/clock reports mode, demo flag and auth requirement", set(c.get("/api/clock").get_json()) >= {"now", "mode", "demo", "admin_key_required"})

print("Stage 6 - QR verification")
code, r = post("/api/gate/scan", {"token": tok, "mode": "entry"})
check("too early is denied", r["decision"] == "DENY" and "early" in r["reason"].lower(), str(r))
code, r = post("/api/gate/scan", {"token": "1-000000000000", "mode": "entry"})
check("tampered QR denied", r["decision"] == "DENY")
clock(f"{DAY} 07:28")
code, r = post("/api/gate/scan", {"token": tok, "mode": "entry"})
check("valid QR admitted in window", r["decision"] == "ADMIT", str(r))
code, r = post("/api/gate/scan", {"token": tok, "mode": "entry"})
check("QR cannot be reused", r["decision"] == "DENY")
check("status now inside", c.get(f"/api/ticket/{tok}").get_json()["queue"]["state"] == "inside")

print("Stage 4 - real-time monitoring")
m = c.get("/api/monitor").get_json()
check("occupancy counts party size", m["inside"] == 2 and m["pct"] == round(2 / config.TEMPLE["max_inside"], 3))
check("heatmap zones present", len(m["zones"]) == 4 and m["zones"][0]["count"] == 2)

print("Stage 8 - exit & feedback loop")
clock(f"{DAY} 08:05")
code, r = post("/api/gate/scan", {"token": tok, "mode": "exit"})
check("exit recorded", r["ok"] and r["minutes_inside"] == 37, str(r))
code, r = post("/api/gate/scan", {"token": tok, "mode": "exit"})
check("double exit rejected", not r["ok"])
code, _ = post("/api/admin/retrain")
check("retrain endpoint is blocked in demo mode (simulated data must not train the real model)", code == 409)
info = engine.retrain()      # the feedback loop itself (writes only to the temp model copy)
check("model retrained with real gate data", info["actual_hours_used"] >= 1, str(info))

print("Booking integrity")
clock(f"{DAY} 09:00")
cap = engine.slot_cap(DAY, "17:00")
n = 0
while True:
    code, r = post("/api/book", {"name": f"P{n}", "phone": f"98000{n:05d}", "date": DAY, "slot": "17:00", "party": 6})
    if code != 200:
        break
    n += 1
check("slot never over-booked", code == 409 and n * 6 <= cap, f"{n=} {cap=} {r}")
code, r = post("/api/cancel/" + tok)
check("cannot cancel a completed visit", code == 409)
code, bx = post("/api/book", {"name": "Cancel Me", "phone": "9111111111", "date": DAY, "slot": "18:00", "party": 1})
code, r = post("/api/cancel/" + bx["token"])
check("cancel works before slot", code == 200)

print("Stage 3 - footfall prediction")
f = c.get("/api/forecast?days=14").get_json()
check("14-day forecast", len(f["forecast"]) == 14 and f["model"]["holdout_r2"] > 0.8)
fest = [d for d in f["forecast"] if d["festival"]]
sun = next(d for d in f["forecast"] if d["weekday"] == "Wednesday" and not d["festival"])
if fest:
    check("festival days forecast higher than a normal Wednesday", max(d["predicted_total"] for d in fest) > sun["predicted_total"])

print("Stage 5 - dynamic crowd control")
post("/api/sim/reset")
FEST = "2026-11-08"
clock(f"{FEST} 05:30")
code, s = post("/api/sim/seed", {"date": FEST, "demand": 1.4})
check("simulated bookings seeded", s["seeded"]["devotees"] > 3000, str(s))
post("/api/sim/clock", {"mode": "set", "time": f"{FEST} 07:00"})
levels = set()
for _ in range(14):
    post("/api/sim/advance", {"minutes": 30})
    d = c.get("/api/admin/dashboard").get_json()
    levels.add(d["control"]["level"])
check("control escalates under heavy load", len(levels - {"Low"}) >= 1, str(levels))
check("occupancy stays within safe capacity with control ON", max(p["inside"] for p in d["series"]) <= config.TEMPLE["max_inside"] * 1.05)
check("dashboard has alerts, hourly and slot data", d["slots"] and d["hourly"] and d["series"])
check("delay notifications were sent", len(c.get("/api/notifications").get_json()["notifications"]) > 0)
post("/api/admin/control", {"enabled": False})
check("control can be switched off", c.get("/api/admin/dashboard").get_json()["control"]["enabled"] is False)

print("Panchanga")
pg = c.get("/api/panchanga?date=2026-11-08").get_json()
check("panchanga returns tithi, festival and honest reasons", pg["festival"] == "Deepavali (Lakshmi Puja)" and pg["tithi"]["label"] and pg["crowd_factors"])
check("Deepavali Lakshmi Puja day is Krishna Chaturdashi at sunrise, Amavasya follows", pg["tithi"]["name"] == "Chaturdashi" and "Amavasya" in pg["tithi"]["next"], str(pg["tithi"]))
check("Ugadi 2027 is Shukla Prathama", c.get("/api/panchanga?date=2027-04-07").get_json()["tithi"]["label"] == "Shukla Prathama")
check("bad panchanga date is a clean 400", c.get("/api/panchanga?date=banana").status_code == 400)
fc = c.get("/api/forecast?days=14").get_json()["forecast"]
check("forecast rows carry tithi", all(r["tithi"] for r in fc))
import panchanga as _pg
from datetime import datetime as _dt, timezone as _tz
check("moon-phase maths: full moon 2026-01-03 10:03 UTC is ~180 degrees", abs(_pg.elongation(_dt(2026, 1, 3, 10, 3, tzinfo=_tz.utc)) - 180) < 1.0)
check("moon-phase maths: new moon 2026-01-18 19:52 UTC is ~0 degrees", min(_pg.elongation(_dt(2026, 1, 18, 19, 52, tzinfo=_tz.utc)), 360 - _pg.elongation(_dt(2026, 1, 18, 19, 52, tzinfo=_tz.utc))) < 1.0)

print("Leaving demo mode returns to untouched real data")
code, r = post("/api/sim/mode", {"enabled": False})
check("demo mode switches off", code == 200 and r["demo"] is False and db.current_path() == config.DB_PATH)
rt = c.get(f"/api/ticket/{rb['token']}").get_json()
check("the real booking survived the whole simulation", rt["queue_no"] == rb["queue_no"] and rt["status"] == "booked")
check("no simulated bookings leaked into the real database",
      db.q1("SELECT COUNT(*) n FROM bookings")["n"] == 1)
check("real clock is live again", c.get("/api/clock").get_json()["mode"] == "live")

print("Admin key protection")
config.ADMIN_KEY = "s3cret"
check("auth status says a key is required", c.get("/api/auth/status").get_json()["admin_key_required"] is True)
check("admin dashboard needs the key", c.get("/api/admin/dashboard").status_code == 401)
check("wrong key rejected", c.get("/api/auth/check", headers={"X-Admin-Key": "nope"}).status_code == 401)
check("right key accepted", c.get("/api/auth/check", headers={"X-Admin-Key": "s3cret"}).status_code == 200)
check("gate scan needs the key", c.post("/api/gate/scan", json={"token": "x", "mode": "entry"}).status_code == 401)
check("simulation switch needs the key", c.post("/api/sim/mode", json={"enabled": True}).status_code == 401)
check("public pages stay public", c.get("/api/availability").status_code == 200 and c.get("/api/monitor").status_code == 200)
config.ADMIN_KEY = None

print(f"\nAll {passed} checks passed.")
