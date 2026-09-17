// Hivewire -- fetch firmware bytes from a URL, for HivewireFwSender.
//
// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0
//
// HEADER-ONLY AND OPT-IN. This is the piece that makes "trigger the hive to
// download something and push it to the swarm" real: HivewireFwSender already
// takes bytes from any Provider (a plain function pointer), and until now the
// only one written was feedFromSerial() in the FirmwarePush example. This is
// the same shape, reading from an HTTP response body instead of a UART.
//
// The CRC and length are supplied by whoever triggers the fetch, not computed
// here. Streaming the body once to learn its CRC and then streaming it AGAIN
// to actually transmit would double the download for no reason -- a build
// pipeline that publishes a .bin already knows its own size and CRC32, so the
// natural place for that information is the command that starts the fetch
// (`fetch <url> <len> <crc32>`), exactly mirroring FirmwarePush's own
// `FW <len> <crc32>` line. HTTP's own Content-Length is checked only as a
// sanity cross-check against what was announced, never trusted on its own --
// a misconfigured or malicious server should not get to decide that for the
// swarm.
//
// One instance per sketch, same constraint as every other uplink/provider
// here: the Provider function pointer has no user-data slot, so the active
// fetch has to be reached through a static.
//
// Same caution as HivewireHttpUplink.h, and for the same underlying reason:
// this needs an active WiFi station connection to work at all, and that is
// not compatible with a device also running ESP-NOW for a swarm -- see that
// header for the documented channel conflict. On a gateway that bridges to a
// swarm, prefer HivewireSerialProvider (in HivewireSerialUplink.h) instead,
// fed over USB by something with its own separate WiFi hardware.

#pragma once
#include <WiFi.h>
#include <HTTPClient.h>

class HivewireHttpFetcher {
 public:
  HivewireHttpFetcher() { _instance = this; }

  // Opens the URL and leaves the response body ready to stream. Returns false
  // on any connection or HTTP-status failure -- the caller should report that
  // rather than start a transfer with nothing behind it.
  bool begin(const char *url) {
    end();
    if (!_http.begin(_client, url)) return false;
    _http.setTimeout(15000);
    int code = _http.GET();
    if (code != HTTP_CODE_OK) { end(); return false; }
    _remaining = (uint32_t)_http.getSize();
    _active = _remaining > 0;
    if (!_active) end();
    return _active;
  }

  // What the server reported, purely informational -- the CALLER's announced
  // length is what actually governs the transfer, per the reasoning above.
  uint32_t contentLength() const { return _remaining; }

  void end() {
    if (_active) _http.end();
    _active = false;
    _remaining = 0;
  }

  bool active() const { return _active; }

  // Matches HivewireFwSender::Provider exactly: fill up to `want` bytes,
  // return how many. A short return before _remaining hits zero means the
  // connection stalled or dropped -- HivewireFwSender's own EOF-by-cumulative
  // -progress and bounded-retry logic (see HivewireFirmware.h) is what
  // survives that; this function does not need its own retry loop on top.
  static size_t feed(uint8_t *buf, size_t want) {
    HivewireHttpFetcher *self = _instance;
    if (!self || !self->_active || !self->_remaining) return 0;
    if (want > self->_remaining) want = (size_t)self->_remaining;

    WiFiClient *stream = self->_http.getStreamPtr();
    size_t got = 0;
    uint32_t idle = millis();
    while (got < want) {
      if (stream->available()) {
        int n = stream->read(buf + got, want - got);
        if (n > 0) { got += (size_t)n; idle = millis(); }
      } else if (!stream->connected() || millis() - idle > 10000) {
        break;                            // server gone, or genuinely stalled
      } else {
        delay(1);
      }
    }
    self->_remaining -= got;
    if (!self->_remaining) self->end();   // tidy up the connection promptly
    return got;
  }

 private:
  inline static HivewireHttpFetcher *_instance = nullptr;

  WiFiClient _client;
  HTTPClient _http;
  bool _active = false;
  uint32_t _remaining = 0;
};
