"""A plant on stream that viewers can water (the admin's Stream page).

Viewers ask from Twitch chat (a command word), with channel points, or with
Bits; each request becomes a dose of water for ONE pump node, given one at a
time. The limits, from the outside in:

  - The node's own firmware: its per-watering and per-24 h caps (slots 43/44)
    and its float switch (47) refuse anything over them, whatever this module
    believes. This module reads them first so it doesn't ask for what will be
    refused.
  - Here: a daily budget of free water, a cooldown for the chat command (one
    for everyone and one per viewer), and a short queue for free water.

Paid water (Bits) is never turned away by the budget or the cooldowns -- Bits
can't be refunded -- so it waits in a queue that survives a restart until the
node can take it. Free water that couldn't be given soon is refused at once,
and channel points are refunded.

A dose counts as given only when the node's own report shows it (its
pumped-in-24-h count, slot 45, going up): the gateway's ACK only means the
write was broadcast. A dose not seen within CONFIRM_S is marked unconfirmed
and never sent again by itself -- the person can retry it from the page.

Everything a viewer typed is data: names are shown escaped on the page, only
the login (letters, digits, _) goes on the video overlay, and nothing from
Twitch ever reaches the chat model.
"""
import json
import os
import queue
import re
import threading
import time

from twitch import EventSub, Helix, TwitchAuth, TwitchError

SLOT_DOSE, SLOT_MAX_DOSE, SLOT_MAX_DAY, SLOT_DAY_ML, SLOT_STATE, SLOT_RESERVOIR = 40, 43, 44, 45, 46, 47
SLOT_LIGHT = 56
PUMP_RUNNING = 1
PUMP_REFUSALS = {3: "refused: over a limit", 4: "refused: reservoir empty", 7: "refused: pump not calibrated"}

DEFAULTS = {
    "enabled": False,            # viewers can water at all
    "paused": False,             # a quick stop from the page, keeping everything else
    "node": None,                # the stream plant's pump node
    "client_id": "",             # the Twitch app's client ID (dev.twitch.tv, client type Public)
    "command": "!water",
    "chat_ml": 5,
    "chat_cooldown_s": 120,      # between free waterings from chat, for everyone
    "user_cooldown_s": 600,      # per viewer
    "points_ml": 15,
    "reward_id": "",             # the channel-point reward this app created
    "reward_title": "Water the plant",
    "reward_cost": 500,
    "bits_ml_per_100": 50,
    "bits_max_ml": 500,          # one cheer, however big
    "viewer_day_ml": 1000,       # free water (chat + points) per rolling 24 h
    "max_free_queue": 3,
    "announce": True,            # say waterings in chat
    "camera_url": "",            # the streaming box's snapshot/status server (tools/streamcam), shown on the page
    "overlay_key": "",
}
LIMITS = {"chat_ml": (1, 500), "chat_cooldown_s": (0, 86400), "user_cooldown_s": (0, 86400),
          "points_ml": (1, 1000), "reward_cost": (1, 1000000), "bits_ml_per_100": (0, 5000),
          "bits_max_ml": (0, 20000), "viewer_day_ml": (0, 20000), "max_free_queue": (1, 50)}
FREE = ("chat", "points", "test")
CONFIRM_S = 180
HISTORY_KEEP = 300
LOGIN_RX = re.compile(r"[^a-z0-9_]")


def clean_login(s):
    return LOGIN_RX.sub("", str(s or "").lower())[:25] or "someone"


def fmt_wait(s):
    s = int(max(0, s))
    return "%d s" % s if s < 90 else "%d min" % round(s / 60.0) if s < 5400 else "%.1f h" % (s / 3600.0)


class ViewerWater:
    """The queue and its rules, with everything outside passed in, so it can be
    tested without a hive or Twitch:

      settings()          -> dict (DEFAULTS merged with the saved settings)
      snapshot()          -> the poller's last dump {time, nodes: {id: {age, slots}}} or None
      stale_s()           -> seconds after which a node's values are old
      send(node, slot, v) -> True when the gateway acknowledged the write
      say(text)           -> a chat message (may be a no-op)
      redeem(r, status)   -> mark a channel-point redemption FULFILLED or CANCELED
      event(kind, text)   -> the admin's event log
      wake()              -> ask the poller for a fresh dump soon
    """

    def __init__(self, path, settings, snapshot, stale_s, send, say, redeem, event, wake, clock=time.time):
        self.path = path
        self.settings, self.snapshot, self.stale_s = settings, snapshot, stale_s
        self.send, self.say, self.redeem, self.event, self.wake = send, say, redeem, event, wake
        self.clock = clock
        self.lock = threading.RLock()
        self.status = "idle"
        self.last_free_at = 0           # the chat command's shared cooldown
        self.user_last = {}             # login -> when their free water was accepted
        self.cooldown_told = {}         # login -> when they were last told to wait (one reply per wait)
        self.retry_at = 0
        st = self._load()
        self.queue, self.history, self.seq = st.get("queue", []), st.get("history", []), st.get("seq", 0)

    # -- persistence ----------------------------------------------------------
    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"queue": self.queue, "history": self.history[-HISTORY_KEEP:], "seq": self.seq}, f)
        os.replace(tmp, self.path)

    # -- the node as the last dump shows it -------------------------------------
    def _node(self):
        s = self.settings()
        snap = self.snapshot() or {}
        rec = (snap.get("nodes") or {}).get(s.get("node"))
        if not rec or rec.get("age", 1e9) > self.stale_s():
            return None, None
        return rec, snap.get("time", self.clock()) - rec.get("age", 0)

    def _max_dose(self):
        rec, _ = self._node()
        v = (rec or {}).get("slots", {}).get(SLOT_MAX_DOSE)
        return v if v else 250

    # -- accounting ---------------------------------------------------------------
    def free_ml_24h(self):
        t = self.clock()
        given = sum(r["ml"] for r in self.history if r["src"] in FREE and r["status"] in ("given", "sent", "unconfirmed")
                    and t - r["at"] < 86400)
        waiting = sum(r["ml"] for r in self.queue if r["src"] in FREE)
        return given + waiting

    def _add(self, src, login, name, ml, redemption=None, bits=0):
        self.seq += 1
        r = {"id": self.seq, "src": src, "login": login, "name": str(name or login)[:40], "ml": int(ml),
             "at": self.clock(), "status": "queued", "redemption": redemption, "bits": bits}
        self.queue.append(r)
        return r

    # -- requests -----------------------------------------------------------------
    def request(self, src, login, name=None, bits=0, redemption=None, ml=None):
        """One viewer's ask. Returns (accepted, message) -- the message is what
        chat is told (None when nothing needs saying yet)."""
        s = self.settings()
        login = clean_login(login)
        who = "@" + (name or login)
        t = self.clock()
        with self.lock:
            if not s.get("enabled") or s.get("node") is None:
                return self._refuse(redemption, None)
            if src == "bits":
                rate = s["bits_ml_per_100"]
                total = min(s["bits_max_ml"], int(round(bits * rate / 100.0)))
                if total <= 0:
                    return False, None
                step, left = self._max_dose(), total
                while left > 0:
                    self._add("bits", login, name, min(step, left), bits=bits)
                    left -= step
                self._save()
                self.event("stream", "%s cheered %d bits: %d ml queued" % (login, bits, total))
                return True, "💧 %s cheered %d bits: %d ml of water on its way." % (who, bits, total)
            # Free water: the page's pause, the budget, the cooldowns, the queue.
            if s.get("paused"):
                return self._refuse(redemption, "%s watering is paused right now." % who)
            ml = int(ml or (s["points_ml"] if src == "points" else s["chat_ml"]))
            if self.free_ml_24h() + ml > s["viewer_day_ml"]:
                return self._refuse(redemption, "%s that's all the free water it gets today%s." % (
                    who, " - Bits still water it" if s["bits_ml_per_100"] > 0 else ""))
            if sum(1 for r in self.queue if r["src"] in FREE) >= s["max_free_queue"]:
                return self._refuse(redemption, "%s the watering can is busy - try again in a minute." % who)
            if src == "chat":
                wait = max(s["chat_cooldown_s"] - (t - self.last_free_at),
                           s["user_cooldown_s"] - (t - self.user_last.get(login, -1e12)))
                if wait > 0:
                    # Told once per wait: a viewer spamming the command gets one answer.
                    if t - self.cooldown_told.get(login, -1e12) < wait:
                        return False, None
                    self.cooldown_told[login] = t
                    return False, "%s the watering can is refilling - free water again in %s%s." % (
                        who, fmt_wait(wait), " (Bits skip the wait)" if s["bits_ml_per_100"] > 0 else "")
                self.last_free_at = t
                self.user_last[login] = t
            step, left = self._max_dose(), ml
            while left > 0:
                self._add(src, login, name, min(step, left), redemption=redemption if left == ml else None)
                left -= step
            self._save()
            return True, None

    def _refuse(self, redemption, message):
        if redemption:
            try:
                self.redeem(redemption, "CANCELED")      # the points go back
            except Exception as e:
                self.event("stream", "refund of a redemption failed: %s" % e)
        return False, message

    def retry(self, rid):
        """The person's call, from the page: send an unconfirmed dose again."""
        with self.lock:
            for i, r in enumerate(self.history):
                if r["id"] == rid and r["status"] == "unconfirmed":
                    r = dict(self.history.pop(i), status="queued", note="retried by hand")
                    self.queue.insert(0, r)
                    self._save()
                    return True
        return False

    def drop(self, rid):
        with self.lock:
            for i, r in enumerate(self.queue):
                if r["id"] == rid and r["status"] == "queued":
                    r = self.queue.pop(i)
                    self._finish(r, "dropped", "removed from the queue on the page")
                    return True
        return False

    # -- the dispatcher -------------------------------------------------------------
    def _finish(self, r, status, note=None):
        r["status"], r["done_at"] = status, self.clock()
        if note:
            r["note"] = note
        if r in self.queue:
            self.queue.remove(r)
        self.history.append(r)
        self.history = self.history[-HISTORY_KEEP:]
        self._save()

    def tick(self):
        """One step: confirm the dose in flight, or send the next one."""
        s = self.settings()
        t = self.clock()
        with self.lock:
            cur = next((r for r in self.queue if r["status"] == "sent"), None)
            rec, heard_at = self._node()
            slots = (rec or {}).get("slots", {})
            if cur:
                return self._confirm(cur, slots, heard_at, t, s)
            nxt = next((r for r in self.queue if r["status"] == "queued"), None)
            if not nxt:
                self.status = "idle"
                return
            if not s.get("enabled") or s.get("paused"):
                self.status = "paused" if s.get("enabled") else "off"
                return
            if t < self.retry_at:
                return
            if rec is None:
                self.status = "waiting: the plant's node isn't reporting"
                return
            if slots.get(SLOT_STATE) == PUMP_RUNNING:
                self.status = "waiting: the pump is running"
                return
            if slots.get(SLOT_RESERVOIR) == 0:
                self.status = "waiting: the reservoir is empty"
                return
            day, cap = slots.get(SLOT_DAY_ML), slots.get(SLOT_MAX_DAY)
            if day is not None and cap and day + nxt["ml"] > cap:
                self.status = "waiting: the node's 24-hour limit (%d of %d ml)" % (day, cap)
                return
            if not self.send(s["node"], SLOT_DOSE, nxt["ml"]):
                # No ACK: the gateway didn't take the write. Try again shortly; a
                # repeat here can only water twice, and the water recirculates.
                self.status = "waiting: the gateway didn't answer"
                self.retry_at = t + 30
                return
            nxt.update(status="sent", sent_at=t, base_day=day, base_state=slots.get(SLOT_STATE))
            self.status = "watering: %d ml for %s" % (nxt["ml"], nxt["login"])
            self._save()
            self.wake()

    def _confirm(self, cur, slots, heard_at, t, s):
        fresh = heard_at is not None and heard_at > cur["sent_at"] + 1
        if fresh:
            day, st = slots.get(SLOT_DAY_ML), slots.get(SLOT_STATE)
            base = cur.get("base_day")
            given = day is not None and base is not None and (day > base or (day < base and st == 2))
            if given:
                self._finish(cur, "given")
                self.event("stream", "%s watered %d ml (%s)" % (cur["login"], cur["ml"], cur["src"]))
                if cur.get("redemption"):
                    try:
                        self.redeem(cur["redemption"], "FULFILLED")
                    except Exception as e:
                        self.event("stream", "marking a redemption fulfilled failed: %s" % e)
                if s.get("announce"):
                    self.say("💧 @%s watered the plant: %d ml." % (cur["name"], cur["ml"]))
                self.status = "idle"
                return
            if st in PUMP_REFUSALS and st != cur.get("base_state"):
                why = PUMP_REFUSALS[st]
                if cur["src"] == "bits":
                    # Paid: back in the queue, to go when the node can take it.
                    cur.update(status="queued", note="node %s; waiting" % why)
                    self.status = "waiting: node %s" % why
                    self.retry_at = t + 300
                    self._save()
                else:
                    self._finish(cur, "refused", "node " + why)
                    if cur.get("redemption"):
                        self._refuse(cur["redemption"], None)
                self.event("stream", "dose for %s %s by the node" % (cur["login"], why))
                return
        if t - cur["sent_at"] > CONFIRM_S:
            self._finish(cur, "unconfirmed", "the node never showed it; not sent again")
            self.event("stream", "dose of %d ml for %s not seen on the node within %d s" % (
                cur["ml"], cur["login"], CONFIRM_S))
            self.status = "idle"
        else:
            self.status = "watering: %d ml for %s (waiting for the node's report)" % (cur["ml"], cur["login"])

    # -- what the page and the overlay show ----------------------------------------
    def view(self):
        with self.lock:
            return {"status": self.status, "queue": [dict(r) for r in self.queue],
                    "history": [dict(r) for r in self.history[-50:]][::-1],
                    "free_ml_24h": self.free_ml_24h()}

    def overlay(self):
        s = self.settings()
        t = self.clock()
        with self.lock:
            today = [r for r in self.history if r["status"] == "given" and t - r["at"] < 86400]
            recent = [{"user": r["login"], "ml": r["ml"], "src": r["src"], "ago_s": int(t - r.get("done_at", r["at"]))}
                      for r in self.history[-5:][::-1] if r["status"] == "given"]
            how = ["type %s in chat" % s["command"]]
            if s.get("reward_id"):
                how.append("channel points")
            if s["bits_ml_per_100"] > 0:
                how.append("Bits: %d ml per 100" % s["bits_ml_per_100"])
            return {"status": "paused" if s.get("paused") else self.status,
                    "waterings_today": len(today), "ml_today": sum(r["ml"] for r in today),
                    "free_left_ml": max(0, s["viewer_day_ml"] - self.free_ml_24h()),
                    "next_free_s": int(max(0, s["chat_cooldown_s"] - (t - self.last_free_at))),
                    "queue": len(self.queue), "recent": recent, "how": " | ".join(how)}


# ---------------------------------------------------------------------------
# Wired to the admin
# ---------------------------------------------------------------------------
class StreamPlant:
    """Settings in config.json under "stream" (no secrets there: /api/config
    serves that file to the page), Twitch tokens in twitch.json (mode 600,
    served by nothing), the queue and history in stream_queue.json."""

    TICK_S = 2
    CHAT_GAP_S = 1.5             # Twitch allows ~20 messages / 30 s; a plant needs far fewer

    def __init__(self, app, data_dir):
        self.app = app
        self.auth = TwitchAuth(os.path.join(data_dir, "twitch.json"), lambda: self.settings()["client_id"])
        self.helix = Helix(self.auth)
        self.water = ViewerWater(
            os.path.join(data_dir, "stream_queue.json"), self.settings,
            snapshot=lambda: app.poller.snapshot,
            stale_s=lambda: app.cfg.data.get("stale_seconds", 300),
            send=app.send_action, say=self.say, redeem=self.redeem,
            event=app.store.event, wake=self.wake_soon)
        self.eventsub = None
        self.chat_out = queue.Queue(maxsize=20)
        self.last_chat_error = None

    # -- settings ---------------------------------------------------------------
    def settings(self):
        s = dict(DEFAULTS)
        s.update(self.app.cfg.data.get("stream") or {})
        return s

    def save_settings(self, patch):
        s = self.app.cfg.data.get("stream") or {}
        for k, v in patch.items():
            if k not in DEFAULTS or k == "overlay_key":
                continue
            if k in LIMITS:
                v = int(v)
                lo, hi = LIMITS[k]
                if not lo <= v <= hi:
                    raise ValueError("%s must be %d-%d" % (k, lo, hi))
            elif k == "node":
                if v in (None, ""):
                    v = None
                else:
                    v = int(v)
                    kind = self.app.kind_of(v, ((self.app.poller.snapshot or {}).get("nodes", {}).get(v) or {})
                                            .get("slots") or {})
                    if not ((self.app.kinds.get(kind) or {}).get("slots", {}).get(str(SLOT_DOSE)) or {}).get("writable"):
                        raise ValueError("node %d has no pump" % v)
            elif k in ("enabled", "paused", "announce"):
                v = bool(v)
            elif k == "camera_url":
                v = str(v or "").strip()
                if v and not re.match(r"^https?://[^\s\"'<>]+$", v):
                    raise ValueError("the camera address must start with http:// or https://")
            elif k == "command":
                v = str(v).strip().split()[0][:25] if str(v).strip() else "!water"
            else:
                v = str(v).strip()[:100]
            s[k] = v
        self.app.cfg.data["stream"] = s
        self.app.cfg.save()

    def new_overlay_key(self):
        s = self.app.cfg.data.get("stream") or {}
        s["overlay_key"] = base64_key()
        self.app.cfg.data["stream"] = s
        self.app.cfg.save()
        return s["overlay_key"]

    # -- threads ----------------------------------------------------------------
    def start(self):
        threading.Thread(target=self._dispatch, daemon=True).start()
        threading.Thread(target=self._chat_sender, daemon=True).start()

    def _dispatch(self):
        last_check = 0
        while True:
            try:
                self.water.tick()
                if time.time() - last_check >= 30:
                    last_check = time.time()
                    self.ensure_eventsub()
            except Exception as e:      # never let the dispatcher die
                print("stream: %s: %s" % (type(e).__name__, e), flush=True)
            time.sleep(self.TICK_S)

    def ensure_eventsub(self):
        s = self.settings()
        want = bool(s.get("enabled") and s.get("client_id") and self.auth.linked)
        alive = self.eventsub is not None and self.eventsub.is_alive()
        if want and not alive:
            self.eventsub = EventSub(self.auth, self.helix, self.wanted, self.on_event,
                                     log=lambda m: self.app.store.event("stream", m))
            self.eventsub.start()
        elif not want and alive:
            self.eventsub.stop()

    def wanted(self, uid):
        return [("channel.chat.message", "1", {"broadcaster_user_id": uid, "user_id": uid}),
                ("channel.channel_points_custom_reward_redemption.add", "1", {"broadcaster_user_id": uid}),
                ("channel.cheer", "1", {"broadcaster_user_id": uid})]

    def wake_soon(self):
        # The node reports a dose within seconds (slot 45 is sampled every 5 s);
        # ask the poller for a dump then, instead of at its usual minute.
        for d in (6, 15, 40):
            t = threading.Timer(d, self.app.poller.wake.set)
            t.daemon = True
            t.start()

    # -- Twitch in and out ----------------------------------------------------------
    def on_event(self, typ, ev):
        s = self.settings()
        msg = None
        if typ == "channel.chat.message":
            if ev.get("cheer"):
                return                  # the cheer event pays for it
            words = ((ev.get("message") or {}).get("text") or "").strip().lower().split()
            if words[:1] != [s["command"].lower()]:
                return
            _, msg = self.water.request("chat", ev.get("chatter_user_login"), ev.get("chatter_user_name"))
        elif typ == "channel.channel_points_custom_reward_redemption.add":
            rw = ev.get("reward") or {}
            if not s.get("reward_id") or rw.get("id") != s["reward_id"]:
                return
            _, msg = self.water.request("points", ev.get("user_login"), ev.get("user_name"),
                                        redemption={"reward_id": rw["id"], "id": ev.get("id")})
        elif typ == "channel.cheer":
            anon = ev.get("is_anonymous") or not ev.get("user_login")
            _, msg = self.water.request("bits", "anonymous" if anon else ev.get("user_login"),
                                        "anonymous" if anon else ev.get("user_name"), bits=int(ev.get("bits") or 0))
        if msg:
            self.say(msg)

    def say(self, text):
        if not (self.auth.linked and self.settings().get("announce")):
            return
        try:
            self.chat_out.put_nowait(text)
        except queue.Full:
            pass                        # a flood of replies: drop, chat has moved on

    def _chat_sender(self):
        while True:
            text = self.chat_out.get()
            try:
                self.helix.say(text)
                self.last_chat_error = None
            except (TwitchError, OSError) as e:
                self.last_chat_error = str(e)
            time.sleep(self.CHAT_GAP_S)

    def redeem(self, redemption, status):
        self.helix.set_redemption(redemption["reward_id"], redemption["id"], status)

    def create_reward(self):
        s = self.settings()
        r = self.helix.create_reward(s["reward_title"], s["reward_cost"],
                                     "Gives the plant %d ml of water." % s["points_ml"], s["chat_cooldown_s"])
        if not r.get("id"):
            raise ValueError("Twitch didn't return a reward")
        self.save_settings({"reward_id": r["id"]})
        return r["id"]

    # -- what the page and the overlay see --------------------------------------
    def plant(self):
        s = self.settings()
        nid = s.get("node")
        if nid is None:
            return {}
        rec = ((self.app.poller.snapshot or {}).get("nodes") or {}).get(nid) or {}
        slots = rec.get("slots") or {}
        kind = self.app.kind_of(nid, slots)
        soil = self.app.derived(nid, kind, slots).get("soil") if slots else None
        meta = self.app.cfg.node(nid)
        hhmm = lambda m: "%02d:%02d" % (m // 60, m % 60) if isinstance(m, int) and 0 <= m < 1440 else None
        return {"node": nid, "name": meta.get("name") or "node %d" % nid,
                "soil": None if soil is None else round(soil),
                "light": None if SLOT_LIGHT not in slots else bool(slots[SLOT_LIGHT]),
                "lights_on_at": hhmm(slots.get(58)), "lights_off_at": hhmm(slots.get(59)),
                "reservoir_empty": slots.get(SLOT_RESERVOIR) == 0}

    def status(self):
        s = self.settings()
        es = self.eventsub
        out = {"settings": {k: v for k, v in s.items() if k != "overlay_key"},
               "overlay_key_set": bool(s.get("overlay_key")),
               "twitch": dict(self.auth.account(), linked=self.auth.linked,
                              connected=bool(es and es.connected), subscriptions=dict(es.subs) if es else {},
                              error=(es.last_error if es else None) or self.auth.last_error,
                              chat_error=self.last_chat_error, device=self.auth.device_status()),
               "plant": self.plant()}
        out.update(self.water.view())
        return out

    def overlay(self):
        o = self.water.overlay()
        o["plant"] = self.plant()
        return o


def base64_key():
    import secrets
    return secrets.token_urlsafe(18)
