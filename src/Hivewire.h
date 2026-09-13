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
#define HIVEWIRE_PROTOCOL   3
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

// Mode 0 must always mean "safe". It is what a unit falls back to on its own
// when beacons stop arriving.
enum HwMode : uint8_t {
  HW_MODE_SAFE = 0,
};

#define HW_FLAG_SENSOR_FAIL 0x01
#define HW_FLAG_LOW_BATT    0x02
#define HW_FLAG_NO_COORD    0x04

struct __attribute__((packed)) HwHeader {
  uint8_t magic;
  uint8_t version;
  uint8_t type;
  uint8_t srcId;
};

struct __attribute__((packed)) HwBeacon {
  HwHeader h;
  uint32_t epoch;    // monotonic; higher always wins
  uint8_t  mode;
  uint8_t  param;
  uint16_t ttlSecs;  // 0 = no expiry
  uint8_t  hops;     // diagnostic only; not used for routing
};

// HW_MSG_STATUS and HW_MSG_SET both carry slotCount records, each a HwSlotRec
// followed by exactly rec.len value bytes.
struct __attribute__((packed)) HwStatusHdr {
  HwHeader h;
  uint32_t epoch;
  uint8_t  mode;
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
  uint32_t trickleImaxMs  = 16000;
  uint8_t  trickleK       = 3;      // suppress once this many neighbours agree
  uint32_t failsafeMs     = 30000;  // no beacon for this long -> HW_MODE_SAFE
  uint32_t minTxGapMs     = 2000;   // floor on transmit rate
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
  typedef void (*ModeCallback)(uint8_t mode, uint8_t param);

  bool begin(uint8_t nodeId, uint8_t role,
             const HwSlotDef *slots, uint8_t slotCount,
             const HwConfig &cfg = HwConfig());
  void loop();
  void onMode(ModeCallback cb) { _modeCb = cb; }

  uint8_t  mode()  const { return _mode; }
  uint8_t  param() const { return _param; }
  uint32_t epoch() const { return _epoch; }
  uint8_t  neighbors() const;
  bool     orphaned() const;

  // Called from the ESP-NOW receive callback; not for application use.
  void _ingest(const uint8_t *data, int len);

 private:
  struct SlotState {
    uint8_t  raw[4];
    bool     valid, dirty;
    uint32_t lastSample, lastReport;
  };

  void sendBeacon(uint8_t hops);
  void sendStatus();
  void handleSet(const uint8_t *data, int len);
  void trickleReset();
  void serviceTrickle(uint32_t now);
  int  findSlot(uint8_t id) const;

  const HwSlotDef *_slots = nullptr;
  SlotState *_st = nullptr;
  uint8_t _slotCount = 0;
  uint8_t _id = 0, _role = 0;
  HwConfig _cfg;
  ModeCallback _modeCb = nullptr;

  uint32_t _epoch = 0, _adoptedAt = 0, _lastBeacon = 0, _lastTx = 0;
  uint8_t  _mode = HW_MODE_SAFE, _param = 0;
  uint16_t _ttl = 0;

  uint32_t _tInterval = 0, _tStart = 0, _tFireAt = 0;
  uint8_t  _tCount = 0;
  bool     _tScheduled = false;

  uint32_t _seen[256];
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
    uint8_t  mode, role, flags, neighbors;
    SlotVal  slots[HIVEWIRE_MAX_SLOTS];
  };

  bool begin(uint8_t nodeId,
             const HwWritableSlot *writable, uint8_t writableCount,
             const HwConfig &cfg = HwConfig());
  void loop();

  // Bump the epoch so the whole swarm converges on a new posture.
  void setState(uint8_t mode, uint8_t param, uint16_t ttlSecs);

  // Write into HW_DIR_IN / HW_DIR_INOUT slots. targetId HIVEWIRE_TARGET_ALL and
  // targetRole HW_ROLE_ANY are wildcards. Returns false if the slot was not
  // declared writable.
  bool set(uint8_t targetId, uint8_t targetRole, uint8_t slotId, int32_t value);

  uint8_t  mode()  const { return _mode; }
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
  uint8_t  _mode = HW_MODE_SAFE, _param = 0, _seq = 0;
  uint16_t _ttl = 0;

  uint32_t _tInterval = 0, _tStart = 0, _tFireAt = 0;
  uint8_t  _tCount = 0;
  bool     _tScheduled = false;
};
