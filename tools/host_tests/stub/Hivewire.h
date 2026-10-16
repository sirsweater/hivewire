#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdarg>
struct HivewireNode {
  uint8_t id() const { return 1; } int8_t hiveRssi() const { return 0; } uint32_t hiveRssiAgeMs() const { return 0; }
  void log(const char *f, ...) { va_list a; va_start(a, f); vprintf(f, a); va_end(a); printf("\n"); } };
