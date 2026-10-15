#!/usr/bin/env python3
"""Offline test for streamcam.py: the overlay text, the stream key's handling,
the ffmpeg command it builds, and the camera check against a stand-in RTSP
server. Runs no ffmpeg.

    python3 tools/streamcam/test_streamcam.py
"""
import os
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import streamcam as S  # noqa: E402

failed = 0


def check(cond, msg, detail=None):
    global failed
    if cond:
        print("ok: " + msg)
    else:
        failed += 1
        print("FAIL: %s %s" % (msg, "" if detail is None else detail))


# --- the overlay ----------------------------------------------------------------
o = {"status": "watering: 5 ml for bob (waiting for the node's report)", "waterings_today": 3, "ml_today": 25,
     "queue": 2, "next_free_s": 75, "how": "type !water in chat | Bits: 50 ml per 100",
     "recent": [{"user": "bob", "ml": 5, "src": "chat", "ago_s": 30}, {"user": "amy", "ml": 50, "src": "bits", "ago_s": 4000}],
     "plant": {"soil": 54, "light": False, "lights_on_at": "06:00", "reservoir_empty": True}}
top, bottom = S.overlay_text(o, "The strawberry")
check(top == "The strawberry   |   soil 54%   |   lights out until 06:00   |   RESERVOIR EMPTY   |   watering: 5 ml for bob",
      "top line: soil, light, an empty reservoir, the dose under way", top)
check(bottom.splitlines() == ["Water me: type !water in chat | Bits: 50 ml per 100",
                              "Today: 3 waterings, 25 ml  -  2 waiting  -  next free water in 1m",
                              "Last: bob 5 ml (30s ago)  amy 50 ml (1h ago)"], "bottom lines", bottom)
top, bottom = S.overlay_text({"status": "paused", "plant": {}}, "T")
check(top == "T   |   free water paused" and len(bottom.splitlines()) == 2, "paused, nothing yet", (top, bottom))
check(all(ord(c) < 128 for c in top + bottom), "plain ASCII: the stream's font has no emoji")

# --- the stream key ------------------------------------------------------------
d = tempfile.mkdtemp()
kf = os.path.join(d, "key")
with open(kf, "w") as f:
    f.write("live_123_secret\n")
os.chmod(kf, 0o644)
cfg = dict(S.DEFAULTS, stream_key_file=kf, workdir=d)
try:
    S.stream_target(cfg)
    check(False, "a key file others can read is refused")
except SystemExit as e:
    check("chmod 600" in str(e), "a key file others can read is refused, saying how to fix it")
os.chmod(kf, 0o600)
url, name = S.stream_target(cfg)
check(url == "rtmp://live.twitch.tv/app/live_123_secret" and "secret" not in name and name.endswith("<stream key>"),
      "the key goes in the URL ffmpeg gets, never in the name that is logged", name)
check(S.stream_target(dict(cfg, stream_key_file=""))[0] is None, "no key file: preview only")
check(S.stream_target(dict(cfg, output="rtmp://127.0.0.1/x"))[0] == "rtmp://127.0.0.1/x", "an output override wins")

# --- the ffmpeg command ---------------------------------------------------------
cmd = S.ffmpeg_cmd(dict(cfg, camera="rtsp://cam/live/0", transform="transpose=1"), "camera", "h264_nvenc", url)
j = " ".join(cmd)
check(["-rtsp_transport", "tcp"] == cmd[cmd.index("-rtsp_transport"):cmd.index("-rtsp_transport") + 2],
      "RTSP over TCP (UDP drops frames on a busy network)")
check("-g 60" in j and "-keyint_min 60" in j and "-b:v 3000k" in j and "-maxrate 3000k" in j,
      "a keyframe every 2 s at a constant 3000 kb/s, as Twitch asks")
check("anullsrc" in j and "-c:a aac" in j, "a silent audio track (Twitch wants one)")
check("transpose=1,scale=1280:720" in j, "the camera's turn is applied before scaling")
check("expansion=none" in j and "reload=1" in j, "overlay files are re-read and never expanded as % sequences")
check("camera offline" not in j, "no slate text over the camera")
check(cmd[-1].endswith("snapshot.jpg") and "-update" in cmd, "a snapshot is written alongside")
slate = " ".join(S.ffmpeg_cmd(cfg, "slate", "libx264", None))
check("camera offline" in slate and "color=c=" in slate and "-f null -" in slate and "-c:v" not in slate,
      "the slate in preview: drawn, not encoded or sent")
check(S.ff_escape("C:\\a's,b") == "C\\:\\\\a\\'s\\,b", "filter values are escaped")


# --- the camera check against a stand-in RTSP server ---------------------------
def rtsp_server(reply):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    got = []

    def run():
        while True:
            c, _ = srv.accept()
            got.append(c.recv(1024).decode())
            c.sendall(reply)
            c.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], got


port, got = rtsp_server(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
ok, why = S.camera_answers("rtsp://user:pw@127.0.0.1:%d/live/0" % port)
check(ok and got[0].startswith("DESCRIBE rtsp://127.0.0.1:%d/live/0 RTSP/1.0" % port),
      "DESCRIBE for the stream itself, with no credentials on the wire", got[:1])
port, _ = rtsp_server(b"RTSP/1.0 404 Not Found\r\n\r\n")
check(not S.camera_answers("rtsp://127.0.0.1:%d/live/0" % port)[0], "a server without the stream counts as down")
port, _ = rtsp_server(b"RTSP/1.0 401 Unauthorized\r\n\r\n")
check(S.camera_answers("rtsp://127.0.0.1:%d/live/0" % port)[0], "401 counts as up (ffmpeg has the credentials)")
s = socket.socket()
s.bind(("127.0.0.1", 0))
free = s.getsockname()[1]
s.close()
check(not S.camera_answers("rtsp://127.0.0.1:%d/x" % free)[0], "nothing listening: down")

print("\n%s" % ("all passed" if not failed else "%d FAILED" % failed))
sys.exit(1 if failed else 0)
