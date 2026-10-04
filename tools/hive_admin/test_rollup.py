#!/usr/bin/env python3
"""Offline test for rollup.py (hourly/daily summaries and the raw cleanup).

Builds a throwaway database with readings whose summaries are known by hand,
then checks every rule: time weighting, outages, bad readings, counters that
restart, wind direction across north, days, and what the cleanup keeps.

    python tools/hive_admin/test_rollup.py
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rollup as R  # noqa: E402

failed = 0


def check(cond, msg, detail=None):
    global failed
    if cond:
        print("ok:", msg)
    else:
        failed += 1
        print("FAIL:", msg, "" if detail is None else detail)


class Store:
    def __init__(self, path):
        self.path = path
        with self.db() as c:
            c.executescript("""
            CREATE TABLE readings(ts INTEGER NOT NULL, node INTEGER NOT NULL,
                                  slot INTEGER NOT NULL, value INTEGER NOT NULL);
            CREATE INDEX readings_nst ON readings(node, slot, ts);""")

    def db(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, rows):
        with self.db() as c:
            c.executemany("INSERT INTO readings VALUES(?,?,?,?)", rows)


MODES = {1: {1: "mean", 8: "mean"},                      # node 1: a soil node
         20: {11: "mean", 13: "angle", 14: "counter", 8: "mean"}}


def plausible(node, slot, v):
    if slot == 1:
        return -4000 <= v <= 8500
    if slot == 13:
        return 0 <= v <= 359
    return True


tmp = tempfile.mkdtemp()
store = Store(os.path.join(tmp, "t.db"))
logs = []
ru = R.Rollup(store, lambda n: MODES.get(n, {}), plausible, raw_days=30, log=logs.append)

# A local midnight, so hours and days line up the way they will on the Pi.
D0 = R.local_day_start(time.time() - 40 * 86400)
H = 3600


def at(h, m=0, s=0):
    return D0 + h * H + m * 60 + s


rows = []
# Node 1, slot 1 (air temp x100): 20.00 C for the first half of hour 0,
# 30.00 C for the second half -> time-weighted 25.00, min 2000, max 3000.
rows += [(at(0), 1, 1, 2000), (at(0, 30), 1, 1, 3000)]
# Uptime pings every 10 min keep the node "heard" through hours 0-1.
rows += [(at(0, m), 1, 8, m) for m in range(0, 120, 10)]
# Hour 1: still 3000 all hour (carried in, no new row) -> avg 3000, n 0.
# Hour 2: a bad reading (-5000 is outside "valid") at :00 -- a gap, not a value --
# and node silent after hour 1's last ping at 1:50 -> alive until 2:25.
rows += [(at(2, 0), 1, 1, -5000)]
# Hour 5: back, 1000 for the whole hour.
rows += [(at(5, 0), 1, 1, 1000)] + [(at(5, m), 1, 8, 300 + m) for m in range(0, 60, 10)]
rows += [(at(6, 0), 1, 8, 360)]

# Node 20: rain tips counter 10 -> 12 -> 15, then a power cut restarts at 0 -> 2.
rows += [(at(0, 5), 20, 14, 10), (at(0, 20), 20, 14, 12), (at(0, 40), 20, 14, 15),
         (at(1, 10), 20, 14, 0), (at(1, 20), 20, 14, 2)]
# Wind direction 350 for half an hour then 10: the mean is north (0), not 180.
rows += [(at(0), 20, 13, 350), (at(0, 30), 20, 13, 10)]
rows += [(at(0, m), 20, 8, m) for m in range(0, 120, 10)]
# Calm (65535) is not a direction.
rows += [(at(1, 0), 20, 13, 65535)]
store.add(rows)

hours, days, pruned = ru.run_once(now_ts=at(30))      # day 1, 06:00


def hour(node, slot, h):
    r = [x for x in ru.series(node, slot, at(h), at(h) + 1)]
    return r[0] if r else None


r = hour(1, 1, 0)
check(r and abs(r[3] - 2500) < 0.01 and r[4] == 2000 and r[5] == 3000 and r[1] == 3600,
      "time-weighted mean, min, max over a full hour", r)
r = hour(1, 1, 1)
check(r and r[3] == 3000 and r[2] == 0 and r[1] == 3600, "a value carried into an hour with no rows", r)
r = hour(1, 1, 2)
check(r is not None and r[1] == 0 and r[3] is None and r[2] == 1,
      "a bad reading is a gap: counted as a row, never as a value", r)
r = hour(1, 1, 3)
check(r is None, "no row for an hour the node was silent", r)
r = hour(1, 1, 5)
check(r and r[3] == 1000 and r[1] == 3600, "readings resume after an outage", r)
r = hour(1, 8, 1)
check(r and r[1] == 3600, "uptime pings keep the node heard all hour", r)

r = hour(20, 14, 0)
check(r and r[6] == 5, "counter: 10 -> 12 -> 15 is 5 tips (first value is the baseline)", r)
r = hour(20, 14, 1)
check(r and r[6] == 2, "counter: a drop to 0 is a restart, not -15 tips", r)
r = hour(20, 13, 0)
check(r and (r[3] < 1 or r[3] > 359), "wind direction 350/10 averages to north", r)
r = hour(20, 13, 1)
check(r is None or r[1] == 0, "calm is not a direction", r)

d = ru.series(20, 14, D0, D0 + 1, res="day")
check(d and d[0][6] == 7, "daily rain = sum of hourly tips", d)
d = ru.series(1, 1, D0, D0 + 1, res="day")
check(d and d[0][4] == 1000 and d[0][5] == 3000, "daily min/max from the hours", d)
# Hours 0, 1, 5 in full, plus hour 6 until 35 min after its 06:00 ping.
exp = (2500 * 3600 + 3000 * 3600 + 1000 * 3600 + 1000 * 2100) / (3 * 3600 + 2100)
check(d and abs(d[0][3] - exp) < 0.01, "daily mean weighted by the time heard", (d, exp))

# Sensors-ok gating: node 30 reports 0.00 C while its air sensor is missing
# (slot 6 bit 1 clear) for the first half hour, then 20.00 C once it reads.
store.add([(at(0), 30, 6, 2), (at(0), 30, 1, 0), (at(0, 30), 30, 6, 3), (at(0, 30), 30, 1, 2000)]
          + [(at(0, m), 30, 8, m) for m in range(0, 70, 10)])
MODES[30] = {1: "mean", 8: "mean"}
ru2 = R.Rollup(store, lambda n: MODES.get(n, {}), plausible,
               slot_gates=lambda n: {1: (6, 1)} if n == 30 else {})
with store.db() as c:
    c.execute("DELETE FROM rollup_state")
ru2.run_once(now_ts=at(30))
r = ru2.series(30, 1, at(0), at(0) + 1)
r = r[0] if r else None
check(r and r[3] == 2000 and r[1] == 1800 and r[4] == 2000,
      "a value is a gap while its sensors-ok bit is clear", r)

# The sensor comes online a poll BEFORE its first reading arrives: the old
# 0.00 left standing must not count for that minute (node 31, live data shape).
store.add([(at(0), 31, 6, 2), (at(0), 31, 1, 0), (at(0, 20), 31, 6, 3), (at(0, 21), 31, 1, 2705)]
          + [(at(0, m), 31, 8, m) for m in range(0, 70, 10)])
MODES[31] = {1: "mean", 8: "mean"}
ru3 = R.Rollup(store, lambda n: MODES.get(n, {}), plausible,
               slot_gates=lambda n: {1: (6, 1)} if n in (30, 31) else {})
with store.db() as c:
    c.execute("DELETE FROM rollup_state")
ru3.run_once(now_ts=at(30))
r = ru3.series(31, 1, at(0), at(0) + 1)
r = r[0] if r else None
check(r and r[4] == 2705 and r[1] == 3600 - 21 * 60,
      "a value from before the sensor came online is stale, not a reading", r)

# Running again does nothing new.
h2, d2, p2 = ru.run_once(now_ts=at(30))
check((h2, d2) == (0, 0), "re-running finds nothing new", (h2, d2, p2))

# Cleanup: 45 days later everything above is past 30 days.
later = at(30) + 45 * 86400
store.add([(later - 60, 1, 8, 999)])
ru.run_once(now_ts=later)
with store.db() as c:
    left = c.execute("SELECT node, slot, ts, value FROM readings ORDER BY node, slot").fetchall()
kept = {(n, s): (t, v) for n, s, t, v in left if t < later - 30 * 86400}
check(kept.get((1, 1)) == (at(5, 0), 1000), "cleanup keeps the value in force (newest row per slot)",
      kept.get((1, 1)))
check(len([x for x in left if x[0] == 20 and x[1] == 14]) == 1, "older rows of the slot are gone", left)
check(any(x[2] == later - 60 for x in left), "recent rows untouched")
check(ru.raw_cutoff() > 0, "the cutoff is recorded for the charts")
check(hour(1, 1, 0) is not None, "summaries survive the cleanup")
check(any("pruned" in m for m in logs), "cleanup is logged", logs)

print("\n%d failed" % failed if failed else "\nall passed")
sys.exit(1 if failed else 0)
