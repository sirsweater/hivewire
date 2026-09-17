// Hivewire -- an internet command channel for the hive, alongside LoRa.
//
// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0
//
// HEADER-ONLY AND OPT-IN, same reasoning as HivewireOta.h: pulls in WiFi and
// HTTPClient, so a hive that never needs internet reach should not pay for it.
// Combine with HivewireMultiUplink to run this alongside a MeshtasticUplink --
// neither depends on the other, and losing WiFi never touches the LoRa path.
//
// Deliberately the simplest thing that works with ZERO server infrastructure:
// periodically GET one URL, and if the body is new text, treat it as one
// command. Any static file host works -- a Pi, a spare web server, even a
// raw file URL you edit by hand -- there is nothing here to install or run on
// the other end. "New" means changed since the last poll, so overwriting the
// file with a fresh command is what triggers the next action; leaving the same
// text in place does not re-fire it on every poll.
//
// If HW_NET_SSID is left empty this uplink's begin() returns false and
// ready() never returns true, so a build with no credentials configured is
// simply LoRa-only -- the internet uplink is a bonus, never a requirement.
// (HivewireMultiUplink already handles one transport failing to begin(); this
// is that path exercised on purpose.)
//
// DO NOT use this on a device that also runs a HivewireCoordinator or
// HivewireNode over ESP-NOW. WiFi.begin() (station mode) forces the radio
// onto the access point's channel -- a documented ESP32 limitation, not a
// guess -- and if the swarm's ESP-NOW peers are fixed on a different channel,
// which they will be unless the AP happens to match, the WiFi connection
// succeeding silently kills ESP-NOW to the swarm. examples/MeshtasticGateway
// hit exactly this and now uses HivewireSerialUplink.h instead, fed by a
// separate device (a Raspberry Pi, say) with its own WiFi hardware and
// therefore no shared radio to fight over. This uplink is for a device with
// no ESP-NOW swarm at all -- a pure LoRa-to-internet relay -- where the
// conflict cannot arise because there is no second radio user to conflict
// with.

#pragma once
#include <Hivewire.h>
#include <WiFi.h>
#include <HTTPClient.h>

#ifndef HW_NET_SSID
#define HW_NET_SSID ""
#endif
#ifndef HW_NET_PASS
#define HW_NET_PASS ""
#endif
#ifndef HW_NET_CMD_URL
#define HW_NET_CMD_URL ""
#endif

class HivewireHttpUplink : public HivewireUplink {
 public:
  bool begin() override {
    if (!HW_NET_SSID[0] || !HW_NET_CMD_URL[0]) return false;
    WiFi.mode(WIFI_STA);
    WiFi.begin(HW_NET_SSID, HW_NET_PASS);
    _lastPoll = millis() - POLL_MS;   // poll as soon as connected, not 20s later
    return true;
  }

  void loop() override {
    if (!HW_NET_SSID[0]) return;
    if (WiFi.status() != WL_CONNECTED) return;
    if (millis() - _lastPoll < POLL_MS) return;
    _lastPoll = millis();
    poll();
  }

  bool ready() override {
    return HW_NET_SSID[0] && WiFi.status() == WL_CONNECTED;
  }

  // Best-effort telemetry out, only if a status URL was configured. Silence on
  // any failure here -- the LoRa uplink is carrying the same line regardless,
  // and a flaky internet path must never hold up the swarm's own reporting.
  void send(const char *line) override {
#if defined(HW_NET_STATUS_URL)
    if (!ready()) return;
    HTTPClient http;
    WiFiClient client;
    if (!http.begin(client, HW_NET_STATUS_URL)) return;
    http.addHeader("Content-Type", "text/plain");
    http.POST((uint8_t *)line, strlen(line));
    http.end();
#else
    (void)line;
#endif
  }

  void onCommand(CommandCallback cb) override { _cb = cb; }

 private:
  static const uint32_t POLL_MS = 20000;

  void poll() {
    HTTPClient http;
    WiFiClient client;
    if (!http.begin(client, HW_NET_CMD_URL)) return;
    http.setTimeout(8000);
    if (http.GET() == HTTP_CODE_OK) {
      String body = http.getString();
      body.trim();
      if (body.length() && body != _lastCmd) {
        _lastCmd = body;
        if (_cb) _cb(_lastCmd.c_str());
      }
    }
    http.end();
  }

  String _lastCmd;
  uint32_t _lastPoll = 0;
  CommandCallback _cb = nullptr;
};
