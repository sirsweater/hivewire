"""Adaptive watering: learn how a pot takes up and loses water, and size the
automatic watering to match.

The node waters on its own when its probe reads drier than a threshold (see
HivewirePump.h, slots 49-54). What it cannot know is how MUCH to give: 100 ml
soaks a small pot and barely wets a big bed, and the same pot needs more in
July than in March. This watches every automatic watering in the history and
learns two numbers per node:

    gain   how far soil moisture rose per ml given (% per ml)
    dry    how fast it fell again afterwards (% per hour)

and sets the node's automatic amount (slot 51) to lift the soil from the
"water below" level to a "fill to" level. The node keeps every safety rule:
the per-dose and daily caps, the minimum gap, the lock-out when a watering
does not reach the probe. This only changes the amount, and only:

  - for a node whose owner switched it on (config "adaptive": {"on": true}),
    towards "fill_pct", or else the middle of the linked plant's range,
  - from AUTOMATIC waterings: a test dose into a measuring cup moves no soil
    and would teach it that the pot absorbs nothing,
  - once at least MIN_EVENTS waterings were seen in the soil,
  - by at most MAX_STEP per change, never past the per-dose cap.

Waterings are found from slot 45 (ml pumped in the last 24 h) going UP: the
pump-state slot can start and finish between two polls of a short dose, but
the running total cannot hide one. An automatic one also resets slot 54
(minutes since the last automatic watering).

With "from_plant", the "water below" level follows the moisture range of the
plant the node is linked to on g4rden (sent back with every upload, kept in
the node's config as "plant_band"): water below the low end, fill towards
the middle. Change the plant, or its stage, and the node follows.
"""
import threading
import time

PUMPED_24H = 45      # ml in the last 24 h (rolling)
AUTO_SINCE = 54      # minutes since the last automatic watering
AUTO_ML = 51         # amount per automatic watering
AUTO_BELOW = 50      # raw threshold
DOSE_CAP = 43
DAY_CAP = 44
SOIL_RAW = 3
AUTO_STATE = 53
AUTO_SETTLING = 6    # the node's own "probe just moved" state (firmware from 2026-10-05)

GROUP_S = 180        # increases this close together are one watering
PRE_S = 600          # the soil level "before" is taken this long before it was seen
PEAK_WINDOW_S = 3 * 3600
DRY_SKIP_S = 30 * 60 # let the water settle before measuring the drying slope
DRY_MIN_SPAN_S = 3 * 3600
MIN_EVENTS = 2
MAX_STEP = 0.4       # never change the amount by more than 40% at once
MIN_CHANGE = 0.1     # ...and not for less than 10%
MIN_RISE_PCT = 2.0   # a smaller rise was not really seen in the soil
# A probe pulled out or moved reads far drier within minutes - soil never dries
# that fast. Waterings near one say nothing about the pot: the "before" and
# "after" readings come from different spots (or from the air).
JUMP_PCT = 10.0
JUMP_S = 600
DISTURB_BEFORE_S = 3600


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def slope_per_hour(points):
    """Least-squares slope of [(ts, value)] in units per hour."""
    n = len(points)
    if n < 3:
        return None
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    sxx = sum((t - mx) ** 2 for t, _ in points)
    if sxx <= 0:
        return None
    sxy = sum((t - mx) * (v - my) for t, v in points)
    return sxy / sxx * 3600.0


def disturbances(soil):
    """Times the probe was moved: a reading JUMP_PCT drier than any in the
    JUMP_S before it. soil is [(ts, pct)] oldest first."""
    out = []
    for i, (t, v) in enumerate(soil):
        j = i - 1
        while j >= 0 and t - soil[j][0] <= JUMP_S:
            if soil[j][1] - v >= JUMP_PCT:
                out.append(t)
                break
            j -= 1
    return out


def waterings(series45, series54):
    """[(ts, ml, auto)] from the 24 h total going up. series are [(ts, value)]
    oldest first; the value before the first row is unknown, so the first row
    is only a baseline."""
    out = []
    prev = None
    for ts, v in series45:
        if prev is not None and v > prev:
            ml = v - prev
            if out and ts - out[-1][0] <= GROUP_S:
                t0, m0, _ = out[-1]
                out[-1] = (ts, m0 + ml, False)
            else:
                out.append((ts, ml, False))
        prev = v
    # Automatic when slot 54 restarted near it (a small value, after a larger one).
    resets = []
    last = None
    for ts, v in series54:
        if v != 65535 and (last is None or last == 65535 or v < last) and v <= 5:
            resets.append(ts)
        last = v
    return [(ts, ml, any(abs(r - ts) <= GROUP_S + 120 for r in resets)) for ts, ml, _ in out]


class WaterLearner:
    def __init__(self, store, calibration, cfg_node, log=None):
        """calibration(nid) -> (dry, wet, calibrated); cfg_node(nid) -> config dict."""
        self.store = store
        self.calibration = calibration
        self.cfg_node = cfg_node
        self.log = log or (lambda msg: None)

    def analyse(self, nid, now_ts=None, days=30):
        """What the history says about this pot. Never writes anything.

        Learning starts over whenever the probe is moved: what a watering did
        to the old spot says little about the new one (a probe sitting in its
        own puddle once read 50% from 49 ml). A move is the node reporting
        AUTO_SETTLING, a dry jump in the readings, or "learn_from" in the
        node's adaptive config, set by hand."""
        now_ts = int(now_ts or time.time())
        t0 = now_ts - days * 86400
        conf = self.cfg_node(nid).get("adaptive") or {}
        settled = [t for t, v in self.store.series(nid, AUTO_STATE, t0, now_ts) if v == AUTO_SETTLING]
        starts = [t0, int(conf.get("learn_from") or 0)] + settled
        dry_raw, wet_raw, calibrated = self.calibration(nid)
        out = {"node": nid, "calibrated": calibrated, "events": [], "gain": None, "dry_per_h": None}
        if not calibrated:
            out["why"] = "calibrate the soil probe first"
            return out

        def pct(raw):
            return 100.0 * (dry_raw - raw) / (dry_raw - wet_raw)

        s45 = self.store.series(nid, PUMPED_24H, t0, now_ts)
        s54 = self.store.series(nid, AUTO_SINCE, t0, now_ts)
        soil = [(t, pct(v)) for t, v in self.store.series(nid, SOIL_RAW, t0 - 6 * 3600, now_ts)
                if 200 < v < 4000]
        moved = disturbances(soil)
        # Start after the latest move; a watering it caused is still marked below.
        t0 = max(starts + [m - PRE_S - DISTURB_BEFORE_S for m in moved])
        out["learn_from"] = t0
        ev = [e for e in waterings(s45, s54) if e[0] >= t0]
        gains, drys = [], []
        for i, (ts, ml, auto) in enumerate(ev):
            nxt = ev[i + 1][0] if i + 1 < len(ev) else now_ts
            # ts is the poll that SAW the total go up, after the dose ended; the
            # dose itself (a few minutes at most) began up to PRE_S earlier. The
            # level before is the value in force then, however old its row.
            before = [v for t, v in soil if t <= ts - PRE_S]
            pre = before[-1] if before else None
            after = [(t, v) for t, v in soil if ts - PRE_S < t <= min(ts + PEAK_WINDOW_S, nxt)]
            e = {"ts": ts, "ml": ml, "auto": auto, "pre": pre, "peak": None, "rise": None, "dry_per_h": None}
            end = min(ts + PEAK_WINDOW_S, nxt)
            if any(ts - PRE_S - DISTURB_BEFORE_S <= m <= end for m in moved):
                e["moved"] = True                  # the probe was moved: learn nothing from it
                out["events"].append(e)
                continue
            if pre is not None and after:
                tp, peak = max(after, key=lambda p: p[1])
                e["peak"], e["rise"] = round(peak, 1), round(peak - pre, 1)
                if auto and peak - pre >= MIN_RISE_PCT:
                    gains.append((peak - pre) / ml)
                stop = min([m for m in moved if m > tp] + [nxt])
                tail = [(t, v) for t, v in soil if tp + DRY_SKIP_S <= t < stop]
                if tail and tail[-1][0] - tail[0][0] >= DRY_MIN_SPAN_S:
                    sl = slope_per_hour(tail)
                    if sl is not None and sl < 0:
                        e["dry_per_h"] = round(-sl, 2)
                        drys.append(-sl)
            out["events"].append(e)
        out["gain"] = median(gains[-5:])
        out["dry_per_h"] = median(drys[-5:])
        out["seen"] = len(gains)
        return out

    def recommend(self, nid, slots, now_ts=None):
        """The amount this pot should get, or None with a reason."""
        a = self.analyse(nid, now_ts)
        conf = (self.cfg_node(nid).get("adaptive") or {})
        rec = {"analysis": a, "ml": None, "why": a.get("why")}
        if not a["calibrated"]:
            return rec
        cur = slots.get(AUTO_ML)
        below = slots.get(AUTO_BELOW)
        if not cur or below is None:
            rec["why"] = "automatic watering is not set up"
            return rec
        dry_raw, wet_raw, _ = self.calibration(nid)
        below_pct = 100.0 * (dry_raw - below) / (dry_raw - wet_raw)
        band = self.cfg_node(nid).get("plant_band") if conf.get("from_plant") else None
        fill = conf.get("fill_pct")
        if fill is None and band:
            fill = (band[0] + band[1]) / 2.0
        fill = float(fill) if fill is not None else min(95.0, below_pct + 25.0)
        rec.update(below_pct=round(below_pct, 1), fill_pct=round(fill, 1), current=cur)
        if a["dry_per_h"]:
            rec["hours_between"] = round((fill - below_pct) / a["dry_per_h"], 1)
        if (a.get("seen") or 0) < MIN_EVENTS or not a["gain"]:
            rec["why"] = "learning: %d of %d automatic waterings seen in the soil so far" % (
                a.get("seen") or 0, MIN_EVENTS)
            return rec
        ideal = (fill - below_pct) / a["gain"]
        lo, hi = cur * (1 - MAX_STEP), cur * (1 + MAX_STEP)
        ml = max(lo, min(hi, ideal))
        cap = slots.get(DOSE_CAP)
        if cap:
            ml = min(ml, cap)
        ml = max(10, int(round(ml / 5.0)) * 5)
        rec["ideal"] = round(ideal)
        rec["ml"] = ml
        if abs(ml - cur) < max(5, cur * MIN_CHANGE):
            rec["why"] = "the current amount is about right"
            rec["ml"] = None
        else:
            rec["why"] = "adjust %d -> %d ml" % (cur, ml)
        return rec

    def below_raw_for_plant(self, nid):
        """The raw "water below" for the low end of the linked plant's range,
        or None when there is no range or no calibration to convert it."""
        band = self.cfg_node(nid).get("plant_band")
        dry, wet, calibrated = self.calibration(nid)
        if not band or not calibrated:
            return None
        return max(1, min(4095, int(round(dry - band[0] / 100.0 * (dry - wet)))))

    def run_once(self, nodes, command, now_ts=None):
        """nodes: {nid: slots} for every pump node heard. command(nid, slot, value)
        writes one slot; returns True when acknowledged."""
        done = []
        for nid, slots in nodes.items():
            conf = (self.cfg_node(nid).get("adaptive") or {})
            if not slots.get(49):
                continue
            if conf.get("from_plant"):
                want = self.below_raw_for_plant(nid)
                dry, wet, _ = self.calibration(nid)
                cur = slots.get(AUTO_BELOW)
                # Re-send only for a real difference (over 1% of the scale).
                if want is not None and (cur is None or abs(cur - want) > abs(dry - wet) / 100.0):
                    if command(nid, AUTO_BELOW, want):
                        self.log("node %d follows its plant: water below %s%% (raw %d)" % (
                            nid, self.cfg_node(nid)["plant_band"][0], want))
                        slots = dict(slots)
                        slots[AUTO_BELOW] = want
            if not conf.get("on"):
                continue
            rec = self.recommend(nid, slots, now_ts)
            if not rec.get("ml"):
                continue
            a = rec["analysis"]
            if command(nid, AUTO_ML, rec["ml"]):
                self.log("node %d learned watering: %d -> %d ml (soil +%.2f%% per 10 ml, dries %s%%/h; fill %s%% from %s%%)" % (
                    nid, rec["current"], rec["ml"], a["gain"] * 10,
                    "%.1f" % a["dry_per_h"] if a["dry_per_h"] else "?", rec["fill_pct"], rec["below_pct"]))
                done.append((nid, rec["ml"]))
        return done


class LearnThread(threading.Thread):
    EVERY_S = 1800

    def __init__(self, learner, nodes, command):
        super().__init__(daemon=True, name="waterlearn")
        self.learner, self.nodes, self.command = learner, nodes, command

    def run(self):
        time.sleep(120)
        while True:
            try:
                self.learner.run_once(self.nodes(), self.command)
            except Exception as e:                  # never take the admin down
                self.learner.log("water learning failed: %s" % e)
            time.sleep(self.EVERY_S)
