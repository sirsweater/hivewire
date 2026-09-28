#!/usr/bin/env python3
"""Build the generic flash images the Flash page uses.

One image per firmware family, built with node id 0: a board flashed with it
reports itself over USB and waits to be given a number, so the same image
serves every board (see src/HivewireProvision.h).

    python tools/build_images.py                    # both families, into ~/hive_data/firmware
    python tools/build_images.py --out D:/somewhere
    python tools/build_images.py --only SoilNode

Needs arduino-cli with the esp32 core installed. The copy bundled with the
Arduino IDE is found automatically on Windows.

To use the images on the Pi, copy the .merged.bin and .json files into the
Pi's ~/hive_data/firmware/ (the Flash tab lists whatever is there).
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# family -> (sketch folder under examples/, extra build flags). WaterNode is
# the SoilNode sketch with its pump compiled in: its own family, so a pump image
# never lands on a sensor-only board or the other way round.
FAMILIES = {
    "SoilNode":  ("SoilNode", ""),
    "WaterNode": ("SoilNode", "-DHW_WITH_PUMP=1"),
    "PumpNode":  ("PumpNode", ""),
    "RangeNode": ("RangeNode", ""),
}
FQBN = "esp32:esp32:esp32c6:CDCOnBoot=cdc"


def find_cli():
    p = shutil.which("arduino-cli")
    if p:
        return p
    for c in glob.glob(os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs",
                                    "Arduino IDE", "resources", "app", "lib", "backend",
                                    "resources", "arduino-cli.exe")):
        return c
    return None


def commit():
    try:
        return subprocess.check_output(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                                       text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), "hive_data", "firmware"))
    ap.add_argument("--only", choices=list(FAMILIES))
    ap.add_argument("--channel", type=int,
                    help="swarm radio channel, only if your swarm is not on the default (6)")
    args = ap.parse_args()

    cli = find_cli()
    if not cli:
        sys.exit("arduino-cli not found: install the Arduino IDE or arduino-cli")
    os.makedirs(args.out, exist_ok=True)
    flags = "-DHW_NODE_ID=0" + (" -DHW_SWARM_CHANNEL=%d" % args.channel if args.channel else "")
    rev = commit()
    ok = True
    for fam in [args.only] if args.only else list(FAMILIES):
        sketch, extra = FAMILIES[fam]
        fam_flags = (flags + " " + extra).strip()
        work = os.path.join(os.path.expanduser("~"), ".hivewire-build", fam)
        outdir = os.path.join(work, "out")
        shutil.rmtree(outdir, ignore_errors=True)
        print("building %s ..." % fam, flush=True)
        r = subprocess.run([cli, "compile", "--fqbn", FQBN, "--library", REPO,
                            "--build-property", "compiler.cpp.extra_flags=" + fam_flags,
                            "--build-path", os.path.join(work, "build"), "--output-dir", outdir,
                            os.path.join(REPO, "examples", sketch)],
                           capture_output=True, text=True)
        merged = os.path.join(outdir, sketch + ".ino.merged.bin")   # named after the sketch
        if r.returncode != 0 or not os.path.exists(merged):
            print(r.stdout[-2000:], r.stderr[-2000:])
            print("%s FAILED" % fam)
            ok = False
            continue
        dest = os.path.join(args.out, "%s-generic.merged.bin" % fam)
        shutil.copyfile(merged, dest)
        meta = {"family": fam, "built": datetime.date.today().isoformat(), "commit": rev,
                "channel": args.channel or 6, "node_id": "set after flashing"}
        with open(dest[: -len(".merged.bin")] + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=1)
        print("%s -> %s (%d KB)" % (fam, dest, os.path.getsize(dest) // 1024))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
