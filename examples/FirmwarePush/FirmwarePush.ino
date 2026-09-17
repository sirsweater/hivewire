/*
 * Hivewire -- FirmwarePush
 *
 * A hive whose only job is to put a firmware image into the swarm, taking the
 * bytes from whatever is plugged into it. No WiFi, no internet, no server.
 *
 * That is the point. The hive will usually have internet and usually should use
 * it, but "usually" is not a foundation to build a house network on. A laptop
 * and a cable must always work, because it is the path that depends on nothing.
 *
 * Host protocol, deliberately trivial so any language can speak it:
 *
 *     >  FW <imageLen> <crc32>\n        host announces
 *     <  READY\n                        hive is listening
 *     <  MORE\n                         hive wants the next SUB-CHUNK
 *     >  <up to SERIAL_SUBCHUNK bytes>  host sends exactly that much
 *     <  DONE\n                         every window sent
 *
 * MORE repeats many times per window, not once -- a host that answers a single
 * MORE with a whole ~12KB window overruns what the USB CDC link can actually
 * absorb as one burst, and the loss is silent: no error, just fewer bytes than
 * were sent. See SERIAL_SUBCHUNK below for what that cost in practice.
 *
 * The hive never stores more than one window, so image size is bounded by the
 * NODE's partition, not by anything here.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <Hivewire.h>
#include <HivewireFirmware.h>

static const uint8_t HIVE_ID = 1;

HivewireCoordinator coord;
HivewireFwSender    sender(coord);

static const HwWritableSlot WRITABLE[] = { { 3, HW_U8 } };

// Pull straight from USB. The sender only ever reads forward, so a stream works
// as well as a file -- repairs come out of the window it already holds in RAM.
static uint32_t g_remaining = 0;

// Stays well under the RX ring buffer, and under whatever the USB CDC path can
// actually absorb in one burst -- which, measured, is less than either. A host
// that answers one "MORE" with a full 12288-byte window silently lost ~1400
// bytes of it: feedFromSerial() returned 10909, not the 12288 the host wrote,
// with no error anywhere. Because that count came back UNDER one window, the
// sender read it as "the final short window" and closed the transfer after
// window 0 of a four-window image, every single time -- the actual cause of
// every stall this feature produced tonight. Bigger RX buffers do not fix a
// burst the USB stack itself cannot deliver as one unit; asking for the data
// in pieces small enough to always arrive intact does.
static const size_t SERIAL_SUBCHUNK = 1024;

static size_t feedFromSerial(uint8_t *buf, size_t want) {
  if (!g_remaining) return 0;
  if (want > g_remaining) want = g_remaining;

  size_t total = 0;
  while (total < want) {
    size_t ask = want - total;
    if (ask > SERIAL_SUBCHUNK) ask = SERIAL_SUBCHUNK;

    Serial.println("MORE");          // ask for ONE sub-chunk; never let the host
                                      // race ahead of what this side can absorb
    size_t got = 0;
    uint32_t idle = millis();
    while (got < ask) {
      int n = Serial.available();
      if (n > 0) {
        size_t take = (size_t)n;
        if (take > ask - got) take = ask - got;
        got += Serial.readBytes(buf + total + got, take);
        idle = millis();
      } else {
        if (millis() - idle > 4000) break;    // host went away
        delay(1);
      }
    }
    total += got;
    if (got < ask) break;            // host stalled mid-subchunk; stop honestly
  }
  g_remaining -= total;
  return total;
}

void setup() {
  // Headroom on top of the handshake. The default is 256 bytes, which is far
  // less than one window and was exactly the size of the truncation this used
  // to produce.
  Serial.setRxBufferSize(8192);
  Serial.begin(115200);
  delay(1500);
  if (!coord.begin(HIVE_ID, WRITABLE, 1)) {
    Serial.println("hivewire: begin failed");
    ESP.restart();
  }
  coord.onRaw([](const uint8_t *d, int n) { sender.ingest(d, n); });
  // A node records what happened to it in its own ring, not on a console
  // nobody can reach. Without a way to read that back, a failed push is
  // indistinguishable from one that never arrived.
  coord.onNodeLog([](uint8_t id, const char *line) {
    Serial.printf("N%u | %s\n", id, line);
  });
  Serial.println("FirmwarePush ready. FW <len> <crc32> | LOG <id>");
}

void loop() {
  coord.loop();
  sender.loop();

  static bool announced = false;
  if (sender.active()) {
    if (!announced) announced = true;
    static uint16_t last = 0xFFFF;
    if (sender.window() != last) {
      last = sender.window();
      Serial.printf("window %u/%u\n", last, sender.windows());
    }
    return;
  }
  if (announced) { announced = false; Serial.println("DONE"); }

  // "FW <len> <crc>" on one line, then the raw image.
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.startsWith("LOG ")) {
    coord.requestLog((uint8_t)atoi(line.c_str() + 4));
    return;
  }
  if (!line.startsWith("FW ")) return;

  uint32_t len = 0, crc = 0;
  if (sscanf(line.c_str() + 3, "%lu %lu", (unsigned long *)&len,
             (unsigned long *)&crc) != 2 || !len) {
    Serial.println("ERR usage: FW <len> <crc32>");
    return;
  }
  g_remaining = len;
  Serial.println("READY");
  // Target every node at once. ESP-NOW is broadcast, so one pass feeds the
  // whole swarm and a chunk two nodes both missed is resent once, not twice.
  sender.begin(HIVEWIRE_TARGET_ALL, len, crc, feedFromSerial);
}
