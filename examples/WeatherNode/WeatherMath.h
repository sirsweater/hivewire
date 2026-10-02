/*
 * WeatherMath.h -- the arithmetic behind WeatherNode, kept apart from the pins
 * so it reads (and can be checked) on its own.
 *
 * SparkFun Weather Meter Kit (SEN-15901) constants, from its datasheet:
 *   anemometer  one switch closure per second = 2.4 km/h
 *   rain gauge  one bucket tip = 0.2794 mm
 *   wind vane   16 positions, each a different resistance to ground; with a
 *               10 k pull-up to 3.3 V each gives its own voltage
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once
#include <stdint.h>
#include <math.h>

namespace wx {

static const float KMH_PER_HZ = 2.4f;
static const float MM_PER_TIP = 0.2794f;

// ---- wind: one bucket per second ------------------------------------------
//
// Average over AVG_S, gust = the strongest 3-second average in the last
// GUST_WINDOW_S. That is how weather services report them (2-minute mean,
// 3-second gust), so the numbers compare with a forecast's.
static const int AVG_S = 120;
static const int GUST_WINDOW_S = 600;
static const int GUST_S = 3;

struct Wind {
  uint8_t pulses[GUST_WINDOW_S] = {0};   // closures per second, newest at head-1
  uint8_t sector[AVG_S];                 // vane sector 0-15 per second, 255 = unknown
  int head = 0, filled = 0;

  Wind() { for (int i = 0; i < AVG_S; i++) sector[i] = 255; }

  void push(uint16_t closures, uint8_t sec) {
    pulses[head] = closures > 255 ? 255 : (uint8_t)closures;
    sector[head % AVG_S] = sec;
    head = (head + 1) % GUST_WINDOW_S;
    if (filled < GUST_WINDOW_S) filled++;
  }
  int at(int ago) const {               // ago = 0 is the newest second
    return pulses[(head - 1 - ago + 2 * GUST_WINDOW_S) % GUST_WINDOW_S];
  }

  // km/h x 10
  uint16_t avg10() const {
    int n = filled < AVG_S ? filled : AVG_S;
    if (!n) return 0;
    uint32_t sum = 0;
    for (int i = 0; i < n; i++) sum += at(i);
    return (uint16_t)lroundf(sum * KMH_PER_HZ * 10.0f / n);
  }
  uint16_t gust10() const {
    int n = filled;
    if (n < GUST_S) return avg10();
    uint32_t best = 0;
    for (int i = 0; i + GUST_S <= n; i++) {
      uint32_t s = 0;
      for (int k = 0; k < GUST_S; k++) s += at(i + k);
      if (s > best) best = s;
    }
    return (uint16_t)lroundf(best * KMH_PER_HZ * 10.0f / GUST_S);
  }
  // Mean direction in degrees, 0-359; 0xFFFF when calm or the vane is unread.
  // A VECTOR mean: NW and NE average to N, not to S as a plain mean would.
  // Only seconds with wind count -- a vane in still air points anywhere.
  uint16_t dirDeg() const {
    int n = filled < AVG_S ? filled : AVG_S;
    float x = 0, y = 0;
    int used = 0;
    for (int i = 0; i < n; i++) {
      int idx = (head - 1 - i + 2 * GUST_WINDOW_S) % GUST_WINDOW_S;
      uint8_t s = sector[idx % AVG_S];
      if (s > 15 || pulses[idx] == 0) continue;
      float a = s * 22.5f * (float)M_PI / 180.0f;
      x += sinf(a); y += cosf(a); used++;
    }
    if (!used || (fabsf(x) < 1e-3f && fabsf(y) < 1e-3f)) return 0xFFFF;
    float deg = atan2f(x, y) * 180.0f / (float)M_PI;
    if (deg < 0) deg += 360.0f;
    uint16_t d = (uint16_t)lroundf(deg);
    return d >= 360 ? 0 : d;
  }
};

// ---- vane: voltage -> sector -----------------------------------------------
// Resistance at each of the 16 positions (datasheet), clockwise from north.
static const float VANE_OHMS[16] = {
  33000, 6570, 8200, 891, 1000, 688, 2200, 1410,
  3900, 3140, 16000, 14120, 120000, 42120, 64900, 21880,
};

// Expected millivolts at each position for a pull-up of `pullup` ohms to
// `supplyMv`. Computed rather than tabulated so a different resistor, or a
// measured supply, is one argument away.
inline float vaneExpectedMv(int sector, float pullup = 10000.0f, float supplyMv = 3300.0f) {
  float r = VANE_OHMS[sector];
  return supplyMv * r / (r + pullup);
}

// Nearest position, or 255 when the reading is not a vane at all: near the
// rail (open circuit -- nothing pulls it down), near zero (shorted), or far
// from every position. `distMv` returns how far from the match it was.
inline uint8_t vaneSector(float mv, float *distMv = nullptr, float pullup = 10000.0f,
                          float supplyMv = 3300.0f) {
  if (mv > supplyMv - 120.0f || mv < 80.0f) { if (distMv) *distMv = -1; return 255; }
  int best = -1;
  float bestD = 1e9f;
  for (int s = 0; s < 16; s++) {
    float d = fabsf(mv - vaneExpectedMv(s, pullup, supplyMv));
    if (d < bestD) { bestD = d; best = s; }
  }
  if (distMv) *distMv = bestD;
  // Neighbouring positions sit ~30 mV apart at the low end; 150 mV off any of
  // them is not a vane reading worth trusting.
  return bestD <= 150.0f ? (uint8_t)best : 255;
}

// ---- rain: tips per minute for the last hour --------------------------------
struct Rain {
  uint16_t perMin[60] = {0};
  int head = 0;
  void rollMinute(uint16_t tipsThisMinute) {
    perMin[head] = tipsThisMinute;
    head = (head + 1) % 60;
  }
  // mm x 100 in the last 60 minutes (the current, unfinished minute added by the caller)
  uint16_t lastHour100(uint16_t thisMinute = 0) const {
    uint32_t t = thisMinute;
    for (int i = 0; i < 60; i++) t += perMin[i];
    float mm100 = t * MM_PER_TIP * 100.0f;
    return mm100 > 65535 ? 65535 : (uint16_t)lroundf(mm100);
  }
};

}  // namespace wx
