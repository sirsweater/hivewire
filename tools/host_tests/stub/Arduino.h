#pragma once
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <cstdarg>
#include <map>
#include <string>
#define OUTPUT 1
#define HIGH 1
#define LOW 0
#define INPUT 0
#define INPUT_PULLUP 2
#include <algorithm>
using std::min; using std::max;
extern uint32_t g_millis; extern int g_pin[64];
inline uint32_t millis() { return g_millis; }
inline void pinMode(int, int) {}
inline void digitalWrite(int p, int v) { g_pin[p] = v; }
inline int digitalRead(int p) { return g_pin[p]; }
