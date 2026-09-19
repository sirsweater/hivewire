// Hivewire -- a command (and firmware) channel fed over the gateway's own
// USB serial port, for a Raspberry Pi or any other small computer with its
// own network connection.
//
// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0
//
// This exists because of a real, well-documented ESP32 limitation:
// WiFi.begin() (station mode) forces the radio onto the access point's
// channel, and if the swarm's ESP-NOW peers are on a different one -- which
// they will be unless you pin the AP to match, and most home routers can
// change channels on their own -- ESP-NOW to the swarm goes silently dead the
// moment the WiFi connection succeeds. See HivewireHttpUplink.h's own comment
// for that path's tradeoffs; this is the alternative that avoids the problem
// by construction, because a Pi's WiFi hardware is completely separate from
// the C6's, so there is no shared radio to fight over.
//
// Deliberately dumb: this class does nothing but split incoming bytes on
// newlines and hand each line to the registered callback, exactly the same
// contract every other HivewireUplink honours. Whatever already drives
// FirmwarePush.ino's plain-text protocol over USB works here unchanged --
// intentionally, since HivewireSerialProvider below is the FirmwarePush
// bytes-transfer logic lifted out to be shared rather than re-solved.
//
// IMPORTANT if you use both on the same port (which a gateway combining this
// with HivewireSerialProvider does): call pause() before starting a firmware
// transfer and resume() once it ends. Both classes read Serial directly, and
// Serial is one stream with one reader position -- left unpaused, this
// uplink's line-splitting loop() and the transfer's raw byte reads would
// compete for the same incoming bytes and corrupt both.

#pragma once
#include <Hivewire.h>

class HivewireSerialUplink : public HivewireUplink {
 public:
  bool begin() override { return true; }   // Serial is already open by setup()

  // MUST be called around any use of HivewireSerialProvider on the same port.
  // Both classes read directly from Serial, and Serial is one stream with one
  // reader position: if this uplink's newline-splitting loop() keeps consuming
  // bytes while a firmware transfer is also trying to read raw bytes off the
  // same port, the two race for the same data and corrupt both -- a command
  // line could eat firmware bytes looking for a '\n' that is really part of
  // the image, and the transfer would come up short with no obvious cause.
  // pause() stops this uplink from touching Serial at all until resume().
  void pause() { _paused = true; }
  void resume() { _paused = false; }

  void loop() override {
    // Re-checked every iteration, not just on entry: the callback invoked
    // below can itself call pause() (a "push" command does exactly that,
    // before the very byte transfer this guards against). Checking only once
    // at the top would let this same call keep draining Serial after pause()
    // returns, on the unproven assumption that the host has not sent anything
    // yet -- true for a well-behaved host following the documented protocol,
    // but not a thing this loop should have to assume to stay correct.
    while (!_paused && Serial.available()) {
      char c = (char)Serial.read();
      if (c == '\n' || c == '\r') {
        if (_len) {
          _buf[_len] = 0;
          if (_cb) _cb(_buf);
          _len = 0;
        }
      } else if (_len < sizeof(_buf) - 1) {
        _buf[_len++] = c;
      }
    }
  }

  // Best-effort: whether or not a Pi is actually listening costs nothing to
  // find out, so this never reports "not ready" the way a real network
  // connection would.
  bool ready() override { return true; }

  void send(const char *line) override {
    Serial.printf("[uplink-usb] %s\n", line);
  }

  void onCommand(CommandCallback cb) override { _cb = cb; }

 private:
  char _buf[200];
  size_t _len = 0;
  bool _paused = false;
  CommandCallback _cb = nullptr;
};

// ---------------------------------------------------------------------------
// A Provider (matches HivewireFwSender's Provider signature exactly) that
// streams firmware bytes over the SAME USB link a command arrived on.
// ---------------------------------------------------------------------------
//
// This is FirmwarePush.ino's feedFromSerial(), unchanged in behaviour, moved
// here so a gateway that ALSO bridges LoRa does not need its own copy. The
// sub-chunked MORE handshake is not decoration: a host that answers one MORE
// with an entire ~12KB window overruns what the USB CDC receive path can
// actually absorb as one burst -- measured directly, roughly 1400 of every
// 12288 bytes silently lost, no error anywhere. Asking for one modest slice
// at a time is what makes the transfer byte-exact.
class HivewireSerialProvider {
 public:
  HivewireSerialProvider() { _instance = this; }

  // Call once the announced length is known, before handing Provider::feed to
  // HivewireFwSender::begin().
  void start(uint32_t totalBytes) { _remaining = totalBytes; _offset = 0; _retries = 0; }
  bool active() const { return _remaining > 0; }
  uint32_t retries() const { return _retries; }   // subchunks that had to be re-asked

  static size_t feed(uint8_t *buf, size_t want) {
    return _instance ? _instance->doFeed(buf, want) : 0;
  }

 private:
  static const size_t   SUBCHUNK  = 1024;
  static const uint8_t  MAX_TRIES = 6;
  // A healthy host answers within milliseconds and paces its reply in small
  // blocks, so this much silence mid-subchunk means bytes were lost, not that
  // the host is slow. Kept short because every loss costs one of these.
  static const uint32_t IDLE_MS   = 800;

  // PROTOCOL: "MORE <offset> <len>" -- the host must answer with exactly
  // <len> bytes of the image starting at absolute byte <offset>, then 4 bytes
  // of little-endian CRC32 over (offset as 4 LE bytes || those <len> bytes).
  //
  // It used to be a bare "MORE", meaning "the next bytes". That cannot survive
  // loss: when the C6's 64-byte USB Serial/JTAG RX FIFO overruns, bytes vanish
  // from the MIDDLE of a subchunk with no error, the host has no idea, and it
  // carries on from where IT thinks the stream is. Measured with host-side
  // pacing already in place: one subchunk in ~1100 still lost bytes, the
  // image arrived 799 bytes short with everything after the gap shifted, and
  // both nodes (correctly) refused it as "fw: short". Pacing makes loss rare;
  // addressing by offset makes it harmless. A short subchunk is thrown away
  // whole -- its surviving bytes are misaligned and cannot be trusted -- and
  // the SAME offset is asked for again.
  size_t doFeed(uint8_t *buf, size_t want) {
    if (!_remaining) return 0;
    if (want > _remaining) want = (size_t)_remaining;

    size_t total = 0;
    while (total < want) {
      size_t ask = want - total;
      if (ask > SUBCHUNK) ask = SUBCHUNK;

      bool ok = false;
      for (uint8_t attempt = 0; attempt < MAX_TRIES && !ok; attempt++) {
        // Late stragglers from a failed attempt must not be read as the start
        // of the retry; on a retry, wait for the line to go quiet first.
        drainInput(attempt ? 60 : 0);
        Serial.printf("MORE %lu %u\n", (unsigned long)_offset, (unsigned)ask);
        // Each reply is <len> data bytes followed by a little-endian CRC32
        // computed over (offset as 4 LE bytes || data). A length check alone
        // proves bytes arrived, not that they are the RIGHT bytes: measured,
        // a prompt garbled by another task's output on this same port made
        // the host fall back to sending the next sequential bytes while the
        // gateway was re-asking an OLDER offset, so a full-length reply of
        // the wrong data was accepted and the image was refused four minutes
        // later with "crc bad". Folding the offset into the checksum means a
        // reply for any other offset fails here, immediately, and is re-asked.
        uint8_t crcb[4];
        ok = readExactly(buf + total, ask) == ask && readExactly(crcb, 4) == 4 &&
             subCrc(_offset, buf + total, ask) ==
                 ((uint32_t)crcb[0] | (uint32_t)crcb[1] << 8 |
                  (uint32_t)crcb[2] << 16 | (uint32_t)crcb[3] << 24);
        if (!ok) _retries++;
      }
      if (!ok) break;              // host really is gone; stop honestly, see below
      total   += ask;
      _offset += ask;
    }
    _remaining -= total;
    // A short return here does NOT mean "end of image" on its own --
    // HivewireFwSender judges completion from cumulative bytes provided
    // against the announced length, not from any single call's result. That
    // fix (see HivewireFirmware.h) is what makes a transient hiccup on this
    // link survivable instead of fatal.
    return total;
  }

  // Standard CRC-32 (IEEE, reflected, as zlib.crc32) over offset||data, so a
  // host can compute it with zlib.crc32(struct.pack('<I', off) + data).
  static uint32_t crcStep(uint32_t crc, const uint8_t *p, size_t n) {
    while (n--) {
      crc ^= *p++;
      for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (-(int32_t)(crc & 1)));
    }
    return crc;
  }
  static uint32_t subCrc(uint32_t offset, const uint8_t *data, size_t n) {
    uint8_t o[4] = {(uint8_t)offset, (uint8_t)(offset >> 8),
                    (uint8_t)(offset >> 16), (uint8_t)(offset >> 24)};
    uint32_t crc = crcStep(0xFFFFFFFFu, o, 4);
    return ~crcStep(crc, data, n);
  }

  size_t readExactly(uint8_t *dst, size_t len) {
    size_t got = 0;
    uint32_t idle = millis();
    while (got < len) {
      int n = Serial.available();
      if (n > 0) {
        size_t take = (size_t)n;
        if (take > len - got) take = len - got;
        got += Serial.readBytes(dst + got, take);
        idle = millis();
      } else if (millis() - idle > IDLE_MS) {
        break;
      } else {
        delay(1);
      }
    }
    return got;
  }

  // Discard whatever is waiting. With quietMs > 0, keep discarding until the
  // line has been silent that long, so bytes still in flight are caught too.
  void drainInput(uint32_t quietMs) {
    uint32_t t = millis();
    do {
      while (Serial.available()) { Serial.read(); t = millis(); }
      if (quietMs) delay(1);
    } while (quietMs && millis() - t < quietMs);
  }

  inline static HivewireSerialProvider *_instance = nullptr;
  uint32_t _remaining = 0;
  uint32_t _offset = 0;
  uint32_t _retries = 0;
};
