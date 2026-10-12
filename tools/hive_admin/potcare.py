"""Pot care the node's own automatic watering doesn't cover.

Seed mode. Seeds sit in the top centimetre; the probe reads several centimetres
down, where a pot stays wet for days after a watering (the bottom of a pot holds
water the drainage hole can't pull out). Automatic watering, judged at probe
depth, would leave the surface to dry out and the seeds to fail. Seed mode gives
a small dose on a timer instead - "10 ml every 12 hours until the 26th" - and
stops by itself on the end date. The node's own caps still apply to every dose.

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

SEED_MIN_ML, SEED_MAX_ML = 1, 200
SEED_MIN_EVERY_H, SEED_MAX_EVERY_H = 1, 72
SEED_MAX_DAYS = 45


def check_seed_mode(ml, every_h, days):
    """The person's settings, refused if they make no sense for seeds."""
    ml, every_h, days = int(ml), float(every_h), float(days)
    if not SEED_MIN_ML <= ml <= SEED_MAX_ML:
        raise ValueError("seed mode doses must be %d-%d ml: small and often keeps the surface damp"
                         % (SEED_MIN_ML, SEED_MAX_ML))
    if not SEED_MIN_EVERY_H <= every_h <= SEED_MAX_EVERY_H:
        raise ValueError("seed mode waters every %d-%d hours" % (SEED_MIN_EVERY_H, SEED_MAX_EVERY_H))
    if not 0 < days <= SEED_MAX_DAYS:
        raise ValueError("seed mode runs for up to %d days, then stops by itself" % SEED_MAX_DAYS)
    return ml, every_h, days


class SeedMode:
    """Sends each node in seed mode its dose when it's due. `cfg` is the admin's
    Config; `command(nid, slot, value)` sends a write and returns True on an ACK;
    `live(nid)` is True when the node is reporting; `event(kind, text)` logs;
    `slots(nid)` gives the node's last reported slots, to see each dose arrive.

    A dose is an action, sent once - but "ACK set" only means the gateway sent
    it, and a node at the edge of range misses some broadcasts. So each dose is
    checked against the node's own pumped-in-24-h count, and a dose that never
    showed up is sent ONE more time; a second miss is logged and left."""

    def __init__(self, cfg, command, live, event, slots=None):
        self.cfg, self.command, self.live, self.event = cfg, command, live, event
        self.slots = slots or (lambda nid: {})
        self.lock = threading.Lock()

    def start(self, nid, ml, every_h, days, now=None):
        ml, every_h, days = check_seed_mode(ml, every_h, days)
        now = now or time.time()
        conf = {"ml": ml, "every_h": every_h, "started": int(now), "until": int(now + days * 86400), "last": None}
        with self.lock:
            self.cfg.set_node(nid, {"seed_mode": conf})
        self.event("config", "node %d seed mode on: %d ml every %g h until %s" % (
            nid, ml, every_h, time.strftime("%b %d %H:%M", time.localtime(conf["until"]))))
        return conf

    def stop(self, nid, why="switched off"):
        with self.lock:
            if not self.cfg.node(nid).get("seed_mode"):
                return False
            self.cfg.set_node(nid, {"seed_mode": None})
        self.event("config", "node %d seed mode off (%s)" % (nid, why))
        return True

    def update(self, nid, **kw):
        with self.lock:
            cur = dict(self.cfg.node(nid).get("seed_mode") or {})
            if cur:
                cur.update(kw)
                self.cfg.set_node(nid, {"seed_mode": cur})

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
            chk = conf.get("check")
            if chk and now - chk["at"] >= CONFIRM_S:
                total = self.slots(nid).get(PUMPED_24H)
                seen = total is not None and total >= chk["before"] + conf["ml"] / 2.0
                self.update(nid, check=None)
                if not seen and not chk.get("retry"):
                    if self.live(nid) and self.command(nid, WATER_NOW, int(conf["ml"])):
                        self.update(nid, check={"at": int(now), "before": total or 0, "retry": True})
                        self.event("command", "node %d seed mode: the last %d ml dose didn't show up in the node's "
                                   "pumped total; sent once more" % (nid, conf["ml"]))
                    continue
                if not seen:
                    self.event("command", "node %d seed mode: a %d ml dose didn't show up even after a re-send; "
                               "check the node's link and pump" % (nid, conf["ml"]))
            if conf.get("last") and now - conf["last"] < conf["every_h"] * 3600:
                continue
            if not self.live(nid):
                continue                    # sent to a node that isn't listening, it would just be lost
            before = self.slots(nid).get(PUMPED_24H) or 0
            if self.command(nid, WATER_NOW, int(conf["ml"])):
                self.update(nid, last=int(now), check={"at": int(now), "before": before})
                self.event("command", "node %d seed mode: %d ml (every %g h until %s)" % (
                    nid, conf["ml"], conf["every_h"], time.strftime("%b %d", time.localtime(conf["until"]))))
                sent.append((nid, conf["ml"]))
            else:
                self.event("command", "node %d seed mode: the %d ml dose was not acknowledged; trying again "
                           "next pass" % (nid, conf["ml"]))
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
