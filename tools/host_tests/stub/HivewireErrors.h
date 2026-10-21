#pragma once
// Stand-in for src/HivewireErrors.h: records the last error raised.
#include "Hivewire.h"
#define HW_E_OUTPUT_LIMIT 306
#define HW_E_SUPPLY_EMPTY 307
extern unsigned g_lastErr, g_lastErrSubject;
inline void hwErr(HivewireNode &, unsigned code, unsigned subject = 0, const char * = nullptr, ...) { g_lastErr = code; g_lastErrSubject = subject; }
