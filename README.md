# Hivewire

Leaderless ESP-NOW state synchronisation for ESP32 swarms, with an optional
bridge to a Meshtastic LoRa node.

> **Status: pre-release, unproven on hardware.** Both examples compile for
> `esp32:esp32:esp32c6`. Nothing here has yet been run on real radios. Treat it
> as a design you can read, not a library you should deploy.

## The idea

Most small mesh protocols send **commands** and then work hard to make delivery
reliable — retries, acknowledgements, sequence tracking, reconciliation. Over a
lossy radio that machinery is where the bugs live.

Hivewire sends **state** instead. A coordinator continuously advertises the
desired state alongside a monotonic epoch:

```
epoch=47  mode=3  param=0
```

Every unit adopts any epoch higher than its own and gossips it onward. A node
that rebooted, missed the change, or wandered out of range simply picks it up on
the next beacon. There is no retry logic anywhere in this library, because
nothing needs retrying — convergence is the default behaviour rather than
something built on top.

[Trickle (RFC 6206)](https://datatracker.ietf.org/doc/html/rfc6206) keeps that
from becoming a broadcast storm. A unit stays quiet when it has already heard
`K` neighbours agreeing with the state it holds, and intervals double while the
network is consistent. A settled swarm goes nearly silent; a state change snaps
it back to fast propagation immediately.

## Slots

The transport does not know what it carries. A unit declares typed **slots**,
each with its own direction, cadences, and limits:

```cpp
//  id  type    dir           sample   report   thresh  min  max  sampler      applier
static const HwSlotDef SLOTS[] = {
  { 1, HW_U16, HW_DIR_OUT,     30000,  300000,      20,   0,   0, sampleLevel, nullptr },
  { 2, HW_U8,  HW_DIR_OUT,     60000,  900000,       2,   0,   0, sampleBatt,  nullptr },
  { 3, HW_U8,  HW_DIR_INOUT,       0,   60000,       0,   0,   1, sampleRelay, applyRelay },
};
```

Sample rate, report rate and change threshold are independent per slot, so a
battery reading can sample hourly while a level sensor samples every 30 seconds
and only reports when it actually moves.

Slot ids are an application-level contract between you and your own decoder.
Nothing in the library interprets them.

## Directions, and why writes fail closed

Not everything wired to a microcontroller is both readable and writable, and
getting that wrong can damage hardware. Every slot declares a direction:

| | |
|---|---|
| `HW_DIR_OUT` | published only; can never be written |
| `HW_DIR_IN` | accepts writes; never published |
| `HW_DIR_INOUT` | both — publishes the resulting value as write confirmation |

A write is **refused** unless it clears every gate:

```
slot exists → is HW_DIR_IN → has an applier
            → type matches → length matches → value within [minVal, maxVal]
```

Nothing partially validated reaches `apply()`, and a bad value is rejected
rather than coerced or clamped.

**This gates logic, not silicon.** It cannot save a pin that is physically wired
to something that drives it. Set pin directions once at boot and never change
them at runtime; use series resistors on anything leaving the board.

## Failing safe

Mode `0` always means safe, and units reach it **on their own**:

- No beacon for `failsafeMs` (default 30 s) → `HW_MODE_SAFE`
- A commanded state may carry a TTL, after which it expires to safe

Nothing in the design requires a packet to arrive in order to stop. Packets
don't arrive; that's the one guarantee radio gives you.

A corollary worth stating: **losing the uplink does not stop the swarm.** Units
keep executing their last goal. For autonomous machines that is the correct
behaviour, not a failure mode.

## Roles

`HW_ROLE_SENSOR`, `HW_ROLE_ACTUATOR`, `HW_ROLE_BOT`, `HW_ROLE_RELAY` allow group
addressing without knowing node ids.

There is a deliberate asymmetry here: **beacons are role-blind.** A safety
posture must reach every unit at once, so role targeting exists only on writes,
where per-group configuration belongs.

## The Meshtastic bridge

`examples/MeshtasticGateway` relays a swarm digest over LoRa through a stock
Meshtastic node connected by UART:

```
phone / remote radio <--LoRa--> Meshtastic node <--UART--> gateway <--ESP-NOW--> swarm
```

Set the Meshtastic node's Serial Module to `TEXTMSG` at 115200. Text rather than
binary means a stock phone app can read digests and issue commands with no
custom software at either end.

The uplink carries **changes and periodic digests, never a per-node stream**. A
LoRa packet costs the better part of a second of shared airtime; one digest line
covers the whole swarm:

```
HW up=10 ok=9 ep=47 m=3 flt=1
D1 2.1=412 2.2=88 3.1=395 3.2=91
```

`ok` against `up` is the number that matters — it says how many units have
actually converged on the current epoch, so a straggler is visible without
polling anything.

Commands back:

```
mode <n> [param] [ttl]            posture; reaches every unit
set <all|rN|id> <slot> <value>    write a slot
status                            force a digest now
```

## Install

Clone into your Arduino `libraries/` directory, or with `arduino-cli`:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32c6 --library /path/to/hivewire examples/BasicNode
```

Requires the ESP32 Arduino core (tested against 3.3.8). Every unit must use the
same `HwConfig::channel`.

## Choosing pins

Avoid strapping and boot-duty pins. On the ESP32-C6 SuperMini that rules out
`IO4`/`IO5`/`IO6`, plus `IO8` and `IO15` (onboard LEDs) and `IO9` (boot mode) —
`IO0`–`IO3` are the safe picks.

## Known rough edges

- **Untested on hardware.** See the status note above.
- `trickleK = 3` is a starting value, not a tuned one. With only two or three
  units it will under-suppress and chatter more than necessary.
- The coordinator allocates a 256-entry node table; shrink it if your swarm is
  small and RAM is tight.
- A node's slot table fills at `HIVEWIRE_MAX_SLOTS`; further ids are dropped
  rather than evicting older ones.

## License

Apache-2.0. See [LICENSE](LICENSE).
