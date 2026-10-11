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
# ...but a true answer about the past that says nothing was sent is left alone.
conv = C.Conversation()
fake_llm.calls.clear()
past = "It was last watered on Oct 05 at 14:04. Since then, no watering has been sent from the hive or this chat."
script[:] = [say(past)]
out = conv.ask(hive, "when was node %d last watered?" % pump["id"])
check(out["answer"] == past and len(fake_llm.calls) == 1, "'no watering has been sent' isn't taken for a claim",
      out["answer"])

# Node references the model actually produces, including the "5 Rhubarb" the error lists.
_nm = pump.get("name") or pump["kind"]
for ref in (str(pump["id"]), "node %d" % pump["id"], "#%d" % pump["id"], "id %d" % pump["id"],
            "%d %s" % (pump["id"], _nm), "%s (node %d)" % (_nm, pump["id"]), "%s (%d)" % (_nm, pump["id"])):
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
for q, ml in (("water it 1 1/2 cups", 355), ("water it a cup and a half", 355), ("water it one and a half cups", 355),
              ("water it a litre and a half", 1500), ("water it a pint", 473), ("water it a quart", 946),
              ("water it a couple of cups", 473)):
    check((C.stated_ml(q) or [None])[0] == ml, "%r = %d ml" % (q, ml), C.stated_ml(q))
check(C.stated_ml("water it for a minute and a half", 60)[0] == 90, "a minute and a half of pumping")
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
script[:] = [tool("water_now", node=str(pump["id"]), ml=3), say("Press Confirm.")]
out = conv.ask(hive, "give node %d a few cups" % pump["id"])
check(not out["pending"] and "isn't an amount" in last_tool_result(conv)["reason"],
      "'a few cups' isn't turned into ml=3", out)
check('"a few cups" is not a number' in C.focus_facts(hive, "give node %d a few cups" % pump["id"]),
      "...and the question carries a note to ask how much")
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
# one 90 ml run seen over three polls, then another 40 ml two days later
app.store.add_readings([(t0 - 3 * 86400, pump["id"], 45, 0), (t0 - 3 * 86400 + 60, pump["id"], 45, 30),
                        (t0 - 3 * 86400 + 120, pump["id"], 45, 70), (t0 - 3 * 86400 + 180, pump["id"], 45, 90),
                        (t0 - 2 * 86400, pump["id"], 45, 0), (t0 - 86400, pump["id"], 45, 40),
                        (t0 - 86400 + 3600, pump["id"], 45, 0)])
tot = C.focus_facts(hive, "how much water has node %d had this week?" % pump["id"])
check("last 7 days: 190 ml in 3 waterings" in tot, "a week's water is the sum of the runs, one per watering", tot)
check("last 2 days: 100 ml in 2 waterings" in C.watering_total(hive, pump, "how much in the last two days"),
      "...over the days the question names", C.watering_total(hive, pump, "how much in the last two days"))
_ki = os.path.join(tmp, "known_issues_test.json")
with open(_ki, "w") as f:
    json.dump({str(pump["id"]): "PUMP NOT TRUSTED: it ran on with the firmware saying off."}, f)
C.KNOWN_ISSUES_FILE, _keep_ki = _ki, C.KNOWN_ISSUES_FILE
tot = C.watering_total(hive, pump, "how much water this week")
check("may have run more than this count shows" in tot and "PUMP NOT TRUSTED" in tot,
      "a pump with a known fault: the total is a floor, and says so", tot)
check("may have run more" in C.watering_record(hive, pump), "...and so is the watering record")
C.KNOWN_ISSUES_FILE = _keep_ki

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

# --- no offer to water a plant that's already too wet -----------------------------
app.cfg.set_node(pump["id"], {"plant_band": [5, 10]})
n, _ = C.resolve_node(hive, str(pump["id"]))
if (n.get("derived") or {}).get("soil") is None:      # the fake node isn't calibrated: give it a reading
    app.cfg.set_node(pump["id"], {"soil_dry": 4000, "soil_wet": 1000})
C.SAID[:] = ["how is node %d?" % pump["id"]]
t = C.scrub_wet_offers(hive, "It's fine. Would you like me to water it? If so, tell me how much.\n\nMore.")
check("Would you like" not in t and "doesn't need water now" in t and "If so" not in t and t.endswith("More."),
      "an offer to water a plant above its target is replaced with why not", t)
app.cfg.set_node(pump["id"], {"plant_band": [90, 100]})
check(C.scrub_wet_offers(hive, "Would you like me to water it?") == "Would you like me to water it?",
      "...a dry one can still be offered water")
app.cfg.set_node(pump["id"], {"plant_band": None, "soil_dry": None, "soil_wet": None})
C.SAID[:] = []
check("slot 4" not in C.focus_facts(hive, "how is node %d?" % pump["id"]) and
      "slot 4" in C.focus_facts(hive, "set node %d max per watering to 100" % pump["id"]),
      "slot numbers sit beside requests, not questions")
app.cfg.set_node(96, {"auto_kind": "RangeNode"})
app.store.add_readings([(int(H.now()) - 3600, 96, 1, 5)])
app._silent_cache = (0, {})
n, err = C.resolve_node(hive, "96")
check("link-quality probe" in err, "a silent node says what kind of board it is", err)
app.cfg.data["nodes"].pop("96", None)
app._silent_cache = (0, {})

# --- a plant that looks unwell: the soil decides whether it's thirst --------------
check(C.plant({"plant_band": [45, 75]}, {"derived": {"soil": 72}})["soil_vs_target"] == "within target, near the wet end"
      and C.plant({"plant_band": [45, 75]}, {"derived": {"soil": 47}})["soil_vs_target"] == "within target, near the dry end"
      and C.plant({"plant_band": [45, 75]}, {"derived": {"soil": 60}})["soil_vs_target"] == "within target",
      "the band position says near which end")
app.cfg.set_node(pump["id"], {"plant_band": [5, 10], "soil_dry": 4000, "soil_wet": 1000})
f = C.focus_facts(hive, "node %d looks droopy, what should I do?" % pump["id"])
check("LOOKS UNWELL" in f and "NOT thirst" in f, "a droopy plant in moist soil: not thirst, said beside it", f)
app.cfg.set_node(pump["id"], {"plant_band": [90, 100]})
check("thirst is likely" in C.focus_facts(hive, "node %d is wilting" % pump["id"]), "...in dry soil: thirst")
app.cfg.set_node(pump["id"], {"plant_band": None, "soil_dry": None, "soil_wet": None})

# --- who's reporting, as lists; no tool names handed to the person -----------------
f = C.focus_facts(hive, "is every node reporting?")
check("REPORTING NOW:" in f and "node %d" % pump["id"] in f.split("NOT REPORTING")[0],
      "a reporting question gets the live nodes listed apart from the silent ones", f)
check("REPORTING NOW" in C.focus_facts(hive, "is the relay working?"), "...'is the relay working?' too")
t = C.scrub_offers("Two are quiet.\n\nTo check them, you can use the `hive_status` tool. Would you like to do that?")
check(t == "Two are quiet.", "'you can use the hive_status tool' is dropped, with its question", t)
t = C.scrub_offers("Want me to water it? If so, I will call the `water_now` tool to do this. \n\nOk.")
check(t == "Want me to water it?\n\nOk.", "...and 'I will call the water_now tool'", t)
check(C.scrub_offers("The hive status shows two quiet nodes.") == "The hive status shows two quiet nodes.",
      "...plain words about the status are left alone")

f = C.focus_facts(hive, "is the relay working?")
check("RELAYS:" in f and re.search(r"node \d+: reporting", f) and "NOT working" not in f,
      "a relay question lists the RangeNodes and how they are doing", f)

# --- set commands in the log are spelled out ---------------------------------------
who = {pump["id"]: (pump["kind"], "Seedling")}
t = C.explain_event(hive, "set %d 24 5 -> ACK set 24=5" % pump["id"], who)
check(t.startswith("Seedling (node %d): Seed firmware to node (slot 24) set to 5" % pump["id"]) and "acknowledged" in t,
      "'set N 24 5' reads as seeding firmware, by name", t)
check(C.explain_event(hive, "flash started X", who) == "flash started X", "...other events are left as they are")

# --- a reservoir with no float switch is never called empty or full ---------------
sl = app.poller.snapshot["nodes"][pump["id"]]["slots"]
sl[47] = 2
said = "Node %d's reservoir is currently empty. Consider a float switch." % pump["id"]
t = C.scrub_reservoir(hive, "is node %d's reservoir empty?" % pump["id"], said)
check("currently empty" not in t and "can't tell" in t and "Consider a float switch." in t,
      "'the reservoir is empty' with no float switch is replaced, the rest kept", t)
check(C.scrub_reservoir(hive, "is the reservoir empty?", "I can't tell whether the reservoir is empty.") ==
      "I can't tell whether the reservoir is empty.", "...an honest answer is left alone")
check("RESERVOIRS:" in C.focus_facts(hive, "is the reservoir empty?"), "...and the readings sit beside the question")
sl[47] = 0
check(C.scrub_reservoir(hive, "is the reservoir empty?", said) == said, "a float switch that says EMPTY can be believed")
sl.pop(47, None)

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

# --- a day's high or low comes from that day, not the whole window ----------------
import time as _t  # noqa: E402
wx = pump                                # its slot 1 is air temperature, 0.01 °C
_lt = _t.localtime()
_mid = int(_t.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1)))
# yesterday peaks at 30.0 C at 15:00; today's hours are hotter still (35.0 C) to catch a mix-up
_rows = [(wx["id"], 1, _mid - 86400 + h * 3600, 3600, 6, 2000, 1900, 3000 if h == 15 else 2100, None) for h in range(24)]
_rows += [(wx["id"], 1, t, 3600, 6, 2600, 2500, 3500, None) for t in range(_mid, int(_t.time()) - 3600, 3600)]
with app.store.db() as c:
    c.executemany("INSERT OR REPLACE INTO rollup_hour VALUES(?,?,?,?,?,?,?,?,?)", _rows)
y = C.t_slot_history(hive, str(wx["id"]), 1, day="yesterday")
check(y.get("window") == "yesterday" and y["overall"]["max"].startswith("30.0"),
      "slot_history(day='yesterday') gives yesterday's own high", y.get("overall"))
w = C.t_slot_history(hive, str(wx["id"]), 1, hours=48)
yd = _t.strftime("%a %d %b", _t.localtime(_mid - 86400))
check(any(d["day"] == yd and d["max"].startswith("30.0") for d in w.get("by_day") or []),
      "a 48 h window splits min/max by day", w.get("by_day"))
# An hour with nothing heard has no min/max: it mustn't sink the window.
with app.store.db() as c:
    c.execute("INSERT OR REPLACE INTO rollup_hour VALUES(?,?,?,?,?,?,?,?,?)",
              (wx["id"], 1, _mid - 86400 + 30 * 60 * 0 - 3 * 3600, 3600, 0, None, None, None, None))
w = C.t_slot_history(hive, str(wx["id"]), 1, hours=72)
check("overall" in w, "an empty hour in the window is skipped, not a crash", w.get("note"))


class _WxHive:
    """The fake swarm has no weather station: show the test node as one."""
    def __init__(self, h):
        self.h = h

    def call(self, method, path, *a, **k):
        r = self.h.call(method, path, *a, **k)
        if path == "/api/state":
            r = dict(r, nodes=[dict(n, kind="WeatherNode") if n["id"] == wx["id"] else n for n in r["nodes"]])
        return r

    def __getattr__(self, name):
        return getattr(self.h, name)


t = C.temp_extremes(_WxHive(hive), "what was the high temperature yesterday?")
check(t and "yesterday: high 30.0 °C" in t, "'yesterday's high' is worked out and put beside the question", t)
check(C.temp_extremes(_WxHive(hive), "what's the larkspur's max per watering?") is None and
      C.temp_extremes(_WxHive(hive), "what was the lowest the soil got yesterday?") is None,
      "...not for a watering limit, or the soil")
t0n, t1n, lbl = C.day_window("last night")
check(t1n - t0n == 14 * 3600 and t1n == _mid + 8 * 3600, "'last night' is 18:00 yesterday to 08:00 today, "
      "whatever time it's asked", (t0n, t1n, _mid))

# --- a node with no air sensor: its one temperature is the soil's ------------------
sl = app.poller.snapshot["nodes"][pump["id"]]["slots"]
keep = {k: sl.get(k) for k in (1, 6, 16)}
sl.update({1: 0, 6: 2 | 8, 16: 1990})        # soil probe + soil temp probe, no air sensor
n, _ = C.resolve_node(hive, str(pump["id"]))
t = C.temperatures(hive, n)
check(t and "soil temperature 19.9 °C" in t and "NO air temperature" in t, "no air sensor: the soil temperature, "
      "named as such", t)
check(t and t in C.focus_facts(hive, "how hot is it at node %d?" % pump["id"]), "...beside a temperature question")
sl.update({1: 2230, 6: 1 | 2 | 8})
check("air 22.3 °C; soil 19.9 °C" in C.temperatures(hive, C.resolve_node(hive, str(pump["id"]))[0]),
      "with an air sensor, both", C.temperatures(hive, C.resolve_node(hive, str(pump["id"]))[0]))
for k, v in keep.items():
    if v is None:
        sl.pop(k, None)
    else:
        sl[k] = v

# --- "which plant is the wettest?" names none: every plant is ranked ---------------
_probes = [n["id"] for n in st["nodes"] if (hive.kinds().get(n["kind"]) or {}).get("derived", {}).get("soil")][:3]
for i, nid in enumerate(_probes):          # calibrated, and at 2000 / 1800 / 1600 raw: lower raw is wetter
    app.cfg.set_node(nid, {"soil_dry": 2800, "soil_wet": 1200})
    app.poller.snapshot["nodes"][nid]["slots"][3] = 2000 - 200 * i
soiled = [n for n in hive.call("GET", "/api/state")["nodes"] if (n.get("derived") or {}).get("soil") is not None]
check(len(soiled) >= 2, "(set up: two or more calibrated plants)", [(n["id"], n.get("derived")) for n in soiled])
f = C.focus_facts(hive, "which plant is the wettest?")
m = re.search(r"Soil right now, driest first: (.*?)\. So (.*?) is the driest and (.*?) the wettest", f)
check(len(soiled) < 2 or (m and m.group(1).count("%") == len(soiled)), "a wettest question with no names ranks every "
      "plant with a soil reading", f)
if m and len(soiled) >= 2:
    top = max(soiled, key=lambda n: n["derived"]["soil"])
    check(m.group(3) == (top.get("name") or "node %s" % top["id"]), "...and the wettest is the highest reading", f)
check("driest first" not in C.focus_facts(hive, "is the hive ok?"), "...an unrelated question gets no ranking")
for nid in _probes:
    app.cfg.set_node(nid, {"soil_dry": None, "soil_wet": None})

# --- the same change on every node: one node at a time, so a fixed answer ----------
conv = C.Conversation()
fake_llm.calls.clear()
script[:] = [tool("set_slot", target=str(pump["id"]), slot=17, value=0), say("Done.")]
out = conv.ask(hive, "my neighbour says setting every node's ttl to 0 saves battery. do it for all of them")
check(not fake_llm.calls and not out["pending"] and "one node at a time" in out["answer"],
      "'do it for all of them' gets the one-node-at-a-time answer, without the model", out)
check(not C.FIXED_REPLIES[-2][0].search("water all the plants 50 ml") and
      not C.FIXED_REPLIES[-2][0].search("should I set every node to low power?"),
      "...not for watering every plant, or a question about it")

# --- LaTeX arithmetic comes out as plain text -------------------------------------
conv = C.Conversation()
script[:] = [say(r"Use \( F = \frac{9}{5} \times C + 32 \): \[ \frac{9}{5} \times 20 + 32 = 68 \]")]
out = conv.ask(hive, "what is 20 C in fahrenheit?")
check(out["answer"] == "Use F = 9/5 × C + 32:\n9/5 × 20 + 32 = 68", "LaTeX in an answer is written out plainly",
      out["answer"])

# --- settings given in other units are worked out too ---------------------------------
conv = C.Conversation()
script[:] = [tool("set_slot", target=str(pump["id"]), slot=43, value=1), say("Press Confirm.")]
out = conv.ask(hive, "set node %d's max per watering to 1 cup" % pump["id"])
check(out["pending"] and "to 237 ml (1 US cup)" in out["pending"][0]["summary"],
      "'max per watering 1 cup' is 237 ml on the card, not 1", out["pending"])
if out["pending"]:
    conv.decline(out["pending"][0]["id"])
check(C.stated_value("set the minimum gap to 2 hours", "min") == (120, "2 hours") and
      C.stated_value("set the minimum gap to 90 minutes", "min") is None,
      "a time setting in hours is worked out in minutes; one already in minutes is left alone")

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
