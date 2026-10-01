// HivewirePump.h -- a pump (or valve) on the swarm that cannot flood anything.
//
// Header-only and opt-in, like HivewireOta.h: a sketch that drives no water
// pays nothing for it. Used by examples/WaterNode (soil sensor + pump on one
// board) and examples/PumpNode (a pump alone).
//
// A pump is the first output in this project that can do real damage when it
// misbehaves: a stuck-on pump empties a 5-gallon bucket onto a floor. So every
// rule that keeps water in the bucket lives here, not in the sketches:
//
//   - OFF unless told otherwise: driven off in begin(), before anything slow,
//     and by stop() from onSafe() (the hive went quiet), before any firmware
//     update, and on every refusal.
//   - A dose is asked for in ml and converted with a measured flow rate
//     (calibrate once: run N seconds into a measuring cup, set ml/min).
//   - Limits are REFUSALS, never silent clipping: a dose over the per-dose cap,
//     or one that would take the last 24 h over the daily cap, is not started
//     and raises E306 (subject: the slot of the limit it hit). Half a dose
//     delivered quietly would read as "watered" to everyone above.
//   - The 24 h total survives a restart (RTC memory: kept through crashes,
//     watchdogs and brownouts, lost only on a full power loss), so a node in
//     a reset loop cannot water once per boot.
//   - An absolute run-time ceiling independent of the calibration: a wrong
//     flow rate can make a dose long, never endless.
//   - An optional float switch: an empty supply refuses and stops a dose
//     (E307) instead of running the pump dry.
//
// Slot convention (the admin's kinds.json labels them; ids chosen clear of
// the maintenance slots 20-27):
//
//   40  dose ml          action   write N: pump N ml now (0 = stop)
//   41  run seconds      action   write N: run N s regardless of calibration
//                                 (for calibrating into a measuring cup)
//   42  flow ml/min      setting  calibration, kept in NVS
//   43  max ml per dose  setting
//   44  max ml per day   setting  (rolling 24 h)
//   45  ml in last 24 h  out
//   46  state            out      see HwPumpState
//   47  reservoir        out      0 empty, 1 ok, 2 no float switch configured
//   48  float switch     setting  0 none, 1 closed = water present, 2 open = water present
//
// Automatic watering (needs a soil probe -- WaterNode feeds it through
// autoLoop(); a bare PumpNode never enables it):
//
//   49  auto on          setting  0/1; writing 1 also clears a lock-out
//   50  water below      setting  soil RAW reading that counts as dry (a
//                                 capacitive probe reads HIGHER when drier; the
//                                 admin converts the user's % with the probe's
//                                 own calibration)
//   51  auto amount ml   setting  each automatic watering
//   52  min gap minutes  setting  never water automatically sooner than this
//   53  auto state       out      see HwAutoState
//   54  minutes since    out      since the last automatic watering
//                                 (65535 = none yet)
//
// It keeps going when the hive is unreachable -- a plant in a heatwave should
// not go thirsty because the Pi is off -- and every limit above still applies.
// It locks itself off (until auto is switched on again) if a watering is not
// seen in the soil: a probe pulled out of the pot reads bone dry, and without
// that lock it would water an empty spot forever.
//
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <esp_attr.h>
#include "Hivewire.h"
#include "HivewireErrors.h"

#define HW_PUMP_SLOT_DOSE     40
#define HW_PUMP_SLOT_RUN_S    41
#define HW_PUMP_SLOT_FLOW     42
#define HW_PUMP_SLOT_MAX_DOSE 43
#define HW_PUMP_SLOT_MAX_DAY  44
#define HW_PUMP_SLOT_DAY_ML   45
#define HW_PUMP_SLOT_STATE    46
#define HW_PUMP_SLOT_RESERVOIR 47
#define HW_PUMP_SLOT_FLOAT    48
#define HW_PUMP_SLOT_AUTO_ON  49
#define HW_PUMP_SLOT_AUTO_BELOW 50
#define HW_PUMP_SLOT_AUTO_ML  51
#define HW_PUMP_SLOT_AUTO_GAP 52
#define HW_PUMP_SLOT_AUTO_STATE 53
#define HW_PUMP_SLOT_AUTO_SINCE 54

enum HwAutoState : uint8_t {
  HW_AUTO_OFF = 0,
  HW_AUTO_WATCHING = 1,      // soil is wet enough
  HW_AUTO_GAP = 2,           // waiting out the minimum gap
  HW_AUTO_LOCKED = 3,        // a watering was not seen in the soil: off until re-enabled
  HW_AUTO_NO_PROBE = 4,      // no soil reading to judge by: not watering
  HW_AUTO_WATERING = 5,
};

enum HwPumpState : uint8_t {
  HW_PUMP_IDLE = 0,          // never run since boot
  HW_PUMP_RUNNING = 1,
  HW_PUMP_DONE = 2,          // last dose finished as asked
  HW_PUMP_REFUSED_LIMIT = 3, // a cap would have been exceeded; nothing ran
  HW_PUMP_STOPPED_EMPTY = 4, // the float switch said empty (before or during)
  HW_PUMP_STOPPED_SAFE = 5,  // stopped by stop(): hive lost, update, command
  HW_PUMP_STOPPED_TIMEOUT = 6, // hit the absolute run-time ceiling
  HW_PUMP_REFUSED_UNCALIBRATED = 7,
};

class HivewirePump {
 public:
  // pin: drives the switch (MOSFET module input); high = on unless activeLow.
  // floatPin: the float switch to GND (internal pull-up), or -1 for none.
  HivewirePump(HivewireNode &node, int8_t pin, int8_t floatPin = -1, bool activeLow = false)
      : _node(node), _pin(pin), _floatPin(floatPin), _activeLow(activeLow) {}

  // Call FIRST in setup(), before anything that can take time: until this
  // runs the pin floats, and whatever the switch makes of that is not "off".
  void off() {
    pinMode(_pin, OUTPUT);
    digitalWrite(_pin, _activeLow ? HIGH : LOW);
  }

  void begin() {
    off();
    if (_floatPin >= 0) pinMode(_floatPin, INPUT_PULLUP);
    Preferences p;
    p.begin(NVS_NS, true);
    _flow = p.getUShort("flow", 0);              // 0 = not calibrated yet
    _maxDose = p.getUShort("maxdose", 250);
    _maxDay = p.getUShort("maxday", 1500);
    _floatMode = p.getUChar("float", 0);
    _autoOn = p.getUChar("aon", 0);
    _autoLocked = p.getUChar("alock", 0);
    _belowRaw = p.getUShort("abelow", 0);
    _autoMl = p.getUShort("aml", 0);
    _gapMin = p.getUShort("agap", 720);
    p.end();
    // Carry the rolling 24 h total across a restart (see the header note).
    if (_rtc.magic == RTC_MAGIC && _rtc.elapsedMs < DAY_MS) {
      _dayMl = _rtc.dayMl;
      _winStart = millis() - _rtc.elapsedMs;      // unsigned: resumes mid-window
      _gapPending = _rtc.autoValid;
      _lastAutoAt = millis() - _rtc.autoElapsedMs;
    } else {
      _dayMl = 0;
      _winStart = millis();
      // After a real power loss nobody knows when it last watered: wait out a
      // full gap rather than risk watering twice in quick succession.
      _gapPending = _autoOn;
      _lastAutoAt = millis();
    }
    saveRtc();
  }

  // ---- commands (from slot appliers; they run in the radio callback, so they
  // only record the request -- loop() does the work) -------------------------
  void requestDose(uint16_t ml) { _reqAuto = false; _reqDose = ml; _reqKind = ml ? REQ_DOSE : REQ_STOP; }
  void requestRunSeconds(uint16_t s) { _reqRunS = s; _reqKind = s ? REQ_RUN : REQ_STOP; }
  void stop(HwPumpState why = HW_PUMP_STOPPED_SAFE) {
    if (_running) {
      off();
      _running = false;
      _state = why;
      addDelivered(millis());
      _node.log("pump stopped (%u), %u ml", why, _deliveredMl);
    }
  }

  // A setting written over the air: queued here (the applier runs in the
  // radio callback, where a flash write does not belong) and saved by loop().
  // `slot` is one of HW_PUMP_SLOT_FLOW / MAX_DOSE / MAX_DAY / FLOAT.
  void requestSetting(uint8_t slot, uint16_t v) {
    uint8_t i = slot - HW_PUMP_SLOT_FLOW;
    if (slot == HW_PUMP_SLOT_FLOAT) i = 3;
    else if (slot >= HW_PUMP_SLOT_AUTO_ON && slot <= HW_PUMP_SLOT_AUTO_GAP) i = 4 + (slot - HW_PUMP_SLOT_AUTO_ON);
    if (i > 7) return;
    _pendVal[i] = v;
    _pendMask |= (1 << i);
  }

  // ---- settings -------------------------------------------------------------
  void setFlow(uint16_t mlPerMin)  { _flow = mlPerMin; saveU16("flow", mlPerMin); }
  void setMaxDose(uint16_t ml)     { _maxDose = ml; saveU16("maxdose", ml); }
  void setMaxDay(uint16_t ml)      { _maxDay = ml; saveU16("maxday", ml); }
  void setFloatMode(uint8_t m)     { _floatMode = m > 2 ? 0 : m; Preferences p; p.begin(NVS_NS, false); p.putUChar("float", _floatMode); p.end(); }

  uint16_t flow() const     { return _flow; }
  uint16_t maxDose() const  { return _maxDose; }
  uint16_t maxDay() const   { return _maxDay; }
  uint8_t  floatMode() const { return _floatMode; }
  uint16_t dayMl() const    { return _dayMl + (_running ? runningMl(millis()) : 0); }
  uint8_t  state() const    { return _state; }
  bool     running() const  { return _running; }
  uint16_t lastDoseMl() const { return _deliveredMl; }
  bool     lastDoseWasAuto() const { return _lastDoseAuto; }
  uint8_t  autoOn() const { return _autoOn; }
  uint16_t autoBelowRaw() const { return _belowRaw; }
  uint16_t autoMl() const { return _autoMl; }
  uint16_t autoGapMin() const { return _gapMin; }
  uint8_t  autoState() const { return _autoState; }
  uint16_t minutesSinceAuto() const {
    if (!_gapPending) return 65535;
    uint32_t m = (millis() - _lastAutoAt) / 60000UL;
    return m > 65534 ? 65534 : (uint16_t)m;
  }

  // The sketch saw a watering that never reached the soil: stop watering by
  // itself until a person looks and switches it back on.
  void autoLockout() {
    if (_autoLocked) return;
    _autoLocked = 1;
    Preferences p; p.begin(NVS_NS, false); p.putUChar("alock", 1); p.end();
    _autoState = HW_AUTO_LOCKED;
    _node.log("auto watering locked off");
  }

  // Call from loop() with the latest soil reading. Judges once a minute, and
  // only waters after two dry judgements in a row: one noisy read is not a
  // dry pot.
  void autoLoop(bool soilOk, uint16_t raw) {
    uint32_t now = millis();
    if (_autoCheckAt && (int32_t)(now - _autoCheckAt) < 0) return;
    _autoCheckAt = now + AUTO_CHECK_MS;
    if (!_autoCheckAt) _autoCheckAt = 1;
    if (!_autoOn || !_belowRaw || !_autoMl) { _autoState = HW_AUTO_OFF; _dryCount = 0; return; }
    if (_autoLocked) { _autoState = HW_AUTO_LOCKED; return; }
    if (_running) { _autoState = HW_AUTO_WATERING; return; }
    if (!soilOk || !raw) { _autoState = HW_AUTO_NO_PROBE; _dryCount = 0; return; }
    if (_gapPending && now - _lastAutoAt < (uint32_t)_gapMin * 60000UL) {
      _autoState = HW_AUTO_GAP;
      _dryCount = 0;
      return;
    }
    if (raw < _belowRaw) { _autoState = HW_AUTO_WATCHING; _dryCount = 0; return; }
    if (++_dryCount < 2) { _autoState = HW_AUTO_WATCHING; return; }
    _dryCount = 0;
    _lastAutoAt = now;           // the gap starts now, even if the dose is refused
    _gapPending = true;
    saveRtc();
    _node.log("auto: soil %u >= %u, watering %u ml", raw, _belowRaw, _autoMl);
    _reqAuto = true;
    _reqDose = _autoMl;
    _reqKind = REQ_DOSE;
    _autoState = HW_AUTO_WATERING;
  }
  uint32_t lastDoseEndedAt() const { return _endedAt; }

  // 0 empty, 1 ok, 2 no float switch configured.
  uint8_t reservoir() const {
    if (_floatPin < 0 || _floatMode == 0) return 2;
    bool closed = digitalRead(_floatPin) == LOW;          // switch to GND, pull-up
    bool water = (_floatMode == 1) ? closed : !closed;
    return water ? 1 : 0;
  }

  void loop() {
    uint32_t now = millis();
    if (now - _winStart >= DAY_MS) {                      // rolling window ends
      _winStart = now;
      _dayMl = 0;
    }
    uint8_t m = _pendMask;
    if (m) {
      _pendMask = 0;
      if (m & 1) { setFlow(_pendVal[0]);    _node.log("pump flow %u ml/min", _flow); }
      if (m & 2) { setMaxDose(_pendVal[1]); _node.log("pump dose cap %u ml", _maxDose); }
      if (m & 4) { setMaxDay(_pendVal[2]);  _node.log("pump day cap %u ml", _maxDay); }
      if (m & 8) { setFloatMode((uint8_t)_pendVal[3]); _node.log("pump float mode %u", _floatMode); }
      if (m & 16) {
        _autoOn = _pendVal[4] ? 1 : 0;
        Preferences p; p.begin(NVS_NS, false); p.putUChar("aon", _autoOn);
        if (_autoOn && _autoLocked) { _autoLocked = 0; p.putUChar("alock", 0); }
        p.end();
        _node.log("auto watering %s", _autoOn ? "on" : "off");
      }
      if (m & 32)  { _belowRaw = _pendVal[5]; saveU16("abelow", _belowRaw); _node.log("auto below raw %u", _belowRaw); }
      if (m & 64)  { _autoMl = _pendVal[6];   saveU16("aml", _autoMl);      _node.log("auto amount %u ml", _autoMl); }
      if (m & 128) { _gapMin = _pendVal[7];   saveU16("agap", _gapMin);     _node.log("auto gap %u min", _gapMin); }
    }
    handleRequest(now);
    if (_running) {
      if (reservoir() == 0) {
        stop(HW_PUMP_STOPPED_EMPTY);
        hwErr(_node, HW_E_SUPPLY_EMPTY, HW_PUMP_SLOT_RESERVOIR, "pump: supply ran empty");
      } else if (now - _startedAt >= _runMs) {
        off();
        _running = false;
        addDelivered(now);
        _state = _hitCeiling ? HW_PUMP_STOPPED_TIMEOUT : HW_PUMP_DONE;
        _node.log("pump %s, %u ml", _hitCeiling ? "cut at time limit" : "done", _deliveredMl);
      }
    }
    if (now - _rtcSavedAt >= 1000) saveRtc();
  }

 private:
  static constexpr const char *NVS_NS = "hwpump";
  static const uint32_t DAY_MS = 24UL * 3600 * 1000;
  // Nothing runs longer than this, whatever the calibration says: a flow rate
  // entered ten times too low makes a dose long, never endless.
  static const uint32_t MAX_RUN_MS = 10UL * 60 * 1000;
  static const uint32_t RTC_MAGIC = 0x504d5032;           // "PMP2" (layout 2)
  static const uint32_t AUTO_CHECK_MS = 60000;
  enum { REQ_NONE, REQ_DOSE, REQ_RUN, REQ_STOP };

  struct RtcState { uint32_t magic, dayMl, elapsedMs, autoElapsedMs, autoValid; };
  static RtcState _rtc;

  void handleRequest(uint32_t now) {
    uint8_t kind = _reqKind;
    if (kind == REQ_NONE) return;
    _reqKind = REQ_NONE;
    if (kind == REQ_STOP) { stop(HW_PUMP_STOPPED_SAFE); return; }
    if (_running) { _node.log("pump: busy, request ignored"); return; }

    uint32_t runMs;
    uint16_t ml;
    if (kind == REQ_RUN) {
      runMs = (uint32_t)_reqRunS * 1000;
      ml = _flow ? (uint16_t)min<uint32_t>(65535, (uint32_t)_flow * runMs / 60000) : 0;
    } else {
      ml = _reqDose;
      if (!_flow) {
        _state = HW_PUMP_REFUSED_UNCALIBRATED;
        hwErr(_node, HW_E_OUTPUT_LIMIT, HW_PUMP_SLOT_FLOW, "pump: not calibrated");
        return;
      }
      if (ml > _maxDose) {
        _state = HW_PUMP_REFUSED_LIMIT;
        hwErr(_node, HW_E_OUTPUT_LIMIT, HW_PUMP_SLOT_MAX_DOSE, "pump: %u ml > dose cap %u", ml, _maxDose);
        return;
      }
      runMs = (uint32_t)ml * 60000UL / _flow;
    }
    if (_flow && (uint32_t)_dayMl + ml > _maxDay) {
      _state = HW_PUMP_REFUSED_LIMIT;
      hwErr(_node, HW_E_OUTPUT_LIMIT, HW_PUMP_SLOT_MAX_DAY, "pump: day cap %u (at %u)", _maxDay, _dayMl);
      return;
    }
    if (reservoir() == 0) {
      _state = HW_PUMP_STOPPED_EMPTY;
      hwErr(_node, HW_E_SUPPLY_EMPTY, HW_PUMP_SLOT_RESERVOIR, "pump: supply empty, not started");
      return;
    }
    _hitCeiling = runMs > MAX_RUN_MS;
    if (_hitCeiling) runMs = MAX_RUN_MS;
    _runMs = runMs;
    _startedAt = now;
    _deliveredMl = 0;
    _lastDoseAuto = _reqAuto;
    _reqAuto = false;
    _running = true;
    _state = HW_PUMP_RUNNING;
    digitalWrite(_pin, _activeLow ? LOW : HIGH);
    _node.log("pump on %lu ms (%u ml)", (unsigned long)runMs, ml);
  }

  uint16_t runningMl(uint32_t now) const {
    if (!_flow) return 0;
    uint32_t ms = now - _startedAt;
    return (uint16_t)min<uint32_t>(65535, (uint32_t)_flow * ms / 60000);
  }

  void addDelivered(uint32_t now) {
    _endedAt = now;
    _deliveredMl = runningMl(now);
    _dayMl = (uint16_t)min<uint32_t>(65535, (uint32_t)_dayMl + _deliveredMl);
    saveRtc();
  }

  void saveRtc() {
    _rtc.magic = RTC_MAGIC;
    _rtc.dayMl = _dayMl;
    _rtc.elapsedMs = millis() - _winStart;
    _rtc.autoValid = _gapPending;
    _rtc.autoElapsedMs = millis() - _lastAutoAt;
    _rtcSavedAt = millis();
  }

  void saveU16(const char *k, uint16_t v) {
    Preferences p;
    p.begin(NVS_NS, false);
    p.putUShort(k, v);
    p.end();
  }

  HivewireNode &_node;
  int8_t _pin, _floatPin;
  bool _activeLow;
  uint16_t _flow = 0, _maxDose = 250, _maxDay = 1500;
  uint8_t _floatMode = 0;
  uint16_t _dayMl = 0, _deliveredMl = 0;
  uint32_t _winStart = 0, _startedAt = 0, _runMs = 0, _endedAt = 0, _rtcSavedAt = 0;
  bool _running = false, _hitCeiling = false;
  uint8_t _state = HW_PUMP_IDLE;
  volatile uint8_t _reqKind = REQ_NONE;
  volatile uint8_t _pendMask = 0;
  volatile uint16_t _pendVal[8] = {0, 0, 0, 0, 0, 0, 0, 0};
  // automatic watering
  uint8_t  _autoOn = 0, _autoLocked = 0, _autoState = HW_AUTO_OFF, _dryCount = 0;
  uint16_t _belowRaw = 0, _autoMl = 0, _gapMin = 720;
  uint32_t _lastAutoAt = 0, _autoCheckAt = 0;
  bool     _gapPending = false, _lastDoseAuto = false;
  volatile bool _reqAuto = false;
  volatile uint16_t _reqDose = 0, _reqRunS = 0;
};

// One pump per sketch: RTC memory survives restarts but not a power loss.
RTC_NOINIT_ATTR HivewirePump::RtcState HivewirePump::_rtc;
