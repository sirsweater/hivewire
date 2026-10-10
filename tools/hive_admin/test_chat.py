#!/usr/bin/env python3
"""Offline test for the Chat page (hive_chat.py + the admin's /api/chat routes).

No model server needed: the model is a script of canned replies, standing in
for one that tries the things a small model really does - picking a node nobody
named, reaching for a firmware slot, claiming it watered something it didn't.
The guards must hold whatever the model says. The hive is the simulated swarm
(--fake), served by the admin's own routes in-process.

    python tools/hive_admin/test_chat.py
"""
import argparse
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hive_admin as H  # noqa: E402
import hive_chat as C  # noqa: E402

failed = 0


def check(cond, msg, detail=None):
    global failed
    if cond:
        print("ok:", msg)
    else:
        failed += 1
        print("FAIL:", msg, "" if detail is None else detail)


tmp = tempfile.mkdtemp()
args = argparse.Namespace(port=None, fake=True, data=tmp, listen="127.0.0.1", http_port=0, push_script=None,
                          yield_to="[n]othing", flash_only=False)
app = H.App(args)
H.Handler.app = app
app.poller.poll()
hive = H.LocalHive(app)

# --- the in-process hive client ------------------------------------------------
st = hive.call("GET", "/api/state")
check(len(st.get("nodes", [])) >= 3, "LocalHive serves /api/state from the admin's own routes")
check(all(isinstance(k, str) for n in st["nodes"] for k in n["slots"]),
      "slot ids arrive as strings, the same as over HTTP")
pump = next(n for n in st["nodes"] if C.has_pump(hive, n["kind"]))
nopump = next(n for n in st["nodes"] if not C.has_pump(hive, n["kind"]))
try:
    hive.call("GET", "/api/no-such-route")
    check(False, "an unknown route raises")
except RuntimeError:
    check(True, "an unknown route raises")

# --- a scripted model ----------------------------------------------------------
script = []


def fake_llm(messages, read_only=False, no_tools=False, wait=180):
    fake_llm.calls.append({"read_only": read_only, "no_tools": no_tools,
                           "tools": [] if no_tools else [n for n in C.TOOLS if not (read_only and n in C.WRITES)]})
    return script.pop(0) if script else {"role": "assistant", "content": "ok"}


fake_llm.calls = []
C.llm_chat = C.ollama_chat = fake_llm


def tool(name, **a):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": a}}]}


def say(text):
    return {"role": "assistant", "content": text}


def last_tool_result(conv):
    return json.loads(next(m["content"] for m in reversed(conv.messages) if m["role"] == "tool"))


# A proposal waits for Confirm; nothing reaches the gateway before the click.
conv = C.Conversation()
script[:] = [tool("water_now", node=str(pump["id"]), ml=20), say("Press Confirm to water it.")]
before = len(app.store.events(500))
out = conv.ask(hive, "water node %d 20 ml" % pump["id"])
check(len(out["pending"]) == 1, "a watering request becomes one pending card", out)
check(last_tool_result(conv).get("awaiting_approval") is True, "the model is told it's NOT sent yet")
sent = [e for e in app.store.events(500)[: len(app.store.events(500)) - before] if e["text"].startswith("set ")]
check(not sent, "nothing was sent to the gateway before Confirm", sent)
r = conv.approve(hive, out["pending"][0]["id"])
check(r["sent"] and "ACK" in str(r["reply"]), "Confirm sends it through /api/set", r)
check(any(e["text"].startswith("set %d 40 20" % pump["id"]) for e in app.store.events(20)), "the gateway got set N 40 20")
try:
    conv.approve(hive, out["pending"][0]["id"])
    check(False, "a second Confirm of the same card is refused")
except ValueError:
    check(True, "a second Confirm of the same card is refused")

# Declined: dropped, and the model is told.
script[:] = [tool("water_now", node=str(pump["id"]), ml=20), say("Waiting for you.")]
out = conv.ask(hive, "water node %d 20 ml again" % pump["id"])
conv.decline(out["pending"][0]["id"])
check(not conv.pending and "declined" in conv.messages[-1]["content"], "Decline drops it and tells the model")

# A node nobody named: refused, whatever the model picks.
conv = C.Conversation()
script[:] = [tool("water_now", node=str(pump["id"]), ml=20), say("Which one?")]
out = conv.ask(hive, "water it")
check(not out["pending"], "'water it' with no node named: nothing proposed", out)
check("Which one" in out["answer"], "...and the person is asked which one", out["answer"])

# A question gets read-only tools; a write tried anyway is refused.
conv = C.Conversation()
fake_llm.calls.clear()
script[:] = [tool("water_now", node=str(pump["id"]), ml=20), say("Its soil is fine.")]
out = conv.ask(hive, "is node %d dry?" % pump["id"])
check(not any(n in C.WRITES for n in fake_llm.calls[0]["tools"]), "a question is offered no write tools")
check(not out["pending"], "a write slipped in on a question is refused", out)

# Firmware and automatic-watering slots are refused outright.
conv = C.Conversation()
script[:] = [tool("set_slot", target=str(pump["id"]), slot=23, value=1), say("Can't do that here.")]
out = conv.ask(hive, "set the OTA arm on node %d to 1" % pump["id"])
check(not out["pending"] and "firmware" in last_tool_result(conv)["reason"], "firmware slot refused", out)
script[:] = [tool("set_slot", target=str(pump["id"]), slot=49, value=0), say("Use the admin page.")]
out = conv.ask(hive, "set automatic watering on node %d to off" % pump["id"])
check(not out["pending"], "automatic-watering slot refused", out)

# A setting the person didn't name (asked about flow, model reaches for the float switch).
conv = C.Conversation()
script[:] = [tool("set_slot", target=str(pump["id"]), slot=48, value=0), say("Done.")]
out = conv.ask(hive, "set the pump flow on node %d to 50" % pump["id"])
check(not out["pending"], "a slot the person didn't name is refused", out)

# No pump: refused before anyone is asked.
conv = C.Conversation()
script[:] = [tool("water_now", node=str(nopump["id"]), ml=20), say("That node has no pump.")]
out = conv.ask(hive, "water node %d 20 ml" % nopump["id"])
check(not out["pending"] and "no pump" in last_tool_result(conv)["reason"], "a node with no pump can't be watered")

# Broadcasting a swarm mode isn't a tool at all.
check("set_mode" not in C.TOOLS, "swarm-wide mode broadcasts are not offered to the model")

# "I've watered it" when nothing was sent: retried, then replaced with the truth.
conv = C.Conversation()
script[:] = [tool("water_now", node=str(nopump["id"]), ml=20), say("I've watered it for you."),
             say("I've watered it for you.")]
out = conv.ask(hive, "water node %d 20 ml" % nopump["id"])
check("watered it" not in out["answer"] and "Nothing was changed" in out["answer"],
      "a false 'I've watered it' is replaced with what happened", out["answer"])

# Node references the model actually produces.
for ref in (str(pump["id"]), "node %d" % pump["id"], "#%d" % pump["id"], "id %d" % pump["id"]):
    n, err = C.resolve_node(hive, ref)
    check(n and n["id"] == pump["id"], "resolve_node understands %r" % ref, err)

# --- the HTTP routes, through a real handler ---------------------------------
h = H._Capture(app, {"url": "ftp://nope", "model": "m"})
try:
    h.route_post("/api/chat/config", {})
    check(False, "a non-http model server address is refused")
except ValueError:
    check(True, "a non-http model server address is refused")
h = H._Capture(app, {"url": "http://127.0.0.1:9/v1", "model": "m", "api": "openai", "api_key": "sekrit"})
h.route_post("/api/chat/config", {})
h = H._Capture(app, None)
h.route_get("/api/chat/config", {})
cfg = h.out[0]
check(cfg["enabled"] and cfg["key_set"] and "sekrit" not in json.dumps(cfg), "the API key is stored but never sent back")
check(os.stat(os.path.join(tmp, "config.json")).st_mode & 0o077 == 0, "config.json (holding the key) is private")

# --- OpenAI <-> Ollama message shapes ------------------------------------------
msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x1", "function": {"name": "hive_status",
                                                                                   "arguments": {}}}]},
        {"role": "tool", "tool_name": "hive_status", "tool_call_id": "x1", "content": "{}"}]
o = C._to_openai(msgs)
check(o[2]["tool_calls"][0]["function"]["arguments"] == "{}" and o[3]["tool_call_id"] == "x1",
      "tool calls convert to the OpenAI shape")
back = C._from_openai({"content": None, "tool_calls": [{"id": "x2", "type": "function",
                                                        "function": {"name": "node_detail", "arguments": '{"node": "3"}'}}]})
check(back["tool_calls"][0]["function"]["arguments"] == {"node": "3"}, "and back")

# --- offers of things the chat can't do are rewritten -------------------------------
t = C.scrub_offers("It isn't safe. Would you like me to disable automatic watering for now? Check the pump.")
check("disable automatic watering for now" not in t and "admin page" in t and "Check the pump" in t,
      "an offer to switch automatic watering off is replaced with where it's done", t)
t = C.scrub_offers("Shall I recalibrate the probe?")
check("recalibrate" not in t and "admin page" in t, "an offer to recalibrate is replaced", t)
t = C.scrub_offers("Automatic watering is on, and calibration was done on Oct 4.")
check(t == "Automatic watering is on, and calibration was done on Oct 4.", "plain statements are left alone", t)

# --- amounts in other units are worked out in code -----------------------------
check(C.stated_ml("water it 1 cup") == (237, "1 US cup"), "1 cup = 237 ml", C.stated_ml("water it 1 cup"))
check(C.stated_ml("water it half a cup")[0] == 118 and C.stated_ml("water it 1.5 litres")[0] == 1500,
      "half a cup, 1.5 litres")
check(C.stated_ml("water it for 30 seconds", 95)[0] == 48, "30 s at 95 ml/min = 48 ml")
check(C.stated_ml("water it for 30 seconds") is None, "...but not without a calibrated flow")
check(C.stated_ml("water it 50 ml, it was dry 2 minutes ago", 95) is None and
      C.stated_ml("what happened 2 minutes ago", 95) is None, "times that aren't pumping, and ml, are left alone")
check(C.stated_ml("water the rhubarb 1 cup and the larkspur 2 cups") is None, "two amounts are left to the model")
conv = C.Conversation()
script[:] = [tool("water_now", node=str(pump["id"]), ml=1), say("Press Confirm.")]
out = conv.ask(hive, "water node %d 1 cup" % pump["id"])
check(out["pending"] and "pump 237 ml (1 US cup)" in out["pending"][0]["summary"],
      "the model's ml=1 for '1 cup' becomes 237 ml on the card", out["pending"])
conv.decline(out["pending"][0]["id"])
app.poller.snapshot["nodes"][pump["id"]]["slots"][42] = 120
script[:] = [tool("water_now", node=str(pump["id"]), ml=30), say("Press Confirm.")]
out = conv.ask(hive, "water node %d for 30 seconds" % pump["id"])
check(out["pending"] and "pump 60 ml (30 s at 120 ml/min)" in out["pending"][0]["summary"],
      "'30 seconds' is pumping time at the node's flow, not 30 ml", out["pending"])
conv.decline(out["pending"][0]["id"])
app.poller.snapshot["nodes"][pump["id"]]["slots"].pop(42, None)
# These read slots by id the way the HTTP API spells them; in-process they used to see
# int ids, find nothing, and quietly skip the check.
app.poller.snapshot["nodes"][pump["id"]]["slots"][43] = 100
script[:] = [tool("water_now", node=str(pump["id"]), ml=150), say("That's over its limit.")]
out = conv.ask(hive, "water node %d 150 ml" % pump["id"])
check(not out["pending"] and "per-watering limit" in last_tool_result(conv)["reason"],
      "the per-watering cap is checked in the web chat too", out)
app.poller.snapshot["nodes"][pump["id"]]["slots"].pop(43, None)
s = C.t_hive_status(hive)
head = [h for h in (hive.kinds().get(pump["kind"]) or {}).get("headline", []) if not h.startswith("d:")]
mine = next(x for x in s["nodes"] if x["id"] == pump["id"])
check(head and any("(slot %s)" % h in k for h in head for k in mine["readings"]),
      "the status carries each node's headline readings", mine["readings"])

# --- watering facts go out labelled ----------------------------------------------
adv = C.t_watering_advice(hive, str(pump["id"]))
check("current" not in adv and "soil moisture now" in adv and
      all(not isinstance(v, (int, float)) or k == "hours between waterings" for k, v in adv.items()),
      "watering advice is labelled words, with the soil as it is now", adv)
t0 = int(H.now())
app.store.add_readings([(t0 - 7200, pump["id"], 45, 0), (t0 - 3600, pump["id"], 45, 0),
                        (t0 - 1800, pump["id"], 45, 60), (t0 - 600, pump["id"], 45, 60)])
rec = C.watering_record(hive, pump)
check("Pump last ran (automatic or sent): %s, 60 ml" % __import__("time").strftime(
      "%b %d %H:%M", __import__("time").localtime(t0 - 1800)) in rec, "the record dates the last pump run", rec)
f = C.focus_facts(hive, "you watered node %d earlier, right?" % pump["id"])
check("WATERING RECORD" in f and "didn't happen" in f, "a claimed past watering gets the record beside it", f)
check("WATERING RECORD" not in C.focus_facts(hive, "water node %d 50 ml" % pump["id"]), "...a new request doesn't")

# --- "let's check..." and then nothing: held to it once --------------------------
conv = C.Conversation()
fake_llm.calls.clear()
script[:] = [say("Let's check its current settings."), tool("hive_status"), say("Everything is reporting.")]
out = conv.ask(hive, "is everything ok?")
check(out["answer"] == "Everything is reporting." and len(fake_llm.calls) == 3,
      "an answer that only promises to check is sent back to check", out["answer"])
fake_llm.calls.clear()
script[:] = [say("Let me check."), say("Let me check.")]
out = conv.ask(hive, "is everything ok?")
check(len(fake_llm.calls) == 2, "...once, not forever", len(fake_llm.calls))

# --- things the chat can't do get a straight answer, and no card -----------------
for q, want in (("water node %d 50 ml in 2 hours" % pump["id"], "can't schedule"),
                ("turn the node %d pump on and leave it running" % pump["id"], "can't be left running")):
    fake_llm.calls.clear()
    out = conv.ask(hive, q)
    check(want in out["answer"] and not out["pending"] and not fake_llm.calls, "'%s': %s, nothing proposed" % (q, want))
check(not any(p.search("should I water node 14 tomorrow?") or p.search("why did the pump run all night?")
              for p, _ in C.FIXED_REPLIES[1:3]), "...but questions about those still reach the model")
f = C.focus_facts(hive, "how's node %d?" % pump["id"])
check(re.search(r"soil now (\d|not calibrated)", f), "a named plant's soil reading sits beside the question", f)
check(hive.fmt("WaterNode", 52, 162) == "162 min (2.7 hours)", "long minute settings are also given in hours",
      hive.fmt("WaterNode", 52, 162))

# --- the 24-hour allowance is worked out in code ---------------------------------
sl = app.poller.snapshot["nodes"][pump["id"]]["slots"]
sl.update({44: 400, 45: 100, 43: 250})
n, _ = C.resolve_node(hive, str(pump["id"]))
b = C.water_budget(hive, n)
check(b and "300 ml more is allowed" in b and "at most 250 ml in any one watering" in b,
      "the water budget is the 24 h limit minus what was pumped", b)
check("WATER BUDGET" in C.focus_facts(hive, "how much more water can node %d get today?" % pump["id"]),
      "...and sits beside a how-much-more question")
for k in (44, 45, 43):
    sl.pop(k, None)

# --- the dew point is worked out in code ----------------------------------------
d = C.dew_point(hive)
m = re.search(r"at (.+?) now: (-?[\d.]+) °C .* air temperature (-?[\d.]+) °C and humidity (\d+)%", d)
if m:
    t, rh = float(m.group(3)), float(m.group(4))
    g = __import__("math").log(rh / 100) + 17.62 * t / (243.12 + t)
check(m and abs(float(m.group(2)) - 243.12 * g / (17.62 - g)) < 0.6, "the dew point follows the Magnus formula", d)
check("Dew point" in C.focus_facts(hive, "what's the dew point outside?"), "...and sits beside a dew point question")

# --- a node the gateway has forgotten ------------------------------------------
# The gateway drops a node some hours after it stops reporting. It must still be
# findable, as "not reporting since ...", not "there is no node 99".
t0 = int(H.now())
app.cfg.set_node(99, {"name": "Old relay", "auto_kind": "RangeNode"})
app.cfg.set_node(98, {"auto_kind": "RangeNode"})
app.cfg.set_node(97, {"auto_kind": "RangeNode", "hidden": True})
app.store.add_readings([(t0 - 3 * 3600, 99, 1, 5), (t0 - 10 * 86400, 98, 1, 5), (t0 - 3600, 97, 1, 5)])
app._silent_cache = (0, {})
st = hive.call("GET", "/api/state")
gone = {x["id"]: x for x in st.get("silent", [])}
check(99 in gone and 98 in gone and 97 not in gone, "state lists known nodes the gateway dropped, not hidden ones",
      sorted(gone))
check(not any(n["id"] in (97, 98, 99) for n in st["nodes"]), "...apart from the live cards")
check(3 * 3600 - 5 <= (gone[99]["age"] or 0) <= 3 * 3600 + 60, "...with when each was last heard", gone[99])
n, err = C.resolve_node(hive, "node 99")
check(n is None and "NOT reporting" in err and "3h ago" in err, "asking for it says it went quiet, and when", err)
n, err = C.resolve_node(hive, "old relay")
check(n is None and "NOT reporting" in err, "...by name too", err)
s = C.t_hive_status(hive)
check(any("node 99" in x for x in s.get("not_reporting", [])) and
      not any("node 98" in x for x in s.get("not_reporting", [])), "the status lists what went quiet this week", s.get("not_reporting"))
check("98" in json.dumps({k: v for k, v in s.items() if k.startswith("silent_over")}),
      "...and names the long-silent ones apart, as probably retired")
f = C.focus_facts(hive, "how's node 99 doing?")
check("NOT reporting" in f, "a question naming it gets the fact beside it", f)
conv = C.Conversation()
out = conv.ask(hive, "reset the counters on node 99")
check(not out["pending"] and "NOT reporting" in out["answer"] and "Which one" not in out["answer"],
      "a command for it says it went quiet, and proposes nothing", out)
for k in (97, 98, 99):
    app.cfg.data["nodes"].pop(str(k), None)

# --- the long-prompt check behind the Test button -------------------------------
def keeps(messages, **kw):
    m = re.search(r"code word is (\S+)\.", messages[0]["content"])
    return {"role": "assistant", "content": m.group(1) if m else "ready"}


def cuts(messages, **kw):        # a server that dropped the front of the prompt
    return {"role": "assistant", "content": "ready" if len(messages) == 1 else "I don't know the code word."}


C.llm_chat = keeps
ok, note = C.context_check()
check(ok, "a server that keeps a long prompt passes the context check", note)
C.llm_chat = cuts
ok, note = C.context_check()
check(not ok and "OLLAMA_CONTEXT_LENGTH" in note, "a server that cuts it fails, and says how to fix it", note)
h = H._Capture(app, {})
h.route_post("/api/chat/test", {})
check(h.out[0].get("context_ok") is False and "8192" in h.out[0].get("context_note", ""),
      "the Test button reports a truncating server", h.out[0])

print("\n%s" % ("all passed" if not failed else "%d FAILED" % failed))
sys.exit(1 if failed else 0)
