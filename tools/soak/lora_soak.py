#!/usr/bin/env python3
"""LoRa soak: exercise a MeshtasticGateway's long-range link from a second radio.

    python lora_soak.py --mac <this radio's USB MAC> --gateway !<gateway node id> --hours 16

Runs on any machine with a Meshtastic radio on USB that shares the swarm's
private channel. It is the far end of the link -- the phone or remote radio in

    this radio <--LoRa--> gateway's Meshtastic node <--UART--> gateway <--ESP-NOW--> swarm

and measures, for as long as it runs:

  - digests: the gateway's routine "HW up=.." line should arrive every 15 min;
    every gap, and the signal each arrived at
  - round trip: once an hour, `status` on the swarm channel, timed until the
    digest comes back
  - commands: once an hour, a harmless slot write (a range node's
    deafen-duration setting, alternated between two values), which must be
    acknowledged over LoRa; with --pi, also confirmed in the host's database
  - completeness: a digest is a health line (HW up=N ...) followed by data
    lines (D1, D2, ...); it is complete when the data lines name all N nodes.
    A digest missing lines means the link, or the gateway's pacing, lost some
  - refusal: every few hours, the same `status` as a DIRECT message on the
    public primary channel; the gateway must refuse it -- silently, since
    answering would tell a stranger the swarm exists -- so the pass condition
    is that no digest comes back

Its own sends go out with no hops (--hop-limit 0): the test is of the direct
link, and a soak should not spend the public mesh's airtime.

Results in --out/lora-<start time>/: packets.jsonl (every packet from the
gateway), events.log (anything unexpected), summary.md (rewritten every 10 min).
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time

try:
    import meshtastic.serial_interface as mt_serial
    from pubsub import pub
    from serial.tools import list_ports
except ImportError:
    sys.exit("needs: pip install meshtastic")

DIGEST_EXPECTED_S = 900
REPLY_WAIT_S = 120


def now():
    return time.time()


def port_for_mac(mac):
    """The serial port whose USB serial number is this MAC. Boards are picked by
    MAC, never by port name: port names move when boards are replugged."""
    want = mac.lower().replace("-", ":")
    for p in list_ports.comports():
        if (p.serial_number or "").lower() == want:
            return p.device
    return None


class LoraSoak:
    def __init__(self, a):
        self.a = a
        self.t0 = now()
        self.dir = os.path.join(a.out, "lora-" + time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self.iface = None
        self.gw_num = int(a.gateway.lstrip("!"), 16)
        self.digests = []            # (ts, snr, rssi, hops)
        self.packets = 0
        self.rx_other_channel = 0
        self.waiting = None          # (kind, sent_at, pattern)
        self.rtt = {"tried": 0, "answered": 0, "s": []}
        self.cmds = {"tried": 0, "acked": 0, "confirmed": 0, "not_confirmed": 0, "s": []}
        self.refusal = {"tried": 0, "refused": 0, "wrongly_answered": 0}
        self.burst = None            # the digest being assembled: {ts, up, nodes, chunks}
        self.bursts = {"complete": 0, "partial": 0, "headless": 0}
        self.pending_confirm = None  # (value, sent_at)
        self.reconnects = 0
        self.send_errors = 0
        self.event("lora soak started: %.1f h, gateway %s, output %s" % (a.hours, a.gateway, self.dir))

    # ------------------------------------------------------------- plumbing
    def event(self, msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        with open(os.path.join(self.dir, "events.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def connect(self):
        port = port_for_mac(self.a.mac)
        if not port:
            raise RuntimeError("no radio with MAC %s on USB" % self.a.mac)
        self.iface = mt_serial.SerialInterface(port)
        time.sleep(2)

    def close(self):
        try:
            if self.iface:
                self.iface.close()
        except Exception:        # noqa: BLE001 -- closing a dead port can throw anything
            pass
        self.iface = None

    def on_receive(self, packet, interface=None):
        try:
            self._on_receive(packet)
        except Exception as e:   # noqa: BLE001 -- never let a callback kill the soak
            self.event("receive handler error: %s: %s" % (type(e).__name__, e))

    def _on_receive(self, p):
        if p.get("from") != self.gw_num:
            return
        dec = p.get("decoded") or {}
        if dec.get("portnum") != "TEXT_MESSAGE_APP":
            return
        text = dec.get("text") or ""
        ch = p.get("channel", 0)
        hops = (p.get("hopStart") or 0) - (p.get("hopLimit") or 0) if p.get("hopStart") else None
        rec = {"ts": round(now(), 1), "ch": ch, "snr": p.get("rxSnr"), "rssi": p.get("rxRssi"),
               "hops": hops, "text": text}
        with open(os.path.join(self.dir, "packets.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        with self.lock:
            self.packets += 1
            if ch != self.a.channel:
                self.rx_other_channel += 1
                self.event("gateway sent on channel %s, not the swarm channel: %s" % (ch, text[:80]))
            if text.startswith("HW up="):
                self.close_burst()
                m = re.match(r"HW up=(\d+)", text)
                self.burst = {"ts": rec["ts"], "up": int(m.group(1)) if m else 0,
                              "nodes": set(), "chunks": set()}
            elif re.match(r"D\d+ ", text):
                n = int(text[1:text.index(" ")])
                if self.burst is None or rec["ts"] - self.burst["ts"] > 90:
                    # Data with no health line in front of it: the HW line was lost.
                    self.close_burst()
                    self.bursts["headless"] += 1
                    self.event("digest data %s arrived without its health line" % text[:3])
                else:
                    self.burst["chunks"].add(n)
                    self.burst["nodes"].update(int(x) for x in re.findall(r" (\d+)\.\d+=", text))
            if text.startswith("HW up="):
                if self.digests and rec["ts"] - self.digests[-1][0] > DIGEST_EXPECTED_S + 300:
                    self.event("digest gap %.1f min" % ((rec["ts"] - self.digests[-1][0]) / 60))
                self.digests.append((rec["ts"], rec["snr"], rec["rssi"], hops))
            if re.search(r"\bE\d{3,4}\b", text) and not text.startswith(("L ", "L|")):
                self.event("gateway reported: " + text[:120])
            w = self.waiting
            if w and re.search(w[2], text):
                dt = rec["ts"] - w[1]
                if w[0] == "rtt":
                    self.rtt["answered"] += 1
                    self.rtt["s"].append(round(dt, 1))
                elif w[0] == "cmd":
                    self.cmds["acked"] += 1
                    self.cmds["s"].append(round(dt, 1))
                    self.pending_confirm = (w[3], w[1])
                elif w[0] == "refusal":
                    self.refusal["refused"] += 1
                self.waiting = None
            elif w and w[0] == "refusal" and text.startswith("HW up=") and rec["ts"] - w[1] < 60:
                self.refusal["wrongly_answered"] += 1
                self.waiting = None
                self.event("SECURITY: gateway answered a command sent on the public channel")

    def send(self, text, direct=False):
        try:
            if direct:
                # A direct message on the PRIMARY channel: encrypted to the
                # gateway's node alone, but arriving on channel 0 -- exactly what
                # the gateway must refuse.
                self.iface.sendText(text, destinationId=self.a.gateway, channelIndex=0, hopLimit=self.a.hop_limit)
            else:
                self.iface.sendText(text, channelIndex=self.a.channel, hopLimit=self.a.hop_limit)
            return True
        except Exception as e:   # noqa: BLE001
            self.send_errors += 1
            self.event("send failed: %s: %s" % (type(e).__name__, e))
            return False

    def await_reply(self, kind, text, pattern, direct=False, value=None):
        with self.lock:
            self.waiting = (kind, now(), pattern, value)
        if not self.send(text, direct):
            with self.lock:
                self.waiting = None
            return
        deadline = now() + (60 if kind == "refusal" else REPLY_WAIT_S)
        while now() < deadline:
            with self.lock:
                if self.waiting is None:
                    return
            time.sleep(1)
        with self.lock:
            self.waiting = None
        if kind == "rtt":
            self.event("no digest came back within %d s of `status`" % REPLY_WAIT_S)
        elif kind == "cmd":
            self.event("`%s` not acknowledged over LoRa within %d s" % (text, REPLY_WAIT_S))
            # The write may still have reached the node -- only the reply was
            # lost. Seen: an unacknowledged write applied a minute later.
            self.pending_confirm = (value, deadline - REPLY_WAIT_S)
        elif kind == "refusal":
            self.refusal["refused"] += 1     # silence is the correct answer

    # --------------------------------------------------------------- probes
    def probe_rtt(self):
        self.rtt["tried"] += 1
        self.await_reply("rtt", "status", r"^HW up=")

    def pi_query(self, sql):
        """One value from the host's database, or None."""
        q = ("import sqlite3,os;db=sqlite3.connect(os.path.expanduser('~/hive_data/hive.db'));"
             "r=db.execute('%s').fetchone();print(r[0] if r else None)" % sql)
        try:
            out = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", self.a.pi,
                                  "python3", "-c", '"%s"' % q],
                                 capture_output=True, text=True, timeout=40).stdout.strip()
        except (subprocess.SubprocessError, OSError) as e:
            self.event("could not ask the host: %s" % e)
            return None
        return None if out in ("", "None") else out

    def probe_cmd(self):
        v = self.a.values[self.cmds["tried"] % 2]
        if self.a.pi:
            # Always a CHANGE from what the node holds: re-writing its current
            # value would be "confirmed" by a routine report even if lost.
            cur = self.pi_query("SELECT value FROM readings WHERE node=%d AND slot=21 "
                                "ORDER BY ts DESC LIMIT 1" % self.a.write_node)
            if cur is not None and int(cur) == v:
                v = self.a.values[1] if v == self.a.values[0] else self.a.values[0]
        self.cmds["tried"] += 1
        self.await_reply("cmd", "set %d 21 %d" % (self.a.write_node, v), r"ACK set 21=%d" % v, value=v)

    def probe_refusal(self):
        self.refusal["tried"] += 1
        # A pattern nothing matches: only a wrong answer ends the wait early.
        self.await_reply("refusal", "status", r"(?!x)x", direct=True)

    def check_confirm(self):
        """The ACK only says the gateway sent the write; the node's own report
        is what says it arrived. Read that from the host's database."""
        if not self.pending_confirm or not self.a.pi:
            return
        v, t = self.pending_confirm
        out = self.pi_query("SELECT MIN(ts) FROM readings WHERE node=%d AND slot=21 AND value=%d "
                            "AND ts>=%d" % (self.a.write_node, v, int(t) - 2))
        if out:
            self.cmds["confirmed"] += 1
            self.pending_confirm = None
        elif now() - t > 1500:
            self.cmds["not_confirmed"] += 1
            self.event("write %d to node %d over LoRa never applied" % (v, self.a.write_node))
            self.pending_confirm = None

    def close_burst(self, older_than=0):
        b = self.burst
        if not b or now() - b["ts"] < older_than:
            return
        self.burst = None
        if len(b["nodes"]) >= b["up"] and (not b["chunks"] or b["chunks"] == set(range(1, max(b["chunks"]) + 1))):
            self.bursts["complete"] += 1
        else:
            self.bursts["partial"] += 1
            self.event("digest incomplete: health line says %d nodes, data lines named %d (chunks %s)" % (
                b["up"], len(b["nodes"]), sorted(b["chunks"]) or "none"))

    # --------------------------------------------------------------- report
    def summary(self, final=False):
        with self.lock:
            self.close_burst(older_than=0 if final else 90)
        el = (now() - self.t0) / 3600
        d = self.digests
        gaps = [b[0] - a[0] for a, b in zip(d, d[1:])]
        snrs = [x[1] for x in d if x[1] is not None]
        rssis = [x[2] for x in d if x[2] is not None]
        med = lambda xs: statistics.median(xs) if xs else None   # noqa: E731
        L = ["# LoRa soak %s" % ("— finished" if final else "— in progress"), "",
             "Started %s, ran %.2f h of %.1f h planned. Gateway %s, swarm channel %d." % (
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(self.t0)), el, self.a.hours,
                 self.a.gateway, self.a.channel), "",
             "## Link", "",
             "- packets from the gateway: %d (%d on the wrong channel)" % (self.packets, self.rx_other_channel),
             "- routine digests: %d; expected about %d" % (len(d), int(el * 3600 / DIGEST_EXPECTED_S)),
             "- digest gaps: median %s min, longest %s min, over 20 min: %d" % (
                 "%.1f" % (med(gaps) / 60) if gaps else "—",
                 "%.1f" % (max(gaps) / 60) if gaps else "—",
                 sum(1 for g in gaps if g > DIGEST_EXPECTED_S + 300)),
             "- signal: SNR min/median/max %s, RSSI min/median/max %s" % (
                 "%.1f / %.1f / %.1f" % (min(snrs), med(snrs), max(snrs)) if snrs else "—",
                 "%d / %d / %d" % (min(rssis), med(rssis), max(rssis)) if rssis else "—"),
             "- digests complete: %d, missing data lines: %d, data without a health line: %d" % (
                 self.bursts["complete"], self.bursts["partial"], self.bursts["headless"]),
             "- radio reconnects: %d; send errors: %d" % (self.reconnects, self.send_errors), "",
             "## Commands over LoRa", "",
             "- `status` round trips: %d tried, %d answered%s" % (
                 self.rtt["tried"], self.rtt["answered"],
                 ", median %.0f s" % med(self.rtt["s"]) if self.rtt["s"] else ""),
             "- slot writes: %d tried, %d acknowledged%s, %d confirmed applied, %d never applied" % (
                 self.cmds["tried"], self.cmds["acked"],
                 " (median %.0f s)" % med(self.cmds["s"]) if self.cmds["s"] else "",
                 self.cmds["confirmed"], self.cmds["not_confirmed"]),
             "- public-channel commands: %d sent, %d refused (no answer), %d wrongly answered" % (
                 self.refusal["tried"], self.refusal["refused"], self.refusal["wrongly_answered"]), "",
             "Notable events are in `events.log`; every packet in `packets.jsonl`."]
        with open(os.path.join(self.dir, "summary.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")

    # ------------------------------------------------------------------ run
    def smoke(self):
        pub.subscribe(self.on_receive, "meshtastic.receive")
        self.connect()
        self.probe_rtt()
        self.probe_cmd()
        for _ in range(12):
            if not self.pending_confirm:
                break
            time.sleep(15)
            self.check_confirm()
        self.probe_refusal()
        self.send("set %d 21 %d" % (self.a.write_node, self.a.restore))
        time.sleep(20)
        self.summary(final=True)
        self.close()
        print(open(os.path.join(self.dir, "summary.md"), encoding="utf-8").read())

    def run(self):
        pub.subscribe(self.on_receive, "meshtastic.receive")
        end = self.t0 + self.a.hours * 3600
        # Each probe has a wall-clock due time. Counting loop passes instead let
        # every probe drift later by however long the previous one blocked
        # (a reply wait is up to two minutes).
        due = {"rtt": self.t0 + 20 * 60, "cmd": self.t0 + 50 * 60,
               "refusal": self.t0 + 40 * 60, "summary": self.t0}
        every = {"rtt": 3600, "cmd": 3600, "refusal": 3600 * self.a.refusal_every, "summary": 600}
        probes = {"rtt": self.probe_rtt, "cmd": self.probe_cmd,
                  "refusal": self.probe_refusal, "summary": self.summary}
        connected_once = False
        while now() < end:
            try:
                if self.iface is None:
                    self.connect()
                    if connected_once:
                        self.reconnects += 1
                        self.event("radio reconnected")
                    connected_once = True
                self.check_confirm()
                for name in ("summary", "rtt", "cmd", "refusal"):
                    if now() >= due[name]:
                        while due[name] <= now():
                            due[name] += every[name]
                        probes[name]()
            except Exception as e:   # noqa: BLE001 -- a soak must outlive its own bugs
                self.event("harness error: %s: %s -- reconnecting" % (type(e).__name__, e))
                self.close()
            time.sleep(max(1, 60 - (now() - self.t0) % 60))
        if self.iface:
            self.send("set %d 21 %d" % (self.a.write_node, self.a.restore))   # leave it as found
        self.summary(final=True)
        self.close()
        open(os.path.join(self.dir, "DONE"), "w").write(time.ctime() + "\n")
        self.event("lora soak finished")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mac", required=True, help="this radio's USB MAC (how its port is found)")
    ap.add_argument("--gateway", required=True, help="the gateway's Meshtastic node id, e.g. !1a2b3c4d")
    ap.add_argument("--channel", type=int, default=1, help="swarm channel index (default 1)")
    ap.add_argument("--hours", type=float, default=16)
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), "hive_soak"))
    ap.add_argument("--write-node", type=int, default=3, help="range node whose deafen duration is toggled")
    ap.add_argument("--values", type=int, nargs=2, default=[602, 603],
                    help="the two values written (differ from the USB soak's, so each can tell its own)")
    ap.add_argument("--restore", type=int, default=600, help="value left behind at the end")
    ap.add_argument("--refusal-every", type=int, default=4, help="hours between public-channel refusal checks")
    ap.add_argument("--pi", help="user@host of the admin, to confirm writes in its database")
    ap.add_argument("--hop-limit", type=int, default=0,
                    help="hops for this radio's own sends (default 0: the test is of the direct "
                         "link, and nothing is rebroadcast across the public mesh)")
    ap.add_argument("--smoke", action="store_true",
                    help="fire each probe once now, wait for the write to be confirmed, and stop")
    args = ap.parse_args()
    soak = LoraSoak(args)
    if args.smoke:
        soak.smoke()
    else:
        soak.run()


if __name__ == "__main__":
    main()
