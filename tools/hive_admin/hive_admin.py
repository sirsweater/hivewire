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
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

DUMP_LINE = re.compile(r"^DUMP (\d+) age=(\d+) hops=(\d+)(.*)$")
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

    def events(self, limit=200):
        with self.db() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))]

    def series(self, node, slot, t0, t1):
        with self.db() as c:
            return [(r[0], r[1]) for r in c.execute(
                "SELECT ts, value FROM readings WHERE node=? AND slot=? AND ts BETWEEN ? AND ? "
                "ORDER BY ts", (node, slot, t0, t1))]

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
                "poll_seconds": 60, "full_every_seconds": 900, "stale_seconds": 300}

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
                    "12=0 13=0 20=0 21=600 22=0 23=0 24=0 25=3502314079"
                    % (random.randint(0, 20), t // 60, 400 + t // 5, self.ep, ),
                    "DUMP 3 age=%d hops=0 1=%d 2=6 3=-38 4=-71 5=2 6=%d 7=3 8=0 9=2 10=0 11=%d "
                    "12=0 13=0 20=0 21=600 22=0 23=0 24=0 25=3502314079"
                    % (random.randint(0, 20), t // 60, 390 + t // 5, self.ep),
                    "DUMP 11 age=%d hops=0 1=%d 2=%d 3=%d 4=45 5=%d 6=3 7=2 8=%d 9=-30 "
                    "22=0 23=0 25=1281381655"
                    % (random.randint(0, 30), temp, 4500 + random.randint(-80, 80), self.soil,
                       4050 + random.randint(-10, 10), t // 60),
                ]
                return "\n".join(lines) + "\nDUMP END 3 ep=%d m=%d up=3 ok=3 flt=0\n" % (self.ep, self.mode)
            if cmd.startswith("mode"):
                self.mode = int(cmd.split()[1]); self.ep += 1
                return "[uplink-usb] ACK mode=%d ep=%d\n" % (self.mode, self.ep)
            if cmd.startswith("set"):
                _, tgt, slot, val = cmd.split()
                return "[uplink-usb] ACK set %s=%s\n" % (slot, val)
            if cmd.startswith("log "):
                nid = int(cmd.split()[1])
                return "\n".join("[uplink-usb] N%d | %s" % (nid, x) for x in
                                 ("boot #2", "adopt ep=%d len=2" % self.ep, "fw: already running 4c605517"))
            if cmd == "log":
                return "[uplink-usb] L | boot ok | cmd dump\n"
            return ""


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

    def run(self):
        reported = "(not yet polled)"
        while True:
            try:
                self.poll()
            except Exception as e:      # never let the poller die
                self.last_error = "%s: %s" % (type(e).__name__, e)
            # Say so in the log when polling starts or stops working -- once
            # per change, not once per poll.
            if self.last_error != reported:
                print("%s poll: %s" % (time.strftime("%H:%M:%S"), self.last_error or "ok"),
                      flush=True)
                reported = self.last_error
            self.wake.wait(max(5, self.cfg.data.get("poll_seconds", 60)))
            self.wake.clear()

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
        self.last_error = None
        t = now()
        d["time"] = t
        self.snapshot = d
        full = self.cfg.data.get("full_every_seconds", 900)
        rows = []
        for nid, rec in d["nodes"].items():
            if nid not in self.seen_nodes:
                self.seen_nodes.add(nid)
                if not self.cfg.node(nid):
                    self.store.event("node", "node %d first seen" % nid)
                    self.cfg.set_node(nid, {"first_seen": t})
            for sid, v in rec["slots"].items():
                prev = self.last_logged.get((nid, sid))
                if prev is None or prev[0] != v or t - prev[1] >= full:
                    rows.append((t, nid, sid, v))
                    self.last_logged[(nid, sid)] = (v, t)
        if rows:
            self.store.add_readings(rows)


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
            if f.endswith(".bin"):
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
        self.gw = FakeGateway() if args.fake else Gateway(args.port, args.yield_to)
        self.poller = Poller(self.gw, self.store, self.cfg)
        push_script = args.push_script or os.path.join(HERE, "..", "hivewire_push.py")
        self.pusher = Pusher(self.gw, self.store, os.path.join(args.data, "firmware"),
                             push_script, args.fake)
        self.kinds = json.load(open(os.path.join(HERE, "kinds.json"), encoding="utf-8"))
        self.sessions = {}              # token -> expiry
        # Until a password exists, anyone on the network who loads the page
        # first could choose it. Setting it needs this code, which only
        # someone with access to this machine can read.
        self.setup_code_path = os.path.join(args.data, "setup_code.txt")
        self.setup_fails = 0
        self.setup_code = None
        if not self.cfg.data["password"]:
            self.new_setup_code()
        self.login_fails = {}           # ip -> (count, first)
        self.store.event("system", "admin started (%s)" % ("simulated gateway" if args.fake else args.port))

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
        n = self.cfg.node(nid)
        return n.get("soil_dry", 2800), n.get("soil_wet", 1200)

    def derived(self, nid, kind, slots):
        """Values computed here rather than in firmware -- e.g. soil moisture
        from the raw reading and this node's calibration, so recalibrating
        never needs a reflash and applies to the whole history."""
        out = {}
        spec = self.kinds.get(kind, {})
        for key, d in spec.get("derived", {}).items():
            if d["type"] == "soil_pct" and d["from"] in slots:
                out[key] = soil_pct(slots[d["from"]], *self.calibration(nid))
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
                okslot = self.kinds.get(kind, {}).get("ok_slot")
                if okslot and str(okslot["slot"]) in map(str, rec["slots"]):
                    v = rec["slots"][okslot["slot"]]
                    for bit, label in okslot["bits"].items():
                        if not v & int(bit):
                            warnings.append(label)
                nodes.append({"id": nid, "kind": kind, "name": meta.get("name") or "",
                              "location": meta.get("location") or "", "age": age,
                              "hops": rec["hops"], "slots": rec["slots"],
                              "derived": self.derived(nid, kind, rec["slots"]),
                              "warnings": warnings, "hidden": bool(meta.get("hidden"))})
        return {"time": now(), "snapshot_time": snap["time"] if snap else None,
                "health": snap["health"] if snap else None, "nodes": nodes,
                "error": self.poller.last_error, "pushing": self.gw.pushing,
                "fake": bool(self.args.fake)}


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
        t = self.token()
        exp = self.app.sessions.get(t) if t else None
        if exp and exp > time.time():
            self.app.sessions[t] = time.time() + 7 * 86400
            return True
        return False

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
            return self.send_json({"authed": self.authed(),
                                   "needs_setup": not self.app.cfg.data["password"]})
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
        self.app.sessions[t] = time.time() + 7 * 86400
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
        if path == "/api/state":
            return self.send_json(app.state())
        if path == "/api/kinds":
            return self.send_json(app.kinds)
        if path == "/api/config":
            d = {k: v for k, v in app.cfg.data.items() if k not in ("password", "secret")}
            return self.send_json(d)
        if path == "/api/series":
            node, slot = int(q["node"]), q["slot"]
            t1 = int(q.get("to") or now())
            t0 = int(q.get("from") or t1 - 86400)
            return self.send_json({"points": self.series(node, slot, t0, t1), "from": t0, "to": t1})
        if path == "/api/report":
            return self.send_json(self.report(int(q["node"]), q["slot"],
                                              int(q["from"]), int(q["to"])))
        if path == "/api/export.csv":
            return self.export(q)
        if path == "/api/events":
            return self.send_json(app.store.events(int(q.get("limit", 200))))
        if path == "/api/firmware":
            with app.store.db() as c:
                hist = [dict(r) for r in c.execute(
                    "SELECT id,ts,file,size,crc,family,result FROM pushes ORDER BY ts DESC LIMIT 50")]
            return self.send_json({"images": app.pusher.images(), "job": app.pusher.job,
                                   "history": hist})
        return self.fail("not found", 404)

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
            dry, wet = app.calibration(node)
            return [(t, soil_pct(v, dry, wet)) for t, v in raw]
        s = int(slot)
        pts = app.store.series(node, s, t0, t1)
        before = app.store.last_before(node, s, t0)
        if before:
            pts.insert(0, (t0, before[1]))
        return pts

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
        if path == "/api/logout":
            app.sessions.pop(self.token(), None)
            return self.send_json({"ok": True})
        if path == "/api/password":
            b = self.jbody()
            if not app.cfg.check_password(b.get("old", "")):
                return self.fail("current password is wrong", 403)
            if len(b.get("new", "")) < 8:
                return self.fail("use at least 8 characters")
            app.cfg.set_password(b["new"])
            app.sessions.clear()
            app.store.event("system", "password changed")
            return self.new_session()
        if path == "/api/poll":
            app.poller.wake.set()
            return self.send_json({"ok": True})
        if path == "/api/node":
            b = self.jbody()
            nid = int(b.pop("id"))
            allowed = {"name", "location", "kind", "soil_dry", "soil_wet", "hidden", "notes"}
            patch = {k: v for k, v in b.items() if k in allowed}
            for k in ("soil_dry", "soil_wet"):
                if k in patch and patch[k] not in (None, ""):
                    patch[k] = int(patch[k])
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
            return self.send_json({"reply": ack})
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
    args = ap.parse_args()
    if not args.fake and not args.port:
        ap.error("--port is required (or --fake)")

    app = App(args)
    Handler.app = app
    app.poller.start()
    srv = Server((args.listen, args.http_port), Handler)
    print("hive admin on http://%s:%d/ (%s)" % (args.listen, args.http_port,
          "simulated" if args.fake else args.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
