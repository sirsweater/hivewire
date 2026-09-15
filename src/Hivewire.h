// Hivewire -- a leaderless ESP-NOW state-sync layer for ESP32 swarms.
//
// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0
//
// The transport is agnostic to what it carries. A unit declares a set of typed
// "slots"; the network moves them without interpreting any of them. Slot ids
// are an application-level contract between you and your own decoder.
//
// State is ADVERTISED, not commanded. A coordinator continuously beacons the
// desired state with a monotonic epoch; every unit adopts any epoch higher than
// its own and gossips it onward. A unit that rebooted or was out of range
// converges on its own, so there is no retry logic, no acknowledgement
// tracking, and no reconciliation code anywhere in this library.
//
// Trickle (RFC 6206) keeps that from becoming a broadcast storm: a unit stays
// quiet when it already heard K neighbours agreeing with the state it holds,
// and intervals double while the network is consistent. A settled swarm is
// nearly silent; a state change snaps it back to fast propagation at once.

#pragma once
#include <Arduino.h>
#include <stdint.h>

#define HIVEWIRE_VERSION_MAJOR 0
#define HIVEWIRE_VERSION_MINOR 1

#define HIVEWIRE_MAGIC      0x5B
#define HIVEWIRE_PROTOCOL   5
#define HIVEWIRE_MAX_STATE  16   // application payload carried by a beacon
#define HIVEWIRE_MAX_PAYLOAD 240  // ESP-NOW caps near 250; leave headroom
#define HIVEWIRE_MAX_SLOTS   24   // per node, coordinator-side storage
#define HIVEWIRE_TARGET_ALL  0    // MSG_SET addressed to every node

// ---------------------------------------------------------------------------
// Wire format
// ---------------------------------------------------------------------------

enum HwMsgType : uint8_t {
  HW_MSG_BEACON = 1,  // coordinator -> all: desired state, gossiped by everyone
  HW_MSG_STATUS = 2,  // node -> all: health + slot values
  HW_MSG_SET    = 3,  // coordinator -> node(s): write into IN/INOUT slots
  HW_MSG_LOGREQ = 4,  // coordinator -> node: replay your diagnostic ring
  HW_MSG_LOGRSP = 5,  // node -> coordinator: ring entries
};

enum HwSlotType : uint8_t {
  HW_U8 = 1, HW_I8, HW_U16, HW_I16, HW_U32, HW_I32, HW_F32,
};

// Not everything wired to a microcontroller is both readable and writable.
// Declaring direction is what lets the library refuse a write that would drive
// a pin the wrong way.
enum HwSlotDir : uint8_t {
  HW_DIR_OUT   = 1,  // node publishes it
  HW_DIR_IN    = 2,  // node accepts writes; never published
  HW_DIR_INOUT = 3,  // both; publishes the resulting value as confirmation
};

// Roles allow group addressing without knowing node ids. Deliberate asymmetry:
// beacons are role-blind so a safety posture always reaches every unit at once.
// Role targeting exists only on HW_MSG_SET.
enum HwRole : uint8_t {
  HW_ROLE_ANY = 0, HW_ROLE_SENSOR, HW_ROLE_ACTUATOR, HW_ROLE_BOT, HW_ROLE_RELAY,
};

// The library has NO concept of a "mode". A beacon carries an opaque
// application payload and deciding what it means is the application's job --
// exactly as it already is for slots. The bundled examples happen to encode
// {mode, param} in the first two bytes, but that is their convention, not the
// protocol's.
//
// What the library does own is ORDERING (monotonic epoch, higher wins),
// PROPAGATION (Trickle gossip) and EXPIRY (TTL, plus falling back when beacons
// stop). Those are transport properties. Meaning is not.
//
// The safe state is likewise the application's: on TTL expiry or beacon loss
// the library guarantees onSafe() fires, and the application decides what
// reaching safety involves.

#define HW_FLAG_SENSOR_FAIL 0x01
#define HW_FLAG_LOW_BATT    0x02
#define HW_FLAG_NO_COORD    0x04

struct __attribute__((packed)) HwHeader {
  uint8_t magic;
  uint8_t version;
  uint8_t type;
  uint8_t srcId;
};

// Followed by exactly `len` bytes of application payload.
struct __attribute__((packed)) HwBeacon {
  HwHeader h;
  uint32_t epoch;    // monotonic; higher always wins
  uint16_t ttlSecs;  // 0 = no expiry
  uint8_t  hops;     // diagnostic only; not used for routing
  uint8_t  len;      // application payload length, <= HIVEWIRE_MAX_STATE
};

// HW_MSG_STATUS and HW_MSG_SET both carry slotCount records, each a HwSlotRec
// followed by exactly rec.len value bytes.
// No "mode" field: the epoch alone says whether a node has converged, which is
// all the transport needs. An application wanting to report what it is actually
// doing publishes that as a slot, where it belongs.
//
// msgId + hops exist so status can be RELAYED. Beacons gossip outward, so a
// distant node adopts state fine -- but without relay its readings never get
// back, and the coordinator sees a swarm smaller than it is. msgId lets a relay
// drop duplicates; hops bounds how far one report travels.
struct __attribute__((packed)) HwStatusHdr {
  HwHeader h;
  uint32_t epoch;
  uint16_t msgId;      // unique per originating report, for dedup
  uint8_t  hops;       // incremented by each relay
  uint8_t  role;
  uint8_t  flags;
  uint8_t  neighbors;
  uint8_t  slotCount;
};

struct __attribute__((packed)) HwSetHdr {
  HwHeader h;
  uint8_t targetId;
  uint8_t targetRole;
  uint8_t seq;
  uint8_t slotCount;
};

struct __attribute__((packed)) HwSlotRec {
  uint8_t id;
  uint8_t type;
  uint8_t len;
};

// ---------------------------------------------------------------------------
// Diagnostic ring
//
// Every unit keeps a short history of its own and can be asked to replay it
// over the air. A deployed node has no USB port anyone can reach -- and on a
// board with native USB, attaching a cable RESETS it, destroying the state you
// were trying to inspect. A unit that cannot account for itself remotely
// cannot be debugged once it leaves the bench.
//
// RAM only, deliberately: flash has write endurance, and a ring that survives
// reboots would buy the one case (why did it restart) at the cost of wearing
// out the part. Log the boot instead and accept losing what came before it.
// ---------------------------------------------------------------------------
#define HIVEWIRE_LOG_LINES 8
#define HIVEWIRE_LOG_WIDTH 40

struct __attribute__((packed)) HwLogReq {
  HwHeader h;
  uint8_t targetId;    // HIVEWIRE_TARGET_ALL, or one node
};

// Followed by `count` entries, each a length byte then that many chars.
struct __attribute__((packed)) HwLogRsp {
  HwHeader h;
  uint8_t count;
};

uint8_t hwSlotTypeLen(uint8_t type);
int32_t hwSlotAsInt(uint8_t type, const uint8_t *raw);

// ---------------------------------------------------------------------------
// Slot declaration
// ---------------------------------------------------------------------------
//
//   reportPeriodMs  0 -> only sent when it changes past changeThreshold
//   changeThreshold 0 -> any change counts
//   minVal/maxVal   enforced on writes; ignored for HW_DIR_OUT slots
//
// A slot with a null apply() can never be written, whatever its dir says.

typedef void (*HwSampler)(void *out);
typedef void (*HwApplier)(const void *in);

struct HwSlotDef {
  uint8_t   id;
  uint8_t   type;
  uint8_t   dir;
  uint32_t  samplePeriodMs;
  uint32_t  reportPeriodMs;
  uint32_t  changeThreshold;
  int32_t   minVal;
  int32_t   maxVal;
  HwSampler sample;
  HwApplier apply;
};

// Coordinators cannot learn the type of an HW_DIR_IN slot from traffic, because
// those are never published. Declare them.
struct HwWritableSlot {
  uint8_t id;
  uint8_t type;
};

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

struct HwConfig {
  uint8_t  channel        = 6;      // must match on every unit
  uint32_t trickleIminMs  = 500;
  // A settled swarm beacons this often. It also decides how many chances a node
  // gets to hear ANYTHING before failsafeMs expires, which is the number that
  // actually matters. At 16 s against a 64 s failsafe a node had four chances,
  // so three consecutive losses disarmed it -- and on a channel dropping ~30%
  // of frames that is not rare. Measured: seven failsafes in two hours on a node
  // whose MEDIAN RSSI was -71 dBm. Good signal, lossy channel; 2.4 GHz is shared
  // with WiFi and with whatever sits nearby (a node beside a PC saw a third the
  // beacon rate of one across the house, USB 3 being a well-known broadband
  // noise source at this frequency).
  //
  // At 8 s a node gets eight chances instead of four. An ESP-NOW beacon is ~30
  // bytes; the extra traffic is nothing set against disarming a machine because
  // a few packets went missing.
  uint32_t trickleImaxMs  = 8000;
  uint8_t  trickleK       = 3;      // suppress once this many neighbours agree
  // No beacon for this long -> onSafe().
  //
  // MUST stay well clear of trickleImaxMs. A converged swarm deliberately goes
  // quiet: intervals double to trickleImaxMs, so the normal gap between beacons
  // approaches 16 s even when everything is perfect, and a single lost packet
  // makes it 32 s. At the old default of 30 s that tripped failsafe on a
  // healthy network -- observed twice in one hour of soaking, a node dropping
  // its state and recovering seconds later with nothing actually wrong.
  //
  // Spurious failsafe is not a harmless false alarm. It is an actuator
  // releasing, and on a machine that is the expensive direction to be wrong in.
  // Eight intervals at the default trickleImaxMs, so seven consecutive losses
  // are tolerated before a unit gives up. Sized from measured loss, not taste.
  uint32_t failsafeMs     = 64000;
  uint32_t minTxGapMs     = 2000;   // floor on transmit rate
  // Say something at least this often even with nothing to report.
  //
  // Without it the quietest, healthiest units are the ones that vanish. A node
  // whose readings are stable crosses no thresholds, so it transmits nothing
  // and the coordinator marks it stale -- while a node on a marginal link
  // rattles its readings, reports constantly and looks perfectly present.
  // Observed exactly that: the near node appeared in 27 digests and the distant
  // one in 78, and half of all censuses reported a swarm one unit smaller than
  // it was. Keep this well under the coordinator's staleness window; an empty
  // status still carries epoch, role, flags and neighbour count, which is
  // everything "up" and "ok" are computed from.
  uint32_t statusKeepaliveMs = 45000;

  // How many times a status report may be relayed onward. 0 disables relaying
  // entirely, which is right when every unit can hear the coordinator: each
  // extra hop multiplies airtime, and a swarm in one room does not need it.
  uint8_t  relayHops      = 2;
  uint32_t relayJitterMs  = 120;    // spread relays so neighbours do not collide
};

// ---------------------------------------------------------------------------
// Uplink
// ---------------------------------------------------------------------------
//
// The long-haul link out of the swarm. Deliberately tiny and deliberately not
// Meshtastic: this library's core is pure ESP-NOW and must stay that way, so a
// dead or diverging uplink project costs you ONE implementation file rather
// than the protocol.
//
// Implementations live in examples/ (or your own sketch), never in src/, so the
// library never acquires a dependency on any particular radio stack. See
// examples/MeshtasticGateway for one built on the Meshtastic client API.
//
// Contract:
//   begin()  once, from setup(). false = unusable.
//   loop()   serviced every pass; may do connection work.
//   ready()  true once it can actually carry traffic.
//   send()   one short line. Implementations may drop when !ready().
//   onCommand() delivers inbound lines that the transport considers
//               TRUSTED -- an implementation is responsible for rejecting
//               anything from an unauthenticated source before calling back.

class HivewireUplink {
 public:
  typedef void (*CommandCallback)(const char *line);

  virtual ~HivewireUplink() {}
  virtual bool begin() = 0;
  virtual void loop() = 0;
  virtual bool ready() = 0;
  virtual void send(const char *line) = 0;
  virtual void onCommand(CommandCallback cb) = 0;
};

// ---------------------------------------------------------------------------
// Node
// ---------------------------------------------------------------------------

class HivewireNode {
 public:
  // Fires when the node adopts a NEW state -- a higher epoch whose payload
  // differs from the one it currently holds. Not called for re-advertisements
  // of state already held.
  typedef void (*StateCallback)(const uint8_t *state, uint8_t len);
  // Fires when the node gives up its state on its own: beacons stopped, or the
  // TTL expired. The application decides what reaching safety involves; the
  // library only guarantees this is called. Never depends on a packet arriving.
  typedef void (*SafeCallback)(void);

  bool begin(uint8_t nodeId, uint8_t role,
             const HwSlotDef *slots, uint8_t slotCount,
             const HwConfig &cfg = HwConfig());
  void loop();
  void onState(StateCallback cb) { _stateCb = cb; }
  void onSafe(SafeCallback cb)   { _safeCb = cb; }

  const uint8_t *state() const { return _state; }
  uint8_t  stateLen() const { return _stateLen; }
  uint32_t epoch() const { return _epoch; }
  uint8_t  neighbors() const;
  bool     orphaned() const;
  // Underflow-safe; see the definition. Public because the age is worth
  // publishing as telemetry -- a climbing value is the earliest warning that a
  // link is degrading, long before anything actually fails.
  uint32_t beaconAgeMs() const;

  // Signal strength of received traffic, in dBm. `last` answers "is it
  // reachable"; `worst` answers "is there any margin", which is the one that
  // decides whether a placement survives someone shutting a door. 0 = nothing
  // heard yet.
  int8_t   lastRssi()  const { return _rssiLast; }
  int8_t   worstRssi() const { return _rssiWorst; }
  void     resetRssi() { _rssiLast = 0; _rssiWorst = 0; }

  // Beacons actually received, and how long since the last one. Inferring
  // these from neighbors() instead measures "a neighbour was visible", which
  // is a different and much blunter thing -- it cannot see packet loss while
  // the link is still nominally up, which is the whole edge-of-range signature.
  uint32_t beaconsRx() const { return _beaconsRx; }
  uint32_t msSinceBeacon() const { return _lastBeacon ? beaconAgeMs() : 0; }
  bool     everHeardBeacon() const { return _lastBeacon != 0; }
  void     resetBeaconCount() { _beaconsRx = 0; }

  // Test aid: ignore everything from one node, to force a topology on a bench
  // where every unit can hear every other. Without it multi-hop relay cannot be
  // exercised indoors at all, because there is never a second hop to take.
  //
  // ALWAYS expires, capped, and never persisted across a reboot. A filter that
  // could outlive its experiment would be able to strand a deployed unit --
  // deafen a node to the only neighbour that can reach it and the sole remedy
  // is to walk over and reflash it. Expiry makes the worst case a wait rather
  // than a trip. Passing id 0 clears it immediately.
  static const uint16_t DEAF_MAX_SECS = 1800;      // hard 30-minute ceiling
  void     deafenTo(uint8_t srcId, uint16_t secs);
  uint8_t  deafTarget() const;
  uint16_t deafSecsLeft() const;

  // Forget the adopted epoch, so the next beacon of ANY epoch is accepted.
  //
  // The escape hatch for the one way a node can be permanently stranded.
  // Adoption requires a strictly higher epoch, and a coordinator that reboots
  // restarts its counter at 1. It normally climbs back above the swarm by
  // reading the epochs nodes report -- but that needs the node's status to
  // reach it. A node whose status does not get back is deaf to its coordinator
  // for good, and no packet can fix it, because rejecting the packet is the
  // bug.
  //
  // Policy belongs to the caller, not here. The sane trigger is a node that has
  // already failed safe and stayed orphaned a long time: it is holding nothing,
  // so there is no state to lose, and accepting a low epoch is strictly better
  // than being unreachable. Call it too eagerly and a node will flap between a
  // live coordinator and a stale one.
  void forgetEpoch();

  // Append to this unit's diagnostic ring. The library records its own
  // transport events here too; applications can add their own.
  void log(const char *fmt, ...);

  // Called from the ESP-NOW receive callback; not for application use.
  void _ingest(const uint8_t *data, int len);
  void _noteRssi(int8_t rssi);

 private:
  struct SlotState {
    uint8_t  raw[4];
    bool     valid, dirty;
    uint32_t lastSample, lastReport;
  };

  void sendBeacon(uint8_t hops);
  void sendStatus();
  void handleSet(const uint8_t *data, int len);
  void handleLogReq(const uint8_t *data, int len);
  void serviceLogRsp(uint32_t now);
  // Small frames survive a marginal link far better than one big one, and the
  // ring is wanted precisely when the link is marginal.
  static const uint8_t  LOGRSP_LINES_PER_PKT = 2;
  static const size_t   LOGRSP_SOFT_MAX      = 96;
  static const uint32_t LOGRSP_GAP_MS        = 150;
  void maybeRelay(const uint8_t *data, int len);
  void serviceRelay(uint32_t now);
  bool seenBefore(uint8_t src, uint16_t msgId);
  void trickleReset();
  void serviceTrickle(uint32_t now);
  int  findSlot(uint8_t id) const;

  const HwSlotDef *_slots = nullptr;
  SlotState *_st = nullptr;
  uint8_t _slotCount = 0;
  uint8_t _id = 0, _role = 0;
  HwConfig _cfg;
  StateCallback _stateCb = nullptr;
  SafeCallback  _safeCb = nullptr;

  uint32_t _epoch = 0, _adoptedAt = 0, _lastTx = 0;
  volatile uint32_t _lastBeacon = 0;   // written from the ESP-NOW callback
  uint8_t  _state[HIVEWIRE_MAX_STATE] = {0};
  uint8_t  _stateLen = 0;
  bool     _holding = false;      // have we adopted anything we must give up?
  uint16_t _ttl = 0;

  uint32_t _tInterval = 0, _tStart = 0, _tFireAt = 0;
  uint8_t  _tCount = 0;
  bool     _tScheduled = false;

  uint32_t _seen[256];

  int8_t   _rssiLast = 0, _rssiWorst = 0;
  uint32_t _beaconsRx = 0;
  uint8_t  _deafId = 0;
  uint32_t _deafUntil = 0;

  char    _log[HIVEWIRE_LOG_LINES][HIVEWIRE_LOG_WIDTH];
  uint8_t _logHead = 0;
  uint8_t _logCount = 0;

  // Recently relayed reports, so a report crossing a loop dies instead of
  // circulating. Small on purpose: it only has to outlive one propagation.
  static const uint8_t RELAY_SEEN = 16;
  struct { uint8_t src; uint16_t id; } _seenMsg[RELAY_SEEN];
  uint8_t  _seenHead = 0;
  uint16_t _msgSeq = 0;
  uint8_t  _logRspLeft = 0;      // ring lines still to send, oldest first
  uint32_t _logRspAt = 0;

  uint8_t  _relayBuf[HIVEWIRE_MAX_PAYLOAD];
  uint16_t _relayLen = 0;
  uint32_t _relayAt = 0;
};

// ---------------------------------------------------------------------------
// Coordinator
// ---------------------------------------------------------------------------

class HivewireCoordinator {
 public:
  struct SlotVal { uint8_t id, type, raw[4]; bool valid; };
  struct NodeRec {
    bool     seen;
    uint32_t lastHeard, epoch;
    uint8_t  role, flags, neighbors;
    uint8_t  hopsAway;      // 0 = heard directly
    uint16_t lastMsgId;
    SlotVal  slots[HIVEWIRE_MAX_SLOTS];
  };

  bool begin(uint8_t nodeId,
             const HwWritableSlot *writable, uint8_t writableCount,
             const HwConfig &cfg = HwConfig());
  void loop();

  // Publish new state to the whole swarm. The payload is opaque to the
  // library; bump happens automatically so higher-epoch-wins ordering holds.
  void setState(const uint8_t *state, uint8_t len, uint16_t ttlSecs);

  // Write into HW_DIR_IN / HW_DIR_INOUT slots. targetId HIVEWIRE_TARGET_ALL and
  // targetRole HW_ROLE_ANY are wildcards. Returns false if the slot was not
  // declared writable.
  bool set(uint8_t targetId, uint8_t targetRole, uint8_t slotId, int32_t value);

  // Ask a node to replay its diagnostic ring. Entries arrive asynchronously
  // through the callback, one per line, oldest first.
  typedef void (*NodeLogCallback)(uint8_t nodeId, const char *line);
  void requestLog(uint8_t nodeId);
  // A log request is the only exchange that needs retrying: unlike a beacon,
  // nothing follows it carrying the same information, so one dropped packet is
  // simply silence. Retries stop as soon as the node answers.
  static const uint8_t  LOGREQ_TRIES    = 4;
  static const uint32_t LOGREQ_RETRY_MS = 1500;
  void onNodeLog(NodeLogCallback cb) { _logCb = cb; }

  const uint8_t *state() const { return _state; }
  uint8_t  stateLen() const { return _stateLen; }
  uint32_t epoch() const { return _epoch; }

  // Swarm rollup. Any argument may be null.
  void census(uint16_t *total, uint16_t *converged, uint16_t *faults,
              uint32_t staleMs = 120000) const;
  const NodeRec &node(uint8_t id) const { return _nodes[id]; }
  bool fresh(uint8_t id, uint32_t staleMs = 120000) const;

  void _ingest(const uint8_t *data, int len);

 private:
  void sendBeacon();
  void trickleReset();
  void serviceTrickle(uint32_t now);
  void storeSlot(NodeRec &r, const HwSlotRec &rec, const uint8_t *val);

  NodeRec *_nodes = nullptr;
  const HwWritableSlot *_writable = nullptr;
  uint8_t _writableCount = 0;
  uint8_t _id = 0;
  HwConfig _cfg;

  uint32_t _epoch = 1;
  void     serviceLogReq(uint32_t now);
  uint8_t  _logReqTarget = 0;
  uint8_t  _logReqTries = 0;
  uint32_t _logReqAt = 0;

  uint8_t  _state[HIVEWIRE_MAX_STATE] = {0};
  uint8_t  _stateLen = 0;
  uint8_t  _seq = 0;
  uint16_t _ttl = 0;

  uint32_t _tInterval = 0, _tStart = 0, _tFireAt = 0;
  uint8_t  _tCount = 0;
  bool     _tScheduled = false;
  NodeLogCallback _logCb = nullptr;
};
