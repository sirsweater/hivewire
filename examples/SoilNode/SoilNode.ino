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
 *   DS18B20    soil temperature probe (optional): VCC -> 3V3, GND -> GND,
 *              DATA -> GPIO6, with 4.7k from DATA to 3V3 (most adapter
 *              boards carry it). Not fitted: slot 16 reports "no reading".
 *              Replacing the AHT20 instead? Reuse its wires: DATA on its
 *              SDA wire, GPIO20 or GPIO22 (19/23 unused) -- found there when
 *              no AHT20 answers.
 *
 * Always on, unlike a Zigbee end device. A swarm node has to hear the hive's
 * beacons to stay converged and to receive firmware, so it cannot deep-sleep
 * between readings; power it from USB or a supply sized for ~30-80 mA.
 *
 * Updatable over the air exactly like RangeNode: the hive or a peer can push
 * an image, and a bad one reverts on its own (HivewireOta's safety net).
 *
 * WATERNODE: the same sketch built with -DHW_WITH_PUMP=1 is a different
 * firmware family, "WaterNode": everything above plus a pump or valve on the
 * same board (see HivewirePump.h for the slots and every safety rule):
 *
 *   Pump switch  logic-level MOSFET module input (e.g. isolated LR7843) -> GPIO4
 *                (the module's own supply and the pump are on the 12 V side)
 *   Float switch optional, GPIO5 <-> GND (set its mode in slot 48)
 *
 * After each dose the soil probe must show the water arrived: no rise within
 * 15 minutes raises application code 1001 (a dry reservoir the float switch
 * missed, a tube off the stake, a dead pump).
 *
 *   arduino-cli compile --build-property compiler.cpp.extra_flags=-DHW_NODE_ID=11
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
#include <driver/gpio.h>

#ifndef HW_WITH_PUMP
#define HW_WITH_PUMP 0
#endif
#if HW_WITH_PUMP
#include <HivewirePump.h>
#define NODE_FAMILY "WaterNode"
#else
#define NODE_FAMILY "SoilNode"
#endif

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
// DS18B20 soil temperature probe, one per node, on its own 1-Wire bus. GPIO6
// is free on both families (WaterNode's pump and float are 4 and 5). -1 to
// leave the pin alone.
#ifndef SOIL_TEMP_PIN
#define SOIL_TEMP_PIN 6
#endif
// ...or on the AHT20's old SDA wire, when the probe replaced the air sensor and
// reuses its wiring: either SDA pad, since boards were built both ways (see
// I2C_SDA_ALT). Only tried when no AHT20 answered -- the two cannot share a
// line -- and the AHT20 is never looked for again once the probe is found there.
static const int SOIL_TEMP_ALT_PINS[] = {I2C_SDA_PIN, I2C_SDA_ALT};

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
// Hands this node's own image to a neighbour (slot 24), the same way RangeNode
// does. A soil node out of the hive's reach can only be updated by a peer of
// its own family, and a range node's image is refused by family.
HivewireFwNodeSender fwTx(node);
// Only images of this same family are accepted -- see setFamily(). SoilNode
// and WaterNode are separate families: a pump image must never land on a
// board without a pump, or a sensor-only image on one that has one.
HW_FW_FAMILY(NODE_FAMILY);

#if HW_WITH_PUMP
#define PUMP_PIN  4
#define FLOAT_PIN 5
HivewirePump pump(node, PUMP_PIN, FLOAT_PIN);
// Application code (1000-1999, described in kinds.json): a dose the soil
// probe never saw arrive.
#define WATER_E_DOSE_NOT_SEEN 1001
static const uint32_t DOSE_CHECK_MS   = 15UL * 60 * 1000;
static const uint16_t DOSE_CHECK_MIN_ML = 50;     // smaller doses may not move the probe
static const uint16_t DOSE_SEEN_DROP  = 30;       // raw counts: wetter reads LOWER
#endif

// ---- readings, refreshed in loop() -- samplers only copy them ---------------
// Samplers run inside the library's slot scheduler; an 80 ms I2C conversion
// does not belong there. loop() reads, samplers report the last result.
static int16_t  tempCenti = 0;       // 0.01 C
static uint16_t humCenti  = 0;       // 0.01 %RH
static uint16_t soilRaw   = 0;       // ADC counts, 0-4095
static uint8_t  soilPct   = 0;
static uint16_t battMv    = 0;       // 0 = no divider fitted / not measured
static uint8_t  battPct   = 0;       // from the LiPo curve, not a linear scale
// bit0 AHT20, bit1 soil, bit2 battery, bit3 soil temp, bit4 soil temp probe
// fitted IN PLACE of the AHT20 (so its absence is not a fault)
static uint8_t  sensorOk  = 0;
static uint16_t ahtFails  = 0;
// INT16_MIN until the probe has given a good reading, and again whenever it
// stops: outside every slot's "valid" range, so it is never charted as 0 C.
static int16_t  soilTempCenti = INT16_MIN;
static bool     soilTempSeen  = false;
static uint16_t soilTempFails = 0;
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

// CRC-8 the AHT20 appends to every reading (poly 0x31, init 0xFF, over the
// status byte and five data bytes -- datasheet section 5.4).
static uint8_t aht20Crc(const uint8_t *p, int n) {
  uint8_t c = 0xFF;
  while (n--) {
    c ^= *p++;
    for (int k = 0; k < 8; k++) c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x31) : (uint8_t)(c << 1);
  }
  return c;
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
  // A reading garbled on the bus -- or a sensor answering with all zeros,
  // which decodes to exactly -50.0 C and 0 %RH -- used to be published as a
  // real one: a rhubarb pot "fell" to -50 C on an afternoon in the 30s, and
  // it went into the history and up to g4rden. The CRC catches garbling, the
  // calibrated bit an uninitialised sensor, and the range whatever is left.
  if (aht20Crc(b, 6) != b[6]) return false;
  if (!(b[0] & 0x08)) return false;              // not calibrated: values are meaningless
  uint32_t h = ((uint32_t)b[1] << 12) | ((uint32_t)b[2] << 4) | (b[3] >> 4);
  uint32_t t = (((uint32_t)b[3] & 0x0F) << 16) | ((uint32_t)b[4] << 8) | b[5];
  rh = h / 1048576.0f * 100.0f;
  tC = t / 1048576.0f * 200.0f - 50.0f;
  if (tC < -40.0f || tC > 85.0f || rh <= 0.0f || rh > 100.0f) return false;   // outside what it can measure
  return true;
}

// Whether it is missing is decided in setup(): a board whose AHT20 was swapped
// for a soil temperature probe is not missing anything.
static bool ahtBegin() {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  if (aht20Init()) return true;
  Wire.end();
  Wire.begin(I2C_SDA_ALT, I2C_SCL_ALT);
  if (aht20Init()) { node.log("aht20 on alt pins %d/%d", I2C_SDA_ALT, I2C_SCL_ALT); return true; }
  Wire.end();
  return false;
}

// ---- DS18B20: minimal 1-Wire driver, one probe, no library -----------------
//
// The pin is open drain: driving it "high" lets go and the pull-up raises the
// line. Each time slot is tens of microseconds and must not be stretched by
// an interrupt (a late sample reads the wrong bit), so the timed part of each
// slot runs in a critical section -- never longer than ~70 us at a time.
#if SOIL_TEMP_PIN >= 0
static portMUX_TYPE owMux = portMUX_INITIALIZER_UNLOCKED;
static gpio_num_t OW = (gpio_num_t)SOIL_TEMP_PIN;

static void owSetup(int pin) {
  OW = (gpio_num_t)pin;
  gpio_config_t c = {};
  c.pin_bit_mask = 1ULL << pin;
  c.mode = GPIO_MODE_INPUT_OUTPUT_OD;
  c.pull_up_en = GPIO_PULLUP_ENABLE;      // weak backup only; the 4.7k is what works
  gpio_config(&c);
  gpio_set_level(OW, 1);
  delayMicroseconds(200);                 // let the line rise before the first reset
}

// True when a device answered the reset with a presence pulse.
static bool owReset() {
  if (!gpio_get_level(OW)) return false;  // line held low: shorted, or no pull-up
  gpio_set_level(OW, 0);
  delayMicroseconds(480);
  portENTER_CRITICAL(&owMux);
  gpio_set_level(OW, 1);
  delayMicroseconds(70);
  bool present = !gpio_get_level(OW);
  portEXIT_CRITICAL(&owMux);
  delayMicroseconds(410);
  return present;
}

static void owWriteBit(bool b) {
  portENTER_CRITICAL(&owMux);
  gpio_set_level(OW, 0);
  delayMicroseconds(b ? 6 : 60);
  gpio_set_level(OW, 1);
  portEXIT_CRITICAL(&owMux);
  delayMicroseconds(b ? 64 : 10);
}

static bool owReadBit() {
  portENTER_CRITICAL(&owMux);
  gpio_set_level(OW, 0);
  delayMicroseconds(3);
  gpio_set_level(OW, 1);
  delayMicroseconds(10);
  bool b = gpio_get_level(OW);
  portEXIT_CRITICAL(&owMux);
  delayMicroseconds(53);
  return b;
}

static void owWrite(uint8_t v) { for (int i = 0; i < 8; i++) owWriteBit((v >> i) & 1); }
static uint8_t owRead() {
  uint8_t v = 0;
  for (int i = 0; i < 8; i++) if (owReadBit()) v |= 1 << i;
  return v;
}

// Dallas/Maxim CRC-8 (x^8 + x^5 + x^4 + 1, reflected), as the scratchpad's
// ninth byte carries it.
static uint8_t owCrc(const uint8_t *p, int n) {
  uint8_t c = 0;
  while (n--) {
    uint8_t b = *p++;
    for (int k = 0; k < 8; k++) {
      uint8_t mix = (c ^ b) & 1;
      c >>= 1;
      if (mix) c ^= 0x8C;
      b >>= 1;
    }
  }
  return c;
}

// SKIP ROM (0xCC) addresses whatever is on the bus: fine with one probe.
static bool dsStartConversion() {
  if (!owReset()) return false;
  owWrite(0xCC);
  owWrite(0x44);
  return true;
}

static bool dsReadCenti(int16_t &centi) {
  if (!owReset()) return false;
  owWrite(0xCC);
  owWrite(0xBE);
  uint8_t s[9];
  for (int i = 0; i < 9; i++) s[i] = owRead();
  // A line that never goes low reads all ones; all ones also passes no CRC
  // check worth having, so reject it outright.
  bool allOnes = true;
  for (int i = 0; i < 9; i++) if (s[i] != 0xFF) allOnes = false;
  if (allOnes || owCrc(s, 8) != s[8]) return false;
  int16_t raw = (int16_t)((s[1] << 8) | s[0]);       // 1/16 C at 12-bit
  // 85.0 C is the power-on value, read before any conversion finished: a
  // probe that browned out mid-read, not soil at 85 C.
  if (raw == 0x0550) return false;
  if (raw < -55 * 16 || raw > 125 * 16) return false;
  centi = (int16_t)((raw * 100) / 16);
  return true;
}
#endif

// Which hole the probe is on: GPIO6, else -- only when no AHT20 answered --
// the AHT20's old SDA wire. Left on GPIO6 when neither answers, so a probe
// fitted there later is picked up without a restart.
static bool soilTempOnAlt = false;
static int  soilTempPin   = -1;           // where a probe answered at boot, -1 none
static void soilTempFind() {
#if SOIL_TEMP_PIN >= 0
  owSetup(SOIL_TEMP_PIN);
  if (owReset()) { soilTempPin = SOIL_TEMP_PIN; return; }
  if (!ahtFound) {
    for (int pin : SOIL_TEMP_ALT_PINS) {
      owSetup(pin);
      if (owReset()) {
        soilTempPin = pin;
        soilTempOnAlt = true;
        return;
      }
      gpio_reset_pin((gpio_num_t)pin);
    }
  }
  owSetup(SOIL_TEMP_PIN);
#endif
}

// Called every READ_EVERY_MS: collects the conversion the last call started,
// then starts the next. A conversion takes up to 750 ms, so this never waits
// for one -- the reading is at most one interval old.
static void readSoilTemp() {
#if SOIL_TEMP_PIN >= 0
  static bool pending = false;
  int16_t c;
  bool ok = pending && dsReadCenti(c);
  pending = dsStartConversion();
  if (ok) {
    soilTempCenti = c;
    sensorOk |= 8;
    if (!soilTempSeen) node.log("soil temp probe found");
    soilTempSeen = true;
  } else if (pending && !soilTempSeen) {
    // First call after boot, or a probe just plugged in: nothing to collect
    // yet, and nothing wrong.
  } else {
    soilTempCenti = INT16_MIN;
    sensorOk &= ~8;
    // A board without a probe is normal and says nothing; one that HAD a
    // probe and lost it is a fault worth raising.
    if (soilTempSeen && ++soilTempFails % 10 == 1)
      hwErr(node, HW_E_PERIPHERAL_READ_FAILED, 16, "soil temp read failed (%u)", soilTempFails);
  }
#endif
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
  if (!ahtFound && !soilTempOnAlt) {     // the probe owns the AHT20's wire now
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
    if (ahtFound && ++ahtFails % 10 == 1)
      hwErr(node, HW_E_PERIPHERAL_READ_FAILED, 1, "aht20 read failed (%u)", ahtFails);
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
    hwErr(node, soilSteady ? HW_E_VALUE_OUT_OF_RANGE : HW_E_INPUT_FLOATING, 3,
          "soil: median %u spread %u", soilRaw, spread);   // subject: slot 3, soil raw
  float pct = 100.0f * (float)(SOIL_ADC_DRY - (int)soilRaw) / (float)(SOIL_ADC_DRY - SOIL_ADC_WET);
  soilPct = pct < 0 ? 0 : pct > 100 ? 100 : (uint8_t)lroundf(pct);

  readSoilTemp();
  if (soilTempOnAlt) sensorOk |= 16; else sensorOk &= ~16;

  // 0 means "no divider on this board", not "flat" -- see batteryFitted().
  if (battFitted) {
    uint32_t mv = medianMilliVolts(BATT_ADC_PIN) * 2;     // 1M/1M divider halves it
    battMv = mv > 65535 ? 65535 : mv;
    battPct = lipoPercent(battMv);
    // Once per crossing, not on every read of an empty cell.
    static bool wasLow = false;
    bool low = battPct < 15;
    if (low && !wasLow) hwErr(node, HW_E_BATTERY_LOW, 10, "battery %umV (%u%%)", battMv, battPct);
    wasLow = low;
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
static void sSoilTemp(void *o) { memcpy(o, &soilTempCenti, 2); }
static void sBatt(void *o)     { memcpy(o, &battMv, 2); }
static void sBattPct(void *o)  { memcpy(o, &battPct, 1); }
static void sOk(void *o)       { memcpy(o, &sensorOk, 1); }
static void sBoots(void *o)    { uint8_t v = bootCount > 255 ? 255 : bootCount; memcpy(o, &v, 1); }
static void sUptimeMin(void *o){ uint16_t v = millis() / 60000UL; memcpy(o, &v, 2); }
// Best path in the last 1-2 minutes, not the last packet: see bestRssi().
static void sRssi(void *o)     { int8_t v = node.bestRssi(); memcpy(o, &v, 1); }
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

// ---- appliers ---------------------------------------------------------------
static void aAction(const void *in) {
  lastAction = *(const uint8_t *)in;
  if (lastAction == 4) { node.log("reboot by command"); delay(50); ESP.restart(); }
  if (lastAction == 5) ota.trigger();
}
static void aOtaArm(const void *in) { otaArm = *(const uint8_t *)in; ota.arm(otaArm); }

#if HW_WITH_PUMP
// ---- pump slots (numbers and meaning: HivewirePump.h) ----------------------
static uint16_t doseReq = 0, runReq = 0;
static void sDose(void *o)     { memcpy(o, &doseReq, 2); }
static void aDose(const void *in) { memcpy(&doseReq, in, 2); pump.requestDose(doseReq); }
static void sRunS(void *o)     { memcpy(o, &runReq, 2); }
static void aRunS(const void *in) { memcpy(&runReq, in, 2); pump.requestRunSeconds(runReq); }
static void sFlow(void *o)     { uint16_t v = pump.flow(); memcpy(o, &v, 2); }
static void aFlow(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_FLOW, v); }
static void sMaxDose(void *o)  { uint16_t v = pump.maxDose(); memcpy(o, &v, 2); }
static void aMaxDose(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_MAX_DOSE, v); }
static void sMaxDay(void *o)   { uint16_t v = pump.maxDay(); memcpy(o, &v, 2); }
static void aMaxDay(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_MAX_DAY, v); }
static void sDayMl(void *o)    { uint16_t v = pump.dayMl(); memcpy(o, &v, 2); }
static void sPumpState(void *o){ uint8_t v = pump.state(); memcpy(o, &v, 1); }
static void sReservoir(void *o){ uint8_t v = pump.reservoir(); memcpy(o, &v, 1); }
static void sFloat(void *o)    { uint8_t v = pump.floatMode(); memcpy(o, &v, 1); }
static void aFloat(const void *in) { pump.requestSetting(HW_PUMP_SLOT_FLOAT, *(const uint8_t *)in); }
// Automatic watering (HivewirePump.h, slots 49-54).
static void sAutoOn(void *o)   { uint8_t v = pump.autoOn(); memcpy(o, &v, 1); }
static void aAutoOn(const void *in) { pump.requestSetting(HW_PUMP_SLOT_AUTO_ON, *(const uint8_t *)in); }
static void sAutoBelow(void *o){ uint16_t v = pump.autoBelowRaw(); memcpy(o, &v, 2); }
static void aAutoBelow(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_AUTO_BELOW, v); }
static void sAutoMl(void *o)   { uint16_t v = pump.autoMl(); memcpy(o, &v, 2); }
static void aAutoMl(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_AUTO_ML, v); }
static void sAutoGap(void *o)  { uint16_t v = pump.autoGapMin(); memcpy(o, &v, 2); }
static void aAutoGap(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_AUTO_GAP, v); }
static void sAutoState(void *o){ uint8_t v = pump.autoState(); memcpy(o, &v, 1); }
static void sAutoSince(void *o){ uint16_t v = pump.minutesSinceAuto(); memcpy(o, &v, 2); }
#endif

// Slot ids 22, 23 and 25 match RangeNode, so the same maintenance commands
// (reboot, arm, firmware CRC) work on every node type.
//  id  type    dir            sample  report  thresh min  max  sampler     applier
static const HwSlotDef SLOTS[] = {
  {  1, HW_I16, HW_DIR_OUT,     30000, 900000,    50,   0,   0, sTemp,      nullptr },  // 0.5 C
  {  2, HW_U16, HW_DIR_OUT,     30000, 900000,   200,   0,   0, sHum,       nullptr },  // 2 %RH
  {  3, HW_U16, HW_DIR_OUT,     30000, 900000,    60,   0,   0, sSoilRaw,   nullptr },
  {  4, HW_U8,  HW_DIR_OUT,     30000, 900000,     3,   0,   0, sSoilPct,   nullptr },
  // 16, not 11: the admin's g4rden upload maps slot numbers to fields for
  // every kind, and 11-15 are wind on a WeatherNode.
  { 16, HW_I16, HW_DIR_OUT,     30000, 900000,    25,   0,   0, sSoilTemp,  nullptr },  // 0.25 C
  {  5, HW_U16, HW_DIR_OUT,     60000, 900000,    50,   0,   0, sBatt,      nullptr },
  { 10, HW_U8,  HW_DIR_OUT,     60000, 900000,     5,   0,   0, sBattPct,   nullptr },
  {  6, HW_U8,  HW_DIR_OUT,     30000, 900000,     1,   0,   0, sOk,        nullptr },
  {  7, HW_U8,  HW_DIR_OUT,    300000, 900000,     1,   0,   0, sBoots,     nullptr },
  {  8, HW_U16, HW_DIR_OUT,     60000, 900000,    15,   0,   0, sUptimeMin, nullptr },
  {  9, HW_I8,  HW_DIR_OUT,     30000, 900000,     6,   0,   0, sRssi,      nullptr },
  { 22, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0,   5, sAction,    aAction },
  { 23, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0, 255, sOtaArm,    aOtaArm },
  { 24, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0, 255, sSeed,      aSeed   },
  { 25, HW_U32, HW_DIR_OUT,    600000, 900000,     1,   0,   0, sFwCrc,     nullptr },
  // The error pair every node publishes (HivewireErrors.h): last code and count.
  { HW_ERR_SLOT_LAST,  HW_U32, HW_DIR_OUT, 30000, 900000, 1, 0, 0, hwErrSlotLast,  nullptr },
  { HW_ERR_SLOT_COUNT, HW_U16, HW_DIR_OUT, 30000, 900000, 1, 0, 0, hwErrSlotCount, nullptr },
#if HW_WITH_PUMP
  { HW_PUMP_SLOT_DOSE,      HW_U16, HW_DIR_INOUT,     0, 900000, 0, 0, 5000, sDose,      aDose    },
  { HW_PUMP_SLOT_RUN_S,     HW_U16, HW_DIR_INOUT,     0, 900000, 0, 0,  600, sRunS,      aRunS    },
  { HW_PUMP_SLOT_FLOW,      HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 1, 5000, sFlow,      aFlow    },
  { HW_PUMP_SLOT_MAX_DOSE,  HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 1, 5000, sMaxDose,   aMaxDose },
  { HW_PUMP_SLOT_MAX_DAY,   HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 1, 20000, sMaxDay,   aMaxDay  },
  { HW_PUMP_SLOT_DAY_ML,    HW_U16, HW_DIR_OUT,    5000, 900000, 1, 0,    0, sDayMl,     nullptr  },
  { HW_PUMP_SLOT_STATE,     HW_U8,  HW_DIR_OUT,    1000, 900000, 1, 0,    0, sPumpState, nullptr  },
  { HW_PUMP_SLOT_RESERVOIR, HW_U8,  HW_DIR_OUT,    5000, 900000, 1, 0,    0, sReservoir, nullptr  },
  { HW_PUMP_SLOT_FLOAT,     HW_U8,  HW_DIR_INOUT, 60000, 900000, 1, 0,    2, sFloat,     aFloat   },
  { HW_PUMP_SLOT_AUTO_ON,   HW_U8,  HW_DIR_INOUT, 60000, 900000, 1, 0,    1, sAutoOn,    aAutoOn    },
  { HW_PUMP_SLOT_AUTO_BELOW,HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 1, 4095, sAutoBelow, aAutoBelow },
  { HW_PUMP_SLOT_AUTO_ML,   HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 1, 5000, sAutoMl,    aAutoMl    },
  { HW_PUMP_SLOT_AUTO_GAP,  HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 10, 43200, sAutoGap, aAutoGap   },
  { HW_PUMP_SLOT_AUTO_STATE,HW_U8,  HW_DIR_OUT,    5000, 900000, 1, 0,    0, sAutoState, nullptr    },
  { HW_PUMP_SLOT_AUTO_SINCE,HW_U16, HW_DIR_OUT,   60000, 900000, 30, 0,   0, sAutoSince, nullptr    },
#endif
};
static const uint8_t N_SLOTS = sizeof(SLOTS) / sizeof(SLOTS[0]);

void setup() {
#if HW_WITH_PUMP
  pump.off();                       // before anything slow: a floating pin is not "off"
#endif
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

  // A pushed image on a board with no stored id cannot know which node it is,
  // and waiting for a USB `setid` would strand a deployed node: go back to the
  // image that knew. A no-op on a board being set up by hand.
  if (NODE_ID == 0) HivewireOta::revertIfProvisional("no node id stored");
  if (NODE_ID == 0) hwprov::waitForId("soilnode", NODE_FAMILY);   // never returns
  hwprov::printId(NODE_FAMILY, NODE_ID);
  // Before the swarm radio: a scan hops channels. Raised now, published once
  // the node has joined (hwErr keeps it until then).
  if (hwprov::radioSelfTest() == 0) hwErr(node, HW_E_RADIO_DEAF, 0, "radio heard no networks");

  node.onRaw([](const uint8_t *d, int n) {
    // Never start accepting an update while sending one: applying it would
    // reboot this node halfway through the transfer it is serving.
    if (!fwTx.active()) fw.ingest(d, n);
    fwTx.ingest(d, n);
    // Someone else's firmware reply on its way to the hive: carry it, so a
    // node the hive hears only through us can still be updated. Not while we
    // are the sender -- then those replies are addressed to us.
    if (!fwTx.active() && n >= (int)sizeof(HwHeader) &&
        ((const HwHeader *)d)->type == HW_MSG_FW_NACK) node.relayRaw(d, n);
  });
  if (!node.begin(NODE_ID, HW_ROLE_SENSOR, SLOTS, N_SLOTS)) {
    Serial.println("E102 hivewire: begin failed");
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
#if HW_WITH_PUMP
  pump.begin();
  // Stop pumping when the hive goes quiet, and before any update: an update
  // is an outage, and a pump must not be left running through one.
  node.onSafe([] { pump.stop(HW_PUMP_STOPPED_SAFE); });
  ota.onBeforeUpdate([] { pump.stop(HW_PUMP_STOPPED_SAFE); });
  fw.onBeforeUpdate([] { pump.stop(HW_PUMP_STOPPED_SAFE); });
#endif

  ahtFound = ahtBegin();
  soilTempFind();
  if (!ahtFound && !soilTempOnAlt)
    hwErr(node, HW_E_PERIPHERAL_MISSING, 1, "aht20 not found");   // subject: slot 1, air temp
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
  // readSensors() above only started the first soil temperature conversion;
  // collect it now rather than a whole interval later.
  delay(800);
  readSoilTemp();
  if (soilTempPin >= 0)
    Serial.printf("soil temp probe on GPIO%d: %s\n", soilTempPin,
                  (sensorOk & 8) ? "reading" : "answers but no reading yet");
  else
    Serial.printf("soil temp probe: none\n");
  if (soilTempOnAlt) node.log("soil temp probe on GPIO%d (in place of the aht20)", soilTempPin);
}

void loop() {
  node.loop();
  ota.loop();
  fw.loop();
  fwTx.loop();
  hwprov::poll("soilnode", NODE_FAMILY, NODE_ID);
#if HW_WITH_PUMP
  pump.loop();
  // Automatic watering judges the same reading the slots publish; bit 1 of
  // sensorOk says the probe is really there (a floating pin is not "dry").
  pump.autoLoop((sensorOk & 2) != 0, soilRaw);
  // Did the water arrive? Soil raw at the start of a dose, compared 15 min
  // after it ends. A capacitive probe reads LOWER when wetter.
  {
    static bool was = false;
    static uint16_t rawBefore = 0;
    static uint32_t checkAt = 0;
    static uint16_t doseMl = 0;
    static bool wasAuto = false;
    bool on = pump.running();
    if (on && !was) rawBefore = soilRaw;
    if (!on && was && pump.state() == HW_PUMP_DONE && pump.lastDoseMl() >= DOSE_CHECK_MIN_ML &&
        (sensorOk & 2)) {
      checkAt = millis() + DOSE_CHECK_MS;
      doseMl = pump.lastDoseMl();
      wasAuto = pump.lastDoseWasAuto();
      if (!checkAt) checkAt = 1;
    }
    was = on;
    if (checkAt && (int32_t)(millis() - checkAt) >= 0) {
      checkAt = 0;
      if ((sensorOk & 2) && soilRaw + DOSE_SEEN_DROP > rawBefore) {
        hwErr(node, WATER_E_DOSE_NOT_SEEN, HW_PUMP_SLOT_DOSE, "dose %u ml not seen: soil %u -> %u",
              doseMl, rawBefore, soilRaw);
        // Only an AUTOMATIC watering locks automatic watering off: a manual
        // one into a measuring cup is supposed to miss the soil.
        if (wasAuto) pump.autoLockout();
      }
      else
        node.log("dose %u ml seen: soil %u -> %u", doseMl, rawBefore, soilRaw);
    }
  }
#endif
  hwErrCheckLink(node);

  // Same rules as RangeNode: never while busy, never to itself, and never an
  // image that has not yet proven it can rejoin the swarm.
  if (seedWanted >= 0) {
    uint8_t v = (uint8_t)seedWanted;
    seedWanted = -1;
    uint8_t target = (v == 255) ? HIVEWIRE_TARGET_ALL : v;
    if (fw.active() || fwTx.active()) {
      node.log("fw: seed refused, busy");
    } else if (v == NODE_ID) {
      node.log("fw: seed refused, self");
    } else if (ota.updating()) {
      node.log("fw: seed refused, image unconfirmed");
    } else if (!flashSrc.begin()) {
      node.log("fw: seed refused, image unverified");
    } else if (fwTx.begin(target, flashSrc.length(), flashSrc.crc(), HivewireFlashProvider::feed)) {
      node.log("fw: seed %lu b crc %08lx to %u", (unsigned long)flashSrc.length(),
               (unsigned long)flashSrc.crc(), v);
    } else {
      node.log("fw: seed refused, sender");
    }
  }
  static bool wasSeeding = false;
  if (wasSeeding && !fwTx.active()) node.log("fw: seed finished");
  wasSeeding = fwTx.active();

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
  if (!fw.active() && !fwTx.active() && millis() - lastRead >= READ_EVERY_MS) {
    lastRead = millis();
    readSensors();
    char st[12];
    if (soilTempCenti == INT16_MIN) snprintf(st, sizeof(st), "none");
    else snprintf(st, sizeof(st), "%.2fC", soilTempCenti / 100.0f);
    Serial.printf("t=%.2fC rh=%.2f%% soil=%u (%u%%) soilT=%s batt=%s ok=%u nb=%u ep=%lu\n",
                  tempCenti / 100.0f, humCenti / 100.0f, soilRaw, soilPct, st,
                  battFitted ? battStr(battMv, battPct) : "none fitted",
                  sensorOk, node.neighbors(), (unsigned long)node.epoch());
  }
}
