#!/usr/bin/env python3
"""Offline test for HiveAlerts (hive problems -> phone alerts via g4rden).

Drives HiveAlerts.check() with hand-made gateway dumps and a fake uploader, so
every rule is exercised without a gateway, a network or a phone:

    python tools/hive_admin/test_alerts.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hive_admin as H  # noqa: E402

failed = 0


def check(cond, msg, detail=None):
    global failed
    if cond:
        print("ok:", msg)
    else:
        failed += 1
        print("FAIL:", msg, "" if detail is None else detail)


class Store:
    def __init__(self):
        self.events = []

    def event(self, kind, text):
        self.events.append((kind, text))


class Cfg:
    def __init__(self):
        self.nodes = {5: {"name": "Rhubarb"}, 3: {}, 12: {"hidden": True}}
        self.data = {"stale_seconds": 300, "g4rden": {"token": "t" * 64, "enabled": True}}

    def node(self, nid):
        return self.nodes.get(nid)


class Uploader:
    def __init__(self):
        self.posts = []
        self.fail = False

    def post(self, path, body, token):
        self.posts.append((path, body))
        return {"ok": not self.fail, "status": 503 if self.fail else 200}


class App:
    def __init__(self):
        self.store = Store()
        self.cfg = Cfg()
        self.uploader = Uploader()
        kinds = {"WaterNode": {"errors": {"1001": {"title": "A watering never reached the soil",
                                                   "fix": "Check the bucket.", "severity": "warning"}}}}
        self.problems = H.Problems(self.store, kinds)


app = App()
A = H.HiveAlerts(app)


def node(age=10, code=None, count=None, subject=0):
    slots = {}
    if code is not None:
        slots[H.ERR_SLOT_LAST] = (subject << 16) | code
        slots[H.ERR_SLOT_COUNT] = count
    return {"age": age, "hops": 0, "slots": slots}


def run(nodes, t=None):
    """One poll; wait for the background send to finish."""
    before = len(app.uploader.posts)
    A.check({"nodes": nodes}, t or time.time())
    for _ in range(100):
        if not A.sending:
            break
        time.sleep(0.01)
    return [e for _, b in app.uploader.posts[before:] for e in b["events"]]


T0 = time.time()
# 1. First poll is a baseline: an existing E307 and an already-quiet node do not alert.
sent = run({5: node(code=307, count=2), 3: node(age=4000), 12: node(code=307, count=1)}, T0)
check(sent == [], "first poll after a start is a baseline: nothing re-alerts", sent)

# 2. Reservoir empty again (count 2 -> 3): instant alert with the fix text.
sent = run({5: node(code=307, count=3), 3: node(age=4100)}, T0 + 60)
check(len(sent) == 1 and sent[0]["kind"] == "node_error" and sent[0]["code"] == 307
      and sent[0]["title"].startswith("🐝 Rhubarb (node 5):"), "new E307 on node 5 -> one alert, named", sent)
check("supply is empty" in sent[0]["title"].lower() or "empty" in sent[0]["title"].lower(), "title says what is wrong", sent[0]["title"])

# 3. Same fault again within the cooldown: no second buzz.
sent = run({5: node(code=307, count=4)}, T0 + 120)
check(sent == [], "same node+code inside 6 h cooldown: suppressed", sent)

# 4. Not worth a buzz: weak link and unexpected reset.
sent = run({5: node(code=104, count=5), 3: node(code=208, count=1)}, T0 + 180)
sent += run({5: node(code=104, count=6), 3: node(code=208, count=2)}, T0 + 240)
check(sent == [], "E104 weak link / E208 restart never alert", sent)

# 5. A live node goes quiet -> once; back, then quiet again inside cooldown -> nothing.
run({11: node(age=10)}, T0 + 300)
sent = run({11: node(age=1900)}, T0 + 2200)
check(len(sent) == 1 and sent[0]["kind"] == "node_quiet" and "Node 11 went quiet" in sent[0]["title"],
      "node quiet past 30 min -> one alert", sent)
sent = run({11: node(age=1960)}, T0 + 2260)
check(sent == [], "still quiet: not repeated", sent)

# 6. Hidden (retired) nodes never alert.
sent = run({12: node(code=307, count=9), 12.0: node(age=99999)}, T0 + 2300)
check(sent == [], "hidden node: no alerts", sent)

# 7. Reboot: count restarts at 1 with an application code (E1001) -> alert.
run({3: node(code=208, count=7)}, T0 + 2400)
sent = run({3: node(code=1001, count=1)}, T0 + 2460)
check(len(sent) == 1 and sent[0]["code"] == 1001 and "watering never reached" in sent[0]["title"],
      "count restarted after reboot with E1001 -> alert", sent)

# 8. Gateway silent: E601 for > 10 min -> one alert; repeated polls do not repeat it.
app.problems.raise_(601, "3 polls in a row failed: incomplete dump")
app.problems.active[601]["since"] = T0 + 2500
sent = run({}, T0 + 2700)
check(sent == [], "E601 for 3 min: not yet", sent)
sent = run({}, T0 + 3200)
check(len(sent) == 1 and sent[0]["kind"] == "gateway", "E601 past 10 min -> gateway alert", sent)
sent = run({}, T0 + 3300)
check(sent == [], "...once per outage", sent)
app.problems.clear(601)
run({}, T0 + 3400)

# 9. A failed send is retried on the next poll, then not repeated once delivered.
app.uploader.fail = True
sent = run({5: node(code=306, count=10)}, T0 + 3500)
check(len(sent) == 1 and sent[0]["code"] == 306, "E306 attempted", sent)
app.uploader.fail = False
sent = run({5: node(code=306, count=10)}, T0 + 3560)
check(len(sent) == 1 and sent[0]["code"] == 306, "failed send retried on the next poll", sent)
sent = run({5: node(code=306, count=10)}, T0 + 3620)
check(sent == [], "delivered: not sent again", sent)

# 10. Not linked to g4rden: nothing is posted anywhere.
app.cfg.data["g4rden"]["enabled"] = False
sent = run({5: node(code=301, count=11)}, T0 + 3700)
check(sent == [], "g4rden uploads off: nothing sent", sent)

# 11. Soil alerts the person asked for ("tell me if the strawberry drops below 70 %").
app.cfg.data["g4rden"]["enabled"] = True
app.cfg.nodes[7] = {"name": "Strawberry"}
app.cfg.save = lambda: None
app.kinds = {"WaterNode": {"derived": {"soil": {"type": "soil_pct", "from": 1}}}, "WeatherNode": {"derived": {}}}
SOIL = {"v": 81.0, "age": 10}
app.state = lambda: {"nodes": [{"id": 7, "kind": "WaterNode", "age": SOIL["age"], "hidden": False,
                                "derived": {"soil": SOIL["v"]}},
                               {"id": 20, "kind": "WeatherNode", "age": 10, "hidden": False, "derived": {}}]}


def soil_at(v, age=10):
    SOIL["v"], SOIL["age"] = v, age
    return A.check_watches(app.state()["nodes"])


w = A.add_watch(7, below=70)
check(w["armed"] and w["below"] == 70 and w["soil"] == 81.0, "a soil alert set at 81 % for below 70 %: armed", w)
check(soil_at(75) == [], "75 %: nothing")
ev = soil_at(69.4)
check(len(ev) == 1 and "Strawberry" in ev[0]["title"] and "69%" in ev[0]["title"] and ev[0]["kind"] == "node_error"
      and "below 70%" in ev[0]["body"], "69 %: one alert, through g4rden's hive-alert kind", ev)
check(soil_at(68) == [] and soil_at(71) == [], "...not again while it stays near the line (68, then 71: not 2 points back)")
check(soil_at(72.5) == [] and app.cfg.data["watches"][0]["armed"], "...re-armed at 72.5 %")
check(len(soil_at(69)) == 1, "...and the next drop alerts again")
check(soil_at(72.5) == [] and soil_at(None) == [] and app.cfg.data["watches"][0]["armed"] and soil_at(50, age=4000) == [],
      "no reading (probe out) or a node not heard lately: no alert, nothing changes")
A.remove_watches(nid=7)
SOIL["v"] = 65.0
w = A.add_watch(7, below=70)
check(not w["armed"] and soil_at(64) == [], "set while already below the line: no alert until it has come back")
check(soil_at(73) == [] and len(soil_at(69)) == 1, "...then it alerts on the next drop")
w2 = A.add_watch(7, below=60)
check(len([x for x in app.cfg.data["watches"] if x["node"] == 7 and x["below"] is not None]) == 1 and w2["below"] == 60,
      "a second 'below' alert for the same plant replaces the first")
SOIL["v"] = 85.0
w3 = A.add_watch(7, above=90)
check(w3["armed"] and len([x for x in app.cfg.data["watches"] if x["node"] == 7]) == 2,
      "an 'above' alert sits beside the 'below' one")
SOIL["v"] = 85.0
soil_at(85)
ev = soil_at(91)
check(len(ev) == 1 and "above 90%" in ev[0]["body"], "above 90 %: alerts at 91", ev)
check(soil_at(89) == [] and soil_at(88) == [] and len(soil_at(91)) == 1, "...re-armed at 88 (2 points back), alerts again")
for kw, why in (({}, "no line"), ({"below": 50, "above": 90}, "two lines"), ({"below": 150}, "150 %"), ({"below": 0}, "0 %")):
    try:
        A.add_watch(7, **kw)
        check(False, "refused: %s" % why)
    except ValueError:
        check(True, "refused: %s" % why)
for nid, why in ((20, "a node with no soil probe"), (99, "a node that isn't reporting")):
    try:
        A.add_watch(nid, below=50)
        check(False, "refused: %s" % why)
    except ValueError:
        check(True, "refused: %s" % why)
n_before = len(app.cfg.data["watches"])
check(A.remove_watches(wid=app.cfg.data["watches"][0]["id"]) == 1 and len(app.cfg.data["watches"]) == n_before - 1,
      "one soil alert removed by its id")
check(A.remove_watches(nid=7) >= 1 and app.cfg.data["watches"] == [], "...or every one for a node")
SOIL["v"] = 80.0
A.add_watch(7, below=70)
SOIL["v"] = 66.0
sent = run({}, T0 + 9000)
check(len(sent) == 1 and "Strawberry" in sent[0]["title"], "the soil alert goes to the phones with the other alerts", sent)
check(any("soil alert added: Strawberry (node 7) below 70%" in t for _, t in app.store.events), "adding one is logged")

print("\n%d failure(s)" % failed if failed else "\nhive alerts: all checks passed")
sys.exit(1 if failed else 0)
