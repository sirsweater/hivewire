/*
 * Hivewire -- WeatherNode
 *
 * A garden weather station on the swarm: the SparkFun Weather Meter Kit
 * (wind speed, gusts, direction, rain) plus an air temperature / humidity
 * sensor, published as slots like every other node. The point is MEASURED
 * weather for this garden -- rain that actually fell here, the wind that
 * actually blew -- where everything else in the system has had to make do
 * with forecasts and model estimates.
 *
 * Wiring (C6 SuperMini; pad labels are on the BACK of the board):
 *
 *   Anemometer  RJ11 inner pair:  one wire -> GPIO2, the other -> GND
 *   Rain gauge  RJ11 middle pair: one wire -> GPIO3, the other -> GND
 *               (both are reed switches; 10 k from the GPIO to 3V3 is
 *               recommended on long cables, the internal pull-up is on anyway)
 *   Wind vane   RJ11 outer pair:  one wire -> GPIO0, the other -> GND,
 *               and a 10 k resistor from GPIO0 to 3V3 (REQUIRED: it and the
 *               vane's own resistors form the divider that encodes direction)
 *   Air sensor  VCC -> 3V3, GND -> GND, SDA -> GPIO20, SCL -> GPIO19
 *               (22/23 is also tried, as on SoilNode). Any of: SHT4x (SHT40/
 *               41/45), SHT3x (SHT30/31/35), AHT20 -- found automatically.
 *               A bare probe with no pull-ups needs 2.2-4.7 k from SDA and SCL
 *               to 3V3; the bus runs slowly (I2C_HZ) so a 2 m lead is fine.
 *
 * Confirm the RJ11 pairs with a multimeter before wiring: spin the cups and
 * the anemometer pair clicks open/closed; tip the bucket and the rain pair
 * does; the vane pair reads a resistance that changes as the vane turns.
 *
 * Always on (a swarm node must hear the hive), so power it from USB.
 *
 *   arduino-cli compile --build-property compiler.cpp.extra_flags=-DHW_NODE_ID=20
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>
#include <HivewireOta.h>
#include <HivewireFirmware.h>
#include <HivewireProvision.h>
#include <HivewireErrors.h>
#include <Preferences.h>
#include <Wire.h>
#include "WeatherMath.h"

#define NODE_FAMILY "WeatherNode"

#ifndef HW_NODE_ID
#define HW_NODE_ID 20
#endif
// Seed only: the id lives in NVS after first boot (see SoilNode). Build with
// -DHW_NODE_ID=0 for the generic image the flasher uses.
static uint8_t NODE_ID = HW_NODE_ID;

// ---- pins -------------------------------------------------------------------
#define ANEMO_PIN 2
#define RAIN_PIN  3
#define VANE_PIN  0          // must be ADC-capable: GPIO0-6 on the C6
#define I2C_SDA_PIN 20
#define I2C_SCL_PIN 19
#define I2C_SDA_ALT 22
#define I2C_SCL_ALT 23
#ifndef I2C_HZ
#define I2C_HZ 20000         // slow on purpose: a 2 m sensor lead, read once a minute
#endif
#ifndef VANE_PULLUP_OHMS
#define VANE_PULLUP_OHMS 10000.0f
#endif

// Shortest believable gap between closures. The anemometer at 240 km/h closes
// 100 times a second (10 ms apart), so 4 ms rejects contact bounce without
// losing real pulses; a bucket cannot tip twice in 80 ms.
static const uint32_t ANEMO_DEBOUNCE_US = 4000;
static const uint32_t RAIN_DEBOUNCE_US  = 80000;
static const uint32_t AIR_EVERY_MS = 30000;

HivewireNode node;
HivewireOta  ota(node);
HivewireFwReceiver fw(node);
HivewireFlashProvider flashSrc;
HivewireFwNodeSender fwTx(node);
HW_FW_FAMILY(NODE_FAMILY);

// ---- pulse counting (interrupts) ---------------------------------------------
static volatile uint32_t anemoCount = 0, rainCount = 0;
static volatile uint32_t anemoLastUs = 0, rainLastUs = 0;

static void IRAM_ATTR onAnemo() {
  uint32_t t = micros();
  if (t - anemoLastUs >= ANEMO_DEBOUNCE_US) { anemoCount++; anemoLastUs = t; }
}
static void IRAM_ATTR onRain() {
  uint32_t t = micros();
  if (t - rainLastUs >= RAIN_DEBOUNCE_US) { rainCount++; rainLastUs = t; }
}

// Rain total survives a reboot or an update (RTC memory), not a power cut. The
// hive works from the CHANGE in this counter, so a reset to 0 costs at most the
// tips since the last report, never a wrong total.
struct RainKeep { uint32_t magic; uint32_t tips; };
RTC_NOINIT_ATTR static RainKeep rainKeep;
static const uint32_t RAIN_MAGIC = 0x52414E31;   // "RAN1"

// ---- air sensor: SHT4x / SHT3x / AHT20 -----------------------------------------
enum AirKind : uint8_t { AIR_NONE = 0, AIR_SHT4X = 1, AIR_SHT3X = 2, AIR_AHT20 = 3 };
static AirKind airKind = AIR_NONE;
static uint8_t airAddr = 0;
static const char *AIR_NAMES[] = {"none", "sht4x", "sht3x", "aht20"};

// Sensirion and Aosong use the same CRC-8 (poly 0x31, init 0xFF).
static uint8_t crc8(const uint8_t *p, int n) {
  uint8_t c = 0xFF;
  while (n--) {
    c ^= *p++;
    for (int k = 0; k < 8; k++) c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x31) : (uint8_t)(c << 1);
  }
  return c;
}

static bool i2cCmd(uint8_t addr, const uint8_t *cmd, int n) {
  Wire.beginTransmission(addr);
  for (int i = 0; i < n; i++) Wire.write(cmd[i]);
  return Wire.endTransmission() == 0;
}
static bool i2cRead(uint8_t addr, uint8_t *buf, int n) {
  if (Wire.requestFrom(addr, (uint8_t)n) != n) return false;
  for (int i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}
// Two 16-bit words, each followed by its CRC: the shape of every Sensirion reply.
static bool sensirionWords(const uint8_t *b, uint16_t &w0, uint16_t &w1) {
  if (crc8(b, 2) != b[2] || crc8(b + 3, 2) != b[5]) return false;
  w0 = ((uint16_t)b[0] << 8) | b[1];
  w1 = ((uint16_t)b[3] << 8) | b[4];
  return true;
}

// Who is on the bus? Each candidate is asked for its serial number in its own
// dialect and must answer with valid CRCs -- an SHT3x and an SHT4x can share
// address 0x44, and only the right command gets a well-formed reply.
static bool probeSht4x(uint8_t addr) {
  const uint8_t c[] = {0x89};
  uint8_t b[6]; uint16_t a, z;
  if (!i2cCmd(addr, c, 1)) return false;
  delay(2);
  return i2cRead(addr, b, 6) && sensirionWords(b, a, z);
}
static bool probeSht3x(uint8_t addr) {
  const uint8_t c[] = {0x37, 0x80};
  uint8_t b[6]; uint16_t a, z;
  if (!i2cCmd(addr, c, 2)) return false;
  delay(2);
  return i2cRead(addr, b, 6) && sensirionWords(b, a, z);
}
static bool probeAht20() {
  const uint8_t reset[] = {0xBA}, cal[] = {0xBE, 0x08, 0x00};
  if (!i2cCmd(0x38, reset, 1)) return false;
  delay(20);
  if (!i2cCmd(0x38, cal, 3)) return false;
  delay(10);
  return true;
}

static uint8_t airProbe() {   // an AirKind (uint8_t: the IDE prototypes functions above the enum)
  if (probeSht4x(0x44)) { airAddr = 0x44; return AIR_SHT4X; }
  if (probeSht4x(0x45)) { airAddr = 0x45; return AIR_SHT4X; }
  if (probeSht3x(0x44)) { airAddr = 0x44; return AIR_SHT3X; }
  if (probeSht3x(0x45)) { airAddr = 0x45; return AIR_SHT3X; }
  if (probeAht20())     { airAddr = 0x38; return AIR_AHT20; }
  return AIR_NONE;
}

static bool airBegin() {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(I2C_HZ);
  airKind = (AirKind)airProbe();
  if (airKind == AIR_NONE) {
    Wire.end();
    Wire.begin(I2C_SDA_ALT, I2C_SCL_ALT);
    Wire.setClock(I2C_HZ);
    airKind = (AirKind)airProbe();
    if (airKind != AIR_NONE) node.log("air sensor on alt pins %d/%d", I2C_SDA_ALT, I2C_SCL_ALT);
  }
  if (airKind == AIR_NONE) {
    hwErr(node, HW_E_PERIPHERAL_MISSING, 1, "no air sensor (sht4x/sht3x/aht20)");
    return false;
  }
  node.log("air sensor %s at 0x%02x, i2c %u Hz", AIR_NAMES[airKind], airAddr, (unsigned)I2C_HZ);
  return true;
}

static bool airRead(float &tC, float &rh) {
  uint8_t b[7];
  uint16_t t, h;
  switch (airKind) {
    case AIR_SHT4X: {
      const uint8_t c[] = {0xFD};                    // high precision
      if (!i2cCmd(airAddr, c, 1)) return false;
      delay(10);
      if (!i2cRead(airAddr, b, 6) || !sensirionWords(b, t, h)) return false;
      tC = -45.0f + 175.0f * t / 65535.0f;
      rh = -6.0f + 125.0f * h / 65535.0f;           // datasheet: clamp 0-100
      if (rh < 0) rh = 0;
      if (rh > 100) rh = 100;
      break;
    }
    case AIR_SHT3X: {
      const uint8_t c[] = {0x24, 0x00};              // single shot, high repeatability
      if (!i2cCmd(airAddr, c, 2)) return false;
      delay(16);
      if (!i2cRead(airAddr, b, 6) || !sensirionWords(b, t, h)) return false;
      tC = -45.0f + 175.0f * t / 65535.0f;
      rh = 100.0f * h / 65535.0f;
      break;
    }
    case AIR_AHT20: {
      const uint8_t c[] = {0xAC, 0x33, 0x00};
      if (!i2cCmd(airAddr, c, 3)) return false;
      delay(80);
      if (!i2cRead(airAddr, b, 7)) return false;
      if (b[0] & 0x80) return false;                  // busy
      if (crc8(b, 6) != b[6]) return false;           // garbled (the -50 C bug)
      if (!(b[0] & 0x08)) return false;               // not calibrated
      uint32_t hr = ((uint32_t)b[1] << 12) | ((uint32_t)b[2] << 4) | (b[3] >> 4);
      uint32_t tr = (((uint32_t)b[3] & 0x0F) << 16) | ((uint32_t)b[4] << 8) | b[5];
      rh = hr / 1048576.0f * 100.0f;
      tC = tr / 1048576.0f * 200.0f - 50.0f;
      if (rh <= 0.0f) return false;                   // all-zero reply
      break;
    }
    default:
      return false;
  }
  return tC >= -40.0f && tC <= 85.0f && rh >= 0.0f && rh <= 100.0f;
}

// SHT4x only: a built-in heater. Outdoors humidity sits near 100% for whole
// nights; condensation on the sensor makes it read high for hours after. A
// short heater pulse now and then, when it has been saturated a while, dries
// it -- and the reading straight after is skipped (it is the heater, not the air).
static uint32_t humidSince = 0, lastHeat = 0;
static bool skipNextAir = false;
static void maybeHeat(float rh) {
  if (airKind != AIR_SHT4X) return;
  if (rh < 95.0f) { humidSince = 0; return; }
  if (!humidSince) humidSince = millis();
  if (millis() - humidSince < 30UL * 60 * 1000) return;
  if (lastHeat && millis() - lastHeat < 60UL * 60 * 1000) return;
  const uint8_t c[] = {0x39};                         // 200 mW, 1 s
  if (i2cCmd(airAddr, c, 1)) {
    delay(1100);
    uint8_t b[6];
    i2cRead(airAddr, b, 6);                           // discard: heated reading
    lastHeat = millis();
    skipNextAir = true;
    node.log("sht4x heater pulse after %lu min near saturation",
             (unsigned long)((millis() - humidSince) / 60000UL));
  }
}

// ---- state published by the samplers ---------------------------------------------
static int16_t  tempCenti = 0;
static uint16_t humCenti = 0;
static uint16_t windAvg10 = 0, windGust10 = 0, windDir = 0xFFFF;
static uint32_t rainTips = 0;
static uint16_t rainHour100 = 0;
static uint16_t vaneMv = 0;
static uint8_t  sensorOk = 0;        // bit0 air sensor, bit1 wind vane
static uint32_t bootCount = 0, runningCrc = 0;
static uint8_t  lastAction = 0, otaArm = 0;
static uint16_t airFails = 0, vaneBad = 0;

static wx::Wind wind;
static wx::Rain rain;

static void readAir() {
  if (airKind == AIR_NONE) {
    static uint32_t lastProbe = 0;          // plugged in after boot: pick it up
    if (lastProbe && millis() - lastProbe < 300000) { sensorOk &= ~1; return; }
    lastProbe = millis();
    if (!airBegin()) { sensorOk &= ~1; return; }
  }
  float tC, rh;
  if (airRead(tC, rh)) {
    if (skipNextAir) { skipNextAir = false; return; }
    tempCenti = (int16_t)lroundf(tC * 100.0f);
    humCenti = (uint16_t)lroundf(rh * 100.0f);
    sensorOk |= 1;
    maybeHeat(rh);
  } else {
    sensorOk &= ~1;
    if (++airFails % 10 == 1) hwErr(node, HW_E_PERIPHERAL_READ_FAILED, 1, "%s read failed (%u)",
                                    AIR_NAMES[airKind], airFails);
  }
}

// Median of 5: the radio transmitting mid-read can kick one ADC sample.
static uint16_t vaneReadMv() {
  uint16_t s[5];
  for (int i = 0; i < 5; i++) { s[i] = analogReadMilliVolts(VANE_PIN); delayMicroseconds(300); }
  for (int i = 1; i < 5; i++) for (int j = i; j > 0 && s[j] < s[j - 1]; j--) { uint16_t x = s[j]; s[j] = s[j - 1]; s[j - 1] = x; }
  return s[2];
}

// Once a second: bank the anemometer's closures and the vane's position.
static void tickSecond() {
  static uint32_t lastAnemo = 0;
  noInterrupts();
  uint32_t a = anemoCount;
  interrupts();
  uint32_t closures = a - lastAnemo;
  lastAnemo = a;

  vaneMv = vaneReadMv();
  float dist = 0;
  uint8_t sec = wx::vaneSector(vaneMv, &dist, VANE_PULLUP_OHMS);
  if (sec <= 15) {
    sensorOk |= 2;
    vaneBad = 0;
  } else {
    sensorOk &= ~2;
    // Once a minute at most, and only after it has stayed wrong for a while: a
    // vane passing between two positions reads in-between for a moment.
    if (++vaneBad == 30)
      hwErr(node, dist < 0 ? HW_E_INPUT_FLOATING : HW_E_VALUE_OUT_OF_RANGE, 13,
            "vane %u mV matches no position", vaneMv);
    if (vaneBad > 90) vaneBad = 29;
  }
  // 100 closures a second is 240 km/h. More than that is electrical noise on
  // the cable, not wind -- count it as nothing rather than as a hurricane.
  if (closures > 120) {
    static uint32_t lastNoise = 0;
    if (!lastNoise || millis() - lastNoise > 600000) {
      lastNoise = millis();
      hwErr(node, HW_E_VALUE_OUT_OF_RANGE, 11, "anemometer %lu closures/s: noise", (unsigned long)closures);
    }
    closures = 0;
  }
  wind.push((uint16_t)closures, sec);
  windAvg10 = wind.avg10();
  windGust10 = wind.gust10();
  windDir = wind.dirDeg();

  noInterrupts();
  uint32_t r = rainCount;
  interrupts();
  rainKeep.tips = rainTips = r;
  static uint32_t minuteStartTips = r, minuteStart = millis();
  if (millis() - minuteStart >= 60000) {
    rain.rollMinute((uint16_t)(r - minuteStartTips));
    minuteStartTips = r;
    minuteStart += 60000;
  }
  rainHour100 = rain.lastHour100((uint16_t)(r - minuteStartTips));
}

// ---- samplers / appliers ---------------------------------------------------------
static void sTemp(void *o)     { memcpy(o, &tempCenti, 2); }
static void sHum(void *o)      { memcpy(o, &humCenti, 2); }
static void sOk(void *o)       { memcpy(o, &sensorOk, 1); }
static void sBoots(void *o)    { uint8_t v = bootCount > 255 ? 255 : bootCount; memcpy(o, &v, 1); }
static void sUptimeMin(void *o){ uint16_t v = millis() / 60000UL; memcpy(o, &v, 2); }
static void sRssi(void *o)     { int8_t v = node.bestRssi(); memcpy(o, &v, 1); }
static void sWind(void *o)     { memcpy(o, &windAvg10, 2); }
static void sGust(void *o)     { memcpy(o, &windGust10, 2); }
static void sDir(void *o)      { memcpy(o, &windDir, 2); }
static void sRainTips(void *o) { memcpy(o, &rainTips, 4); }
static void sRainHour(void *o) { memcpy(o, &rainHour100, 2); }
static void sVaneMv(void *o)   { memcpy(o, &vaneMv, 2); }
static void sAirKind(void *o)  { uint8_t v = airKind; memcpy(o, &v, 1); }
static void sAction(void *o)   { memcpy(o, &lastAction, 1); }
static void sOtaArm(void *o)   { memcpy(o, &otaArm, 1); }
static void sFwCrc(void *o)    { memcpy(o, &runningCrc, 4); }
static uint8_t seedTarget = 0;
static volatile int16_t seedWanted = -1;
static void sSeed(void *o)     { memcpy(o, &seedTarget, 1); }
static void aSeed(const void *in) {
  seedTarget = *(const uint8_t *)in;
  if (seedTarget) seedWanted = seedTarget;
}
static void aAction(const void *in) {
  lastAction = *(const uint8_t *)in;
  if (lastAction == 4) { node.log("reboot by command"); delay(50); ESP.restart(); }
  if (lastAction == 5) ota.trigger();
}
static void aOtaArm(const void *in) { otaArm = *(const uint8_t *)in; ota.arm(otaArm); }

// Slots 1, 2 and 6-9 mean the same as on SoilNode (so air temperature and
// humidity upload to g4rden unchanged); 22-27 are the maintenance set every
// node shares. 11-18 are this node's own.
//  id  type    dir            sample  report  thresh min  max  sampler     applier
static const HwSlotDef SLOTS[] = {
  {  1, HW_I16, HW_DIR_OUT,     30000, 900000,    20,   0,   0, sTemp,      nullptr },  // 0.2 C
  {  2, HW_U16, HW_DIR_OUT,     30000, 900000,   100,   0,   0, sHum,       nullptr },  // 1 %RH
  {  6, HW_U8,  HW_DIR_OUT,     10000, 900000,     1,   0,   0, sOk,        nullptr },
  {  7, HW_U8,  HW_DIR_OUT,    300000, 900000,     1,   0,   0, sBoots,     nullptr },
  {  8, HW_U16, HW_DIR_OUT,     60000, 900000,    15,   0,   0, sUptimeMin, nullptr },
  {  9, HW_I8,  HW_DIR_OUT,     30000, 900000,     6,   0,   0, sRssi,      nullptr },
  { 11, HW_U16, HW_DIR_OUT,     30000, 900000,    20,   0,   0, sWind,      nullptr },  // 2 km/h
  { 12, HW_U16, HW_DIR_OUT,     30000, 900000,    30,   0,   0, sGust,      nullptr },  // 3 km/h
  { 13, HW_U16, HW_DIR_OUT,     30000, 900000,    23,   0,   0, sDir,       nullptr },  // ~one position
  { 14, HW_U32, HW_DIR_OUT,     10000, 900000,     1,   0,   0, sRainTips,  nullptr },  // every tip
  { 15, HW_U16, HW_DIR_OUT,     30000, 900000,    28,   0,   0, sRainHour,  nullptr },  // one tip
  { 17, HW_U16, HW_DIR_OUT,     60000, 900000,    40,   0,   0, sVaneMv,    nullptr },
  { 18, HW_U8,  HW_DIR_OUT,    300000, 900000,     1,   0,   0, sAirKind,   nullptr },
  { 22, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0,   5, sAction,    aAction },
  { 23, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0, 255, sOtaArm,    aOtaArm },
  { 24, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0, 255, sSeed,      aSeed   },
  { 25, HW_U32, HW_DIR_OUT,    600000, 900000,     1,   0,   0, sFwCrc,     nullptr },
  { HW_ERR_SLOT_LAST,  HW_U32, HW_DIR_OUT, 30000, 900000, 1, 0, 0, hwErrSlotLast,  nullptr },
  { HW_ERR_SLOT_COUNT, HW_U16, HW_DIR_OUT, 30000, 900000, 1, 0, 0, hwErrSlotCount, nullptr },
};
static const uint8_t N_SLOTS = sizeof(SLOTS) / sizeof(SLOTS[0]);

void setup() {
  Serial.begin(115200);
  delay(200);
  analogReadResolution(12);

  Preferences prefs;
  prefs.begin("weathernode", false);
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  if (!prefs.isKey("id")) prefs.putUChar("id", HW_NODE_ID);
  NODE_ID = prefs.getUChar("id", HW_NODE_ID);
  prefs.end();

  if (NODE_ID == 0) HivewireOta::revertIfProvisional("no node id stored");
  if (NODE_ID == 0) hwprov::waitForId("weathernode", NODE_FAMILY);   // never returns
  hwprov::printId(NODE_FAMILY, NODE_ID);
  if (hwprov::radioSelfTest() == 0) hwErr(node, HW_E_RADIO_DEAF, 0, "radio heard no networks");

  // Rain total carried over a reboot or update; a cold start begins at 0.
  if (rainKeep.magic != RAIN_MAGIC) { rainKeep.magic = RAIN_MAGIC; rainKeep.tips = 0; }
  rainCount = rainTips = rainKeep.tips;

  pinMode(ANEMO_PIN, INPUT_PULLUP);
  pinMode(RAIN_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(ANEMO_PIN), onAnemo, FALLING);
  attachInterrupt(digitalPinToInterrupt(RAIN_PIN), onRain, FALLING);

  node.onRaw([](const uint8_t *d, int n) {
    if (!fwTx.active()) fw.ingest(d, n);
    fwTx.ingest(d, n);
    if (!fwTx.active() && n >= (int)sizeof(HwHeader) &&
        ((const HwHeader *)d)->type == HW_MSG_FW_NACK) node.relayRaw(d, n);
  });
  if (!node.begin(NODE_ID, HW_ROLE_SENSOR, SLOTS, N_SLOTS)) {
    Serial.println("E102 hivewire: begin failed");
    delay(1000);
    ESP.restart();
  }
  node.log("boot #%lu, rain carried over %lu tips", (unsigned long)bootCount, (unsigned long)rainTips);
  ota.begin();
  if (flashSrc.begin()) {
    runningCrc = flashSrc.crc();
    fw.setRunningImage(flashSrc.length(), runningCrc);
  }
  fw.setFamily(hwFwFamily);
  fw.onApplied([] { ota.markPending(); });

  airBegin();
  readAir();
  Serial.printf("WeatherNode %u up, boot #%lu, air=%s, vane=%u mV\n", NODE_ID,
                (unsigned long)bootCount, AIR_NAMES[airKind], vaneReadMv());
}

void loop() {
  node.loop();
  ota.loop();
  fw.loop();
  fwTx.loop();
  hwprov::poll("weathernode", NODE_FAMILY, NODE_ID);
  hwErrCheckLink(node);

  if (seedWanted >= 0) {
    uint8_t v = (uint8_t)seedWanted;
    seedWanted = -1;
    uint8_t target = (v == 255) ? HIVEWIRE_TARGET_ALL : v;
    if (fw.active() || fwTx.active())       node.log("fw: seed refused, busy");
    else if (v == NODE_ID)                  node.log("fw: seed refused, self");
    else if (ota.updating())                node.log("fw: seed refused, image unconfirmed");
    else if (!flashSrc.begin())             node.log("fw: seed refused, image unverified");
    else if (fwTx.begin(target, flashSrc.length(), flashSrc.crc(), HivewireFlashProvider::feed))
      node.log("fw: seed %lu b crc %08lx to %u", (unsigned long)flashSrc.length(),
               (unsigned long)flashSrc.crc(), v);
    else                                    node.log("fw: seed refused, sender");
  }

  static uint32_t orphanSince = 0;
  if (node.orphaned()) {
    if (!orphanSince) orphanSince = millis();
    if (millis() - orphanSince >= 600000) { node.forgetEpoch(); orphanSince = millis(); }
  } else {
    orphanSince = 0;
  }

  // Wind and rain keep counting through a firmware transfer (interrupts); only
  // the slower I2C read waits until it is over.
  static uint32_t lastTick = millis();
  if (millis() - lastTick >= 1000) {
    lastTick += 1000;
    if (millis() - lastTick > 5000) lastTick = millis();   // fell behind: don't replay seconds
    tickSecond();
  }
  static uint32_t lastAir = 0;
  if (!fw.active() && !fwTx.active() && millis() - lastAir >= AIR_EVERY_MS) {
    lastAir = millis();
    readAir();
    Serial.printf("t=%.2fC rh=%.1f%% wind=%.1f gust=%.1f km/h dir=%s vane=%umV rain=%lu tips (%.2f mm/h) ok=%u\n",
                  tempCenti / 100.0f, humCenti / 100.0f, windAvg10 / 10.0f, windGust10 / 10.0f,
                  windDir == 0xFFFF ? "calm" : String(windDir).c_str(), vaneMv,
                  (unsigned long)rainTips, rainHour100 / 100.0f, sensorOk);
  }
}
