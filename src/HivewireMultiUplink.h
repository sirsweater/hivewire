// Hivewire -- combine several HivewireUplink transports into one.
//
// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0
//
// HEADER-ONLY, no new dependency: it only needs the HivewireUplink interface
// every transport already implements. "Use all its connections" turns out to
// mean something simple at this layer -- send small state on every transport
// that is up, and act on a command from whichever one delivers it first. There
// is no leader-election between transports and no attempt to pick the "best"
// one; redundancy is the entire point, and it is cheap here because nothing
// this carries is bulk data. Bulk data (firmware) is deliberately NOT part of
// this interface -- see HivewireFirmware.h, which moves bytes over ESP-NOW
// specifically because replicating a megabyte across every uplink would be
// the opposite of cheap.
//
// A command arriving on two transports at once (an operator resending on LoRa
// after already sending over the internet uplink, say) is not de-duplicated
// here on purpose: everything this library lets you command is already safe
// to receive twice -- state converges to the same result, a write is refused
// or applied the same way either time, a status pull just answers again. Cross
// -transport dedup would be real complexity spent protecting against a
// non-problem.

#pragma once
#include <Hivewire.h>

class HivewireMultiUplink : public HivewireUplink {
 public:
  static const uint8_t MAX_UPLINKS = 4;

  // Register a transport. Call before begin(). Ownership stays with the
  // caller -- this only ever holds pointers, so the underlying MeshtasticUplink,
  // HivewireHttpUplink, or whatever else stays a normal sketch-level global.
  bool add(HivewireUplink *u) {
    if (_count >= MAX_UPLINKS || !u) return false;
    _links[_count++] = u;
    return true;
  }

  bool begin() override {
    g_self = this;
    bool any = false;
    for (uint8_t i = 0; i < _count; i++) {
      // One transport with no cable plugged in, or no WiFi credentials
      // configured, must not stop the others from starting -- that is the
      // entire reason to have more than one.
      if (_links[i]->begin()) any = true;
      _links[i]->onCommand(dispatch);
    }
    return any;
  }

  void loop() override {
    for (uint8_t i = 0; i < _count; i++) _links[i]->loop();
  }

  bool ready() override {
    for (uint8_t i = 0; i < _count; i++) if (_links[i]->ready()) return true;
    return false;
  }

  // Replicate to every transport that is currently up. A status line is a
  // handful of bytes; paying for it twice is free next to what losing the
  // only copy would cost.
  void send(const char *line) override {
    for (uint8_t i = 0; i < _count; i++)
      if (_links[i]->ready()) _links[i]->send(line);
  }

  void onCommand(CommandCallback cb) override { _cb = cb; }

  // Which transports are actually up right now, for a status line -- "we have
  // a LoRa command channel but the internet one dropped" is worth being able
  // to say, and cannot be answered from ready() alone.
  uint8_t upCount() const {
    uint8_t n = 0;
    for (uint8_t i = 0; i < _count; i++) if (_links[i]->ready()) n++;
    return n;
  }
  uint8_t linkCount() const { return _count; }

 private:
  static void dispatch(const char *line) {
    if (g_self && g_self->_cb) g_self->_cb(line);
  }

  inline static HivewireMultiUplink *g_self = nullptr;

  HivewireUplink *_links[MAX_UPLINKS] = {};
  uint8_t _count = 0;
  CommandCallback _cb = nullptr;
};
