# Stake enclosure: print notes

Source: `enclosure.scad` (editable). Printed meshes: `stl/stake.stl`, `stl/lid.stl`.

## Status

| Check | Result |
| --- | --- |
| Meshes watertight (every edge shared by exactly two faces) | Passed, both parts |
| Slicer time on the venue printer | **Not yet measured. Fill in below before printing.** |
| Physical fit of Uno, accelerometer, moisture sensor | **Not yet checked. Fill in below after printing.** |
| `enclosure.scad` rendered in OpenSCAD | **Not yet checked.** It was written to match the generator that made the STLs; render it once (F6) and compare against `stl/stake.stl` before editing. |

## Measured parts and clearances

| Part | Measured | Opening in the print | Play |
| --- | --- | --- | --- |
| Arduino Uno R4 + Grove Base Shield | 70 × 50 × 25 mm (team measurement); stock Uno PCB is 68.6 × 53.4 mm | Pocket 54.4 × 70.6 mm, 38 mm deep | 0.5 mm per side on a 53.4 mm board. Set `UNO_W = 51.0` if the board really is 50 mm wide. |
| Grove accelerometer | 25 × 20 mm, 1.6 mm PCB | Rails 25.5 mm apart, groove 2.0 mm wide, 1.5 mm deep | 0.5 mm along the edge, 0.4 mm on thickness |
| Grove moisture sensor | 20 mm wide | Groove 21 × 3.5 mm in the shaft front | 0.5 mm per side; zip ties hold it |
| USB-C plug | about 12 × 7 mm overmold | Slot 30 × 12.5 mm in the housing top wall | Wide on purpose: the USB-C position varies between boards |

## Orientation and assembly

- Print the stake **lying on its back** (back plate on the bed) and the lid **flat side down**, lip up. No supports needed: every opening is either vertical, open to the top, or a short bridge.
- When standing in the ground: tip down, lid facing the viewer. Arduino USB-C edge **up**. The Uno rests on the housing floor, so it cannot slide down.
- Accelerometer: push it into the slot on the inside left wall, 25 mm edge along the stake, until it stops at the back plate. Its Grove connector faces the outer wall. Add a dab of hot glue: any wobble shows up as fake tilt.
- Moisture sensor: lay it in the shaft groove, components facing out, prongs past the tip. Strap with two zip ties through the side slots. Run the Grove cable up the groove and in through the floor notch.
- The tilt baseline is captured after installation (`python -m backend.sensors calibrate --step baseline`), so the accelerometer's mounting angle does not need to be exact. It must be rigid.

## Print settings

| Setting | Value |
| --- | --- |
| Printer | Bambu P1S or Flashforge Adventurer 5M |
| Material | PLA |
| Nozzle / layer | 0.4 mm / 0.20 mm |
| Walls / infill | 2 walls, 15 % infill |
| Supports | None |
| Plate footprint (both parts) | about 148 × 184 mm, fits both beds |
| Time estimate | Rough estimate 1.5 to 2 h for both parts (own slice-based estimate, not the vendor slicer). **Replace with the slicer's number:** ______ |
| Limit | Under 3.5 h including both parts. If over: 0.28 mm layers, or print the lid as a separate job. |

## Fit checks (after printing)

- [ ] Uno slides in with the USB-C edge up and does not drop or rattle
- [ ] USB-C cable plugs in through the top slot without forcing the board
- [ ] Accelerometer slides fully into the rails and stays put when the stake is shaken
- [ ] Moisture sensor sits flat in the groove; both zip ties pass through
- [ ] Lid presses on and stays on; the cable notch is not blocked
- [ ] Standing stake: housing is 7 to 9 cm above the soil with 2 to 3 cm of shaft buried

## Limits

PLA prints are not waterproof and soften in hot sun. This is a demonstration enclosure, not a field-ready one.
