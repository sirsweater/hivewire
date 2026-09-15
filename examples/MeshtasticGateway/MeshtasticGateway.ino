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

// UART to the Meshtastic node.
//
// These pins let an ESP32-C6 SuperMini sit BACK-TO-BACK on a Heltec V4 with the
// pads meeting directly -- no jumper wires at all. Reversed, the C6's left row
// lands on the Heltec's top row such that GND meets GND and 3V3 meets 3V3:
//
//   C6  8 -> Heltec 40      C6 20  -> Heltec 3V3
//   C6  9 -> Heltec 41      C6 3V3 -> Heltec 3V3
//   C6 14 -> Heltec 42      C6 GND -> Heltec GND
//
// CAREFUL: 8 and 9 are ESP32-C6 strapping pins, so the direction assignment is
// NOT arbitrary. GPIO9 is the boot-mode pin -- low at reset puts the chip in
// download mode instead of running this sketch. So GPIO9 must be the pin WE
// drive (TX): at reset it is high-Z with an internal pullup and always boots.
// The externally-driven line goes to GPIO8, where a low at reset only affects
// ROM log printing.
//
// Reverse these two and you get intermittent boot-into-bootloader failures.
//
// GPIO8 also drives the onboard RGB LED on SuperMini boards, so it will flicker
// with inbound traffic. Cosmetic; the WS2812 data input is high-impedance.
#define LINK_RX_PIN 8    // <- Heltec pad 40 (its TX)
#define LINK_TX_PIN 9    // -> Heltec pad 41 (its RX)
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
  {  3, HW_U8 },     // relay on/off            (BasicNode)
  { 20, HW_U8 },     // deafen to node id, 0=clear   (RangeNode)
  { 21, HW_U16 },    // deafen duration, seconds     (RangeNode)
  { 22, HW_U8 },     // 1=counters 2=rssi 3=forget epoch 4=reboot 5=OTA
  { 23, HW_U8 },     // arm OTA for THIS node id (see HivewireOta.h)
};
static const uint8_t N_WRITABLE = sizeof(WRITABLE) / sizeof(WRITABLE[0]);

HivewireCoordinator coord;

// The only Meshtastic-aware object in this sketch. Swap this line for another
// HivewireUplink implementation and nothing below changes.
MeshtasticUplink gwUplink(LINK_RX_PIN, LINK_TX_PIN, SWARM_CHANNEL, LINK_BAUD);

static uint32_t lastDigest = 0;
static uint16_t lastFaults = 0xFFFF, lastTotal = 0xFFFF;

// ---------------------------------------------------------------------------
// Diagnostic ring
//
// Attaching to this board's USB RESETS it, which destroys the very session you
// are trying to inspect -- debugging by USB changes the thing being debugged.
// So the gateway keeps its own short history in RAM and reports it over the
// mesh on request, and nobody has to plug anything in.
//
// Deliberately small and only sent when asked: LoRa airtime is shared.
// ---------------------------------------------------------------------------
#define LOG_LINES 10
#define LOG_WIDTH 52

static char     logRing[LOG_LINES][LOG_WIDTH];
static uint8_t  logHead = 0;      // next slot to write
static uint8_t  logCount = 0;     // how many are populated

static void logf(const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(logRing[logHead], LOG_WIDTH, fmt, ap);
  va_end(ap);
  Serial.printf("%lus %s\n", (unsigned long)(millis() / 1000), logRing[logHead]);
  logHead = (logHead + 1) % LOG_LINES;
  if (logCount < LOG_LINES) logCount++;
}

static void uplink(const char *line) {
  Serial.printf("[uplink ch%u] %s\n", SWARM_CHANNEL, line);
  gwUplink.send(line);
}

// Dump the ring oldest-first, packed into as few messages as will hold it.
static void sendLog() {
  if (!logCount) { uplink("LOG empty"); return; }
  char buf[MAX_LINE];
  size_t n = 0;
  uint8_t start = (logHead + LOG_LINES - logCount) % LOG_LINES;
  for (uint8_t k = 0; k < logCount; k++) {
    const char *entry = logRing[(start + k) % LOG_LINES];
    size_t need = strlen(entry) + 3;
    if (n && n + need >= MAX_LINE) { uplink(buf); n = 0; }
    if (!n) n = snprintf(buf, sizeof(buf), "L");
    n += snprintf(buf + n, sizeof(buf) - n, " | %s", entry);
  }
  if (n > 1) uplink(buf);
}

// Health line, then per-node slot values chunked across as many lines as
// needed. Never one packet per node.
static void sendDigest() {
  uint16_t total = 0, converged = 0, faults = 0;
  coord.census(&total, &converged, &faults, NODE_STALE_MS);

  char line[96];
  // This example's convention: byte 0 of the opaque state is a "mode".
  uint8_t mode = coord.stateLen() > 0 ? coord.state()[0] : 0;
  snprintf(line, sizeof(line), "HW up=%u ok=%u ep=%lu m=%u flt=%u",
           total, converged, (unsigned long)coord.epoch(), mode, faults);
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
//   log                                    replay the diagnostic ring
static void handleCommand(const char *line) {
  char buf[128];
  snprintf(buf, sizeof(buf), "%s", line);
  logf("cmd %.40s", buf);

  if (!strncmp(buf, "mode", 4)) {
    int m = 0, p = 0, t = 0;
    if (sscanf(buf + 4, "%d %d %d", &m, &p, &t) >= 1 && m >= 0 && m <= 255) {
      // Encode this example's {mode, param} into the opaque state payload.
      uint8_t st[2] = {(uint8_t)m, (uint8_t)p};
      coord.setState(st, sizeof(st), (uint16_t)t);
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
  } else if (!strncmp(buf, "log", 3)) {
    int who = 0;
    // "log" alone replays the gateway's own ring; "log <id>" asks that node
    // for its history over the air. A deployed node has no reachable USB port.
    if (sscanf(buf + 3, "%d", &who) == 1 && who > 0) {
      coord.requestLog((uint8_t)who);
      logf("log req node %d", who);
    } else {
      sendLog();
    }
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
  gwUplink.onLog([](const char *m) { logf("%s", m); });

  // A node's replayed history arrives a line at a time; relay each one out.
  coord.onNodeLog([](uint8_t nodeId, const char *line) {
    char out[96];
    snprintf(out, sizeof(out), "N%u | %s", nodeId, line);
    uplink(out);
  });
  if (!gwUplink.begin()) {
    Serial.println("hivewire: uplink begin failed");
    ESP.restart();
  }

  lastDigest = millis();
  logf("boot ok");
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
