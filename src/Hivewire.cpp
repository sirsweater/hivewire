// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0

#include "Hivewire.h"
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <string.h>
#include <math.h>

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

  _st = (SlotState *)calloc(slotCount ? slotCount : 1, sizeof(SlotState));
  if (!_st) return false;

  g_node = this;
  if (!hwRadioBegin(_cfg.channel)) return false;
  randomSeed(esp_random());
  trickleReset();
  return true;
}

int HivewireNode::findSlot(uint8_t id) const {
  for (uint8_t i = 0; i < _slotCount; i++) if (_slots[i].id == id) return i;
  return -1;
}

uint8_t HivewireNode::neighbors() const {
  uint8_t n = 0;
  uint32_t now = millis();
  for (int i = 0; i < 256; i++)
    if (_seen[i] && now - _seen[i] < 60000) n++;
  return n;
}

bool HivewireNode::orphaned() const {
  return millis() - _lastBeacon > _cfg.failsafeMs;
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
  HwBeacon b{};
  b.h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_BEACON, _id};
  b.epoch = _epoch; b.mode = _mode; b.param = _param;
  b.ttlSecs = _ttl; b.hops = hops;
  esp_now_send(HW_BCAST, (uint8_t *)&b, sizeof(b));
}

void HivewireNode::sendStatus() {
  uint8_t buf[HIVEWIRE_MAX_PAYLOAD];
  HwStatusHdr *hdr = (HwStatusHdr *)buf;
  hdr->h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_STATUS, _id};
  hdr->epoch = _epoch;
  hdr->mode = _mode;
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
  if (!count) return;

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
    if (i < 0) continue;                                  // unknown slot
    if (!(_slots[i].dir & HW_DIR_IN)) continue;           // not writable
    if (!_slots[i].apply) continue;                       // no applier
    if (r.type != _slots[i].type) continue;               // wrong type
    if (r.len != hwSlotTypeLen(_slots[i].type)) continue; // wrong length
    int32_t v = hwSlotAsInt(r.type, val);
    if (v < _slots[i].minVal || v > _slots[i].maxVal) continue;  // out of range

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
  _seen[h->srcId] = millis();

  if (h->type == HW_MSG_BEACON && len >= (int)sizeof(HwBeacon)) {
    const HwBeacon *b = (const HwBeacon *)data;
    _lastBeacon = millis();
    if (b->epoch > _epoch) {
      _epoch = b->epoch; _ttl = b->ttlSecs; _adoptedAt = millis();
      if (b->mode != _mode || b->param != _param) {
        _mode = b->mode; _param = b->param;
        if (_modeCb) _modeCb(_mode, _param);
      }
      trickleReset();
    } else if (b->epoch == _epoch && _tCount < 255) {
      _tCount++;                                  // consistent: suppress
    }
  } else if (h->type == HW_MSG_SET) {
    handleSet(data, len);
  }
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

  // Orphaned, or the commanded state expired: go safe without being told.
  bool expired = _ttl && (now - _adoptedAt > (uint32_t)_ttl * 1000UL);
  if (_mode != HW_MODE_SAFE && (orphaned() || expired)) {
    _mode = HW_MODE_SAFE; _param = 0; _ttl = 0;
    if (_modeCb) _modeCb(_mode, _param);
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
    if (_tCount < _cfg.trickleK) sendBeacon();
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
  HwBeacon b{};
  b.h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_BEACON, _id};
  b.epoch = _epoch; b.mode = _mode; b.param = _param;
  b.ttlSecs = _ttl; b.hops = 0;
  esp_now_send(HW_BCAST, (uint8_t *)&b, sizeof(b));
}

void HivewireCoordinator::setState(uint8_t mode, uint8_t param, uint16_t ttlSecs) {
  _epoch++;
  _mode = mode; _param = param; _ttl = ttlSecs;
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
    NodeRec &r = _nodes[h->srcId];
    r.seen = true;
    r.lastHeard = millis();
    r.epoch = sh->epoch;
    r.mode = sh->mode;
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
  }
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
  serviceTrickle(millis());
}
