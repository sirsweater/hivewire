# Hivewire error codes

*Generated from [`errors/codes.json`](errors/codes.json) by `tools/gen_errors.py`. Edit the JSON, not this file.*

Every code has a number that never changes meaning. A code is raised by a
**node**, the **gateway**, or the **host** running the admin page, and may
carry a **subject**: which slot (for a peripheral) or uplink it is about,
shown as `E303/3`. The admin page explains each code where it appears, and
**Report a problem** there collects recent codes into a GitHub issue.

Codes **1000–1999** belong to applications built on Hivewire and are never used
by the library. An application documents its own in its node type
(`kinds.json` → `errors`).

## Radio

### E101 `RADIO_DEAF` — Radio heard no Wi-Fi networks at boot

*Error, raised by the node.*

**Cause.** The boot-time scan found nothing on any channel. Seen on a used board whose saved flash data was stale; also a damaged or missing antenna.

**What to do.** Wipe the board and reflash it. If it still hears nothing, inspect the antenna; replace the board if it is damaged.

### E102 `RADIO_START_FAILED` — ESP-NOW failed to start

*Error, raised by the node.*

**Cause.** node.begin() failed: the Wi-Fi driver or ESP-NOW could not initialise.

**What to do.** Usually transient; the bundled sketches restart themselves. If it persists, reflash with a wipe.

### E103 `NO_COORDINATOR` — No beacon from the coordinator for the failsafe period

*Warning, raised by the node.*

**Cause.** The node stopped hearing the coordinator and fell back to its safe state: out of range, the coordinator is down, or the swarm channel differs.

**What to do.** Check the coordinator is running; move the node or add a relay; confirm every unit uses the same HW_SWARM_CHANNEL.

### E104 `WEAK_LINK` — Link to the hive weaker than -90 dBm

*Warning, raised by the node.*

**Cause.** The hive's own beacons, heard directly, arrive at the edge of range, so some traffic will be lost. Raised once when the link turns weak (and again every few hours while it stays weak); a node that only hears the hive through a relay does not raise it.

**What to do.** Move the node or a relay closer, and keep metal and batteries away from the board's antenna.

## Firmware and updates

### E201 `FW_CRC_BAD` — Firmware image refused: checksum mismatch

*Warning, raised by the node.*

**Cause.** An image arrived over ESP-NOW but did not match its CRC. The node kept running its current firmware.

**What to do.** Push again. Repeated failures point at radio noise near the sender.

### E202 `FW_TRANSFER_STALLED` — Firmware transfer stalled and was abandoned

*Warning, raised by the node.*

**Cause.** The image stopped arriving part way. The node kept running its current firmware.

**What to do.** Push again with the node closer to the sender, or through a relay.

### E203 `FW_WRONG_FAMILY` — Firmware image for a different node type, refused

*Info, raised by the node.*

**Cause.** A push carried another family's image. Refusing it is correct.

**What to do.** Nothing to fix: push the image built for this node's family.

### E204 `FW_NO_SPACE` — Firmware image too large for the update partition

*Error, raised by the node.*

**Cause.** The image is bigger than the space the partition table leaves for an update.

**What to do.** Build with a partition scheme that has an OTA slot large enough, or shrink the image.

### E205 `FW_WRITE_FAILED` — Firmware image could not be written or finalised

*Error, raised by the node.*

**Cause.** The flash write came up short, or finishing the update failed. The node kept running its current firmware.

**What to do.** Push again. If it repeats on one node only, its flash may be failing.

### E206 `UPDATE_ROLLED_BACK` — New firmware did not rejoin the swarm and was rolled back

*Error, raised by the node.*

**Cause.** An update booted but never proved it could rejoin (or crashed repeatedly), so the node reverted to its previous image.

**What to do.** The new image is faulty or mis-built (a different channel or protocol version?). Check it before pushing again.

### E207 `OTA_WIFI_FAILED` — Over-the-air Wi-Fi update could not complete

*Warning, raised by the node.*

**Cause.** Not configured, not armed, no Wi-Fi, a bad URL or a failed download.

**What to do.** Check HW_OTA_SSID and HW_OTA_URL in the build, arm the node first, and that the image server is reachable.

### E208 `UNEXPECTED_RESET` — Node restarted without being asked to

*Warning, raised by the node.*

**Cause.** The chip reported why it restarted, and it was not power-on, an update or a reboot command. The subject is the reason: 9 brownout (the power supply sagged), 4 crash, 5/6/7 a watchdog.

**What to do.** Brownout (/9): power it from a solid supply -- a wall USB charger with a short cable, not a power bank, which may switch off at low current. Crash or watchdog: fetch the node's log and report it, with the firmware CRC.

## Peripherals (raised by applications about any slot)

### E301 `PERIPHERAL_MISSING` — A peripheral did not answer

*Error, raised by the node.*

**Cause.** A sensor or device the application expects was not found. The subject says which slot it feeds.

**What to do.** Check that device's power, ground and data wiring.

### E302 `PERIPHERAL_READ_FAILED` — A peripheral stopped answering

*Warning, raised by the node.*

**Cause.** The device was found earlier but a read failed. The slot's value is stale until it recovers.

**What to do.** Reseat its connector. Repeated failures point at a loose wire or a damaged device.

### E303 `INPUT_FLOATING` — An input looks unconnected

*Error, raised by the node.*

**Cause.** Consecutive samples disagreed far more than a connected source would: typically a floating analog pin.

**What to do.** Check the wire to that input and that the device is powered.

### E304 `VALUE_OUT_OF_RANGE` — A reading is outside its plausible range

*Warning, raised by the node.*

**Cause.** A steady value the connected device should never produce: shorted, mis-wired, or the wrong part.

**What to do.** Check the wiring and that the part is the one the firmware expects.

### E305 `BATTERY_LOW` — Battery low

*Warning, raised by the node.*

**Cause.** The node's battery is nearly empty.

**What to do.** Charge or replace it.

## Provisioning

### E401 `UNASSIGNED` — Board has no node number yet

*Info, raised by the node.*

**Cause.** It runs a generic image and waits to be given a number over USB. It will not join the swarm until then.

**What to do.** Give it a number from the Flash page, or send `setid <n>` over its USB serial.

### E402 `BAD_NODE_ID` — Node number rejected

*Warning, raised by the node.*

**Cause.** A setid asked for a number outside 1-254.

**What to do.** Use a number from 1 to 254 that no other node has.

## Gateway and uplinks

### E501 `UPLINK_DOWN` — Gateway lost an uplink

*Error, raised by the gateway.*

**Cause.** An uplink (LoRa, USB or HTTP) stopped answering. The subject names which.

**What to do.** Check that uplink's cable or radio. For a Meshtastic uplink, see the README's notes on the Serial Module and GPS pin.

### E502 `UNTRUSTED_COMMAND` — Command from an untrusted source refused

*Warning, raised by the gateway.*

**Cause.** A command arrived somewhere it is not accepted from (for example another LoRa channel). Refusing it is correct.

**What to do.** Nothing to fix unless it is you: send commands on the trusted channel.

## Host (the machine running the admin)

### E601 `GATEWAY_SILENT` — Gateway not answering on USB

*Error, raised by the host.*

**Cause.** The host's poll of the gateway failed: unplugged, reset, or its serial port is held by another program.

**What to do.** Check the gateway's USB cable and that nothing else has its port open.

### E602 `CLOCK_BEHIND` — Host clock is behind, readings held

*Warning, raised by the host.*

**Cause.** The host booted with a stale clock and the time is earlier than readings already stored.

**What to do.** Usually fixes itself once network time syncs. Check the host has internet or an NTP source.

### E603 `UPLOAD_FAILED` — Upload to an external service failed

*Warning, raised by the host.*

**Cause.** A batch of readings could not be delivered. It is kept and retried.

**What to do.** Check the host's internet connection and the service's pairing.

### E604 `FLASH_FAILED` — Flashing a board failed

*Warning, raised by the host.*

**Cause.** The Flash page could not write or set up a board; its log says at which step.

**What to do.** Replug the board with a data (not charge-only) USB cable and try again with a wipe.

### E605 `HOST_UNDERVOLTAGE` — Host power supply is too weak

*Warning, raised by the host.*

**Cause.** The host (a Raspberry Pi) reported undervoltage or throttling. USB devices on it make this worse, and it can corrupt the SD card.

**What to do.** Use the host's official power supply, or a powered USB hub for the boards.

### E606 `WRITE_NOT_APPLIED` — A setting was sent but the node never confirmed it

*Warning, raised by the host.*

**Cause.** The gateway sent the write and re-sent it several times, but the node's reports never showed the new value: it is out of range, asleep, or only reachable through a relay (commands are not relayed yet).

**What to do.** Check the node is up and how far it is from the gateway; try again when it is heard directly.
