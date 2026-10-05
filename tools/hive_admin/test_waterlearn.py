#!/usr/bin/env python3
"""Offline test for waterlearn.py (adaptive watering).

Simulates a pot: soil dries at a known rate, an automatic watering lifts it by
a known amount per ml, a manual test dose goes into a cup and lifts nothing.
Then checks what the learner concludes and what it would write.

    python tools/hive_admin/test_waterlearn.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hive_admin as H  # noqa: E402
import waterlearn as W  # noqa: E402

failed = 0


def check(cond, msg, detail=None):
    global failed
    if cond:
        print("ok:", msg)
    else:
        failed += 1
        print("FAIL:", msg, "" if detail is None else detail)


DRY, WET = 2700, 900          # raw at 0% and 100%
NODE = 7


def raw(pct):
    return int(round(DRY - pct / 100.0 * (DRY - WET)))


tmp = tempfile.mkdtemp()
store = H.Store(os.path.join(tmp, "t.db"))
cfg = {NODE: {"adaptive": {"on": True, "from_plant": True}, "plant_band": [30, 70]}}
logs = []
L = W.WaterLearner(store, lambda n: (DRY, WET, True), lambda n: cfg.get(n, {}), log=logs.append)

# Simulated day: start 45%, dry 1%/h, poll every 5 min. Automatic waterings of
# 100 ml lift the soil 10% (0.1%/ml). A manual 50 ml dose into a cup at hour 5
# lifts nothing and must not count.
t0 = int(time.time()) - 40 * 3600
rows = []
pct, auto_at = 45.0, None
dose_log = []
events = {16: ("auto", 100), 32: ("auto", 100), 5: ("manual", 50)}
last = {}


def report(ts, slot, value):
    # Change-based, like the admin's store: a row only when the value moves.
    if last.get(slot) != value:
        rows.append((ts, NODE, slot, value))
        last[slot] = value


for step in range(0, 40 * 12):
    ts = t0 + step * 300
    hour = step / 12.0
    pct -= 1.0 / 12
    if step % 12 == 0 and int(hour) in events:
        kind, ml = events[int(hour)]
        if kind == "auto":
            pct += 0.1 * ml
            auto_at = ts
        dose_log.append((ts, ml))
    # The node's own outputs: the rolling 24 h total (doses age out of it) and
    # minutes since the last automatic watering (reported in 30-min steps).
    report(ts, 45, sum(m for t, m in dose_log if t > ts - 86400))
    report(ts, 54, 65535 if auto_at is None else (ts - auto_at) // 60 // 30 * 30)
    report(ts, 3, raw(pct))
store.add_readings(rows)
now = t0 + 40 * 3600

a = L.analyse(NODE, now_ts=now)
autos = [e for e in a["events"] if e["auto"]]
manual = [e for e in a["events"] if not e["auto"]]
check(len(a["events"]) == 3 and len(autos) == 2 and len(manual) == 1,
      "three waterings found, two automatic and one manual", a["events"])
check(all(e["ml"] == 100 for e in autos) and manual and manual[0]["ml"] == 50,
      "each watering's ml from the 24 h total going up", a["events"])
check(a["gain"] is not None and abs(a["gain"] - 0.1) < 0.02, "learned ~0.1% per ml from the automatic ones", a["gain"])
check(a["dry_per_h"] is not None and abs(a["dry_per_h"] - 1.0) < 0.15, "learned ~1%/h drying", a["dry_per_h"])
check(a["seen"] == 2, "the cup test taught it nothing", a["seen"])

slots = {49: 1, 50: raw(30), 51: 100, 43: 250, 3: raw(pct)}
rec = L.recommend(NODE, slots, now_ts=now)
# Fill to the middle of 30-70 = 50 from 30: 20% / 0.1%/ml = 200 ml, but one step
# is capped at +40% of the current 100 ml.
check(rec["ideal"] in range(180, 221), "ideal amount ~200 ml (30% -> 50%)", rec)
check(rec["ml"] == 140, "one step changes it by at most 40% (100 -> 140 ml)", rec)
check(rec["hours_between"] and 17 <= rec["hours_between"] <= 23, "~20 h between waterings predicted", rec)

writes = []
done = L.run_once({NODE: dict(slots)}, lambda n, s, v: writes.append((n, s, v)) or True, now_ts=now)
check((NODE, 51, 140) in writes and done == [(NODE, 140)], "the new amount is written to slot 51", writes)
check(any("learned watering" in m for m in logs), "and logged", logs)

# Following the plant: the band moves (seedling -> grown), the threshold follows.
cfg[NODE]["plant_band"] = [45, 75]
writes.clear()
L.run_once({NODE: dict(slots)}, lambda n, s, v: writes.append((n, s, v)) or True, now_ts=now)
check((NODE, 50, raw(45)) in writes, "water-below follows the plant's new low end", writes)

# Learning off: the plant is still followed, the amount is not touched.
cfg[NODE]["adaptive"] = {"on": False, "from_plant": True}
writes.clear()
L.run_once({NODE: {**slots, 50: raw(45)}}, lambda n, s, v: writes.append((n, s, v)) or True, now_ts=now)
check(not any(w[1] == 51 for w in writes), "learning off: the amount is left alone", writes)

# Automatic watering off on the node: nothing is touched at all.
cfg[NODE]["adaptive"] = {"on": True, "from_plant": True}
writes.clear()
L.run_once({NODE: {**slots, 49: 0}}, lambda n, s, v: writes.append((n, s, v)) or True, now_ts=now)
check(not writes, "auto watering off on the node: no writes", writes)

# Too little history: one automatic watering is not enough to act on.
store2 = H.Store(os.path.join(tmp, "t2.db"))
store2.add_readings([r for r in rows if r[0] < t0 + 20 * 3600])
L2 = W.WaterLearner(store2, lambda n: (DRY, WET, True), lambda n: cfg.get(n, {}))
rec2 = L2.recommend(NODE, slots, now_ts=t0 + 20 * 3600)
check(rec2["ml"] is None and "learning" in (rec2["why"] or ""), "one watering seen: still learning", rec2["why"])

print("\n%d failed" % failed if failed else "\nall passed")
sys.exit(1 if failed else 0)
