#!/usr/bin/env python3
"""Hive admin: a web page for configuring, monitoring and reporting on a
Hivewire swarm, served from the computer on the hive gateway's USB port.

    python3 hive_admin.py --port /dev/serial/by-id/usb-...      # real gateway
    python3 hive_admin.py --fake                                 # simulated swarm

Then browse to http://<this machine>:8080 and set a password on first visit.

Standard library only (plus pyserial for the real gateway), and the page loads
nothing from the internet, so it keeps working at a site with no connection.

This process OWNS the gateway's serial port. Two programs reading one tty split
its output between them, so each sees half of its own replies; everything that
talks to the gateway -- polling, slot writes, log requests, firmware pushes --
goes through one lock here. External tools matching --yield-to are respected:
while one runs, this stays off the port.

Only `dump` is polled. It answers on USB alone, so polling costs no LoRa
airtime. Commands a person issues (set, mode, log <node>) are relayed over
every uplink by the gateway, LoRa included -- which is why they are never
issued automatically.
"""
import argparse
import base64
import hashlib
import hmac
import http.server
import json
import os
import random
import re
import secrets
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
sys.path.insert(0, HERE)
from rollup import (Rollup, RollupThread, hour_floor, slot_gates_from_kind,  # noqa: E402
                    slot_modes_from_kind)
from waterlearn import LearnThread, WaterLearner  # noqa: E402
from forecasts import Forecasts, ForecastThread  # noqa: E402

DUMP_LINE = re.compile(r"^DUMP (\d+) age=(\d+) hops=(\d+)(.*)$")
# Stored like a slot so it charts and reports like everything else, but it is
# the hive's observation of the node, not something the node published. Node
# slot ids are an application's own business, so this sits well clear of them.
HOPS_SLOT = 250
DUMP_END = re.compile(r"DUMP END (\d+)(.*)")
PAIRS = re.compile(r"(\w+)=(-?\d+)")
FAMILY = re.compile(rb"<hwfam:([A-Za-z0-9_.-]{1,40})>")


def now():
    return int(time.time())


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class Store:
    """SQLite, one connection per call: the HTTP server is threaded."""

    def __init__(self, path):
        self.path = path
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS readings(
              ts INTEGER NOT NULL, node INTEGER NOT NULL,
              slot INTEGER NOT NULL, value INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS readings_nst ON readings(node, slot, ts);
            CREATE TABLE IF NOT EXISTS events(
              ts INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS pushes(
              id INTEGER PRIMARY KEY, ts INTEGER, file TEXT, size INTEGER,
              crc TEXT, family TEXT, result TEXT, log TEXT);
            -- How far each node's readings have been uploaded. Kept here, not
            -- in memory, so a restart mid-outage resumes instead of re-sending.
            CREATE TABLE IF NOT EXISTS upload_marks(
              node INTEGER PRIMARY KEY, last_ts INTEGER NOT NULL);
            """)

    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def add_readings(self, rows):
        with self.db() as c:
            c.executemany("INSERT INTO readings VALUES(?,?,?,?)", rows)

    def event(self, kind, text):
        with self.db() as c:
            c.execute("INSERT INTO events VALUES(?,?,?)", (now(), kind, text))

    def notes(self, t0, t1):
        """Markers a person left, for drawing on charts: "moved the cactus",
        "unplugged it". A reading alone cannot say why it changed."""
        with self.db() as c:
            return [{"ts": r[0], "text": r[2]} for r in c.execute(
                "SELECT * FROM events WHERE kind='note' AND ts BETWEEN ? AND ? ORDER BY ts",
                (t0, t1))]

    def events(self, limit=200):
        with self.db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))]

    def series(self, node, slot, t0, t1):
        with self.db() as c:
            return [(r[0], r[1]) for r in c.execute(
                "SELECT ts, value FROM readings WHERE node=? AND slot=? AND ts BETWEEN ? AND ? "
                "ORDER BY ts", (node, slot, t0, t1))]

    def state_at(self, node, ts):
        """{slot: value} in force at a moment: the last row at or before it."""
        with self.db() as c:
            return {r[0]: r[1] for r in c.execute(
                "SELECT slot, value, MAX(ts) FROM readings WHERE node=? AND ts<=? GROUP BY slot",
                (node, ts))}

    def rows_since(self, node, since, until):
        with self.db() as c:
            return [(r[0], r[1], r[2]) for r in c.execute(
                "SELECT ts, slot, value FROM readings WHERE node=? AND ts>? AND ts<=? "
                "ORDER BY ts", (node, since, until))]

    def upload_mark(self, node):
        with self.db() as c:
            r = c.execute("SELECT last_ts FROM upload_marks WHERE node=?", (node,)).fetchone()
        return r[0] if r else 0

    def set_upload_mark(self, node, ts):
        with self.db() as c:
            c.execute("INSERT INTO upload_marks VALUES(?,?) ON CONFLICT(node) "
                      "DO UPDATE SET last_ts=excluded.last_ts", (node, ts))

    def max_ts(self):
        with self.db() as c:
            r = c.execute("SELECT MAX(ts) FROM readings").fetchone()
        return r[0] or 0

    def latest(self, node):
        """{slot: value} as last stored for a node. (SQLite returns the row
        holding MAX(ts) for bare columns in an aggregate query.)"""
        with self.db() as c:
            return {r[0]: r[1] for r in c.execute(
                "SELECT slot, value, MAX(ts) FROM readings WHERE node=? GROUP BY slot", (node,))}

    def last_before(self, node, slot, t):
        with self.db() as c:
            r = c.execute("SELECT ts, value FROM readings WHERE node=? AND slot=? AND ts<? "
                          "ORDER BY ts DESC LIMIT 1", (node, slot, t)).fetchone()
            return (r[0], r[1]) if r else None

    def import_csv(self, path):
        """One-time import of the old logger's readings.csv."""
        import csv
        rows = []
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    ts = int(time.mktime(time.strptime(r["time"], "%Y-%m-%dT%H:%M:%S")))
                    rows.append((ts, int(r["node"]), int(r["slot"]), int(r["value"])))
                except (KeyError, ValueError):
                    pass
        self.add_readings(rows)
        return len(rows)


# ---------------------------------------------------------------------------
# Configuration (node names, calibration, password) -- a JSON file
# ---------------------------------------------------------------------------
class Config:
    DEFAULTS = {"password": None, "secret": None, "nodes": {},
                "poll_seconds": 60, "full_every_seconds": 900, "stale_seconds": 300,
                # Uploading to an outside service starts switched OFF and cannot
                # be switched on until a send has actually worked -- see Uploader.
                "g4rden": {"enabled": False, "base_url": "https://g4rden.com",
                           "token": "", "gateway_id": None, "seq": 0, "retry_seq": None,
                           "interval_seconds": 900, "min_gap_seconds": 300,
                           "max_per_device": 96, "devices": {}}}

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(self.DEFAULTS)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        if not self.data["secret"]:
            self.data["secret"] = secrets.token_hex(32)
            self.save()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)       # holds the password hash
            except OSError:
                pass

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
        self.save()

    # PBKDF2, salted. Never stored or logged in the clear.
    def set_password(self, pw):
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200000)
        self.data["password"] = base64.b64encode(salt + h).decode()
        self.save()

    def check_password(self, pw):
        if not self.data["password"]:
            return False
        raw = base64.b64decode(self.data["password"])
        salt, h = raw[:16], raw[16:]
        return hmac.compare_digest(h, hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200000))


# ---------------------------------------------------------------------------
# Gateway access
# ---------------------------------------------------------------------------
class Gateway:
    """Every exchange with the gateway, serialized. `transact` opens the port,
    sends one command, reads until `until` matches or `quiet` seconds pass
    with nothing new, and closes -- so an external tool can have the port
    whenever this is idle."""

    def __init__(self, port, yield_to):
        self.port = port
        self.yield_to = yield_to
        self.lock = threading.Lock()
        self.pushing = False

    def busy_elsewhere(self):
        """Is another tool using the port? Excludes this process: its own
        command line names hivewire_push.py (--push-script), so without that
        it matched itself and skipped every poll, forever."""
        if not self.yield_to:
            return False
        r = subprocess.run(["pgrep", "-f", self.yield_to], capture_output=True, text=True)
        me = {os.getpid(), os.getppid()}
        return any(int(p) not in me for p in r.stdout.split())

    def transact(self, cmd, until=None, timeout=4.0, quiet=None):
        import serial
        with self.lock:
            if self.pushing:
                raise RuntimeError("a firmware push is using the gateway")
            with serial.Serial(self.port, 115200, timeout=0.05) as s:
                s.reset_input_buffer()
                s.write((cmd + "\n").encode())
                s.flush()
                buf, t_end, t_last = b"", time.time() + timeout, time.time()
                while time.time() < t_end:
                    chunk = s.read(max(1, s.in_waiting))
                    if chunk:
                        buf += chunk
                        t_last = time.time()
                        if until and re.search(until, buf.decode("utf-8", "replace")):
                            # finish the matching line
                            if buf.endswith(b"\n"):
                                break
                    elif quiet and buf and time.time() - t_last > quiet:
                        break
            return buf.decode("utf-8", "replace")

    def dump(self):
        text = self.transact("dump", until=r"DUMP END \d+[^\n]*\n", timeout=3)
        return parse_dump(text)

    def command(self, cmd, ack):
        """A user command. The gateway relays its reply over every uplink,
        LoRa included, and echoes it here."""
        text = self.transact(cmd, until=ack, timeout=5)
        m = re.search(r"(ACK[^\r\n]*|ERR[^\r\n]*)", text)
        return m.group(1) if m else None

    def node_log(self, nid):
        text = self.transact("log %d" % nid, timeout=10, until=None)
        out = []
        for line in text.splitlines():
            m = re.search(r"N%d \| (.*)$" % nid, line)
            if m and m.group(1) not in out:
                out.append(m.group(1))
        return out

    def gateway_log(self):
        text = self.transact("log", timeout=4, quiet=1.5)
        out = []
        for line in text.splitlines():
            if "] L |" in line:
                out += [p.strip() for p in line.split("] L |", 1)[1].split(" | ") if p.strip()]
        # each line is echoed once per uplink; keep the first copy
        seen, uniq = set(), []
        for x in out:
            if x not in seen:
                seen.add(x); uniq.append(x)
        return uniq


def parse_dump(text):
    end = DUMP_END.search(text)
    if not end:
        return None
    nodes = {}
    for line in text.splitlines():
        m = DUMP_LINE.match(line.strip())
        if m:
            nodes[int(m.group(1))] = {
                "age": int(m.group(2)), "hops": int(m.group(3)),
                "slots": {int(a): int(b) for a, b in re.findall(r"(\d+)=(-?\d+)", m.group(4))}}
    if len(nodes) != int(end.group(1)):
        return None                      # a line was lost; next poll will do
    health = {k: int(v) for k, v in PAIRS.findall(end.group(2))}
    return {"nodes": nodes, "health": health}


class FakeGateway(Gateway):
    """A simulated swarm for trying the page without hardware."""

    def __init__(self):
        super().__init__(None, None)
        self.t0 = time.time()
        self.ep = 3
        self.mode = 0
        self.soil = 2100.0
        # Written settings, applied with a real radio's unreliability: half of
        # all writes never arrive, so the page's retry path gets exercised.
        self.written = {}               # (node, slot) -> value

    def transact(self, cmd, until=None, timeout=4.0, quiet=None):
        with self.lock:
            if self.pushing:
                raise RuntimeError("a firmware push is using the gateway")
            time.sleep(0.2)
            t = time.time() - self.t0
            if cmd == "dump":
                self.soil += random.uniform(-6, 5)
                temp = 2150 + 250 * __import__("math").sin(t / 900)
                lines = [
                    "DUMP 2 age=%d hops=0 1=%d 2=3 3=-41 4=-67 5=2 6=%d 7=4 8=0 9=2 10=0 11=%d "
                    "12=0 13=0 20=%d 21=%d 22=0 23=0 24=0 25=3502314079"
                    % (random.randint(0, 20), t // 60, 400 + t // 5, self.ep,
                       self.written.get((2, 20), 0), self.written.get((2, 21), 600)),
                    "DUMP 3 age=%d hops=0 1=%d 2=6 3=-38 4=-71 5=2 6=%d 7=3 8=0 9=2 10=0 11=%d "
                    "12=0 13=0 20=%d 21=%d 22=0 23=0 24=0 25=3502314079"
                    % (random.randint(0, 20), t // 60, 390 + t // 5, self.ep,
                       self.written.get((3, 20), 0), self.written.get((3, 21), 600)),
                    # 26/27: last error E303 (input floating) about slot 3, 3 since boot
                    "DUMP 11 age=%d hops=0 1=%d 2=%d 3=%d 4=45 5=%d 6=3 7=2 8=%d 9=-30 "
                    "22=0 23=0 25=1281381655 26=196911 27=3"
                    % (random.randint(0, 30), temp, 4500 + random.randint(-80, 80), self.soil,
                       4050 + random.randint(-10, 10), t // 60),
                    # Switched off hours ago: the gateway still lists its last
                    # values, with an age that keeps growing.
                    "DUMP 13 age=%d hops=0 1=2417 2=4025 3=2693 4=7 5=4072 6=7 7=2 8=720 9=-40 "
                    "22=0 23=0 25=1281381655" % (9800 + t),
                    # A WaterNode: soil sensor plus a pump (slots 40-48). A dose
                    # written to 40 shows up as ml pumped (45) and state done (46).
                    "DUMP 14 age=%d hops=0 1=%d 2=5100 3=%d 4=40 5=4090 6=7 7=3 8=%d 9=-60 "
                    "22=0 23=0 25=1281381655 40=%d 41=0 42=%d 43=%d 44=%d 45=%d 46=%d 47=1 48=%d"
                    % (random.randint(0, 20), temp, 1900 - self.written.get((14, "dosed"), 0) // 10,
                       t // 60, self.written.get((14, 40), 0), self.written.get((14, 42), 0),
                       self.written.get((14, 43), 250), self.written.get((14, 44), 1500),
                       self.written.get((14, "dosed"), 0), 2 if self.written.get((14, "dosed")) else 0,
                       self.written.get((14, 48), 0)),
                ]
                return "\n".join(lines) + "\nDUMP END %d ep=%d m=%d up=3 ok=3 flt=0\n" % (
                    len(lines), self.ep, self.mode)
            if cmd.startswith("mode"):
                self.mode = int(cmd.split()[1]); self.ep += 1
                return "[uplink-usb] ACK mode=%d ep=%d\n" % (self.mode, self.ep)
            if cmd.startswith("set"):
                _, tgt, slot, val = cmd.split()
                if tgt.isdigit() and random.random() < 0.5:
                    self.written[(int(tgt), int(slot))] = int(val)
                    if int(tgt) == 14 and int(slot) == 40 and self.written.get((14, 42)):
                        # the simulated pump only doses once calibrated, like the real one
                        self.written[(14, "dosed")] = self.written.get((14, "dosed"), 0) + int(val)
                return "[uplink-usb] ACK set %s=%s\n" % (slot, val)
            if cmd.startswith("log "):
                nid = int(cmd.split()[1])
                return "\n".join("[uplink-usb] N%d | %s" % (nid, x) for x in
                                 ("boot #2", "adopt ep=%d len=2" % self.ep, "fw: already running 4c605517"))
            if cmd == "log":
                return "[uplink-usb] L | boot ok | cmd dump\n"
            return ""


# ---------------------------------------------------------------------------
# Problems: Hivewire's error codes, and the host's own conditions
# ---------------------------------------------------------------------------
# Codes and their meaning come from errors.json (generated from the library's
# errors/codes.json) plus any an application declares in kinds.json under
# "errors". Nodes carry theirs in slots 26/27; the host raises its own here.
# A report of all of it -- stripped of anything that says whose swarm this is
# or where -- can be sent as a GitHub issue from the Problems page.
ERR_SLOT_LAST, ERR_SLOT_COUNT = 26, 27
REPORT_REPO = "sirsweater/hivewire"


class Problems:
    def __init__(self, store, kinds):
        self.store = store
        self.lock = threading.Lock()
        self.active = {}                # code -> {"detail", "since"}
        try:
            reg = json.load(open(os.path.join(HERE, "errors.json"), encoding="utf-8"))
        except (OSError, ValueError):
            reg = {"codes": {}, "app_range": [1000, 1999]}
        self.app_range = reg.get("app_range", [1000, 1999])
        self.codes = {int(k): dict(v) for k, v in reg.get("codes", {}).items()}
        for kind, spec in kinds.items():
            for k, v in (spec.get("errors") or {}).items():
                self.codes.setdefault(int(k), dict(v, area="application", by="node", kind=kind))

    def explain(self, code):
        c = self.codes.get(int(code))
        if c:
            return dict(c, code=int(code))
        lo, hi = self.app_range
        return {"code": int(code), "title": "application code, not described in kinds.json"
                if lo <= int(code) <= hi else "unknown code (newer firmware than this page?)",
                "severity": "warning", "fix": "", "cause": "", "name": "UNKNOWN"}

    def raise_(self, code, detail):
        """Start a condition. Logged once when it starts, not on every check."""
        with self.lock:
            if code in self.active:
                self.active[code]["detail"] = detail
                return
            self.active[code] = {"detail": detail, "since": now()}
        self.store.event("error", "E%d %s" % (code, detail))

    def clear(self, code):
        with self.lock:
            p = self.active.pop(code, None)
        if p:
            self.store.event("error", "E%d resolved after %s" % (code, fmt_age(now() - p["since"])))

    def once(self, code, detail):
        """A one-off event rather than a lasting condition (a failed flash)."""
        self.store.event("error", "E%d %s" % (code, detail))

    def active_list(self):
        with self.lock:
            items = sorted(self.active.items())
        return [dict(self.explain(c), detail=p["detail"], since=p["since"]) for c, p in items]

    def node_error(self, slots):
        v = slots.get(ERR_SLOT_LAST)
        if not v:
            return None
        code, subject = v & 0xFFFF, (v >> 16) & 0xFF
        if not code:
            return None
        e = self.explain(code)
        e.update(subject=subject, count=slots.get(ERR_SLOT_COUNT),
                 label="E%d%s" % (code, "/%d" % subject if subject else ""))
        return e


# What a report must never carry: where the swarm is, whose it is, or anything
# that would let someone reach it. Applied to every line of free text.
_SCRUB = [
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    # Lookarounds, not \b: a USB device name puts "_" right before the MAC,
    # and "_" counts as a word character, so \b never matched there.
    (re.compile(r"(?<![0-9a-fA-F:])[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}(?![0-9a-fA-F:])"), "<mac>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\b(?:gw|dv)_[A-Za-z0-9]+"), "<id>"),
    (re.compile(r"/home/[^/\s]+"), "~"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"), "~"),
    (re.compile(r"\b[A-Za-z0-9_\-]{24,}\b"), "<redacted>"),
]


def scrub(text):
    for rx, sub in _SCRUB:
        text = rx.sub(sub, str(text))
    return text


# ---------------------------------------------------------------------------
# Poller: keeps the latest snapshot and records history
# ---------------------------------------------------------------------------
class Poller(threading.Thread):
    daemon = True

    def __init__(self, gw, store, cfg):
        super().__init__()
        self.gw, self.store, self.cfg = gw, store, cfg
        self.snapshot = None            # last good dump, with "time"
        self.last_error = None
        self.last_logged = {}           # (node, slot) -> (value, ts)
        self.wake = threading.Event()
        self.seen_nodes = set()
        # Writes the gateway acknowledged but no node has confirmed yet. The
        # gateway's ACK only means it SENT the write: one broadcast, which a
        # node at the edge of range misses often enough to matter (a soak
        # measured 1 in 6 lost at -87 dBm). The node's own report is the only
        # proof it arrived, so keep re-sending until that report shows it.
        self.pending = {}               # (node, slot) -> {value, sent, tries, first}
        self.pending_lock = threading.Lock()
        self.problems = None            # set by App
        self.alerts = None              # set by App: HiveAlerts
        self.plausible = None           # set by App: (nid, slot, value) -> bool
        self.fails = 0                  # polls in a row that got no dump

    WRITE_RETRY_S = 45
    WRITE_TRIES = 5

    def track_write(self, nid, slot, value):
        with self.pending_lock:
            self.pending[(nid, slot)] = {"value": value, "sent": now(), "tries": 1, "first": now()}

    def pending_writes(self):
        with self.pending_lock:
            return {"%d.%d" % k: dict(v) for k, v in self.pending.items()}

    def check_writes(self, d, t):
        with self.pending_lock:
            items = list(self.pending.items())
        for (nid, slot), p in items:
            rec = d["nodes"].get(nid)
            heard_at = t - rec["age"] if rec else 0
            if rec and heard_at >= p["sent"] - 1 and rec["slots"].get(slot) == p["value"]:
                self.store.event("command", "set %d %d %d confirmed by the node (%d %s, %.0f s)" % (
                    nid, slot, p["value"], p["tries"], "try" if p["tries"] == 1 else "tries",
                    t - p["first"]))
                with self.pending_lock:
                    self.pending.pop((nid, slot), None)
            elif t - p["sent"] >= self.WRITE_RETRY_S:
                if p["tries"] >= self.WRITE_TRIES:
                    msg = "set %d %d %d not applied after %d tries" % (nid, slot, p["value"], p["tries"])
                    if self.problems:
                        self.problems.once(606, msg)
                    else:
                        self.store.event("error", "E606 " + msg)
                    with self.pending_lock:
                        self.pending.pop((nid, slot), None)
                    continue
                ack = self.gw.command("set %d %d %d" % (nid, slot, p["value"]), r"ACK set|ERR")
                with self.pending_lock:
                    if (nid, slot) in self.pending:
                        p = self.pending[(nid, slot)]
                        p["tries"] += 1
                        p["sent"] = now()
                if not ack or not ack.startswith("ACK"):
                    self.store.event("command", "set %d %d %d retry -> %s" % (
                        nid, slot, p["value"], ack or "no reply"))

    def run(self):
        reported = "(not yet polled)"
        while True:
            try:
                self.poll()
            except Exception as e:      # never let the poller die
                self.last_error = "%s: %s" % (type(e).__name__, e)
            self.note_problems()
            if self.alerts and self.snapshot:
                try:
                    self.alerts.check(self.snapshot)
                except Exception as e:  # an alert bug must never stop polling
                    print("alerts: %s: %s" % (type(e).__name__, e), flush=True)
            # Say so in the log when polling starts or stops working -- once
            # per change, not once per poll.
            if self.last_error != reported:
                print("%s poll: %s" % (time.strftime("%H:%M:%S"), self.last_error or "ok"),
                      flush=True)
                reported = self.last_error
            self.wake.wait(max(5, self.cfg.data.get("poll_seconds", 60)))
            self.wake.clear()

    def note_problems(self):
        """Poll outcome -> host codes. A single missed dump is normal (a line
        lost on the USB link); three in a row is a gateway that is not there."""
        if not self.problems:
            return
        err = self.last_error or ""
        if err.startswith("clock is"):
            self.problems.raise_(602, err)
            return
        self.problems.clear(602)
        if not err:
            self.fails = 0
            self.problems.clear(601)
        elif "another tool" not in err and "push" not in err:
            self.fails += 1
            if self.fails >= 3:
                self.problems.raise_(601, "%d polls in a row failed: %s" % (self.fails, err))

    def poll(self):
        if self.gw.pushing:
            return
        if self.gw.busy_elsewhere():
            self.last_error = "another tool is using the gateway"
            return
        d = self.gw.dump()
        if d is None:
            self.last_error = "incomplete dump"
            return
        t = now()
        # A Pi has no battery-backed clock: it boots at whatever time it last
        # shut down and jumps when NTP answers, which can be hours. Readings
        # written in that window are stamped hours early and quietly corrupt
        # every chart and daily summary. If the clock is behind data we already
        # hold, it is wrong -- show live values, store nothing.
        newest = self.store.max_ts()
        if newest and t < newest - 5:
            self.snapshot = dict(d, time=t)
            self.last_error = ("clock is %s behind the newest stored reading -- "
                               "waiting for time sync before storing"
                               % fmt_age(newest - t))
            return
        self.last_error = None
        d["time"] = t
        self.snapshot = d
        full = self.cfg.data.get("full_every_seconds", 900)
        stale = self.cfg.data.get("stale_seconds", 300)
        rows = []
        for nid, rec in d["nodes"].items():
            if nid not in self.seen_nodes:
                self.seen_nodes.add(nid)
                if not self.cfg.node(nid):
                    self.store.event("node", "node %d first seen" % nid)
                    self.cfg.set_node(nid, {"first_seen": t})
            # The gateway keeps a node's last values after it goes quiet, and
            # lists them in every dump with a growing age. Storing them would
            # record a switched-off node as reporting the same values every
            # 15 minutes -- found by a soak, where a node that had been off for
            # three hours still showed "last heard 3 min ago" and its frozen
            # readings were uploaded as current. Nothing from a quiet node is
            # new, so store nothing; the page still shows it, marked stale.
            if rec["age"] > stale:
                continue
            for sid, v in list(rec["slots"].items()) + [(HOPS_SLOT, rec["hops"])]:
                if self.plausible and not self.plausible(nid, sid, v):
                    continue            # a failed sensor read, not a measurement
                prev = self.last_logged.get((nid, sid))
                if prev is None or prev[0] != v or t - prev[1] >= full:
                    rows.append((t, nid, sid, v))
                    self.last_logged[(nid, sid)] = (v, t)
        if rows:
            self.store.add_readings(rows)
        self.check_writes(d, t)


# ---------------------------------------------------------------------------
# Hive problems -> instant phone alerts (through g4rden)
# ---------------------------------------------------------------------------
class HiveAlerts:
    """Turns hive problems worth acting on into phone notifications.

    g4rden already delivers this head's soil alerts to whoever watches it;
    POST /api/device/event (same write token as the uploads) sends anything
    else, instantly unless the user turned that off. This end decides what is
    worth interrupting someone for and de-duplicates; g4rden only delivers.

      node error   a node's error COUNT went up (or restarted at >0 after a
                   reboot) with a code in ALERT_CODES or the node's own
                   application range (WaterNode E1001: dose never arrived)
      node quiet   not heard from for QUIET_S; once per outage
      gateway      E601 (gateway not answering) for longer than GW_SILENT_S

    Deliberately NOT alerted: weak link (E104), unexpected restarts (E208),
    host undervoltage (E605) -- logged on the Problems page, not worth a
    phone buzz. The first poll after a start is a baseline: nothing that was
    already wrong re-alerts because the admin restarted. Hidden (retired)
    nodes never alert. Same key (node+code, node quiet, gateway) at most once
    per COOLDOWN_S.
    """
    ALERT_CODES = {101, 102, 206, 301, 303, 306, 307}
    QUIET_S = 30 * 60
    GW_SILENT_S = 10 * 60
    COOLDOWN_S = 6 * 3600

    def __init__(self, app):
        self.app = app
        self.counts = {}                # nid -> error count last seen
        self.quiet = set()              # nids currently counted as quiet
        self.gw_alerted = False
        self.baselined = False
        self.sent = {}                  # key -> ts of last successful send
        self.pending = []               # events that failed to send, retried
        self.lock = threading.Lock()
        self.sending = False

    def worth(self, code):
        lo, hi = self.app.problems.app_range
        return code in self.ALERT_CODES or lo <= code <= hi

    def label(self, nid):
        meta = self.app.cfg.node(nid) or {}
        name = (meta.get("name") or "").strip()
        return "%s (node %d)" % (name, nid) if name else "Node %d" % nid

    def check(self, snap, t=None):
        """Called after every poll with the latest dump."""
        t = t or now()
        events = []
        stale = self.app.cfg.data.get("stale_seconds", 300)
        for nid, rec in ((snap or {}).get("nodes") or {}).items():
            meta = self.app.cfg.node(nid) or {}
            if meta.get("hidden"):
                continue
            age = rec.get("age") or 0
            if age > self.QUIET_S:
                if nid not in self.quiet:
                    self.quiet.add(nid)
                    if self.baselined:
                        events.append({"key": "quiet:%d" % nid, "kind": "node_quiet", "node": nid,
                                       "title": "🐝 %s went quiet" % self.label(nid),
                                       "body": "Not heard from for %s. Check its power, or whether it "
                                               "moved out of range of the hive." % fmt_age(age)})
            elif age <= stale:
                self.quiet.discard(nid)
            slots = rec.get("slots") or {}
            v, count = slots.get(ERR_SLOT_LAST), slots.get(ERR_SLOT_COUNT)
            if age > stale or count is None:
                continue
            prev = self.counts.get(nid)
            self.counts[nid] = count
            if prev is None or not v:
                continue
            new = count > prev or (count < prev and count > 0)   # < prev: rebooted, count restarted
            code, subject = v & 0xFFFF, (v >> 16) & 0xFF
            if new and self.worth(code):
                e = self.app.problems.explain(code)
                events.append({"key": "err:%d:%d" % (nid, code), "kind": "node_error", "node": nid,
                               "code": code,
                               "title": "🐝 %s: %s" % (self.label(nid), e.get("title") or "E%d" % code),
                               "body": ("E%d%s. %s" % (code, "/%d" % subject if subject else "",
                                                       e.get("fix") or e.get("cause") or "")).strip()})
        p = self.app.problems.active.get(601)
        if p and t - p["since"] > self.GW_SILENT_S:
            if not self.gw_alerted:
                self.gw_alerted = True
                events.append({"key": "gw:601", "kind": "gateway", "code": 601,
                               "title": "🐝 Hive gateway not answering",
                               "body": "The hive admin cannot reach the gateway on USB (%s). Check its "
                                       "cable and power; readings are not being collected." % p["detail"]})
        elif not p:
            self.gw_alerted = False
        self.baselined = True
        self.queue(events, t)

    def queue(self, events, t):
        with self.lock:
            for e in events:
                last = self.sent.get(e["key"])
                if last and t - last < self.COOLDOWN_S:
                    continue
                if not any(x["key"] == e["key"] for x in self.pending):
                    self.pending.append(dict(e, queued=t))
            # A failed send is retried for an hour, then dropped (the
            # Problems page still has it).
            self.pending = [e for e in self.pending if t - e["queued"] < 3600][-20:]
            if not self.pending or self.sending:
                return
            self.sending = True
            batch = self.pending[:5]
        threading.Thread(target=self.send, args=(batch,), daemon=True).start()

    def send(self, batch):
        try:
            g = self.app.cfg.data.get("g4rden") or {}
            if not (g.get("token") and g.get("enabled")):
                return                  # not linked to g4rden: nothing to send through
            body = {"events": [{k: e[k] for k in ("kind", "node", "code", "title", "body") if k in e}
                               for e in batch]}
            r = self.app.uploader.post("/api/device/event", body, g["token"])
            with self.lock:
                if r.get("ok"):
                    keys = {e["key"] for e in batch}
                    self.pending = [e for e in self.pending if e["key"] not in keys]
                    for k in keys:
                        self.sent[k] = now()
            if r.get("ok"):
                for e in batch:
                    self.app.store.event("alert", "sent to phones: %s" % e["title"])
            else:
                self.app.store.event("alert", "alert not sent (%s), will retry: %s" % (
                    r.get("status") or r.get("error"), batch[0]["title"]))
        finally:
            with self.lock:
                self.sending = False


# ---------------------------------------------------------------------------
# Firmware pushes
# ---------------------------------------------------------------------------
class Pusher:
    def __init__(self, gw, store, fw_dir, push_script, fake):
        self.gw, self.store, self.fw_dir = gw, store, fw_dir
        self.push_script, self.fake = push_script, fake
        self.job = None
        os.makedirs(fw_dir, exist_ok=True)

    @staticmethod
    def is_app_image(data):
        """An ESP32 APP image, not just any .bin: image magic 0xE9 at 0, and
        the app descriptor's magic word 0xABCD5432 right after the first
        segment header (offset 32). A merged image starts with the
        bootloader and a bootloader has no app descriptor, so both fail."""
        return (len(data) > 65536 and data[0] == 0xE9 and
                data[32:36] == bytes.fromhex("3254cdab"))

    @classmethod
    def inspect(cls, path):
        data = open(path, "rb").read()
        fams = sorted({m.group(1).decode() for m in FAMILY.finditer(data)})
        return {"size": len(data), "crc": "%08x" % (zlib.crc32(data) & 0xFFFFFFFF),
                "families": fams, "esp_image": cls.is_app_image(data)}

    def images(self):
        out = []
        for f in sorted(os.listdir(self.fw_dir)):
            # *.merged.bin are the Flash page's USB images (bootloader +
            # partitions + app). Pushed over the air they would be written
            # into an app slot and never boot, so they are not offered here.
            if f.endswith(".bin") and not f.endswith(".merged.bin"):
                p = os.path.join(self.fw_dir, f)
                info = self.inspect(p)
                info.update(name=f, mtime=int(os.path.getmtime(p)))
                out.append(info)
        return out

    def start(self, name):
        if self.job and self.job["state"] == "running":
            raise RuntimeError("a push is already running")
        path = os.path.join(self.fw_dir, os.path.basename(name))
        if not os.path.exists(path):
            raise RuntimeError("no such image")
        if path.endswith(".merged.bin"):
            raise RuntimeError("that is a USB flash image, not an app image; it cannot be pushed")
        info = self.inspect(path)
        if not info["esp_image"]:
            raise RuntimeError("not an ESP32 app image")
        self.job = {"state": "running", "file": name, "started": now(), "lines": [],
                    "info": info, "fed": 0}
        threading.Thread(target=self._run, args=(path,), daemon=True).start()

    def _run(self, path):
        job = self.job
        # Take the port for the whole push: wait out any exchange in progress,
        # then keep the poller and every user command off until it is done.
        with self.gw.lock:
            self.gw.pushing = True
        ok = False
        try:
            if self.fake:
                size = job["info"]["size"]
                for i in range(0, 101, 4):
                    job["fed"] = size * i // 100
                    job["lines"].append("fake: fed %d/%d" % (job["fed"], size))
                    time.sleep(0.3)
                job["lines"].append("PUSH END usb_retries=0")
                ok = True
            else:
                env = dict(os.environ, HW_IDLE_TIMEOUT="150", HW_HARD_TIMEOUT="900")
                p = subprocess.Popen([sys.executable, "-u", self.push_script, path, self.gw.port],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", env=env)
                for line in p.stdout:
                    line = line.rstrip()
                    m = re.search(r"MORE (\d+) (\d+)", line)
                    if m:
                        job["fed"] = int(m.group(1)) + int(m.group(2))
                        continue            # thousands of these; the bar is enough
                    if "progress:" in line:
                        continue
                    job["lines"].append(line)
                    job["lines"] = job["lines"][-400:]
                p.wait()
                # The host's own summary says whether every byte went out; the
                # gateway's PUSH END says the transfer finished on its side.
                ok = (any("PUSH END" in l for l in job["lines"]) and
                      any("(complete)" in l for l in job["lines"]))
        except Exception as e:
            job["lines"].append("error: %s" % e)
        finally:
            self.gw.pushing = False
        job["state"] = "done" if ok else "failed"
        job["ended"] = now()
        result = "complete" if ok else "FAILED"
        with self.store.db() as c:
            c.execute("INSERT INTO pushes(ts,file,size,crc,family,result,log) VALUES(?,?,?,?,?,?,?)",
                      (job["started"], job["file"], job["info"]["size"], job["info"]["crc"],
                       ",".join(job["info"]["families"]), result, "\n".join(job["lines"][-60:])))
        self.store.event("firmware", "push %s (%s) %s" % (job["file"], job["info"]["crc"], result))


# ---------------------------------------------------------------------------
# Uploading readings to the g4rden site
# ---------------------------------------------------------------------------
# Speaks the same API as g4rden's Zigbee head (tools/zigbee-head): claim a
# gateway once with a code from the site, then POST batches of readings.
#
#   POST /api/device/claim   {code, kind, fw}            -> {token, gatewayId}
#   POST /api/device/ingest  {seq, fw, nodes[]}          Bearer <token>
#     nodes: [{ieee, name?, model?, readings: [{age, soil, soilRaw, airTemp,
#                                               airHum, battery, volts, rssi}]}]
#
# Two of that design's decisions are carried over deliberately:
#
#   AGE, NOT TIMESTAMPS. A Pi that boots without a network has no correct clock
#   for a few seconds, and a wrong wall clock writes garbage into a time series.
#   The server stamps the time; we only say how long ago each sample was taken.
#
#   ONE seq PER BATCH, HELD ACROSS RETRIES. The common failure is an upload that
#   succeeded while its response was lost. Re-sending the same seq lets the
#   server recognise the retry instead of writing every reading twice.
#
# What is NOT carried over is the in-memory buffer: this admin already stores
# every reading in SQLite, so a batch is read back out of the database from
# where the last one finished. A Pi that was offline for six hours uploads those
# six hours when it returns, and restarting the admin loses nothing.
class Uploader(threading.Thread):
    """Posts stored readings to g4rden.

    Deliberately awkward to switch on: this is the one part of the program that
    sends anything off the property, so automatic uploads cannot be enabled
    until a send tried from the page has actually succeeded. Preview builds the
    exact body and touches no network.
    """
    daemon = True
    FW = "hivewire-admin/1.0.0"

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.wake = threading.Event()
        self.last = None                # result of the most recent attempt
        self.last_success = None

    def cfg(self):
        return self.app.cfg.data["g4rden"]

    # --- pairing -----------------------------------------------------------
    def claim(self, code):
        """Trade a claim code from the site for a long-lived write token. The
        code is single-use and expires; the token can only post readings."""
        g = self.cfg()
        r = self.post("/api/device/claim",
                      {"code": code.strip(), "kind": "hivewire", "fw": self.FW}, token=None)
        if r.get("ok") and isinstance(r.get("json"), dict) and r["json"].get("token"):
            g["token"] = r["json"]["token"]
            g["gateway_id"] = r["json"].get("gatewayId")
            self.app.cfg.save()
            self.app.store.event("upload", "claimed gateway %s" % g["gateway_id"])
            r["gateway_id"] = g["gateway_id"]
        else:
            self.app.store.event("upload", "claim failed: %s" % (r.get("error") or r.get("status")))
        return r

    # --- what gets sent ----------------------------------------------------
    # Slot -> the field names /api/device/ingest accepts, with the scaling each
    # needs. Only measurements the site stores; anything a node has not reported
    # is left out rather than sent as zero.
    FIELDS = [("airTemp", 1, 0.01, 2), ("airHum", 2, 0.01, 1), ("soilRaw", 3, 1, 0),
              ("volts", 5, 0.001, 3), ("battery", 10, 1, 0), ("rssi", 9, 1, 0),
              ("soilTemp", 16, 0.01, 2)]
    MEASURED = ("soil", "soilRaw", "soilTemp", "airTemp", "airHum", "battery", "volts")

    def samples_for(self, nid, since, now_ts, min_gap, max_n):
        """Replay stored readings into whole samples.

        Readings are stored one row per changed value, so a row at time T means
        "this value held from T until the next row". Walking them forward and
        taking a snapshot every min_gap seconds turns that back into the samples
        a sensor would have produced -- including the values that did not change
        and therefore have no row of their own."""
        state = self.app.store.state_at(nid, since)
        rows = self.app.store.rows_since(nid, since, now_ts)
        # A sensor coming online (its sensors-ok bit set) makes the value left
        # standing from before stale -- a weather node holds 0.00 C until its
        # air sensor answers -- so it is dropped until the sensor's own first
        # reading. Sensors-ok rows go first within a poll for that to work.
        gates = self.app.slot_gates(nid)
        ok_slots = {g[0] for g in gates.values()}
        rows.sort(key=lambda r: (r[0], r[1] not in ok_slots))
        out, last_emit = [], None
        for ts, slot, value in rows:
            if last_emit is not None and ts - last_emit >= min_gap and state:
                out.append((last_ts, dict(state)))
                last_emit = last_ts
            if slot in ok_slots:
                gained = value & ~state.get(slot, 0)
                for gs, (o, bit) in gates.items():
                    if o == slot and gained & bit:
                        state.pop(gs, None)
            state[slot] = value
            last_ts = ts
            if last_emit is None:
                last_emit = ts
        # Finish with the newest state, unless that moment was just emitted.
        if rows and state and (not out or out[-1][0] != rows[-1][0]):
            out.append((rows[-1][0], dict(state)))
        # Oldest first, capped: a long outage should not post a year in one go.
        return out[-max_n:]

    def reading_from(self, nid, ts, slots, now_ts):
        r = {"age": max(0, now_ts - ts)}
        for name, slot, scale, dec in self.FIELDS:
            if (slot in slots and self.app.plausible(nid, slot, slots[slot])
                    and self.app.believed(nid, slot, slots)):
                v = slots[slot] * scale
                r[name] = round(v, dec) if dec else int(v)
        if 3 in slots:
            # Only send a percentage when both ends of the scale were measured.
            # soilRaw always goes, so the site keeps the evidence and a later
            # calibration can be applied to it rather than to a guess.
            dry, wet, calibrated = self.app.calibration(nid)
            pct = soil_pct(slots[3], dry, wet) if calibrated else None
            if pct is not None:
                r["soil"] = pct
        return r if any(k in r for k in self.MEASURED) else None

    WX_MAX_HOURS = 48

    def weather_for(self, nid, now_ts):
        """A weather station's hours for /api/device/ingest (field map: kinds.json
        "weather_upload"): every finished hour not yet sent, then the hour in
        progress, which the site replaces on each upload. Returns (hours, next
        mark) -- the mark only moves once the site has the finished hours.

        The first time, it starts at the current hour: whatever the gauge
        counted before -- a bench test, carrying it outside -- is not rain."""
        fmap = self.app.kind_spec(nid).get("weather_upload")
        ru = self.app.rollup
        if not fmap or not ru:
            return [], None
        key = "wx_next:%d" % nid
        start = ru.get_mark(key)
        if start is None:
            start = hour_floor(now_ts)
            ru.set_mark(key, start)
        done = ru.hour_done()
        finished = ru.hours_by_ts(nid, start, done)
        hours, nxt = [], start
        for h in sorted(finished)[: self.WX_MAX_HOURS]:
            row = self.wx_row(finished[h], fmap, h, now_ts)
            if row:
                hours.append(row)
            nxt = h + 3600
        if done > nxt and len(finished) < self.WX_MAX_HOURS:
            nxt = done                       # empty hours (station off) are done too
        ph, part = ru.partial_hour(nid, now_ts)
        if ph >= done and ph >= start:
            row = self.wx_row(part, fmap, ph, now_ts)
            if row:
                row["partial"] = True
                hours.append(row)
        return hours, nxt

    @staticmethod
    def wx_row(slots, fmap, h, now_ts):
        stat = {"avg": 2, "min": 3, "max": 4, "sum": 5}
        row = {"age": max(0, now_ts - h)}
        secs = 0
        for name, (slot, which, scale) in fmap.items():
            r = slots.get(slot)
            if not r:
                continue
            secs = max(secs, r[0] or 0)
            v = r[stat[which]]
            if v is not None:
                row[name] = round(v * scale, 2)
        if len(row) == 1:
            return None
        row["secs"] = secs
        return row

    def build_nodes(self, now_ts=None):
        """The ingest body's `nodes`, plus the newest timestamp per node so a
        successful send knows exactly what to mark as sent."""
        g = self.cfg()
        now_ts = now_ts or now()
        min_gap = max(60, int(g.get("min_gap_seconds", 300)))
        max_n = max(1, int(g.get("max_per_device", 96)))
        nodes, marks = [], {}
        for key, ieee in sorted(g.get("devices", {}).items()):
            nid = int(key)
            since = self.app.store.upload_mark(nid)
            samples = self.samples_for(nid, since, now_ts, min_gap, max_n)
            readings = []
            for ts, slots in samples:
                r = self.reading_from(nid, ts, slots, now_ts)
                if r:
                    readings.append(r)
            weather, wx_next = self.weather_for(nid, now_ts)
            if not readings and not weather:
                continue
            meta = self.app.cfg.node(nid)
            node = {"ieee": ieee, "readings": readings}
            if weather:
                node["weather"] = weather
                marks[("wx", nid)] = wx_next
            if meta.get("name"):
                node["name"] = meta["name"][:60]
            node["model"] = (meta.get("auto_kind") or meta.get("kind") or "Hivewire")[:40]
            nodes.append(node)
            if samples:
                marks[nid] = samples[-1][0]
        return nodes, marks

    # --- sending -----------------------------------------------------------
    def post(self, path, body, token):
        g = self.cfg()
        url = (g.get("base_url") or "https://g4rden.com").rstrip("/") + path
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", self.FW)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        res = {"ts": now(), "url": url}
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read(4000).decode("utf-8", "replace")
                res.update(ok=200 <= r.status < 300, status=r.status, response=text)
        except urllib.error.HTTPError as e:
            text = e.read(4000).decode("utf-8", "replace")
            res.update(ok=False, status=e.code, response=text)
        except Exception as e:
            res.update(ok=False, error="%s: %s" % (type(e).__name__, e))
            return res
        try:
            res["json"] = json.loads(res.get("response") or "")
        except ValueError:
            pass
        return res

    def send_once(self):
        """One upload. Never raises: the caller is a web request or a loop."""
        g = self.cfg()
        # Ages are computed against this clock; if it is behind the data, every
        # age would be wrong (and some negative). Wait rather than send rubbish.
        newest = self.app.store.max_ts()
        if newest and now() < newest - 5:
            return {"ok": False, "ts": now(),
                    "error": "the Pi's clock is behind its own readings; waiting for time sync"}
        if not g.get("token"):
            return {"ok": False, "error": "not paired yet -- enter a claim code from the site",
                    "ts": now()}
        nodes, marks = self.build_nodes()
        if not nodes:
            return {"ok": False, "ts": now(),
                    "error": "nothing to send: map a node to a g4rden device, "
                             "or wait for a new reading since the last upload"}
        # A retry reuses the previous seq so the server can spot the duplicate.
        if not g.get("retry_seq"):
            g["seq"] = int(g.get("seq", 0)) + 1
            g["retry_seq"] = g["seq"]
            self.app.cfg.save()
        body = {"seq": g["retry_seq"], "fw": self.FW, "nodes": nodes}
        res = self.post("/api/device/ingest", body, g["token"])
        res["sent"] = sum(len(n["readings"]) for n in nodes)  # readings only
        res["weather_hours"] = sum(len(n.get("weather", [])) for n in nodes)
        res["nodes"] = len(nodes)
        if res.get("ok"):
            for nid, ts in marks.items():
                if isinstance(nid, tuple):
                    self.app.rollup.set_mark("wx_next:%d" % nid[1], ts)
                else:
                    self.app.store.set_upload_mark(nid, ts)
            g["retry_seq"] = None
            self.app.cfg.save()
            # The site can retune the interval without anyone reflashing anything.
            j = res.get("json") or {}
            iv = (j.get("config") or {}).get("intervalSec")
            if iv and 60 <= int(iv) <= 86400 and int(iv) != int(g.get("interval_seconds", 900)):
                g["interval_seconds"] = int(iv)
                self.app.cfg.save()
                res["interval_changed"] = int(iv)
            self.last_success = res
            try:
                self.app.note_plant_bands(((res.get("json") or {}).get("nodes")) or [])
            except Exception as e:          # a range is a nicety; never fail the upload over it
                print("plant ranges: %s" % e, flush=True)
        elif res.get("status") == 401:
            res["error"] = ("g4rden rejected the token (401). The gateway was probably "
                            "removed on the site; pair again with a fresh claim code.")
        self.last = res
        # What the site says it KEPT, not just that it answered: a soak compares
        # these, and a clock that disagrees with the site's skews every age sent.
        extra = ""
        j = res.get("json") or {}
        if res.get("ok"):
            extra = ", accepted %s" % j.get("accepted", "?")
            if j.get("duplicate"):
                extra += " (duplicate of the last upload)"
            st = (j.get("config") or {}).get("serverTime")
            if isinstance(st, (int, float)) and st > 0:
                res["clock_skew_s"] = round(res["ts"] - (st / 1000.0 if st > 1e11 else st), 1)
                extra += ", clock %+.0f s vs site" % res["clock_skew_s"]
        else:
            extra = " -- %s" % (res.get("error") or res.get("status"))
        wx = ", %d weather hour(s)" % res["weather_hours"] if res["weather_hours"] else ""
        self.app.store.event("upload", "%s %d reading(s)%s across %d node(s)%s" % (
            "sent" if res.get("ok") else "FAILED sending", res["sent"], wx, res["nodes"], extra))
        if res.get("ok"):
            self.app.problems.clear(603)
        else:
            self.app.problems.raise_(603, "g4rden upload failed: %s" % (res.get("error") or res.get("status")))
        return res

    def run(self):
        while True:
            g = self.cfg()
            try:
                if g.get("enabled") and g.get("token"):
                    self.send_once()
            except Exception as e:
                print("upload loop: %s: %s" % (type(e).__name__, e), flush=True)
            self.wake.wait(max(60, int(g.get("interval_seconds", 900))))
            self.wake.clear()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class App:
    def __init__(self, args):
        self.args = args
        os.makedirs(args.data, exist_ok=True)
        self.cfg = Config(os.path.join(args.data, "config.json"))
        self.store = Store(os.path.join(args.data, "hive.db"))
        csv_path = os.path.join(args.data, "readings.csv")
        if os.path.exists(csv_path) and not self.cfg.data.get("csv_imported"):
            n = self.store.import_csv(csv_path)
            self.cfg.data["csv_imported"] = True
            self.cfg.save()
            self.store.event("system", "imported %d readings from readings.csv" % n)
        self.flash_only = bool(getattr(args, "flash_only", False))
        if self.flash_only:
            # A PC with a board on its USB: no swarm, just the Flash page.
            self.gw = self.poller = self.uploader = self.pusher = None
        else:
            self.gw = FakeGateway() if args.fake else Gateway(args.port, args.yield_to)
            self.poller = Poller(self.gw, self.store, self.cfg)
            self.uploader = Uploader(self)
            push_script = args.push_script or os.path.join(HERE, "..", "hivewire_push.py")
            self.pusher = Pusher(self.gw, self.store, os.path.join(args.data, "firmware"),
                                 push_script, args.fake)
        from flasher import Flasher
        os.makedirs(os.path.join(args.data, "firmware"), exist_ok=True)
        self.flasher = Flasher(os.path.join(args.data, "firmware"),
                               gateway_port=None if args.fake else args.port, store=self.store,
                               known_ids=lambda: list(self.cfg.data.get("nodes", {}).keys()),
                               protect_present=not (self.flash_only or args.fake))
        self.kinds = json.load(open(os.path.join(HERE, "kinds.json"), encoding="utf-8"))
        self.problems = Problems(self.store, self.kinds)
        self.power_bits = None
        self.flasher.problems = self.problems
        if self.poller:
            self.poller.problems = self.problems
            self.poller.plausible = self.plausible
            self.alerts = HiveAlerts(self)
            self.poller.alerts = self.alerts
        # Hourly and daily summaries, and the 30-day raw cleanup (rollup.py).
        self.rollup = None if self.flash_only else Rollup(
            self.store, self.slot_modes, self.plausible,
            log=lambda msg: self.store.event("system", msg), slot_gates=self.slot_gates)
        # Automatic watering that follows the linked plant and learns the amount
        # (waterlearn.py). On by default for every water node; see adaptive().
        self.learner = None if self.flash_only else WaterLearner(
            self.store, self.calibration, self.adaptive_node,
            log=lambda msg: self.store.event("command", msg))
        # Public model forecasts saved next to what the station measures, and
        # scored against it (forecasts.py). Off until the config has a place.
        self.forecasts = None if self.flash_only else Forecasts(
            self.store, lambda: self.cfg.data.get("forecast"),
            log=lambda msg: self.store.event("system", msg))
        if shutil.which("vcgencmd") and not args.fake:
            threading.Thread(target=self.watch_power, daemon=True).start()
        # Signed-in sessions survive a restart. They used to live only in
        # memory, so every deploy and every power cut silently signed the
        # owner out -- which reads as "the password stopped working", not as
        # "the service restarted". Only the HASH of each cookie is stored, so
        # the file cannot be used to sign in even if it is read.
        self.sessions = {}              # sha256(token) -> expiry
        self.load_sessions()
        # Until a password exists, anyone on the network who loads the page
        # first could choose it. Setting it needs this code, which only
        # someone with access to this machine can read.
        self.setup_code_path = os.path.join(args.data, "setup_code.txt")
        self.setup_fails = 0
        self.setup_code = None
        if not self.cfg.data["password"] and not self.flash_only:
            self.new_setup_code()
        self.login_fails = {}           # ip -> (count, first)
        self.store.event("system", "admin started (%s)" % (
            "flash only" if self.flash_only else "simulated gateway" if args.fake else args.port))

    def new_setup_code(self):
        code = "%06d" % secrets.randbelow(10 ** 6)
        with open(self.setup_code_path, "w") as f:
            f.write(code + "\n")
        try:
            os.chmod(self.setup_code_path, 0o600)
        except OSError:
            pass
        self.setup_code = code
        self.setup_fails = 0
        print("first run: setup code %s (also in %s)" % (code, self.setup_code_path), flush=True)

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def session_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def load_sessions(self):
        now_t = time.time()
        stored = self.cfg.data.get("sessions") or {}
        self.sessions = {h: exp for h, exp in stored.items()
                         if isinstance(exp, (int, float)) and exp > now_t}
        if len(self.sessions) != len(stored):
            self.save_sessions()        # drop the expired ones as we pass

    def save_sessions(self):
        self.cfg.data["sessions"] = dict(self.sessions)
        self.cfg.save()

    def confirmable(self, nid, slot):
        """Whether a write to this slot can be re-sent until the node confirms it.
        Only a setting the node reports back can be confirmed at all; and an
        ACTION (reboot, arm OTA, seed firmware) must never be repeated behind
        the operator's back -- a reboot re-sent five times is five reboots.
        Actions are marked `"action": true` in kinds.json."""
        snap = self.poller.snapshot or {}
        rec = (snap.get("nodes") or {}).get(nid)
        if not rec or slot not in rec["slots"]:
            return False
        spec = self.kinds.get(self.kind_of(nid, rec["slots"]), {})
        return not (spec.get("slots", {}).get(str(slot), {}).get("action"))

    def station_node(self):
        """The weather station forecasts are scored against: the config's
        forecast.station_node, else the first WeatherNode heard."""
        n = (self.cfg.data.get("forecast") or {}).get("station_node")
        if n is not None:
            return int(n)
        nodes = ((self.poller.snapshot or {}).get("nodes") or {}) if self.poller else {}
        return next((nid for nid in sorted(nodes) if self.node_kind(nid) == "WeatherNode"), None)

    def node_kind(self, nid):
        """The node's kind without needing the page open. kind_of() learns a
        kind from live slots, but only ran when someone loaded /api/state, so
        a node nobody had looked at yet -- a new weather station -- had no kind,
        and with it no summaries, no "valid" ranges and no weather upload."""
        meta = self.cfg.node(nid)
        for k in (meta.get("kind"), meta.get("auto_kind")):
            if k in self.kinds:
                return k
        cache = self.__dict__.setdefault("_kind_tries", {})
        t, k = cache.get(nid, (0, None))
        if time.time() - t < 300:
            return k
        rec = ((self.poller.snapshot or {}).get("nodes") or {}).get(nid) if self.poller else None
        slots = rec["slots"] if rec else self.store.latest(nid)
        k = self.kind_of(nid, slots) if slots else None
        cache[nid] = (time.time(), k)
        return k

    # Defaults for a water node nobody has configured: follow the linked
    # plant's range and learn the amount. "user_off" is set when the owner
    # saves automatic watering switched OFF, and is never overridden.
    ADAPTIVE_DEFAULT = {"on": True, "from_plant": True}

    def adaptive_node(self, nid):
        """The node's config with its adaptive-watering settings filled in."""
        n = dict(self.cfg.node(nid))
        a = dict(self.ADAPTIVE_DEFAULT)
        a.update(n.get("adaptive") or {})
        n["adaptive"] = a
        return n

    def note_plant_bands(self, nodes):
        """g4rden's ingest reply says which plant range each node is linked to
        (functions/lib/devices.js plantBand). Keep it in the node's config, where
        waterlearn.py reads it; only a real change is written."""
        devices = self.cfg.data.get("g4rden", {}).get("devices", {})
        by_ieee = {str(v).lower(): int(k) for k, v in devices.items() if str(k).isdigit()}
        for n in nodes or []:
            nid = by_ieee.get(str(n.get("ieee", "")).lower())
            if nid is None or "plantLinked" not in n:
                continue
            lo, hi = n.get("moistMin"), n.get("moistMax")
            band = [lo, hi] if n.get("plantLinked") and lo is not None and hi is not None else None
            if self.cfg.node(nid).get("plant_band") != band:
                self.cfg.set_node(nid, {"plant_band": band})
                self.store.event("config", "node %d plant range from g4rden: %s" % (
                    nid, "%s-%s%%" % (lo, hi) if band else "none (no plant linked)"))

    def auto_defaults(self, nid, slots):
        """Switch automatic watering on for a water node that is ready for it
        and nobody switched off: soil probe calibrated, pump flow measured.
        Water below the plant's low end (or 40%), 100 ml, 6 h apart, at most
        600 ml a day. Returns the writes made, for the log."""
        conf = self.adaptive_node(nid)["adaptive"]
        if conf.get("user_off") or slots.get(49) or not slots.get(42):
            return []
        dry, wet, calibrated = self.calibration(nid)
        if not calibrated:
            return []
        band = self.cfg.node(nid).get("plant_band") if conf.get("from_plant") else None
        below_pct = band[0] if band else 40
        below_raw = max(1, min(4095, int(round(dry - below_pct / 100.0 * (dry - wet)))))
        writes = [(51, slots.get(51) or 100), (52, slots.get(52) if slots.get(52) and slots.get(52) != 720 else 360),
                  (50, below_raw), (44, min(slots.get(44) or 600, 1500)), (49, 1)]
        done = []
        for slot, value in writes:
            ack = self.gw.command("set %d %d %d" % (nid, slot, value), r"ACK set|ERR")
            if not (ack and ack.startswith("ACK")):
                return done
            self.poller.track_write(nid, slot, value)
            done.append((slot, value))
        self.store.event("command", "node %d automatic watering switched on by default: below %s%% (raw %d)%s" % (
            nid, below_pct, below_raw, " from its plant" if band else ""))
        return done

    def slot_modes(self, nid):
        """{slot: "mean" | "counter" | "angle"} worth summarising for a node,
        from its kind in kinds.json (rollup.slot_modes_from_kind)."""
        return slot_modes_from_kind(self.kind_spec(nid))

    def kind_spec(self, nid):
        k = self.node_kind(nid)
        return self.kinds.get(k, {}) if k else {}

    def slot_gates(self, nid):
        return slot_gates_from_kind(self.kind_spec(nid))

    def believed(self, nid, slot, slots):
        """False when the node's own sensors-ok bit says this slot's sensor is
        not reading -- a weather node with no air sensor sends 0.00 C."""
        g = self.slot_gates(nid).get(slot)
        return g is None or g[0] not in slots or bool(slots[g[0]] & g[1])

    def plausible(self, nid, slot, value):
        """Whether a stored value is one the sensor could really have measured.
        kinds.json gives a slot `"valid": [min, max]` in raw units. A failed
        air-sensor read used to arrive as exactly -50.0 C and 0 %RH and was
        stored, charted and uploaded as real; the firmware now refuses such a
        read, and this keeps any already stored -- or sent by a node not yet
        updated -- out of charts, reports and uploads without deleting them."""
        rng = (self.kind_spec(nid).get("slots", {}).get(str(slot), {}) or {}).get("valid")
        if not rng or value is None:
            return True
        return rng[0] <= value <= rng[1]

    def watch_power(self):
        """A Raspberry Pi says when its supply sags. Undervoltage with a
        gateway and boards on its USB is how SD cards get corrupted, and it
        looks like flaky radios long before it looks like power."""
        while True:
            try:
                out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                     text=True, timeout=10).stdout
                m = re.search(r"0x([0-9a-fA-F]+)", out)
                if m:
                    bits = int(m.group(1), 16)
                    self.power_bits = bits
                    if bits & 0x1:
                        self.problems.raise_(605, "undervoltage right now (throttled=0x%x)" % bits)
                    else:
                        self.problems.clear(605)
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(60)

    def problem_report(self):
        """Everything useful for diagnosing this swarm, nothing that says whose
        or where it is: no names, locations, addresses, ids or tokens."""
        st = self.state()
        L = ["**What went wrong?** (what you did, what you expected, what happened)", "", "", "",
             "---", "*Generated by the Hivewire admin page. Names, locations, network",
             "addresses and tokens are removed; check it before sending.*", ""]
        try:
            model = open("/proc/device-tree/model", errors="replace").read().strip("\x00\n ")
        except OSError:
            model = sys.platform
        L += ["### Host", "",
              "- admin %s, Python %s, %s" % (Uploader.FW, sys.version.split()[0], model),
              "- gateway: %s" % ("simulated" if self.args.fake else ("ok" if st["snapshot_time"] else "no data")),
              "- poll: %s" % scrub(st.get("error") or "ok")]
        if self.power_bits is not None:
            L.append("- power flags: 0x%x" % self.power_bits)
        h = st.get("health") or {}
        if h:
            L.append("- swarm: %s" % " ".join("%s=%s" % kv for kv in sorted(h.items())))
        act = self.problems.active_list()
        L += ["", "### Active problems", ""]
        L += ["- **E%d** %s -- %s" % (a["code"], a["title"], scrub(a["detail"])) for a in act] or ["- none"]
        L += ["", "### Nodes", "",
              "| id | kind | heard | hops | firmware | last error | errors since boot | warnings |",
              "|---|---|---|---|---|---|---|---|"]
        for n in st["nodes"]:
            e = n.get("error")
            fw = n["slots"].get(25)
            L.append("| %d | %s | %s ago | %s | %s | %s | %s | %s |" % (
                n["id"], n.get("kind") or "?", fmt_age(n["age"]), n["hops"],
                "%08x" % (fw & 0xFFFFFFFF) if fw is not None else "-",
                "%s %s" % (e["label"], e["title"]) if e else "-",
                n["slots"].get(ERR_SLOT_COUNT, "-"),
                scrub("; ".join(w for w in n["warnings"] if not (e and w.startswith(e["label"]))) or "-")))
        evs = [e for e in self.store.events(400) if e["kind"] in ("error", "flash", "upload", "system")][:25]
        L += ["", "### Recent events", "", "```"]
        L += ["%s %s %s" % (time.strftime("%m-%d %H:%M", time.localtime(e["ts"])), e["kind"], scrub(e["text"]))
              for e in reversed(evs)] or ["(none)"]
        L += ["```"]
        worst = act[0] if act else next((n["error"] for n in st["nodes"] if n.get("error")), None)
        title = ("E%d: %s" % (worst["code"], worst["title"])) if worst else "Problem report"
        return {"title": title, "body": "\n".join(L),
                "repo": self.cfg.data.get("report_repo") or REPORT_REPO}

    def kind_of(self, nid, slots):
        """A kind set in Settings wins; else one recognised from the slots;
        else the one recognised last time. A gateway that just rebooted knows
        only the slots reported since, so recognition can fail for up to a
        report interval -- remembering it keeps the page from calling a known
        node "unknown" after every gateway restart."""
        meta = self.cfg.node(nid)
        if meta.get("kind") in self.kinds:
            return meta["kind"]
        ids = set(slots)
        for name, spec in self.kinds.items():
            m = spec.get("match", {})
            if set(m.get("has", [])) <= ids and not (set(m.get("lacks", [])) & ids):
                if meta.get("auto_kind") != name:
                    self.cfg.set_node(nid, {"auto_kind": name})
                return name
        return meta.get("auto_kind") if meta.get("auto_kind") in self.kinds else None

    def calibration(self, nid):
        """Both ends of the soil scale, and whether a person actually measured
        them. A default wet point is a guess, and a percentage computed against
        a guess is a confident lie -- node 12 read 95% against the shipped
        default and about 72% against a real wet point. So an unmeasured end
        means no percentage at all: the raw value still charts, and the page
        says what is missing."""
        n = self.cfg.node(nid)
        dry, wet = n.get("soil_dry"), n.get("soil_wet")
        calibrated = dry is not None and wet is not None and dry != wet
        return (dry if dry is not None else 2800,
                wet if wet is not None else 1200,
                calibrated)

    def batt_scale(self, nid):
        """Correction for this board's divider and ADC. The resistors are 1%
        or 5% parts and the C6's ADC is good to a few percent, so a node can
        read a full cell as 4.13 V. Measured once and applied here, the whole
        stored history is corrected rather than just readings from now on."""
        try:
            return max(0.8, min(1.25, float(self.cfg.node(nid).get("batt_scale", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    def derived(self, nid, kind, slots):
        """Values computed here rather than in firmware -- e.g. soil moisture
        from the raw reading and this node's calibration, so recalibrating
        never needs a reflash and applies to the whole history."""
        out = {}
        spec = self.kinds.get(kind, {})
        for key, d in spec.get("derived", {}).items():
            if d["type"] == "soil_pct" and d["from"] in slots:
                dry, wet, calibrated = self.calibration(nid)
                out[key] = soil_pct(slots[d["from"]], dry, wet) if calibrated else None
            elif d["type"] == "lipo_pct" and slots.get(d["from"]):
                out[key] = lipo_pct(slots[d["from"]] * self.batt_scale(nid))
            elif d["type"] == "volts" and slots.get(d["from"]):
                out[key] = round(slots[d["from"]] * self.batt_scale(nid) / 1000.0, 2)
        return out

    def state(self):
        snap = self.poller.snapshot
        stale = self.cfg.data.get("stale_seconds", 300)
        nodes = []
        if snap:
            age_of_snap = now() - snap["time"]
            for nid, rec in sorted(snap["nodes"].items()):
                # A gateway that just rebooted knows only what was reported
                # since; show the last stored value for the rest, rather than
                # a node that seems to have lost half its readings.
                merged = self.store.latest(nid)
                merged.update(rec["slots"])
                rec = dict(rec, slots=merged)
                kind = self.kind_of(nid, rec["slots"])
                meta = self.cfg.node(nid)
                age = rec["age"] + age_of_snap
                warnings = []
                if age > stale:
                    warnings.append("not heard for %s" % fmt_age(age))
                spec = self.kinds.get(kind, {})
                okslot = spec.get("ok_slot")
                if okslot and okslot["slot"] in rec["slots"]:
                    v = rec["slots"][okslot["slot"]]
                    for bit, label in okslot["bits"].items():
                        # {"label", "unless": bit}: not a fault when that other
                        # bit says the sensor was deliberately left out.
                        if isinstance(label, dict):
                            if v & int(label.get("unless", 0)):
                                continue
                            label = label["label"]
                        if not v & int(bit):
                            warnings.append(label)
                # One rule or several: {slot, value, label, ignore, unit}.
                # Say why a soil percentage is missing, and notice a reading
                # that has wandered outside the range its owner measured --
                # either the probe moved, or the calibration was taken
                # somewhere the probe no longer is.
                if spec.get("derived", {}).get("soil") and 3 in rec["slots"]:
                    dry, wet, calibrated = self.calibration(nid)
                    raw3 = rec["slots"][3]
                    if not calibrated:
                        warnings.append("soil not calibrated - raw reading only")
                    elif raw3 < wet:
                        # Below the value measured in water. Soil cannot be
                        # wetter than water, so this is the probe, not the pot:
                        # these boards wick if inserted past their line and
                        # then read pinned near the bottom for good.
                        warnings.append("soil raw %d reads wetter than water (%d) - check the probe"
                                        % (raw3, wet))
                    elif raw3 > dry:
                        warnings.append("soil raw %d is drier than its dry point (%d)" % (raw3, dry))
                rules = spec.get("warn_below") or []
                if isinstance(rules, dict):
                    rules = [rules]
                dvals = self.derived(nid, kind, rec["slots"])
                for low in rules:
                    ref = str(low["slot"])
                    src, key = (dvals, ref[2:]) if ref.startswith("d:") else (rec["slots"], low["slot"])
                    if key not in src:
                        continue
                    v = src[key]
                    # `ignore` is the value that means "not measured" rather
                    # than a real low reading -- a board with no battery
                    # divider reports 0, and must not look like a flat one.
                    if v <= low["value"] and v != low.get("ignore"):
                        warnings.append("%s (%s%s)" % (low["label"], v, low.get("unit", "")))
                err = self.problems.node_error(rec["slots"])
                if err:
                    warnings.insert(0, "%s %s%s" % (err["label"], err["title"],
                                    " (%s since boot)" % err["count"] if err.get("count") else ""))
                nodes.append({"id": nid, "kind": kind, "name": meta.get("name") or "",
                              "error": err,
                              "location": meta.get("location") or "", "age": age,
                              "hops": rec["hops"], "slots": rec["slots"],
                              "derived": self.derived(nid, kind, rec["slots"]),
                              "warnings": warnings, "hidden": bool(meta.get("hidden"))})
        return {"time": now(), "snapshot_time": snap["time"] if snap else None,
                "health": snap["health"] if snap else None, "nodes": nodes,
                "error": self.poller.last_error, "pushing": self.gw.pushing,
                "problems": self.problems.active_list(),
                "fake": bool(self.args.fake)}


# The same LiPo discharge curve the node uses, so the calibrated percentage and
# the node's own agree on everything except the voltage correction.
LIPO_CURVE = [(3300, 0), (3450, 5), (3680, 10), (3740, 20), (3770, 30), (3790, 40),
              (3820, 50), (3870, 60), (3950, 70), (4000, 80), (4100, 90), (4200, 100)]


def lipo_pct(mv):
    if mv <= LIPO_CURVE[0][0]:
        return 0.0
    if mv >= LIPO_CURVE[-1][0]:
        return 100.0
    for i in range(1, len(LIPO_CURVE)):
        v1, p1 = LIPO_CURVE[i]
        if mv < v1:
            v0, p0 = LIPO_CURVE[i - 1]
            return round(p0 + (mv - v0) * (p1 - p0) / float(v1 - v0), 1)
    return 100.0


def soil_pct(raw, dry, wet):
    if dry == wet:
        return None
    p = 100.0 * (dry - raw) / (dry - wet)
    return round(max(0.0, min(100.0, p)), 1)


def fmt_age(s):
    if s < 120:
        return "%ds" % s
    if s < 7200:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


class Handler(http.server.BaseHTTPRequestHandler):
    app = None
    server_version = "HiveAdmin/1"

    def log_message(self, fmt, *a):
        pass                             # quiet; events go to the store

    # --- plumbing ------------------------------------------------------------
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, msg, code=400):
        self.send_json({"error": msg}, code)

    def body(self, limit=4 << 20):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n)

    def jbody(self):
        return json.loads(self.body(65536) or b"{}")

    def token(self):
        c = self.headers.get("Cookie") or ""
        m = re.search(r"hive_session=([A-Za-z0-9_-]+)", c)
        return m.group(1) if m else None

    def authed(self):
        # Flash-only mode listens on this machine alone (main() enforces it),
        # so whoever can reach the page is already at the keyboard.
        if self.app.flash_only:
            return True
        t = self.token()
        if not t:
            return False
        h = self.app.session_hash(t)
        exp = self.app.sessions.get(h)
        if not exp or exp <= time.time():
            return False
        # Slide the expiry in memory on every request, but only write it out
        # when it has moved by a day -- otherwise every page load rewrites the
        # config file.
        fresh = time.time() + 7 * 86400
        if fresh - exp > 86400:
            self.app.sessions[h] = fresh
            self.app.save_sessions()
        else:
            self.app.sessions[h] = max(exp, fresh - 86400)
        return True

    def csrf_ok(self):
        # Every state-changing call comes from our own page with this header;
        # a form on another site cannot set it.
        return self.headers.get("X-Hive") == "1"

    # --- routing -------------------------------------------------------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path in ("/", "/index.html") or u.path.startswith("/static/"):
            return self.static(u.path)
        if u.path == "/api/session":
            return self.send_json({"authed": self.authed(), "flash_only": self.app.flash_only,
                                   "needs_setup": not self.app.flash_only
                                   and not self.app.cfg.data["password"]})
        if not self.authed():
            return self.fail("login required", 401)
        try:
            return self.route_get(u.path, q)
        except Exception as e:
            return self.fail("%s: %s" % (type(e).__name__, e), 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not self.csrf_ok():
            return self.fail("bad request", 403)
        if u.path == "/api/login":
            return self.login()
        if u.path == "/api/setup":
            return self.first_setup()
        if not self.authed():
            return self.fail("login required", 401)
        try:
            return self.route_post(u.path, dict(urllib.parse.parse_qsl(u.query)))
        except (ValueError, RuntimeError, KeyError) as e:
            return self.fail(str(e))
        except Exception as e:
            return self.fail("%s: %s" % (type(e).__name__, e), 500)

    def static(self, path):
        if path in ("/", "/index.html"):
            path = "/static/index.html"
        # Resolve first, then check containment: "static/../x" is a prefix
        # match on the unresolved string. This route is served WITHOUT login,
        # so escaping it would expose everything this user can read.
        full = os.path.realpath(os.path.join(STATIC, urllib.parse.unquote(path[len("/static/"):])))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            return self.fail("not found", 404)
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                 ".css": "text/css", ".svg": "image/svg+xml"}.get(os.path.splitext(full)[1],
                                                                 "application/octet-stream")
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    # --- auth ----------------------------------------------------------------
    def new_session(self):
        t = secrets.token_urlsafe(32)
        self.app.sessions[self.app.session_hash(t)] = time.time() + 7 * 86400
        self.app.save_sessions()
        self.send_response(200)
        self.send_header("Set-Cookie", "hive_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800" % t)
        self.send_header("Content-Type", "application/json")
        body = b'{"ok":true}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def first_setup(self):
        if self.app.cfg.data["password"]:
            return self.fail("already set up", 403)
        b = self.jbody()
        pw = b.get("password", "")
        code = str(b.get("code", "")).strip()
        if not self.app.setup_code or not hmac.compare_digest(code, self.app.setup_code):
            # Six digits could be guessed with enough parallel tries; a fresh
            # code every ten misses means guessing never converges.
            self.app.setup_fails += 1
            if self.app.setup_fails >= 10:
                self.app.new_setup_code()
            time.sleep(1)
            return self.fail("wrong setup code; it is in %s on the Pi" % self.app.setup_code_path, 403)
        if len(pw) < 8:
            return self.fail("use at least 8 characters")
        self.app.cfg.set_password(pw)
        self.app.setup_code = None
        try:
            os.remove(self.app.setup_code_path)
        except OSError:
            pass
        self.app.store.event("system", "password set")
        return self.new_session()

    def login(self):
        ip = self.client_address[0]
        n, first = self.app.login_fails.get(ip, (0, time.time()))
        if time.time() - first > 900:
            n, first = 0, time.time()
        if n >= 10:
            return self.fail("too many attempts; wait 15 minutes", 429)
        if self.app.cfg.check_password(self.jbody().get("password", "")):
            self.app.login_fails.pop(ip, None)
            return self.new_session()
        self.app.login_fails[ip] = (n + 1, first)
        time.sleep(1)
        return self.fail("wrong password", 403)

    # --- API -----------------------------------------------------------------
    def route_get(self, path, q):
        app = self.app
        if path == "/api/flash":
            from flasher import find_esptool
            return self.send_json({"ports": app.flasher.ports(), "images": app.flasher.images(),
                                   "job": app.flasher.job, "next_id": app.flasher.next_id(),
                                   "esptool": find_esptool() is not None,
                                   "known_ids": sorted(int(i) for i in app.cfg.data.get("nodes", {})
                                                       if str(i).isdigit())})
        if app.flash_only:
            return self.fail("not available in flash-only mode", 404)
        if path == "/api/state":
            return self.send_json(app.state())
        if path == "/api/kinds":
            return self.send_json(app.kinds)
        if path == "/api/problems":
            evs = [e for e in app.store.events(400) if e["kind"] == "error"][:50]
            return self.send_json({"active": app.problems.active_list(), "events": evs,
                                   "codes": {str(k): v for k, v in sorted(app.problems.codes.items())}})
        if path == "/api/problems/report":
            return self.send_json(app.problem_report())
        if path == "/api/config":
            d = {k: v for k, v in app.cfg.data.items()
                 if k not in ("password", "secret", "sessions")}
            return self.send_json(d)
        if path == "/api/series":
            node, slot = int(q["node"]), q["slot"]
            t1 = int(q.get("to") or now())
            t0 = int(q.get("from") or t1 - 86400)
            return self.send_json({"points": self.series(node, slot, t0, t1), "from": t0, "to": t1})
        if path == "/api/rollup":
            # Hourly or daily summaries: {ts, secs, n, avg, min, max, sum} in
            # the slot's raw units (scale with kinds.json).
            node, slot = int(q["node"]), int(q["slot"])
            res = "day" if q.get("res") == "day" else "hour"
            t1 = int(q.get("to") or now())
            t0 = int(q.get("from") or t1 - (90 if res == "day" else 7) * 86400)
            keys = ("ts", "secs", "n", "avg", "min", "max", "sum")
            rows = [dict(zip(keys, r)) for r in app.rollup.series(node, slot, t0, t1, res)]
            return self.send_json({"rows": rows, "res": res, "from": t0, "to": t1,
                                   "raw_cutoff": app.rollup.raw_cutoff()})
        if path == "/api/waterlearn":
            # What adaptive watering has learned about a node, and what it would do.
            nid = int(q["node"])
            snap = (app.poller.snapshot or {}).get("nodes", {}).get(nid, {})
            rec = app.learner.recommend(nid, snap.get("slots") or {})
            node = app.adaptive_node(nid)
            return self.send_json({"adaptive": node["adaptive"], "plant_band": node.get("plant_band"),
                                   "recommendation": rec})
        if path == "/api/forecast/score":
            # How each public model has done against the station: mean error and
            # bias per variable and lead time (forecast - measured).
            nid = int(q["node"]) if q.get("node") else app.station_node()
            days = max(1, min(35, int(q.get("days") or 14)))
            score = app.forecasts.score(nid, days) if nid is not None else {}
            return self.send_json({"node": nid, "days": days, "score": score})
        if path == "/api/report":
            return self.send_json(self.report(int(q["node"]), q["slot"],
                                              int(q["from"]), int(q["to"])))
        if path == "/api/export.csv":
            return self.export(q)
        if path == "/api/g4rden":
            g = dict(app.cfg.data["g4rden"])
            g.pop("token", None)                  # never hand the token back out
            g["paired"] = bool(app.cfg.data["g4rden"].get("token"))
            nodes, _marks = app.uploader.build_nodes()
            return self.send_json({"config": g, "last": app.uploader.last,
                                   "last_success": app.uploader.last_success,
                                   "queued": sum(len(n["readings"]) for n in nodes),
                                   "marks": {k: app.store.upload_mark(int(k))
                                             for k in g.get("devices", {})}})
        if path == "/api/linkpath":
            # How much of a period a node's reports reached the hive directly
            # rather than through a relay. Weighted by TIME, not by row count:
            # values are stored on change, so counting rows would say a node
            # that flipped to the relay once and stayed there was "mostly
            # direct". A reading holds until the next one replaces it.
            node = int(q["node"])
            t1 = int(q.get("to") or now())
            t0 = int(q.get("from") or t1 - 86400)
            pts = app.store.series(node, HOPS_SLOT, t0, t1)
            before = app.store.last_before(node, HOPS_SLOT, t0)
            if before:
                pts.insert(0, (t0, before[1]))
            direct = relay = 0
            for i, (ts, v) in enumerate(pts):
                span = (pts[i + 1][0] if i + 1 < len(pts) else t1) - ts
                if v:
                    relay += span
                else:
                    direct += span
            total = direct + relay
            return self.send_json({"direct_seconds": direct, "relay_seconds": relay,
                                   "relay_fraction": round(relay / total, 4) if total else None,
                                   "from": t0, "to": t1, "samples": len(pts)})
        if path == "/api/notes":
            t1 = int(q.get("to") or now())
            t0 = int(q.get("from") or t1 - 86400)
            return self.send_json(app.store.notes(t0, t1))
        if path == "/api/events":
            return self.send_json(app.store.events(int(q.get("limit", 200))))
        if path == "/api/firmware":
            with app.store.db() as c:
                hist = [dict(r) for r in c.execute(
                    "SELECT id,ts,file,size,crc,family,result FROM pushes ORDER BY ts DESC LIMIT 50")]
            return self.send_json({"images": app.pusher.images(), "job": app.pusher.job,
                                   "history": hist})
        return self.fail("not found", 404)

    def autowater(self, b):
        """Automatic watering settings for a pump node with a soil probe, in the
        person's units: water below a moisture %, an amount in ml, a minimum
        gap in hours, a daily maximum in ml. The node judges dryness on its RAW
        probe reading (it has no calibration of its own), so the % is converted
        here with this probe's measured dry and wet points -- and refused if
        they were never measured, since a guessed scale would water at a guess.
        Settings go first and the on switch last, each re-sent until the node's
        own report confirms it."""
        app = self.app
        nid = int(b["node"])
        enable = 1 if b.get("enable") else 0
        conf = dict((app.cfg.node(nid).get("adaptive") or {}))
        if "from_plant" in b:
            conf["from_plant"] = bool(b["from_plant"])
        if "adaptive" in b:
            conf["on"] = bool(b["adaptive"])
        if b.get("fill_pct") not in (None, ""):
            conf["fill_pct"] = float(b["fill_pct"])
        elif "fill_pct" in b:
            conf.pop("fill_pct", None)
        # Off on purpose stays off: auto_defaults() never switches it back on.
        conf["user_off"] = not enable
        app.cfg.set_node(nid, {"adaptive": conf})
        band = app.cfg.node(nid).get("plant_band")
        if conf.get("from_plant", True) and band and b.get("below_pct") in (None, ""):
            b["below_pct"] = band[0]
        pct = float(b["below_pct"])
        ml = int(b["ml"])
        gap_min = int(round(float(b["gap_hours"]) * 60))
        day_ml = int(b["day_ml"]) if b.get("day_ml") not in (None, "") else None
        if not 0 <= pct <= 100:
            raise ValueError("moisture must be 0-100 %")
        if not 1 <= ml <= 5000:
            raise ValueError("amount must be 1-5000 ml")
        if not 10 <= gap_min <= 43200:
            raise ValueError("gap must be between 10 minutes and 30 days")
        if day_ml is not None and not (ml <= day_ml <= 20000):
            raise ValueError("the daily maximum must be at least one watering, and at most 20000 ml")
        dry, wet, calibrated = app.calibration(nid)
        if not calibrated:
            raise ValueError("calibrate this node's soil probe first (Settings: Use as dry, Use as wet)")
        below_raw = int(round(dry - pct / 100.0 * (dry - wet)))
        below_raw = max(1, min(4095, below_raw))
        snap = (app.poller.snapshot or {}).get("nodes", {}).get(nid, {})
        cap = (snap.get("slots") or {}).get(43)
        writes = [(51, ml), (52, gap_min), (50, below_raw)]
        if cap is not None and ml > cap:
            writes.append((43, ml))         # an automatic watering must not be refused by its own cap
        if day_ml is not None:
            writes.append((44, day_ml))
        writes.append((49, enable))
        replies = []
        for slot, value in writes:
            ack = app.gw.command("set %d %d %d" % (nid, slot, value), r"ACK set|ERR")
            replies.append("%d=%d %s" % (slot, value, ack or "no reply"))
            if ack and ack.startswith("ACK"):
                app.poller.track_write(nid, slot, value)
        app.store.event("command", "node %d automatic watering %s: below %.0f%% (raw %d), %d ml, gap %s h%s" % (
            nid, "ON" if enable else "off", pct, below_raw, ml, b["gap_hours"],
            ", max %d ml/24 h" % day_ml if day_ml is not None else ""))
        return {"ok": all("ACK" in r for r in replies), "below_raw": below_raw, "replies": replies}

    def series(self, node, slot, t0, t1):
        """Stored values are change-based: a value holds until the next row.
        Prepend the value in force at t0 so a chart starts at the left edge."""
        app = self.app
        if slot.startswith("d:"):
            kind = app.kind_of(node, {int(s): 0 for s in self.known_slots(node)})
            d = app.kinds.get(kind, {}).get("derived", {}).get(slot[2:])
            if not d:
                return []
            raw = self.series(node, str(d["from"]), t0, t1)
            # The same conversions as the live values, so a chart and the card
            # it came from cannot disagree.
            if d["type"] == "soil_pct":
                dry, wet, calibrated = app.calibration(node)
                if not calibrated:
                    return []
                return [(t, soil_pct(v, dry, wet)) for t, v in raw]
            sc = app.batt_scale(node)
            if d["type"] == "lipo_pct":
                return [(t, lipo_pct(v * sc) if v else None) for t, v in raw]
            if d["type"] == "volts":
                return [(t, round(v * sc / 1000.0, 2) if v else None) for t, v in raw]
            return []
        s = int(slot)
        # Raw rows older than the cleanup cutoff are gone; their hourly means
        # stand in for them, so a long chart still reaches back.
        cut = app.rollup.raw_cutoff() if app.rollup else 0
        old = []
        if t0 < cut:
            old = [(t, a) for t, _secs, _n, a, _lo, _hi, _sum in
                   app.rollup.series(node, s, t0, min(cut, t1)) if a is not None]
            t0 = cut
        if t0 >= t1:
            return old
        pts = [(t, v) for t, v in app.store.series(node, s, t0, t1) if app.plausible(node, s, v)]
        before = app.store.last_before(node, s, t0)
        if before and app.plausible(node, s, before[1]):
            pts.insert(0, (t0, before[1]))
        return old + pts

    def known_slots(self, node):
        snap = self.app.poller.snapshot
        if snap and node in snap["nodes"]:
            return list(snap["nodes"][node]["slots"])
        with self.app.store.db() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT slot FROM readings WHERE node=?", (node,))]

    def report(self, node, slot, t0, t1):
        pts = self.series(node, slot, t0, t1)
        days = {}
        for t, v in pts:
            if v is None:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(t))
            days.setdefault(day, []).append(v)
        rows = [{"day": d, "min": min(v), "max": max(v), "avg": sum(v) / len(v), "n": len(v)}
                for d, v in sorted(days.items())]
        # Days before the raw cutoff only have hourly means in the points;
        # their real low and high are in the daily summaries.
        cut = self.app.rollup.raw_cutoff() if self.app.rollup else 0
        if t0 < cut and not slot.startswith("d:"):
            daily = {time.strftime("%Y-%m-%d", time.localtime(t)): (a, lo, hi, n)
                     for t, _secs, n, a, lo, hi, _sum in
                     self.app.rollup.series(node, int(slot), t0, cut, res="day")}
            for r in rows:
                d = daily.get(r["day"])
                if d and d[0] is not None:
                    r.update({"avg": d[0], "min": d[1], "max": d[2], "n": d[3]})
        vals = [v for _, v in pts if v is not None]
        summary = ({"min": min(vals), "max": max(vals), "avg": sum(vals) / len(vals), "n": len(vals)}
                   if vals else None)
        return {"points": pts, "days": rows, "summary": summary, "from": t0, "to": t1}

    def export(self, q):
        t1 = int(q.get("to") or now())
        t0 = int(q.get("from") or 0)
        node = q.get("node")
        with self.app.store.db() as c:
            sql = "SELECT ts,node,slot,value FROM readings WHERE ts BETWEEN ? AND ?"
            args = [t0, t1]
            if node:
                sql += " AND node=?"; args.append(int(node))
            if q.get("slot") and not q["slot"].startswith("d:"):
                sql += " AND slot=?"; args.append(int(q["slot"]))
            rows = c.execute(sql + " ORDER BY ts", args).fetchall()
        lines = ["time,node,node_name,slot,label,value,scaled"]
        names, labels = {}, {}
        for ts, n, s, v in rows:
            if n not in names:
                names[n] = self.app.cfg.node(n).get("name", "")
            key = (n, s)
            if key not in labels:
                kind = self.app.kind_of(n, {int(x): 0 for x in self.known_slots(n)})
                spec = self.app.kinds.get(kind, {}).get("slots", {}).get(str(s), {})
                labels[key] = (spec.get("label", "slot %d" % s), spec.get("scale", 1))
            label, scale = labels[key]
            lines.append("%s,%d,%s,%d,%s,%d,%s" % (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)), n,
                csv_field(names[n]), s, csv_field(label), v, round(v * scale, 4)))
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Disposition", 'attachment; filename="hive-readings.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route_post(self, path, q):
        app = self.app
        if path == "/api/flash/start":
            b = self.jbody()
            job = self.app.flasher.start(b.get("device"), b.get("image"), b.get("node_id"),
                                         erase=bool(b.get("erase")))
            self.app.store.event("flash", "started %s on %s (%s), node %s" % (
                job["image"], job["mac"], job["device"], job["node_id"] or "unassigned"))
            return self.send_json({"ok": True})
        if self.app.flash_only:
            return self.fail("not available in flash-only mode", 404)
        if path == "/api/logout":
            tok = self.token()
            if tok:
                app.sessions.pop(app.session_hash(tok), None)
                app.save_sessions()
            return self.send_json({"ok": True})
        if path == "/api/password":
            b = self.jbody()
            if not app.cfg.check_password(b.get("old", "")):
                return self.fail("current password is wrong", 403)
            if len(b.get("new", "")) < 8:
                return self.fail("use at least 8 characters")
            app.cfg.set_password(b["new"])
            app.sessions.clear()             # every other device signs in again
            app.save_sessions()
            app.store.event("system", "password changed")
            return self.new_session()
        if path == "/api/g4rden/config":
            b = self.jbody()
            g = dict(app.cfg.data["g4rden"])
            if "base_url" in b:
                u = (b["base_url"] or "").strip().rstrip("/")
                if u and not re.match(r"^https?://[A-Za-z0-9.:_-]+(/.*)?$", u):
                    raise ValueError("address must start with http:// or https://")
                if u != g.get("base_url"):
                    # A different destination has not been proven; make them test
                    # again rather than silently posting the garden somewhere new.
                    g["enabled"] = False
                    g["token"] = ""
                    g["gateway_id"] = None
                g["base_url"] = u or "https://g4rden.com"
            for k, lo, hi in (("interval_seconds", 60, 86400), ("min_gap_seconds", 60, 86400),
                              ("max_per_device", 1, 500)):
                if k in b:
                    g[k] = max(lo, min(hi, int(b[k])))
            if "devices" in b:
                # node id -> the id this node has on the site. Its charset is
                # what the server accepts for `ieee`.
                out = {}
                for k, v in b["devices"].items():
                    v = str(v).strip()
                    if not v:
                        continue
                    if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,64}", v):
                        raise ValueError("device id %r: letters, digits, : _ . - only" % v)
                    out[str(int(k))] = v
                g["devices"] = out
            app.cfg.data["g4rden"] = g
            app.cfg.save()
            app.store.event("config", "g4rden: %s, devices=%s" % (g["base_url"], g["devices"] or "(none)"))
            return self.send_json({"ok": True})
        if path == "/api/g4rden/claim":
            code = str(self.jbody().get("code", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9-]{4,40}", code):
                raise ValueError("a claim code looks like K7M2P-QR9WX")
            return self.send_json(app.uploader.claim(code))
        if path == "/api/g4rden/unpair":
            app.cfg.data["g4rden"].update(token="", gateway_id=None, enabled=False)
            app.cfg.save()
            app.store.event("config", "g4rden unpaired")
            return self.send_json({"ok": True})
        if path == "/api/g4rden/preview":
            # Exactly what a send would post, built the same way. No network.
            g = app.cfg.data["g4rden"]
            nodes, _marks = app.uploader.build_nodes()
            return self.send_json({
                "url": (g.get("base_url") or "").rstrip("/") + "/api/device/ingest",
                "paired": bool(g.get("token")),
                "body": {"seq": int(g.get("retry_seq") or g.get("seq", 0)) + (0 if g.get("retry_seq") else 1),
                         "fw": Uploader.FW, "nodes": nodes}})
        if path == "/api/g4rden/send":
            return self.send_json(app.uploader.send_once())
        if path == "/api/g4rden/enable":
            want = bool(self.jbody().get("enabled"))
            if want and not app.uploader.last_success:
                raise ValueError("send one now first -- automatic uploads stay off "
                                 "until a send to this URL has worked")
            app.cfg.data["g4rden"]["enabled"] = want
            app.cfg.save()
            app.uploader.wake.set()
            app.store.event("config", "g4rden uploads %s" % ("ON" if want else "off"))
            return self.send_json({"ok": True})
        if path == "/api/note":
            text = str(self.jbody().get("text", "")).strip()[:200]
            if not text:
                raise ValueError("a marker needs a note saying what happened")
            app.store.event("note", text)
            return self.send_json({"ok": True, "ts": now()})
        if path == "/api/poll":
            app.poller.wake.set()
            return self.send_json({"ok": True})
        if path == "/api/node":
            b = self.jbody()
            nid = int(b.pop("id"))
            allowed = {"name", "location", "kind", "soil_dry", "soil_wet", "hidden",
                       "notes", "batt_scale"}
            patch = {k: v for k, v in b.items() if k in allowed}
            for k in ("soil_dry", "soil_wet"):
                if k in patch and patch[k] not in (None, ""):
                    patch[k] = int(patch[k])
            if patch.get("batt_scale") not in (None, ""):
                sc = float(patch["batt_scale"])
                if not 0.8 <= sc <= 1.25:
                    raise ValueError("battery correction must be between 0.8 and 1.25; "
                                     "a bigger gap than that is a wiring problem, not tolerance")
                patch["batt_scale"] = round(sc, 4)
            app.cfg.set_node(nid, patch)
            app.store.event("config", "node %d: %s" % (nid, ", ".join(
                "%s=%s" % kv for kv in patch.items())))
            return self.send_json({"ok": True})
        if path == "/api/settings":
            b = self.jbody()
            for k in ("poll_seconds", "full_every_seconds", "stale_seconds"):
                if k in b:
                    v = int(b[k])
                    if not 10 <= v <= 86400:
                        raise ValueError("%s out of range" % k)
                    app.cfg.data[k] = v
            app.cfg.save()
            app.poller.wake.set()
            return self.send_json({"ok": True})
        if path == "/api/set":
            b = self.jbody()
            target, slot, value = str(b["target"]), int(b["slot"]), int(b["value"])
            if not re.fullmatch(r"all|r\d{1,3}|\d{1,3}", target):
                raise ValueError("bad target")
            ack = app.gw.command("set %s %d %d" % (target, slot, value), r"ACK set|ERR")
            app.store.event("command", "set %s %d %d -> %s" % (target, slot, value, ack or "no reply"))
            tracking = False
            if ack and ack.startswith("ACK") and target.isdigit():
                tracking = app.confirmable(int(target), slot)
                if tracking:
                    app.poller.track_write(int(target), slot, value)
            return self.send_json({"reply": ack, "confirming": tracking})
        if path == "/api/autowater":
            return self.send_json(self.autowater(self.jbody()))
        if path == "/api/mode":
            b = self.jbody()
            m, p, ttl = int(b["mode"]), int(b.get("param", 0)), int(b.get("ttl", 0))
            if not (0 <= m <= 255 and 0 <= p <= 255 and 0 <= ttl <= 65535):
                raise ValueError("out of range")
            ack = app.gw.command("mode %d %d %d" % (m, p, ttl), r"ACK mode|ERR")
            app.store.event("command", "mode %d %d %d -> %s" % (m, p, ttl, ack or "no reply"))
            return self.send_json({"reply": ack})
        if path == "/api/nodelog":
            nid = int(self.jbody()["id"])
            lines = app.gw.node_log(nid)
            app.store.event("command", "log %d -> %d lines" % (nid, len(lines)))
            return self.send_json({"lines": lines})
        if path == "/api/gatewaylog":
            return self.send_json({"lines": app.gw.gateway_log()})
        if path == "/api/firmware/upload":
            name = os.path.basename(q.get("name", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,80}\.bin", name):
                raise ValueError("file name must end in .bin, letters/digits/._+- only")
            data = self.body(limit=4 << 20)
            if not Pusher.is_app_image(data):
                raise ValueError("not an ESP32 app image (a merged or bootloader .bin will not work)")
            path_ = os.path.join(app.pusher.fw_dir, name)
            with open(path_, "wb") as f:
                f.write(data)
            info = app.pusher.inspect(path_)
            app.store.event("firmware", "uploaded %s (%d bytes, crc %s, family %s)" % (
                name, info["size"], info["crc"], ",".join(info["families"]) or "none"))
            return self.send_json(info)
        if path == "/api/firmware/delete":
            name = os.path.basename(self.jbody()["name"])
            os.remove(os.path.join(app.pusher.fw_dir, name))
            app.store.event("firmware", "deleted %s" % name)
            return self.send_json({"ok": True})
        if path == "/api/firmware/push":
            name = self.jbody()["name"]
            app.pusher.start(name)
            app.store.event("firmware", "push started: %s" % name)
            return self.send_json({"ok": True})
        return self.fail("not found", 404)


def csv_field(s):
    s = str(s)
    return '"%s"' % s.replace('"', '""') if any(c in s for c in ',"\n') else s


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", help="gateway serial device (use /dev/serial/by-id/...)")
    ap.add_argument("--fake", action="store_true", help="simulate a swarm, no hardware")
    ap.add_argument("--data", default=os.path.expanduser("~/hive_data"))
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--push-script", help="path to hivewire_push.py")
    ap.add_argument("--yield-to", default="[h]ivewire_push.py",
                    help="pgrep -f pattern; stay off the port while it matches")
    ap.add_argument("--flash-only", action="store_true",
                    help="no swarm: just the Flash page, for a board on THIS machine's USB")
    args = ap.parse_args()
    if args.flash_only:
        # No login in this mode, so it must never be reachable from the network.
        args.listen = "127.0.0.1"
    elif not args.fake and not args.port:
        ap.error("--port is required (or --fake or --flash-only)")

    app = App(args)
    Handler.app = app
    if not app.flash_only:
        app.poller.start()
        app.uploader.start()
        RollupThread(app.rollup).start()
        ForecastThread(app.forecasts).start()

        def pump_nodes():
            snap = (app.poller.snapshot or {}).get("nodes", {})
            out = {}
            for nid, rec in snap.items():
                slots = rec.get("slots") or {}
                if 49 in slots and 3 in slots:          # a pump with a soil probe
                    try:
                        if app.auto_defaults(nid, slots):
                            slots = {**slots, 49: 1}
                    except Exception as e:
                        app.store.event("system", "auto default for node %s failed: %s" % (nid, e))
                    out[nid] = slots
            return out

        def command(nid, slot, value):
            ack = app.gw.command("set %d %d %d" % (nid, slot, value), r"ACK set|ERR")
            ok = bool(ack and ack.startswith("ACK"))
            if ok:
                app.poller.track_write(nid, slot, value)
            return ok

        LearnThread(app.learner, pump_nodes, command).start()
    srv = Server((args.listen, args.http_port), Handler)
    print("hive admin on http://%s:%d/ (%s)" % (args.listen, args.http_port,
          "flash only" if app.flash_only else "simulated" if args.fake else args.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
