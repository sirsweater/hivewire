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
  void start(uint32_t totalBytes) { _remaining = totalBytes; }
  bool active() const { return _remaining > 0; }

  static size_t feed(uint8_t *buf, size_t want) {
    return _instance ? _instance->doFeed(buf, want) : 0;
  }

 private:
  static const size_t SUBCHUNK = 1024;

  size_t doFeed(uint8_t *buf, size_t want) {
    if (!_remaining) return 0;
    if (want > _remaining) want = (size_t)_remaining;

    size_t total = 0;
    while (total < want) {
      size_t ask = want - total;
      if (ask > SUBCHUNK) ask = SUBCHUNK;

      Serial.println("MORE");
      size_t got = 0;
      uint32_t idle = millis();
      while (got < ask) {
        int n = Serial.available();
        if (n > 0) {
          size_t take = (size_t)n;
          if (take > ask - got) take = ask - got;
          got += Serial.readBytes(buf + total + got, take);
          idle = millis();
        } else if (millis() - idle > 4000) {
          break;                          // host went away mid-subchunk
        } else {
          delay(1);
        }
      }
      total += got;
      if (got < ask) break;               // stalled; stop honestly, see below
    }
    _remaining -= total;
    // A short return here does NOT mean "end of image" on its own --
    // HivewireFwSender judges completion from cumulative bytes provided
    // against the announced length, not from any single call's result. That
    // fix (see HivewireFirmware.h) is what makes a transient hiccup on this
    // link survivable instead of fatal.
    return total;
  }

  inline static HivewireSerialProvider *_instance = nullptr;
  uint32_t _remaining = 0;
};
