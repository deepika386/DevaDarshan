# DevaDarshan: Temple & Pilgrimage Crowd Management System

Working prototype of your synopsis (BCS586 Mini Project, Team 026), set up for **Sri Banashankari Amma Temple, Bengaluru**
(single main gate, predictable spikes on Tuesdays, Fridays, Sundays and festivals). Everything temple-specific is in `config.py`.

## Run it

```bash
pip install -r requirements.txt      # includes tzdata, needed on Windows
python app.py                        # open http://localhost:5000
python tests.py                      # 65 end-to-end checks through the real HTTP API
python evaluate.py                   # simulated days, control OFF vs ON -> results/ (takes a few minutes)
```

## The website (static/)

| Section | What it does | Backend it uses |
|---|---|---|
| Home / overview | Hero + 6 live stat cards. Shows "Unavailable" if a request fails, never fake numbers | `/api/availability`, `/api/forecast`, `/api/monitor` |
| Book Darshan | 14-day date strip, slot grid (Available / Limited / Full / Closed), expected-crowd chart, validated form, demo payment, ticket | `/api/forecast`, `/api/availability`, `/api/book` |
| My Ticket | Open by reference, find by mobile + name, tickets remembered on this device, live queue, print/PDF, QR download, cancel | `/api/ticket`, `/api/lookup`, `/api/qr`, `/api/cancel`, `/api/arrive` |
| Crowd Forecast | 14 forecast cards, chart, per-day Panchanga + "why this forecast" | `/api/forecast`, `/api/panchanga`, `/api/availability` |
| Live Crowd | Occupancy, level, zone heatmap, slot utilization, peak hours | `/api/monitor`, `/api/availability` |
| QR Scanner | Camera scan (BarcodeDetector, falling back to bundled jsQR) or manual entry. **Lookup is read-only.** Staff gate actions (admit / exit) are a separate, confirmed step | `/api/ticket`, `/api/gate/scan`, `/api/arrive` |
| Administration | Occupancy, crowd-risk score, control toggle, alerts, slot table, gate events, notifications, model + demo mode | `/api/admin/*`, `/api/sim/*`, `/api/notifications` |

The camera is only requested when you press **Start scanner**, and it is switched off after a scan, on Stop, when the scanner
scrolls out of view, and when the tab is hidden.

## What changed in this version (and why)

Backend (existing routes were kept; nothing was deleted):
- **New:** `GET /api/clock`, `GET /api/ticket?token=`, `GET /api/qr?token=` (query-style aliases of the existing routes), `GET /api/panchanga?date=`, `GET /api/auth/status`, `GET /api/auth/check`, `POST /api/sim/mode`. `/api/forecast` rows now also carry `tithi`.
- **`panchanga.py` (new):** the project had no Panchanga, so tithi is computed from sun/moon positions (no API, no key). It matches four known full/new moons to within 0.05 degrees, but treat it as approximate (about +/- 1 hour at tithi boundaries).
- **Simulation can no longer touch real data.** Previously `/api/sim/reset` deleted all bookings in the same database. Now demo mode switches the server to a separate `data/demo.db`; sim routes return 409 outside demo mode, and `db.wipe_operational()` refuses to run on the real database. Retraining is blocked in demo mode so simulated data never trains the real model.
- **Privacy:** `/api/lookup` now needs mobile **and** name (it used to return ticket tokens to anyone who knew a phone number). Public tickets hide e-mail, payment reference and show only the last 4 digits of the phone. There is still no OTP, so do not treat this as production-grade identity checking.
- **Thread-safety fix:** all DB access now runs under one lock. Before, a simulation step and a page refresh could collide on the shared SQLite connection and produce a 500.
- **Festival calendar corrected** by cross-checking against computed tithi: Ganesha Chaturthi 2026 moved to 14 Sep and Navaratri 2027 to 30 Sep (they were wrong). The three "Banashankari Jatre" dates are now marked *date to be confirmed*. Please verify the rest with the temple.
- Database schema is unchanged. `data/devadarshan.db` is created on first run; back it up before experimenting.

Frontend: replaced the single `static/index.html` with `index.html` + `css/style.css` + `js/app.js` (+ `js/vendor/jsQR.js`, Apache-2.0).
The old screen would not work against this backend (its phone lookup and demo buttons are now rejected), so replace it, do not mix them.

## Try the demo (2 minutes)

1. Administration -> **Open dashboard** -> **Enter demo mode**.
2. Pick 8 Nov 2026, demand **Surge**, **Seed bookings**, **Jump to 07:00**, then **+1 hour** a few times.
3. Watch the crowd-load gauge, heatmap and alerts react; the Live Crowd section is labelled *simulated*. Untick *Dynamic control* to see the unprotected baseline.
4. **Leave demo mode**: your real bookings are exactly as you left them.

## Simulation results (`python evaluate.py`, safe capacity 600)

| Scenario | Peak inside (off / on) | Minutes above safe capacity (off / on) | Mean gate hold, min (off / on) |
|---|---|---|---|
| A. Normal weekday | 171 / 171 | 0 / 0 | 0.5 / 0.5 |
| B. Deepavali, forecast sets slot capacity | 615 / 531 | 10 / 0 | 0.5 / 2.7 |
| C. Deepavali, surge the forecast missed | 709 / 569 | 78 / 0 | 0.5 / 4.7 |
| D. New Year, extreme surge the forecast missed | 778 / 546 | 417 / 0 | 0.5 / 52.2 |

Control keeps the temple under its safe limit by turning overcrowding inside into a managed wait at the gate; the wait grows with
how far demand exceeds capacity (about 52 min on average in the extreme case D). On normal days it does nothing.

## Honest limitations

- **All crowd data is synthetic.** The model is trained on generated history; its R2 (~0.94) is measured on that same synthetic data and says nothing about real accuracy. It improves only as real gate data accumulates.
- **The simulation assumes devotee behaviour** (arrival jitter, 5% no-shows, longer stays when crowded). It does not verify the ~85% wait reduction quoted from the literature.
- **Tithi is computed, not looked up**, and is shown for information; the forecast model does not use it. Festival dates, capacity, fees and timings are placeholders to verify with the temple.
- **Mocked:** payment gateway and SMS/e-mail/push (stored and shown in-app). Weather falls back to a seasonal estimate when offline.
- **Live Crowd is not sensor data.** Counts are QR entry/exit scans recorded by this system.
- Camera scanning needs HTTPS or localhost (browser rule). Fonts load from Google Fonts when online and fall back to system fonts otherwise.
- Admin protection is a single shared `ADMIN_KEY` header (kept per browser tab). Set it before deploying; with no key set, admin pages are open.
- SQLite instead of MySQL; plain HTML/JS instead of React/Flutter. Flask + REST match the synopsis.

## Deploying

Needs a Python host (Render, Railway, PythonAnywhere). GitHub Pages / Netlify / Vercel only serve static files and cannot run it.
Start command: see `Procfile` (one worker, several threads, on purpose). Set `ADMIN_KEY` and `DEVADARSHAN_SECRET`.
Free hosts reset SQLite on restart, which is fine for a demo.

## Layout

```
app.py          Flask API + serves the site         engine.py     the 8 stages
footfall.py     forecast model + feedback loop      panchanga.py  tithi + crowd reasons (new)
simulator.py    minute-by-minute simulation         services.py   calendar, weather, payment, notify
db.py           SQLite + demo-DB safety             config.py     temple + tuning
evaluate.py     OFF vs ON experiments -> results/   tests.py      65 API checks
static/         index.html, css/style.css, js/app.js, js/vendor/jsQR.js
```
