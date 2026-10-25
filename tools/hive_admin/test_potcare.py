#!/usr/bin/env python3
"""Offline test for potcare.py: seed mode's timer and limits, the soak analysis
that finds a pot's field capacity, and the band scaled to it.

    python tools/hive_admin/test_potcare.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import potcare as P  # noqa: E402

failed = 0


def check(cond, msg, detail=None):
    global failed
    if cond:
        print("ok: " + msg)
    else:
        failed += 1
        print("FAIL: %s %s" % (msg, "" if detail is None else detail))


# --- field capacity from a soak ------------------------------------------------
# The shape the cactus pot showed on 2026-10-12: ~48% before, 100% within four
# minutes of soaking, a bouncing drain, then a level it holds.
T = 1_800_000_000
soak = [(T - 7200 + i * 600, 48.0) for i in range(12)]                  # two hours at 48%
soak += [(T, 70.0), (T + 60, 99.5), (T + 120, 100.0), (T + 300, 100.0)]  # the soak
soak += [(T + 600, 85.0), (T + 700, 87.0), (T + 900, 82.0), (T + 1100, 86.0)]  # drain, bouncing
soak += [(T + 1500 + i * 120, 78.0 + (0.5 if i % 2 else 0)) for i in range(40)]  # holds ~78
a = P.analyze_soak(soak, now=T + 1500 + 40 * 120)
check(a["status"] == "settled" and abs(a["settled"] - 78.2) < 1 and a["peak"] == 100.0 and a["start"] == 48.0,
      "a soak's peak, start and settled level are found", a)
a = P.analyze_soak(soak[:20], now=soak[19][0])
check(a["status"] == "draining" and "settled" not in a, "...and while it's still bouncing, it says draining", a)
check(P.analyze_soak([(T + i * 600, 50.0 + i * 0.1) for i in range(30)])["status"] == "no soak",
      "...slow drift is not a soak")
check(P.analyze_soak([(T, 45.0), (T + 600, 65.0)])["status"] == "no soak",
      "...nor a watering that never reached saturation")
check(P.analyze_soak([])["status"] == "no soak", "...nor no data")

# --- the band scaled to the pot --------------------------------------------------
check(P.scaled_band([5, 20], 80.0) == [4.0, 16.0], "a 5-20% band at 80% field capacity is 4-16%")
check(P.scaled_band([45, 75], None) == [45, 75] and P.scaled_band(None, 80) is None, "...unchanged without both")

# --- seed mode -----------------------------------------------------------------


class _Cfg:
    def __init__(self):
        self.data = {"nodes": {}}

    def node(self, nid):
        return self.data["nodes"].get(str(nid), {})

    def set_node(self, nid, patch):
        n = dict(self.node(nid))
        for k, v in patch.items():
            if v in (None, ""):
                n.pop(k, None)
            else:
                n[k] = v
        self.data["nodes"][str(nid)] = n


cfg, sent, events, up = _Cfg(), [], [], {7: True}
ack = {"ok": True}
seed = P.SeedMode(cfg, lambda nid, slot, v: (sent.append((nid, slot, v)), ack["ok"])[1],
                  lambda nid: up.get(nid, False), lambda kind, text: events.append(text))
for bad in ((0, 12, 7), (500, 12, 7), (10, 0.2, 7), (10, 12, 0), (10, 12, 90)):
    try:
        seed.start(7, *bad, now=T)
        check(False, "seed mode refuses %r" % (bad,))
    except ValueError:
        check(True, "seed mode refuses %r" % (bad,))
seed.start(7, 10, 12, 3, now=T)
check(seed.run_once(now=T + 5) == [(7, 10)] and sent == [(7, 40, 10)], "the first dose goes at once, as water-now ml")
check(seed.run_once(now=T + 6 * 3600) == [], "...nothing until the interval passes")
check(seed.run_once(now=T + 12 * 3600 + 10) == [(7, 10)], "...then the next one")
up[7] = False
check(seed.run_once(now=T + 24 * 3600 + 20) == [], "a node that isn't reporting gets nothing (it would be lost)")
up[7], ack["ok"] = True, False
n = len(sent)
check(seed.run_once(now=T + 24 * 3600 + 30) == [] and len(sent) == n + 1 and cfg.node(7)["seed_mode"]["last"] == T + 12 * 3600 + 10,
      "an unacknowledged dose isn't counted, so it's tried again next pass")
ack["ok"] = True
check(seed.run_once(now=T + 24 * 3600 + 90) == [(7, 10)], "...and goes when the gateway sends it")
check(seed.run_once(now=T + 3 * 86400 + 1) == [] and "seed_mode" not in cfg.node(7) and
      any("end date passed" in e for e in events), "it stops by itself on the end date, and says so")
seed.start(7, 10, 12, 3, now=T)
check(seed.stop(7) and "seed_mode" not in cfg.node(7) and not seed.stop(7), "switching it off clears it once")

# A dose the node never reported (a missed broadcast) is sent once more, not forever.
pumped = {"45": 0}
cfg2, sent2, ev2 = _Cfg(), [], []
s2 = P.SeedMode(cfg2, lambda nid, slot, v: (sent2.append(v), True)[1], lambda nid: True,
                lambda kind, text: ev2.append(text), slots=lambda nid: {45: pumped["45"]})
s2.start(7, 10, 12, 3, now=T)
s2.run_once(now=T)
check(s2.run_once(now=T + 300) == [] and len(sent2) == 1, "no re-send before the check time")
s2.run_once(now=T + 700)
check(len(sent2) == 2 and any("sent once more" in e for e in ev2), "a dose not seen in the pumped total is sent once more", ev2)
s2.run_once(now=T + 1400)
check(len(sent2) == 2 and any("even after a re-send" in e for e in ev2), "...and a second miss is logged, not re-sent", ev2)
pumped["45"] = 10
s2.run_once(now=T + 12 * 3600 + 5)
pumped["45"] = 20                       # the node reports the dose
s2.run_once(now=T + 12 * 3600 + 700)
check(len(sent2) == 3, "...while a dose that shows up is left alone (no re-send)", sent2)

# The node's total is a rolling 24 h. Doses every 6 h: each new one lands as the one from 24 h before ages out, so the
# total doesn't move - that must not read as a lost dose (it did, and every dose was sent twice). Modelled like the real
# thing: the hive passes once a minute, and the node reports its total every 10 minutes and right after it pumps.
def rolling_run(seed_cls, lose=None):
    cfg4, sent4, ev4, log4, rep = _Cfg(), [], [], [], {"value": 0}
    clock = {"now": T}
    total = lambda now: sum(ml for ts, ml in log4 if now - ts < 86400)  # noqa: E731
    def cmd4(nid, slot, v):
        sent4.append(v)
        if len(sent4) - 1 == lose:                                      # this one never reaches the pump
            return True
        log4.append((clock["now"] + 20, v))                             # it pumps a few seconds later...
        rep["value"] = total(clock["now"] + 25); return True            # ...and reports straight after
    s4 = seed_cls(cfg4, cmd4, lambda nid: True, lambda kind, text: ev4.append(text), slots=lambda nid: {45: rep["value"]})
    s4.start(7, 5, 6, 10, now=T)
    for minute in range(0, 3 * 24 * 60):
        clock["now"] = T + minute * 60
        if minute % 10 == 0:
            rep["value"] = total(clock["now"])                          # the regular report
        s4.run_once(now=clock["now"])
    return sent4, ev4
sent4, ev4 = rolling_run(P.SeedMode)
lost_sent, lost_ev = rolling_run(P.SeedMode, lose=6)                  # day two, with the 24 h window full
check(len(lost_sent) == 13 and sum("sent once more" in e for e in lost_ev) == 1 and not any("even after" in e for e in lost_ev),
      "...while a dose really lost once the window is full is still sent once more (and the re-send counts)", (len(lost_sent), lost_ev[-2:]))
check(not any("sent once more" in e or "even after" in e for e in ev4) and len(sent4) == 12,
      "doses every 6 h for three days: none mistaken for lost once the 24 h window is full, none sent twice",
      (len(sent4), [e for e in ev4 if "once more" in e or "even after" in e][:2]))

# A node that restarted lost its 24 h total: whether the dose arrived is unknown, so no re-send.
cfg3, sent3, ev3, pumped3 = _Cfg(), [], [], {"45": 40}
s3 = P.SeedMode(cfg3, lambda nid, slot, v: (sent3.append(v), True)[1], lambda nid: True,
                lambda kind, text: ev3.append(text), slots=lambda nid: {45: pumped3["45"]})
s3.start(7, 10, 12, 3, now=T)
s3.run_once(now=T)
pumped3["45"] = 0                       # rebooted: the count starts over
s3.run_once(now=T + 700)
check(len(sent3) == 1 and any("restarted" in e for e in ev3), "a dose isn't re-sent after the node restarted", ev3)

# --- germination holds doses while the probe reads soggy ---------------------------
cfg4, sent4, ev4, soil4 = _Cfg(), [], [], {"pct": 85.0}
s4 = P.SeedMode(cfg4, lambda nid, slot, v: (sent4.append(v), True)[1], lambda nid: True,
                lambda kind, text: ev4.append(text), soil=lambda nid: soil4["pct"])
s4.start(7, 5, 6, 60, now=T, max_days=60, stage="germination", skip_above=80)
check(s4.run_once(now=T) == [] and cfg4.node(7)["seed_mode"].get("skipping"), "no dose while the probe reads 85% (80% holds)")
check(sum("holding doses" in e for e in ev4) == 1, "...said once", ev4)
s4.run_once(now=T + 60)
check(sum("holding doses" in e for e in ev4) == 1, "...not again every pass", ev4)
soil4["pct"] = 76.0
check(s4.run_once(now=T + 120) == [(7, 5)] and "skipping" not in cfg4.node(7)["seed_mode"] and
      any("doses resume" in e for e in ev4), "once it dries below, the dose goes at once and that's said", ev4)
check(s4.run_once(now=T + 3 * 3600) == [], "...then the interval holds")
soil4["pct"] = None
check(s4.run_once(now=T + 6 * 3600 + 200) == [(7, 5)], "no probe reading: the timer waters anyway (seeds can't wait)")
check(any("germination mode" in e for e in ev4) and not any("seed mode:" in e for e in ev4), "its log lines say germination mode", ev4)
try:
    s4.start(7, 5, 6, 60, now=T)
    check(False, "plain seed mode still stops at 45 days")
except ValueError:
    check(True, "plain seed mode still stops at 45 days")

# --- grow stages --------------------------------------------------------------------
cfg5, ev5, autos, learn = _Cfg(), [], [], []
auto_ok = {"ok": True}
seed5 = P.SeedMode(cfg5, lambda nid, slot, v: True, lambda nid: True, lambda kind, text: ev5.append(text),
                   soil=lambda nid: 60.0)
gs = P.GrowStages(cfg5, seed5, lambda nid, en, **kw: (autos.append((en, kw)), auto_ok["ok"])[1],
                  lambda kind, text: ev5.append(text), learn_from=lambda nid, ts: learn.append(ts))
st = gs.germinate(7, now=T)
check(st["stage"] == "germination" and cfg5.node(7)["seed_mode"]["stage"] == "germination" and
      cfg5.node(7)["seed_mode"]["skip_above"] == 80 and cfg5.node(7)["seed_mode"]["ml"] == P.GERM_ML,
      "germination: timed doses with the soggy hold, defaults", cfg5.node(7))
check(autos[-1] == (False, {}), "...and the node's own automatic watering switched off", autos)
check(cfg5.node(7)["seed_mode"]["until"] - T == P.GERM_MAX_DAYS * 86400, "...for up to %d days" % P.GERM_MAX_DAYS)
check(st["band"] == [60.0, 80.0], "...its range stands in for the plant's", st)
for bad in ((5, 6, 20), (0, 6, 80), (5, 0.1, 80)):
    try:
        gs.germinate(8, *bad, now=T)
        check(False, "germination refuses %r" % (bad,))
    except ValueError:
        check(True, "germination refuses %r" % (bad,))
auto_ok["ok"] = False
try:
    gs.germinate(9, now=T)
    check(False, "germination needs the node to switch its own watering off")
except RuntimeError:
    check("seed_mode" not in cfg5.node(9) and "grow_stage" not in cfg5.node(9),
          "germination needs the node to switch its own watering off: nothing starts without it")
try:
    gs.seedling(7, now=T + 9 * 86400)
    check(False, "an unacknowledged seedling switch is refused")
except RuntimeError:
    check(cfg5.node(7)["grow_stage"]["stage"] == "germination" and cfg5.node(7).get("seed_mode"),
          "an unacknowledged seedling switch leaves germination running")
auto_ok["ok"] = True
st = gs.seedling(7, now=T + 9 * 86400)
check(st["stage"] == "seedling" and "seed_mode" not in cfg5.node(7), "seedling: germination's timer stops", cfg5.node(7))
check(autos[-1] == (True, {"below_pct": 55.0, "ml": 10, "gap_min": 180, "day_ml": 60, "pulses": 2}),
      "...the node's automatic watering is set for seedlings", autos[-1])
check(st["germinated_at"] == T + 9 * 86400 and st["germination_since"] == T and
      any("germinated after 9.0 days" in e for e in ev5), "...and how long germination took is kept", ev5)
check(learn == [T + 9 * 86400], "...and the learner starts over", learn)
st2 = gs.seedling(7, below_pct=60, now=T + 10 * 86400)
check(st2["germinated_at"] == T + 9 * 86400 and st2["settings"]["below_pct"] == 60, "re-saving seedling settings keeps the germination record")
check(gs.finish(7, now=T + 30 * 86400) and "grow_stage" not in cfg5.node(7) and not gs.finish(7),
      "grown: out of the stage, once")
gs.germinate(7, now=T)
seed5.stop(7)
check("grow_stage" not in cfg5.node(7), "stopping germination's doses (the Seed mode card) ends the stage too")
gs.germinate(7, now=T)
seed5.run_once(now=T + P.GERM_MAX_DAYS * 86400 + 1)
check("grow_stage" not in cfg5.node(7) and "seed_mode" not in cfg5.node(7), "60 days without germinating: it ends by itself")

print("\n%s" % ("%d FAILED" % failed if failed else "all passed"))
sys.exit(1 if failed else 0)
