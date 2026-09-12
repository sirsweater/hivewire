/*
 * Hivewire -- BasicNode
 *
 * A swarm member. It publishes a sensor reading, accepts a writable output,
 * and follows whatever posture the coordinator advertises.
 *
 * Everything unit-specific lives in the SLOTS table. The library never
 * interprets a slot; slot ids mean whatever your application decides.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>

static const uint8_t NODE_ID = 2;   // must be unique across the swarm

HivewireNode node;

// --- samplers: produce a value, touch nothing else -------------------------
static void sampleLevel(void *out) {
  uint16_t v = analogRead(A0);
  memcpy(out, &v, sizeof(v));
}

static void sampleBattery(void *out) {
  uint8_t v = 100;                  // replace with a real divider read
  memcpy(out, &v, sizeof(v));
}

// --- appliers: the ONLY place a writable slot touches hardware -------------
// Reached only with a value that already matched the declared type, length and
// range. There is nothing left to validate here.
#define RELAY_PIN 10
static uint8_t relayState = 0;

static void applyRelay(const void *in) {
  relayState = *(const uint8_t *)in;      // guaranteed 0 or 1
  digitalWrite(RELAY_PIN, relayState);
}

static void sampleRelay(void *out) {
  memcpy(out, &relayState, sizeof(relayState));
}

// --- the slot table --------------------------------------------------------
//  id  type    dir           sample   report   thresh  min  max  sampler        applier
static const HwSlotDef SLOTS[] = {
  { 1, HW_U16, HW_DIR_OUT,     30000,  300000,      20,   0,   0, sampleLevel,   nullptr },
  { 2, HW_U8,  HW_DIR_OUT,     60000,  900000,       2,   0,   0, sampleBattery, nullptr },
  { 3, HW_U8,  HW_DIR_INOUT,       0,   60000,       0,   0,   1, sampleRelay,   applyRelay },
};
static const uint8_t N_SLOTS = sizeof(SLOTS) / sizeof(SLOTS[0]);

static void onMode(uint8_t mode, uint8_t param) {
  Serial.printf("[mode] -> %u (param %u)\n", mode, param);
  if (mode == HW_MODE_SAFE) {
    relayState = 0;
    digitalWrite(RELAY_PIN, LOW);     // safe state costs nothing to reach
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);

  // Pin directions are set once, here, and never changed. HW_DIR_* gates the
  // protocol; this is what protects the hardware.
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);

  if (!node.begin(NODE_ID, HW_ROLE_ACTUATOR, SLOTS, N_SLOTS)) {
    Serial.println("hivewire: begin failed");
    ESP.restart();
  }
  node.onMode(onMode);
  Serial.printf("hivewire node %u up, %u slots\n", NODE_ID, N_SLOTS);
}

void loop() {
  node.loop();
}
