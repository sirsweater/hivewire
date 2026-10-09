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

print("\n%s" % ("all passed" if not failed else "%d FAILED" % failed))
sys.exit(1 if failed else 0)
