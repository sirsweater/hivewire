#pragma once
#include <map>
#include <string>
extern std::map<std::string, unsigned> g_nvs;
struct Preferences {
  std::string ns;
  bool begin(const char *n, bool) { ns = n; return true; }
  void end() {}
  unsigned char getUChar(const char *k, unsigned char d) { auto i = g_nvs.find(ns + k); return i == g_nvs.end() ? d : i->second; }
  unsigned short getUShort(const char *k, unsigned short d) { auto i = g_nvs.find(ns + k); return i == g_nvs.end() ? d : i->second; }
  void putUChar(const char *k, unsigned char v) { g_nvs[ns + k] = v; }
  void putUShort(const char *k, unsigned short v) { g_nvs[ns + k] = v; }
};
