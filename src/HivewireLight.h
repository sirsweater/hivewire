// HivewireLight.h -- a grow light on the swarm, switched on a daily schedule.
//
// Header-only and opt-in, like HivewirePump.h. Used by examples/SoilNode built
// with -DHW_WITH_PUMP=1 -DHW_WITH_LIGHT=1: the GrowNode family (soil sensor,
// pump and light on one board).
//
// The switch is anything a GPIO can drive: a logic-level MOSFET module in the
// ground wire of a USB or 12 V lamp, or for a mains lamp a pre-built relay box
// with a low-voltage control input (never mains wiring next to a drip line).
//
// Rules, and why they differ from the pump's:
//
//   - The schedule runs on the NODE, on its own clock. A plant's day must not
//     end because the Pi rebooted or the gateway was unplugged, so the light
//     is deliberately NOT switched off by onSafe(): a lamp left on cannot do
//     what a pump left on does.
//   - The node has no wall clock. The hive's host writes the local minute of
//     the day into slot 60 and the node counts on from there with millis().
//     The C6's crystal is good to ~20 ppm: under a second a day, so the host
//     re-sending every few hours is plenty, and a daylight-saving change is
//     picked up on the next one.
//   - Until a clock arrives, a light on a schedule stays OFF and slot 60 reads
//     65535 -- the host sees that and sends the time at once. A restart that
//     is not a power loss (crash, watchdog, update) keeps the clock in RTC
//     memory, like the pump's 24 h total, so the light does not blink off
//     after every reboot.
//   - Settings are saved in NVS and survive power loss; the override modes
//     (always on / always off) are settings too, so a lamp switched off for
//     maintenance stays off through a restart.
//
// Slot convention (ids clear of the pump's 40-55):
//
//   56  light            out      0 off, 1 on: what the switch is doing now
//   57  light mode       setting  0 always off, 1 always on, 2 on the schedule
//   58  lights on at     setting  minute of the day, 0-1439 (360 = 06:00)
//   59  lights off at    setting  minute of the day (1320 = 22:00). On later
//                                 than off spans midnight; on == off is never on
//   60  clock            in/out   write: the local minute of the day now;
//                                 reads the node's own count (65535 = not set)
//
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <esp_attr.h>
#include "Hivewire.h"

#define HW_LIGHT_SLOT_STATE 56
#define HW_LIGHT_SLOT_MODE  57
#define HW_LIGHT_SLOT_ON_AT 58
#define HW_LIGHT_SLOT_OFF_AT 59
#define HW_LIGHT_SLOT_CLOCK 60

#define HW_LIGHT_CLOCK_UNSET 65535

enum HwLightMode : uint8_t {
  HW_LIGHT_OFF = 0,
  HW_LIGHT_ON = 1,
  HW_LIGHT_SCHEDULE = 2,
};

// Pure schedule rule, kept apart so it can be tested on a PC: is the light on
// at minute `m` of the day (0-1439) for a window from `onAt` to `offAt`?
inline bool hwLightWindow(uint16_t m, uint16_t onAt, uint16_t offAt) {
  if (onAt == offAt) return false;
  if (onAt < offAt) return m >= onAt && m < offAt;
  return m >= onAt || m < offAt;               // spans midnight
}

class HivewireLight {
 public:
  // pin: drives the switch; high = on unless activeLow (many relay boards).
  HivewireLight(HivewireNode &node, int8_t pin, bool activeLow = false)
      : _node(node), _pin(pin), _activeLow(activeLow) {}

  // Call early in setup(): until then the pin floats.
  void off() {
    pinMode(_pin, OUTPUT);
    digitalWrite(_pin, _activeLow ? HIGH : LOW);
    _on = false;
  }

  void begin() {
    off();
    Preferences p;
    p.begin(NVS_NS, true);
    _mode = p.getUChar("mode", HW_LIGHT_SCHEDULE);
    _onAt = p.getUShort("on", 360);
    _offAt = p.getUShort("off", 1320);
    p.end();
    if (_mode > HW_LIGHT_SCHEDULE) _mode = HW_LIGHT_SCHEDULE;
    if (_onAt > 1439) _onAt = 360;
    if (_offAt > 1439) _offAt = 1320;
    // A restart that kept RTC memory: carry on counting from the saved minute.
    // The reboot itself takes about a second, which the count cannot see.
    if (_rtc.magic == RTC_MAGIC && _rtc.minute < 1440) {
      _clockMin = _rtc.minute;
      _clockSetAt = millis() - _rtc.msIntoMinute;
      _clockKnown = true;
      _node.log("light: clock %02u:%02u kept through restart", _clockMin / 60, _clockMin % 60);
    }
    apply(millis());
  }

  // ---- from slot appliers (radio callback): record only, loop() acts --------
  void requestSetting(uint8_t slot, uint16_t v) {
    if (slot < HW_LIGHT_SLOT_MODE || slot > HW_LIGHT_SLOT_CLOCK) return;
    uint8_t i = slot - HW_LIGHT_SLOT_MODE;
    _pendVal[i] = v;
    _pendMask |= (1 << i);
  }

  // ---- readings -------------------------------------------------------------
  uint8_t  isOn() const   { return _on ? 1 : 0; }
  uint8_t  mode() const   { return _mode; }
  uint16_t onAt() const   { return _onAt; }
  uint16_t offAt() const  { return _offAt; }
  uint16_t clock() const  { return _clockKnown ? minuteNow(millis()) : HW_LIGHT_CLOCK_UNSET; }

  void loop() {
    uint32_t now = millis();
    uint8_t m = _pendMask;
    if (m) {
      _pendMask = 0;
      if (m & 1) {
        uint8_t v = _pendVal[0] > HW_LIGHT_SCHEDULE ? HW_LIGHT_SCHEDULE : (uint8_t)_pendVal[0];
        _mode = v;
        Preferences p; p.begin(NVS_NS, false); p.putUChar("mode", _mode); p.end();
        _node.log("light mode %u", _mode);
      }
      if ((m & 2) && _pendVal[1] <= 1439) {
        _onAt = _pendVal[1];
        saveU16("on", _onAt);
        _node.log("light on at %02u:%02u", _onAt / 60, _onAt % 60);
      }
      if ((m & 4) && _pendVal[2] <= 1439) {
        _offAt = _pendVal[2];
        saveU16("off", _offAt);
        _node.log("light off at %02u:%02u", _offAt / 60, _offAt % 60);
      }
      if ((m & 8) && _pendVal[3] <= 1439) {
        bool was = _clockKnown;
        uint16_t before = was ? minuteNow(now) : 0;
        _clockMin = _pendVal[3];
        _clockSetAt = now;
        _clockKnown = true;
        // Only worth a log line when it was unset or moved: the host re-sends
        // every few hours and an unchanged clock is not news.
        int d = (int)_clockMin - (int)before;
        if (!was || (d > 1 && d < 1439) || (d < -1 && d > -1439))
          _node.log("light: clock set %02u:%02u", _clockMin / 60, _clockMin % 60);
      }
    }
    apply(now);
    if (now - _rtcSavedAt >= 1000) saveRtc(now);
  }

 private:
  static constexpr const char *NVS_NS = "hwlight";
  static constexpr uint32_t RTC_MAGIC = 0x4C495431;   // "LIT1"

  struct RtcState {
    uint32_t magic;
    uint16_t minute;
    uint32_t msIntoMinute;
  };
  static RtcState _rtc;

  uint16_t minuteNow(uint32_t now) const {
    uint32_t mins = (now - _clockSetAt) / 60000UL;
    return (uint16_t)((_clockMin + mins) % 1440);
  }

  void apply(uint32_t now) {
    bool want = false;
    if (_mode == HW_LIGHT_ON) want = true;
    else if (_mode == HW_LIGHT_SCHEDULE && _clockKnown) want = hwLightWindow(minuteNow(now), _onAt, _offAt);
    if (want == _on && _applied) return;
    _applied = true;
    _on = want;
    digitalWrite(_pin, (want != _activeLow) ? HIGH : LOW);
    _node.log("light %s", want ? "on" : "off");
  }

  void saveRtc(uint32_t now) {
    _rtcSavedAt = now;
    if (!_clockKnown) { _rtc.magic = 0; return; }
    uint32_t el = now - _clockSetAt;
    _rtc.minute = (uint16_t)((_clockMin + el / 60000UL) % 1440);
    _rtc.msIntoMinute = el % 60000UL;
    _rtc.magic = RTC_MAGIC;
  }

  void saveU16(const char *k, uint16_t v) {
    Preferences p;
    p.begin(NVS_NS, false);
    p.putUShort(k, v);
    p.end();
  }

  HivewireNode &_node;
  int8_t _pin;
  bool _activeLow;
  bool _on = false;
  bool _applied = false;
  uint8_t _mode = HW_LIGHT_SCHEDULE;
  uint16_t _onAt = 360, _offAt = 1320;
  bool _clockKnown = false;
  uint16_t _clockMin = 0;
  uint32_t _clockSetAt = 0;
  uint32_t _rtcSavedAt = 0;
  volatile uint8_t _pendMask = 0;
  volatile uint16_t _pendVal[4] = {0, 0, 0, 0};
};

RTC_NOINIT_ATTR HivewireLight::RtcState HivewireLight::_rtc;
