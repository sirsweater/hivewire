#include "Arduino.h"
uint32_t g_millis = 0; int g_pin[64]; std::map<std::string, unsigned> g_nvs;
#include "HivewirePump.h"
static int fails = 0;
#define CHECK(c, msg) do { if (!(c)) { printf("FAIL: %s\n", msg); fails++; } else printf("ok: %s\n", msg); } while (0)

// One soil reading for the whole run, changed by the scenarios below. A
// capacitive probe reads LOWER when wetter.
static uint16_t g_raw = 2100;
static bool g_probe = true;
static void run(HivewirePump &P, uint32_t ms) {
  for (uint32_t t = 0; t < ms; t += 1000) { g_millis += 1000; P.loop(); P.autoLoop(g_probe, g_raw); }
}
// Run until the pump has been on and is off again (or give up after max_s).
static bool runWatering(HivewirePump &P, uint16_t wetRaw, uint32_t max_s = 3600) {
  bool seenOn = false;
  for (uint32_t s = 0; s < max_s; s++) {
    g_millis += 1000; P.loop(); P.autoLoop(g_probe, g_raw);
    if (P.running() || P.pulsing()) { seenOn = true; g_raw = wetRaw; }
    else if (seenOn) return true;
  }
  return false;
}
static HivewirePump *fresh(HivewireNode &node) {
  g_nvs.clear();
  HivewirePump::_rtc.magic = 0;                  // power-on: nothing to resume
  HivewirePump *P = new HivewirePump(node, 4);
  P->begin();
  P->setFlow(600);                               // 10 ml/s
  P->_autoOn = 1; P->_belowRaw = 2000; P->_autoMl = 100; P->_gapMin = 60; P->_pulses = 1;
  return P;
}

int main() {
  HivewireNode node;
  g_millis = 10000;

  // 1. Fast-draining pot: dry, watered, drains back to where it was within
  //    minutes. That jump is drainage: no hour of "probe disturbed".
  {
    HivewirePump &P = *fresh(node);
    g_raw = 2100; g_probe = true;
    CHECK(runWatering(P, 1500), "dry pot gets an automatic watering");
    CHECK(P._preWaterRaw == 2100, "the reading before the watering is kept");
    run(P, 120000);                               // 2 min wet
    g_raw = 2120;                                 // drained back, 20 counts past where it was
    run(P, 180000);
    CHECK(P.autoState() != HW_AUTO_SETTLING, "draining back after a watering is not a disturbance");
    CHECK(P.autoState() == HW_AUTO_GAP, "...it waits out the minimum gap instead");
    delete &P;
  }

  // 2. Probe pulled out straight after a watering: reads air, far drier than
  //    the pot ever was. Still a disturbance.
  {
    HivewirePump &P = *fresh(node);
    g_raw = 2100; g_probe = true;
    runWatering(P, 1500);
    run(P, 120000);
    g_raw = 3000;                                 // in the air
    run(P, 120000);
    CHECK(P.autoState() == HW_AUTO_SETTLING, "probe pulled after a watering still waits");
    delete &P;
  }

  // 3. The old rule is untouched with no watering in sight.
  {
    HivewirePump &P = *fresh(node);
    g_raw = 1600; g_probe = true;                 // wet, so no automatic watering
    run(P, 600000);
    g_raw = 1800;                                 // +200 in a minute, no dose for ages
    run(P, 120000);
    CHECK(P.autoState() == HW_AUTO_SETTLING, "a dry jump with no recent watering still waits");
    delete &P;
  }

  // 4. Grace runs out: 40 min after the watering, the same jump is a
  //    disturbance again.
  {
    HivewirePump &P = *fresh(node);
    g_raw = 2100; g_probe = true;
    runWatering(P, 1500);
    run(P, 40UL * 60 * 1000);                     // wet and still for 40 min
    g_raw = 2100;
    run(P, 120000);
    CHECK(P.autoState() == HW_AUTO_SETTLING, "after AUTO_DRAIN_GRACE_MS a jump is a disturbance again");
    delete &P;
  }

  // 5. A burst of doses (stream chat): the second, inside the grace, keeps the
  //    reading from before the first rather than the wet soil the first made.
  {
    HivewirePump &P = *fresh(node);
    P._autoOn = 0;                                // manual doses only
    g_raw = 1900; g_probe = true;
    P._autoOn = 1; P._belowRaw = 2500;            // auto on (to judge) but never dry enough to water
    run(P, 120000);
    P.requestDose(50); runWatering(P, 1500, 120);
    CHECK(P._preWaterRaw == 1900, "first dose takes the pre-watering reading");
    run(P, 300000);
    P.requestDose(50); runWatering(P, 1450, 120);
    CHECK(P._preWaterRaw == 1900, "a dose 5 min later keeps it");
    run(P, 120000);
    g_raw = 1920;
    run(P, 120000);
    CHECK(P.autoState() != HW_AUTO_SETTLING, "draining back after the burst is not a disturbance");
    delete &P;
  }

  printf("\n%s\n", fails ? "FAILED" : "all passed");
  return fails != 0;
}
