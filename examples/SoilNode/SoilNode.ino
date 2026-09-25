/*
 * Hivewire -- SoilNode
 *
 * A plant sensor on the swarm: an AHT20 (air temperature and humidity over
 * I2C), a capacitive soil moisture probe (analog) and an optional battery
 * divider, published as slots. The hive keeps the latest value of each, so
 * whatever sits on its USB port -- a Raspberry Pi, say -- can read the whole
 * garden with one `dump` command and no LoRa airtime at all.
 *
 * Wiring (C6 SuperMini; check the silkscreen on the BACK of the board):
 *
 *   AHT20      VCC -> 3V3, GND -> GND, SDA -> GPIO20, SCL -> GPIO19
 *              (22/23 is also tried -- see I2C_SDA_ALT below)
 *   Soil probe VCC -> GPIO18 (switched; see SOIL_PWR_PIN), GND -> GND,
 *              AOUT -> GPIO0
 *   Battery    LiPo+ -[1M]-+-[1M]- GND, midpoint -> GPIO1   (optional)
 *
 * Always on, unlike a Zigbee end device. A swarm node has to hear the hive's
 * beacons to stay converged and to receive firmware, so it cannot deep-sleep
 * between readings; power it from USB or a supply sized for ~30-80 mA.
 *
 * Updatable over the air exactly like RangeNode: the hive or a peer can push
 * an image, and a bad one reverts on its own (HivewireOta's safety net).
 *
 *   arduino-cli compile --build-property compiler.cpp.extra_flags=-DHW_NODE_ID=11
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>
#include <HivewireOta.h>
#include <HivewireFirmware.h>
#include <HivewireProvision.h>
#include <Preferences.h>
#include <Wire.h>

#ifndef HW_NODE_ID
#define HW_NODE_ID 11
#endif
// Seed only: the id lives in NVS after first boot, so one image can be pushed
// to every node and each keeps who it is (same rule as RangeNode).
//
// Build with -DHW_NODE_ID=0 for the GENERIC image the flasher uses: a board
// that boots with id 0 never joins the swarm; it reports itself over USB and
// waits for `setid <n>` (see HivewireProvision.h).
static uint8_t NODE_ID = HW_NODE_ID;

// ---- pins -------------------------------------------------------------------
#define I2C_SDA_PIN 20
#define I2C_SCL_PIN 19
// A built board was found with its AHT20 on 22/23 and nothing on 20/19: the
// SuperMini's pad labels are on the back, and neighbouring pads on the other
// row are an easy mix-up. Both pairs are probed, so either wiring just works.
#define I2C_SDA_ALT 22
#define I2C_SCL_ALT 23
#define SOIL_ADC_PIN 0
#define BATT_ADC_PIN 1
// Probe power, switched rather than wired to 3V3. A capacitive probe left
// energised corrodes -- its exposed traces electrolyse whatever they sit in --
// and drifts as it does, which looks exactly like the soil slowly drying. It
// is also the node's biggest continuous draw. So the probe is powered only for
// the moment it is read: VCC to this pin instead of the 3V3 pad.
//
// Harmless if the probe is still wired to 3V3: the pin drives nothing and
// readings are unaffected, so a board can be rewired whenever it is convenient.
// Set to -1 to leave the pin alone entirely.
#ifndef SOIL_PWR_PIN
#define SOIL_PWR_PIN 18
#endif
// A capacitive probe's oscillator needs a moment after power-up before its
// output means anything; measured settling is tens of ms, so this is generous.
#define SOIL_SETTLE_MS 120
// How far apart nine consecutive samples may be before the probe is considered
// disconnected. A wired probe on this bench moved ~10 counts; a bare pin moved
// over a thousand. Anything in between is noise worth knowing about.
#ifndef SOIL_MAX_SPREAD
#define SOIL_MAX_SPREAD 250
#endif

// ---- soil calibration -------------------------------------------------------
// Placeholders until measured on this probe: note the raw value (slot 3) in
// open air and fully submerged, then set these. Raw is published alongside the
// percentage precisely so history can be re-calibrated later.
#ifndef SOIL_ADC_DRY
#define SOIL_ADC_DRY 2800
#endif
#ifndef SOIL_ADC_WET
#define SOIL_ADC_WET 1200
#endif

static const uint8_t  AHT20_ADDR = 0x38;
static const uint32_t READ_EVERY_MS = 30000;

HivewireNode node;
HivewireOta  ota(node);
HivewireFwReceiver fw(node);
HivewireFlashProvider flashSrc;
// Only images of this same sketch are accepted -- see setFamily().
HW_FW_FAMILY("SoilNode");

// ---- readings, refreshed in loop() -- samplers only copy them ---------------
// Samplers run inside the library's slot scheduler; an 80 ms I2C conversion
// does not belong there. loop() reads, samplers report the last result.
static int16_t  tempCenti = 0;       // 0.01 C
static uint16_t humCenti  = 0;       // 0.01 %RH
static uint16_t soilRaw   = 0;       // ADC counts, 0-4095
static uint8_t  soilPct   = 0;
static uint16_t battMv    = 0;       // 0 = no divider fitted / not measured
static uint8_t  battPct   = 0;       // from the LiPo curve, not a linear scale
static uint8_t  sensorOk  = 0;       // bit0 AHT20, bit1 soil
static uint16_t ahtFails  = 0;
static uint16_t soilBadReads = 0;
static uint32_t bootCount = 0;
static uint32_t runningCrc = 0;
static uint8_t  lastAction = 0, otaArm = 0;
static bool     ahtFound = false;
static bool     battFitted = false;

// ---- AHT20: minimal driver, Wire only ---------------------------------------
static bool aht20Init() {
  Wire.beginTransmission(AHT20_ADDR);
  Wire.write(0xBA);                              // soft reset
  if (Wire.endTransmission() != 0) return false;
  delay(20);
  Wire.beginTransmission(AHT20_ADDR);
  Wire.write(0xBE); Wire.write(0x08); Wire.write(0x00);   // calibrate
  if (Wire.endTransmission() != 0) return false;
  delay(10);
  return true;
}

static bool aht20Read(float &tC, float &rh) {
  Wire.beginTransmission(AHT20_ADDR);
  Wire.write(0xAC); Wire.write(0x33); Wire.write(0x00);   // measure
  if (Wire.endTransmission() != 0) return false;
  delay(80);
  if (Wire.requestFrom((uint8_t)AHT20_ADDR, (uint8_t)7) != 7) return false;
  uint8_t b[7];
  for (int i = 0; i < 7; i++) b[i] = Wire.read();
  if (b[0] & 0x80) return false;                 // still busy
  uint32_t h = ((uint32_t)b[1] << 12) | ((uint32_t)b[2] << 4) | (b[3] >> 4);
  uint32_t t = (((uint32_t)b[3] & 0x0F) << 16) | ((uint32_t)b[4] << 8) | b[5];
  rh = h / 1048576.0f * 100.0f;
  tC = t / 1048576.0f * 200.0f - 50.0f;
  return true;
}

static bool ahtBegin() {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  if (aht20Init()) return true;
  Wire.end();
  Wire.begin(I2C_SDA_ALT, I2C_SCL_ALT);
  if (aht20Init()) { node.log("aht20 on alt pins %d/%d", I2C_SDA_ALT, I2C_SCL_ALT); return true; }
  node.log("aht20 not found");
  return false;
}

// --- sampling helpers --------------------------------------------------------
static int cmpU16(const void *a, const void *b) {
  uint16_t x = *(const uint16_t *)a, y = *(const uint16_t *)b;
  return x < y ? -1 : x > y ? 1 : 0;
}

// Middle of N reads. `powered` energises the probe for the measurement only.
// `spread` returns how far apart those reads were: that, not the value itself,
// is what tells a connected probe from a bare pin -- see readSensors().
static uint16_t medianAdc(uint8_t pin, bool powered, uint16_t *spread = nullptr) {
#if SOIL_PWR_PIN >= 0
  if (powered) { digitalWrite(SOIL_PWR_PIN, HIGH); delay(SOIL_SETTLE_MS); }
#else
  (void)powered;
#endif
  uint16_t s[9];
  for (int i = 0; i < 9; i++) { s[i] = analogRead(pin); delay(3); }
#if SOIL_PWR_PIN >= 0
  if (powered) digitalWrite(SOIL_PWR_PIN, LOW);
#endif
  qsort(s, 9, sizeof(s[0]), cmpU16);
  if (spread) *spread = s[8] - s[0];
  return s[4];
}

// Is anything actually connected to the battery pin?
//
// A bare ADC pin drifts to roughly mid-scale on its own, which on a 1M/1M
// divider's scale reads as a plausible 4.0-4.2 V LiPo that does not exist. No
// amount of averaging finds the truth, because nothing is driving the pin
// towards a value -- and a battery level that is pure noise is worse than none.
//
// The test: drive the pin low, release it, read; then drive it high, release,
// read. A divider pulls the pin back to the battery's fraction from either
// direction, so the two agree; a bare pin is still near where it was left.
//
// Read back MICROSECONDS after releasing the pin, not milliseconds. A 1M/1M
// divider recharges the ADC's input capacitance in tens of microseconds
// (500k x ~20pF), so it returns to the battery's half-voltage almost at once.
// A bare pin drifts to roughly mid-scale on its own, but slowly -- and the
// first version of this test waited 5 ms, by which time a bare pin had already
// drifted to the same place. Measured then: battery pin 2020/2019 mV, a pin
// with nothing on it 2095/2103 mV. Identical, so the test proved nothing.
static bool pinDriven(uint8_t pin, uint16_t *after_low, uint16_t *after_high) {
  uint16_t v[2];
  for (int i = 0; i < 2; i++) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, i ? HIGH : LOW);
    delayMicroseconds(2000);
    pinMode(pin, INPUT);
    delayMicroseconds(150);
    v[i] = analogReadMilliVolts(pin);
  }
  if (after_low) *after_low = v[0];
  if (after_high) *after_high = v[1];
  uint16_t lo = v[0] < v[1] ? v[0] : v[1], hi = v[0] < v[1] ? v[1] : v[0];
  return (hi - lo) < 200;          // recovered to the same value from both sides
}

// A pin with nothing on it, tested the same way at boot. It is the control for
// the check above: without it, "the battery pin looks driven" is a claim with
// nothing to compare against, and a detector that always says yes would look
// identical to one that works.
//
// It MUST be ADC-capable: on the ESP32-C6 that is GPIO0-GPIO6 only. The first
// version used GPIO7, which reads 0 mV whatever you do to it -- two identical
// readings, which the test scored as "driven", which then vetoed every battery
// reading. A control that cannot measure is worse than none: it silently
// disabled the thing it was meant to check.
#ifndef BATT_CONTROL_PIN
#define BATT_CONTROL_PIN 3
#endif

// Nine samples, not five. The LiPo curve is steep around 4.0 V -- a reading
// 80 mV apart is 8 percentage points there -- so ADC noise that looks small in
// millivolts makes the reported percentage jump.
static uint16_t medianMilliVolts(uint8_t pin) {
  uint16_t s[9];
  for (int i = 0; i < 9; i++) { s[i] = analogReadMilliVolts(pin); delay(3); }
  qsort(s, 9, sizeof(s[0]), cmpU16);
  return s[4];
}

// A LiPo's voltage is nothing like linear in its charge: it sits near 3.8 V
// for most of the discharge and then falls off a cliff. Reporting
// (V - 3.3) / (4.2 - 3.3) would read ~55% for most of the battery's life and
// then drop to nothing in an afternoon. This is the usual discharge curve,
// interpolated between measured points, so "20%" really is about a fifth left.
static uint8_t lipoPercent(uint16_t mv) {
  static const uint16_t V[] = {3300, 3450, 3680, 3740, 3770, 3790, 3820,
                               3870, 3950, 4000, 4100, 4200};
  static const uint8_t  P[] = {   0,    5,   10,   20,   30,   40,   50,
                                 60,   70,   80,   90,  100};
  const int n = sizeof(P) / sizeof(P[0]);
  if (mv <= V[0]) return 0;
  if (mv >= V[n - 1]) return 100;
  for (int i = 1; i < n; i++) {
    if (mv < V[i]) {
      uint16_t span = V[i] - V[i - 1];
      return P[i - 1] + (uint8_t)((uint32_t)(mv - V[i - 1]) * (P[i] - P[i - 1]) / span);
    }
  }
  return 100;
}

static const char *battStr(uint16_t mv, uint8_t pct) {
  static char s[24];
  snprintf(s, sizeof(s), "%umV (%u%%)", mv, pct);
  return s;
}

static void readSensors() {
  float tC, rh;
  // Retry the bus setup now and then: a sensor plugged in, or reseated, after
  // boot should start reporting without a trip to reset the board.
  if (!ahtFound) {
    static uint32_t lastProbe = 0;
    if (!lastProbe || millis() - lastProbe > 300000) { lastProbe = millis(); ahtFound = ahtBegin(); }
  }
  if (ahtFound && aht20Read(tC, rh)) {
    tempCenti = (int16_t)lroundf(tC * 100.0f);
    humCenti  = (uint16_t)lroundf(rh * 100.0f);
    sensorOk |= 1;
  } else {
    // Stale values would look like a live reading; the ok bit is what says
    // they are not.
    sensorOk &= ~1;
    if (ahtFound && ++ahtFails % 10 == 1) node.log("aht20 read failed (%u)", ahtFails);
  }

  // MEDIAN of several samples, not the mean. A capacitive probe's output is
  // noisy, and one electrical glitch -- the radio transmitting mid-read is
  // enough -- drags a mean far enough to trip the report threshold and land a
  // fictional reading in the history. A median ignores an outlier completely.
  uint16_t spread = 0;
  soilRaw = medianAdc(SOIL_ADC_PIN, true, &spread);
  // Judge the probe by how far its own samples DISAGREE, not by whether the
  // number looks sane. A disconnected pin gives readings that are individually
  // plausible and collectively nonsense: measured on a board with no probe,
  // 1592, 453 and 1468 within a few seconds, every one of them inside the
  // "sensible" range and averaging to a confident lie. A connected probe moves
  // by a handful of counts across the same nine samples.
  bool soilSteady = spread <= SOIL_MAX_SPREAD;
  bool soilPlausible = soilRaw > 200 && soilRaw < 4000 && soilSteady;
  sensorOk = soilPlausible ? (sensorOk | 2) : (sensorOk & ~2);
  if (!soilPlausible && ++soilBadReads % 20 == 1)
    node.log("soil: median %u spread %u -- %s", soilRaw, spread,
             soilSteady ? "out of range" : "probe not connected?");
  float pct = 100.0f * (float)(SOIL_ADC_DRY - (int)soilRaw) / (float)(SOIL_ADC_DRY - SOIL_ADC_WET);
  soilPct = pct < 0 ? 0 : pct > 100 ? 100 : (uint8_t)lroundf(pct);

  // 0 means "no divider on this board", not "flat" -- see batteryFitted().
  if (battFitted) {
    uint32_t mv = medianMilliVolts(BATT_ADC_PIN) * 2;     // 1M/1M divider halves it
    battMv = mv > 65535 ? 65535 : mv;
    battPct = lipoPercent(battMv);
    sensorOk |= 4;
  } else {
    battMv = battPct = 0;
    sensorOk &= ~4;
  }
}

// ---- samplers ---------------------------------------------------------------
static void sTemp(void *o)     { memcpy(o, &tempCenti, 2); }
static void sHum(void *o)      { memcpy(o, &humCenti, 2); }
static void sSoilRaw(void *o)  { memcpy(o, &soilRaw, 2); }
static void sSoilPct(void *o)  { memcpy(o, &soilPct, 1); }
static void sBatt(void *o)     { memcpy(o, &battMv, 2); }
static void sBattPct(void *o)  { memcpy(o, &battPct, 1); }
static void sOk(void *o)       { memcpy(o, &sensorOk, 1); }
static void sBoots(void *o)    { uint8_t v = bootCount > 255 ? 255 : bootCount; memcpy(o, &v, 1); }
static void sUptimeMin(void *o){ uint16_t v = millis() / 60000UL; memcpy(o, &v, 2); }
static void sRssi(void *o)     { int8_t v = node.lastRssi(); memcpy(o, &v, 1); }
static void sAction(void *o)   { memcpy(o, &lastAction, 1); }
static void sOtaArm(void *o)   { memcpy(o, &otaArm, 1); }
static void sFwCrc(void *o)    { memcpy(o, &runningCrc, 4); }

// ---- appliers ---------------------------------------------------------------
static void aAction(const void *in) {
  lastAction = *(const uint8_t *)in;
  if (lastAction == 4) { node.log("reboot by command"); delay(50); ESP.restart(); }
  if (lastAction == 5) ota.trigger();
}
static void aOtaArm(const void *in) { otaArm = *(const uint8_t *)in; ota.arm(otaArm); }

// Slot ids 22, 23 and 25 match RangeNode, so the same maintenance commands
// (reboot, arm, firmware CRC) work on every node type.
//  id  type    dir            sample  report  thresh min  max  sampler     applier
static const HwSlotDef SLOTS[] = {
  {  1, HW_I16, HW_DIR_OUT,     30000, 900000,    50,   0,   0, sTemp,      nullptr },  // 0.5 C
  {  2, HW_U16, HW_DIR_OUT,     30000, 900000,   200,   0,   0, sHum,       nullptr },  // 2 %RH
  {  3, HW_U16, HW_DIR_OUT,     30000, 900000,    60,   0,   0, sSoilRaw,   nullptr },
  {  4, HW_U8,  HW_DIR_OUT,     30000, 900000,     3,   0,   0, sSoilPct,   nullptr },
  {  5, HW_U16, HW_DIR_OUT,     60000, 900000,    50,   0,   0, sBatt,      nullptr },
  { 10, HW_U8,  HW_DIR_OUT,     60000, 900000,     5,   0,   0, sBattPct,   nullptr },
  {  6, HW_U8,  HW_DIR_OUT,     30000, 900000,     1,   0,   0, sOk,        nullptr },
  {  7, HW_U8,  HW_DIR_OUT,    300000, 900000,     1,   0,   0, sBoots,     nullptr },
  {  8, HW_U16, HW_DIR_OUT,     60000, 900000,    15,   0,   0, sUptimeMin, nullptr },
  {  9, HW_I8,  HW_DIR_OUT,     30000, 900000,     6,   0,   0, sRssi,      nullptr },
  { 22, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0,   5, sAction,    aAction },
  { 23, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0, 255, sOtaArm,    aOtaArm },
  { 25, HW_U32, HW_DIR_OUT,    600000, 900000,     1,   0,   0, sFwCrc,     nullptr },
};
static const uint8_t N_SLOTS = sizeof(SLOTS) / sizeof(SLOTS[0]);

void setup() {
  Serial.begin(115200);
  delay(200);
  analogReadResolution(12);
#if SOIL_PWR_PIN >= 0
  // Idle low: the probe is dark except while being read.
  pinMode(SOIL_PWR_PIN, OUTPUT);
  digitalWrite(SOIL_PWR_PIN, LOW);
#endif

  Preferences prefs;
  prefs.begin("soilnode", false);
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  if (!prefs.isKey("id")) prefs.putUChar("id", HW_NODE_ID);
  NODE_ID = prefs.getUChar("id", HW_NODE_ID);
  prefs.end();

  if (NODE_ID == 0) hwprov::waitForId("soilnode", "SoilNode");   // never returns
  hwprov::printId("SoilNode", NODE_ID);
  hwprov::radioSelfTest();          // before the swarm radio: a scan hops channels

  node.onRaw([](const uint8_t *d, int n) { fw.ingest(d, n); });
  if (!node.begin(NODE_ID, HW_ROLE_SENSOR, SLOTS, N_SLOTS)) {
    Serial.println("hivewire: begin failed");
    delay(1000);
    ESP.restart();                  // never sit dead where nobody can reach it
  }
  node.log("boot #%lu", (unsigned long)bootCount);
  ota.begin();                      // picks up an unconfirmed update, if any
  if (flashSrc.begin()) {
    runningCrc = flashSrc.crc();
    fw.setRunningImage(flashSrc.length(), runningCrc);
  }
  fw.setFamily(hwFwFamily);
  fw.onApplied([] { ota.markPending(); });

  ahtFound = ahtBegin();
  uint16_t bl = 0, bh = 0, cl = 0, ch = 0;
  bool battLooksDriven = pinDriven(BATT_ADC_PIN, &bl, &bh);
  bool ctrl = pinDriven(BATT_CONTROL_PIN, &cl, &ch);
  // The control pin has nothing on it. If IT looks driven, the test cannot
  // tell the difference on this board, so believe nothing: report no battery
  // rather than publish a number that may be a floating pin. Build with
  // -DSOIL_HAS_BATTERY=1 to say a divider is definitely fitted.
#ifdef SOIL_HAS_BATTERY
  battFitted = SOIL_HAS_BATTERY;
#else
  battFitted = battLooksDriven && !ctrl;
#endif
  // Both numbers, not just the verdict: a divider recovers to the same value
  // from either direction, a bare pin keeps what it was left at.
  node.log("batt pin%d %u/%umV %s; pin%d %u/%umV %s", BATT_ADC_PIN, bl, bh,
           battFitted ? "fitted" : "open", BATT_CONTROL_PIN, cl, ch,
           ctrl ? "driven" : "open");
  Serial.printf("battery check: pin%d %u/%u mV -> %s | control pin%d %u/%u mV -> %s\n",
                BATT_ADC_PIN, bl, bh, battFitted ? "fitted" : "open",
                BATT_CONTROL_PIN, cl, ch, ctrl ? "driven (unexpected)" : "open");
  readSensors();
  Serial.printf("SoilNode %u up, boot #%lu, aht20=%d\n", NODE_ID,
                (unsigned long)bootCount, ahtFound);
}

void loop() {
  node.loop();
  ota.loop();
  fw.loop();
  hwprov::poll("soilnode", "SoilNode", NODE_ID);

  // A hive that reboots restarts its epoch at 1, and adoption needs a HIGHER
  // one -- so without this a node would ignore a rebooted hive forever. After
  // a long orphan it holds nothing anyway, so accepting a lower epoch is free.
  static uint32_t orphanSince = 0;
  if (node.orphaned()) {
    if (!orphanSince) orphanSince = millis();
    if (millis() - orphanSince >= 600000) { node.forgetEpoch(); orphanSince = millis(); }
  } else {
    orphanSince = 0;
  }

  // No sensor I/O while an image is arriving: the transfer is what this node
  // must not stall, and the readings can wait a minute.
  static uint32_t lastRead = 0;
  if (!fw.active() && millis() - lastRead >= READ_EVERY_MS) {
    lastRead = millis();
    readSensors();
    Serial.printf("t=%.2fC rh=%.2f%% soil=%u (%u%%) batt=%s ok=%u nb=%u ep=%lu\n",
                  tempCenti / 100.0f, humCenti / 100.0f, soilRaw, soilPct,
                  battFitted ? battStr(battMv, battPct) : "none fitted",
                  sensorOk, node.neighbors(), (unsigned long)node.epoch());
  }
}
