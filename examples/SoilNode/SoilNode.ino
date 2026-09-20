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
 *   Soil probe VCC -> 3V3, GND -> GND, AOUT -> GPIO0
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
#include <Preferences.h>
#include <Wire.h>

#ifndef HW_NODE_ID
#define HW_NODE_ID 11
#endif
// Seed only: the id lives in NVS after first boot, so one image can be pushed
// to every node and each keeps who it is (same rule as RangeNode).
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
static uint8_t  sensorOk  = 0;       // bit0 AHT20, bit1 soil
static uint16_t ahtFails  = 0;
static uint32_t bootCount = 0;
static uint32_t runningCrc = 0;
static uint8_t  lastAction = 0, otaArm = 0;
static bool     ahtFound = false;

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

  // Average a few samples: a capacitive probe's output is noisy enough that a
  // single read would trip the report threshold on its own.
  uint32_t acc = 0;
  for (int i = 0; i < 8; i++) { acc += analogRead(SOIL_ADC_PIN); delay(2); }
  soilRaw = acc / 8;
  // A floating pin reads near 0 or near full scale; a real probe sits between.
  bool soilPlausible = soilRaw > 200 && soilRaw < 4000;
  sensorOk = soilPlausible ? (sensorOk | 2) : (sensorOk & ~2);
  float pct = 100.0f * (float)(SOIL_ADC_DRY - (int)soilRaw) / (float)(SOIL_ADC_DRY - SOIL_ADC_WET);
  soilPct = pct < 0 ? 0 : pct > 100 ? 100 : (uint8_t)lroundf(pct);

  uint32_t mv = analogReadMilliVolts(BATT_ADC_PIN) * 2;   // 1M/1M divider halves it
  battMv = mv > 65535 ? 65535 : mv;
}

// ---- samplers ---------------------------------------------------------------
static void sTemp(void *o)     { memcpy(o, &tempCenti, 2); }
static void sHum(void *o)      { memcpy(o, &humCenti, 2); }
static void sSoilRaw(void *o)  { memcpy(o, &soilRaw, 2); }
static void sSoilPct(void *o)  { memcpy(o, &soilPct, 1); }
static void sBatt(void *o)     { memcpy(o, &battMv, 2); }
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

  Preferences prefs;
  prefs.begin("soilnode", false);
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  if (!prefs.isKey("id")) prefs.putUChar("id", HW_NODE_ID);
  NODE_ID = prefs.getUChar("id", HW_NODE_ID);
  prefs.end();

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
  readSensors();
  Serial.printf("SoilNode %u up, boot #%lu, aht20=%d\n", NODE_ID,
                (unsigned long)bootCount, ahtFound);
}

void loop() {
  node.loop();
  ota.loop();
  fw.loop();

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
    Serial.printf("t=%.2fC rh=%.2f%% soil=%u (%u%%) batt=%umV ok=%u nb=%u ep=%lu\n",
                  tempCenti / 100.0f, humCenti / 100.0f, soilRaw, soilPct, battMv,
                  sensorOk, node.neighbors(), (unsigned long)node.epoch());
  }
}
