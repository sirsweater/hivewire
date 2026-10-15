#pragma once
struct HivewireNode { void log(const char *f, ...) { va_list a; va_start(a, f); vprintf(f, a); va_end(a); printf("\n"); } };
