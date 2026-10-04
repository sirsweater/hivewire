"""Hourly and daily summaries of every measurement, and the raw-data cleanup
that keeps the database from growing for ever.

Raw readings are change-based: a row at time T means "this value held from T
until the next row for that slot". That is what the live cards and recent
charts need, and it is far too much to keep for years. Once an hour is over it
becomes one row per measurement:

    rollup_hour(node, slot, ts, secs, n, avg, min, max, sum)
    rollup_day (node, slot, ts, secs, n, avg, min, max, sum)

    ts    start of the hour / local day (epoch seconds)
    secs  how long the node was actually heard during it: a value does not
          "hold" through an outage, so a node unplugged at noon does not
          average its last reading over the afternoon
    n     raw rows that arrived in it
    avg   time-weighted mean, in the slot's raw units (scale with kinds.json)
    sum   counters only: how much the running count went up (rain tips)

Which slots, and how, comes from kinds.json: every slot with "chart": true is
averaged; "rollup": "counter" sums the increments of a running count (it
restarts at 0 after a power cut, which is a reset, not a negative rainfall);
"rollup": "angle" takes a vector mean, so north-west and north-east average
to north rather than to south. "ok_bit": n says the value only means
something while that bit of the node's sensors-ok slot is set: a weather
node with no air sensor fitted reports 0.00 C, and that must be a gap, not a
frost.

Raw rows older than RAW_DAYS are deleted once their hours are summarised --
except the newest row of each slot before the cutoff, which is the value
still in force at that moment (a pump setting written once and never changed
must not vanish from the node's state).
"""
import math
import threading
import time

RAW_DAYS = 30
# A node is "heard" for this long after any row from it. Every node reports
# uptime at least every 15 minutes, so a longer silence is an outage.
ALIVE_S = 35 * 60
HOUR = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS rollup_hour(
  node INTEGER NOT NULL, slot INTEGER NOT NULL, ts INTEGER NOT NULL,
  secs INTEGER NOT NULL, n INTEGER NOT NULL,
  avg REAL, min INTEGER, max INTEGER, sum INTEGER,
  PRIMARY KEY(node, slot, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rollup_day(
  node INTEGER NOT NULL, slot INTEGER NOT NULL, ts INTEGER NOT NULL,
  secs INTEGER NOT NULL, n INTEGER NOT NULL,
  avg REAL, min INTEGER, max INTEGER, sum INTEGER,
  PRIMARY KEY(node, slot, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rollup_state(
  key TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def hour_floor(t):
    return int(t) // HOUR * HOUR


def local_day_start(t):
    lt = time.localtime(t)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def next_local_day(day_ts):
    # +26 h then floor: lands in the next day whatever DST did.
    return local_day_start(day_ts + 26 * HOUR)


class Acc:
    """One slot over one period."""
    __slots__ = ("secs", "n", "wsum", "x", "y", "lo", "hi", "inc")

    def __init__(self):
        self.secs = self.n = 0
        self.wsum = self.x = self.y = 0.0
        self.lo = self.hi = None
        self.inc = 0

    def seen(self, v):
        self.lo = v if self.lo is None else min(self.lo, v)
        self.hi = v if self.hi is None else max(self.hi, v)

    def hold(self, v, secs, mode):
        if secs <= 0:
            return
        self.secs += secs
        if mode == "angle":
            a = math.radians(v)
            self.x += math.sin(a) * secs
            self.y += math.cos(a) * secs
        else:
            self.wsum += v * secs

    def row(self, mode):
        if not self.secs and not self.n:
            return None
        avg = None
        if self.secs:
            if mode == "angle":
                if abs(self.x) > 1e-9 or abs(self.y) > 1e-9:
                    avg = round(math.degrees(math.atan2(self.x, self.y)) % 360.0, 1)
            else:
                avg = round(self.wsum / self.secs, 3)
        return (self.secs, self.n, avg, self.lo, self.hi,
                self.inc if mode == "counter" else None)


class Rollup:
    def __init__(self, store, slot_modes, plausible, raw_days=RAW_DAYS, log=None,
                 slot_gates=None):
        """slot_modes(node) -> {slot: "mean" | "counter" | "angle"} for that
        node's kind; plausible(node, slot, value) -> bool (kinds.json "valid");
        slot_gates(node) -> {slot: (ok_slot, bit)} (kinds.json "ok_bit")."""
        self.store = store
        self.slot_modes = slot_modes
        self.slot_gates = slot_gates or (lambda node: {})
        self.plausible = plausible
        self.raw_days = raw_days
        self.log = log or (lambda msg: None)
        self.lock = threading.Lock()
        with store.db() as c:
            c.executescript(SCHEMA)

    # --- state ---------------------------------------------------------------
    def _get(self, c, key):
        r = c.execute("SELECT value FROM rollup_state WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set(self, c, key, value):
        c.execute("INSERT INTO rollup_state VALUES(?,?) ON CONFLICT(key) "
                  "DO UPDATE SET value=excluded.value", (key, int(value)))

    def raw_cutoff(self):
        """Raw readings before this moment may have been deleted."""
        with self.store.db() as c:
            v = self._get(c, "raw_cutoff")
        return v or 0

    # --- hours ---------------------------------------------------------------
    def roll_hours(self, now_ts=None, max_hours=24 * 60):
        """Summarise every finished hour not yet done. Returns hours written."""
        now_ts = int(now_ts or time.time())
        end = hour_floor(now_ts)                    # the current hour is not finished
        with self.store.db() as c:
            start = self._get(c, "hour_done")
            if start is None:
                first = c.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
                if first is None:
                    return 0
                start = hour_floor(first)
            nodes = [r[0] for r in c.execute("SELECT DISTINCT node FROM readings")]
        end = min(end, start + max_hours * HOUR)
        if end <= start:
            return 0
        rows = []
        for node in nodes:
            modes = self.slot_modes(node)
            if modes:
                rows += self._node_hours(node, modes, start, end)
        with self.store.db() as c:
            c.executemany("INSERT OR REPLACE INTO rollup_hour VALUES(?,?,?,?,?,?,?,?,?)", rows)
            self._set(c, "hour_done", end)
        return (end - start) // HOUR

    def _node_hours(self, node, modes, t0, t1):
        gates = {s: g for s, g in self.slot_gates(node).items() if s in modes}
        ok_slots = sorted({g[0] for g in gates.values()})
        slots = sorted(set(modes) | set(ok_slots))
        q = ",".join("?" * len(slots))
        # Sensors-ok rows sort first within a poll, so a value and the bit that
        # says whether to believe it are judged together.
        first = ",".join(str(int(x)) for x in ok_slots) or "-1"
        with self.store.db() as c:
            # Value in force at t0 for each slot, and when the node was last heard.
            cur = {}
            for s, v, ts in c.execute(
                    "SELECT slot, value, MAX(ts) FROM readings WHERE node=? AND ts<? AND slot IN (%s) "
                    "GROUP BY slot" % q, [node, t0] + slots):
                cur[s] = v
            last_seen = c.execute("SELECT MAX(ts) FROM readings WHERE node=? AND ts<?",
                                  (node, t0)).fetchone()[0]
            events = c.execute(
                "SELECT ts, slot, value FROM readings WHERE node=? AND ts>=? AND ts<? "
                "ORDER BY ts, CASE WHEN slot IN (%s) THEN 0 ELSE 1 END" % first,
                (node, t0, t1)).fetchall()
        okv = {s: cur.pop(s) for s in ok_slots if s in cur and s not in modes}
        for s in ok_slots:
            if s in modes and s in cur:
                okv[s] = cur[s]

        def believed(s):
            g = gates.get(s)
            return g is None or bool(okv.get(g[0], 0) & g[1])
        # The count a counter had before t0 -- increments are measured from it.
        prev_count = {s: cur.get(s) for s in slots if modes.get(s) == "counter"}
        for s in list(cur):
            if not self.plausible(node, s, cur[s]):
                cur.pop(s)

        accs = {}                                   # (hour, slot) -> Acc

        def acc(h, s):
            a = accs.get((h, s))
            if a is None:
                a = accs[(h, s)] = Acc()
            return a

        def hold_until(a_ts, b_ts):
            """Credit every slot's current value for [a_ts, b_ts) while alive."""
            if last_seen is None:
                return
            stop = min(b_ts, last_seen + ALIVE_S)
            t = a_ts
            while t < stop:
                h = hour_floor(t)
                seg_end = min(stop, h + HOUR)
                for s, v in cur.items():
                    m = modes[s]
                    if m == "counter" or not believed(s):
                        continue
                    x = acc(h, s)
                    x.hold(v, seg_end - t, m)
                    x.seen(v)
                t = seg_end

        t = t0
        for ts, s, v in events:
            if ts > t:
                hold_until(t, ts)
                t = ts
            last_seen = ts
            if s in okv or s in ok_slots:
                # A sensor that just came online makes the value standing from
                # before it stale (a weather node holds 0.00 C until its air
                # sensor answers): a gap until the sensor's own first reading.
                gained = v & ~okv.get(s, 0)
                for gs, (o, bit) in gates.items():
                    if o == s and gained & bit:
                        cur.pop(gs, None)
                okv[s] = v
            if s not in modes:
                continue
            h = hour_floor(ts)
            m = modes[s]
            if m == "counter":
                p = prev_count.get(s)
                x = acc(h, s)
                x.n += 1
                if p is not None:
                    x.inc += v - p if v >= p else v   # a drop is a restart from 0
                prev_count[s] = v
                x.seen(v)
                continue
            acc(h, s).n += 1
            if self.plausible(node, s, v):
                cur[s] = v
                if believed(s):
                    acc(h, s).seen(v)
            else:
                cur.pop(s, None)                     # a bad reading is a gap, not a value
        hold_until(t, t1)

        out = []
        for (h, s), a in accs.items():
            r = a.row(modes[s])
            if r:
                out.append((node, s, h) + r)
        return out

    # --- days ----------------------------------------------------------------
    def roll_days(self, now_ts=None):
        """Build each finished local day from its hours."""
        now_ts = int(now_ts or time.time())
        with self.store.db() as c:
            hour_done = self._get(c, "hour_done")
            if hour_done is None:
                return 0
            day = self._get(c, "day_done")
            if day is None:
                first = c.execute("SELECT MIN(ts) FROM rollup_hour").fetchone()[0]
                if first is None:
                    return 0
                day = local_day_start(first)
            done = 0
            while True:
                nxt = next_local_day(day)
                if nxt > hour_done or nxt > now_ts:
                    break
                self._day(c, day, nxt)
                day = nxt
                done += 1
                self._set(c, "day_done", day)
        return done

    def _day(self, c, d0, d1):
        groups = {}
        for node, slot, ts, secs, n, avg, lo, hi, sm in c.execute(
                "SELECT node, slot, ts, secs, n, avg, min, max, sum FROM rollup_hour "
                "WHERE ts>=? AND ts<?", (d0, d1)):
            groups.setdefault((node, slot), []).append((secs, n, avg, lo, hi, sm))
        rows = []
        for (node, slot), hs in groups.items():
            mode = self.slot_modes(node).get(slot, "mean")
            secs = sum(h[0] for h in hs)
            n = sum(h[1] for h in hs)
            lows = [h[3] for h in hs if h[3] is not None]
            highs = [h[4] for h in hs if h[4] is not None]
            weighted = [(h[0], h[2]) for h in hs if h[2] is not None and h[0]]
            avg = None
            if weighted:
                w = sum(s for s, _ in weighted)
                if mode == "angle":
                    x = sum(s * math.sin(math.radians(a)) for s, a in weighted)
                    y = sum(s * math.cos(math.radians(a)) for s, a in weighted)
                    if abs(x) > 1e-9 or abs(y) > 1e-9:
                        avg = round(math.degrees(math.atan2(x, y)) % 360.0, 1)
                else:
                    avg = round(sum(s * a for s, a in weighted) / w, 3)
            sm = sum(h[5] or 0 for h in hs) if mode == "counter" else None
            rows.append((node, slot, d0, secs, n, avg,
                         min(lows) if lows else None, max(highs) if highs else None, sm))
        c.executemany("INSERT OR REPLACE INTO rollup_day VALUES(?,?,?,?,?,?,?,?,?)", rows)

    # --- cleanup -------------------------------------------------------------
    def prune(self, now_ts=None):
        """Delete raw rows older than raw_days that are already summarised.
        Returns rows deleted."""
        now_ts = int(now_ts or time.time())
        with self.store.db() as c:
            hour_done = self._get(c, "hour_done") or 0
            cut = min(now_ts - self.raw_days * 86400, hour_done)
            if cut <= (self._get(c, "raw_cutoff") or 0):
                return 0
            # Keep the newest row of each slot before the cutoff: the value
            # still in force there. (SQLite returns the MAX(ts) row's rowid.)
            c.execute("CREATE TEMP TABLE keep AS SELECT rowid AS id, MAX(ts) FROM readings "
                      "WHERE ts<? GROUP BY node, slot", (cut,))
            n = c.execute("DELETE FROM readings WHERE ts<? AND rowid NOT IN (SELECT id FROM keep)",
                          (cut,)).rowcount
            c.execute("DROP TABLE keep")
            self._set(c, "raw_cutoff", cut)
        return n

    def vacuum_if_due(self, now_ts=None):
        """Hand freed pages back to the disk once a week. Deleted rows are
        reused anyway, so this only matters after the first big cleanup."""
        now_ts = int(now_ts or time.time())
        with self.store.db() as c:
            last = self._get(c, "vacuumed") or 0
            if now_ts - last < 7 * 86400:
                return False
            self._set(c, "vacuumed", now_ts)
        c = self.store.db()
        try:
            c.isolation_level = None
            c.execute("VACUUM")
        finally:
            c.close()
        return True

    def run_once(self, now_ts=None):
        with self.lock:
            hours = days = pruned = 0
            while True:                             # catch up in 60-day steps
                h = self.roll_hours(now_ts)
                hours += h
                if h < 24 * 60:
                    break
            days = self.roll_days(now_ts)
            pruned = self.prune(now_ts)
            vac = self.vacuum_if_due(now_ts) if pruned else False
            if hours > 1 or days or pruned:
                self.log("rollup: %d hours, %d days summarised; %d raw rows pruned%s" % (
                    hours, days, pruned, ", vacuumed" if vac else ""))
            return hours, days, pruned

    def partial_hour(self, node, now_ts=None):
        """The hour in progress, summarised up to now: {slot: row tuple} with
        row = (secs, n, avg, min, max, sum). Not stored -- it changes."""
        now_ts = int(now_ts or time.time())
        modes = self.slot_modes(node)
        h = hour_floor(now_ts)
        if not modes or now_ts <= h:
            return h, {}
        return h, {r[1]: r[3:] for r in self._node_hours(node, modes, h, now_ts)}

    def hours_by_ts(self, node, t0, t1):
        """Finished hours in [t0, t1): {hour_ts: {slot: (secs, n, avg, min, max, sum)}}."""
        out = {}
        with self.store.db() as c:
            for r in c.execute("SELECT ts, slot, secs, n, avg, min, max, sum FROM rollup_hour "
                               "WHERE node=? AND ts>=? AND ts<? ORDER BY ts", (node, t0, t1)):
                out.setdefault(r[0], {})[r[1]] = tuple(r[2:])
        return out

    def hour_done(self):
        with self.store.db() as c:
            return self._get(c, "hour_done") or 0

    def get_mark(self, key):
        with self.store.db() as c:
            return self._get(c, key)

    def set_mark(self, key, value):
        with self.store.db() as c:
            self._set(c, key, value)

    # --- reading back --------------------------------------------------------
    def series(self, node, slot, t0, t1, res="hour"):
        table = "rollup_day" if res == "day" else "rollup_hour"
        with self.store.db() as c:
            return [tuple(r) for r in c.execute(
                "SELECT ts, secs, n, avg, min, max, sum FROM %s WHERE node=? AND slot=? "
                "AND ts>=? AND ts<? ORDER BY ts" % table, (node, slot, t0, t1))]


def slot_modes_from_kind(spec):
    """{slot: mode} for one kinds.json entry."""
    out = {}
    for s, meta in (spec or {}).get("slots", {}).items():
        mode = meta.get("rollup")
        if mode is False:
            continue
        if mode in ("counter", "angle", "mean"):
            out[int(s)] = mode
        elif meta.get("chart"):
            out[int(s)] = "mean"
    return out


def slot_gates_from_kind(spec):
    """{slot: (ok_slot, bit)} for slots whose meaning depends on a sensors-ok bit."""
    ok = (spec or {}).get("ok_slot", {}).get("slot")
    if ok is None:
        return {}
    return {int(s): (int(ok), int(meta["ok_bit"]))
            for s, meta in (spec or {}).get("slots", {}).items() if meta.get("ok_bit")}


class RollupThread(threading.Thread):
    EVERY_S = 600

    def __init__(self, rollup):
        super().__init__(daemon=True, name="rollup")
        self.rollup = rollup
        self.wake = threading.Event()

    def run(self):
        time.sleep(30)                              # let the poller settle first
        while True:
            try:
                self.rollup.run_once()
            except Exception as e:                  # never take the admin down
                self.rollup.log("rollup failed: %s" % e)
            self.wake.wait(self.EVERY_S)
            self.wake.clear()
