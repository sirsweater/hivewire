#!/usr/bin/env python3
"""Offline test for forecasts.py: parse, store, prune, and score against the
station's hourly summaries - no network.

    python tools/hive_admin/test_forecasts.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import forecasts as F  # noqa: E402
import hive_admin as H  # noqa: E402
import rollup as R  # noqa: E402

failed = 0


def check(cond, msg, detail=None):
    global failed
    if cond:
        print("ok:", msg)
    else:
        failed += 1
        print("FAIL:", msg, "" if detail is None else detail)


HOUR = 3600
now = (int(time.time()) // HOUR) * HOUR + 600          # ten past the hour
issued = now // HOUR * HOUR
times = [issued + HOUR * k for k in range(0, 8)]       # Open-Meteo stamps

def body_for(temp_offsets):
    """Two models; temperature = 20 + offset; rain 1 mm in the hour ending at times[3]."""
    h = {"time": times}
    for m, off in temp_offsets.items():
        h["temperature_2m_%s" % m] = [20 + off] * len(times)
        h["precipitation_%s" % m] = [0, 0, 0, 1.0, 0, 0, 0, 0]
        h["wind_speed_10m_%s" % m] = [10] * len(times)
        h["relative_humidity_2m_%s" % m] = [60, None, 60, 60, 60, 60, 60, 60]
    return {"hourly": h}

rows = F.parse(body_for({"gfs_global": 2, "ecmwf_ifs025": -1}))
rain = [r for r in rows if r[0] == "gfs_global" and r[2] == "rain" and r[3] > 0]
check(rain and rain[0][1] == times[3] - HOUR, "rain is stored against the hour it fell in (stamp - 1 h)", rain)
check(not any(r[2] == "hum" and r[1] == times[1] for r in rows), "missing values are skipped, not stored as 0")
check(not any(r[0] == "gfs_hrrr" for r in rows), "a model absent from the reply stores nothing")

tmp = tempfile.mkdtemp()
store = H.Store(os.path.join(tmp, "t.db"))
R.Rollup(store, lambda n: {}, lambda *a: True)          # creates rollup_hour
cfg = {"lat": 33.2, "lon": -97.1}
calls = []
fc = F.Forecasts(store, lambda: cfg, fetch=lambda url: (calls.append(url), body_for({"gfs_global": 2, "ecmwf_ifs025": -1}))[1])
n = fc.collect(now=now)
check(n > 0 and calls and "latitude=33.200" in calls[0] and "gfs_global" in calls[0], "collects from the configured place and models", (n, calls[:1]))
with store.db() as c:
    cnt = c.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0]
    old = c.execute("SELECT COUNT(*) FROM forecasts WHERE valid < ?", (issued,)).fetchone()[0]
check(cnt == n and old == 0, "only hours from now on are stored", (cnt, old))

# Not configured: nothing fetched.
calls.clear()
check(F.Forecasts(store, lambda: {}, fetch=lambda u: calls.append(u)).collect(now=now) == 0 and not calls,
      "no location configured: no fetch")

# Score: the station measured 21.0 C every hour and 1 mm of rain in the right hour.
with store.db() as c:
    for k in range(0, 7):
        c.execute("INSERT INTO rollup_hour VALUES(?,?,?,?,?,?,?,?,?)", (20, 1, times[k], 3600, 1, 2100.0, 2100, 2100, None))
    c.execute("INSERT INTO rollup_hour VALUES(?,?,?,?,?,?,?,?,?)", (20, 14, times[2], 3600, 1, None, 0, 4, 4))
later = times[7] + 10
s = fc.score(20, days=1, now=later)
g, e = s["gfs_global"]["temp"]["0-6h"], s["ecmwf_ifs025"]["temp"]["0-6h"]
check(abs(g["bias"] - 1.0) < 1e-6 and abs(g["mae"] - 1.0) < 1e-6, "GFS 22 C vs measured 21: bias +1, error 1", g)
check(abs(e["bias"] + 2.0) < 1e-6 and abs(e["mae"] - 2.0) < 1e-6, "ECMWF 19 C vs 21: bias -2, error 2", e)
r = s["gfs_global"]["rain"]["0-6h"]
# (scores are rounded to 0.01)
check(r["n"] == 1 and abs(r["bias"] - (1.0 - 4 * 0.2794)) < 0.006, "rain scored against the gauge's tips in that hour", r)

# Prune: forecasts issued more than KEEP_DAYS ago go.
with store.db() as c:
    c.execute("INSERT INTO forecasts VALUES(?,?,?,?,?)", (now - 40 * 86400, "gfs_global", now - 40 * 86400, "temp", 1.0))
fc.collect(now=now)
with store.db() as c:
    check(c.execute("SELECT COUNT(*) FROM forecasts WHERE issued < ?", (now - 35 * 86400,)).fetchone()[0] == 0,
          "forecasts older than 35 days are pruned")

print("\n%d failed" % failed if failed else "\nall passed")
sys.exit(1 if failed else 0)
