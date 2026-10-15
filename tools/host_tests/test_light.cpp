#include "Arduino.h"
uint32_t g_millis = 0; int g_pin[64]; std::map<std::string, unsigned> g_nvs;
#include "HivewireLight.h"
static int fails = 0;
#define CHECK(c, msg) do { if (!(c)) { printf("FAIL: %s\n", msg); fails++; } else printf("ok: %s\n", msg); } while (0)
static void run(HivewireLight &L, uint32_t ms) { for (uint32_t t = 0; t < ms; t += 1000) { g_millis += 1000; L.loop(); } }
int main() {
  // window rule
  CHECK(hwLightWindow(360, 360, 1320) && !hwLightWindow(1320, 360, 1320) && !hwLightWindow(359, 360, 1320), "06:00-22:00 window edges");
  CHECK(hwLightWindow(1400, 1200, 300) && hwLightWindow(100, 1200, 300) && !hwLightWindow(600, 1200, 300), "window across midnight");
  CHECK(!hwLightWindow(500, 500, 500), "on == off is never on");
  HivewireNode node;
  HivewireLight::_rtc.magic = 0;                     // power-on: RTC memory not valid
  HivewireLight L(node, 7);
  g_millis = 5000; L.begin();
  CHECK(L.clock() == 65535 && g_pin[7] == 0 && L.mode() == 2, "fresh boot: clock unset, schedule mode, light off");
  run(L, 600000);
  CHECK(g_pin[7] == 0, "still off with no clock after 10 min");
  L.requestSetting(HW_LIGHT_SLOT_CLOCK, 21 * 60 + 59); L.loop();     // 21:59
  CHECK(L.clock() == 21 * 60 + 59 && g_pin[7] == 1 && L.isOn(), "clock 21:59 -> on");
  run(L, 61000);
  CHECK(L.clock() == 22 * 60 && g_pin[7] == 0, "22:00 -> off");
  run(L, 8 * 3600 * 1000UL);                            // to 06:00 next day
  CHECK(L.clock() == 6 * 60 && g_pin[7] == 1, "counts past midnight; 06:00 -> on");
  L.requestSetting(HW_LIGHT_SLOT_MODE, 0); L.loop();
  CHECK(g_pin[7] == 0 && g_nvs["hwlightmode"] == 0, "mode 0 -> off, saved");
  L.requestSetting(HW_LIGHT_SLOT_MODE, 1); L.loop();
  CHECK(g_pin[7] == 1, "mode 1 -> on");
  L.requestSetting(HW_LIGHT_SLOT_MODE, 9); L.loop();
  CHECK(L.mode() == 2, "out-of-range mode -> schedule");
  L.requestSetting(HW_LIGHT_SLOT_ON_AT, 2000); L.loop();
  CHECK(L.onAt() == 360, "on-at 2000 refused");
  L.requestSetting(HW_LIGHT_SLOT_ON_AT, 420); L.loop();
  CHECK(L.onAt() == 420 && g_pin[7] == 0, "on-at 07:00 at 06:00 -> off");
  // soft restart: RTC memory kept, millis restarts
  uint16_t before = L.clock();
  g_millis = 0;
  HivewireLight L2(node, 7);
  g_millis = 900; L2.begin();
  CHECK(L2.clock() == before && L2.onAt() == 420 && L2.mode() == 2, "soft restart keeps clock and settings");
  // power loss: RTC invalid
  HivewireLight::_rtc.magic = 0;
  HivewireLight L3(node, 7); g_millis = 100; L3.begin();
  CHECK(L3.clock() == 65535 && g_pin[7] == 0, "power loss: clock unset, light off until the host sends it");
  // active-low relay board
  HivewireLight L4(node, 9, true); L4.begin();
  CHECK(g_pin[9] == 1, "active-low: off drives the pin high");
  L4.requestSetting(HW_LIGHT_SLOT_MODE, 1); L4.loop();
  CHECK(g_pin[9] == 0, "active-low: on drives it low");
  // millis wrap (49.7 days)
  HivewireLight::_rtc.magic = 0;
  HivewireLight L5(node, 11); g_millis = 0xFFFFFFFF - 30000; L5.begin();
  L5.requestSetting(HW_LIGHT_SLOT_MODE, 2); L5.requestSetting(HW_LIGHT_SLOT_CLOCK, 600); L5.loop();
  run(L5, 120000);
  CHECK(L5.clock() == 602 && g_pin[11] == 1, "millis wrap: clock keeps counting");
  printf("\n%s\n", fails ? "FAILED" : "all passed");
  return fails != 0;
}
