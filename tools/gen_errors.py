#!/usr/bin/env python3
"""Generate the error-code artefacts from errors/codes.json.

    python tools/gen_errors.py          # write all three
    python tools/gen_errors.py --check  # exit 1 if any generated file is stale

Writes:
  src/HivewireErrors.h          the codes as an enum, plus the small recorder
                                every node and gateway uses to raise them
  ERRORS.md                     what each code means and what to do about it
  tools/hive_admin/errors.json  the lookup the admin page shows codes with

Refuses a registry with a duplicate or out-of-range code, a missing field, or a
library code inside the range reserved for applications.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "errors", "codes.json")
HEADER = os.path.join(REPO, "src", "HivewireErrors.h")
DOC = os.path.join(REPO, "ERRORS.md")
ADMIN = os.path.join(HERE, "hive_admin", "errors.json")

FIELDS = ("code", "name", "area", "severity", "by", "title", "cause", "fix")
SEVERITIES = ("info", "warning", "error")
AREAS = [("radio", "Radio"), ("firmware", "Firmware and updates"),
         ("peripherals", "Peripherals (raised by applications about any slot)"),
         ("provisioning", "Provisioning"), ("gateway", "Gateway and uplinks"),
         ("host", "Host (the machine running the admin)")]

HEADER_TAIL = r'''
// ---------------------------------------------------------------------------
// Raising a code
// ---------------------------------------------------------------------------
// A node keeps the most recent error and a count since boot, and publishes
// them in two slots by convention (see HW_ERR_SLOT_*), so the host sees them
// on its normal poll without asking. Each raise also goes into the node's log
// as "E<code>/<subject> <detail>", which is what `log <id>` fetches.
//
// `subject` says WHICH thing the code is about, so one generic code serves any
// application: for a peripheral code it is the slot id the device feeds; for
// an uplink code, the uplink's index; 0 when there is nothing more specific.
//
// Codes 1000-1999 are the application's own (HW_E_APP_FIRST..LAST). The
// library never uses them; describe them in the node type's kinds.json entry
// so the admin can explain them.

#define HW_E_APP_FIRST 1000
#define HW_E_APP_LAST  1999

// Slot ids the bundled sketches use for the error pair. Applications that
// raise codes should publish these too, with the samplers below.
#define HW_ERR_SLOT_LAST  26   // HW_U32: code | subject << 16
#define HW_ERR_SLOT_COUNT 27   // HW_U16: errors raised since boot

struct HwErrState {
  uint16_t code = 0;        // most recent, 0 = none since boot
  uint8_t  subject = 0;
  uint16_t count = 0;       // since boot, saturating
  uint32_t atMs = 0;        // millis() when it was raised
};

inline HwErrState &hwErrState() {
  static HwErrState s;
  return s;
}

// Record an error. Safe before node.begin(): the log line is skipped until the
// node has an id, but the slots still carry the code once the node joins.
inline void hwErr(HivewireNode &node, uint16_t code, uint8_t subject = 0,
                  const char *fmt = nullptr, ...) {
  HwErrState &s = hwErrState();
  s.code = code;
  s.subject = subject;
  if (s.count < 0xFFFF) s.count++;
  s.atMs = millis();
  char detail[40] = "";
  if (fmt) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(detail, sizeof(detail), fmt, ap);
    va_end(ap);
  }
  if (node.id()) {
    if (subject) node.log("E%u/%u %s", code, subject, detail);
    else         node.log("E%u %s", code, detail);
  }
}

// Samplers for the two error slots:
//   { HW_ERR_SLOT_LAST,  HW_U32, HW_DIR_OUT, 30000, 900000, 1, 0, 0, hwErrSlotLast,  nullptr },
//   { HW_ERR_SLOT_COUNT, HW_U16, HW_DIR_OUT, 30000, 900000, 1, 0, 0, hwErrSlotCount, nullptr },
inline void hwErrSlotLast(void *out) {
  const HwErrState &s = hwErrState();
  uint32_t v = (uint32_t)s.code | ((uint32_t)s.subject << 16);
  memcpy(out, &v, 4);
}
inline void hwErrSlotCount(void *out) {
  uint16_t v = hwErrState().count;
  memcpy(out, &v, 2);
}

// WEAK_LINK: the hive's own beacons, heard directly, are arriving weaker than
// `dbm`. Call from loop(); it samples once a minute.
//
// Two things the first version got wrong, both found in a soak. It judged the
// LAST packet from anyone, so a node beside the hive (-69 dBm median) logged
// "weak link -91 dBm" whenever a far neighbour happened to speak last. And it
// logged every 30 minutes while weak, so within hours the 8-line log ring held
// nothing else -- a diagnostic crowding out every other diagnostic.
//
// Now: the hive's direct beacons only (a node that hears the hive only through
// relays says nothing -- that is relaying working, not a weak link), smoothed,
// logged once on the way down and once on the way back up, and repeated while
// still weak only every `remindMs`. The count in slot 27 still says how often.
inline void hwErrCheckLink(HivewireNode &node, int8_t dbm = -90,
                           uint32_t remindMs = 6UL * 3600 * 1000) {
  static uint32_t lastSample = 0, raisedAt = 0;
  static int16_t avg10 = 0;              // dBm x10, smoothed; 0 = no sample yet
  static bool weak = false;
  uint32_t now = millis();
  if (lastSample && now - lastSample < 60000UL) return;
  lastSample = now;
  int8_t r = node.hiveRssi();
  if (r == 0 || node.hiveRssiAgeMs() > 10UL * 60 * 1000) return;   // relayed or not yet heard
  avg10 = avg10 ? (int16_t)((avg10 * 3 + r * 10) / 4) : (int16_t)(r * 10);
  int avg = avg10 / 10;
  if (!weak && avg < dbm) {
    weak = true;
    raisedAt = now;
    hwErr(node, HW_E_WEAK_LINK, 0, "weak link %d dBm", avg);
  } else if (weak && avg > dbm + 3) {    // 3 dB of hysteresis: no flapping at the line
    weak = false;
    node.log("link ok %d dBm", avg);
  } else if (weak && now - raisedAt >= remindMs) {
    raisedAt = now;
    hwErr(node, HW_E_WEAK_LINK, 0, "still weak %d dBm", avg);
  }
}
'''


def load():
    reg = json.load(open(SRC, encoding="utf-8"))
    lo, hi = reg["app_range"]
    seen, names = set(), set()
    for c in reg["codes"]:
        missing = [f for f in FIELDS if not c.get(f)]
        if missing:
            sys.exit("code %s is missing %s" % (c.get("code"), ", ".join(missing)))
        if c["code"] in seen or c["name"] in names:
            sys.exit("duplicate code or name: %s %s" % (c["code"], c["name"]))
        if not 100 <= c["code"] <= 999 or lo <= c["code"] <= hi:
            sys.exit("library code %s must be 100-999 (and never %d-%d)" % (c["code"], lo, hi))
        if c["severity"] not in SEVERITIES:
            sys.exit("code %s: severity must be one of %s" % (c["code"], SEVERITIES))
        if c["area"] not in dict(AREAS):
            sys.exit("code %s: unknown area %s" % (c["code"], c["area"]))
        seen.add(c["code"])
        names.add(c["name"])
    reg["codes"].sort(key=lambda c: c["code"])
    return reg


def header(reg):
    out = ["// HivewireErrors.h -- Hivewire's error codes and the recorder that raises them.",
           "//",
           "// GENERATED by tools/gen_errors.py from errors/codes.json. Edit the JSON and",
           "// regenerate; do not edit this file. What each code means: ERRORS.md.",
           "//",
           "// SPDX-License-Identifier: Apache-2.0",
           "#pragma once",
           "",
           "#include <Arduino.h>",
           "#include <stdarg.h>",
           "#include \"Hivewire.h\"",
           "",
           "enum HwErrCode : uint16_t {",
           "  HW_E_NONE = 0,"]
    for c in reg["codes"]:
        out.append("  HW_E_%s = %d,%s// %s" % (c["name"], c["code"],
                                            " " * max(1, 28 - len(c["name"]) - len(str(c["code"]))),
                                            c["title"]))
    out.append("};")
    return "\n".join(out) + "\n" + HEADER_TAIL


def doc(reg):
    lines = ["# Hivewire error codes", "",
             "*Generated from [`errors/codes.json`](errors/codes.json) by "
             "`tools/gen_errors.py`. Edit the JSON, not this file.*", "",
             "Every code has a number that never changes meaning. A code is raised by a",
             "**node**, the **gateway**, or the **host** running the admin page, and may",
             "carry a **subject**: which slot (for a peripheral) or uplink it is about,",
             "shown as `E303/3`. The admin page explains each code where it appears, and",
             "**Report a problem** there collects recent codes into a GitHub issue.", "",
             "Codes **%d–%d** belong to applications built on Hivewire and are never used" % tuple(reg["app_range"]),
             "by the library. An application documents its own in its node type",
             "(`kinds.json` → `errors`).", ""]
    for key, label in AREAS:
        rows = [c for c in reg["codes"] if c["area"] == key]
        if not rows:
            continue
        lines += ["## %s" % label, ""]
        for c in rows:
            lines += ["### E%d `%s` — %s" % (c["code"], c["name"], c["title"]), "",
                      "*%s, raised by the %s.*" % (c["severity"].capitalize(), c["by"]), "",
                      "**Cause.** %s" % c["cause"], "",
                      "**What to do.** %s" % c["fix"], ""]
    return "\n".join(lines)


def admin(reg):
    return json.dumps({"app_range": reg["app_range"],
                       "codes": {str(c["code"]): {k: c[k] for k in FIELDS if k != "code"}
                                 for c in reg["codes"]}}, indent=1) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = ap.parse_args()
    reg = load()
    outputs = {HEADER: header(reg), DOC: doc(reg), ADMIN: admin(reg)}
    stale = []
    for path, text in outputs.items():
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if cur != text:
            stale.append(os.path.relpath(path, REPO))
            if not args.check:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
    if args.check:
        if stale:
            sys.exit("stale: " + ", ".join(stale) + " -- run python tools/gen_errors.py")
        print("error codes up to date (%d codes)" % len(reg["codes"]))
    else:
        print("%d codes; wrote %s" % (len(reg["codes"]), ", ".join(stale) or "nothing (up to date)"))


if __name__ == "__main__":
    main()
