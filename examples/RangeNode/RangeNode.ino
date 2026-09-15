/*
 * Hivewire -- RangeNode
 *
 * A node built to be CARRIED AWAY and left there. Everything it can be asked
 * to do is reachable over the air, because the cost of a missing feature is a
 * walk across the house rather than a recompile.
 *
 * That constraint drove three choices worth stating:
 *
 *   - It answers questions instead of performing a task. The unknown when you
 *     move a unit to the far side of a building is not "does the relay work",
 *     it is "does anything arrive, and with what margin". So it publishes link
 *     quality rather than sensor readings.
 *
 *   - Nothing it can be told is permanent. The deafness filter expires on its
 *     own and no setting survives a power cycle, so the worst outcome of a bad
 *     command is a wait, and pulling the power is always a full reset.
 *
 *   - It heals from the one state no packet can fix. A coordinator that reboots
 *     restarts its epoch at 1, and adoption requires a HIGHER epoch, so a node
 *     whose status never gets home would ignore it forever. After a long orphan
 *     this node forgets its epoch and accepts whatever comes next.
 *
 * Reading the numbers: `status` pulls a digest on demand. Report cadences are
 * deliberately slow because every push costs LoRa airtime -- poll for a
 * snapshot instead of making the node chatty.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>
#include <Preferences.h>

// One firmware, one id per board, chosen at flash time:
//
//   arduino-cli compile --build-property compiler.cpp.extra_flags=-DHW_NODE_ID=3
//
// Use compiler.cpp.extra_flags, NOT build.extra_flags -- the latter REPLACES
// the core's own flags instead of adding to them, which silently drops
// ARDUINO_USB_CDC_ON_BOOT and leaves you with a board that boots and prints
// nothing. Keeping the id out of the source means adding a unit never means
// editing, and two boards can never end up flashed from divergent copies.
#ifndef HW_NODE_ID
#define HW_NODE_ID 2
#endif
static const uint8_t NODE_ID = HW_NODE_ID;   // must be unique across the swarm

HivewireNode node;
Preferences prefs;

// --- observed --------------------------------------------------------------
static uint32_t bootCount    = 0;
static uint32_t adoptions    = 0;
static uint32_t safeEvents   = 0;
static uint32_t orphanSince  = 0;
static uint8_t  lastAction   = 0;

// Duration used by the next deafen command. Kept separate so triggering stays a
// single value write: `set 2 21 600`, then `set 2 20 1`.
static uint16_t deafenSecs = 600;

// A node orphaned this long has already failed safe and holds nothing, so
// accepting a fresh low epoch costs nothing and recovers it from a rebooted
// coordinator it can no longer out-rank.
static const uint32_t FORGET_EPOCH_AFTER_MS = 600000;   // 10 min

// --- samplers --------------------------------------------------------------
static void sUptimeMin(void *o)  { uint16_t v = millis() / 60000UL; memcpy(o, &v, 2); }
static void sBoots(void *o)      { uint8_t  v = bootCount > 255 ? 255 : bootCount; memcpy(o, &v, 1); }
static void sRssiLast(void *o)   { int8_t   v = node.lastRssi();  memcpy(o, &v, 1); }
static void sRssiWorst(void *o)  { int8_t   v = node.worstRssi(); memcpy(o, &v, 1); }
static void sNeighbors(void *o)  { uint8_t  v = node.neighbors(); memcpy(o, &v, 1); }
static void sBeacons(void *o)    { uint32_t c = node.beaconsRx(); uint16_t v = c > 65535 ? 65535 : c; memcpy(o, &v, 2); }
static void sAdoptions(void *o)  { uint16_t v = adoptions > 65535 ? 65535 : adoptions; memcpy(o, &v, 2); }
static void sSafeEvents(void *o) { uint8_t  v = safeEvents > 255 ? 255 : safeEvents; memcpy(o, &v, 1); }
static void sOrphaned(void *o)   { uint8_t  v = node.orphaned() ? 1 : 0; memcpy(o, &v, 1); }
static void sEpoch(void *o)      { uint16_t v = node.epoch() > 65535 ? 65535 : node.epoch(); memcpy(o, &v, 2); }
static void sDeafTarget(void *o) { uint8_t  v = node.deafTarget(); memcpy(o, &v, 1); }
static void sDeafLeft(void *o)   { uint16_t v = node.deafSecsLeft(); memcpy(o, &v, 2); }
static void sDeafSecsCfg(void *o){ memcpy(o, &deafenSecs, 2); }
static void sAction(void *o)     { memcpy(o, &lastAction, 1); }

// Seconds since a beacon last arrived -- the most direct range signal there is.
// It climbs as soon as the link starts dropping packets, long before the node
// gives up altogether. 9999 means nothing has ever been heard, which is a very
// different report from "heard one recently" and must not look like zero.
static void sBeaconAge(void *o) {
  uint32_t age = node.everHeardBeacon() ? node.msSinceBeacon() / 1000 : 9999;
  uint16_t v = age > 65535 ? 65535 : age;
  memcpy(o, &v, 2);
}

// --- appliers --------------------------------------------------------------
// Reached only with a value already checked against type, length and range.
static void aDeafTarget(const void *in) {
  node.deafenTo(*(const uint8_t *)in, deafenSecs);
}

static void aDeafSecs(const void *in) {
  memcpy(&deafenSecs, in, 2);
}

// Occasional maintenance, so a stuck counter or a pessimistic RSSI floor never
// justifies a trip across the house.
static void aAction(const void *in) {
  lastAction = *(const uint8_t *)in;
  switch (lastAction) {
    case 1:                                  // zero the counters
      adoptions = safeEvents = 0;
      node.resetBeaconCount();
      node.log("counters reset");
      break;
    case 2:                                  // re-measure the RSSI floor
      node.resetRssi();
      node.log("rssi reset");
      break;
    case 3:                                  // forget epoch, accept anything
      node.forgetEpoch();
      break;
    case 4:
      node.log("reboot by command");
      delay(50);
      ESP.restart();
      break;
  }
}

// --- slots -----------------------------------------------------------------
// Cadences are long and thresholds wide on purpose: every change pushed from
// here becomes LoRa airtime. Use `status` to pull a snapshot on demand.
//  id  type    dir            sample  report  thresh min   max  sampler       applier
static const HwSlotDef SLOTS[] = {
  {  1, HW_U16, HW_DIR_OUT,     60000, 900000,     5,   0,     0, sUptimeMin,   nullptr },
  {  2, HW_U8,  HW_DIR_OUT,    300000, 900000,     1,   0,     0, sBoots,       nullptr },
  {  3, HW_I8,  HW_DIR_OUT,     15000, 600000,     6,   0,     0, sRssiLast,    nullptr },
  {  4, HW_I8,  HW_DIR_OUT,     15000, 600000,     6,   0,     0, sRssiWorst,   nullptr },
  {  5, HW_U8,  HW_DIR_OUT,     30000, 600000,     1,   0,     0, sNeighbors,   nullptr },
  {  6, HW_U16, HW_DIR_OUT,     30000, 900000,    50,   0,     0, sBeacons,     nullptr },
  {  7, HW_U16, HW_DIR_OUT,     15000, 300000,    30,   0,     0, sBeaconAge,   nullptr },
  {  8, HW_U8,  HW_DIR_OUT,     30000, 900000,     1,   0,     0, sSafeEvents,  nullptr },
  {  9, HW_U16, HW_DIR_OUT,     30000, 900000,     1,   0,     0, sAdoptions,   nullptr },
  { 10, HW_U8,  HW_DIR_OUT,     15000, 600000,     1,   0,     0, sOrphaned,    nullptr },
  { 11, HW_U16, HW_DIR_OUT,     30000, 900000,     1,   0,     0, sEpoch,       nullptr },
  { 12, HW_U8,  HW_DIR_OUT,     15000, 600000,     1,   0,     0, sDeafTarget,  nullptr },
  { 13, HW_U16, HW_DIR_OUT,     15000, 600000,    30,   0,     0, sDeafLeft,    nullptr },
  { 20, HW_U8,  HW_DIR_INOUT,       0, 600000,     0,   0,   255, sDeafTarget,  aDeafTarget },
  { 21, HW_U16, HW_DIR_INOUT,       0, 600000,     0,   0,  1800, sDeafSecsCfg, aDeafSecs   },
  { 22, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0,     4, sAction,      aAction     },
};
static const uint8_t N_SLOTS = sizeof(SLOTS) / sizeof(SLOTS[0]);

static void onState(const uint8_t *state, uint8_t len) {
  adoptions++;
  Serial.printf("[state] mode=%u param=%u ep=%lu rssi=%d\n",
                len > 0 ? state[0] : 0, len > 1 ? state[1] : 0,
                (unsigned long)node.epoch(), node.lastRssi());
}

static void onSafe() {
  safeEvents++;
  Serial.println("[safe] lost the swarm");
}

void setup() {
  Serial.begin(115200);
  delay(200);

  // Reboot count is the one thing worth keeping across a power cycle: it is how
  // an unattended unit reports that its supply is sagging. Deliberately the
  // ONLY persisted value -- anything else would survive the power cycle that is
  // meant to be a guaranteed clean reset.
  prefs.begin("rangenode", false);
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  prefs.end();

  node.onState(onState);
  node.onSafe(onSafe);

  if (!node.begin(NODE_ID, HW_ROLE_SENSOR, SLOTS, N_SLOTS)) {
    Serial.println("hivewire: begin failed");
    delay(1000);
    ESP.restart();                  // never sit dead where nobody can reach it
  }
  node.log("boot #%lu", (unsigned long)bootCount);
  Serial.printf("RangeNode %u up, boot #%lu, %u slots\n",
                NODE_ID, (unsigned long)bootCount, N_SLOTS);
}

void loop() {
  node.loop();
  uint32_t now = millis();
  uint8_t n = node.neighbors();

  // Deep recovery, and only after the node has already failed safe: it holds
  // nothing at that point, so accepting a lower epoch loses nothing.
  if (node.orphaned()) {
    if (!orphanSince) orphanSince = now;
    if (now - orphanSince >= FORGET_EPOCH_AFTER_MS) {
      node.forgetEpoch();
      orphanSince = now;            // re-arm rather than spin
    }
  } else {
    orphanSince = 0;
  }

  static uint32_t lastPrint = 0;
  if (now - lastPrint > 30000) {
    lastPrint = now;
    Serial.printf("up=%lum boots=%lu rssi=%d/%d nb=%u bcn=%lu age=%lus "
                  "ep=%lu orph=%d deaf=%u/%us\n",
                  (unsigned long)(now / 60000), (unsigned long)bootCount,
                  node.lastRssi(), node.worstRssi(), n,
                  (unsigned long)node.beaconsRx(),
                  (unsigned long)(node.everHeardBeacon() ? node.msSinceBeacon() / 1000 : 9999),
                  (unsigned long)node.epoch(), node.orphaned() ? 1 : 0,
                  node.deafTarget(), node.deafSecsLeft());
  }
}
