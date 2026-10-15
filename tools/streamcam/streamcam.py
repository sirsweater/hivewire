#!/usr/bin/env python3
"""streamcam -- a plant's camera to Twitch, with the hive's overlay drawn on it.

Runs on whatever machine has ffmpeg and the bandwidth (not the hive's Pi: the
Pi holds the gateway, and video encoding is the wrong neighbour for it). It:

  - pulls the camera's RTSP stream (a Luckfox Pico's stock image serves one),
  - draws the overlay text from the admin's Stream page (/api/stream/overlay,
    fetched every 2 s with the overlay key): soil, light, who watered it, how
    to water it,
  - sends it to Twitch (RTMP) or, until a stream key is given, nowhere -- the
    preview mode, which still writes the snapshot,
  - writes a snapshot every few seconds and serves it with a status page on
    a small HTTP port, so the admin can show the plant's camera next to its
    readings,
  - shows a "camera offline" slate when the camera stops answering, and goes
    back to the camera on its own when it returns.

The Twitch stream key is read from a file only its owner can read
(stream_key_file, mode 600). It is never logged; it does appear in ffmpeg's
command line, which other users of the same machine could see with `ps`.

    python3 streamcam.py --config streamcam.json
    python3 streamcam.py --config streamcam.json --check   # print the plan, run nothing

stdlib only. ffmpeg 6 or later on the PATH (or "ffmpeg" in the config).
SPDX-License-Identifier: Apache-2.0
"""
import argparse
import http.server
import json
import os
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

DEFAULTS = {
    "camera": "",                    # rtsp://<camera>/live/0
    "overlay_url": "",               # http://<hive>:8080/api/stream/overlay?key=...
    "stream_key_file": "",           # holds the Twitch stream key; empty = preview only
    "ingest": "rtmp://live.twitch.tv/app/",
    "output": "",                    # overrides ingest+key (e.g. a local RTMP server for a test)
    "width": 1280, "height": 720, "fps": 30,
    "bitrate_k": 3000,               # Twitch's guidance for 720p30: 3000-4500
    "encoder": "auto",               # auto | h264_nvenc | libx264
    "font": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "transform": "",                 # extra ffmpeg filters for the camera, e.g. "transpose=1" or "hflip,vflip"
    "title": "Water the plant",
    "workdir": "~/.cache/streamcam",
    "listen": "0.0.0.0", "http_port": 8787,
    "snapshot_every_s": 5,
    "ffmpeg": "ffmpeg",
}
OVERLAY_EVERY_S = 2
CAMERA_RETRY_S = 15
# A camera that vanishes mid-stream doesn't always make ffmpeg exit: with the
# RTSP session still open and no frames, it waited forever (seen against a
# test server whose publisher was killed). No new snapshot for this long
# means no frames are going out either: restart it.
STALL_S = 25


def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_config(path):
    c = dict(DEFAULTS)
    with open(path) as f:
        c.update(json.load(f))
    c["workdir"] = os.path.expanduser(c["workdir"])
    os.makedirs(c["workdir"], exist_ok=True)
    return c


# ---------------------------------------------------------------------------
# The overlay: the admin's JSON -> two small text files ffmpeg re-reads
# ---------------------------------------------------------------------------
def ago(s):
    s = int(s)
    return "%ds" % s if s < 60 else "%dm" % (s // 60) if s < 3600 else "%dh" % (s // 3600)


def overlay_text(o, title):
    """(top line, bottom lines) from /api/stream/overlay. Plain ASCII: the
    stream's font has no emoji, and logins are already [a-z0-9_]."""
    p = o.get("plant") or {}
    top = [title]
    if p.get("soil") is not None:
        top.append("soil %d%%" % p["soil"])
    if p.get("light") is not None:
        top.append("light on" if p["light"] else
                   "lights out%s" % (" until %s" % p["lights_on_at"] if p.get("lights_on_at") else ""))
    if p.get("reservoir_empty"):
        top.append("RESERVOIR EMPTY")
    st = o.get("status") or ""
    if st.startswith("watering"):
        top.append(st.split(" (")[0])
    elif st == "paused":
        top.append("free water paused")
    recent = o.get("recent") or []
    last = "  ".join("%s %d ml (%s ago)" % (r["user"], r["ml"], ago(r["ago_s"])) for r in recent[:4])
    bottom = ["Water me: %s" % (o.get("how") or "")]
    line2 = "Today: %d waterings, %d ml" % (o.get("waterings_today", 0), o.get("ml_today", 0))
    if o.get("queue"):
        line2 += "  -  %d waiting" % o["queue"]
    if o.get("next_free_s"):
        line2 += "  -  next free water in %s" % ago(o["next_free_s"])
    bottom.append(line2)
    if last:
        bottom.append("Last: " + last)
    return "   |   ".join(top), "\n".join(bottom)


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)        # ffmpeg's reload never sees half a file


class OverlayFetcher(threading.Thread):
    daemon = True

    def __init__(self, cfg, status):
        super().__init__()
        self.cfg, self.status = cfg, status
        self.top = os.path.join(cfg["workdir"], "overlay_top.txt")
        self.bottom = os.path.join(cfg["workdir"], "overlay_bottom.txt")
        write_atomic(self.top, cfg["title"])
        write_atomic(self.bottom, "")

    def run(self):
        while True:
            if self.cfg["overlay_url"]:
                try:
                    with urllib.request.urlopen(self.cfg["overlay_url"], timeout=5) as r:
                        o = json.load(r)
                    t, b = overlay_text(o, self.cfg["title"])
                    write_atomic(self.top, t)
                    write_atomic(self.bottom, b)
                    self.status["overlay"] = "ok"
                except Exception as e:      # the hive being down must not stop the video
                    self.status["overlay"] = "%s: %s" % (type(e).__name__, e)
            time.sleep(OVERLAY_EVERY_S)


# ---------------------------------------------------------------------------
# The camera and ffmpeg
# ---------------------------------------------------------------------------
def camera_answers(url, timeout=4):
    """An RTSP DESCRIBE for the stream itself: the camera is up AND has that
    stream. OPTIONS isn't enough -- a server with the stream missing answers it
    with 200 and then 404s ffmpeg, which kept restarting instead of showing the
    slate. 401 counts as up: the credentials are in the URL for ffmpeg."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("rtsp", "rtsps"):
        return True, "not RTSP: left to ffmpeg"
    bare = u._replace(netloc=u.hostname + (":%d" % u.port if u.port else "")).geturl()   # no user:pass on the wire
    try:
        with socket.create_connection((u.hostname, u.port or 554), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(("DESCRIBE %s RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n"
                       "User-Agent: streamcam\r\n\r\n" % bare).encode())
            head = s.recv(512).decode("latin-1", "replace").split("\r\n")[0]
        parts = head.split()
        return len(parts) >= 2 and parts[0].startswith("RTSP/") and parts[1] in ("200", "401"), head[:60]
    except OSError as e:
        return False, str(e)


def pick_encoder(cfg):
    if cfg["encoder"] != "auto":
        return cfg["encoder"]
    try:
        subprocess.run([cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=s=256x144:r=30", "-t", "0.5", "-c:v", "h264_nvenc", "-f", "null", "-"],
                       check=True, capture_output=True, timeout=30)
        return "h264_nvenc"         # a GPU encoder leaves the CPU for decoding the camera
    except (subprocess.SubprocessError, OSError):
        return "libx264"


def stream_target(cfg):
    """(url for ffmpeg, what to call it in logs). The key never reaches a log."""
    if cfg["output"]:
        return cfg["output"], cfg["output"]
    kf = os.path.expanduser(cfg["stream_key_file"] or "")
    if not kf:
        return None, "preview only (no stream_key_file)"
    st = os.stat(kf)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit("%s is readable by other users: chmod 600 it" % kf)
    key = open(kf).read().strip()
    if not key:
        raise SystemExit("%s is empty" % kf)
    return cfg["ingest"].rstrip("/") + "/" + key, cfg["ingest"].rstrip("/") + "/<stream key>"


def ff_escape(s):
    """A value inside an ffmpeg filter option: \\ ' : and , are special."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace(",", "\\,")


def ffmpeg_cmd(cfg, mode, encoder, target):
    W, H, F = cfg["width"], cfg["height"], cfg["fps"]
    wd = cfg["workdir"]
    font = ff_escape(cfg["font"])
    cmd = [cfg["ffmpeg"], "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if mode == "camera":
        if cfg["camera"].startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "5000000"]
        cmd += ["-fflags", "+genpts", "-i", cfg["camera"]]
    else:
        cmd += ["-f", "lavfi", "-i", "color=c=0x16301e:s=%dx%d:r=%d" % (W, H, F)]
    # Twitch wants an audio track; a plant is quiet.
    cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    v = "[0:v]"
    chain = []
    if mode == "camera" and cfg["transform"]:
        chain.append(cfg["transform"])
    chain += ["scale=%d:%d:force_original_aspect_ratio=decrease" % (W, H),
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (W, H), "fps=%d" % F, "format=yuv420p"]
    fs_top, fs_bot = max(16, H // 30), max(14, H // 36)
    chain.append("drawbox=x=0:y=0:w=iw:h=%d:color=black@0.55:t=fill" % (fs_top * 2))
    chain.append("drawtext=fontfile='%s':textfile='%s':reload=1:expansion=none:fontcolor=white:fontsize=%d:x=%d:y=%d"
                 % (font, ff_escape(os.path.join(wd, "overlay_top.txt")), fs_top, fs_top // 2, fs_top // 2))
    chain.append("drawbox=x=0:y=ih-%d:w=iw:h=%d:color=black@0.55:t=fill" % (fs_bot * 5, fs_bot * 5))
    chain.append("drawtext=fontfile='%s':textfile='%s':reload=1:expansion=none:fontcolor=white:fontsize=%d"
                 ":line_spacing=%d:x=%d:y=h-%d" % (font, ff_escape(os.path.join(wd, "overlay_bottom.txt")), fs_bot,
                                                    fs_bot // 3, fs_bot // 2, fs_bot * 5 - fs_bot // 2))
    if mode != "camera":
        chain.append("drawtext=fontfile='%s':text='camera offline - back soon':expansion=none:fontcolor=white"
                     ":fontsize=%d:x=(w-tw)/2:y=(h-th)/2" % (font, H // 14))
    graph = "%s%s,split=2[out][snap0];[snap0]fps=1/%d,scale=640:-2[snap]" % (
        v, ",".join(chain), max(1, int(cfg["snapshot_every_s"])))
    cmd += ["-filter_complex", graph]
    if target:
        cmd += ["-map", "[out]", "-map", "1:a"]
        if encoder == "h264_nvenc":
            cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "cbr"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]
        k = cfg["bitrate_k"]
        # A keyframe every 2 s, constant bitrate: what Twitch asks of a stream.
        cmd += ["-b:v", "%dk" % k, "-maxrate", "%dk" % k, "-bufsize", "%dk" % (2 * k),
                "-g", str(2 * F), "-keyint_min", str(2 * F), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-f", "flv", target]
    else:
        cmd += ["-map", "[out]", "-f", "null", "-"]
    cmd += ["-map", "[snap]", "-update", "1", "-q:v", "5", "-y", os.path.join(wd, "snapshot.jpg")]
    return cmd


class Runner(threading.Thread):
    """Keeps ffmpeg running: the camera when it answers, the slate when it
    doesn't, and back to the camera when it returns."""
    daemon = True

    def __init__(self, cfg, status):
        super().__init__()
        self.cfg, self.status = cfg, status
        self.proc = None
        self.stopping = False
        self.encoder = pick_encoder(cfg)
        self.target, self.target_name = stream_target(cfg)
        status.update(encoder=self.encoder, output=self.target_name, restarts=0)

    def run(self):
        fails = 0
        while not self.stopping:
            up, why = camera_answers(self.cfg["camera"]) if self.cfg["camera"] else (False, "no camera configured")
            mode = "camera" if up else "slate"
            self.status.update(mode=mode, camera=why, since=time.time())
            log("ffmpeg: %s -> %s (%s)" % (mode, self.target_name, why))
            self.proc = subprocess.Popen(ffmpeg_cmd(self.cfg, mode, self.encoder, self.target),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            started = checked = time.time()
            threading.Thread(target=self._pump_stderr, args=(self.proc,), daemon=True).start()
            snap = os.path.join(self.cfg["workdir"], "snapshot.jpg")
            stall = max(STALL_S, 3 * int(self.cfg["snapshot_every_s"]))
            while self.proc.poll() is None and not self.stopping:
                time.sleep(1)
                t = time.time()
                newest = max(started, os.path.getmtime(snap) if os.path.exists(snap) else 0)
                if t - newest > stall + (10 if newest == started else 0):
                    log("no frames for %.0f s: restarting ffmpeg" % (t - newest))
                    self.status["camera"] = "stalled"
                    self.proc.terminate()
                    break
                if mode == "slate" and self.cfg["camera"] and t - checked >= CAMERA_RETRY_S:
                    checked = t
                    if camera_answers(self.cfg["camera"])[0]:
                        log("camera is back")
                        self.proc.terminate()
            try:
                rc = self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                rc = self.proc.wait()
            if self.stopping:
                break
            self.status["restarts"] += 1
            ran = time.time() - self.status["since"]
            fails = fails + 1 if ran < 30 else 0
            log("ffmpeg exited (%s) after %.0f s" % (rc, ran))
            time.sleep(min(60, 2 ** min(fails, 6)))

    def _pump_stderr(self, proc):
        for line in proc.stderr:
            line = line.strip()
            if line:
                if self.target and self.target != self.target_name:
                    line = line.replace(self.target, self.target_name)
                self.status["last_ffmpeg"] = line[-200:]
                log("ffmpeg: " + line[-200:])

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------------------
# Snapshot and status for the admin page
# ---------------------------------------------------------------------------
def serve(cfg, status):
    snap = os.path.join(cfg["workdir"], "snapshot.jpg")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/snapshot.jpg" and os.path.exists(snap):
                body, ctype = open(snap, "rb").read(), "image/jpeg"
            elif path == "/status.json":
                st = dict(status, snapshot_age_s=round(time.time() - os.path.getmtime(snap)) if os.path.exists(snap) else None)
                body, ctype = json.dumps(st).encode(), "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # The admin page on another machine shows these.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = S((cfg["listen"], int(cfg["http_port"])), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("snapshot and status on http://%s:%d/" % (cfg["listen"], cfg["http_port"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--check", action="store_true", help="print the plan and the ffmpeg command, run nothing")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if not shutil.which(cfg["ffmpeg"]):
        raise SystemExit("ffmpeg not found (%s)" % cfg["ffmpeg"])
    status = {"mode": "starting", "overlay": "not fetched yet"}
    if a.check:
        target, name = stream_target(cfg)
        enc = pick_encoder(cfg)
        print("camera:  %s -> %s" % (cfg["camera"] or "(none)", camera_answers(cfg["camera"]) if cfg["camera"] else "-"))
        print("encoder: %s\noutput:  %s" % (enc, name))
        cmd = ffmpeg_cmd(cfg, "camera", enc, target)
        print("ffmpeg:  " + " ".join(name if c == target else c for c in cmd))
        return
    OverlayFetcher(cfg, status).start()
    serve(cfg, status)
    runner = Runner(cfg, status)
    signal.signal(signal.SIGTERM, lambda *_: (runner.stop(), sys.exit(0)))
    runner.start()
    try:
        while runner.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        runner.stop()


if __name__ == "__main__":
    main()
