#!/usr/bin/env python3
"""Unattended soak test of a running Hivewire swarm, on the hive's host.

    nohup python3 hive_soak.py --hours 4 > soak.out 2>&1 &

Tests the system AS IT RUNS: the admin keeps polling and uploading, the nodes
keep reporting, and this watches all of it, plus a few safe active checks. It
never holds the gateway's serial port except inside short `gw_cmd.py` calls,
which the admin already steps aside for -- two readers on one port is itself
a known bug.

Every minute (passive):
  - per node: when last heard, gaps between reports, reboots (boot counter up
    or uptime going backwards), signal, relay use, sensor-ok bits, error slots
  - the admin: still running, poll errors, uploads sent/failed
  - the host: undervoltage/throttling, temperature, load, memory, disk

Every 10 minutes (active, USB only): `dump`, for the gateway's own view --
nodes up/converged/faulted and its epoch. The gateway's uptime comes from the
timestamp on its log lines; going backwards means it rebooted.

Every 30 minutes (active, over the air): a slot WRITE to a range node's
deafen-duration setting, alternating two harmless values, then confirming the
node really applied it. Once an hour: a log fetch from one node, round-robin.
(Log replies also go out over LoRa, so this is kept rare.)

Results in --out/<start time>/: samples.jsonl (raw), events.log (anything
notable, as it happens) and summary.md (rewritten every 10 minutes, so a
partial result exists if this is stopped early). A file named DONE marks a
finished run.
"""

import argparse
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
GW_CMD = os.path.join(HOME, "gw_cmd.py")


def now():
    return time.time()


def sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return ""


class Soak:
    def __init__(self, args):
        self.a = args
        self.t0 = now()
        self.dir = os.path.join(args.out, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(self.dir, exist_ok=True)
        self.db_path = os.path.join(args.data, "hive.db")
        self.cfg_path = os.path.join(args.data, "config.json")
        self.log_path = os.path.join(args.data, "admin.log")
        self.nodes = {}          # id -> dict of running stats
        self.last_row_ts = self.t0 - 60
        self.last_event_ts = self.t0
        self.log_offset = os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0
        self.host = {"throttle_events": 0, "throttle_now": None, "temp_max": 0.0,
                     "load_max": 0.0, "mem_avail_min": None, "disk_free_min": None,
                     "admin_down_minutes": 0, "admin_pids": set()}
        self.admin = {"poll_errors": 0, "uploads_ok": 0, "uploads_failed": 0, "other_events": 0}
        self.gw_stats = {"dumps": 0, "dump_failed": 0, "uptime_last": None, "reboots": 0,
                   "epoch_last": None, "epoch_changes": 0, "up_min": None, "ok_min": None,
                   "flt_max": 0, "timeline": []}
        self.writes = {"tried": 0, "acked": 0, "applied": 0, "latency_s": []}
        self.logs = {"tried": 0, "answered": 0}
        self.notes = []
        self.pending_write = None
        self.log_rr = 0
        self.event("soak started: %.1f h, output %s" % (args.hours, self.dir))

    # ---------------------------------------------------------------- output
    def event(self, msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        with open(os.path.join(self.dir, "events.log"), "a") as f:
            f.write(line + "\n")

    def sample(self, kind, data):
        data = dict(data, kind=kind, t=round(now(), 1))
        with open(os.path.join(self.dir, "samples.jsonl"), "a") as f:
            f.write(json.dumps(data, default=list) + "\n")

    # ---------------------------------------------------------------- passive
    def kinds(self):
        try:
            c = json.load(open(self.cfg_path))
            return {int(k): v.get("auto_kind") or v.get("kind") for k, v in c.get("nodes", {}).items()}
        except (OSError, ValueError):
            return {}

    def node(self, nid):
        return self.nodes.setdefault(nid, {
            "readings": 0, "last": None, "max_gap": 0, "gaps_over_20m": 0,
            "reboots": 0, "boots_last": None, "uptime_last": None, "rssi": [],
            "hops": [], "ok_bits": None, "ok_drops": 0, "err_last": 0, "err_count": 0,
            "soil_raw": [], "batt": []})

    def poll_db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        rows = db.execute("SELECT ts,node,slot,value FROM readings WHERE ts > ? ORDER BY ts",
                          (self.last_row_ts,)).fetchall()
        ev = db.execute("SELECT ts,kind,text FROM events WHERE ts > ? ORDER BY ts",
                        (self.last_event_ts,)).fetchall()
        db.close()
        kinds = self.kinds()
        for ts, nid, slot, val in rows:
            self.last_row_ts = max(self.last_row_ts, ts)
            n = self.node(nid)
            n["kind"] = kinds.get(nid)
            if n["last"] is not None and ts - n["last"] > n["max_gap"]:
                n["max_gap"] = ts - n["last"]
            if n["last"] is not None and ts - n["last"] > 1200:
                n["gaps_over_20m"] += 1
                self.event("node %d silent for %.0f min before this report" % (nid, (ts - n["last"]) / 60))
            n["last"] = max(n["last"] or 0, ts)
            n["readings"] += 1
            kind = n["kind"]
            boots_slot, uptime_slot, rssi_slot = (2, 1, 3) if kind == "RangeNode" else (7, 8, 9)
            if slot == boots_slot:
                if n["boots_last"] is not None and val > n["boots_last"]:
                    n["reboots"] += val - n["boots_last"]
                    self.event("node %d REBOOTED (boot counter %d -> %d)" % (nid, n["boots_last"], val))
                n["boots_last"] = val
            elif slot == uptime_slot:
                if n["uptime_last"] is not None and val + 2 < n["uptime_last"] and n["boots_last"] is None:
                    self.event("node %d uptime went backwards (%d -> %d)" % (nid, n["uptime_last"], val))
                n["uptime_last"] = val
            elif slot == rssi_slot:
                n["rssi"].append(val)
            elif slot == 250:
                n["hops"].append(val)
            elif slot == 6 and kind == "SoilNode":
                if n["ok_bits"] is not None and (n["ok_bits"] & ~val) & 3:
                    n["ok_drops"] += 1
                    self.event("node %d sensor-ok bits dropped %d -> %d" % (nid, n["ok_bits"], val))
                n["ok_bits"] = val
            elif slot == 3 and kind == "SoilNode":
                n["soil_raw"].append(val)
            elif slot == 10 and kind == "SoilNode":
                n["batt"].append(val)
            elif slot == 26:
                code, subj = val & 0xFFFF, (val >> 16) & 0xFF
                if code and val != n["err_last"]:
                    self.event("node %d raised E%d%s" % (nid, code, "/%d" % subj if subj else ""))
                n["err_last"] = val
            elif slot == 27:
                n["err_count"] = val
        for ts, kind, text in ev:
            self.last_event_ts = max(self.last_event_ts, ts)
            if kind == "upload":
                if text.startswith("sent"):
                    self.admin["uploads_ok"] += 1
                    m = re.match(r"sent (\d+) reading.*?accepted (\d+)", text)
                    if m:
                        sent, acc = int(m.group(1)), int(m.group(2))
                        self.admin["readings_sent"] = self.admin.get("readings_sent", 0) + sent
                        self.admin["readings_accepted"] = self.admin.get("readings_accepted", 0) + acc
                        if acc < sent and "duplicate" not in text:
                            self.event("g4rden kept %d of %d readings in one upload" % (acc, sent))
                    m = re.search(r"clock ([+-]\d+) s vs site", text)
                    if m:
                        sk = int(m.group(1))
                        self.admin["skew_max"] = max(self.admin.get("skew_max", 0), abs(sk))
                        if abs(sk) > 30:
                            self.event("host clock is %+d s off the site's" % sk)
                else:
                    self.admin["uploads_failed"] += 1
                    self.event("upload problem: " + text[:120])
            elif kind not in ("poll",):
                self.admin["other_events"] += 1
                self.event("admin event [%s] %s" % (kind, text[:120]))

    def poll_admin_log(self):
        if not os.path.exists(self.log_path):
            return
        size = os.path.getsize(self.log_path)
        if size < self.log_offset:
            self.log_offset = 0
        with open(self.log_path, errors="replace") as f:
            f.seek(self.log_offset)
            new = f.read()
            self.log_offset = f.tell()
        for line in new.splitlines():
            if "another tool is using the gateway" in line:
                continue            # that tool is this soak's own probe
            if "poll:" in line and "poll: ok" not in line:
                self.admin["poll_errors"] += 1
                if self.admin["poll_errors"] <= 20:
                    self.event("admin poll error: " + line.strip()[:140])
            elif line.startswith("hive admin on"):
                self.event("admin (re)started")

    def poll_host(self):
        h = self.host
        pids = sh("pgrep -f 'python3 [^ ]*hive_admin\\.py'").split()
        if not pids:
            h["admin_down_minutes"] += 1
            self.event("admin NOT RUNNING")
        elif h["admin_pids"] and set(pids) != h["admin_pids"]:
            self.event("admin process changed %s -> %s (restart)" % (sorted(h["admin_pids"]), pids))
        h["admin_pids"] = set(pids)
        thr = sh("vcgencmd get_throttled").strip()
        m = re.search(r"0x([0-9a-f]+)", thr)
        if m:
            bits = int(m.group(1), 16)
            now_bits = bits & 0xF          # current: undervoltage, freq cap, throttled, temp limit
            if now_bits and now_bits != h["throttle_now"]:
                h["throttle_events"] += 1
                self.event("HOST POWER/THERMAL: throttled=0x%x (now bits 0x%x)" % (bits, now_bits))
            h["throttle_now"] = now_bits
        tm = re.search(r"([\d.]+)", sh("vcgencmd measure_temp"))
        temp = float(tm.group(1)) if tm else 0.0
        h["temp_max"] = max(h["temp_max"], temp)
        try:
            load = os.getloadavg()[0]
            h["load_max"] = max(h["load_max"], load)
        except OSError:
            load = None
        mem = re.search(r"MemAvailable:\s+(\d+)", open("/proc/meminfo").read())
        avail = int(mem.group(1)) // 1024 if mem else None
        if avail is not None:
            h["mem_avail_min"] = avail if h["mem_avail_min"] is None else min(h["mem_avail_min"], avail)
        st = os.statvfs(HOME)
        free_gb = st.f_bavail * st.f_frsize / 1e9
        h["disk_free_min"] = free_gb if h["disk_free_min"] is None else min(h["disk_free_min"], free_gb)
        self.sample("host", {"temp": temp, "load": load, "mem_avail_mb": avail,
                             "disk_free_gb": round(free_gb, 2), "throttled": thr})

    # ---------------------------------------------------------------- active
    def gw(self, cmd, secs):
        if not os.path.exists(GW_CMD):
            return None
        return sh("python3 %s %s %s" % (GW_CMD, json.dumps(cmd), secs), timeout=secs + 25)

    def probe_dump(self):
        out = self.gw("dump", 6)
        s = self.gw_stats
        if not out or "DUMP END" not in out:
            s["dump_failed"] += 1
            self.event("gateway did not answer dump")
            return
        s["dumps"] += 1
        m = re.search(r"DUMP END (\d+) ep=(\d+) m=(\d+) up=(\d+) ok=(\d+) flt=(\d+)", out)
        if m:
            _n, ep, _m, up, ok, flt = map(int, m.groups())
            if s["epoch_last"] is not None and ep != s["epoch_last"]:
                s["epoch_changes"] += 1
                self.event("gateway epoch %d -> %d%s" % (s["epoch_last"], ep,
                           " (went BACKWARDS: gateway rebooted?)" if ep < s["epoch_last"] else ""))
            s["epoch_last"] = ep
            s["up_min"] = up if s["up_min"] is None else min(s["up_min"], up)
            s["ok_min"] = ok if s["ok_min"] is None else min(s["ok_min"], ok)
            s["flt_max"] = max(s["flt_max"], flt)
            s["timeline"].append([round(now()), up, ok, flt])
            ages = {int(a): int(b) for a, b in re.findall(r"DUMP (\d+) age=(\d+)", out)}
            self.sample("dump", {"ep": ep, "up": up, "ok": ok, "flt": flt, "ages": ages})
            # A node the gateway has not heard for a while. Its old values stay
            # in every dump, so this is the only place a switched-off node shows.
            for nid, age in ages.items():
                n = self.node(nid)
                quiet = age > 300
                if quiet != n.get("quiet", False):
                    self.event("node %d %s (gateway last heard it %d s ago)" % (
                        nid, "went QUIET" if quiet else "is back", age))
                    if quiet:
                        n["quiet_events"] = n.get("quiet_events", 0) + 1
                n["quiet"] = quiet
                n["age"] = age

    def check_gw_uptime(self, out):
        s = self.gw_stats
        ups = [int(x) for x in re.findall(r"<- (\d+)s ", out or "")]
        if ups:
            u = ups[-1]
            if s["uptime_last"] is not None and u + 5 < s["uptime_last"]:
                s["reboots"] += 1
                self.event("GATEWAY REBOOTED (uptime %ds -> %ds)" % (s["uptime_last"], u))
            s["uptime_last"] = u

    def probe_write(self):
        """Alternate a range node's deafen-duration setting between two harmless
        values and confirm the node applied it. Deafen only takes effect when a
        deafen TARGET is also set, which this never touches."""
        target = self.a.write_node
        if target is None:
            return
        # Always a CHANGE from what the node last reported: writing the value it
        # already holds would be "confirmed" by a write that never arrived.
        want = 601 if self.last_value(target, 21) == 600 else 600
        out = self.gw("set %d 21 %d" % (target, want), 6)
        self.check_gw_uptime(out)
        self.writes["tried"] += 1
        if out and "ACK set 21=%d" % want in out:
            self.writes["acked"] += 1
            self.pending_write = (want, now())
        else:
            self.event("write to node %d not acknowledged" % target)

    def check_write_applied(self):
        if not self.pending_write:
            return
        want, t = self.pending_write
        db = sqlite3.connect(self.db_path, timeout=10)
        row = db.execute("SELECT ts,value FROM readings WHERE node=? AND slot=21 AND value=? AND ts>=? "
                         "ORDER BY ts LIMIT 1", (self.a.write_node, want, int(t) - 1)).fetchone()
        db.close()
        if row:
            self.writes["applied"] += 1
            self.writes["latency_s"].append(round(row[0] - t, 1))
            self.pending_write = None
        elif now() - t > 1500:
            self.event("write %d to node %d never showed up in its reports" % (want, self.a.write_node))
            self.pending_write = None

    def last_value(self, nid, slot):
        db = sqlite3.connect(self.db_path, timeout=10)
        row = db.execute("SELECT value FROM readings WHERE node=? AND slot=? ORDER BY ts DESC LIMIT 1",
                         (nid, slot)).fetchone()
        db.close()
        return row[0] if row else None

    def probe_log(self):
        ids = sorted(n for n in self.nodes if self.nodes[n]["last"] and not self.nodes[n].get("quiet"))
        if not ids:
            return
        nid = ids[self.log_rr % len(ids)]
        self.log_rr += 1
        out = self.gw("log %d" % nid, 12)
        self.check_gw_uptime(out)
        self.logs["tried"] += 1
        if out and ("N%d |" % nid) in out:
            self.logs["answered"] += 1
            # Each distinct error line once per run: the gateway echoes every
            # line on two uplinks, and the node's ring still holds last hour's.
            seen = self.__dict__.setdefault("log_lines_seen", set())
            for code in re.findall(r"N%d \| (E\d+[^\n]*)" % nid, out):
                key = (nid, code.strip())
                if key not in seen:
                    seen.add(key)
                    self.event("node %d log carries %s" % (nid, code.strip()[:80]))
        else:
            hops = self.nodes[nid]["hops"][-12:]
            relayed = sum(1 for h in hops if h > 0)
            self.event("node %d did not answer a log request%s" % (
                nid, " (reaches the gateway through a relay %d/%d of late reports)" % (relayed, len(hops))
                if relayed else ""))

    # ---------------------------------------------------------------- report
    def summary(self, final=False):
        el = (now() - self.t0) / 3600
        L = ["# Hivewire soak test %s" % ("— finished" if final else "— in progress"), "",
             "Started %s, ran %.2f h of %.1f h planned." % (
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(self.t0)), el, self.a.hours), ""]
        L += ["## Nodes", "", "| Node | Kind | Readings stored | Last stored | Longest gap between stored readings | Gaps >20 min | Reboots | Signal min/median/max | Relayed | Sensor drops | Errors (last, count) | Went quiet |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for nid in sorted(self.nodes):
            n = self.nodes[nid]
            r = n["rssi"]
            sig = "%d / %d / %d" % (min(r), statistics.median(r), max(r)) if r else "—"
            hop = "%d%%" % round(100 * sum(1 for h in n["hops"] if h > 0) / len(n["hops"])) if n["hops"] else "—"
            last = "%.0f min ago" % ((now() - n["last"]) / 60) if n["last"] else "never"
            err = n["err_last"]
            errs = ("E%d%s, %d" % (err & 0xFFFF, "/%d" % (err >> 16) if err >> 16 else "", n["err_count"])) if err else "none"
            quiet = ("%d time(s)%s" % (n.get("quiet_events", 0), ", NOW" if n.get("quiet") else "")
                     if n.get("quiet_events") else "no")
            L.append("| %d | %s | %d | %s | %.1f min | %d | %d | %s | %s | %d | %s | %s |" % (
                nid, n.get("kind") or "?", n["readings"], last, n["max_gap"] / 60, n["gaps_over_20m"],
                n["reboots"], sig, hop, n["ok_drops"], errs, quiet))
        g = self.gw_stats
        L += ["", "## Gateway", "",
              "- dump probes answered: %d, failed: %d" % (g["dumps"], g["dump_failed"]),
              "- nodes up (min over run): %s, converged (min): %s, faults (max): %d" % (g["up_min"], g["ok_min"], g["flt_max"]),
              "- epoch changes: %d; gateway reboots detected: %d; last uptime seen: %s s" % (
                  g["epoch_changes"], g["reboots"], g["uptime_last"]),
              "- slot writes: %d tried, %d acknowledged, %d confirmed applied%s" % (
                  self.writes["tried"], self.writes["acked"], self.writes["applied"],
                  (", apply latency median %.0f s" % statistics.median(self.writes["latency_s"])) if self.writes["latency_s"] else ""),
              "- over-the-air log fetches: %d tried, %d answered" % (self.logs["tried"], self.logs["answered"])]
        h = self.host
        L += ["", "## Host", "",
              "- admin not running for %d minute(s); poll errors: %d; uploads ok: %d, failed: %d" % (
                  h["admin_down_minutes"], self.admin["poll_errors"], self.admin["uploads_ok"], self.admin["uploads_failed"]),
              "- g4rden: %d reading(s) sent, %d accepted; largest clock difference from the site %s s" % (
                  self.admin.get("readings_sent", 0), self.admin.get("readings_accepted", 0),
                  self.admin.get("skew_max", "—")),
              "- power/thermal throttling events: %d (current bits: %s)" % (h["throttle_events"], h["throttle_now"]),
              "- temperature max %.1f °C, load max %.2f, memory available min %s MB, disk free min %.1f GB" % (
                  h["temp_max"], h["load_max"], h["mem_avail_min"], h["disk_free_min"] or 0)]
        L += ["", "Notable events are in `events.log`; raw samples in `samples.jsonl`."]
        with open(os.path.join(self.dir, "summary.md"), "w") as f:
            f.write("\n".join(L) + "\n")

    def run(self):
        end = self.t0 + self.a.hours * 3600
        minute = 0
        while now() < end:
            try:
                self.poll_db()
                self.poll_admin_log()
                self.poll_host()
                self.check_write_applied()
                if minute % 10 == 0:
                    self.probe_dump()
                if minute % 30 == 5:
                    self.probe_write()
                if minute % 60 == 15:
                    self.probe_log()
                if minute % 10 == 0:
                    self.summary()
            except Exception as e:      # noqa: BLE001 -- a soak must outlive its own bugs
                self.event("soak harness error: %s: %s" % (type(e).__name__, e))
            minute += 1
            time.sleep(max(1, 60 - (now() - self.t0) % 60))
        if self.a.write_node is not None:
            self.gw("set %d 21 600" % self.a.write_node, 6)   # leave it as found
        self.poll_db()
        self.summary(final=True)
        open(os.path.join(self.dir, "DONE"), "w").write(time.ctime() + "\n")
        self.event("soak finished")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=4)
    ap.add_argument("--data", default=os.path.join(HOME, "hive_data"))
    ap.add_argument("--out", default=os.path.join(HOME, "hive_soak"))
    ap.add_argument("--write-node", type=int, default=3,
                    help="range node whose deafen-duration setting is toggled (none to skip)")
    args = ap.parse_args()
    Soak(args).run()


if __name__ == "__main__":
    main()
