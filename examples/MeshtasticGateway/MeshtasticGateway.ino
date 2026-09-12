/*
 * Hivewire -- MeshtasticGateway
 *
 * Bridges an ESP-NOW swarm to a long-range LoRa link by talking to a Meshtastic
 * node over UART:
 *
 *   phone / remote radio <--LoRa--> Meshtastic node <--UART--> this <--ESP-NOW--> swarm
 *
 * Configure the Meshtastic node's Serial Module in TEXTMSG mode at 115200 and
 * point its rxd/txd at whatever pins you wire to. TEXTMSG means a stock phone
 * app can read the digests and issue commands with no custom software.
 *
 * The uplink carries CHANGES and periodic digests, never a per-node stream --
 * LoRa airtime is shared and a packet costs the better part of a second.
 * If the LoRa link dies the swarm keeps running its last goal, which is the
 * intended behaviour rather than a failure.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>

static const uint8_t GATEWAY_ID = 1;

// UART to the Meshtastic node. Check these against YOUR board before wiring.
// Avoid strapping and boot-duty pins -- on the ESP32-C6 SuperMini that rules
// out IO4/IO5/IO6, plus IO8 and IO15 (onboard LEDs) and IO9 (boot mode).
// IO0-IO3 are the safe picks there; 2 and 3 leave 0 and 1 free for ADC.
#define LINK_RX_PIN 2
#define LINK_TX_PIN 3
#define LINK_BAUD   115200
#define LINK        Serial1

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

static uint32_t lastDigest = 0;
static uint16_t lastFaults = 0xFFFF, lastTotal = 0xFFFF;

// Health line, then per-node slot values chunked across as many lines as
// needed. Never one packet per node.
static void sendDigest() {
  uint16_t total = 0, converged = 0, faults = 0;
  coord.census(&total, &converged, &faults, NODE_STALE_MS);

  char line[96];
  snprintf(line, sizeof(line), "HW up=%u ok=%u ep=%lu m=%u flt=%u",
           total, converged, (unsigned long)coord.epoch(), coord.mode(), faults);
  LINK.println(line);
  Serial.printf("[uplink] %s\n", line);

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
        LINK.println(buf);
        chunk++;
        n = snprintf(buf, sizeof(buf), "D%u", chunk);
      }
      memcpy(buf + n, item, m + 1);
      n += m;
    }
  }
  if (n > 2) LINK.println(buf);
}

// Uplink early when the picture materially changed, rate-limited so it can
// never degrade into a stream.
static void checkTriggers() {
  if (millis() - lastDigest < DIGEST_MIN_GAP_MS) return;
  uint16_t total = 0, faults = 0;
  coord.census(&total, nullptr, &faults, NODE_STALE_MS);
  if (faults != lastFaults || total != lastTotal) sendDigest();
}

// Commands arriving from the remote operator:
//   mode <n> [param] [ttl]                 posture; reaches every unit
//   set <all|rN|id> <slot> <value>         write a slot
//   status                                 force a digest now
static void handleCommand(char *line) {
  if (!strncmp(line, "mode", 4)) {
    int m = 0, p = 0, t = 0;
    if (sscanf(line + 4, "%d %d %d", &m, &p, &t) >= 1 && m >= 0 && m <= 255) {
      coord.setState((uint8_t)m, (uint8_t)p, (uint16_t)t);
      LINK.printf("ACK mode=%d ep=%lu\n", m, (unsigned long)coord.epoch());
    } else {
      LINK.println("ERR usage: mode <n> [param] [ttl]");
    }
  } else if (!strncmp(line, "set", 3)) {
    char tgt[16] = {0};
    int slot = 0;
    long val = 0;
    if (sscanf(line + 3, "%15s %d %ld", tgt, &slot, &val) == 3) {
      uint8_t id = HIVEWIRE_TARGET_ALL, role = HW_ROLE_ANY;
      if (!strcmp(tgt, "all"))  { /* wildcards already set */ }
      else if (tgt[0] == 'r')   role = (uint8_t)atoi(tgt + 1);
      else                      id = (uint8_t)atoi(tgt);

      if (coord.set(id, role, (uint8_t)slot, (int32_t)val))
        LINK.printf("ACK set %u=%ld\n", slot, val);
      else
        LINK.printf("ERR slot %d not declared writable\n", slot);
    } else {
      LINK.println("ERR usage: set <all|rN|id> <slot> <value>");
    }
  } else if (!strncmp(line, "status", 6)) {
    sendDigest();
  } else {
    LINK.println("ERR unknown cmd");
  }
}

void setup() {
  Serial.begin(115200);
  LINK.begin(LINK_BAUD, SERIAL_8N1, LINK_RX_PIN, LINK_TX_PIN);
  delay(200);

  if (!coord.begin(GATEWAY_ID, WRITABLE, N_WRITABLE)) {
    Serial.println("hivewire: begin failed");
    ESP.restart();
  }
  Serial.println("hivewire gateway up");
}

void loop() {
  coord.loop();

  if (millis() - lastDigest > DIGEST_PERIOD_MS) sendDigest();
  checkTriggers();

  static char buf[128];
  static size_t n = 0;
  while (LINK.available()) {
    char c = LINK.read();
    if (c == '\n' || c == '\r') {
      if (n) { buf[n] = 0; handleCommand(buf); n = 0; }
    } else if (n < sizeof(buf) - 1) {
      buf[n++] = c;
    }
  }
}
