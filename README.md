# Hivewire

Leaderless ESP-NOW state synchronisation for ESP32 swarms, with an optional
bridge to a Meshtastic LoRa node.

Verified on ESP32-C6 hardware: `7 passed, 0 failed` from
[`examples/SelfTest`](examples/SelfTest), which asserts the safety properties
against live radios rather than in simulation, plus an overnight soak driving
real LoRa and ESP-NOW traffic across a house — several hundred commands, each
checked against an expectation. That soak found eight bugs the self-test could
not, every one of which survived short tests and only appeared over hours. A
separate nine-cycle burn test of firmware distribution (see below) found seven
more the same way, and proved the one property that matters most for a unit
nobody can reach: across every single cycle, a node that received a corrupted
or interrupted image refused it and kept running, with no exceptions.

## The idea

Most small mesh protocols send **commands** and then work hard to make delivery
reliable — retries, acknowledgements, sequence tracking, reconciliation. Over a
lossy radio that machinery is where the bugs live.

Hivewire advertises **state** instead. A coordinator continuously beacons the
desired state alongside a monotonic epoch. Every unit adopts any epoch higher
than its own and gossips it onward, so a node that rebooted, missed the change,
or wandered out of range picks it up on the next beacon. **State needs no retry
logic**, because the next beacon carries the same thing — convergence is the
default behaviour rather than something built on top.

That guarantee covers state, and only state. A `set` write and a log request are
*requests*: nothing follows them carrying the same information, so a lost one
stays lost. Writes are one-shot by design; log requests are retried. Both are
spelled out below, because "no retries anywhere" would be a comfortable thing to
claim and not quite true.

[Trickle (RFC 6206)](https://datatracker.ietf.org/doc/html/rfc6206) keeps that
from becoming a broadcast storm: a unit stays quiet when it has already heard
`K` neighbours agreeing with the state it holds, and intervals double while the
network is consistent. A settled swarm quietens to one beacon per
`trickleImaxMs`; a state change snaps it back to fast propagation instantly.
**Quiet, not silent** — the coordinator is exempt from suppression, because its
beacon is what every unit's failsafe is measured against.

## The transport carries meaning, it does not define it

A beacon's payload is **opaque**. The library owns three things:

| | |
|---|---|
| **Ordering** | monotonic epoch, higher always wins |
| **Propagation** | Trickle gossip |
| **Expiry** | TTL, and falling back when beacons stop |

What the state *means* is yours. The bundled examples encode `{mode, param}` in
the first two bytes; a task-allocation application would decode the same field
as a task board. Neither requires touching the protocol.

```cpp
uint8_t desired[2] = {MODE_PATROL, 0};
coord.setState(desired, sizeof(desired), 300);   // expires in 300s

node.onState([](const uint8_t *s, uint8_t len) { /* your decoding */ });
node.onSafe([]{ /* your definition of safe */ });
```

## Slots

Units publish typed **slots**, each with its own direction, cadences and limits:

```cpp
//  id  type    dir            sample  report  thresh  min  max  sampler      applier
static const HwSlotDef SLOTS[] = {
  { 1, HW_U16, HW_DIR_OUT,     30000, 300000,     20,   0,   0, sampleLevel, nullptr },
  { 2, HW_U8,  HW_DIR_OUT,     60000, 900000,      2,   0,   0, sampleBatt,  nullptr },
  { 3, HW_U8,  HW_DIR_INOUT,       0,  60000,      0,   0,   1, sampleRelay, applyRelay },
};
```

Sample rate, report rate and change threshold are independent per slot, so a
battery reading can sample hourly while a level sensor samples every 30 seconds
and reports only when it actually moves.

## Directions, and why writes fail closed

Not everything wired to a microcontroller is both readable and writable, and
getting that wrong damages hardware. Every slot declares a direction:

| | |
|---|---|
| `HW_DIR_OUT` | published only; can never be written |
| `HW_DIR_IN` | accepts writes; never published |
| `HW_DIR_INOUT` | both — publishes the result as write confirmation |

A write is **refused** unless it clears every gate:

```
slot exists → is HW_DIR_IN → has an applier
            → type matches → length matches → value within [minVal, maxVal]
```

Nothing partially validated reaches `apply()`, and a bad value is rejected
rather than coerced. Both refusal paths are covered by the self-test.

**Writes are one-shot.** There is no retry and no acknowledgement: a `set` that
is lost on the air is simply lost. That is a deliberate consequence of
advertising state rather than commanding it — a beacon needs no retry because
the next one carries the same state, but nothing follows a write to carry it
again. If a write must land, make it a slot the coordinator beacons as state, or
read the slot back and reissue. The one exception is a log request, which is
retried until answered, because it is a request whose loss is indistinguishable
from a dead node.

**This gates logic, not silicon.** It cannot save a pin physically wired to
something that drives it. Set pin directions once at boot and never change them.

## Failing safe

Units reach safety **on their own**:

- No beacon for `failsafeMs` (default 64 s) → `onSafe()`
- State may carry a TTL, after which it expires → `onSafe()`

Two constraints make that work, both learned by soaking rather than reasoning:

**`failsafeMs` must stay well clear of `trickleImaxMs`.** A converged swarm goes
quiet on purpose, so the normal beacon gap approaches `trickleImaxMs` even when
everything is healthy, and one lost packet doubles it. The default is four
intervals; `begin()` corrects a configuration that puts them closer rather than
honouring one that cannot work.

**The coordinator never suppresses its own beacon.** Trickle's redundancy check
is right for gossip and wrong for the source: that beacon is the liveness signal
every failsafe is measured against, so suppressing it makes a swarm fall silent
*because* it converged, and every unit then drops state for exactly that reason.
Interval doubling is kept, so a settled swarm still quietens to one beacon per
`trickleImaxMs`. Quiet, not silent — that distinction is the safety property.

Nothing in the design requires a packet to arrive in order to stop. Packets
don't arrive; that's the one guarantee radio gives you.

A corollary worth stating: **losing the uplink does not stop the swarm.** Units
keep executing their last goal. For autonomous machines that is correct
behaviour, not a failure mode.

## Every unit can account for itself

A deployed node has no USB port anyone can reach — and on boards with native
USB, attaching a cable **resets the chip**, destroying the state you were trying
to inspect. So each unit keeps its last events in RAM and replays them on
request:

```
node2 | boot id=2 role=2
node2 | adopt ep=4 len=2
node2 | set 1: read-only
node2 | set 3: 9 out of range
node2 | safe: no beacon 71s
```

The library records its own transport events; `node.log()` is public for yours.
A coordinator fetches a node's ring with `requestLog(id)`.

The refusal entries are the highest-value ones: from outside, a refused write
and a message that never arrived look identical. Now the node says which.

## Updating a deployed unit

[`src/HivewireOta.h`](src/HivewireOta.h) is **opt-in and header-only** — include
it and you get WiFi firmware updates triggered over the swarm; ignore it and the
library core stays pure ESP-NOW with no dependencies.

**The swarm carries the trigger, WiFi carries the bytes.** That split is
deliberate:

| Transport | ~1 MB image | Verdict |
|---|---|---|
| WiFi | seconds | what this uses |
| ESP-NOW | minutes, on a good link | ~4000 packets, and needs the acks and windowing this library exists to avoid |
| LoRa | **65+ hours** of airtime | not viable at any speed |

Credentials are compile-time, so nothing site-specific is ever committed:

```bash
arduino-cli compile --build-property 'compiler.cpp.extra_flags=-DHW_OTA_SSID="net" -DHW_OTA_PASS="pw" -DHW_OTA_URL="http://host/fw.bin"'
```

Two gates, both there to prevent the failure that cannot be undone remotely:

- **Arming names one node.** `set <id> 23 <that same id>` then `set <id> 22 5`.
  A broadcast arm only matches the node it names, so `set all 22 5` cannot brick
  a whole swarm at once. The arm expires on its own.
- **Self-revert.** The node records "updated, unconfirmed" before rebooting. If
  the new image has not rejoined the hive within three minutes it switches the
  boot partition back and restarts. Done in application code, because the
  Arduino core does not enable the bootloader's own rollback. See
  [the safety net](#the-safety-net-under-every-update) for exactly what counts
  as rejoining; it applies to every install path, not just WiFi.

The unit also reaches its safe state *before* the radio goes down — an update is
a deliberate outage and should release anything being driven, exactly as a lost
coordinator would.

**Limits worth knowing before you rely on it.** Self-revert covers an image that
runs but cannot reach the hive, and one that crash-loops after `ota.begin()`; it
does **not** cover one that crashes *before* `ota.begin()`, because nothing is
left executing to perform the revert. Test a
build on a reachable node first. The updater is plain HTTP — HTTPS needs a
`WiFiClientSecure` and a cert. And a node that includes it grows by roughly
150 KB, which on a 1.25 MB partition is real: the RangeNode example goes from
74% to 86% full.

## Distributing firmware from the hive, with no WiFi on the nodes at all

[`src/HivewireFirmware.h`](src/HivewireFirmware.h) is the other half of the
update story, and answers a different question than `HivewireOta.h` above: what
if a node has no WiFi credentials, or no WiFi coverage at all, and the only
thing with internet access is the hive itself? [`examples/FirmwarePush`](examples/FirmwarePush)
is a hive that takes an image from **a USB cable and nothing else** — no WiFi,
no server — and pushes it into the swarm over ESP-NOW, broadcast, so one pass
updates every node at once rather than one at a time.

The image moves in small windows (12 KB), each verified by NACK before the next
one starts, so a node needs 12 KB of RAM regardless of image size, and out-of-
order chunks never have to touch flash out of order. A CRC32 covers the whole
image; a node that receives a corrupted or incomplete transfer **refuses it and
keeps running what it already had** rather than boot anything unverified.

**That refusal is the one property proven completely solid.** Across nine
separate burn-test cycles — a deliberately corrupted image, and a transfer cut
off mid-flight, both repeated with a fresh build after every fix — the node
refused or abandoned and kept running **every single time**, with zero
exceptions: no crash, no boot of a bad image, no bricked unit. That is the
property that actually matters for a node nobody can walk to, and it held
throughout, including through several real bugs found and fixed along the way
(a USB burst overrunning the host's receive path, an end-of-transfer heuristic
that trusted a single short read instead of cumulative progress, a sender that
could not tell a truly dead receiver from a temporarily quiet one and reported
false success, and a failed radio send that could busy-loop instead of backing
off).

**Full ~1.1 MB images now complete and apply, end to end.** Measured on
hardware: 1,134,192 bytes from the hive to every node at once in about 200 s
(~5.6 KB/s), each node verifying the CRC and rebooting into the new image with
its identity intact. Burn-tested with 55 deliberately injected USB losses per
run, recovered every time.

**An earlier version of this README blamed physical RF interference for
transfers that never finished. That was wrong, and how it was wrong is worth
keeping.** Every observation fit an RF story — transfers dying at random
points, one node at a time, while ordinary swarm traffic carried on normally —
and a controlled channel change even "confirmed" it by improving nothing.
Instrumenting further found software bugs, none of them radio:

1. **The host overran the gateway's USB receive FIFO.** Answering each `MORE`
   with one 1024-byte write overflows the ESP32-C6's 64-byte USB Serial/JTAG
   FIFO; bytes vanished mid-subchunk, the gateway waited out a 4 s timeout on
   *every* subchunk, and throughput sat at a metronome-steady ~230 B/s. The
   tell was the regularity: timing every prompt showed a gap of exactly 4.4 s
   after each one. Pacing the host's writes (128 B blocks, 2 ms apart) took it
   to ~5 KB/s.
2. **Receivers abandoned transfers because of an unsigned-time race.** The stall
   check computed `millis() - _lastRx` while the radio callback, on another
   task, could stamp `_lastRx` one tick *after* the clock was read — wrapping
   to ~49 days and abandoning on the spot. A per-packet counter showed the
   receiver switching itself off ~20 s in (the timeout is 90 s) while every
   later packet still arrived perfectly. It struck each node at a random
   moment, which is exactly what made it look like RF. Same trap as the
   beacon-age underflow already fixed in the core; two more siblings were found
   and fixed alongside it.
3. **The sender closed a window as soon as ANY node replied.** Nodes answer a
   poll at the same instant, the broadcasts collide, the stronger survives:
   one node's replies were lost in 28 of 93 windows, and whenever one of those
   windows had holes that node was silently left with them and refused the
   image at the end. The sender now tracks its audience — every node that has
   ever answered must answer each window — and receivers jitter their replies
   so they rarely collide at all (lost replies fell from 31 to 1 per transfer).
4. **A slow member was dropped after about a second.** Found later, by the
   rollback test below: the audience rule gave up on a silent member after four
   polls 300 ms apart, and a dropped member loses the whole update, because it
   cannot rejoin a window it missed. One node went quiet for ~1.1 s at a time,
   eight times in a single push, and ended short while its neighbour updated.
   The sender now keeps asking a known member for 5 s of silence before
   dropping it.

The USB link between host and gateway is now self-healing rather than merely
careful: each subchunk is requested as `MORE <offset> <len>` and answered with
the data plus a CRC32 over *offset and data*, so a short, corrupted, or
wrong-offset reply is discarded and re-asked instead of silently shifting the
rest of the image. The lesson generalises: when every symptom fits a physical
explanation, that is the moment to instrument harder, not to stop.

## Nodes flashing each other

A node already holds a complete, verified copy of its firmware: the partition it
booted from. [`HivewireFlashProvider`](src/HivewireFirmware.h) streams that out,
and the sender is templated on its link (`HivewireFwNodeSender` for a node,
`HivewireFwSender` for the hive, unchanged), so any node can pass its running
image to its peers over exactly the transfer the hive uses — no hive, no host,
no internet in the data path. That is what lets an update reach nodes beyond
the hive's own range: whoever got it hands it on.

In the RangeNode example it is one writable slot:

```
set 2 24 3      node 2: send your firmware to node 3
set 2 24 255    node 2: send your firmware to every node
```

Measured: 1,134,192 bytes node-to-node in ~65 s (**~17.5 KB/s, about 3× the
hive's rate** — a node reads its own flash in milliseconds, where the hive waits
on USB). The image's length comes from its own header, never the padded
partition size, and its CRC from the same function receivers check against.
Across five successive re-seeds, back and forth between two nodes, the CRC
never changed: an image handed on from node to node arrives byte-identical.

Burn-tested 8/8, including the refusals: a node will not seed itself, will not
start a second seed while one is running, will not accept an update while it is
sending one (applying it would reboot it mid-transfer), and a seed aimed at a
node that does not exist gives up cleanly with nothing rebooted.

A node declines an image identical to the one it runs (`fw: already running
<crc>` in its ring) instead of rebooting into the same bytes, and a node running
an image it has not yet confirmed refuses to seed it onward: a bad build can
only travel once it has proved itself.

**Not yet done:** seeding is triggered by command, not automatic. Hop-by-hop
spread beyond the hive's range works mechanically but has not been tested with
nodes physically out of the hive's reach.

## The safety net under every update

Whatever installed the image — WiFi, a push from the hive, a seed from a peer —
the receiver calls `ota.markPending()` before rebooting, so the next boot is
provisional. [`HivewireOta`](src/HivewireOta.h) then gives the new image two
ways to fail:

- **It never rejoins the hive** within three minutes: switch back to the
  previous partition and restart.
- **It keeps crashing.** A provisional boot is counted in NVS; on the fourth, it
  reverts without waiting. The three-minute window alone would never fire for an
  image that dies after 20 s — it restarts into the same image forever.

To confirm, the image must stay up 60 s **and** have adopted the hive's epoch,
not merely hear a neighbour. That second condition came from a failed test: a
wrong-channel image pushed to two nodes put them on the wrong channel
*together*. They heard each other (`nb=1`, not orphaned) and the old check
confirmed the broken build on both. Only the hive creates an epoch, so nodes
cut off from it — however many — cannot supply one.

Burn-tested on two nodes, each step checked against the node's own boot counter
and the CRC it reports of its running partition:

| Case | Result |
|---|---|
| Good image | applies, confirms, +1 boot |
| Identical image | declined, no reboot |
| Wrong-channel ("deaf") image | applies, then reverts on its own: +2 boots, back on the old CRC |
| Image that aborts 20 s after boot | loops 3 times, reverts: +5 boots, back on the old CRC |
| Seed of the image a peer already runs | declined, no reboot on either side |
| Push cut off at 160 KB | rejected as short, old image untouched |

## More than one uplink, and telling the hive to fetch something itself

A gateway does not have to depend on exactly one path home.
[`src/HivewireMultiUplink.h`](src/HivewireMultiUplink.h) combines any number of
`HivewireUplink` implementations into one: a command is acted on from whichever
transport delivers it first, and a status line replicates to every transport
currently up. Nothing here tries to pick the "best" uplink — redundancy is the
whole point, and it is cheap because nothing carried on this channel is bulk
data.

**The second transport is deliberately not WiFi on the gateway chip itself.**
The obvious design — give the gateway its own WiFi station connection for
internet reach — runs straight into a real, documented ESP32 limitation:
`WiFi.begin()` forces the radio onto the access point's channel, and the
swarm's ESP-NOW peers are fixed on a different one unless the AP happens to
match, which is not something to rely on when most routers can change
channels on their own. The WiFi connection succeeding would silently kill
ESP-NOW to the swarm. (`src/HivewireHttpUplink.h` and `HivewireHttpFetch.h`
still exist for the case where that conflict cannot arise — a pure
LoRa-to-internet relay with no ESP-NOW swarm at all — with the tradeoff
documented in their own headers.)

[`src/HivewireSerialUplink.h`](src/HivewireSerialUplink.h) sidesteps the
problem by construction: it is a second uplink fed over the gateway's own USB
port, from anything with its **own, separate** WiFi hardware — a Raspberry Pi
is the obvious choice. No shared radio, no conflict. The same header carries
`HivewireSerialProvider`, which feeds a transfer over that USB link using the
offset-addressed, CRC-checked `MORE <offset> <len>` protocol described above.
(`examples/FirmwarePush` still uses the older bare-`MORE` feed and has not been
moved to it.)

That second uplink is what makes a new command possible:

```
push <len> <crc32>
```

sent over *either* uplink, arms a transfer; the bytes are then expected over
the USB link from whatever is plugged in there. This is the concrete shape of
"reach the hive from anywhere with a radio, and let it do the heavy lifting
once it's home": a LoRa command sent from far away, a couple dozen bytes, arms
a push that a Pi (already having fetched the real bytes over its own internet
connection) then streams down over USB and out to the whole swarm over
ESP-NOW — none of which ever touches the LoRa link.

**Verified on hardware**, and specifically because this design needs no
external credentials to test: the multi-uplink wrapper introduces no
regression to the LoRa path (status/mode/ACK all round-tripped through it
exactly as before), and a full push arrived byte-exact over the new USB path —
the receiving node correctly refused to boot a test blob that was not a valid
firmware image and stayed running, the command channel resumed correctly the
moment the transfer ended, and the reply to a status command sent afterward
came back over **both** transports, `[uplink ch1]` and `[uplink-usb]`, with
identical content — the multi-uplink replication working exactly as intended.

## Roles

`HW_ROLE_SENSOR / ACTUATOR / BOT / RELAY` allow group addressing without
knowing node ids.

Deliberate asymmetry: **beacons are role-blind.** A safety posture must reach
every unit at once, so role targeting exists only on writes.

## The Meshtastic bridge

[`examples/MeshtasticGateway`](examples/MeshtasticGateway) relays a swarm digest
over LoRa through a stock Meshtastic node:

```
phone / remote radio <--LoRa--> Meshtastic node <--UART--> gateway <--ESP-NOW--> swarm
```

Set the node's Serial Module to **`PROTO`** mode at 115200. Not `TEXTMSG`:
PROTO exposes the full protobuf client API, which is the only way to choose a
channel per message *and* see which channel an inbound command arrived on. That
second part is the security property — commands not on the private channel are
refused, and the host node keeps its place on the public mesh.

The uplink carries changes and periodic digests, never a per-node stream:

```
HW up=10 ok=9 ep=47 m=3 flt=1
D1 2.1=412 2.2=88 3.1=395
```

`ok` against `up` is the number that matters — how many units have actually
converged, so a straggler is visible without polling. Commands back:

```
mode <n> [param] [ttl]            posture; reaches every unit
set <all|rN|id> <slot> <value>    write a slot
status                            force a digest
log [nodeId]                      replay a unit's diagnostic ring
```

Meshtastic is a dependency of **one file**. The library core is pure ESP-NOW and
never includes it, so a LoRaWAN or cellular uplink is a new implementation of
`HivewireUplink`, not a fork.

## Install

Clone into your Arduino `libraries/` directory, or with `arduino-cli`:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32c6 --library /path/to/hivewire examples/BasicNode
```

Requires the ESP32 Arduino core (tested against 3.3.8). Every unit must use the
same `HwConfig::channel`.

## Notes from bringing this up

Things that cost real time, in case they save you some:

- **Mind the strapping pins,** but don't over-avoid them. On the ESP32-C6
  SuperMini `IO4`–`IO6`, `IO8`/`IO15` (LEDs) and `IO9` (boot mode) all carry
  boot-time meaning. `IO8` is fine as a **UART RX** — an idle UART line sits
  high, which is what the strap wants anyway, and the LED on that net does not
  degrade it. `IO9` must be an **output**: driven low at reset it enters
  download mode instead of running your sketch.
- **Enable USB CDC through the board menu,** `--fqbn esp32:esp32:esp32c6:CDCOnBoot=cdc`.
  Without it `Serial` is UART0 on other pins and your sketch appears to boot and
  print nothing. Do not reach for `--build-property build.extra_flags=...` to
  define `ARDUINO_USB_CDC_ON_BOOT` by hand: that *replaces* the core's flags
  rather than adding to them, and silently kills USB serial.
- **A Meshtastic node serves one API client at a time.** Attaching a second over
  USB stops packets reaching your gateway.
- **Check what else on the node owns your UART pin.** A Meshtastic node with no
  GPS still runs its GPS subsystem: it searched for 900 s, gave up, moved the
  GPS to `HARDSLEEP`, and pulled the pad low — the pad carrying the node's
  serial TX. The node went on logging `Config Send Complete` into a dead wire,
  so every layer above looked healthy. `position.gps_mode = NOT_PRESENT` fixes
  it. The tell was the 15-minute period: anything that survives exactly N
  minutes and dies is a timer, not a loose connection.
- **If a link works, dies, then returns after you reseat something — stop
  debugging software.** Intermittent contacts produce contradictory results that
  look exactly like protocol bugs. The inverse trap is just as expensive: this
  gateway's symptom looked *exactly* like a bad contact — worked once, then
  never — and was entirely in software. What separated them was a control test,
  re-running the sketch that had worked rather than reasoning about the one that
  hadn't.
- **Never compute elapsed time against a value another context can move.**
  `millis() - _lastBeacon` looks harmless until you notice the timestamp is
  written from a radio callback. When one lands mid-calculation the subtraction
  wraps to ~49 days, so a node concluded it had heard nothing for seven weeks at
  the exact instant a packet proved it alive. Two separate bugs of this shape
  turned up in one night. Snapshot once, compare **signed**, and treat a
  negative age as zero.
- **Fix a lying diagnostic before chasing what it reports.** The failsafe path
  re-evaluated its condition to choose a log message, so beacon loss was
  reported as a TTL expiry — for a TTL never set. That cost twenty minutes
  chasing a phantom. Once it told the truth it printed `no beacon 4294967s`,
  and the real bug was obvious in seconds.
- **Soak before believing.** Every bug on this page survived short tests and
  died in long ones. A three-minute check will confirm almost any broken thing
  is working.
- **Scan before trusting a default ESP-NOW channel.** 6 is the library's
  default and the single most common factory default for consumer WiFi
  routers, which makes it a bad choice for a broadcast protocol with no
  MAC-layer retry: a collision on a shared channel is not survived the way a
  unicast client's would be. `WiFi.scanNetworks()` on one board is enough to
  see what is actually occupied nearby; `HW_SWARM_CHANNEL` (a compile-time
  define, every unit must agree) lets you act on it without touching library
  internals. One caution from testing it: switching to a channel with zero
  WiFi neighbours did **not** meaningfully change a reliability problem that
  turned out to be USB-noise-shaped (see the firmware distribution section) --
  co-channel WiFi congestion and broadband RF noise are different problems,
  and moving off a busy channel only fixes the first one.

### Two defects in Meshtastic-arduino worth knowing about

Both are in the receive path, both present as "transmits fine, hears nothing",
and [`MeshtasticUplink.h`](examples/MeshtasticGateway/MeshtasticUplink.h) works
around both from outside — the library is GPL-3.0 and cannot be vendored here.

- **`handle_config_complete_id()` calls a null callback.** It nulls
  `node_report_callback` when a handshake completes, then calls it with no null
  check if a later `config_complete` arrives — which a node sends every time it
  reboots. On RISC-V that is an instruction fetch at `0x0`: an immediate panic.
  The reboot then cuts a UART frame in half and desyncs the *node's* parser too,
  so the link stays dead afterwards. Re-arm the pointer directly; calling
  `mt_request_node_report()` to re-arm sends another `want_config` and loops.
- **The 512-byte receive buffer looks like it can deadlock permanently.**
  `mt_protocol_check_packet()` has two paths that abandon the buffer without
  clearing it, and `mt_loop()` only ever offers the reader `PB_BUFSIZE - pb_size`
  bytes of space. Once it held a frame that could never complete, no byte would
  be read and no packet parsed again. **Read from the source, never observed:**
  the guard against it has not once fired on hardware here, and the deafness it
  was written to explain turned out to be the GPS problem above. It is cheap
  insurance against a real-looking hazard, not a diagnosed bug — treat it as
  such, and don't let its presence talk you out of looking elsewhere.

Also, `ready()` cannot be built on `mt_loop()`'s return value — in serial mode
`mt_serial_loop()` is `return true;` unconditionally, so it reports success even
with the node unplugged.

## Known rough edges

- `trickleK = 3` is a starting value, not a tuned one. With two or three units
  it will under-suppress.
- The coordinator allocates a 256-entry node table; shrink it if RAM is tight.
- A node's slot table fills at `HIVEWIRE_MAX_SLOTS`; further ids are dropped.
- Relay has now been exercised where a node really was reachable only through
  another — a unit on the far side of a house, deafened to the coordinator, so
  `coordinator → relay → node` was the only path. It adopted two successive
  epochs it could only have heard via gossip, and its slots kept arriving via
  the relay; the census held at `up=2 ok=2` throughout. Still only ever **two**
  hops and three units.

## License

Apache-2.0. See [LICENSE](LICENSE).

One caveat if you redistribute binaries. The library core has no third-party
dependencies, but [`examples/MeshtasticGateway`](examples/MeshtasticGateway)
links [Meshtastic-arduino](https://github.com/meshtastic/Meshtastic-arduino),
which is **GPL-3.0**. Apache-2.0 source may be combined into a GPL-3.0 work, so
this is fine — but a binary you build from that example is covered by GPL-3.0,
not Apache-2.0. The source in this repository stays Apache-2.0 either way, and
nothing outside that one example is affected. That separation is deliberate:
Meshtastic is a dependency of a single file, so a differently-licensed uplink is
a new `HivewireUplink`, not a fork.
