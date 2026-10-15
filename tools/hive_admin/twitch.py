"""Twitch, as much of it as a plant on stream needs (stdlib only, like the rest
of the admin).

- Auth: the device-code flow. The admin asks Twitch for a short code, the person
  types it at twitch.tv/activate on any device and approves; no client secret
  and no redirect address, so nothing secret is ever typed into the admin and
  the Pi needs no port open to the internet. Tokens live in a file of their own
  (mode 600) that no API serves; a public client's refresh token is single-use,
  so every refresh saves the new pair.
- EventSub over a WebSocket the admin opens itself (outbound only): chat
  messages, channel-point redemptions and cheers arrive as notifications.
- Helix: send a chat message, mark a redemption fulfilled or refunded, create
  the channel-point reward.

Everything that arrives from Twitch -- names, messages, reward input -- is
viewer-typed text: it is shown escaped and never handed to the chat model.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ID_BASE = "https://id.twitch.tv/oauth2"
API_BASE = "https://api.twitch.tv/helix"
EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
SCOPES = ["user:read:chat", "user:write:chat", "channel:read:redemptions",
          "channel:manage:redemptions", "bits:read"]


class TwitchError(RuntimeError):
    def __init__(self, status, message):
        super().__init__("%s %s" % (status, message))
        self.status, self.message = status, message


def _http(method, url, headers=None, form=None, body=None, timeout=15):
    data = None
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            j = json.loads(raw)
            msg = j.get("message") or j.get("error") or raw[:200]
        except ValueError:
            msg = raw[:200].decode("utf-8", "replace")
        raise TwitchError(e.code, msg)


# ---------------------------------------------------------------------------
# Auth: device-code flow, refresh, hourly validation
# ---------------------------------------------------------------------------
class TwitchAuth:
    """Tokens for one Twitch account, kept in `path` (mode 600).

    Twitch asks apps that keep a session to validate the token hourly; a token
    revoked on twitch.tv (the person disconnects the app) then shows up here
    within the hour instead of at the next failed call."""

    VALIDATE_EVERY_S = 3600

    def __init__(self, path, client_id_fn, id_base=ID_BASE, http=_http):
        self.path = path
        self.client_id_fn = client_id_fn     # the client ID is a setting; read it when needed
        self.id_base = id_base
        self.http = http
        self.lock = threading.Lock()
        self.tok = self._load()
        self.device = None                   # an approval in progress, as shown to the person
        self.last_error = None
        self.validated_at = 0

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.tok, f)
        os.replace(tmp, self.path)

    @property
    def linked(self):
        return bool(self.tok.get("access_token"))

    def account(self):
        return {"login": self.tok.get("login"), "user_id": self.tok.get("user_id"),
                "scopes": self.tok.get("scopes") or []}

    def forget(self):
        with self.lock:
            self.tok = {}
            self.device = None
            try:
                os.remove(self.path)
            except OSError:
                pass

    # -- device-code flow ----------------------------------------------------
    def start_device(self):
        cid = self.client_id_fn()
        if not cid:
            raise ValueError("set the Twitch app's client ID first (dev.twitch.tv, client type Public)")
        r = self.http("POST", self.id_base + "/device", form={"client_id": cid, "scopes": " ".join(SCOPES)})
        self.device = {"device_code": r["device_code"], "user_code": r["user_code"],
                       "verification_uri": r["verification_uri"], "interval": max(1, int(r.get("interval", 5))),
                       "expires_at": time.time() + int(r.get("expires_in", 1800)), "state": "waiting"}
        threading.Thread(target=self._poll_device, args=(self.device,), daemon=True).start()
        return {k: self.device[k] for k in ("user_code", "verification_uri", "expires_at", "state")}

    def device_status(self):
        d = self.device
        return None if not d else {k: d[k] for k in ("user_code", "verification_uri", "expires_at", "state")}

    def _poll_device(self, d):
        cid = self.client_id_fn()
        while time.time() < d["expires_at"] and self.device is d:
            time.sleep(d["interval"])
            try:
                r = self.http("POST", self.id_base + "/token", form={
                    "client_id": cid, "scopes": " ".join(SCOPES), "device_code": d["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            except TwitchError as e:
                if "authorization_pending" in str(e.message):
                    continue
                if "slow_down" in str(e.message):
                    d["interval"] += 5
                    continue
                d["state"] = "failed: %s" % e.message
                return
            except OSError as e:
                d["state"] = "waiting (network: %s)" % e
                continue
            with self.lock:
                self.tok = {"access_token": r["access_token"], "refresh_token": r.get("refresh_token"),
                            "expires_at": time.time() + int(r.get("expires_in", 0) or 0),
                            "scopes": r.get("scope") or SCOPES}
            try:
                self.validate(force=True)
            except (TwitchError, OSError) as e:
                d["state"] = "failed: %s" % e
                return
            d["state"] = "linked"
            return
        if d["state"] == "waiting":
            d["state"] = "expired"

    # -- refresh and validate -----------------------------------------------
    def refresh(self):
        with self.lock:
            rt = self.tok.get("refresh_token")
            if not rt:
                raise TwitchError(401, "no refresh token: link the Twitch account again")
            try:
                r = self.http("POST", self.id_base + "/token", form={
                    "client_id": self.client_id_fn(), "grant_type": "refresh_token", "refresh_token": rt})
            except TwitchError as e:
                if e.status in (400, 401):
                    # A public client's refresh token is single-use and expires after 30
                    # days unused: a dead one means linking again, not retrying.
                    self.tok["access_token"] = None
                    self._save()
                    self.last_error = "Twitch refused the refresh token (%s): link the account again" % e.message
                raise
            self.tok.update(access_token=r["access_token"], refresh_token=r.get("refresh_token") or rt,
                            expires_at=time.time() + int(r.get("expires_in", 0) or 0))
            self._save()

    def validate(self, force=False):
        if not force and time.time() - self.validated_at < self.VALIDATE_EVERY_S:
            return True
        try:
            r = self.http("GET", self.id_base + "/validate",
                          headers={"Authorization": "OAuth %s" % self.tok.get("access_token")})
        except TwitchError as e:
            if e.status != 401:
                raise
            self.refresh()
            r = self.http("GET", self.id_base + "/validate",
                          headers={"Authorization": "OAuth %s" % self.tok.get("access_token")})
        with self.lock:
            self.tok.update(login=r.get("login"), user_id=r.get("user_id"), scopes=r.get("scopes") or [])
            self._save()
        self.validated_at = time.time()
        self.last_error = None
        return True

    def headers(self):
        return {"Authorization": "Bearer %s" % self.tok.get("access_token"), "Client-Id": self.client_id_fn()}


# ---------------------------------------------------------------------------
# Helix calls
# ---------------------------------------------------------------------------
class Helix:
    def __init__(self, auth, api_base=API_BASE, http=_http):
        self.auth, self.api_base, self.http = auth, api_base, http

    def call(self, method, path, query=None, body=None):
        url = self.api_base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            return self.http(method, url, headers=self.auth.headers(), body=body)
        except TwitchError as e:
            if e.status != 401:
                raise
            self.auth.refresh()           # expired: one refresh, one retry
            return self.http(method, url, headers=self.auth.headers(), body=body)

    def subscribe(self, sub_type, version, condition, session_id):
        return self.call("POST", "/eventsub/subscriptions", body={
            "type": sub_type, "version": version, "condition": condition,
            "transport": {"method": "websocket", "session_id": session_id}})

    def say(self, text):
        uid = self.auth.tok.get("user_id")
        return self.call("POST", "/chat/messages", body={"broadcaster_id": uid, "sender_id": uid,
                                                          "message": text[:480]})

    def set_redemption(self, reward_id, redemption_id, status):
        """status: FULFILLED, or CANCELED (refunds the points). Only works for a
        reward this same app created."""
        return self.call("PATCH", "/channel_points/custom_rewards/redemptions",
                         query={"id": redemption_id, "broadcaster_id": self.auth.tok.get("user_id"),
                                "reward_id": reward_id}, body={"status": status})

    def create_reward(self, title, cost, prompt, cooldown_s):
        r = self.call("POST", "/channel_points/custom_rewards",
                      query={"broadcaster_id": self.auth.tok.get("user_id")},
                      body={"title": title[:45], "cost": int(cost), "prompt": prompt[:200], "is_enabled": True,
                            "is_global_cooldown_enabled": cooldown_s > 0,
                            "global_cooldown_seconds": max(1, int(cooldown_s)) if cooldown_s > 0 else 1})
        return (r.get("data") or [{}])[0]


# ---------------------------------------------------------------------------
# A WebSocket client: just what EventSub needs (RFC 6455, client side)
# ---------------------------------------------------------------------------
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_FRAME = 1 << 20          # EventSub messages are a few kB


class WsClient:
    def __init__(self, url, timeout=15):
        u = urllib.parse.urlparse(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        sock.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket closed during the handshake")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("websocket handshake too long")
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
            raise ConnectionError("websocket handshake refused: %s" % lines[0][:80])
        hdrs = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if hdrs.get("sec-websocket-accept") != want:
            raise ConnectionError("websocket handshake: wrong accept key")
        self.sock = sock

    def settimeout(self, s):
        self.sock.settimeout(s)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_frame(self, op, data=b""):
        n = len(data)
        hdr = bytes([0x80 | op])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        """The next text message, or None when the server closes. Pings are
        answered here. A timeout (socket.timeout) means the link is dead: the
        caller reconnects rather than reading on from the middle of a frame."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            if n > WS_MAX_FRAME:
                raise ConnectionError("websocket frame of %d bytes" % n)
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x9:
                self.send_frame(0xA, data)
            elif op == 0x8:
                return None
            elif op in (0x0, 0x1, 0x2):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.send_frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# EventSub: one WebSocket session, its subscriptions, reconnects
# ---------------------------------------------------------------------------
class EventSub(threading.Thread):
    """Keeps an EventSub WebSocket open and hands each notification to
    on_event(type, event). `wanted(user_id)` lists (type, version, condition)
    to subscribe to; each one's outcome is kept in `subs` for the page (channel
    points and Bits answer 403 until the channel is a Twitch Affiliate).

    Twitch closes a session that doesn't subscribe within 10 s of its welcome,
    and sends a keepalive every keepalive_timeout_seconds when there is
    nothing else: silence past that means the link is dead."""
    daemon = True

    def __init__(self, auth, helix, wanted, on_event, log, url=EVENTSUB_URL):
        super().__init__()
        self.auth, self.helix, self.wanted, self.on_event, self.log = auth, helix, wanted, on_event, log
        self.url = url
        self.stop_evt = threading.Event()
        self.connected = False
        self.subs = {}                  # type -> "ok" or the error
        self.last_error = None
        self.last_message_at = 0
        self.seen = []                  # recent message ids: Twitch may deliver one twice

    def stop(self):
        self.stop_evt.set()

    def _welcome(self, ws):
        m = json.loads(ws.recv() or "{}")
        if (m.get("metadata") or {}).get("message_type") != "session_welcome":
            raise ConnectionError("EventSub: expected a welcome, got %s" % str(m)[:120])
        s = m["payload"]["session"]
        ws.settimeout(int(s.get("keepalive_timeout_seconds") or 10) + 10)
        return s["id"]

    def _subscribe(self, session_id):
        uid = self.auth.tok.get("user_id")
        for sub_type, version, cond in self.wanted(uid):
            try:
                self.helix.subscribe(sub_type, version, cond, session_id)
                self.subs[sub_type] = "ok"
            except TwitchError as e:
                self.subs[sub_type] = "%s %s" % (e.status, e.message)
                self.log("twitch: %s not subscribed: %s %s" % (sub_type, e.status, e.message))

    def _handle(self, raw):
        m = json.loads(raw)
        md = m.get("metadata") or {}
        mt = md.get("message_type")
        self.last_message_at = time.time()
        if mt == "notification":
            mid = md.get("message_id")
            if mid in self.seen:
                return None
            self.seen = (self.seen + [mid])[-200:]
            p = m.get("payload") or {}
            try:
                self.on_event((p.get("subscription") or {}).get("type"), p.get("event") or {})
            except Exception as e:      # a handler bug must not drop the session
                self.log("twitch: event handler failed: %s: %s" % (type(e).__name__, e))
        elif mt == "session_reconnect":
            return m["payload"]["session"]["reconnect_url"]
        elif mt == "revocation":
            st = (m.get("payload") or {}).get("subscription") or {}
            self.subs[st.get("type")] = "revoked: %s" % st.get("status")
            self.log("twitch: %s revoked (%s)" % (st.get("type"), st.get("status")))
        return None

    def run(self):
        backoff = 1
        ws = None
        url, resubscribe = self.url, True
        while not self.stop_evt.is_set():
            try:
                if ws is None:
                    self.auth.validate()
                    ws = WsClient(url)
                    sid = self._welcome(ws)
                    if resubscribe:
                        self._subscribe(sid)
                    self.connected, self.last_error, backoff = True, None, 1
                    url, resubscribe = self.url, True
                raw = ws.recv()
                if raw is None:
                    raise ConnectionError("EventSub closed the session")
                new_url = self._handle(raw)
                if new_url:
                    # Twitch's move to another server: open the new session first and only
                    # then drop the old one, reading what it still delivered -- events in
                    # between go to the old session. Subscriptions carry over.
                    nws = WsClient(new_url)
                    self._welcome(nws)
                    ws.settimeout(0.5)
                    try:
                        while True:
                            raw = ws.recv()
                            if raw is None:
                                break
                            self._handle(raw)
                    except (OSError, ConnectionError, ValueError):
                        pass
                    ws.close()
                    ws = nws
            except (OSError, ConnectionError, ValueError, KeyError, TwitchError) as e:
                self.connected = False
                self.last_error = "%s: %s" % (type(e).__name__, e)
                if ws is not None:
                    ws.close()
                    ws = None
                if isinstance(e, TwitchError) and e.status in (400, 401):
                    self.log("twitch: %s -- stopped; link the account again" % self.last_error)
                    return
                self.log("twitch: EventSub down (%s), retrying in %d s" % (self.last_error, backoff))
                self.stop_evt.wait(backoff)
                backoff = min(backoff * 2, 120)
        if ws is not None:
            ws.close()
        self.connected = False
