# AGENTS.md — using and contributing to Hivewire

This file is for AI agents (and anyone else) who will **use Hivewire in a
project** or **change Hivewire itself**. Read it before writing code against the
library. The [README](README.md) is the long-form explanation of *why* each
design choice exists; this file is the working guide: the mental model, the
rules that are easy to break, and how to verify a change before calling it done.

---

## 1. What Hivewire is, in one screen

A leaderless state-synchronisation library for **ESP32 swarms over ESP-NOW**
(Arduino core, tested on the ESP32-C6), with optional uplinks to a Meshtastic
LoRa node or a USB host such as a Raspberry Pi.

- **State, not commands.** A coordinator beacons the *desired state* with a
  monotonic **epoch**. Every unit adopts any higher epoch and gossips it on, so
  a node that rebooted or wandered out of range catches up on the next beacon.
  There is no retry logic for state because none is needed.
- **Trickle (RFC 6206)** keeps gossip quiet: a unit stays silent when enough
  neighbours already agree, and intervals back off while the swarm is
  consistent. The coordinator never suppresses its own beacon.
- **The beacon payload is opaque** (≤ `HIVEWIRE_MAX_STATE` bytes). The library
  owns ordering, propagation and expiry; *what the state means* is the
  application's business.
- **Slots** are how a node publishes readings and accepts writes: a typed,
  direction-gated table (`HwSlotDef`) with sample/report periods and change
  thresholds. Writes (`set`) are one-shot requests, not state — a lost one stays
  lost, by design.
- **Failsafe.** A node that stops hearing beacons, or whose state's TTL
  expires, drops to its safe state (`onSafe`). Anything a node drives must be
  released there.
- **Firmware** moves three ways, all with the same safety net: a node pulls an
  image over Wi-Fi (`HivewireOta.h`), the hive pushes one over ESP-NOW
  (`HivewireFirmware.h`), or nodes seed each other. A new image boots
  **provisionally** and reverts on its own if it doesn't rejoin the swarm.

## 2. Repository map

```
src/Hivewire.h, .cpp          core: node, beacons, Trickle, slots, relay, logs
src/HivewireOta.h             Wi-Fi pull updates + the provisional-boot rollback net
src/HivewireFirmware.h        ESP-NOW image distribution; firmware families
src/HivewireProvision.h       node number over USB after flashing (setid), radio self-test
src/HivewireErrors.h          error codes + hwErr() (GENERATED from errors/codes.json)
src/Hivewire*Uplink.h         Serial / HTTP / multi-uplink digests out of the swarm
examples/BasicNode            smallest useful node
examples/SelfTest             asserts the safety properties on live radios
examples/RangeNode            relay/range extender, LEDs off, weak-link warnings
examples/SoilNode             plant sensor: AHT20, capacitive soil probe, battery
                              (-DHW_WITH_PUMP=1: the WaterNode family, sensor + pump)
examples/PumpNode             a pump or valve alone (HivewirePump.h)
examples/MeshtasticGateway    the hive: coordinator + LoRa + USB, `dump`/`push`
examples/FirmwarePush         hive that takes an image over USB and distributes it
tools/hive_admin/             web admin for the hive's host (Pi); Flash page
tools/build_images.py         builds the generic images the Flash page writes
tools/hivewire_push.py        streams an image to the gateway's `push` command
tools/gen_errors.py           errors/codes.json -> HivewireErrors.h, ERRORS.md, admin lookup
tools/soak/                   long-running tests: hive_soak.py (host), lora_soak.py (LoRa link)
hardware/                     enclosure brief (the PCB lives in its own repo)
```

## 3. Using Hivewire in a sketch

```cpp
#include <Hivewire.h>
#include <HivewireFirmware.h>
#include <HivewireProvision.h>

HivewireNode node;
HW_FW_FAMILY("MyNode");              // images of other families are refused

static int16_t tempCenti;
static void sTemp(void *o) { memcpy(o, &tempCenti, 2); }

//  id  type    dir          sample  report  thresh min max sampler applier
static const HwSlotDef SLOTS[] = {
  {  1, HW_I16, HW_DIR_OUT,  30000, 900000,   50,  0,  0, sTemp, nullptr },
};

void setup() {
  Serial.begin(115200);
  // node id: read from NVS (see SoilNode for the Preferences pattern);
  // id 0 = unassigned -> hwprov::waitForId(...) and never join the swarm
  node.begin(nodeId, HW_ROLE_SENSOR, SLOTS, sizeof(SLOTS) / sizeof(SLOTS[0]));
}

void loop() {
  node.loop();
  // read sensors HERE, not in samplers: samplers only copy the last value
}
```

Rules that matter when writing a node:

- **Samplers only copy.** They run inside the library's scheduler; slow I/O (an
  80 ms I2C conversion, a probe settle) belongs in `loop()`, and the sampler
  reports the last result.
- **Slot ids are the application's**, but these are shared conventions across
  the bundled sketches and the admin: `20`/`21` deafen to one sender for N
  seconds (a test hook for forcing relay paths), `22` action (4 = reboot,
  5 = start a Wi-Fi OTA pull), `23` OTA arm, `24` seed this node's firmware to
  another node (RangeNode and SoilNode), `25` running firmware CRC, `26` last error (`code | subject << 16`),
  `27` errors since boot, `40`-`48` a pump (HivewirePump.h). The admin also stores the hive's
  observed hop count as pseudo-slot `250`. Don't reuse those for other meanings.
- **Node ids 1–254.** `0` is `HIVEWIRE_TARGET_ALL` on the wire and means
  "unassigned" for provisioning. Store the id in NVS and never let a new image
  override it: one image must serve every board.
- **Every unit must use the same channel** (`HW_SWARM_CHANNEL`, default 6) and
  the same `HIVEWIRE_PROTOCOL`.
- **Release outputs in `onSafe`**, and call it before any update starts
  (`ota.onBeforeUpdate`, `fw.onBeforeUpdate`) — an update is an outage.
- **Scan the radio before `node.begin()`** if you scan at all
  (`hwprov::radioSelfTest()`): a scan hops channels and would drop swarm traffic
  later.

## 4. Building and flashing

```bash
arduino-cli compile --fqbn esp32:esp32:esp32c6:CDCOnBoot=cdc \
  --library /path/to/hivewire examples/SoilNode
```

- **Always `CDCOnBoot=cdc`** in the FQBN, or `Serial` goes to UART0 and the board
  appears to print nothing. **Never** set `build.extra_flags` to force it — that
  replaces the core's flags and kills USB serial. Per-build defines go in
  `compiler.cpp.extra_flags`.
- **Target boards by MAC, never by COM/ttyACM number.** Port numbers change when
  USB re-enumerates. The C6's base MAC is in its USB serial number; confirm it
  with `esptool read_mac` before writing.
- **The easy path:** build generic images once (`python tools/build_images.py`),
  then flash and number boards from the **Flash** page (the admin's Flash tab on
  the hive's host, or `tools/flash_board.bat` / `hive_admin.py --flash-only` on
  a PC). It checks the MAC, never offers the hive gateway or any ESP32 plugged
  in when the admin started, wipes, writes, sends `setid`, and reports the
  board's radio and sensors.
- **Wipe a used board before trusting it.** A board whose saved flash data was
  stale heard *no* Wi-Fi networks on any channel and looked like dead
  hardware; after `erase_flash` it heard eleven and joined immediately.
- Pulling a USB serial port open with pyserial's defaults raises DTR/RTS, which
  **resets** a C6. Open with both low if you need to talk to a running board.
- Over-the-air pushes take the plain app image (`Sketch.ino.bin`), never
  `.merged.bin`. Push to one node (`hivewire_push.py image.bin <port> <node>`);
  a node the hive only hears through a relay is updated once a relay runs
  firmware that passes NACKs on, and one it cannot reach at all by seeding
  (`set <peer> 24 <node>`) from a confirmed same-family peer.
- A generic image (node id 0) is safe to push: a board that somehow has no
  stored id reverts to its previous image instead of waiting for `setid`
  (`HivewireOta::revertIfProvisional`).

## 5. The hive, the gateway and the admin

The gateway (`examples/MeshtasticGateway`) takes line commands over USB and,
where noted, over LoRa:

```
set <all|rN|id> <slot> <value>   write a slot
mode <m> <param> <ttl>           set the coordinator's state (example encoding)
status                           force a digest now
dump                             every node's slots, USB ONLY (the admin polls this)
log [id]                         the gateway's ring, or ask a node for its own
push <len> <crc32> [node]        arm an ESP-NOW firmware transfer (one node, or all)
```

- **`ACK set` only means the gateway SENT the write** -- one ESP-NOW broadcast,
  which a node at the edge of range misses (a soak lost 1 in 6 at -87 dBm). The
  node's own report is the confirmation. The admin re-sends a *setting* until
  the node reports it (E606 after five tries); it never repeats an *action*.
  Mark one-shot slots (reboot, arm OTA, seed) `"action": true` in `kinds.json`.
- **Commands are relayed**: `set` and log requests travel outward, log replies
  and firmware NACKs inward, each passed on once per unit (see the README,
  "Reaching a node behind a relay"). A `set` is applied once however many
  copies arrive. An add-on that defines its own reply type can relay it with
  `node.relayRaw()` from its `onRaw()` handler.
- **Meshtastic accepts one text message from a client every 2 s** and silently
  drops the rest (PhoneAPI rate limit). Anything sending several lines over the
  LoRa uplink must pace them; `MeshtasticUplink` queues and sends one per 2.5 s.

`tools/hive_admin` is the only program that may hold the gateway's port — two
readers on one tty each see half the replies. Try it without hardware:
`python3 tools/hive_admin/hive_admin.py --fake`. Node types, slot labels and
units are data, in `tools/hive_admin/kinds.json`: add an entry for a new sketch
rather than hard-coding it in the page.

## 6. Contributing

### Verification is the job, not a formality

**No change is done on one successful run.** Every bug listed in the README
survived short tests and only appeared over hours. Before calling something
finished:

1. **Test the failure paths on purpose**: pull the power mid-transfer, feed a
   corrupt image, deafen a node (`deafenTo`), unplug the sensor. Say what
   happened, not what should have happened.
2. **Soak** anything timing-related for hours where that's plausible, with the
   results checked against expectations rather than eyeballed.
3. **Use a control.** When something "stops working", re-run the thing that
   last worked before theorising about the thing that doesn't. Intermittent
   contacts and software bugs look identical from above.
4. **Measure, then write the number in the comment.** Comments in this codebase
   record the measurement that justified a constant (settle times, sample
   spreads, RSSI, the 900 s GPS timer). Keep doing that; a constant without its
   evidence gets "tidied" into a bug.
5. **Report honestly**, including what is still unresolved. The README's
   "Known rough edges" section exists for that; add to it.

`examples/SelfTest` asserts the core safety properties against live radios.
Run it after any change to `src/Hivewire.*`.

### Error codes

Every failure a user could hit gets a code, so a report can be matched to a
cause without anyone reading the code. The registry is `errors/codes.json`:

- **Add a code in the same change as the failure path** that raises it, with a
  cause and a fix written for the person holding the board. Run
  `python tools/gen_errors.py` (CI-style check: `--check`) and commit the
  generated files with it.
- **Never renumber or reuse a code**; only add. Ranges: 1xx radio, 2xx firmware,
  3xx peripherals (generic, with a subject = the slot it concerns), 4xx
  provisioning, 5xx gateway, 6xx host.
- **Keep library codes general.** Hivewire does not know it is watching soil;
  a missing sensor is `E301/<slot>`, not a soil code. Applications use
  1000–1999 and describe theirs in their `kinds.json` entry under `"errors"`.
- Raise with `hwErr(node, HW_E_..., subject, "detail")`; publish slots 26/27 with
  `hwErrSlotLast`/`hwErrSlotCount` so the admin sees codes without asking.
- The admin's **Problems** page explains every code and builds a report for a
  GitHub issue with names, locations, addresses and tokens removed (`scrub()` in
  `hive_admin.py`). Anything new the report includes must go through `scrub()`.

### Rules that protect deployed units

- **Never weaken the update safety net.** A corrupt or incomplete image must be
  refused; a new image must prove itself (rejoin the swarm) or revert. If a
  change touches `HivewireOta.h` or `HivewireFirmware.h`, re-run the corrupt,
  truncated and rollback burn tests.
- **Protocol changes bump `HIVEWIRE_PROTOCOL`.** Mixed versions ignore each
  other silently, so say so in the commit and README, and remember that deployed
  nodes update over the air — the hive and every node must be able to reach the
  new version.
- **Elapsed time:** never subtract against a timestamp another context (a radio
  callback) can move. Snapshot once, compare signed, treat negative ages as zero.
- **Diagnostics must tell the truth.** A log line that misreports its cause
  costs more than no log line; fix it before chasing what it says.
- **Don't break the hive's host.** The admin runs unattended on a Pi; changes to
  it must survive a restart, a stale clock and an unplugged gateway.

### Style

- Match the surrounding code: comment density, naming, the "why, with the
  evidence" comment style. Header-only helpers are fine for small modules.
- Commit messages explain **why** and what was verified, in plain sentences.
- Update the README in the same change when behaviour or a workflow changes.

### Repository hygiene (this repository is public)

- **Never commit secrets or site details**: Wi-Fi credentials, API tokens,
  claim codes, hostnames or IP addresses of anyone's network, passwords. Build
  credentials in with `-D` flags at compile time, never in source.
- **No AI attribution in commits or PRs** — no `Co-Authored-By` trailers or
  "generated with" lines. The maintainer is the sole contributor of record.
- Leave local working notes (handoff files, scratch builds) out of commits.

## 7. Where help is most useful

- **Tuning `trickleK`** for small swarms (it under-suppresses with 2–3 units).
- **Relaying firmware data**, not just the NACKs, so a node the hive cannot hear
  at all can be pushed to directly instead of seeded by a same-family peer.
- **Routing instead of flooding** for relayed commands, for larger swarms.
- **More than two relay hops** — only ever tested at two.
- **Sleepy sensors**: nodes today must stay awake to hear beacons and receive
  firmware; a battery node that deep-sleeps needs a design that keeps both.
- **FTM ranging** for node location awareness (not yet bench-tested).
- **New node types**: a sketch in `examples/`, a family name, a `kinds.json`
  entry, and a line in the README.
