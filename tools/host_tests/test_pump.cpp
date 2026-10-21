// The pump's "ml in the last 24 h" (slot 45) and the day cap built on it: rounding, a true rolling window, and
// restarts. Before this, the total was zeroed every 24 h from boot (fixed blocks) and each run was rounded down.
#include "Arduino.h"
uint32_t g_millis = 0; int g_pin[64]; std::map<std::string, unsigned> g_nvs;
unsigned g_lastErr = 0, g_lastErrSubject = 0;
#include "HivewirePump.h"
static int fails = 0, checks = 0;
#define CHECK(c, msg) do { checks++; if (!(c)) { printf("FAIL: %s\n", msg); fails++; } else printf("ok: %s\n", msg); } while (0)
static const uint32_t H = 3600UL * 1000, M = 60UL * 1000;

// Time passes in `step` ms ticks with loop() running, as on the board (its loop runs every few ms).
static void run(HivewirePump &P, uint32_t ms, uint32_t step = 1000) {
  for (uint32_t t = 0; t < ms; t += step) { g_millis += step; P.loop(); }
}
// One dose to the end, 1 ms ticks while it runs.
static void dose(HivewirePump &P, uint16_t ml) {
  P.requestDose(ml); P.loop();
  while (P.running()) { g_millis += 1; P.loop(); }
}
// A fresh board: empty NVS (default caps), and RTC memory gone as after a power-on.
static HivewirePump *fresh(HivewireNode &node, uint32_t at) {
  g_nvs.clear();
  HivewirePump::_rtc.magic = 0;
  g_millis = at;
  HivewirePump *P = new HivewirePump(node, 4);
  P->begin();
  P->requestSetting(HW_PUMP_SLOT_FLOW, 95); P->loop();          // node 7's calibration
  return P;
}

int main() {
  HivewireNode node;
  HivewirePump *P = fresh(node, 5000);
  CHECK(P->dayMl() == 0 && g_pin[4] == 0, "power-on: nothing pumped, pin off");

  // rounding
  dose(*P, 5);
  CHECK(P->lastDoseMl() == 5 && P->dayMl() == 5, "5 ml at 95 ml/min (3157 ms) counts as 5, not 4");
  P->requestRunSeconds(6); P->loop();
  while (P->running()) { g_millis += 1; P->loop(); }
  CHECK(P->lastDoseMl() == 10 && P->dayMl() == 15, "a 6 s run at 95 ml/min (9.5 ml) counts as 10");

  // a dose in progress is part of the total
  P->requestDose(10); P->loop();
  g_millis += 3158; P->loop();
  CHECK(P->running() && P->dayMl() == 20, "half way through a 10 ml dose: 15 + 5");
  while (P->running()) { g_millis += 1; P->loop(); }
  CHECK(P->dayMl() == 25, "after it: 25");

  // rolling window: four 5 ml doses 6 h apart, on a fresh node
  delete P; P = fresh(node, 1000);
  uint32_t t0 = g_millis;
  for (int i = 0; i < 4; i++) { dose(*P, 5); if (i < 3) run(*P, 6 * H); }
  CHECK(P->dayMl() == 20, "four germination doses in 18 h: 20");
  run(*P, 6 * H - 20000);                                        // just short of 24 h after the first ended
  CHECK(P->dayMl() == 20, "23h59m after the first dose: still 20");
  run(*P, 30000);
  CHECK(P->dayMl() == 15, "24 h after the first dose: it drops out, 15 (old code: kept until a fixed reset)");
  run(*P, 6 * H);
  CHECK(P->dayMl() == 10, "30 h: the second drops out too, 10");
  CHECK(P->_nRuns == 2, "expired runs are pruned");

  // a soft restart (crash, watchdog, OTA reboot) keeps the runs and their ages
  uint32_t before = P->dayMl();
  run(*P, 2000);                                                  // RTC copy is refreshed every second
  g_millis = 800;                                                 // millis() starts again
  HivewirePump *P2 = new HivewirePump(node, 4);
  P2->begin();
  CHECK(P2->dayMl() == before, "soft restart: same 24 h total");
  run(*P2, 6 * H - 40000);
  CHECK(P2->dayMl() == 10, "after the restart, the third dose hasn't expired early");
  run(*P2, 60000);
  CHECK(P2->dayMl() == 5, "and expires on time: 24 h after it ran, across the restart");
  delete P;

  // day cap judged on the rolling window
  P = P2;
  P->requestSetting(HW_PUMP_SLOT_MAX_DAY, 20); P->loop();
  dose(*P, 5); run(*P, H); dose(*P, 5); run(*P, H); dose(*P, 5);
  CHECK(P->dayMl() == 20, "at the 20 ml cap");
  g_lastErr = 0;
  P->requestDose(5); P->loop();
  CHECK(!P->running() && P->state() == HW_PUMP_REFUSED_LIMIT && g_lastErr == 306 && g_lastErrSubject == HW_PUMP_SLOT_MAX_DAY,
        "a 5 ml dose over the day cap is refused (E306, subject slot 44)");
  CHECK(P->dayMl() == 20 && g_pin[4] == 0, "refused: nothing ran");
  run(*P, 4 * H);                                                 // the 5 ml from before the restart expires
  dose(*P, 5);
  CHECK(P->state() == HW_PUMP_DONE && P->dayMl() == 20, "once the oldest dose is 24 h old, there's room again");
  delete P;

  // power loss: RTC memory gone, the total starts again (the documented limit)
  P = fresh(node, 300);
  CHECK(P->dayMl() == 0, "power loss: total starts at 0");
  // an image with the old RTC layout ("PMP2") is not misread after an update
  HivewirePump::_rtc.magic = 0x504d5032;
  g_millis = 300;
  HivewirePump P3(node, 4); P3.begin();
  CHECK(P3.dayMl() == 0 && P3._nRuns == 0, "old RTC layout ignored");

  // more runs than the table holds: folded, total exact
  delete P; P = fresh(node, 1000);
  for (int i = 0; i < 40; i++) { dose(*P, 1); run(*P, 5 * M); }
  CHECK(P->_nRuns <= 32 && P->dayMl() == 40, "40 small doses: table capped at 32, total still 40");
  run(*P, 24 * H);
  CHECK(P->dayMl() == 0, "and all of it expires within a day");

  // millis() wraps every 49.7 days
  delete P; P = fresh(node, 0xFFFFFFFFUL - H);
  dose(*P, 5);
  run(*P, 2 * H);
  CHECK(g_millis < 2 * H && P->dayMl() == 5, "across the millis() wrap: still counted");
  run(*P, 22 * H - 10000);
  CHECK(P->dayMl() == 5, "just under 24 h: still there");
  run(*P, 20000);
  CHECK(P->dayMl() == 0, "24 h after: gone");
  delete P;

  // a stop part way counts what actually ran
  P = fresh(node, 1000);
  P->requestDose(20); P->loop();
  g_millis += 6316; P->loop();                                    // 10 ml worth
  P->requestDose(0); P->loop();
  CHECK(!P->running() && P->lastDoseMl() == 10 && P->dayMl() == 10 && g_pin[4] == 0, "stopped half way: 10 ml counted, pin off");

  printf("%d checks, %d failed\n", checks, fails);
  return fails ? 1 : 0;
}
