#!/bin/bash
# PC tests for header-only modules whose logic doesn't need a radio.
# The stubs stand in for Arduino, Preferences (NVS) and the node's log; the
# header is copied next to them so its own #include "Hivewire.h" finds the stub.
set -eo pipefail   # a FAIL must fail the script, not vanish into the grep
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
cp "$HERE"/stub/* "$T"/
cp "$HERE"/../../src/HivewireLight.h "$HERE"/../../src/HivewirePump.h "$HERE"/../../src/HivewireErrors.h "$T"/
g++ -std=c++17 -Wall -Wno-unused -I"$T" -include cstdarg -Dprivate=public "$HERE"/test_light.cpp -o "$T"/test_light
"$T"/test_light | grep -v "^light"
g++ -std=c++17 -Wall -Wno-unused -I"$T" -include cstdarg -Dprivate=public "$HERE"/test_pump.cpp -o "$T"/test_pump
"$T"/test_pump | grep -E "^(ok|FAIL|all passed|FAILED)|^$"
