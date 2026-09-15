// Hivewire -- optional over-the-air firmware update.
//
// Copyright 2026 the Hivewire authors.
// SPDX-License-Identifier: Apache-2.0
//
// HEADER-ONLY AND OPT-IN. Including it pulls in WiFi, HTTPClient and Update;
// the library core stays pure ESP-NOW with no dependencies, exactly as the
// Meshtastic bridge is confined to one file. A swarm that never updates in the
// field should not pay for a WiFi stack.
//
// The swarm carries the TRIGGER, WiFi carries the BYTES. That split matters:
//
//   - ESP-NOW could carry the image, but a ~1 MB build is ~4000 packets and
//     would need acknowledgements, windowing and retransmission -- the exact
//     machinery this library avoids because that is where the bugs live.
//   - LoRa cannot carry it at all. At the ~1-2 kbps a Meshtastic link really
//     achieves, 1 MB is over 65 hours of continuous airtime before duty-cycle
//     limits are even considered.
//   - WiFi moves it in seconds, and the node is off the swarm only while it
//     downloads.
//
// So the command path that already works reliably is used to say "go update",
// and nothing more.
//
// ---------------------------------------------------------------------------
// The two safety properties
// ---------------------------------------------------------------------------
//
// ARMING. An update must name ONE node. `set all 22 5` bricking every unit at
// once is the failure this design cannot recover from, so triggering requires
// first writing this node's own id to the arm slot. A broadcast arm still only
// matches the node whose id was named, and the arm expires on its own, so a
// forgotten one cannot fire later.
//
// SELF-REVERT. A node records "updated, unconfirmed" in NVS before rebooting
// into the new image. If that image cannot hear the swarm within
// CONFIRM_MS, it switches the boot partition back and restarts. Without this a
// bad build strands a unit somewhere inconvenient and the only fix is a walk
// and a cable -- which is the cost this whole feature exists to remove. Done in
// application code rather than relying on the bootloader's rollback, which the
// Arduino core does not enable by default.
//
// Credentials are compile-time, so nothing site-specific is ever committed:
//
//   --build-property compiler.cpp.extra_flags="-DHW_OTA_SSID=\"net\"
//       -DHW_OTA_PASS=\"pw\" -DHW_OTA_URL=\"http://host/fw.bin\""
//
// Leave HW_OTA_SSID undefined and the whole feature refuses politely.

#pragma once
#include <Hivewire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Update.h>
#include <Preferences.h>
#include <esp_ota_ops.h>

#ifndef HW_OTA_SSID
#define HW_OTA_SSID ""
#endif
#ifndef HW_OTA_PASS
#define HW_OTA_PASS ""
#endif
#ifndef HW_OTA_URL
#define HW_OTA_URL ""
#endif

class HivewireOta {
 public:
  typedef void (*BeforeCallback)(void);

  explicit HivewireOta(HivewireNode &node) : _node(node) {}

  // Called before the radio goes down. The node is about to stop hearing the
  // swarm entirely, so anything it drives must be released FIRST -- an update
  // is a deliberate outage and should reach the same safe state a lost
  // coordinator would.
  void onBeforeUpdate(BeforeCallback cb) { _before = cb; }

  // Call after node.begin(). Picks up an unconfirmed update from the reboot
  // that just happened, if there was one.
  void begin() {
    Preferences p;
    p.begin(NVS_NS, false);
    bool pending = p.getUChar(NVS_KEY, 0) != 0;
    p.end();
    if (!pending) return;
    _confirming = true;
    _confirmBy = millis() + CONFIRM_MS;
    _node.log("ota: new image, verifying");
  }

  void loop() {
    uint32_t now = millis();

    if (_confirming) {
      // Proof the new image works is simple: it can hear the swarm again.
      if (_node.neighbors() > 0 && !_node.orphaned()) {
        clearPending();
        _confirming = false;
        _node.log("ota: image confirmed");
      } else if ((int32_t)(now - _confirmBy) >= 0) {
        _node.log("ota: no swarm, reverting");
        revert();                       // does not return
      }
    }

    if (_pending && (int32_t)(now - _startAt) >= 0) {
      _pending = false;
      run();
    }
  }

  // --- called from slot appliers ------------------------------------------

  // Arm. Only the node whose id is named can be armed, so a broadcast is safe.
  void arm(uint8_t nodeId) {
    if (nodeId != _node.id()) return;   // addressed to somebody else
    _armedBy = millis() + ARM_WINDOW_MS;
    _node.log("ota: armed %us", (unsigned)(ARM_WINDOW_MS / 1000));
  }

  // Trigger. Refuses unless armed, and says so in the ring -- from outside, a
  // refused update and a lost command look identical otherwise.
  bool trigger() {
    if (!HW_OTA_SSID[0]) { _node.log("ota: not configured"); return false; }
    if (!_armedBy || (int32_t)(millis() - _armedBy) >= 0) {
      _node.log("ota: refused, not armed");
      return false;
    }
    _armedBy = 0;
    _pending = true;
    _startAt = millis() + 400;          // let the reply digest get out first
    _node.log("ota: starting");
    return true;
  }

  bool updating() const { return _pending || _confirming; }

 private:
  static const uint32_t ARM_WINDOW_MS = 120000;   // 2 min to follow through
  static const uint32_t CONFIRM_MS    = 180000;   // 3 min to rejoin, or revert
  static const uint32_t WIFI_WAIT_MS  = 20000;
  static constexpr const char *NVS_NS  = "hwota";
  static constexpr const char *NVS_KEY = "pend";

  void setPending() {
    Preferences p; p.begin(NVS_NS, false); p.putUChar(NVS_KEY, 1); p.end();
  }
  void clearPending() {
    Preferences p; p.begin(NVS_NS, false); p.putUChar(NVS_KEY, 0); p.end();
  }

  // Boot the partition we came from. After a successful update the running
  // image is the new one, so the "next update" partition is the old one -- the
  // image that was demonstrably working a few minutes ago.
  void revert() {
    const esp_partition_t *prev = esp_ota_get_next_update_partition(NULL);
    clearPending();
    if (prev && esp_ota_set_boot_partition(prev) == ESP_OK) {
      delay(100);
      ESP.restart();
    }
    // Nothing left to try. Clearing the flag at least stops a reboot loop.
    _node.log("ota: revert failed");
    _confirming = false;
  }

  void run() {
    if (_before) _before();             // release anything we drive, first

    _node.log("ota: wifi %.12s", HW_OTA_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(HW_OTA_SSID, HW_OTA_PASS);
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_WAIT_MS) delay(200);
    if (WiFi.status() != WL_CONNECTED) {
      _node.log("ota: wifi failed");
      return rejoin();
    }

    HTTPClient http;
    http.setTimeout(15000);
    if (!http.begin(HW_OTA_URL)) { _node.log("ota: bad url"); return rejoin(); }
    int code = http.GET();
    if (code != HTTP_CODE_OK) {
      _node.log("ota: http %d", code);
      http.end();
      return rejoin();
    }

    int len = http.getSize();
    if (len <= 0) { _node.log("ota: no length"); http.end(); return rejoin(); }
    if (!Update.begin((size_t)len)) {
      _node.log("ota: no space %d", len);
      http.end();
      return rejoin();
    }

    size_t wrote = Update.writeStream(*http.getStreamPtr());
    http.end();
    if (wrote != (size_t)len || !Update.end(true)) {
      _node.log("ota: write %u/%d", (unsigned)wrote, len);
      Update.abort();
      return rejoin();
    }

    // Mark BEFORE restarting: the flag is what makes the next boot provisional.
    setPending();
    _node.log("ota: ok %d, rebooting", len);
    delay(200);
    ESP.restart();
  }

  // Any failure before the reboot leaves the running image untouched, so the
  // only thing to undo is the radio. Restarting is the honest way back: the
  // ESP-NOW state was torn down by bringing WiFi up, and half-restoring it by
  // hand is how you get a node that is up but deaf.
  void rejoin() {
    _node.log("ota: aborted, restarting");
    delay(200);
    ESP.restart();
  }

  HivewireNode &_node;
  BeforeCallback _before = nullptr;
  bool     _pending = false;
  bool     _confirming = false;
  uint32_t _armedBy = 0;
  uint32_t _startAt = 0;
  uint32_t _confirmBy = 0;
};
