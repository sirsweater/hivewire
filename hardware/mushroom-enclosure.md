# Mushroom enclosure for a SoilNode — build spec

A 3D-printed housing for one Hivewire [SoilNode](../examples/SoilNode/SoilNode.ino):
a mushroom whose **cap sheds water away from the electronics**, whose **stem is
the soil probe itself**, and whose **cap underside carries the air sensor** in
still, ventilated air that rain never reaches.

This document is the brief for whoever models it. It states what the parts are,
what the geometry has to achieve and why, and what has to be true before the
result is trusted. Where a dimension is nominal rather than measured, it says
so — **every one of those must be checked against the actual part with
calipers before cutting geometry**, because the boards vary between batches.

---

## 1. What goes inside

| Part | Nominal size (VERIFY) | Notes |
|---|---|---|
| ESP32-C6 SuperMini | 22.5 × 18.0 × 3.2 mm board | USB-C on one short edge; **ceramic antenna on the opposite short edge**. Most units have **no mounting holes** — assume none and clamp it. |
| Capacitive soil probe v1.2/2.0 | 98 × 23 × 1.6 mm | 3-pin header at the top. Doubles as the structural stem. |
| AHT20 breakout | ~15 × 13 × 3 mm, 4-pin | Temperature and humidity. Lives under the cap. |
| LiPo cell | 603048 (6 × 30 × 48 mm) or 103450 (10 × 34 × 50 mm) | Pick one and model for it; do not make a bay that fits "most". JST-PH 2.0 lead. |
| Battery divider | two 1 MΩ resistors | Can sit on a 10 × 10 mm scrap of perfboard or be heat-shrunk inline. |

Wiring, as the firmware expects it:

```
AHT20        SDA -> GPIO20, SCL -> GPIO19   (falls back to 22/23 — the built
                                             boards here are wired 22/23)
Soil probe   AOUT -> GPIO0
             VCC  -> GPIO18   (switched: powered only while being read)
             GND  -> GND
Battery      cell+ -[1M]-+-[1M]- GND, midpoint -> GPIO1
```

Four conductors run from the cap down to the probe head (AOUT, VCC, GND, spare).
Model a **4 mm cable channel** through the stem; do not rely on wires being
squeezed past the probe.

---

## 2. What the shape has to do

### The cap sheds water, and keeps it off the underside

1. **Overhang.** The cap rim must extend **at least 12 mm beyond the widest
   part of the stem** in every direction, so run-off leaves the cap clear of
   everything below.
2. **Drip groove.** A **2 mm wide, 1 mm deep** concentric groove on the
   underside, set **6–8 mm inboard of the rim**. This is the part people omit
   and it is the part that matters: without it, surface tension walks water
   around the rim and onto the underside, which is exactly where the air sensor
   is. The groove breaks that path and makes the drop fall.
3. **Slope.** Convex cap, **minimum 25° at the rim**. Shallower than that and
   water pools on top instead of leaving.
4. **No fasteners through the top surface.** Every screw enters from
   underneath. A screw head on the cap's top face is a funnel into the
   electronics.

### The underside is a ventilated, sheltered pocket

The AHT20 must read the air the plant is in — so it needs airflow — while never
seeing liquid water.

- Recess the sensor in a pocket **at least 5 mm above the lowest plane of the
  cap underside**, so a drop clinging to the underside cannot bridge to it.
- Vent through **slots facing down and outward, 1.5 mm wide**, with a baffle so
  there is no straight path from outside air to the sensor face. A labyrinth of
  two turns is enough.
- Do not enclose the sensor in a sealed chamber: a sealed pocket reads its own
  microclimate and lags the room by hours.
- Keep the sensor away from the C6 and the cell — both are warm. **10 mm
  minimum**, and put a printed wall between them if the layout allows.

### The stem clamps the probe and sets its depth

The probe is the structural member. It is a 1.6 mm PCB, so it is strong in
bending along its length and weak across it; the stem's job is to spread the
load and stop it flexing where it enters the housing.

- **Slot:** 1.8 mm wide (1.6 mm board + 0.2 mm clearance), **25 mm of
  engagement** up inside the stem.
- **Clamp:** a separate printed plate pulled down by **two M2 screws**, not a
  press-fit. Press-fits crack when the probe is pulled out for cleaning.
- **Depth stop:** a **removable collar** that slides on the probe and seats
  against the bottom of the stem. Print it in a few heights (30/40/50 mm of
  exposed probe) so the submersion line can be set per pot without reprinting
  the body. The probe's own marked line is the maximum — **the electronics at
  the top of the probe must never go under**.
- The stem must not seal the pot's soil surface. Leave the stem's footprint
  small, or foot it on three small feet, so watering still reaches the soil
  around it.

### The C6 has to be reachable for charging

- Mount it so the **USB-C port is accessible without disassembly**: either a
  10 × 6 mm port cut-out under the cap's overhang (shadowed by the rim, so
  run-off cannot enter), or the board on a sled that slides out from underneath.
- **The antenna end of the board must point up and outward**, with **10 mm of
  clear plastic and no metal, battery or wet soil beside it**. The cell is the
  worst offender — keep it below the board and offset, never behind the antenna.
- Do not route the probe cable across the antenna end.

### Fasteners

- **M2 × 6 self-tapping** into printed bosses with **1.7 mm pilot holes** for
  boards and the probe clamp, or **M2 heat-set inserts** (3.2 mm hole, 4 mm
  deep) if you would rather it survives repeated opening. Say which in the
  model's notes; do not mix.
- **M3** for cap-to-stem, three of them on a triangle, entering from below.
- Every boss gets a **3 mm minimum wall** around it or it splits on the first
  screw.

---

## 3. Printing

| Setting | Value | Why |
|---|---|---|
| Material | **PETG** (indoors PLA is fine; outdoors ASA) | PLA creeps in a warm window and fails outdoors |
| Walls | 3 perimeters / **1.2 mm** minimum, 2.4 mm on the cap | The cap is the structural shell |
| Orientation | Cap printed **dome up**, stem **vertical** | Layer lines then run across the water's path, not along it |
| Supports | Avoid — design the vents and the port cut-out as **overhangs ≤ 45°** | Supports inside a vent labyrinth cannot be removed |
| Clearance | **0.2 mm** on all sliding fits, 0.15 mm on the probe slot | |
| Infill | 20% | |

Print the depth collars separately, and print the clamp plate flat.

---

## 4. Before it is trusted

Each of these is a pass/fail, not a look-over:

1. **Pour test.** A litre of water poured over the cap from 300 mm. Nothing on
   the underside pocket, nothing at the port cut-out, nothing on the board.
   Tissue paper inside shows the truth better than eyes do.
2. **Humidity response.** Breathe on it, or move it between rooms: the reading
   must track within a couple of minutes. If it lags by an hour, the vents are
   too closed — a sensor reading its own sealed box is worse than no sensor.
3. **Depth stop.** With the collar fitted, the probe's marked line sits
   **above** the soil surface, and the probe cannot slide under load.
4. **Radio, measured not assumed.** This is the one people skip. Note the
   node's RSSI in the admin, assemble it into the enclosure **without moving
   it**, and compare. A cell or a screw behind the antenna can cost 6–10 dB,
   and on the cactus node — which already sits at −91 dBm and needs the relay
   for one report in ten — that is the difference between working and not.
   Press **Add marker** in the admin as you assemble, so the step lands on the
   chart beside the reading.
5. **Charge access.** Plug and unplug a USB-C cable ten times without removing
   the cap or straining the board.

---

## 5. Deliberately left open

- **Waterproofing rating.** This is water-shedding, not sealed. No gaskets are
  specified. If it ever goes outdoors permanently, revisit — an O-ring groove
  at the cap joint and a plugged port are the next step, not a redesign.
- **Cell size.** Pick one before modelling.
- **Whether the node sleeps.** If the [sleepy-sensor work](../README.md) lands,
  the cell gets smaller and the charging port matters less. The housing should
  not be designed around a battery that may shrink by a factor of ten — leave
  the bay a separate printed part if that is easy.

---

## 6. Facts the modeller should not have to rediscover

- The C6 SuperMini prints its pad labels **on the back**, so any silkscreen
  reference in a model is invisible once the board is mounted.
- GPIO8 drives the onboard RGB LED and GPIO15 a second LED. Both draw current
  continuously and **cannot be switched off in firmware** — if this housing is
  ever for a battery-only node, plan for removing them physically, and leave
  access to that corner of the board.
- The probe's exposed traces corrode when left powered, which is why the
  firmware energises it only for the moment of the reading. Nothing in the
  housing should defeat that — do not wire the probe's VCC to 3V3 "to simplify
  the loom".
