# Hivewire

Leaderless ESP-NOW state synchronisation for ESP32 swarms, with an optional
bridge to a Meshtastic LoRa node.

Verified on ESP32-C6 hardware: `7 passed, 0 failed` from
[`examples/SelfTest`](examples/SelfTest), which asserts the safety properties
against live radios rather than in simulation.

## The idea

Most small mesh protocols send **commands** and then work hard to make delivery
reliable — retries, acknowledgements, sequence tracking, reconciliation. Over a
lossy radio that machinery is where the bugs live.

Hivewire advertises **state** instead. A coordinator continuously beacons the
desired state alongside a monotonic epoch. Every unit adopts any epoch higher
than its own and gossips it onward, so a node that rebooted, missed the change,
or wandered out of range picks it up on the next beacon. There is no retry
logic anywhere in this library, because nothing needs retrying — convergence is
the default behaviour rather than something built on top.

[Trickle (RFC 6206)](https://datatracker.ietf.org/doc/html/rfc6206) keeps that
from becoming a broadcast storm: a unit stays quiet when it has already heard
`K` neighbours agreeing with the state it holds, and intervals double while the
network is consistent. A settled swarm goes nearly silent; a state change snaps
it back to fast propagation instantly.

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

**This gates logic, not silicon.** It cannot save a pin physically wired to
something that drives it. Set pin directions once at boot and never change them.

## Failing safe

Units reach safety **on their own**:

- No beacon for `failsafeMs` (default 30 s) → `onSafe()`
- State may carry a TTL, after which it expires → `onSafe()`

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
node2 | safe: ttl expired
```

The library records its own transport events; `node.log()` is public for yours.
A coordinator fetches a node's ring with `requestLog(id)`.

The refusal entries are the highest-value ones: from outside, a refused write
and a message that never arrived look identical. Now the node says which.

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
- **If a link works, dies, then returns after you reseat something — stop
  debugging software.** Intermittent contacts produce contradictory results that
  look exactly like protocol bugs. The inverse trap is just as expensive: this
  gateway's symptom looked *exactly* like a bad contact — worked once, then
  never — and was entirely in software. What separated them was a control test,
  re-running the sketch that had worked rather than reasoning about the one that
  hadn't.

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
- **The 512-byte receive buffer can deadlock permanently.**
  `mt_protocol_check_packet()` has two paths that abandon the buffer without
  clearing it, and `mt_loop()` only ever offers the reader `PB_BUFSIZE - pb_size`
  bytes of space. Once it holds a frame that can never complete, no byte is read
  and no packet parsed, forever. Nothing looks wrong: sends still succeed and the
  node still accepts heartbeats. Watch for a non-zero fill level that stops
  changing, and reset it.

Also, `ready()` cannot be built on `mt_loop()`'s return value — in serial mode
`mt_serial_loop()` is `return true;` unconditionally, so it reports success even
with the node unplugged.

## Known rough edges

- `trickleK = 3` is a starting value, not a tuned one. With two or three units
  it will under-suppress.
- The coordinator allocates a 256-entry node table; shrink it if RAM is tight.
- A node's slot table fills at `HIVEWIRE_MAX_SLOTS`; further ids are dropped.
- Status relay (`relayHops`, default 2) is implemented and dedupes on
  `(node, msgId)`, but has only been exercised with every unit in range of every
  other. It has not been tested where a node is genuinely reachable only via a
  relay.

## License

Apache-2.0. See [LICENSE](LICENSE).
