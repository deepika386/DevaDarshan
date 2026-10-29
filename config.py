"""DevaDarshan configuration.

Everything temple-specific lives here, so the same code can be pointed at a
different temple (small shrine or large pilgrimage site) by editing one dict.
Numbers are illustrative defaults for a prototype -- replace them with the
temple's real figures before any real-world use.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DEVADARSHAN_DB", os.path.join(BASE_DIR, "data", "devadarshan.db"))
DEMO_DB_PATH = os.environ.get("DEVADARSHAN_DEMO_DB", os.path.join(BASE_DIR, "data", "demo.db"))
MODEL_PATH = os.path.join(BASE_DIR, "data", "footfall_model.joblib")
FESTIVALS_PATH = os.path.join(BASE_DIR, "data", "festivals.json")
SECRET = os.environ.get("DEVADARSHAN_SECRET", "dev-only-secret-change-me").encode()
ADMIN_KEY = os.environ.get("ADMIN_KEY")  # if set, admin/gate/sim endpoints require header X-Admin-Key

TEMPLE = {
    "name": "Sri Banashankari Amma Temple",
    "city": "Bengaluru, Karnataka",
    "lat": 12.9255,
    "lon": 77.5468,
    "open": "06:00",
    "close": "20:00",
    "gate": "Main Gate (single entry / exit)",
    "max_inside": 600,   # safe number of devotees inside the premises at once
}

TIMEZONE = "Asia/Kolkata"

# --- Slots & gate ----------------------------------------------------------
SLOT_MIN = 30            # length of a darshan slot
SLOT_CAPACITY = 300      # max devotees bookable per slot
MAX_PARTY = 6            # max devotees on one booking
BATCH_SIZE = 60          # devotees admitted per batch at normal pace
BATCH_INTERVAL_MIN = 5   # minutes between batches at normal pace
GRACE_EARLY_MIN = 10     # QR accepted this many minutes before the slot
GRACE_LATE_MIN = 20      # ...and this many minutes after the slot ends
FEE_PER_PERSON = 30      # demo booking/seva fee in INR (0 = free darshan, payment step skipped)

# --- Monitoring & control --------------------------------------------------
MONITOR_INTERVAL_MIN = 5          # crowd snapshot cadence ("updates every few minutes")
SCORE_WEIGHTS = {"current": 0.4, "booked": 0.4, "predicted": 0.2}
DEFAULT_DWELL_MIN = 42            # average time inside; replaced by the measured average once exits are recorded
# (upper bound of score, level, extra minutes added between batches, gate paused?)
CONTROL_LEVELS = [
    (80, "Low",      0,  False),   # open normally
    (90, "Moderate", 3,  False),   # slow down: +3 min between batches
    (97, "High",     10, False),   # delay next batch by 10 minutes
    (101, "Critical", 0, True),    # pause new entries
]

# Capacity planning from the footfall forecast: (predicted slot demand / SLOT_CAPACITY at least, capacity factor)
CAPACITY_TIERS = [(1.0, 0.75), (0.8, 0.9)]

# Dwell-time bands (minutes since entry) used to build the zone heatmap.
ZONES = [
    # name, from_min, to_min, share of max_inside that the zone can comfortably hold (zones overlap in use)
    ("Entry & outer prakara", 0, 10, 0.25),
    ("Inner hall queue",      10, 25, 0.35),
    ("Sanctum darshan",       25, 40, 0.35),
    ("Prasada & exit",        40, 10_000, 0.40),
]
