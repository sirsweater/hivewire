// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0

#include "Hivewire.h"
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>
#include <math.h>
#include <stdarg.h>

static uint8_t HW_BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ESP-NOW hands packets to a plain C callback, so the active instance is held
// here. A sketch is a node or a coordinator, never both.
static HivewireNode *g_node = nullptr;
static HivewireCoordinator *g_coord = nullptr;

uint8_t hwSlotTypeLen(uint8_t type) {
  switch (type) {
    case HW_U8: case HW_I8:   return 1;
    case HW_U16: case HW_I16: return 2;
    case HW_U32: case HW_I32: case HW_F32: return 4;
    default: return 0;
  }
}

int32_t hwSlotAsInt(uint8_t type, const uint8_t *p) {
  switch (type) {
    case HW_U8:  return (int32_t)p[0];
    case HW_I8:  return (int32_t)(int8_t)p[0];
    case HW_U16: { uint16_t v; memcpy(&v, p, 2); return (int32_t)v; }
    case HW_I16: { int16_t  v; memcpy(&v, p, 2); return (int32_t)v; }
    case HW_U32: { uint32_t v; memcpy(&v, p, 4); return (int32_t)v; }
    case HW_I32: { int32_t  v; memcpy(&v, p, 4); return v; }
    case HW_F32: { float    v; memcpy(&v, p, 4); return (int32_t)v; }
    default: return 0;
  }
}

static uint32_t hwDelta(uint8_t type, const uint8_t *a, const uint8_t *b) {
  if (type == HW_F32) {
    float x, y; memcpy(&x, a, 4); memcpy(&y, b, 4);
    return (uint32_t)fabsf(x - y);
  }
  int64_t d = (int64_t)hwSlotAsInt(type, a) - (int64_t)hwSlotAsInt(type, b);
  return (uint32_t)(d < 0 ? -d : d);
}

static void hwOnRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
#if HIVEWIRE_DEBUG
  Serial.printf("[hw] RX len=%d magic=%02x ver=%u type=%u src=%u\n", len,
                len > 0 ? data[0] : 0, len > 1 ? data[1] : 0,
                len > 2 ? data[2] : 0, len > 3 ? data[3] : 0);
#endif
  // Signal strength exists only here, in the driver callback -- by the time a
  // packet reaches _ingest the radio metadata is gone. Capturing it is what
  // separates "it converged" from "it converged with 3 dB to spare", which is
  // the difference that matters when deciding where a unit can live.
  if (g_node && info && info->rx_ctrl)
    g_node->_noteRssi((int8_t)info->rx_ctrl->rssi);

  if (g_node) g_node->_ingest(data, len);
  if (g_coord) g_coord->_ingest(data, len);
}

#ifndef HIVEWIRE_DEBUG
#define HIVEWIRE_DEBUG 0
#endif
#if HIVEWIRE_DEBUG
#define HWLOG(...) Serial.printf(__VA_ARGS__)
static void hwOnSent(const wifi_tx_info_t *info, esp_now_send_status_t st) {
  HWLOG("[hw] send status %s\n", st == ESP_NOW_SEND_SUCCESS ? "OK" : "FAIL");
}
#else
#define HWLOG(...) do {} while (0)
#endif

static bool hwRadioBegin(uint8_t channel) {
  WiFi.mode(WIFI_STA);
  delay(500);                              // let USB CDC enumerate before we log
  esp_err_t ce = esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
  HWLOG("[hw] set_channel(%u) -> %d\n", channel, (int)ce);

  esp_err_t ie = esp_now_init();
  HWLOG("[hw] esp_now_init -> %d\n", (int)ie);
  if (ie != ESP_OK) return false;
  esp_now_register_recv_cb(hwOnRecv);
#if HIVEWIRE_DEBUG
  esp_now_register_send_cb(hwOnSent);
#endif

  esp_now_peer_info_t peer{};
  memcpy(peer.peer_addr, HW_BCAST, 6);
  peer.channel = 0;                        // 0 = use the interface's channel
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  esp_err_t pe = esp_now_add_peer(&peer);
  HWLOG("[hw] add_peer -> %d  (mac %02x:%02x:%02x:%02x:%02x:%02x)\n", (int)pe,
        HW_BCAST[0], HW_BCAST[1], HW_BCAST[2], HW_BCAST[3], HW_BCAST[4], HW_BCAST[5]);

  uint8_t pch = 0; wifi_second_chan_t sec;
  esp_wifi_get_channel(&pch, &sec);
  HWLOG("[hw] radio is on channel %u\n", pch);
  return pe == ESP_OK;
}

// ---------------------------------------------------------------------------
// HivewireNode
// ---------------------------------------------------------------------------

bool HivewireNode::begin(uint8_t nodeId, uint8_t role,
                         const HwSlotDef *slots, uint8_t slotCount,
                         const HwConfig &cfg) {
  _id = nodeId; _role = role;
  _slots = slots; _slotCount = slotCount;
  _cfg = cfg;
  memset(_seen, 0, sizeof(_seen));

  // Raising trickleImaxMs without raising failsafeMs re-creates a fault that is
  // very hard to read from the outside: a converged swarm goes quiet by design,
  // so the gap between beacons approaches trickleImaxMs even when nothing is
  // wrong, and one lost packet doubles it. Set the two too close and nodes drop
  // their state at random on a perfectly healthy network -- which on a machine
  // means an actuator releasing for no reason. Correct it loudly rather than
  // honouring a combination that cannot work.
  if (_cfg.failsafeMs < _cfg.trickleImaxMs * 6) {
    uint32_t want = _cfg.trickleImaxMs * 8;
    log("failsafe %lu->%lu (too near trickle)",
        (unsigned long)_cfg.failsafeMs, (unsigned long)want);
    _cfg.failsafeMs = want;
  }

  _st = (SlotState *)calloc(slotCount ? slotCount : 1, sizeof(SlotState));
  if (!_st) return false;

  g_node = this;
  if (!hwRadioBegin(_cfg.channel)) return false;
  randomSeed(esp_random());
  trickleReset();
  log("boot id=%u role=%u", nodeId, role);
  return true;
}

void HivewireNode::log(const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(_log[_logHead], HIVEWIRE_LOG_WIDTH, fmt, ap);
  va_end(ap);
  _logHead = (_logHead + 1) % HIVEWIRE_LOG_LINES;
  if (_logCount < HIVEWIRE_LOG_LINES) _logCount++;
}

// Replay the ring to whoever asked. Oldest first, packed into one message --
// the ring is sized so it always fits.
// Queue the reply rather than sending it here. Two reasons, both learned the
// hard way: this runs in the ESP-NOW receive callback, where doing real work
// stalls the radio; and the whole ring in one packet is the largest frame the
// protocol ever sends -- roughly 190 bytes against a status report's ~110.
//
// That size is the problem. A long frame is more likely to be corrupted, and a
// node at the edge of range is exactly where you most need its history and
// least likely to get it. Observed: a node at -86 dBm answered every SET and
// every status for an hour, yet returned its ring zero times out of two, while
// a closer node returned all 64 lines. Diagnostics are rare and on demand, so
// spending a few extra small packets to make them actually arrive is the right
// trade every time.
void HivewireNode::handleLogReq(const uint8_t *data, int len) {
  if (len < (int)sizeof(HwLogReq)) return;
  const HwLogReq *rq = (const HwLogReq *)data;
  if (rq->targetId != HIVEWIRE_TARGET_ALL && rq->targetId != _id) return;
  _logRspLeft = _logCount;      // send the whole ring, a little at a time
  _logRspAt = millis();
}

void HivewireNode::serviceLogRsp(uint32_t now) {
  if (!_logRspLeft || (int32_t)(now - _logRspAt) < 0) return;

  uint8_t buf[HIVEWIRE_MAX_PAYLOAD];
  HwLogRsp *rsp = (HwLogRsp *)buf;
  rsp->h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_LOGRSP, _id};

  // Oldest first, resuming where the last packet stopped.
  uint8_t start = (_logHead + HIVEWIRE_LOG_LINES - _logCount) % HIVEWIRE_LOG_LINES;
  uint8_t from  = _logCount - _logRspLeft;

  size_t off = sizeof(HwLogRsp);
  uint8_t n = 0;
  while (_logRspLeft && n < LOGRSP_LINES_PER_PKT) {
    const char *e = _log[(start + from + n) % HIVEWIRE_LOG_LINES];
    uint8_t elen = (uint8_t)strnlen(e, HIVEWIRE_LOG_WIDTH);
    if (off + 1 + elen > LOGRSP_SOFT_MAX) break;
    buf[off++] = elen;
    memcpy(buf + off, e, elen);
    off += elen;
    n++;
    _logRspLeft--;
  }
  if (!n) { _logRspLeft = 0; return; }        // a line too long to ever fit

  rsp->count = n;
  esp_now_send(HW_BCAST, buf, off);
  _logRspAt = now + LOGRSP_GAP_MS;            // let the air clear between frames
}

bool HivewireNode::seenBefore(uint8_t src, uint16_t msgId) {
  for (uint8_t i = 0; i < RELAY_SEEN; i++)
    if (_seenMsg[i].src == src && _seenMsg[i].id == msgId) return true;
  _seenMsg[_seenHead].src = src;
  _seenMsg[_seenHead].id = msgId;
  _seenHead = (_seenHead + 1) % RELAY_SEEN;
  return false;
}

// Carry another node's report one hop further toward the coordinator.
//
// Beacons gossip outward on their own, so distant units adopt state fine. The
// return path is what is missing: without this a node two hops out is adopting
// orders nobody can see it obey, and the coordinator undercounts the swarm.
//
// Bounded three ways, because relaying is how broadcast networks melt:
// a hop limit, a duplicate cache so a report crossing a loop dies, and random
// jitter so neighbours hearing the same report do not all repeat it at once.
void HivewireNode::maybeRelay(const uint8_t *data, int len) {
  if (!_cfg.relayHops) return;
  if (len < (int)sizeof(HwStatusHdr) || len > HIVEWIRE_MAX_PAYLOAD) return;

  const HwStatusHdr *sh = (const HwStatusHdr *)data;
  if (sh->h.srcId == _id) return;                 // never repeat ourselves
  if (sh->hops >= _cfg.relayHops) return;         // travelled far enough
  if (seenBefore(sh->h.srcId, sh->msgId)) return; // already carried this one

  // Queue it rather than sending here. This runs in the ESP-NOW receive
  // callback -- WiFi task context -- where blocking for jitter would risk
  // dropped packets and a watchdog reset. loop() does the actual send.
  if (_relayLen) return;                          // one in flight is enough
  memcpy(_relayBuf, data, len);
  ((HwStatusHdr *)_relayBuf)->hops = sh->hops + 1;
  _relayLen = len;
  _relayAt = millis() + (_cfg.relayJitterMs ? random(_cfg.relayJitterMs) : 0);
}

void HivewireNode::serviceRelay(uint32_t now) {
  if (!_relayLen || (int32_t)(now - _relayAt) < 0) return;
  esp_now_send(HW_BCAST, _relayBuf, _relayLen);
  _relayLen = 0;
}

int HivewireNode::findSlot(uint8_t id) const {
  for (uint8_t i = 0; i < _slotCount; i++) if (_slots[i].id == id) return i;
  return -1;
}

void HivewireNode::_noteRssi(int8_t rssi) {
  if (!rssi) return;
  _rssiLast = rssi;
  // More negative is weaker. Seed on the first reading rather than comparing
  // against the 0 sentinel, which would look like the strongest signal possible.
  if (!_rssiWorst || rssi < _rssiWorst) _rssiWorst = rssi;
}

void HivewireNode::deafenTo(uint8_t srcId, uint16_t secs) {
  if (!srcId || !secs) {
    if (_deafId) log("deaf cleared");
    _deafId = 0;
    _deafUntil = 0;
    return;
  }
  if (secs > DEAF_MAX_SECS) secs = DEAF_MAX_SECS;
  _deafId = srcId;
  _deafUntil = millis() + (uint32_t)secs * 1000;
  log("deaf to %u for %us", srcId, secs);
}

uint16_t HivewireNode::deafSecsLeft() const {
  if (!_deafId || !_deafUntil) return 0;
  int32_t left = (int32_t)(_deafUntil - millis());
  return left > 0 ? (uint16_t)(left / 1000) : 0;
}

uint8_t HivewireNode::deafTarget() const {
  return deafSecsLeft() ? _deafId : 0;
}

void HivewireNode::forgetEpoch() {
  if (!_epoch) return;
  log("forget ep=%lu", (unsigned long)_epoch);
  _epoch = 0;
  _holding = false;
  _ttl = 0;
  _stateLen = 0;
  trickleReset();          // advertise again promptly once something arrives
}

uint8_t HivewireNode::neighbors() const {
  uint8_t n = 0;
  uint32_t now = millis();
  for (int i = 0; i < 256; i++)
    if (_seen[i] && now - _seen[i] < 60000) n++;
  return n;
}

// How long since a beacon, computed so it can never run backwards.
//
// _lastBeacon is written from the ESP-NOW receive callback, so it can move
// AFTER millis() has been read here. The unsigned subtraction then wraps to
// about 49 days and every caller concludes the opposite of the truth. In
// orphaned() that meant a node failed safe at the exact instant a beacon
// arrived -- which is why every spurious failsafe had a healthy beacon age
// logged on both sides of it, and why it looked like the network was fine at
// precisely the moments units were dropping their state.
//
// Snapshot the value once, then compare signed: a negative age means a beacon
// landed mid-calculation, which is the strongest possible evidence of liveness.
uint32_t HivewireNode::beaconAgeMs() const {
  uint32_t last = _lastBeacon;            // one read; the ISR may move it after
  int32_t age = (int32_t)(millis() - last);
  return age > 0 ? (uint32_t)age : 0;
}

bool HivewireNode::orphaned() const {
  return beaconAgeMs() > _cfg.failsafeMs;
}

void HivewireNode::trickleReset() {
  _tInterval = _cfg.trickleIminMs;
  _tStart = millis();
  _tCount = 0;
  _tFireAt = _tStart + _tInterval / 2 + random(_tInterval / 2);
  _tScheduled = true;
}

void HivewireNode::serviceTrickle(uint32_t now) {
  if (_tScheduled && (int32_t)(now - _tFireAt) >= 0) {
    if (_tCount < _cfg.trickleK) sendBeacon(1);
    _tScheduled = false;
  }
  if (!_tScheduled && now - _tStart >= _tInterval) {
    _tInterval = min<uint32_t>(_tInterval * 2, _cfg.trickleImaxMs);
    _tStart = now;
    _tCount = 0;
    _tFireAt = now + _tInterval / 2 + random(_tInterval / 2);
    _tScheduled = true;
  }
}

void HivewireNode::sendBeacon(uint8_t hops) {
  uint8_t buf[sizeof(HwBeacon) + HIVEWIRE_MAX_STATE];
  HwBeacon *b = (HwBeacon *)buf;
  b->h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_BEACON, _id};
  b->epoch = _epoch;
  b->ttlSecs = _ttl;
  b->hops = hops;
  b->len = _stateLen;
  memcpy(buf + sizeof(HwBeacon), _state, _stateLen);
  esp_now_send(HW_BCAST, buf, sizeof(HwBeacon) + _stateLen);
}

void HivewireNode::sendStatus() {
  uint8_t buf[HIVEWIRE_MAX_PAYLOAD];
  HwStatusHdr *hdr = (HwStatusHdr *)buf;
  hdr->h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_STATUS, _id};
  hdr->epoch = _epoch;
  hdr->msgId = ++_msgSeq;
  hdr->hops = 0;
  hdr->role = _role;
  hdr->flags = orphaned() ? HW_FLAG_NO_COORD : 0;
  hdr->neighbors = neighbors();

  uint32_t now = millis();
  size_t off = sizeof(HwStatusHdr);
  uint8_t count = 0;
  for (uint8_t i = 0; i < _slotCount; i++) {
    if (!(_slots[i].dir & HW_DIR_OUT)) continue;   // never publish IN-only
    if (!_st[i].valid) continue;
    bool due = _slots[i].reportPeriodMs &&
               (now - _st[i].lastReport >= _slots[i].reportPeriodMs);
    if (!_st[i].dirty && !due) continue;

    uint8_t len = hwSlotTypeLen(_slots[i].type);
    if (off + sizeof(HwSlotRec) + len > HIVEWIRE_MAX_PAYLOAD) break;

    HwSlotRec r{_slots[i].id, _slots[i].type, len};
    memcpy(buf + off, &r, sizeof(r));      off += sizeof(r);
    memcpy(buf + off, _st[i].raw, len);    off += len;
    _st[i].dirty = false;
    _st[i].lastReport = now;
    count++;
  }
  // Nothing to report is still worth reporting, occasionally -- see
  // statusKeepaliveMs. Being silent is what makes a healthy node look absent.
  if (!count && (now - _lastTx) < _cfg.statusKeepaliveMs) return;

  hdr->slotCount = count;
  esp_now_send(HW_BCAST, buf, off);
  _lastTx = now;
}

// Every gate a write must clear. Any failure refuses that record and moves on;
// nothing partially validated reaches apply().
void HivewireNode::handleSet(const uint8_t *data, int len) {
  if (len < (int)sizeof(HwSetHdr)) return;
  const HwSetHdr *sh = (const HwSetHdr *)data;
  if (sh->targetId != HIVEWIRE_TARGET_ALL && sh->targetId != _id) return;
  if (sh->targetRole != HW_ROLE_ANY && sh->targetRole != _role) return;

  size_t off = sizeof(HwSetHdr);
  for (uint8_t k = 0; k < sh->slotCount; k++) {
    if (off + sizeof(HwSlotRec) > (size_t)len) return;
    HwSlotRec r;
    memcpy(&r, data + off, sizeof(r));
    off += sizeof(r);
    if (off + r.len > (size_t)len) return;
    const uint8_t *val = data + off;
    off += r.len;

    int i = findSlot(r.id);
    if (i < 0)                    { log("set %u: unknown", r.id); continue; }
    if (!(_slots[i].dir & HW_DIR_IN)) { log("set %u: read-only", r.id); continue; }
    if (!_slots[i].apply)         { log("set %u: no applier", r.id); continue; }
    if (r.type != _slots[i].type) { log("set %u: bad type", r.id); continue; }
    if (r.len != hwSlotTypeLen(_slots[i].type)) { log("set %u: bad len", r.id); continue; }
    int32_t v = hwSlotAsInt(r.type, val);
    if (v < _slots[i].minVal || v > _slots[i].maxVal) {
      log("set %u: %ld out of range", r.id, (long)v);
      continue;
    }

    _slots[i].apply(val);
    memcpy(_st[i].raw, val, r.len);
    _st[i].valid = true;
    if (_slots[i].dir & HW_DIR_OUT) _st[i].dirty = true;  // echo confirmation
  }
}

void HivewireNode::_ingest(const uint8_t *data, int len) {
  if (len < (int)sizeof(HwHeader)) return;
  const HwHeader *h = (const HwHeader *)data;
  if (h->magic != HIVEWIRE_MAGIC || h->version != HIVEWIRE_PROTOCOL) return;

  // Our own report, handed back by a neighbour that relayed it. Two reasons to
  // drop it before anything else looks at it: it would count us as our own
  // neighbour, inflating every neighbour count by one the moment a relay is in
  // range; and maybeRelay() would forward it onward again, because the dedup
  // table only remembers reports we relayed, never the ones we originated.
  if (h->srcId == _id) return;

  // Test filter, checked before anything is recorded: a node we are pretending
  // not to hear must not show up as a neighbour either, or the topology we are
  // trying to force would still be visible in the telemetry.
  if (h->srcId == _deafId && deafSecsLeft()) return;

  _seen[h->srcId] = millis();

  if (h->type == HW_MSG_BEACON && len >= (int)sizeof(HwBeacon)) {
    const HwBeacon *b = (const HwBeacon *)data;
    _lastBeacon = millis();
    _beaconsRx++;

    uint8_t plen = b->len;
    if (plen > HIVEWIRE_MAX_STATE) return;                       // malformed
    if (len < (int)sizeof(HwBeacon) + plen) return;              // truncated
    const uint8_t *payload = data + sizeof(HwBeacon);

    if (b->epoch > _epoch) {
      _epoch = b->epoch; _ttl = b->ttlSecs; _adoptedAt = millis();
      _holding = true;
      // Only surface genuinely new state. A re-advertisement of what we already
      // hold must not re-fire the callback -- applications act on these.
      bool changed = (plen != _stateLen) || memcmp(payload, _state, plen) != 0;
      if (changed) {
        memcpy(_state, payload, plen);
        _stateLen = plen;
        log("adopt ep=%lu len=%u", (unsigned long)_epoch, plen);
        if (_stateCb) _stateCb(_state, _stateLen);
      }
      trickleReset();
    } else if (b->epoch == _epoch && _tCount < 255) {
      _tCount++;                                  // consistent: suppress
    }
  } else if (h->type == HW_MSG_SET) {
    handleSet(data, len);
  } else if (h->type == HW_MSG_LOGREQ) {
    handleLogReq(data, len);
  } else if (h->type == HW_MSG_STATUS) {
    maybeRelay(data, len);
  } else if (_rawCb) {
    _rawCb(data, len);          // a type the core does not define; see onRaw()
  }
}

bool HivewireNode::sendRaw(const uint8_t *data, uint16_t len) {
  if (!data || !len || len > HIVEWIRE_MAX_PAYLOAD) return false;
  return esp_now_send(HW_BCAST, data, len) == ESP_OK;
}

void HivewireNode::loop() {
  uint32_t now = millis();

  for (uint8_t i = 0; i < _slotCount; i++) {
    if (!_slots[i].sample) continue;              // write-only slot
    if (_st[i].valid && now - _st[i].lastSample < _slots[i].samplePeriodMs) continue;
    uint8_t fresh[4] = {0};
    _slots[i].sample(fresh);
    _st[i].lastSample = now;
    if (!_st[i].valid) {
      memcpy(_st[i].raw, fresh, 4);
      _st[i].valid = true;
      _st[i].dirty = true;
    } else if (memcmp(fresh, _st[i].raw, 4) != 0 &&
               hwDelta(_slots[i].type, fresh, _st[i].raw) >= _slots[i].changeThreshold) {
      memcpy(_st[i].raw, fresh, 4);
      _st[i].dirty = true;
    }
  }

  if (now - _lastTx >= _cfg.minTxGapMs) sendStatus();
  serviceTrickle(now);
  serviceRelay(now);
  serviceLogRsp(now);

  // Orphaned, or the state expired: give it up without being told. The library
  // guarantees this fires; what "safe" involves is the application's business.
  // Evaluate each condition ONCE. Calling orphaned() again to choose the log
  // message is a race: beacons arrive from the ESP-NOW receive callback, so one
  // landing between the test and the message turns a beacon-loss failsafe into
  // a reported "ttl expired". A diagnostic that lies about the only thing it
  // exists to explain is worse than none -- this one cost a wrong diagnosis
  // before it was caught. Record the measured gap too, so the next occurrence
  // can be judged instead of guessed at.
  bool isOrphan = orphaned();
  bool expired  = _ttl && (now - _adoptedAt > (uint32_t)_ttl * 1000UL);
  if (_holding && (isOrphan || expired)) {
    _holding = false;
    _stateLen = 0;
    if (isOrphan) log("safe: no beacon %lus",
                      (unsigned long)(beaconAgeMs() / 1000UL));
    else          log("safe: ttl %us elapsed", _ttl);
    _ttl = 0;
    if (_safeCb) _safeCb();
  }
}

// ---------------------------------------------------------------------------
// HivewireCoordinator
// ---------------------------------------------------------------------------

bool HivewireCoordinator::begin(uint8_t nodeId,
                                const HwWritableSlot *writable,
                                uint8_t writableCount,
                                const HwConfig &cfg) {
  _id = nodeId;
  _writable = writable;
  _writableCount = writableCount;
  _cfg = cfg;

  _nodes = (NodeRec *)calloc(256, sizeof(NodeRec));
  if (!_nodes) return false;

  g_coord = this;
  if (!hwRadioBegin(_cfg.channel)) return false;
  randomSeed(esp_random());
  trickleReset();
  return true;
}

void HivewireCoordinator::trickleReset() {
  _tInterval = _cfg.trickleIminMs;
  _tStart = millis();
  _tCount = 0;
  _tFireAt = _tStart + _tInterval / 2 + random(_tInterval / 2);
  _tScheduled = true;
}

void HivewireCoordinator::serviceTrickle(uint32_t now) {
  if (_tScheduled && (int32_t)(now - _tFireAt) >= 0) {
    // The coordinator does NOT suppress. Trickle's redundancy check is right
    // for gossip -- a node has no reason to repeat what its neighbours already
    // echoed -- but this beacon is the liveness signal every node's failsafe is
    // measured against. Suppressing it means a swarm goes quiet precisely
    // because it is converged and healthy, and then every unit in it drops to
    // safe state for exactly that reason.
    //
    // Observed: nodes failing safe with "no beacon 64s" while reporting a live
    // neighbour on both sides of the event. The worst failure mode this design
    // can have -- an actuator releasing because nothing was wrong.
    //
    // Interval doubling is kept, so a settled swarm still quietens to one
    // beacon per trickleImaxMs. Quiet, not silent; the distinction is the whole
    // safety property.
    sendBeacon();
    _tScheduled = false;
  }
  if (!_tScheduled && now - _tStart >= _tInterval) {
    _tInterval = min<uint32_t>(_tInterval * 2, _cfg.trickleImaxMs);
    _tStart = now;
    _tCount = 0;
    _tFireAt = now + _tInterval / 2 + random(_tInterval / 2);
    _tScheduled = true;
  }
}

void HivewireCoordinator::sendBeacon() {
  uint8_t buf[sizeof(HwBeacon) + HIVEWIRE_MAX_STATE];
  HwBeacon *b = (HwBeacon *)buf;
  b->h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_BEACON, _id};
  b->epoch = _epoch;
  b->ttlSecs = _ttl;
  b->hops = 0;
  b->len = _stateLen;
  memcpy(buf + sizeof(HwBeacon), _state, _stateLen);
  esp_now_send(HW_BCAST, buf, sizeof(HwBeacon) + _stateLen);
}

void HivewireCoordinator::setState(const uint8_t *state, uint8_t len,
                                   uint16_t ttlSecs) {
  if (len > HIVEWIRE_MAX_STATE) len = HIVEWIRE_MAX_STATE;
  _epoch++;
  memcpy(_state, state, len);
  _stateLen = len;
  _ttl = ttlSecs;
  trickleReset();
  sendBeacon();
}

bool HivewireCoordinator::set(uint8_t targetId, uint8_t targetRole,
                              uint8_t slotId, int32_t value) {
  int8_t type = -1;
  for (uint8_t i = 0; i < _writableCount; i++)
    if (_writable[i].id == slotId) type = _writable[i].type;
  if (type < 0) return false;

  uint8_t len = hwSlotTypeLen((uint8_t)type);
  if (!len) return false;

  uint8_t buf[sizeof(HwSetHdr) + sizeof(HwSlotRec) + 4];
  HwSetHdr *sh = (HwSetHdr *)buf;
  sh->h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_SET, _id};
  sh->targetId = targetId;
  sh->targetRole = targetRole;
  sh->seq = ++_seq;
  sh->slotCount = 1;

  HwSlotRec rec{slotId, (uint8_t)type, len};
  memcpy(buf + sizeof(HwSetHdr), &rec, sizeof(rec));
  uint32_t raw = (uint32_t)value;
  memcpy(buf + sizeof(HwSetHdr) + sizeof(rec), &raw, len);

  esp_now_send(HW_BCAST, buf, sizeof(HwSetHdr) + sizeof(rec) + len);
  return true;
}

// Ask, and keep asking until answered.
//
// This is the one exchange in the protocol that genuinely needs retrying, and
// the reason is that it is a REQUEST, not an advertisement. Everything else
// here converges on its own: a lost beacon is followed by another carrying the
// same state, so nothing needs to be re-sent. A dropped log request converges
// on nothing -- it just vanishes, and the operator sees silence that is
// indistinguishable from a dead node.
//
// Measured: on a link healthy enough to carry every beacon, write and status
// report, a log request still went missing often enough that consecutive
// fetches returned nothing at all. Diagnostics are rare and on demand, so a
// handful of extra small packets costs nothing and is the difference between a
// feature that works and one that works most of the time.
void HivewireCoordinator::requestLog(uint8_t nodeId) {
  _logReqTarget = nodeId;
  _logReqTries  = LOGREQ_TRIES;
  _logReqAt     = 0;                 // send the first one immediately
}

void HivewireCoordinator::serviceLogReq(uint32_t now) {
  if (!_logReqTries || (_logReqAt && (int32_t)(now - _logReqAt) < 0)) return;

  HwLogReq rq{};
  rq.h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_LOGREQ, _id};
  rq.targetId = _logReqTarget;
  esp_now_send(HW_BCAST, (uint8_t *)&rq, sizeof(rq));

  _logReqTries--;
  _logReqAt = now + LOGREQ_RETRY_MS;
}

void HivewireCoordinator::storeSlot(NodeRec &r, const HwSlotRec &rec,
                                    const uint8_t *val) {
  int free = -1;
  for (uint8_t i = 0; i < HIVEWIRE_MAX_SLOTS; i++) {
    if (r.slots[i].valid && r.slots[i].id == rec.id) { free = i; break; }
    if (!r.slots[i].valid && free < 0) free = i;
  }
  if (free < 0) return;                 // table full; drop rather than evict
  r.slots[free].id = rec.id;
  r.slots[free].type = rec.type;
  memset(r.slots[free].raw, 0, 4);
  memcpy(r.slots[free].raw, val, rec.len > 4 ? 4 : rec.len);
  r.slots[free].valid = true;
}

void HivewireCoordinator::_ingest(const uint8_t *data, int len) {
  if (len < (int)sizeof(HwHeader)) return;
  const HwHeader *h = (const HwHeader *)data;
  if (h->magic != HIVEWIRE_MAGIC || h->version != HIVEWIRE_PROTOCOL) return;

  if (h->type == HW_MSG_STATUS && len >= (int)sizeof(HwStatusHdr)) {
    const HwStatusHdr *sh = (const HwStatusHdr *)data;

    // Epoch recovery. A coordinator restarts its counter at 1, but the swarm
    // remembers whatever it last adopted -- and nodes only take a HIGHER epoch.
    // So a rebooted coordinator is silently ignored: digests still arrive, slot
    // data still flows, and every command is discarded. One power blip does
    // this, and nothing looks wrong.
    //
    // Nodes report the epoch they hold, so climb above the highest we see.
    //
    // Strictly GREATER THAN. Using >= here creates a runaway: a converged node
    // reports our own epoch back, we read it as "higher", bump, the node adopts
    // and reports the new one, and the counter escalates forever.
    if (sh->epoch > _epoch) _epoch = sh->epoch + 1;
    NodeRec &r = _nodes[h->srcId];
    // The same report can arrive directly AND via a relay. Keep the first copy
    // and ignore the rest, so a node is not counted twice or aged oddly.
    if (r.seen && r.lastMsgId == sh->msgId) return;
    r.lastMsgId = sh->msgId;
    r.hopsAway = sh->hops;
    r.seen = true;
    r.lastHeard = millis();
    r.epoch = sh->epoch;
    r.role = sh->role;
    r.flags = sh->flags;
    r.neighbors = sh->neighbors;

    size_t off = sizeof(HwStatusHdr);
    for (uint8_t k = 0; k < sh->slotCount; k++) {
      if (off + sizeof(HwSlotRec) > (size_t)len) return;
      HwSlotRec rec;
      memcpy(&rec, data + off, sizeof(rec));
      off += sizeof(rec);
      if (off + rec.len > (size_t)len) return;
      storeSlot(r, rec, data + off);
      off += rec.len;
    }
  } else if (h->type == HW_MSG_BEACON && len >= (int)sizeof(HwBeacon)) {
    const HwBeacon *b = (const HwBeacon *)data;
    if (b->epoch == _epoch && _tCount < 255) _tCount++;
  } else if (h->type == HW_MSG_LOGRSP && len >= (int)sizeof(HwLogRsp)) {
    // Answered: stop asking. Retries exist only to survive a dropped request,
    // and the ring arrives as several frames, so this must not stop after the
    // first one -- it clears the whole retry schedule, not one attempt.
    if (h->srcId == _logReqTarget) _logReqTries = 0;
    const HwLogRsp *rsp = (const HwLogRsp *)data;
    size_t off = sizeof(HwLogRsp);
    char line[HIVEWIRE_LOG_WIDTH + 1];
    for (uint8_t k = 0; k < rsp->count; k++) {
      if (off >= (size_t)len) break;
      uint8_t elen = data[off++];
      if (elen > HIVEWIRE_LOG_WIDTH || off + elen > (size_t)len) break;
      memcpy(line, data + off, elen);
      line[elen] = 0;
      off += elen;
      if (_logCb) _logCb(h->srcId, line);
    }
  } else if (_rawCb) {
    _rawCb(data, len);          // a type the core does not define; see onRaw()
  }
}

bool HivewireCoordinator::sendRaw(const uint8_t *data, uint16_t len) {
  if (!data || !len || len > HIVEWIRE_MAX_PAYLOAD) return false;
  return esp_now_send(HW_BCAST, data, len) == ESP_OK;
}

bool HivewireCoordinator::fresh(uint8_t id, uint32_t staleMs) const {
  const NodeRec &r = _nodes[id];
  return r.seen && (millis() - r.lastHeard) <= staleMs;
}

void HivewireCoordinator::census(uint16_t *total, uint16_t *converged,
                                 uint16_t *faults, uint32_t staleMs) const {
  uint16_t t = 0, c = 0, f = 0;
  for (int i = 0; i < 256; i++) {
    if (!fresh((uint8_t)i, staleMs)) continue;
    t++;
    if (_nodes[i].epoch == _epoch) c++;
    if (_nodes[i].flags) f++;
  }
  if (total) *total = t;
  if (converged) *converged = c;
  if (faults) *faults = f;
}

void HivewireCoordinator::loop() {
  uint32_t now = millis();
  serviceTrickle(now);
  serviceLogReq(now);
}
