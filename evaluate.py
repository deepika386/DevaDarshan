"""Evaluation: simulate temple days with dynamic crowd control OFF vs ON.

    python evaluate.py

Mirrors the synopsis' evaluation plan (synthetic crowd data, normal and festival-day
scenarios). Writes results/evaluation.md and results/evaluation.json.

IMPORTANT: results come from a SIMULATION with assumed devotee behaviour (arrival
jitter, dwell time that grows with crowding). They demonstrate the logic, not
real-world performance; they do NOT verify the ~85% wait-time reduction quoted
from the literature.
"""
import json
import os

import config
import db
import simulator

SCENARIOS = [
    ("A. Normal weekday", "2026-11-04", False),
    ("B. Deepavali Sunday - forecast plans slot capacity", "2026-11-08", False),
    ("C. Deepavali Sunday - surge the forecast missed", "2026-11-08", True),
    ("D. New Year's Day - extreme surge the forecast missed", "2027-01-01", True),
]


def main():
    db.use(":memory:")
    safe = config.TEMPLE["max_inside"]
    original_tiers = list(config.CAPACITY_TIERS)
    rows = []
    for label, date, forecast_missed in SCENARIOS:
        config.CAPACITY_TIERS = [] if forecast_missed else original_tiers
        for on in (False, True):
            r = simulator.run_day(date, control_on=on)
            r["scenario"] = label
            rows.append(r)
            print(f"{label} | control {'ON ' if on else 'OFF'} | peak {r['peak_inside']:4d} | "
                  f"min over safe {r['minutes_over_safe_capacity']:4d} | mean hold {r['mean_gate_hold_min']:5.1f}")
    config.CAPACITY_TIERS = original_tiers

    os.makedirs("results", exist_ok=True)
    json.dump(rows, open("results/evaluation.json", "w"), indent=1)
    md = [f"# DevaDarshan simulation results ({config.TEMPLE['name']})\n",
          f"Safe capacity inside the premises: **{safe}** devotees. Single gate. Slot = {config.SLOT_MIN} min.\n",
          "| Scenario | Control | Devotees booked | Peak inside (% of safe) | Minutes above safe capacity | "
          "Mean / p95 hold at gate (min) | No-shows |",
          "|---|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        md.append(f"| {r['scenario']} | {'ON' if r['control_on'] else 'OFF'} | {r['booked_devotees']} | "
                  f"{r['peak_inside']} ({r['peak_pct_of_safe_capacity']}%) | {r['minutes_over_safe_capacity']} | "
                  f"{r['mean_gate_hold_min']} / {r['p95_gate_hold_min']} | {r['no_show_devotees']} |")
    md.append("\n*Hold at gate* = minutes between a devotee reaching the gate and being admitted. "
              "Simulation assumptions: 5% no-shows; 55% arrive within -5..+10 min of slot start; dwell 25-60 min "
              "scaled up to 1.7x as the temple fills. Results are illustrative, not field measurements.\n")
    open("results/evaluation.md", "w").write("\n".join(md))
    print("\nWrote results/evaluation.md and results/evaluation.json")


if __name__ == "__main__":
    main()
