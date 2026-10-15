#!/usr/bin/env python3
"""Offline test for streamplant.py and twitch.py: the viewer-watering rules and
queue, a WebSocket and EventSub session against a local stand-in for Twitch,
and the device-code login against a fake token endpoint. No network, no hive.

    python tools/hive_admin/test_stream.py
"""
import base64
import hashlib
import json
import os
import socket
import stat
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import streamplant as SP  # noqa: E402
import twitch as TW  # noqa: E402

failed = 0


def check(cond, msg, detail=None):
    global failed
    if cond:
        print("ok: " + msg)
    else:
        failed += 1
        print("FAIL: %s %s" % (msg, "" if detail is None else detail))


TMP = tempfile.mkdtemp()


# ---------------------------------------------------------------------------
# The rules and the queue
# ---------------------------------------------------------------------------
class World:
    """A pump node as the poller's dump shows it, and everything the queue calls."""

    def __init__(self):
        self.t = 1_800_000_000.0
        self.settings = dict(SP.DEFAULTS, enabled=True, node=15)
        self.slots = {40: 0, 43: 250, 44: 1500, 45: 0, 46: 0, 47: 1}
        self.age = 5
        self.sent, self.said, self.redeemed, self.events, self.wakes = [], [], [], [], 0
        self.ack = True
        self.pump_works = True

    def snapshot(self):
        return {"time": self.t, "nodes": {15: {"age": self.age, "slots": dict(self.slots)}}}

    def send(self, node, slot, v):
        self.sent.append((node, slot, v))
        if self.ack and self.pump_works:
            self.slots[45] += v            # the node pumps and reports it
            self.slots[46] = 2
            self.age = -1                  # heard after the send (made fresh by the next step)
        return self.ack

    def step(self, secs=3):
        self.t += secs
        if self.age < 0:
            self.age = 0
        self.age += secs if self.age > 0 else 0

    def make(self, name="q.json"):
        return SP.ViewerWater(os.path.join(TMP, name), lambda: self.settings, self.snapshot, lambda: 300,
                              self.send, self.said.append, lambda r, st: self.redeemed.append((r["id"], st)),
                              lambda k, txt: self.events.append(txt), self.wake, clock=lambda: self.t)

    def wake(self):
        self.wakes += 1


w = World()
q = w.make()

ok, msg = q.request("chat", "Alice", "Alice")
check(ok and msg is None and len(q.queue) == 1 and q.queue[0]["ml"] == 5, "a chat !water queues the chat amount")
ok, msg = q.request("chat", "bob", "bob")
check(not ok and "refilling" in msg and "2 min" in msg, "a second one inside the shared cooldown is told to wait",
      msg)
ok, msg = q.request("chat", "bob", "bob")
check(not ok and msg is None, "...and told only once per wait (spamming gets one answer)")
q.tick()
check(w.sent == [(15, 40, 5)] and q.queue[0]["status"] == "sent", "the dispatcher sends the dose", w.sent)
w.step()
q.tick()
check(not q.queue and q.history[-1]["status"] == "given" and any("watered the plant: 5 ml" in s for s in w.said),
      "a rise in the node's pumped-24-h count confirms it, and chat is told", w.said)
check(w.wakes == 1, "the poller is asked for a fresh dump after a send")

w.t += 130
ok, _ = q.request("chat", "alice", "alice")
check(not ok, "the per-viewer cooldown still holds after the shared one ends")
ok, _ = q.request("chat", "carol", "carol")
check(ok, "another viewer can water once the shared cooldown ends")

# Points: no chat cooldown, refunds when refused.
w.settings["paused"] = True
ok, msg = q.request("points", "dave", "Dave", redemption={"reward_id": "r1", "id": "x1"})
check(not ok and "paused" in msg and ("x1", "CANCELED") in w.redeemed, "paused: channel points are refunded")
ok, msg = q.request("bits", "erin", "Erin", bits=100)
check(ok and "100 bits" in msg and q.queue[-1]["src"] == "bits", "Bits are taken even while paused (they can't be refunded)")
q.tick()
check(len(w.sent) == 1, "...but nothing is sent while paused", w.sent)
w.settings["paused"] = False

# Bits beyond a dose cap are split; the budget doesn't apply to them.
w.slots[43] = 40
q2 = World().make("q2.json")
w2 = World()
w2.slots[43] = 40
q2 = w2.make("q2b.json")
ok, msg = q2.request("bits", "frank", "Frank", bits=200)
check(ok and [r["ml"] for r in q2.queue] == [40, 40, 20], "100 ml of Bits water at a 40 ml cap goes as 40+40+20",
      [r["ml"] for r in q2.queue])
w2.settings["viewer_day_ml"] = 10
ok, msg = q2.request("bits", "frank", "Frank", bits=20)
check(ok, "Bits ignore the free-water budget")
w2.settings["viewer_day_ml"] = 3
w2.t += 1000
ok, msg = q2.request("chat", "gina", "gina")
check(not ok and "all the free water" in msg and "Bits still water it" in msg,
      "free water over the day's budget is refused", msg)
w2.settings["viewer_day_ml"] = 1000
w2.settings["bits_max_ml"] = 60
ok, msg = q2.request("bits", "hal", "Hal", bits=10000)
check(ok and "60 ml" in msg, "one huge cheer is held to bits_max_ml", msg)

# The dispatcher waits for the node, never sends past it.
w3 = World()
q3 = w3.make("q3.json")
q3.request("test", "admin", "test", ml=50)
w3.age = 900
q3.tick()
check(not w3.sent and "isn't reporting" in q3.status, "a node that isn't reporting gets nothing", q3.status)
w3.age = 5
w3.slots[46] = 1
q3.tick()
check(not w3.sent and "pump is running" in q3.status, "an automatic watering in progress is waited out")
w3.slots[46] = 0
w3.slots[47] = 0
q3.tick()
check(not w3.sent and "reservoir" in q3.status, "an empty reservoir (float switch) is waited out")
w3.slots[47] = 1
w3.slots[45] = 1480
q3.tick()
check(not w3.sent and "24-hour limit" in q3.status, "the node's own 24 h cap is respected before asking", q3.status)
w3.slots[45] = 0
w3.ack = False
q3.tick()
check(w3.sent and q3.queue[0]["status"] == "queued" and "didn't answer" in q3.status,
      "no ACK from the gateway: kept in the queue, retried later")
w3.ack = True
q3.tick()
check(len(w3.sent) == 1, "...not before its 30 s back-off")
w3.t += 31
q3.tick()
check(len(w3.sent) == 2 and q3.queue[0]["status"] == "sent", "...and sent after it")

# A dose the node never shows: unconfirmed after CONFIRM_S, never sent again by itself.
w4 = World()
w4.pump_works = False
q4 = w4.make("q4.json")
q4.request("chat", "ivy", "Ivy")
q4.tick()
for _ in range(40):
    w4.step(5)
    q4.tick()
check(q4.history and q4.history[-1]["status"] == "unconfirmed" and len(w4.sent) == 1,
      "a dose never seen is marked unconfirmed after %d s and not re-sent" % SP.CONFIRM_S,
      (q4.history[-1:], w4.sent))
check(q4.retry(q4.history[-1]["id"]) and q4.queue and q4.queue[0]["status"] == "queued",
      "the person can retry it from the page")

# The node refusing: free water is dropped and refunded, Bits go back in the queue.
w5 = World()
w5.pump_works = False
q5 = w5.make("q5.json")
q5.request("points", "jo", "Jo", redemption={"reward_id": "r1", "id": "p9"})
q5.request("bits", "kim", "Kim", bits=20)
q5.tick()
w5.slots[46] = 4                     # stopped: supply empty -- and the float switch says so
w5.slots[47] = 0
w5.age = 0
w5.step()
q5.tick()
check(q5.history[-1]["status"] == "refused" and ("p9", "CANCELED") in w5.redeemed,
      "a node refusal (reservoir empty) refunds channel points")
q5.tick()
w5.step()
q5.tick()
bits = [r for r in q5.queue if r["src"] == "bits"]
check(bits and bits[0]["status"] == "queued", "a refused Bits dose waits in the queue for later", q5.queue)

# Points fulfilled on delivery; the queue survives a restart.
w6 = World()
q6 = w6.make("q6.json")
q6.request("points", "lu", "Lu", redemption={"reward_id": "r1", "id": "p1"})
q6.request("bits", "max", "Max", bits=100)
q6.tick()
w6.step()
q6.tick()
check(("p1", "FULFILLED") in w6.redeemed, "a delivered channel-point watering is marked fulfilled")
q6b = w6.make("q6.json")
check([r["src"] for r in q6b.queue] == ["bits"] and q6b.history[-1]["login"] == "lu",
      "the queue and history are reloaded after a restart")

# Logins on the overlay are cleaned; display names stay for chat.
check(SP.clean_login("Bad Name<script>") == "badnamescript", "only letters, digits and _ of a login are kept")
o = q6.overlay()
check(o["recent"][0]["user"] == "lu" and "type !water in chat" in o["how"] and o["queue"] == 1,
      "the overlay lists recent waterers and how to water", o)

# ---------------------------------------------------------------------------
# A local stand-in for Twitch's EventSub WebSocket server
# ---------------------------------------------------------------------------
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(op, data, fin=True, mask=False):
    b = bytes([(0x80 if fin else 0) | op])
    n = len(data)
    m = 0x80 if mask else 0
    if n < 126:
        b += bytes([m | n])
    elif n < 65536:
        b += bytes([m | 126]) + struct.pack(">H", n)
    else:
        b += bytes([m | 127]) + struct.pack(">Q", n)
    if mask:
        k = b"\x01\x02\x03\x04"
        return b + k + bytes(c ^ k[i % 4] for i, c in enumerate(data))
    return b + data


class MockWs(threading.Thread):
    """Accepts connections; for each, runs script(conn) after the handshake."""
    daemon = True

    def __init__(self, script):
        super().__init__()
        self.script = script
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.got = []                   # frames the client sent: (op, payload)
        self.conns = 0

    def url(self, path="/ws"):
        return "ws://127.0.0.1:%d%s" % (self.port, path)

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns += 1
            threading.Thread(target=self.serve, args=(c, self.conns), daemon=True).start()

    def serve(self, c, n):
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        key = [ln.split(":", 1)[1].strip() for ln in req.decode().split("\r\n") if ln.lower().startswith("sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
        threading.Thread(target=self.reader, args=(c,), daemon=True).start()
        try:
            self.script(c, n, self)
        except OSError:
            pass

    def reader(self, c):
        buf = b""
        try:
            while True:
                d = c.recv(4096)
                if not d:
                    return
                buf += d
                while len(buf) >= 6:
                    op, n = buf[0] & 0x0F, buf[1] & 0x7F
                    if len(buf) < 6 + n:
                        break
                    k, data = buf[2:6], buf[6:6 + n]
                    self.got.append((op, bytes(x ^ k[i % 4] for i, x in enumerate(data))))
                    buf = buf[6 + n:]
        except OSError:
            return


def basic(c, n, srv):
    c.sendall(frame(0x9, b"hi"))                                     # a ping
    c.sendall(frame(0x1, b'{"a":', fin=False) + frame(0x0, b'1}'))    # a fragmented message
    c.sendall(frame(0x1, b"masked", mask=True))
    c.sendall(frame(0x1, b"x" * 70000))
    c.sendall(frame(0x8, struct.pack(">H", 1000)))
    time.sleep(0.5)


srv = MockWs(basic)
srv.start()
ws = TW.WsClient(srv.url())
m1, m2, m3, m4 = ws.recv(), ws.recv(), ws.recv(), ws.recv()
check(m1 == '{"a":1}', "a fragmented text message is joined", m1)
check(m2 == "masked" and m3 == "x" * 70000, "masked and 64-bit-length frames are read")
check(m4 is None, "a close frame ends it")
time.sleep(0.2)
check((0xA, b"hi") in srv.got, "a ping is answered with a pong carrying its data", srv.got)


# ---------------------------------------------------------------------------
# EventSub: welcome, subscribe, notifications, a duplicate, a server move
# ---------------------------------------------------------------------------
def note(mid, typ, ev):
    return json.dumps({"metadata": {"message_id": mid, "message_type": "notification"},
                       "payload": {"subscription": {"type": typ}, "event": ev}}).encode()


def welcome(sid, ka=10):
    return json.dumps({"metadata": {"message_type": "session_welcome"},
                       "payload": {"session": {"id": sid, "keepalive_timeout_seconds": ka}}}).encode()


MOVE = {}


def eventsub_script(c, n, srv):
    if n == 1:
        c.sendall(frame(0x1, welcome("s1")))
        time.sleep(0.3)
        chat = {"chatter_user_login": "zed", "chatter_user_name": "Zed", "message": {"text": "!water please"}}
        c.sendall(frame(0x1, note("m1", "channel.chat.message", chat)))
        c.sendall(frame(0x1, note("m1", "channel.chat.message", chat)))          # delivered twice
        c.sendall(frame(0x1, json.dumps({"metadata": {"message_type": "session_reconnect"},
                                         "payload": {"session": {"reconnect_url": srv.url("/moved")}}}).encode()))
        time.sleep(0.2)
        # Twitch keeps using the old session until the new one is welcomed.
        c.sendall(frame(0x1, note("m2", "channel.cheer", {"user_login": "amy", "user_name": "Amy", "bits": 100})))
        time.sleep(1.0)
    elif n == 2:
        MOVE["moved"] = True
        c.sendall(frame(0x1, welcome("s2")))
        time.sleep(0.3)
        c.sendall(frame(0x1, note("m3", "channel.cheer", {"is_anonymous": True, "bits": 10})))
        time.sleep(3)


class FakeAuth:
    tok = {"user_id": "42", "access_token": "t"}
    linked = True

    def validate(self, force=False):
        return True


class FakeHelix:
    def __init__(self):
        self.subs = []

    def subscribe(self, typ, ver, cond, sid):
        self.subs.append((typ, sid))
        if typ == "channel.channel_points_custom_reward_redemption.add":
            raise TW.TwitchError(403, "The broadcaster must have partner or affiliate status.")


# threading.Thread keeps its own state in "_" names, and which ones changes by
# version: on Python 3.13 self._handle is the thread's handle, and a method
# EventSub had of that name made the first notification raise on the Pi.
check(not [n for n in vars(TW.EventSub) if n.startswith("_") and not n.startswith("__")],
      "EventSub (a Thread) defines no '_' names that a Python version could shadow")

es_srv = MockWs(eventsub_script)
es_srv.start()
events = []
fh = FakeHelix()
logs = []
es = TW.EventSub(FakeAuth(), fh, lambda uid: [("channel.chat.message", "1", {"broadcaster_user_id": uid, "user_id": uid}),
                                             ("channel.channel_points_custom_reward_redemption.add", "1", {}),
                                             ("channel.cheer", "1", {})],
                 lambda typ, ev: events.append((typ, ev)), logs.append, url=es_srv.url())
es.start()
for _ in range(60):
    if len(events) >= 3:
        break
    time.sleep(0.1)
es.stop()
types = [t for t, _ in events]
check(types.count("channel.chat.message") == 1, "a notification delivered twice is handled once", types)
check([t for t, s in fh.subs] == ["channel.chat.message", "channel.channel_points_custom_reward_redemption.add",
                                    "channel.cheer"] and all(s == "s1" for _, s in fh.subs),
      "subscribes once, on the first session's welcome", fh.subs)
check(es.subs.get("channel.channel_points_custom_reward_redemption.add", "").startswith("403")
      and es.subs.get("channel.cheer") == "ok", "a refused subscription (not an Affiliate) is kept for the page", es.subs)
check(MOVE.get("moved") and types.count("channel.cheer") == 2,
      "on a server move: the old session's last event and the new session's both arrive", events)

# The glue: Twitch events become requests with the right source and amount.
w7 = World()
sp = SP.StreamPlant.__new__(SP.StreamPlant)
sp.water = w7.make("q7.json")
sp.settings = lambda: w7.settings
said = []
sp.say = said.append
sp.on_event("channel.chat.message", {"chatter_user_login": "nia", "chatter_user_name": "Nia",
                                      "message": {"text": "!WATER"}})
sp.on_event("channel.chat.message", {"chatter_user_login": "ola", "chatter_user_name": "Ola",
                                      "message": {"text": "I'd !water it"}})
sp.on_event("channel.chat.message", {"chatter_user_login": "pat", "message": {"text": "!water"}, "cheer": {"bits": 5}})
sp.on_event("channel.cheer", {"is_anonymous": True, "bits": 100})
srcs = [(r["src"], r["login"]) for r in sp.water.queue]
check(srcs == [("chat", "nia"), ("bits", "anonymous")],
      "!water (any case) as the first word counts; mid-sentence doesn't; a cheer message is left to the cheer event",
      srcs)
w7.settings["reward_id"] = "rw"
sp.on_event("channel.channel_points_custom_reward_redemption.add",
            {"user_login": "quin", "user_name": "Quin", "id": "red1", "reward": {"id": "other"}})
sp.on_event("channel.channel_points_custom_reward_redemption.add",
            {"user_login": "quin", "user_name": "Quin", "id": "red2", "reward": {"id": "rw"}})
check([r["redemption"] for r in sp.water.queue if r["src"] == "points"] == [{"reward_id": "rw", "id": "red2"}],
      "only this app's reward counts as a watering")


# ---------------------------------------------------------------------------
# Device-code login against a fake token endpoint
# ---------------------------------------------------------------------------
class FakeId:
    def __init__(self):
        self.calls = []
        self.pending = 2
        self.access = "A1"
        self.refresh_ok = True

    def __call__(self, method, url, headers=None, form=None, body=None, timeout=15):
        self.calls.append((method, url.rsplit("/", 1)[1], dict(form or {})))
        if url.endswith("/device"):
            return {"device_code": "dc", "user_code": "ABCD-EFGH", "verification_uri": "https://www.twitch.tv/activate",
                    "interval": 1, "expires_in": 60}
        if url.endswith("/token") and form.get("grant_type", "").endswith("device_code"):
            if self.pending:
                self.pending -= 1
                raise TW.TwitchError(400, "authorization_pending")
            return {"access_token": "A1", "refresh_token": "R1", "expires_in": 14000, "scope": TW.SCOPES}
        if url.endswith("/token") and form.get("grant_type") == "refresh_token":
            if not self.refresh_ok:
                raise TW.TwitchError(400, "Invalid refresh token")
            self.access = "A2"
            return {"access_token": "A2", "refresh_token": "R2", "expires_in": 14000}
        if url.endswith("/validate"):
            if headers["Authorization"] != "OAuth " + self.access:
                raise TW.TwitchError(401, "invalid access token")
            return {"login": "gardener", "user_id": "42", "scopes": TW.SCOPES}
        raise AssertionError(url)


fid = FakeId()
tokpath = os.path.join(TMP, "twitch.json")
auth = TW.TwitchAuth(tokpath, lambda: "cid", http=fid)
d = auth.start_device()
check(d["user_code"] == "ABCD-EFGH" and "device_code" not in d, "the page is shown the code, not the device secret")
for _ in range(80):
    if (auth.device_status() or {}).get("state") == "linked":
        break
    time.sleep(0.1)
check(auth.linked and auth.account()["login"] == "gardener", "approval is picked up after 'authorization_pending'",
      auth.device_status())
check(stat.S_IMODE(os.stat(tokpath).st_mode) == 0o600, "the token file is readable by the admin's user only")
fid.access = "A2"                     # Twitch's side moved on: the old token gets a 401
auth.validated_at = 0
auth.tok["access_token"] = "stale"
auth.validate()
check(auth.tok["access_token"] == "A2" and auth.tok["refresh_token"] == "R2",
      "a 401 on validate refreshes, and the new single-use refresh token is saved")
check(json.load(open(tokpath))["refresh_token"] == "R2", "...to the file")
fid.refresh_ok = False
auth.tok["access_token"] = "stale"
auth.validated_at = 0
try:
    auth.validate()
    check(False, "a dead refresh token raises")
except TW.TwitchError:
    check(not auth.linked and "link the account again" in auth.last_error,
          "a dead refresh token unlinks, with a message saying what to do")
auth2 = TW.TwitchAuth(os.path.join(TMP, "none.json"), lambda: "", http=fid)
try:
    auth2.start_device()
    check(False, "no client ID refuses to start")
except ValueError as e:
    check("client ID" in str(e), "no client ID: says where to get one")

print("\n%s" % ("all passed" if not failed else "%d FAILED" % failed))
sys.exit(1 if failed else 0)
