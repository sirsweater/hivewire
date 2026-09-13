// A HivewireUplink built on the Meshtastic client API.
//
// This file is the ONLY place Hivewire touches Meshtastic. The library core
// never includes it. Swap this for a LoRaWAN, cellular or satellite
// implementation and the swarm protocol is untouched.
//
// Requires the Meshtastic node's Serial Module set to PROTO mode at 115200
// with rxd/txd pointed at the pins passed to the constructor. PROTO exposes
// the full protobuf client API -- the same one the phone app speaks -- which
// is what lets us pick a channel per message and, crucially, SEE which channel
// an inbound message arrived on.
//
// That last part is the security property. The Serial Module's TEXTMSG mode
// publishes on the primary channel only and never reports the sender's
// channel, so anyone in radio range could issue commands. Here we refuse
// anything that did not arrive on the private channel.
//
// SPDX-License-Identifier: Apache-2.0

#pragma once
#include <Hivewire.h>
#include <Meshtastic.h>

// The library's callbacks are plain C function pointers with no user data, so
// the active instance is held here. One uplink per sketch.
class MeshtasticUplink;
static MeshtasticUplink *g_mtUplink = nullptr;

class MeshtasticUplink : public HivewireUplink {
 public:
  MeshtasticUplink(int8_t rxPin, int8_t txPin, uint8_t channelIndex,
                   uint32_t baud = 115200)
      : _rx(rxPin), _tx(txPin), _ch(channelIndex), _baud(baud) {}

  bool begin() override {
    g_mtUplink = this;
    mt_serial_init(_rx, _tx, _baud);
    set_text_message_callback(&MeshtasticUplink::onText);
    handshake();
    return true;
  }

  void loop() override {
    uint32_t now = millis();
    _ready = mt_loop(now);

    // The node only forwards received packets to a client that has completed a
    // want_config handshake. If the NODE reboots, it forgets us -- but our
    // heartbeats keep succeeding, so nothing looks wrong: digests still go out
    // and inbound commands silently vanish. A roof-mounted node WILL reboot on
    // a power blip, so re-handshake periodically rather than assuming the one
    // at boot lasts forever.
    if (_ready && now - _lastHandshake >= HANDSHAKE_REFRESH_MS) handshake();
  }
  bool ready() override { return _ready; }

  void send(const char *line) override {
    if (!_ready) return;
    mt_send_text(line, BROADCAST_ADDR, _ch);
  }

  void onCommand(CommandCallback cb) override { _cb = cb; }

  uint8_t channel() const { return _ch; }

 private:
  // Cheap: this is a UART exchange with the attached node, not LoRa airtime.
  static const uint32_t HANDSHAKE_REFRESH_MS = 240000;   // 4 min

  void handshake() {
    _lastHandshake = millis();
    // Always pass the callback: the library nulls its stored pointer once a
    // handshake completes, and calls it without a null check if another
    // config_complete arrives. Re-arming it every time avoids that.
    mt_request_node_report(&MeshtasticUplink::onConnected);
  }

  static void onConnected(mt_node_t *node, mt_nr_progress_t progress) {
    if (g_mtUplink && !g_mtUplink->_announced) {
      g_mtUplink->_announced = true;
      Serial.println("[uplink] Meshtastic node connected");
    }
  }

  // Refuse anything that did not arrive on our private channel. This is the
  // check no Serial Module mode could perform.
  static void onText(uint32_t from, uint32_t to, uint8_t channel,
                     const char *text) {
    if (!g_mtUplink) return;
    if (channel != g_mtUplink->_ch) {
      Serial.printf("[uplink] REFUSED ch=%u (not swarm channel): %s\n",
                    channel, text);
      return;
    }
    Serial.printf("[uplink] cmd ch=%u from=0x%08lx: %s\n", channel,
                  (unsigned long)from, text);
    if (g_mtUplink->_cb) g_mtUplink->_cb(text);
  }

  int8_t _rx, _tx;
  uint8_t _ch;
  uint32_t _baud;
  bool _ready = false;
  bool _announced = false;
  uint32_t _lastHandshake = 0;
  CommandCallback _cb = nullptr;
};
