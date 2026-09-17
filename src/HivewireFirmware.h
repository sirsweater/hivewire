// Hivewire -- firmware distribution from the hive to the swarm, over ESP-NOW.
//
// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0
//
// HEADER-ONLY AND OPT-IN, and built on onRaw()/sendRaw() rather than inside the
// core, because it needs everything the core refuses to carry: sequence
// numbers, acknowledgement, retransmission. For advertised STATE that machinery
// is unnecessary. For a megabyte of firmware it is unavoidable. Keeping it out
// here means a swarm that never updates in the field pays nothing for it.
//
// ---------------------------------------------------------------------------
// Why this shape
// ---------------------------------------------------------------------------
//
// Only the HIVE needs a source of bytes. Nodes end up in gardens and far rooms
// where WiFi may not reach and where putting credentials on every unit would be
// silly. One device fetches; the swarm distributes. ESP-NOW is broadcast, so a
// single pass feeds EVERY node at once rather than one at a time.
//
// The transfer is windowed, and the window size is the whole trick:
//
//   Out-of-order writes to a flash OTA partition are awkward -- Arduino's
//   Update only appends. Buffering the entire image is impossible; there is no
//   PSRAM and ~1 MB will not fit in RAM. So the image moves one small window at
//   a time: broadcast the window, ask who missed what, repair, and only when a
//   window is COMPLETE append it to flash. Writes stay strictly sequential and
//   the node needs a buffer of one window, not one image.
//
// Repair is NACK-based, which is what makes multiple nodes cheap: the hive
// broadcasts, then asks, and only the chunks somebody actually missed get sent
// again. Ten nodes missing the same chunk cost one retransmission, not ten.
//
// Rough cost for a ~1.1 MB image: ~5700 chunks in ~90 windows. Tens of seconds
// on a healthy channel, minutes on a poor one -- against 65 HOURS for the same
// image over LoRa, which is why that is not a transport for firmware at all.

#pragma once
#include <Hivewire.h>
#include <Update.h>

// Temporary: -DHW_FW_DEBUG=1 prints transfer mechanics to Serial. The ring
// records outcomes; this records how it got there.
#ifndef HW_FW_DEBUG
#define HW_FW_DEBUG 0
#endif
#if HW_FW_DEBUG
#define FWDBG(...) Serial.printf(__VA_ARGS__)
#else
#define FWDBG(...) do {} while (0)
#endif

// Message types the core does not define; they arrive through onRaw().
enum HwFwMsg : uint8_t {
  HW_MSG_FW_BEGIN = 0x40,   // hive: an image is coming
  HW_MSG_FW_CHUNK = 0x41,   // hive: one piece of the current window
  HW_MSG_FW_POLL  = 0x42,   // hive: window sent, who missed what?
  HW_MSG_FW_NACK  = 0x43,   // node: this is what I am missing
  HW_MSG_FW_NEXT  = 0x44,   // hive: window complete, advance
  HW_MSG_FW_END   = 0x45,   // hive: that was the last window
};

#define HW_FW_CHUNK_DATA   192     // payload bytes per chunk; ESP-NOW caps ~250
#define HW_FW_WINDOW       64      // chunks per window -> 12 KB RAM, 8-byte map
#define HW_FW_WINDOW_BYTES ((uint32_t)HW_FW_CHUNK_DATA * HW_FW_WINDOW)

struct __attribute__((packed)) HwFwBegin {
  HwHeader h;
  uint8_t  targetId;        // one node, or HIVEWIRE_TARGET_ALL
  uint32_t imageLen;
  uint32_t imageCrc;
  uint16_t windowCount;
};

struct __attribute__((packed)) HwFwChunk {
  HwHeader h;
  uint16_t window;
  uint8_t  index;           // within the window
  uint8_t  len;
};

struct __attribute__((packed)) HwFwPoll {
  HwHeader h;
  uint16_t window;
  uint8_t  expected;        // chunks this window actually contains
};

struct __attribute__((packed)) HwFwNack {
  HwHeader h;
  uint16_t window;
  uint8_t  bitmap[HW_FW_WINDOW / 8];   // 1 = still missing
};

struct __attribute__((packed)) HwFwSimple {
  HwHeader h;
  uint16_t window;
};

// CRC32 (same polynomial as zlib) so the node can prove the image arrived
// intact before it ever becomes bootable. Without this a single silently
// corrupted chunk is a brick that has to be walked to.
inline uint32_t hwFwCrc(uint32_t crc, const uint8_t *p, size_t n) {
  crc = ~crc;
  while (n--) {
    crc ^= *p++;
    for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (-(int32_t)(crc & 1)));
  }
  return ~crc;
}

// ---------------------------------------------------------------------------
// Node side: receive an image and apply it
// ---------------------------------------------------------------------------
class HivewireFwReceiver {
 public:
  explicit HivewireFwReceiver(HivewireNode &node) : _node(node) {}

  typedef void (*BeforeCallback)(void);
  // Fires before the first byte is written. The node is committing to an
  // outage, so it must release whatever it drives first -- same reasoning as
  // losing the coordinator.
  void onBeforeUpdate(BeforeCallback cb) { _before = cb; }

  bool active() const { return _active; }

  // Call from the sketch's onRaw handler.
  void ingest(const uint8_t *data, int len) {
    if (len < (int)sizeof(HwHeader)) return;
    const HwHeader *h = (const HwHeader *)data;

    switch (h->type) {
      case HW_MSG_FW_BEGIN: onBegin(data, len); break;
      case HW_MSG_FW_CHUNK: onChunk(data, len); break;
      case HW_MSG_FW_POLL:  onPoll(data, len);  break;
      case HW_MSG_FW_NEXT:  onNext(data, len);  break;
      case HW_MSG_FW_END:   onEnd(data, len);   break;
      default: break;
    }
  }

  void loop() {
    // ALL flash writing happens HERE, never in the receive callback.
    // Update.write() blocks for milliseconds and the radio drops whatever
    // arrives meanwhile, so writing from the callback loses the packets behind
    // every write -- loss that triggers repairs, which trigger further writes.
    if (_active) {
      while (_window < _flushTo) flushWindow();
      if (_endWanted && _window >= _flushTo) {
        _endWanted = false;
        finalise();
        return;
      }
    }

    // A transfer that stops mid-flight must not leave the node wedged with a
    // half-written partition and no way back. Give up, tidy, carry on.
    if (_active && millis() - _lastRx > STALL_MS) {
      _node.log("fw: stalled, abandoned");
      Update.abort();
      _active = false;
    }
  }

 private:
  // Measured a genuine multi-second stall on the SENDER side mid-transfer --
  // most likely USB/Serial contention on the hive from asking a host for data
  // in many small round trips over a long transfer -- that briefly exceeded
  // what a tight timeout here can tell apart from a truly dead sender. 30s
  // was tight enough that a real run gave up here at window 3 of 93 while the
  // sender pressed on for another five minutes, oblivious, and reported a
  // false DONE at the end. This does not fix that hiccup; it gives the sender
  // room to recover from one without the receiver quitting first. The sender
  // now has its own giving-up logic (consecutive fully-silent windows) for
  // when the receiver is genuinely gone, so this can afford to be patient.
  static const uint32_t STALL_MS = 90000;

  void onBegin(const uint8_t *data, int len) {
    if (len < (int)sizeof(HwFwBegin)) return;
    const HwFwBegin *b = (const HwFwBegin *)data;
    if (b->targetId != HIVEWIRE_TARGET_ALL && b->targetId != _node.id()) return;
    if (_active) return;                       // already receiving this one

    if (_before) _before();
    if (!Update.begin(b->imageLen)) {
      _node.log("fw: no space %lu", (unsigned long)b->imageLen);
      return;
    }
    _imageLen = b->imageLen;
    _imageCrc = b->imageCrc;
    _windows  = b->windowCount;
    _window   = 0;
    _flushTo  = 0;
    _endWanted = false;
    _crc      = 0;
    _written  = 0;
    _active   = true;
    _lastRx   = millis();
    memset(_have, 0, sizeof(_have));
    _node.log("fw: begin %lu b", (unsigned long)_imageLen);
  }

  void onChunk(const uint8_t *data, int len) {
    if (!_active || len < (int)sizeof(HwFwChunk)) return;
    const HwFwChunk *c = (const HwFwChunk *)data;

    // Any firmware traffic proves the hive is still there, even traffic for a
    // window we are not on. Stamping this first is what stops a desync from
    // looking like a dead sender and tripping the stall timer.
    _lastRx = millis();

    if (c->window < _window) {
      FWDBG("[fw] STALE chunk w=%u but we are on %u (flushTo=%u)\n",
            c->window, _window, _flushTo);
      return;                                             // stale, already past it
    }
    // Do NOT write flash here. This runs in the ESP-NOW receive callback, and
    // Update.write() blocks for milliseconds -- long enough for the radio to
    // drop the packets arriving behind it, which is a loss that then needs
    // repairing, during which more flash writes happen. Defer to loop() and
    // let this chunk be repaired; the NACK machinery already exists for it.
    if (c->window > _window) { _flushTo = c->window; return; }
    // The hive moved on and we did not: FOLLOW IT. FW_NEXT is sent once and
    // unacknowledged, so losing that single packet used to strand a node on a
    // window nothing would ever match again -- every later chunk rejected,
    // silence, then abandonment. The window number on every chunk makes the
    // stream self-synchronising, which leaves NEXT as an optimisation rather
    // than a thing whose loss is fatal.
    if (c->index >= HW_FW_WINDOW) return;
    if (c->len > HW_FW_CHUNK_DATA) return;
    if (len < (int)sizeof(HwFwChunk) + c->len) return;  // truncated

    _lastRx = millis();
    if (_have[c->index >> 3] & (1 << (c->index & 7))) return;   // already had it
    memcpy(_buf + (uint32_t)c->index * HW_FW_CHUNK_DATA,
           data + sizeof(HwFwChunk), c->len);
    _len[c->index] = c->len;
    _have[c->index >> 3] |= (1 << (c->index & 7));
  }

  void onPoll(const uint8_t *data, int len) {
    if (!_active || len < (int)sizeof(HwFwPoll)) return;
    const HwFwPoll *p = (const HwFwPoll *)data;
    _lastRx = millis();
    if (p->window < _window) {
      FWDBG("[fw] STALE poll w=%u but we are on %u (flushTo=%u)\n",
            p->window, _window, _flushTo);
      return;
    }
    if (p->window > _window) { _flushTo = p->window; return; }   // catch up first

    HwFwNack n{};
    n.h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_FW_NACK, _node.id()};
    n.window = _window;
    uint8_t miss = 0;
    bool missing = false;
    for (uint8_t i = 0; i < p->expected; i++) {
      if (!(_have[i >> 3] & (1 << (i & 7)))) {
        n.bitmap[i >> 3] |= (1 << (i & 7));
        missing = true; miss++;
      }
    }
    // Answer either way. Silence is ambiguous -- the hive cannot tell a node
    // that has everything from one that has gone away, and it must not advance
    // the window while somebody is still behind.
    (void)missing;
    FWDBG("[fw] poll w=%u expect=%u missing=%u\n", p->window, p->expected, miss);
    _node.sendRaw((const uint8_t *)&n, sizeof(n));
  }

  void onNext(const uint8_t *data, int len) {
    if (!_active || len < (int)sizeof(HwFwSimple)) return;
    const HwFwSimple *s = (const HwFwSimple *)data;
    _lastRx = millis();
    if (s->window != _window) return;      // resync is handled by chunks/poll
    _flushTo = _window + 1;
  }

  void onEnd(const uint8_t *data, int len) {
    if (!_active || len < (int)sizeof(HwFwSimple)) return;
    _lastRx = millis();
    _flushTo = _window + 1;
    _endWanted = true;                 // finalise in loop(), never in here
  }

  void finalise() {

    if (_written != _imageLen) {
      _node.log("fw: short %lu/%lu", (unsigned long)_written, (unsigned long)_imageLen);
      Update.abort(); _active = false; return;
    }
    if (_crc != _imageCrc) {
      // Refuse rather than boot it. A corrupt image that runs is a brick you
      // have to walk to; a refused one leaves a working node exactly as it was.
      _node.log("fw: crc bad, refused");
      Update.abort(); _active = false; return;
    }
    if (!Update.end(true)) {
      _node.log("fw: end failed");
      Update.abort(); _active = false; return;
    }
    _node.log("fw: ok, rebooting");
    _active = false;
    delay(150);
    ESP.restart();
  }

  // Append a completed window. Only whole windows reach flash, which is what
  // keeps writes sequential despite chunks arriving in any order.
  void flushWindow() {
    uint32_t n = 0;
    for (uint8_t i = 0; i < HW_FW_WINDOW; i++) {
      if (!(_have[i >> 3] & (1 << (i & 7)))) break;      // hole: stop here
      memmove(_buf + n, _buf + (uint32_t)i * HW_FW_CHUNK_DATA, _len[i]);
      n += _len[i];
    }
    FWDBG("[fw] flush w=%u bytes=%lu have=%02x%02x written=%lu heap=%lu\n", _window,
          (unsigned long)n, _have[0], _have[1], (unsigned long)(_written + n),
          (unsigned long)ESP.getFreeHeap());
    if (n) {
      Update.write(_buf, n);
      _crc = hwFwCrc(_crc, _buf, n);
      _written += n;
    }
    _window++;
    memset(_have, 0, sizeof(_have));
  }

  HivewireNode &_node;
  BeforeCallback _before = nullptr;
  bool     _active = false;
  uint32_t _imageLen = 0, _imageCrc = 0, _crc = 0, _written = 0, _lastRx = 0;
  uint16_t _windows = 0, _window = 0, _flushTo = 0;
  bool     _endWanted = false;
  uint8_t  _have[HW_FW_WINDOW / 8];
  uint8_t  _len[HW_FW_WINDOW];
  uint8_t  _buf[HW_FW_WINDOW_BYTES];
};

// ---------------------------------------------------------------------------
// Hive side: push an image to the swarm
// ---------------------------------------------------------------------------
//
// The source is a callback, not a URL. Where the bytes come from is the hive's
// business and must not be baked in: the internet when it is there, a laptop on
// the LAN, a phone hotspot, SPIFFS, or a cable straight into the hive. A design
// that hard-codes HTTP quietly makes the internet a dependency, which is the one
// thing this is meant to avoid.
//
// The provider only ever needs SEQUENTIAL reads. Repairs are served from the
// window already buffered in RAM, so a source that cannot seek -- a stream down
// a USB cable, say -- works exactly as well as a file.
class HivewireFwSender {
 public:
  // Fill up to `want` bytes, return how many. Short read = end of image.
  typedef size_t (*Provider)(uint8_t *buf, size_t want);

  explicit HivewireFwSender(HivewireCoordinator &coord) : _coord(coord) {}

  bool begin(uint8_t targetId, uint32_t imageLen, uint32_t imageCrc, Provider p) {
    if (_state != IDLE || !p || !imageLen) return false;
    _target = targetId; _imageLen = imageLen; _imageCrc = imageCrc; _provider = p;
    _windows = (uint16_t)((imageLen + HW_FW_WINDOW_BYTES - 1) / HW_FW_WINDOW_BYTES);
    _window = 0; _sent = 0; _round = 0; _pollTries = 0; _lastTxUs = 0;
    _provided = 0; _deadWindows = 0; _stuckSinceMs = 0; _zeroFills = 0;
    memset(_nack, 0, sizeof(_nack));

    HwFwBegin b{};
    b.h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_FW_BEGIN, _coord.id()};
    b.targetId = targetId; b.imageLen = imageLen; b.imageCrc = imageCrc;
    b.windowCount = _windows;
    // Announced three times: if this one packet is lost nothing else in the
    // transfer means anything, and it is far cheaper to repeat than to discover
    // the loss after sending a megabyte nobody was listening for.
    for (int i = 0; i < 3; i++) {
      _coord.sendRaw((const uint8_t *)&b, sizeof(b));
      delay(20);
    }
    _state = FILL;
    return true;
  }

  void ingest(const uint8_t *data, int len) {
    if (len < (int)sizeof(HwFwNack)) return;
    const HwHeader *h = (const HwHeader *)data;
    if (h->type != HW_MSG_FW_NACK) return;
    const HwFwNack *n = (const HwFwNack *)data;
    if (n->window != _window) return;
    // Union across nodes: one retransmission repairs every node that missed it,
    // which is what makes a broadcast push cheap for a whole swarm at once.
    for (size_t i = 0; i < sizeof(_nack); i++) _nack[i] |= n->bitmap[i];
    _replies++;
    FWDBG("[fwtx] nack from=%u w=%u replies=%u nack0=%02x\n",
          h->srcId, n->window, _replies, _nack[0]);
  }

  void loop() {
    switch (_state) {
      case IDLE: return;

      case FILL: {
        _fill = _provider(_buf, HW_FW_WINDOW_BYTES);
        if (!_fill) {
          // A zero return meant "done" unconditionally, which was the same
          // bug as the short-window heuristic wearing a different shape: it
          // trusted ONE call's result instead of the cumulative total. Caught
          // live -- window 7 of 93 came back as a bare zero (not merely
          // short) after an unexplained multi-second gap on the host link,
          // and the transfer closed there with 92 windows of real image data
          // never sent. A zero when _provided already covers the whole image
          // is the ordinary, expected end. A zero before that is not proof of
          // anything except that THIS ONE ATTEMPT got nothing back -- retry a
          // bounded number of times, and only finish() once retries are
          // exhausted, which is what lets a genuinely disconnected host (the
          // deliberate ABORT test) still terminate rather than hang forever.
          if (_provided >= _imageLen) { finish(); return; }
          if (++_zeroFills >= MAX_ZERO_FILLS) {
            FWDBG("[fwtx] provider empty %u times running, giving up\n",
                  _zeroFills);
            finish();
          }
          return;
        }
        _zeroFills = 0;
        _provided += _fill;   // the ONLY thing advance() trusts to detect EOF
        _chunks = (uint8_t)((_fill + HW_FW_CHUNK_DATA - 1) / HW_FW_CHUNK_DATA);
        _state = SEND; _next = 0; _round = 0;
        return;
      }

      case SEND: {
        // Two things had to be true, and checking the queue-accept return value
        // was only the first. esp_now_send() returning OK means the driver
        // QUEUED the packet, not that it reached the air -- a broadcast frame
        // gets no MAC-layer ACK or retry, so back-to-back sends collide on a
        // real channel even when every call succeeds. Measured directly: 64
        // chunks offered in under a millisecond, and a node's own flush log
        // showed only 46 had actually arrived by the time the window closed.
        // CHUNK_GAP_US paces transmission to something the air can carry.
        if (_next < _chunks) {
          if (micros() - _lastTxUs < CHUNK_GAP_US) return;
          if (sendChunk(_next)) {
            _next++; _lastTxUs = micros(); _stuckSinceMs = 0;
            return;
          }
          // The queue refused the packet. _lastTxUs is untouched, so the pacing
          // gate above stays open on every next call -- without a floor here
          // that turns into a tight busy-loop hammering esp_now_send() as fast
          // as the CPU allows. That is not harmless spinning: it denies the
          // WiFi/LWIP task the scheduling slices it needs to actually drain
          // whatever made the queue full in the first place, which can turn an
          // ordinary transient backpressure moment into an extended one -- a
          // plausible mechanism for the multi-second, unpredictable stalls
          // seen only on long transfers (a short one never sends enough back
          // to back to hit real backpressure at all). Yield, and if a single
          // chunk stays stuck past SEND_STUCK_MS the queue is not recovering
          // on its own; abandon the rest of this window's sends and let NACK
          // repair -- or, if the swarm is genuinely gone, dead-window
          // detection -- take it from here instead of hanging forever.
          delay(2);
          if (!_stuckSinceMs) _stuckSinceMs = millis();
          else if (millis() - _stuckSinceMs > SEND_STUCK_MS) {
            FWDBG("[fwtx] send queue stuck %ums, giving up on w=%u chunk %u\n",
                  (unsigned)(millis() - _stuckSinceMs), _window, _next);
            _stuckSinceMs = 0;
            pollFresh();
          }
          return;
        }
        _stuckSinceMs = 0;
        pollFresh();
        return;
      }

      case WAIT: {
        // The FIRST wait after any poll stays short: every window that ever
        // succeeded got its replies back within a couple hundred milliseconds,
        // so there is no reason to slow the common case down. Only once nobody
        // has answered at all does it make sense to wait longer before asking
        // again -- that is specifically the condition an extended RF blackout
        // produces, and it is retries in THAT state that need real duration to
        // outlast one, not the ordinary per-window round trip.
        uint32_t budget = (_pollTries <= 1) ? POLL_WAIT_MS : DEAD_RETRY_WAIT_MS;
        if (millis() - _polledAt < budget) return;
        FWDBG("[fwtx] wait-done w=%u replies=%u tries=%u round=%u nack0=%02x heap=%lu\n",
              _window, _replies, _pollTries, _round, _nack[0],
              (unsigned long)ESP.getFreeHeap());

        // Silence is not evidence. If literally nobody answered -- the poll
        // itself was lost, or every reply was -- an empty NACK bitmap looks
        // identical to "nobody is missing anything", and advancing on that
        // reading is how a window closed with barely two-thirds of its data
        // and the loss was never noticed until the receiver's own ring said
        // so. A window that genuinely finished should have gotten at least one
        // reply; if it did not, ask again -- WITHOUT clearing what a straggler
        // may already have told us -- before believing it.
        if (_replies == 0 && _pollTries < MAX_POLL_TRIES) { pollRetry(); return; }

        // Nobody answered even after every retry. That is different from "one
        // straggler is missing a few chunks" -- it means NO node is hearing us
        // at all for this window, and pressing on regardless is how a real run
        // finished: the sender walked all the way to window 92 of a 93-window
        // image and printed DONE, having heard nothing back since window 3,
        // because the receiver had already hit its own stall timeout and quit
        // while the sender had no way to know. A few consecutive dead windows
        // stops the transfer instead of burning the rest of it talking to no
        // one and reporting a false DONE.
        if (_replies == 0) {
          if (++_deadWindows >= MAX_DEAD_WINDOWS) {
            FWDBG("[fwtx] no replies for %u windows straight, giving up\n",
                  _deadWindows);
            finish();
            return;
          }
          advance();
          return;
        }
        _deadWindows = 0;

        bool any = false;
        for (size_t i = 0; i < sizeof(_nack); i++) if (_nack[i]) { any = true; break; }
        if (!any) { advance(); return; }
        if (++_round > MAX_ROUNDS) {
          // Somebody is unreachable. Press on rather than stalling the whole
          // swarm: nodes that did not get a complete image refuse it on CRC and
          // stay on the firmware they already had.
          advance();
          return;
        }
        _state = REPAIR; _next = 0;
        return;
      }

      case REPAIR: {
        while (_next < _chunks) {
          uint8_t i = _next;
          if (_nack[i >> 3] & (1 << (i & 7))) {
            if (micros() - _lastTxUs < CHUNK_GAP_US) return;
            if (sendChunk(i)) {
              _next++; _lastTxUs = micros(); _stuckSinceMs = 0;
              return;
            }
            // Same escape hatch as SEND, and for the same reason: a busy-loop
            // here is just as capable of starving the radio task of the time
            // it needs to recover.
            delay(2);
            if (!_stuckSinceMs) _stuckSinceMs = millis();
            else if (millis() - _stuckSinceMs > SEND_STUCK_MS) {
              _stuckSinceMs = 0;
              pollFresh();
            }
            return;
          }
          _next++;
        }
        _stuckSinceMs = 0;
        pollFresh();       // repair changed what's missing: verify with a clean slate
        return;
      }
    }
  }

  bool active() const { return _state != IDLE; }
  uint16_t window() const { return _window; }
  uint16_t windows() const { return _windows; }

 private:
  enum State { IDLE, FILL, SEND, WAIT, REPAIR };
  static const uint32_t POLL_WAIT_MS   = 300;   // nodes must have time to answer
  static const uint32_t DEAD_RETRY_WAIT_MS = 1000;  // longer wait once nobody has answered at all
  static const uint8_t  MAX_ROUNDS     = 6;
  static const uint8_t  MAX_POLL_TRIES = 4;     // retries when NOBODY answers
  // Consecutive fully-silent windows tolerated before concluding the receiver
  // is truly gone, rather than just quiet for a while.
  //
  // The value that mattered here was NOT this constant on its own -- it was
  // the total patience it buys, and the first version bought only ~5 seconds
  // (3 windows x 4 tries x 300ms), which measured evidence showed is nowhere
  // near enough. Traced a real transfer where a single node stopped receiving
  // ANY firmware traffic (no stale-window rejections either -- genuinely
  // nothing arrived) for well over ten seconds while remaining fully present
  // on ordinary swarm traffic the whole time: heap flat on both ends, no
  // desync, nothing to fix in the STATE MACHINE. The likely cause is physical:
  // that node's board sits on this PC's USB, and USB 3 is a documented
  // broadband noise source in the 2.4 GHz band -- a plausible source of
  // exactly this kind of extended, intermittent RF blackout under the sustained
  // back-to-back transmission a firmware push produces. Software cannot fix
  // interference; it can only outlast it. 20 windows here buys roughly
  // 20 x 4 x 1000ms = 80s, deliberately close to the receiver's own 90s
  // STALL_MS, so neither side gives up on a blackout the other is still
  // patiently waiting out.
  static const uint8_t  MAX_DEAD_WINDOWS = 20;
  static const uint32_t SEND_STUCK_MS = 2000;   // longest a single chunk may be refused
  static const uint8_t  MAX_ZERO_FILLS = 5;     // retries before a zero read means "gone"
  static const uint32_t CHUNK_GAP_US   = 2000;  // pace to what broadcast air can carry

  bool sendChunk(uint8_t i) {
    uint8_t pkt[sizeof(HwFwChunk) + HW_FW_CHUNK_DATA];
    HwFwChunk *c = (HwFwChunk *)pkt;
    c->h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_FW_CHUNK, _coord.id()};
    c->window = _window;
    c->index = i;
    uint32_t off = (uint32_t)i * HW_FW_CHUNK_DATA;
    uint32_t n = _fill - off;
    if (n > HW_FW_CHUNK_DATA) n = HW_FW_CHUNK_DATA;
    c->len = (uint8_t)n;
    memcpy(pkt + sizeof(HwFwChunk), _buf + off, n);
    if (!_coord.sendRaw(pkt, sizeof(HwFwChunk) + n)) return false;   // queue full
    _sent++;
    return true;
  }

  // A fresh verification round: after every chunk in this window has been
  // (re)sent, so any NACK bit still standing from before is stale information,
  // not evidence. Clearing it here, and only here, is what lets a retried poll
  // (below) safely NOT clear -- the two must never collapse into one call, or
  // a straggler's answer to try #1 gets erased by try #2's reset.
  void pollFresh() {
    memset(_nack, 0, sizeof(_nack));
    _pollTries = 0;
    sendPoll();
  }

  // Retry within the same verification round: nobody answered the last poll,
  // so ask again WITHOUT touching _nack -- a reply already banked from a
  // slower node must survive a retry aimed at a faster one that stayed silent.
  void pollRetry() { sendPoll(); }

  void sendPoll() {
    _replies = 0;
    _pollTries++;
    HwFwPoll p{};
    p.h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_FW_POLL, _coord.id()};
    p.window = _window; p.expected = _chunks;
    _coord.sendRaw((const uint8_t *)&p, sizeof(p));
    _polledAt = millis();
    _state = WAIT;
  }

  void advance() {
    HwFwSimple s{};
    s.h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_FW_NEXT, _coord.id()};
    s.window = _window;
    _coord.sendRaw((const uint8_t *)&s, sizeof(s));
    _window++;
    // Judge end-of-image by CUMULATIVE bytes actually provided against the
    // announced image length -- never by whether one window's fill happened
    // to come back short. That heuristic was the real bug tonight: a single
    // transient hiccup in the host link (measured: an unexplained multi-second
    // stall appeared even mid-transfer, well before end of image, on a fully
    // healthy USB connection) could make one ordinary window look "short," and
    // the sender would close the whole transfer there -- discarding everything
    // still queued behind it. _provided is exact and can never be fooled by a
    // slow or partial single read; a short fill just means the next FILL asks
    // for the remainder, whatever caused the shortfall.
    if (_provided >= _imageLen) finish();
    else _state = FILL;
  }

  void finish() {
    HwFwSimple e{};
    e.h = {HIVEWIRE_MAGIC, HIVEWIRE_PROTOCOL, HW_MSG_FW_END, _coord.id()};
    e.window = _window;
    for (int i = 0; i < 3; i++) { _coord.sendRaw((const uint8_t *)&e, sizeof(e)); delay(20); }
    _state = IDLE;
  }

  HivewireCoordinator &_coord;
  Provider _provider = nullptr;
  State    _state = IDLE;
  uint8_t  _target = 0;
  uint32_t _imageLen = 0, _imageCrc = 0, _fill = 0, _sent = 0, _polledAt = 0;
  uint32_t _provided = 0;   // cumulative bytes handed over; the sole EOF signal
  uint32_t _lastTxUs = 0, _stuckSinceMs = 0;
  uint16_t _windows = 0, _window = 0, _replies = 0;
  uint8_t  _pollTries = 0, _deadWindows = 0, _zeroFills = 0;
  uint8_t  _chunks = 0, _next = 0, _round = 0;
  uint8_t  _nack[HW_FW_WINDOW / 8];
  uint8_t  _buf[HW_FW_WINDOW_BYTES];
};
