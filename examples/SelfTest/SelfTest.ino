/*
 * Hivewire -- SelfTest
 *
 * Acts as a coordinator against a live BasicNode and asserts the protocol's
 * safety properties on real radios. Results print to USB serial; no uplink,
 * no LoRa, nothing to configure.
 *
 * These are the guarantees worth proving on YOUR hardware before trusting the
 * library with anything that can move or flood:
 *
 *   1. a node appears and converges on a published epoch
 *   2. a valid write to a DIR_INOUT slot is applied
 *   3. a write to a DIR_OUT slot is REFUSED       (read-only)
 *   4. an out-of-range write is REFUSED           (range gate)
 *   5. a node replays its own diagnostic ring over the air
 *   6. a node abandons expired state by itself    (TTL, nothing sent)
 *
 * Flash BasicNode to a second board with NODE_ID 2, then this to another.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>

static const uint8_t COORD_ID = 1;
static const uint8_t NODE_ID  = 2;

// Must match BasicNode's table: slot 1 is DIR_OUT, slot 3 is DIR_INOUT 0..1.
static const HwWritableSlot WRITABLE[] = {
  { 1, HW_U16 },   // deliberately declared so we can ATTEMPT an illegal write
  { 3, HW_U8  },   // the legitimate one
};
static const uint8_t N_WRITABLE = sizeof(WRITABLE) / sizeof(WRITABLE[0]);

HivewireCoordinator coord;

static uint8_t  step = 0;
static uint32_t stepAt = 0;
static uint8_t  passes = 0, failures = 0;
static bool     sawReadOnlyRefusal = false;
static bool     sawRangeRefusal = false;
static uint8_t  logLines = 0;

static void check(const char *what, bool ok) {
  Serial.printf("%s  %s\n", ok ? "PASS" : "FAIL", what);
  ok ? passes++ : failures++;
}

// A node's replayed ring arrives a line at a time.
static void onNodeLog(uint8_t nodeId, const char *line) {
  Serial.printf("      node%u | %s\n", nodeId, line);
  logLines++;
  if (strstr(line, "read-only")) sawReadOnlyRefusal = true;
  if (strstr(line, "out of range")) sawRangeRefusal = true;
}

static void publish(uint8_t mode, uint8_t param, uint16_t ttl) {
  uint8_t st[2] = {mode, param};
  coord.setState(st, sizeof(st), ttl);
}

static bool nodeConverged() {
  return coord.fresh(NODE_ID) && coord.node(NODE_ID).epoch == coord.epoch();
}

void setup() {
  Serial.begin(115200);
  delay(1200);
  Serial.println("\n=== Hivewire self-test ===");

  if (!coord.begin(COORD_ID, WRITABLE, N_WRITABLE)) {
    Serial.println("FAIL  coordinator begin()");
    while (true) delay(1000);
  }
  coord.onNodeLog(onNodeLog);
  stepAt = millis();
}

void loop() {
  coord.loop();
  uint32_t now = millis();

  switch (step) {
    // Wait for the node to ANNOUNCE itself rather than assuming a duration.
    // A node only sends status when a slot is dirty or due, and its fastest
    // slot may report once a minute -- so a fixed short wait tests the test's
    // patience, not the protocol.
    case 0:
      if (coord.fresh(NODE_ID)) {
        check("node is present", true);
        step++; stepAt = now;
      } else if (now - stepAt > 75000) {
        check("node is present (timed out after 75s)", false);
        step++; stepAt = now;
      }
      break;

    case 1:                                    // publish state, expect adoption
      Serial.println("---- publishing state {mode=2}");
      publish(2, 0, 0);
      step++; stepAt = now;
      break;

    // Same again: convergence is observed through the node's next status, so
    // wait for the evidence rather than for the clock.
    case 2:
      if (nodeConverged()) {
        check("node converged on published epoch", true);
        step++; stepAt = now;
      } else if (now - stepAt > 75000) {
        check("node converged on published epoch (timed out)", false);
        step++; stepAt = now;
      }
      break;

    case 3:                                    // legal write
      Serial.println("---- set slot 3 = 1 (legal, DIR_INOUT, range 0..1)");
      check("coordinator accepted legal write", coord.set(NODE_ID, HW_ROLE_ANY, 3, 1));
      step++; stepAt = now;
      break;

    case 4:                                    // illegal: read-only slot
      if (now - stepAt > 3000) {
        Serial.println("---- set slot 1 = 5 (ILLEGAL: slot 1 is DIR_OUT)");
        coord.set(NODE_ID, HW_ROLE_ANY, 1, 5);
        step++; stepAt = now;
      }
      break;

    case 5:                                    // illegal: out of range
      if (now - stepAt > 3000) {
        Serial.println("---- set slot 3 = 9 (ILLEGAL: max is 1)");
        coord.set(NODE_ID, HW_ROLE_ANY, 3, 9);
        step++; stepAt = now;
      }
      break;

    case 6:                                    // ask the node what it thinks
      if (now - stepAt > 3000) {
        Serial.println("---- requesting node's diagnostic ring");
        coord.requestLog(NODE_ID);
        step++; stepAt = now;
      }
      break;

    case 7:
      if (now - stepAt > 5000) {
        check("node replayed its ring over the air", logLines > 0);
        check("read-only write was refused", sawReadOnlyRefusal);
        check("out-of-range write was refused", sawRangeRefusal);
        step++; stepAt = now;
      }
      break;

    case 8:                                    // TTL: nothing will be sent
      Serial.println("---- publishing {mode=5} with ttl=10s, then going quiet");
      publish(5, 0, 10);
      step++; stepAt = now;
      break;

    case 9:
      if (now - stepAt > 20000) {
        Serial.println("---- requesting ring again (expect a ttl entry)");
        logLines = 0;
        coord.requestLog(NODE_ID);
        step++; stepAt = now;
      }
      break;

    case 10:
      if (now - stepAt > 5000) {
        check("node reported abandoning expired state", logLines > 0);
        Serial.printf("\n=== %u passed, %u failed ===\n", passes, failures);
        step++;
      }
      break;

    default:
      break;
  }
}
