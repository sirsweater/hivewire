/*
 * Hivewire -- MeshtasticGateway
 *
 * Bridges an ESP-NOW swarm to a long-range LoRa link by acting as a Meshtastic
 * CLIENT over UART:
 *
 *   phone / remote radio <--LoRa--> Meshtastic node <--UART--> this <--ESP-NOW--> swarm
 *
 * Set the Meshtastic node's Serial Module to PROTO mode at 115200 and point its
 * rxd/txd at the pins below. PROTO exposes the full protobuf client API -- the
 * same one the phone app speaks -- which is what lets us choose a channel per
 * message and, crucially, see which channel an incoming command arrived on.
 *
 * That matters: the Serial Module's TEXTMSG mode publishes on the PRIMARY
 * channel only and cannot tell you the sender's channel, so anyone in radio
 * range could command the swarm. Here we reject anything not on our private
 * channel, and the node's primary can stay on the public mesh.
 *
 * The uplink carries CHANGES and periodic digests, never a per-node stream --
 * LoRa airtime is shared and a packet costs the better part of a second.
 * If the LoRa link dies the swarm keeps running its last goal, by design.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>
#include "MeshtasticUplink.h"

static const uint8_t GATEWAY_ID = 1;

// UART to the Meshtastic node. Chosen for physical convenience on the ESP32-C6
// SuperMini: its left row runs GND, 3V3, 20, 19, so one header strip picks up
// ground and both data lines.
#define LINK_RX_PIN 19
#define LINK_TX_PIN 20
#define LINK_BAUD   115200

// Channel index carrying swarm traffic. 0 is the public primary -- never use it
// for this. Commands arriving on any other channel are refused.
#define SWARM_CHANNEL 1

static const uint32_t DIGEST_PERIOD_MS  = 900000;  // 15 min routine uplink
static const uint32_t DIGEST_MIN_GAP_MS = 60000;   // floor between uplinks
static const uint32_t NODE_STALE_MS     = 120000;
static const size_t   MAX_LINE          = 180;     // Meshtastic caps near 237

// HW_DIR_IN slots are never published, so the coordinator cannot learn their
// type from traffic. Declare anything you intend to write.
static const HwWritableSlot WRITABLE[] = {
  { 3, HW_U8 },      // relay on/off
};
static const uint8_t N_WRITABLE = sizeof(WRITABLE) / sizeof(WRITABLE[0]);

HivewireCoordinator coord;

// The only Meshtastic-aware object in this sketch. Swap this line for another
// HivewireUplink implementation and nothing below changes.
MeshtasticUplink gwUplink(LINK_RX_PIN, LINK_TX_PIN, SWARM_CHANNEL, LINK_BAUD);

static uint32_t lastDigest = 0;
static uint16_t lastFaults = 0xFFFF, lastTotal = 0xFFFF;

static void uplink(const char *line) {
  Serial.printf("[uplink ch%u] %s\n", SWARM_CHANNEL, line);
  gwUplink.send(line);
}

// Health line, then per-node slot values chunked across as many lines as
// needed. Never one packet per node.
static void sendDigest() {
  uint16_t total = 0, converged = 0, faults = 0;
  coord.census(&total, &converged, &faults, NODE_STALE_MS);

  char line[96];
  snprintf(line, sizeof(line), "HW up=%u ok=%u ep=%lu m=%u flt=%u",
           total, converged, (unsigned long)coord.epoch(), coord.mode(), faults);
  uplink(line);

  lastFaults = faults;
  lastTotal = total;
  lastDigest = millis();
  if (!total) return;

  char buf[MAX_LINE];
  size_t n = 0;
  uint8_t chunk = 1;
  for (int i = 0; i < 256; i++) {
    if (!coord.fresh((uint8_t)i, NODE_STALE_MS)) continue;
    const HivewireCoordinator::NodeRec &r = coord.node((uint8_t)i);
    for (uint8_t s = 0; s < HIVEWIRE_MAX_SLOTS; s++) {
      if (!r.slots[s].valid) continue;
      char item[32];
      int m = snprintf(item, sizeof(item), " %d.%u=%ld", i, r.slots[s].id,
                       (long)hwSlotAsInt(r.slots[s].type, r.slots[s].raw));
      if (m <= 0) continue;
      if (!n) n = snprintf(buf, sizeof(buf), "D%u", chunk);
      if (n + m >= MAX_LINE) {
        uplink(buf);
        chunk++;
        n = snprintf(buf, sizeof(buf), "D%u", chunk);
      }
      memcpy(buf + n, item, m + 1);
      n += m;
    }
  }
  if (n > 2) uplink(buf);
}

// Uplink early when the picture materially changed, rate-limited so it can
// never degrade into a stream.
static void checkTriggers() {
  if (millis() - lastDigest < DIGEST_MIN_GAP_MS) return;
  uint16_t total = 0, faults = 0;
  coord.census(&total, nullptr, &faults, NODE_STALE_MS);
  if (faults != lastFaults || total != lastTotal) sendDigest();
}

// Commands from the remote operator:
//   mode <n> [param] [ttl]                 posture; reaches every unit
//   set <all|rN|id> <slot> <value>         write a slot
//   status                                 force a digest now
static void handleCommand(const char *line) {
  char buf[128];
  snprintf(buf, sizeof(buf), "%s", line);

  if (!strncmp(buf, "mode", 4)) {
    int m = 0, p = 0, t = 0;
    if (sscanf(buf + 4, "%d %d %d", &m, &p, &t) >= 1 && m >= 0 && m <= 255) {
      coord.setState((uint8_t)m, (uint8_t)p, (uint16_t)t);
      char ack[64];
      snprintf(ack, sizeof(ack), "ACK mode=%d ep=%lu", m, (unsigned long)coord.epoch());
      uplink(ack);
    } else {
      uplink("ERR usage: mode <n> [param] [ttl]");
    }
  } else if (!strncmp(buf, "set", 3)) {
    char tgt[16] = {0};
    int slot = 0;
    long val = 0;
    if (sscanf(buf + 3, "%15s %d %ld", tgt, &slot, &val) == 3) {
      uint8_t id = HIVEWIRE_TARGET_ALL, role = HW_ROLE_ANY;
      if (!strcmp(tgt, "all"))  { /* wildcards already set */ }
      else if (tgt[0] == 'r')   role = (uint8_t)atoi(tgt + 1);
      else                      id = (uint8_t)atoi(tgt);

      char ack[64];
      if (coord.set(id, role, (uint8_t)slot, (int32_t)val))
        snprintf(ack, sizeof(ack), "ACK set %d=%ld", slot, val);
      else
        snprintf(ack, sizeof(ack), "ERR slot %d not writable", slot);
      uplink(ack);
    } else {
      uplink("ERR usage: set <all|rN|id> <slot> <value>");
    }
  } else if (!strncmp(buf, "status", 6)) {
    sendDigest();
  }
  // Anything else is ignored in silence -- this channel carries human chat too.
}

void setup() {
  Serial.begin(115200);
  delay(600);

  if (!coord.begin(GATEWAY_ID, WRITABLE, N_WRITABLE)) {
    Serial.println("hivewire: begin failed");
    ESP.restart();
  }

  // The uplink is responsible for rejecting untrusted senders before this
  // callback is ever reached -- see MeshtasticUplink::onText.
  gwUplink.onCommand(handleCommand);
  if (!gwUplink.begin()) {
    Serial.println("hivewire: uplink begin failed");
    ESP.restart();
  }

  lastDigest = millis();
  Serial.println("hivewire gateway up");
}

void loop() {
  coord.loop();
  gwUplink.loop();

  if (gwUplink.ready()) {
    if (millis() - lastDigest > DIGEST_PERIOD_MS) sendDigest();
    checkTriggers();
  }
}
