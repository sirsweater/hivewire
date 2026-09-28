/*
 * Hivewire -- PumpNode
 *
 * A pump (or valve) on the swarm, with nothing else on the board: one bucket
 * feeding a bed, say. For a pump in a pot that also has a soil probe, build
 * SoilNode with -DHW_WITH_PUMP=1 instead (the WaterNode family), which can
 * also check that each dose actually reached the soil.
 *
 * Every rule that keeps the water in the bucket is in HivewirePump.h: off at
 * power-up and whenever the hive goes quiet or an update starts, doses in ml
 * against per-dose and per-24 h caps that are refused rather than clipped, an
 * absolute run-time ceiling, and an optional float switch.
 *
 * Wiring (C6 SuperMini):
 *
 *   Pump switch  logic-level MOSFET module input (e.g. isolated LR7843) -> GPIO4
 *                (the module's own supply and the pump are on the 12 V side)
 *   Float switch optional, GPIO5 <-> GND (set its mode in slot 48)
 *
 * Slots: the pump's 40-48 (HivewirePump.h), plus the maintenance slots every
 * bundled sketch shares: 7 boots, 8 uptime, 9 signal, 22 action, 23 OTA arm,
 * 24 seed, 25 firmware CRC, 26/27 errors.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>
#include <HivewireOta.h>
#include <HivewireFirmware.h>
#include <HivewireProvision.h>
#include <HivewireErrors.h>
#include <HivewirePump.h>
#include <Preferences.h>

#ifndef HW_NODE_ID
#define HW_NODE_ID 0            // generic image: numbered over USB after flashing
#endif
static uint8_t NODE_ID = HW_NODE_ID;

#define PUMP_PIN  4
#define FLOAT_PIN 5

HivewireNode node;
HivewireOta  ota(node);
HivewireFwReceiver fw(node);
HivewireFlashProvider flashSrc;
HivewireFwNodeSender fwTx(node);
HivewirePump pump(node, PUMP_PIN, FLOAT_PIN);
HW_FW_FAMILY("PumpNode");

static uint32_t bootCount = 0;
static uint32_t runningCrc = 0;
static uint8_t  lastAction = 0, otaArm = 0, seedTarget = 0;
static volatile int16_t seedWanted = -1;
static uint16_t doseReq = 0, runReq = 0;

// ---- samplers and appliers ------------------------------------------------
static void sBoots(void *o)     { uint8_t v = bootCount > 255 ? 255 : bootCount; memcpy(o, &v, 1); }
static void sUptimeMin(void *o) { uint16_t v = millis() / 60000UL; memcpy(o, &v, 2); }
static void sRssi(void *o)      { int8_t v = node.bestRssi(); memcpy(o, &v, 1); }
static void sAction(void *o)    { memcpy(o, &lastAction, 1); }
static void aAction(const void *in) {
  lastAction = *(const uint8_t *)in;
  if (lastAction == 4) { pump.stop(); node.log("reboot by command"); delay(50); ESP.restart(); }
  if (lastAction == 5) ota.trigger();
}
static void sOtaArm(void *o)    { memcpy(o, &otaArm, 1); }
static void aOtaArm(const void *in) { otaArm = *(const uint8_t *)in; ota.arm(otaArm); }
static void sSeed(void *o)      { memcpy(o, &seedTarget, 1); }
static void aSeed(const void *in) { seedTarget = *(const uint8_t *)in; if (seedTarget) seedWanted = seedTarget; }
static void sFwCrc(void *o)     { memcpy(o, &runningCrc, 4); }

static void sDose(void *o)      { memcpy(o, &doseReq, 2); }
static void aDose(const void *in) { memcpy(&doseReq, in, 2); pump.requestDose(doseReq); }
static void sRunS(void *o)      { memcpy(o, &runReq, 2); }
static void aRunS(const void *in) { memcpy(&runReq, in, 2); pump.requestRunSeconds(runReq); }
static void sFlow(void *o)      { uint16_t v = pump.flow(); memcpy(o, &v, 2); }
static void aFlow(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_FLOW, v); }
static void sMaxDose(void *o)   { uint16_t v = pump.maxDose(); memcpy(o, &v, 2); }
static void aMaxDose(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_MAX_DOSE, v); }
static void sMaxDay(void *o)    { uint16_t v = pump.maxDay(); memcpy(o, &v, 2); }
static void aMaxDay(const void *in) { uint16_t v; memcpy(&v, in, 2); pump.requestSetting(HW_PUMP_SLOT_MAX_DAY, v); }
static void sDayMl(void *o)     { uint16_t v = pump.dayMl(); memcpy(o, &v, 2); }
static void sPumpState(void *o) { uint8_t v = pump.state(); memcpy(o, &v, 1); }
static void sReservoir(void *o) { uint8_t v = pump.reservoir(); memcpy(o, &v, 1); }
static void sFloat(void *o)     { uint8_t v = pump.floatMode(); memcpy(o, &v, 1); }
static void aFloat(const void *in) { pump.requestSetting(HW_PUMP_SLOT_FLOAT, *(const uint8_t *)in); }

//  id  type    dir            sample  report  thresh min  max    sampler     applier
static const HwSlotDef SLOTS[] = {
  {  7, HW_U8,  HW_DIR_OUT,    300000, 900000,     1,   0,    0, sBoots,     nullptr },
  {  8, HW_U16, HW_DIR_OUT,     60000, 900000,    15,   0,    0, sUptimeMin, nullptr },
  {  9, HW_I8,  HW_DIR_OUT,     30000, 900000,     6,   0,    0, sRssi,      nullptr },
  { 22, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0,    5, sAction,    aAction },
  { 23, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0,  255, sOtaArm,    aOtaArm },
  { 24, HW_U8,  HW_DIR_INOUT,       0, 900000,     0,   0,  255, sSeed,      aSeed   },
  { 25, HW_U32, HW_DIR_OUT,    600000, 900000,     1,   0,    0, sFwCrc,     nullptr },
  { HW_ERR_SLOT_LAST,  HW_U32, HW_DIR_OUT, 30000, 900000, 1, 0, 0, hwErrSlotLast,  nullptr },
  { HW_ERR_SLOT_COUNT, HW_U16, HW_DIR_OUT, 30000, 900000, 1, 0, 0, hwErrSlotCount, nullptr },
  { HW_PUMP_SLOT_DOSE,      HW_U16, HW_DIR_INOUT,     0, 900000, 0, 0,  5000, sDose,      aDose    },
  { HW_PUMP_SLOT_RUN_S,     HW_U16, HW_DIR_INOUT,     0, 900000, 0, 0,   600, sRunS,      aRunS    },
  { HW_PUMP_SLOT_FLOW,      HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 1,  5000, sFlow,      aFlow    },
  { HW_PUMP_SLOT_MAX_DOSE,  HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 1,  5000, sMaxDose,   aMaxDose },
  { HW_PUMP_SLOT_MAX_DAY,   HW_U16, HW_DIR_INOUT, 60000, 900000, 1, 1, 20000, sMaxDay,    aMaxDay  },
  { HW_PUMP_SLOT_DAY_ML,    HW_U16, HW_DIR_OUT,    5000, 900000, 1, 0,     0, sDayMl,     nullptr  },
  { HW_PUMP_SLOT_STATE,     HW_U8,  HW_DIR_OUT,    1000, 900000, 1, 0,     0, sPumpState, nullptr  },
  { HW_PUMP_SLOT_RESERVOIR, HW_U8,  HW_DIR_OUT,    5000, 900000, 1, 0,     0, sReservoir, nullptr  },
  { HW_PUMP_SLOT_FLOAT,     HW_U8,  HW_DIR_INOUT, 60000, 900000, 1, 0,     2, sFloat,     aFloat   },
};
static const uint8_t N_SLOTS = sizeof(SLOTS) / sizeof(SLOTS[0]);

void setup() {
  pump.off();                       // before anything slow: a floating pin is not "off"
  Serial.begin(115200);
  delay(200);

  Preferences prefs;
  prefs.begin("pumpnode", false);
  bootCount = prefs.getUInt("boots", 0) + 1;
  prefs.putUInt("boots", bootCount);
  if (!prefs.isKey("id")) prefs.putUChar("id", HW_NODE_ID);
  NODE_ID = prefs.getUChar("id", HW_NODE_ID);
  prefs.end();

  if (NODE_ID == 0) HivewireOta::revertIfProvisional("no node id stored");
  if (NODE_ID == 0) hwprov::waitForId("pumpnode", "PumpNode");   // never returns
  hwprov::printId("PumpNode", NODE_ID);
  if (hwprov::radioSelfTest() == 0) hwErr(node, HW_E_RADIO_DEAF, 0, "radio heard no networks");

  node.onRaw([](const uint8_t *d, int n) {
    if (!fwTx.active()) fw.ingest(d, n);
    fwTx.ingest(d, n);
    if (!fwTx.active() && n >= (int)sizeof(HwHeader) &&
        ((const HwHeader *)d)->type == HW_MSG_FW_NACK) node.relayRaw(d, n);
  });
  if (!node.begin(NODE_ID, HW_ROLE_ACTUATOR, SLOTS, N_SLOTS)) {
    Serial.println("E102 hivewire: begin failed");
    delay(1000);
    ESP.restart();
  }
  node.log("boot #%lu", (unsigned long)bootCount);
  ota.begin();
  if (flashSrc.begin()) {
    runningCrc = flashSrc.crc();
    fw.setRunningImage(flashSrc.length(), runningCrc);
  }
  fw.setFamily(hwFwFamily);
  fw.onApplied([] { ota.markPending(); });

  pump.begin();
  node.onSafe([] { pump.stop(HW_PUMP_STOPPED_SAFE); });
  ota.onBeforeUpdate([] { pump.stop(HW_PUMP_STOPPED_SAFE); });
  fw.onBeforeUpdate([] { pump.stop(HW_PUMP_STOPPED_SAFE); });
  Serial.printf("PumpNode %u up, boot #%lu, flow %u ml/min\n", NODE_ID,
                (unsigned long)bootCount, pump.flow());
}

void loop() {
  node.loop();
  ota.loop();
  fw.loop();
  fwTx.loop();
  pump.loop();
  hwprov::poll("pumpnode", "PumpNode", NODE_ID);
  hwErrCheckLink(node);

  if (seedWanted >= 0) {
    uint8_t v = (uint8_t)seedWanted;
    seedWanted = -1;
    uint8_t target = (v == 255) ? HIVEWIRE_TARGET_ALL : v;
    if (fw.active() || fwTx.active()) node.log("fw: seed refused, busy");
    else if (v == NODE_ID)            node.log("fw: seed refused, self");
    else if (ota.updating())          node.log("fw: seed refused, image unconfirmed");
    else if (!flashSrc.begin())       node.log("fw: seed refused, image unverified");
    else if (fwTx.begin(target, flashSrc.length(), flashSrc.crc(), HivewireFlashProvider::feed))
      node.log("fw: seed %lu b to %u", (unsigned long)flashSrc.length(), v);
    else node.log("fw: seed refused, sender");
  }
}
