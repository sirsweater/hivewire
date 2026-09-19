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
#include <HivewireMultiUplink.h>
#include <HivewireSerialUplink.h>
#include <HivewireFirmware.h>

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
  { 24, HW_U8 },     // seed this node's firmware to node N, 255 = all (RangeNode)
};
static const uint8_t N_WRITABLE = sizeof(WRITABLE) / sizeof(WRITABLE[0]);

HivewireCoordinator coord;

// The only Meshtastic-aware object in this sketch. Swap this line for another
// HivewireUplink implementation and nothing below changes.
MeshtasticUplink gwUplink(LINK_RX_PIN, LINK_TX_PIN, SWARM_CHANNEL, LINK_BAUD);

// LoRa always exists here and needs no configuration to work. The second
// uplink is deliberately NOT a WiFi one on this chip: this gateway already
// runs ESP-NOW for the swarm, and WiFi.begin() (station mode) forces the
// radio onto the access point's channel -- documented ESP32 behaviour, not a
// guess -- which silently kills ESP-NOW to the swarm the moment it connects,
// unless the AP happens to sit on the exact same channel the swarm uses.
// HivewireSerialUplink sidesteps the problem by construction: feed commands
// (and, via HivewireSerialProvider below, firmware bytes) to this gateway
// over USB from something with its OWN separate WiFi hardware -- a Raspberry
// Pi is the obvious choice -- and there is no shared radio to fight over.
// (HivewireHttpUplink.h still exists for a gateway that does NOT also run
// ESP-NOW -- a pure LoRa<->internet relay with no swarm -- where this
// conflict cannot arise; its own header explains the tradeoff.)
HivewireSerialUplink netUplink;

// Every command handler below reads from this instead of from gwUplink
// directly, so a command arriving on LoRa and one arriving over the USB link
// both reach the exact same code path.
HivewireMultiUplink links;

// Lets a command trigger the hive to pull an image and push it into the swarm
// over ESP-NOW -- see the "push" command below. Built on the same
// HivewireFwSender used by the FirmwarePush example; only the Provider (where
// the bytes come from) differs, and here it is the USB link itself.
HivewireFwSender      fwSender(coord);
HivewireSerialProvider fwBytes;

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
  links.send(line);   // replicated to every transport that is currently up
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

// Commands from the remote operator, arriving over LoRa, the USB link, or
// both:
//   mode <n> [param] [ttl]                 posture; reaches every unit
//   set <all|rN|id> <slot> <value>         write a slot
//   status                                 force a digest now
//   log                                    replay the diagnostic ring
//   push <len> <crc32>                     arm a firmware transfer; the bytes
//                                          come over USB -- see below
static void handleCommand(const char *line) {
  // Sized to Meshtastic's own ceiling (see MAX_LINE above), comfortably wider
  // than any command here actually needs -- so nothing this receives over
  // LoRa could have arrived any longer anyway.
  char buf[200];
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
  } else if (!strncmp(buf, "push", 4)) {
    // Triggered over LoRa or the internet (whichever reaches this gateway),
    // but the BYTES only ever come over the USB link to whatever is plugged
    // in there -- a Raspberry Pi with its own internet access, matching the
    // architecture note above the uplink declarations. This command just
    // arms the transfer and announces size/CRC; the Pi is expected to have
    // already fetched the image and to start streaming it the moment it sees
    // "READY", using the exact MORE-driven protocol FirmwarePush.ino documents.
    //
    // No arm/trigger split here, unlike HivewireOta.h's per-node WiFi pull.
    // That split exists specifically because a broadcast trigger there would
    // let ONE command update every node's own WiFi credentials/URL handling
    // at once with no independent safety net underneath. This path is
    // different: the hive is fed ONCE over USB, then hands the bytes to the
    // SAME ESP-NOW distributor proven over nine burn cycles to always refuse
    // a corrupt or incomplete image and keep every node running its current
    // firmware. The access control that matters is already in place -- only
    // someone with the private channel's key, or access to the USB host,
    // can issue this at all.
    if (fwSender.active()) {
      uplink("ERR push already in progress");
    } else {
      unsigned long len = 0, crc = 0;
      if (sscanf(buf + 4, "%lu %lu", &len, &crc) == 2 && len) {
        // Stop the command-line reader from fighting the byte-transfer reader
        // over the same Serial stream -- see HivewireSerialUplink.h. Resumed
        // in loop() the moment fwSender goes inactive again.
        netUplink.pause();
        fwBytes.start(len);
        fwSender.begin(HIVEWIRE_TARGET_ALL, (uint32_t)len, (uint32_t)crc,
                       HivewireSerialProvider::feed);
        logf("push %lu b", len);
        Serial.println("READY");
      } else {
        uplink("ERR usage: push <len> <crc32>");
      }
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
  // callback is ever reached -- see MeshtasticUplink::onText. HivewireHttpUplink
  // has no equivalent notion of "channel" to police; its own access control is
  // whatever protects the URL it polls (network reachability, an unguessable
  // path, HTTP auth if the host in front of it adds one -- Hivewire does not
  // impose a scheme here).
  links.add(&gwUplink);
  links.add(&netUplink);
  links.onCommand(handleCommand);
  gwUplink.onLog([](const char *m) { logf("%s", m); });

  // A node's replayed history arrives a line at a time; relay each one out.
  coord.onNodeLog([](uint8_t nodeId, const char *line) {
    char out[96];
    snprintf(out, sizeof(out), "N%u | %s", nodeId, line);
    uplink(out);
  });
  // Firmware bytes for the swarm arrive on message types the core does not
  // define; onRaw() is exactly the escape hatch built for that.
  coord.onRaw([](const uint8_t *d, int n) { fwSender.ingest(d, n); });

  if (!links.begin()) {
    // Both transports failed, or neither was configured -- the LoRa path is
    // the one that must always work, so treat total failure here the same as
    // the old single-uplink gateway always did.
    Serial.println("hivewire: uplink begin failed");
    ESP.restart();
  }

  lastDigest = millis();
  logf("boot ok");
  Serial.printf("hivewire gateway up (%u/%u uplinks live)\n",
                links.upCount(), links.linkCount());
}

void loop() {
  coord.loop();
  links.loop();
  fwSender.loop();

  // Resume the command reader the moment the transfer it was paused for ends
  // -- successfully or not. Tracking the transition (not just "is active")
  // is what stops this from calling resume() every single iteration; harmless
  // either way, but the edge is the actual event worth noticing.
  static bool wasActive = false;
  if (wasActive && !fwSender.active()) {
    netUplink.resume();
    // Tell the USB host the transfer is over, rather than leaving it to infer
    // that from a long silence. The retry count is the gateway's own tally of
    // subchunks it had to re-ask for; the host keeps one too, and the two
    // should agree.
    Serial.printf("PUSH END usb_retries=%lu\n", (unsigned long)fwBytes.retries());
  }
  wasActive = fwSender.active();

  if (links.ready()) {
    if (millis() - lastDigest > DIGEST_PERIOD_MS) sendDigest();
    checkTriggers();
  }
}
