// HivewireProvision.h -- give a freshly flashed board its identity over USB.
//
// Why: building one image per node number made flashing a job that needed a
// compiler and someone who knew the flags. With this, each sketch is built ONCE
// with node id 0, and the flasher tells the board who it is afterwards over the
// USB serial port; the board stores that in NVS and reboots as that node.
//
// Id 0 is safe to mean "unassigned" because it can never be a real node: it is
// HIVEWIRE_TARGET_ALL on the wire. An unassigned board NEVER joins the swarm --
// a half-finished flash must not show up as a second node 11 -- it only checks
// its radio and waits.
//
// The serial protocol is line based, 115200 baud, and deliberately tiny:
//
//   board -> host   HWID family=<name> id=<n|unassigned> mac=<aa:bb:..>
//                   HWRADIO networks=<n> best=<dBm|none> [dead]
//   host -> board   id?            -> repeats the HWID line
//                   setid <1-254>  -> HWID-SET <n>, then reboots as that node
//
// A board that hears no networks at all has a dead radio (seen in practice: a
// C6 whose sensors all worked and whose radio heard nothing on any channel).
// That shows up here, at flash time, instead of as a node that never joins.
//
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_mac.h>

namespace hwprov {

inline void printId(const char *family, uint8_t id) {
  // The chip's base MAC: the same one in the USB serial device's name, which is
  // how a flasher knows it is talking to the board it just wrote.
  // 8 bytes of room: on the C6 some MAC calls return the 64-bit (EUI-64)
  // form, and a 6-byte buffer would be overrun. The Wi-Fi station MAC is the
  // 6-byte base MAC on this chip.
  uint8_t m[8] = {0};
  esp_read_mac(m, ESP_MAC_WIFI_STA);
  if (id)
    Serial.printf("HWID family=%s id=%u mac=%02x:%02x:%02x:%02x:%02x:%02x\n", family, id,
                  m[0], m[1], m[2], m[3], m[4], m[5]);
  else
    Serial.printf("HWID family=%s id=unassigned mac=%02x:%02x:%02x:%02x:%02x:%02x\n", family,
                  m[0], m[1], m[2], m[3], m[4], m[5]);
}

// One active scan across every 2.4 GHz channel. Run it BEFORE the swarm radio
// starts: a scan hops channels, which would drop swarm traffic if it ran later.
// Returns the number of networks heard.
inline int radioSelfTest() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  int n = WiFi.scanNetworks(false, true);
  int best = -128;
  for (int i = 0; i < n; i++) if (WiFi.RSSI(i) > best) best = WiFi.RSSI(i);
  WiFi.scanDelete();
  if (n < 0) n = 0;
  if (n > 0) Serial.printf("HWRADIO networks=%d best=%d\n", n, best);
  else       Serial.printf("HWRADIO networks=0 best=none dead\n");
  return n;
}

// Reads one line from Serial without blocking. Returns true when `out` holds a
// complete line.
inline bool readLine(String &buf, String &out) {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') { out = buf; buf = ""; out.trim(); return out.length() > 0; }
    if (buf.length() < 40) buf += c;
  }
  return false;
}

// Handle one command line. Stores the id under `ns`/"id" and reboots on setid.
inline void handle(const String &line, const char *ns, const char *family, uint8_t id) {
  if (line == "id?") { printId(family, id); return; }
  if (line.startsWith("setid ")) {
    long v = line.substring(6).toInt();
    if (v < 1 || v > 254) { Serial.println("HWID-ERR id must be 1-254"); return; }
    Preferences p;
    p.begin(ns, false);
    p.putUChar("id", (uint8_t)v);
    p.end();
    Serial.printf("HWID-SET %ld\n", v);
    Serial.flush();
    delay(100);
    ESP.restart();
  }
}

// Called from loop() on a provisioned node: answers id? and accepts a new id,
// so a node can be renumbered on the bench without reflashing.
inline void poll(const char *ns, const char *family, uint8_t id) {
  static String buf;
  String line;
  if (readLine(buf, line)) handle(line, ns, family, id);
}

// Called from setup() when the stored id is 0. Never returns: it announces the
// board, tests the radio, and waits for setid (which reboots). The HWID line is
// repeated every few seconds so a flasher that opened the port late still sees
// it.
[[noreturn]] inline void waitForId(const char *ns, const char *family) {
  delay(300);
  printId(family, 0);
  radioSelfTest();
  String buf, line;
  uint32_t last = millis();
  for (;;) {
    if (readLine(buf, line)) handle(line, ns, family, 0);
    if (millis() - last > 3000) { last = millis(); printId(family, 0); }
    delay(5);
  }
}

}  // namespace hwprov
