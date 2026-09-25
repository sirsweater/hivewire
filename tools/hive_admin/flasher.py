"""Flash an ESP32-C6 plugged into this machine's USB, and give it a node number.

One generic image per firmware family (SoilNode, RangeNode), built with node id
0. A board running it reports itself over USB and waits; this module writes the
image, then tells the board its number (`setid <n>`, see HivewireProvision.h)
and reads back what the board says about its radio and sensors.

Safety rules this module enforces rather than trusting the caller:
  - A board is identified by its MAC, never by COM/ttyACM number. The MAC in
    the USB serial number is checked against what esptool reads from the chip
    before anything is written.
  - The hive gateway's own port is never offered and never written.
  - Only one flash runs at a time.

Used by hive_admin.py: as a tab on the Pi, and as the whole page in
--flash-only mode on a PC.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import serial
import serial.tools.list_ports

ESPRESSIF_VID = 0x303A
MAC_RE = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.I)


def norm_mac(s):
    m = MAC_RE.search(s or "")
    return m.group(1).lower() if m else None


def find_esptool():
    """A command prefix that runs esptool, or None.

    Prefers the Python module (what `pip install esptool` gives on the Pi), then
    the copy the Arduino IDE ships on Windows, then anything on PATH.
    """
    try:
        import esptool  # noqa: F401
        return [sys.executable, "-m", "esptool"]
    except ImportError:
        pass
    home = os.path.expanduser("~")
    hits = sorted(glob.glob(os.path.join(
        home, "AppData", "Local", "Arduino15", "packages", "esp32", "tools",
        "esptool_py", "*", "esptool.exe")))
    if hits:
        return [hits[-1]]
    for name in ("esptool", "esptool.py", "esptool.exe"):
        p = shutil.which(name)
        if p:
            return [p]
    return None


class Flasher:
    def __init__(self, image_dir, gateway_port=None, store=None, known_ids=None):
        self.image_dir = image_dir
        self.gateway_mac = norm_mac(os.path.basename(gateway_port or ""))
        self.store = store                 # optional: events land in the admin's log
        self.known_ids = known_ids or (lambda: [])
        self.job = None
        self._lock = threading.Lock()

    # --- what is plugged in ------------------------------------------------
    def ports(self):
        out = []
        for p in serial.tools.list_ports.comports():
            if p.vid != ESPRESSIF_VID:
                continue
            mac = norm_mac(p.serial_number)
            if not mac:
                continue
            gw = self.gateway_mac is not None and mac == self.gateway_mac
            busy = self.job and self.job["state"] == "running" and self.job["mac"] == mac
            out.append({"device": p.device, "mac": mac, "gateway": gw, "busy": bool(busy),
                        "description": p.description or ""})
        out.sort(key=lambda x: x["mac"])
        return out

    # --- which images can be flashed -----------------------------------------
    def images(self):
        """Complete images (bootloader + partitions + app) for a blank board.

        A merged image, not the plain app image the swarm push uses: a board
        straight out of the bag has no bootloader to boot an app with.
        """
        out = []
        for path in sorted(glob.glob(os.path.join(self.image_dir, "*.merged.bin"))):
            name = os.path.basename(path)
            meta = {}
            side = path[: -len(".merged.bin")] + ".json"
            if os.path.exists(side):
                try:
                    meta = json.load(open(side, encoding="utf-8"))
                except ValueError:
                    meta = {}
            family = meta.get("family") or name.split("-")[0].split(".")[0]
            st = os.stat(path)
            out.append({"name": name, "family": family, "size": st.st_size,
                        "mtime": int(st.st_mtime), "built": meta.get("built"),
                        "commit": meta.get("commit")})
        return out

    def next_id(self):
        used = set(int(i) for i in self.known_ids() if str(i).isdigit())
        # On a PC there is no swarm to ask, so any guess could be a number
        # already in use. Suggest nothing rather than something wrong.
        if not used:
            return None
        n = 2
        while n in used:
            n += 1
        return n if n <= 254 else None

    # --- the job -------------------------------------------------------------
    def start(self, device, image, node_id, erase=False):
        with self._lock:
            if self.job and self.job["state"] == "running":
                raise RuntimeError("a flash is already running")
            port = next((p for p in self.ports() if p["device"] == device), None)
            if not port:
                raise ValueError("that board is no longer plugged in")
            if port["gateway"]:
                raise ValueError("that is the hive gateway; it is never flashed from here")
            img = next((i for i in self.images() if i["name"] == image), None)
            if not img:
                raise ValueError("unknown image")
            nid = None
            if node_id not in (None, ""):
                nid = int(node_id)
                if not 1 <= nid <= 254:
                    raise ValueError("node number must be 1-254")
            self.job = {"state": "running", "device": device, "mac": port["mac"],
                        "image": image, "family": img["family"], "node_id": nid,
                        "erase": bool(erase),
                        "started": time.time(), "step": "starting", "lines": [],
                        "result": {}}
            threading.Thread(target=self._run, daemon=True).start()
            return self.job

    def _log(self, msg):
        self.job["lines"].append(time.strftime("%H:%M:%S ") + msg)
        del self.job["lines"][:-300]

    def _step(self, name):
        self.job["step"] = name
        self._log("== " + name)

    def _fail(self, msg):
        self._log("FAILED: " + msg)
        self.job["state"] = "failed"
        self.job["error"] = msg
        if self.store:
            self.store.event("flash", "%s %s: failed, %s" % (self.job["mac"], self.job["image"], msg))

    def _esptool(self, args, timeout):
        cmd = self.esptool + ["--chip", "esp32c6", "--port", self.job["device"]] + args
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace")
        out = []
        t0 = time.time()
        for line in p.stdout:
            line = line.rstrip()
            out.append(line)
            if not line:
                continue
            # Progress lines arrive by the hundred; keep only the latest one
            # on the job (the page shows it) and log everything else.
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
            if "Writing at" in line and m:
                self.job["progress"] = float(m.group(1))
            else:
                self._log(line)
            if time.time() - t0 > timeout:
                p.kill()
                raise RuntimeError("esptool took too long")
        return p.wait(), "\n".join(out)

    def _run(self):
        try:
            self._flow()
        except Exception as e:                 # noqa: BLE001 -- report, never die silently
            self._fail("%s: %s" % (type(e).__name__, e))

    def _flow(self):
        job = self.job
        self.esptool = find_esptool()
        if not self.esptool:
            return self._fail("esptool is not installed (pip install esptool)")

        self._step("checking the board is the one plugged in")
        rc, out = self._esptool(["read-mac"], 60)
        base = re.search(r"BASE MAC:\s*([0-9a-f:]{17})", out, re.I) or \
            re.search(r"MAC:\s*([0-9a-f:]{17})", out, re.I)
        chip_mac = base.group(1).lower() if base else None
        if rc != 0 or not chip_mac:
            return self._fail("could not talk to the board (is it an ESP32-C6?)")
        if chip_mac != job["mac"]:
            return self._fail("board reports MAC %s, expected %s; not flashing" % (chip_mac, job["mac"]))
        self._log("board %s confirmed" % chip_mac)

        if job["erase"]:
            # A board keeps its node number in NVS, which writing an image
            # does not touch. Wiping makes a recycled board start as new.
            self._step("wiping the board")
            rc, out = self._esptool(["erase-flash"], 120)
            if rc != 0:
                return self._fail("wipe failed")

        self._step("writing " + job["image"])
        path = os.path.join(self.image_dir, job["image"])
        rc, out = self._esptool(["--baud", "460800", "write-flash", "0x0", path], 300)
        if rc != 0 or "Hash of data verified" not in out:
            return self._fail("write did not verify")
        self._log("written and verified")

        # Every boot prints its HWID line, then scans (~3 s) and prints
        # HWRADIO. Wait for the radio line; the HWID line is already in `seen`.
        self._step("waiting for the board to report")
        time.sleep(1.5)
        hello = self._wait_serial(lambda s: s.startswith("HWRADIO"), 30)
        if not hello:
            return self._fail("board did not report after flashing (old firmware without USB setup?)")
        info = self._last_hwid(hello["seen"])
        if not info:
            # The ID line comes out a few hundred ms after reset, often before
            # this end has the port open again. Ask for it instead.
            asked = self._command("id?", lambda s: s.startswith("HWID family"), 10)
            info = self._last_hwid(asked["seen"]) if asked else None
        if not info:
            return self._fail("board reported a radio result but no identity")
        job["result"].update(info)
        job["result"].update(self._parse_radio(hello["seen"]))

        want = job["node_id"]
        if want and str(info.get("id")) != str(want):
            self._step("setting node number %d" % want)
            back = None
            for attempt in range(3):
                ack = self._command("setid %d" % want, lambda s: s.startswith(("HWID-SET", "HWID-ERR")), 8)
                if ack and ack["line"].startswith("HWID-SET"):
                    back = self._wait_serial(lambda s: s.startswith("HWRADIO"), 40)
                    break
                if ack and ack["line"].startswith("HWID-ERR"):
                    return self._fail("board refused: " + ack["line"])
                self._log("no acknowledgement, asking again")
            back_id = self._last_hwid(back["seen"]) if back else None
            if back and not back_id:
                asked = self._command("id?", lambda s: s.startswith("HWID family"), 10)
                back_id = self._last_hwid(asked["seen"]) if asked else None
            if not back_id or str(back_id.get("id")) != str(want):
                return self._fail("board did not come back as node %d" % want)
            job["result"].update(back_id)
            job["result"].update(self._parse_radio(back["seen"]))
        elif not want and info.get("id") == "unassigned":
            self._log("left unassigned: it will not join the swarm until it has a number")

        if job["result"].get("id") not in (None, "unassigned"):
            # A SoilNode prints its first reading line ~30 s after boot; a
            # RangeNode has no sensors and only announces itself.
            self._step("checking sensors")
            if (job["result"].get("family") or job["family"]) == "SoilNode":
                got = self._wait_serial(lambda s: s.startswith("t="), 45)
            else:
                got = self._wait_serial(lambda s: " up, boot" in s, 15)
            if got:
                for s in got["seen"]:
                    if s.startswith("t="):
                        job["result"]["sensors"] = s
            self._log(job["result"].get("sensors") or "no sensor line (RangeNode has none)")

        r = job["result"]
        radio = r.get("radio_networks")
        if radio == 0:
            job["warning"] = ("The radio heard no Wi-Fi networks at all. A working C6 near a "
                              "router hears several: this board's radio or antenna is faulty.")
            self._log("WARNING: " + job["warning"])
        job["state"] = "done"
        self._step("done")
        if self.store:
            self.store.event("flash", "%s %s -> %s id %s, radio %s networks" % (
                job["mac"], job["image"], r.get("family"), r.get("id"), radio))

    # --- serial conversation with the board ----------------------------------
    def _open(self):
        for _ in range(20):
            try:
                # Open WITHOUT raising DTR/RTS: on the C6's USB serial those
                # lines are wired to reset, and pyserial raises both by default
                # -- which rebooted the board and swallowed the command.
                s = serial.Serial()
                s.port, s.baudrate, s.timeout = self.job["device"], 115200, 0.3
                s.dtr = False
                s.rts = False
                s.open()
                return s
            except (serial.SerialException, OSError):
                time.sleep(0.5)             # the port vanishes while the board reboots
        raise RuntimeError("could not open " + self.job["device"])

    def _wait_serial(self, pred, timeout, reopen=False, send=None):
        seen = []
        deadline = time.time() + timeout
        s = None
        buf = b""
        try:
            while time.time() < deadline:
                if s is None:
                    try:
                        s = self._open()
                        if send:
                            s.write((send + "\n").encode())
                            send = None
                    except RuntimeError:
                        continue
                try:
                    chunk = s.read(512)
                except (serial.SerialException, OSError):
                    s.close()
                    s = None                 # rebooting: reopen and keep listening
                    continue
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    seen.append(line)
                    if line.startswith(("HW", "t=")) or " up, boot" in line:
                        self._log("board: " + line[:160])
                    if pred(line):
                        return {"line": line, "seen": seen}
                if s is not None and not chunk and not s.is_open:
                    s = None
        finally:
            if s is not None:
                s.close()
        return None

    def _command(self, cmd, pred, timeout):
        return self._wait_serial(pred, timeout, send=cmd)

    @classmethod
    def _last_hwid(cls, lines):
        for s in reversed(lines or []):
            if s.startswith("HWID family"):
                return cls._parse_hwid(s)
        return None

    @staticmethod
    def _parse_hwid(line):
        d = dict(re.findall(r"(\w+)=(\S+)", line))
        return {"family": d.get("family"), "id": d.get("id"), "reported_mac": d.get("mac")}

    @staticmethod
    def _parse_radio(lines):
        for s in reversed(lines):
            if s.startswith("HWRADIO"):
                d = dict(re.findall(r"(\w+)=(\S+)", s))
                try:
                    n = int(d.get("networks", "0"))
                except ValueError:
                    n = 0
                best = d.get("best")
                return {"radio_networks": n, "radio_best": None if best in (None, "none") else int(best)}
        return {}
