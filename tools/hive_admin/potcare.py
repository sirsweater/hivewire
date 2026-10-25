"""Pot care the node's own automatic watering doesn't cover.

Seed mode. Seeds sit in the top centimetre; the probe reads several centimetres
down, where a pot stays wet for days after a watering (the bottom of a pot holds
water the drainage hole can't pull out). Automatic watering, judged at probe
depth, would leave the surface to dry out and the seeds to fail. Seed mode gives
a small dose on a timer instead - "10 ml every 12 hours until the 26th" - and
stops by itself on the end date. The node's own caps still apply to every dose.

Grow stages. A pot of seeds is watered three ways in turn. Germination: seed
mode's small timed doses, held back while the probe reads soggy (wet seed mix
grows mould and rots seeds), with the node's own automatic watering off, until
the person marks the seeds germinated. Seedling: the node's automatic watering,
set for seedlings - small pulsed doses below a wetter level than most plants
want, with a gap that lets the surface dry a little (damping-off thrives on a
surface that never does). Then the plant's own band and the learned amount.

Field capacity. How wet this pot's soil stays once a soak has drained: the most
it holds. Soak the pot, let it drain, and the probe's reading falls from 100% and
levels off; that level is the pot's field capacity on the probe's air-to-water
scale. Measured per pot, because a gritty cactus mix and a peat seed mix hold
very different amounts. With it, a node can have its plant's moisture band
scaled to its own pot ("band_scale": "fc"), so a band the pot can never reach
stops looking "too wet" forever.
"""
import threading
import time

WATER_NOW = 40       # slot: water N ml now
PUMPED_24H = 45      # slot: ml pumped in the last 24 h
CONFIRM_S = 600      # a dose not seen in the node's own total by then is sent once more
REPORT_S = 900       # how stale the node's last reported 24 h total can be (it reports every few minutes)

SEED_MIN_ML, SEED_MAX_ML = 1, 200
SEED_MIN_EVERY_H, SEED_MAX_EVERY_H = 1, 72
SEED_MAX_DAYS = 45


def check_seed_mode(ml, every_h, days, max_days=SEED_MAX_DAYS):
    """The person's settings, refused if they make no sense for seeds."""
    ml, every_h, days = int(ml), float(every_h), float(days)
    if not SEED_MIN_ML <= ml <= SEED_MAX_ML:
        raise ValueError("seed mode doses must be %d-%d ml: small and often keeps the surface damp"
                         % (SEED_MIN_ML, SEED_MAX_ML))
    if not SEED_MIN_EVERY_H <= every_h <= SEED_MAX_EVERY_H:
        raise ValueError("seed mode waters every %d-%d hours" % (SEED_MIN_EVERY_H, SEED_MAX_EVERY_H))
    if not 0 < days <= max_days:
        raise ValueError("seed mode runs for up to %d days, then stops by itself" % max_days)
    return ml, every_h, days


def label(conf):
    return "germination mode" if (conf or {}).get("stage") == "germination" else "seed mode"


class SeedMode:
    """Sends each node in seed mode its dose when it's due. `cfg` is the admin's
    Config; `command(nid, slot, value)` sends a write and returns True on an ACK;
    `live(nid)` is True when the node is reporting; `event(kind, text)` logs;
    `slots(nid)` gives the node's last reported slots, to see each dose arrive.

    A dose is an action, sent once - but "ACK set" only means the gateway sent
    it, and a node at the edge of range misses some broadcasts. So each dose is
    checked against the node's own pumped-in-24-h count, and a dose that never
    showed up is sent ONE more time; a second miss is logged and left."""

    def __init__(self, cfg, command, live, event, slots=None, soil=None):
        """soil(nid): the probe's moisture % now, or None when there is no
        trustworthy reading (uncalibrated, unplugged, out of range)."""
        self.cfg, self.command, self.live, self.event = cfg, command, live, event
        self.slots = slots or (lambda nid: {})
        self.soil = soil or (lambda nid: None)
        self.lock = threading.Lock()

    def start(self, nid, ml, every_h, days, now=None, max_days=SEED_MAX_DAYS, **extra):
        """extra: "stage" (set by GrowStages) and "skip_above" (no dose while
        the probe reads this % or more) are kept with the settings."""
        ml, every_h, days = check_seed_mode(ml, every_h, days, max_days)
        now = now or time.time()
        conf = {"ml": ml, "every_h": every_h, "started": int(now), "until": int(now + days * 86400), "last": None}
        conf.update({k: v for k, v in extra.items() if v is not None})
        with self.lock:
            self.cfg.set_node(nid, {"seed_mode": conf})
        self.event("config", "node %d %s on: %d ml every %g h%s until %s" % (
            nid, label(conf), ml, every_h,
            ", none while the probe reads %g%% or more" % conf["skip_above"] if "skip_above" in conf else "",
            time.strftime("%b %d %H:%M", time.localtime(conf["until"]))))
        return conf

    def stop(self, nid, why="switched off"):
        with self.lock:
            conf = self.cfg.node(nid).get("seed_mode")
            if not conf:
                return False
            self.cfg.set_node(nid, {"seed_mode": None})
            # Germination's doses ARE its stage: without them it isn't germinating.
            if conf.get("stage") == "germination" and (self.cfg.node(nid).get("grow_stage") or {}).get("stage") == "germination":
                self.cfg.set_node(nid, {"grow_stage": None})
        self.event("config", "node %d %s off (%s)" % (nid, label(conf), why))
        return True

    def update(self, nid, **kw):
        with self.lock:
            cur = dict(self.cfg.node(nid).get("seed_mode") or {})
            if cur:
                cur.update(kw)
                cur = {k: v for k, v in cur.items() if v is not None or k == "last"}
                self.cfg.set_node(nid, {"seed_mode": cur})

    @staticmethod
    def _sent(conf, now):
        """The doses the hive has sent this node in the last 26 h, with this one: [[ts, ml], ...] - what is due to
        age out of the node's rolling 24 h total when a dose is checked."""
        return [d for d in (conf.get("sent") or []) if now - d[0] < 26 * 3600] + [[int(now), int(conf["ml"])]]

    def run_once(self, now=None):
        """One pass: end what's expired, water what's due. Returns [(nid, ml)] sent."""
        now = now or time.time()
        sent = []
        for key, n in list(self.cfg.data.get("nodes", {}).items()):
            conf = n.get("seed_mode")
            if not conf or not key.isdigit():
                continue
            nid = int(key)
            if now >= conf.get("until", 0):
                self.stop(nid, "its end date passed")
                continue
            what = label(conf)
            chk = conf.get("check")
            if chk and now - chk["at"] >= CONFIRM_S:
                total = self.slots(nid).get(PUMPED_24H)
                # The node's total is a ROLLING 24 h: since "before" was read, whatever the hive sent 24 h
                # earlier has dropped out of it. With doses every 6 h, yesterday's dose left exactly as today's
                # arrived and the total didn't move - every dose looked lost and was sent twice.
                # ("before" is the node's last report, up to REPORT_S old: a dose sent a little over 24 h before it
                # can still be in it and gone from the total now)
                aged = sum(ml for ts, ml in conf.get("sent") or [] if chk["at"] - 86400 - REPORT_S <= ts <= now - 86400)
                base = chk["before"] - aged
                seen = total is not None and total >= base + conf["ml"] / 2.0
                self.update(nid, check=None)
                if total is not None and total < base:
                    # The node's 24 h total went DOWN: it restarted and lost the count,
                    # so it can't say whether the dose arrived. A re-send could double it.
                    self.event("command", "node %d %s: the node restarted, so whether the last %d ml dose "
                               "arrived is unknown; not re-sending it" % (nid, what, conf["ml"]))
                    seen = True
                if not seen and not chk.get("retry"):
                    if self.live(nid) and self.command(nid, WATER_NOW, int(conf["ml"])):
                        self.update(nid, check={"at": int(now), "before": total or 0, "retry": True},
                                    sent=self._sent(conf, now))
                        self.event("command", "node %d %s: the last %d ml dose didn't show up in the node's "
                                   "pumped total; sent once more" % (nid, what, conf["ml"]))
                    continue
                if not seen:
                    self.event("command", "node %d %s: a %d ml dose didn't show up even after a re-send; "
                               "check the node's link and pump" % (nid, what, conf["ml"]))
            if conf.get("last") and now - conf["last"] < conf["every_h"] * 3600:
                continue
            if not self.live(nid):
                continue                    # sent to a node that isn't listening, it would just be lost
            skip = conf.get("skip_above")
            if skip is not None:
                pct = self.soil(nid)
                if pct is not None and pct >= skip:
                    # Soggy seed mix grows mould and rots seeds: wait until it dries a little.
                    if not conf.get("skipping"):
                        self.update(nid, skipping=True)
                        self.event("command", "node %d %s: holding doses while the probe reads %.0f%% (%g%% or "
                                   "more is too wet for seeds)" % (nid, what, pct, skip))
                    continue
                if conf.get("skipping"):
                    self.update(nid, skipping=None)
                    self.event("command", "node %d %s: probe down to %s; doses resume" % (
                        nid, what, "%.0f%%" % pct if pct is not None else "no reading"))
            before = self.slots(nid).get(PUMPED_24H) or 0
            if self.command(nid, WATER_NOW, int(conf["ml"])):
                self.update(nid, last=int(now), check={"at": int(now), "before": before}, sent=self._sent(conf, now))
                self.event("command", "node %d %s: %d ml (every %g h until %s)" % (
                    nid, what, conf["ml"], conf["every_h"], time.strftime("%b %d", time.localtime(conf["until"]))))
                sent.append((nid, conf["ml"]))
            else:
                self.event("command", "node %d %s: the %d ml dose was not acknowledged; trying again "
                           "next pass" % (nid, what, conf["ml"]))
        return sent


class SeedThread(threading.Thread):
    EVERY_S = 60

    def __init__(self, seed):
        super().__init__(daemon=True, name="seedmode")
        self.seed = seed

    def run(self):
        while True:
            try:
                self.seed.run_once()
            except Exception as e:              # never take the admin down
                self.seed.event("system", "seed mode pass failed: %s" % e)
            time.sleep(self.EVERY_S)


# --- grow stages ----------------------------------------------------------------

GERM_ML, GERM_EVERY_H, GERM_SKIP_ABOVE = 5, 6, 80
GERM_MAX_DAYS = 60    # strawberries can take six weeks; a seed not up by then won't be
SEEDLING_BELOW, SEEDLING_ML, SEEDLING_GAP_H, SEEDLING_DAY_ML = 55, 10, 3, 60
SEEDLING_PULSES = 2


def check_germination(ml, every_h, skip_above):
    ml, every_h, _ = check_seed_mode(ml, every_h, 1)
    skip = float(skip_above)
    if not 30 <= skip <= 100:
        raise ValueError("germination holds doses above a level of 30-100 %%, not %s" % skip_above)
    return ml, every_h, skip


def check_seedling(below_pct, ml, gap_h, day_ml):
    below, ml, gap_h, day_ml = float(below_pct), int(ml), float(gap_h), int(day_ml)
    if not 20 <= below <= 90:
        raise ValueError("seedlings are watered below 20-90 %%, not %s" % below_pct)
    if not 1 <= ml <= 200:
        raise ValueError("a seedling dose is 1-200 ml")
    if not 0.5 <= gap_h <= 72:
        raise ValueError("seedlings get 0.5-72 hours between waterings")
    if not ml <= day_ml <= 2000:
        raise ValueError("the daily maximum must be at least one dose, and at most 2000 ml")
    return below, ml, gap_h, day_ml


class GrowStages:
    """Germination, then seedling, then the plant's own watering (see the top of
    this file). The stage lives in the node's config as "grow_stage":
    {"stage", "since", "band"[, "germinated_at", "germination_since", "settings"]};
    "band" is the stage's moisture range, which stands in for the plant's while
    the stage lasts. While a node has a stage, the learner and the admin's
    default switch-on leave its automatic watering alone (hive_admin.py).

    seed: SeedMode. auto(nid, enable, below_pct=None, ml=None, gap_min=None,
    day_ml=None, pulses=None) sends the node's automatic-watering settings and
    returns True only when every one was acknowledged. learn_from(nid, ts)
    restarts the learner's history (a seedling pot is not the pot it learned)."""

    def __init__(self, cfg, seed, auto, event, learn_from=None):
        self.cfg, self.seed, self.auto, self.event = cfg, seed, auto, event
        self.learn_from = learn_from or (lambda nid, ts: None)

    def stage(self, nid):
        return self.cfg.node(nid).get("grow_stage")

    def germinate(self, nid, ml=GERM_ML, every_h=GERM_EVERY_H, skip_above=GERM_SKIP_ABOVE, now=None):
        ml, every_h, skip = check_germination(ml, every_h, skip_above)
        now = int(now or time.time())
        # The timer is the only thing watering seeds: the node's own automatic
        # watering, judged at probe depth, would drown or starve them.
        if not self.auto(nid, False):
            raise RuntimeError("the node didn't acknowledge switching its own automatic watering off; "
                               "germination mode not started")
        self.seed.start(nid, ml, every_h, GERM_MAX_DAYS, now=now, max_days=GERM_MAX_DAYS,
                        stage="germination", skip_above=skip)
        st = {"stage": "germination", "since": now, "band": [max(0.0, skip - 20), skip]}
        self.cfg.set_node(nid, {"grow_stage": st})
        self.event("config", "node %d germination mode: %d ml every %g h, held while the probe reads %g%% or more; "
                   "the node's own automatic watering is off until the seeds are marked germinated" % (
                       nid, ml, every_h, skip))
        return st

    def seedling(self, nid, below_pct=SEEDLING_BELOW, ml=SEEDLING_ML, gap_h=SEEDLING_GAP_H,
                 day_ml=SEEDLING_DAY_ML, now=None):
        below, ml, gap_h, day_ml = check_seedling(below_pct, ml, gap_h, day_ml)
        now = int(now or time.time())
        cur = self.stage(nid) or {}
        if not self.auto(nid, True, below_pct=below, ml=ml, gap_min=int(round(gap_h * 60)),
                         day_ml=day_ml, pulses=SEEDLING_PULSES):
            raise RuntimeError("not every automatic-watering setting was acknowledged by the node; "
                               "still in %s" % ("%s mode" % cur["stage"] if cur else "no stage"))
        if cur.get("stage") == "germination":
            self.seed.stop(nid, "the seeds germinated")
        st = {"stage": "seedling", "since": now, "band": [below, min(100.0, below + 20)],
              "settings": {"below_pct": below, "ml": ml, "gap_h": gap_h, "day_ml": day_ml, "pulses": SEEDLING_PULSES}}
        took = ""
        if cur.get("stage") == "germination":
            st["germination_since"], st["germinated_at"] = cur.get("since"), now
            if cur.get("since"):
                took = " after %.1f days" % ((now - cur["since"]) / 86400.0)
        elif cur.get("germinated_at"):
            st["germination_since"], st["germinated_at"] = cur.get("germination_since"), cur["germinated_at"]
        self.cfg.set_node(nid, {"grow_stage": st})
        self.learn_from(nid, now)
        self.event("config", "node %d %sseedling mode: the node waters %d ml in %d pulses below %g%%, at least %g h "
                   "apart, at most %d ml a day" % (
                       nid, "seeds germinated%s; " % took if cur.get("stage") == "germination" else "",
                       ml, SEEDLING_PULSES, below, gap_h, day_ml))
        return st

    def finish(self, nid, now=None):
        """Out of any stage: the plant's band and the learned amount take over."""
        cur = self.stage(nid)
        if not cur:
            return False
        now = int(now or time.time())
        if cur.get("stage") == "germination":
            self.seed.stop(nid, "germination mode ended")
        self.cfg.set_node(nid, {"grow_stage": None})
        self.learn_from(nid, now)
        self.event("config", "node %d out of %s mode: the plant's own band and learned watering take over%s" % (
            nid, cur.get("stage"), "" if cur.get("stage") == "seedling" else
            " (the node's own automatic watering is still off: switch it on on its card)"))
        return True


# --- field capacity -------------------------------------------------------------

SOAK_RISE = 15.0      # a rise this big (points) within an hour is a soak, not drift
SETTLE_AFTER_S = 1800 # draining needs at least this long after the peak
SETTLE_SPAN_S = 1800  # ...and a window this long that holds...
SETTLE_BAND = 2.0     # ...within this many points


def analyze_soak(points, now=None):
    """[(ts, soil %)] change-based, oldest first, covering a soak. Finds the most
    recent soak (a fast rise to a peak) and, once the drain has levelled off, the
    level it settled at: this pot's field capacity.

    Returns {"status": "no soak" | "draining" | "settled", "peak", "peak_at",
    "start", "settled", "settled_at"}."""
    now = now or time.time()
    pts = [(t, v) for t, v in points if v is not None]
    if len(pts) < 2:
        return {"status": "no soak"}
    # Change-based rows: carry the last value forward to now, so a level that
    # simply held shows up as holding.
    if pts[-1][0] < now:
        pts.append((now, pts[-1][1]))
    # The most recent peak that followed a fast rise.
    peak_i = None
    for i in range(len(pts) - 1, -1, -1):
        t, v = pts[i]
        before = [w for s, w in pts if t - 3600 <= s < t]
        if before and v - min(before) >= SOAK_RISE and v >= 90:
            peak_i = i
            break
    if peak_i is None:
        return {"status": "no soak"}
    # The top of that soak: the highest value in the hour after the rise began.
    t_rise = pts[peak_i][0]
    window = [(t, v) for t, v in pts if t_rise <= t <= t_rise + 3600]
    peak_t, peak_v = max(window, key=lambda p: (p[1], -p[0]))
    start_v = min(w for s, w in pts if t_rise - 3600 <= s < t_rise)
    out = {"status": "draining", "peak": round(peak_v, 1), "peak_at": int(peak_t), "start": round(start_v, 1)}
    after = [(t, v) for t, v in pts if t >= peak_t + SETTLE_AFTER_S]
    # Walk forward: the first window of SETTLE_SPAN_S that held within SETTLE_BAND.
    for i, (t, _) in enumerate(after):
        win = [(s, w) for s, w in after[i:] if s <= t + SETTLE_SPAN_S]
        if win[-1][0] - t < SETTLE_SPAN_S - 60:
            break
        vals = [w for _, w in win]
        if max(vals) - min(vals) <= SETTLE_BAND:
            out.update(status="settled", settled=round(sum(vals) / len(vals), 1), settled_at=int(t))
            break
    return out


def scaled_band(band, fc):
    """A plant's band on this pot's scale: the band's numbers are read as a share
    of what the pot holds when drained (field capacity), not of a cup of water."""
    if not band or not fc:
        return band
    return [round(band[0] * fc / 100.0, 1), round(band[1] * fc / 100.0, 1)]
