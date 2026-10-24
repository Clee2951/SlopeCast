// SlopeCast stake enclosure
// Upright stake: moisture sensor clamped in a groove at the bottom of the shaft, Uno (+ Grove
// Base Shield) in a housing held off the ground, Grove accelerometer in a slot on the
// housing's inside wall. Prints lying on its back with no supports.
//
// Coordinates as printed: x across, y along the stake (y = 0 is the tip), z up from the bed.
// Units: mm. Change the measured values below, then F6 and File > Export > STL.
// Set PART to "stake", "lid" or "plate" (both, side by side) before exporting.
//
// Mirrors the Python generator used to make hardware/stl/*.stl; keep the two in step
// (see print-notes.md).

PART = "plate";
$fn = 48;

// ---------- measured parts (edit these)
UNO_W = 54.4;          // pocket width: stock Uno PCB is 53.4 (set 51.0 if yours really is 50)
UNO_L = 70.6;          // pocket length along the stake: PCB 68.6 + USB-C + clearance
UNO_RIB_H = 2.0;       // ribs keep solder pins off the back plate
ACC_L = 25.0;          // accelerometer edge along the stake
ACC_D = 20.0;          // accelerometer edge that slides in toward the back plate
ACC_CLEAR = 0.5;       // play along ACC_L
SENSOR_W = 20.0;       // moisture sensor PCB width

// ---------- walls and fits
W = 1.6;               // wall
BACK = 1.6;            // back plate (on the bed)
GROOVE_W = 2.0;        // accelerometer PCB groove: 1.6 mm board + 0.4
GROOVE_X = 7.0;        // groove distance from the side wall (room for the Grove connector)
GROOVE_DEPTH = 1.5;    // how far each board edge sits in its rail
COMP_W = 11.0;         // accelerometer compartment width
DIV_T = 1.2;           // divider between compartment and Uno pocket
IN_Z = 38.0;           // housing inside depth: Uno 25 + Grove plugs
SHAFT_L = 110.0;       // tip to housing
SHAFT_W = 28.0;
SHAFT_T = 11.0;
GROOVE_SW = SENSOR_W + 1.0;   // sensor groove width (also carries the Grove cable)
GROOVE_SD = 3.5;              // sensor groove depth
USB_X0 = 20.0; USB_X1 = 50.0; // USB-C opening, from the board's left edge (USB edge up)
USB_Z0 = 0.5;  USB_Z1 = 13.0; // above the inside of the back plate
LID_T = 1.6; LIP_H = 4.0; LIP_W = 1.4; LID_CLEAR = 0.25;

// ---------- derived
IN_X = COMP_W + DIV_T + UNO_W;
IN_Y = UNO_L;
HX0 = 0; HY0 = SHAFT_L + W; HZ0 = BACK;
OX0 = HX0 - W; OX1 = HX0 + IN_X + W;
OY0 = SHAFT_L; OY1 = HY0 + IN_Y + W;
OZ1 = HZ0 + IN_Z;
CX = (OX0 + OX1) / 2;
UNO_X0 = HX0 + COMP_W + DIV_T;

module box(x0, y0, z0, x1, y1, z1) {
    translate([x0, y0, z0]) cube([x1 - x0, y1 - y0, z1 - z0]);
}

module rbox(x0, y0, z0, x1, y1, z1, r) {
    hull() for (x = [x0 + r, x1 - r], y = [y0 + r, y1 - r])
        translate([x, y, z0]) cylinder(r = r, h = z1 - z0);
}

module accel_rails() {
    ya = HY0 + 3.5;                    // lower board edge
    yb = ya + ACC_L + ACC_CLEAR;       // upper board edge
    gx0 = HX0 + GROOVE_X; gx1 = gx0 + GROOVE_W;
    rz1 = HZ0 + ACC_D + 2;
    difference() {
        box(HX0 - 0.5, ya - 3, HZ0 - 0.5, HX0 + COMP_W + 0.5, ya + GROOVE_DEPTH, rz1);
        box(gx0, ya - GROOVE_DEPTH, HZ0, gx1, ya + GROOVE_DEPTH + 1, rz1 + 1);
        box(HX0 - 1, ya, HZ0, gx0 - 1.0, ya + GROOVE_DEPTH + 1, rz1 + 1);
    }
    difference() {
        box(HX0 - 0.5, yb - GROOVE_DEPTH, HZ0 - 0.5, HX0 + COMP_W + 0.5, yb + 3, rz1);
        box(gx0, yb - GROOVE_DEPTH - 1, HZ0, gx1, yb + GROOVE_DEPTH, rz1 + 1);
        box(HX0 - 1, yb - GROOVE_DEPTH - 1, HZ0, gx0 - 1.0, yb, rz1 + 1);
    }
}

module stake() {
    difference() {
        union() {
            // housing shell with USB-C opening (top wall) and cable notch (floor, open to the front)
            difference() {
                rbox(OX0, OY0, 0, OX1, OY1, OZ1, 3.0);
                box(UNO_X0 + USB_X0, OY1 - W - 1, HZ0 + USB_Z0, UNO_X0 + USB_X1, OY1 + 1, HZ0 + USB_Z1);
                box(CX - GROOVE_SW / 2, OY0 - 1, SHAFT_T - GROOVE_SD, CX + GROOVE_SW / 2, HY0 + 1, OZ1 + 1);
                rbox(HX0, HY0, HZ0, HX0 + IN_X, HY0 + IN_Y, OZ1 + 1, 1.4);
            }
            // shaft, overlapping into the housing floor
            box(CX - SHAFT_W / 2, 0, 0, CX + SHAFT_W / 2, OY0 + W * 0.5, SHAFT_T);
            // gussets where shaft meets housing
            box(CX - SHAFT_W / 2 - 10, OY0 - 12, 0, CX - SHAFT_W / 2, OY0 + 0.5, 4);
            box(CX + SHAFT_W / 2, OY0 - 12, 0, CX + SHAFT_W / 2 + 10, OY0 + 0.5, 4);
            // divider between accelerometer compartment and Uno pocket
            box(HX0 + COMP_W, HY0 - 0.5, HZ0 - 0.5, HX0 + COMP_W + DIV_T, HY0 + IN_Y + 0.5, HZ0 + 30);
            // ribs under the Uno
            for (rx = [UNO_X0 + 6, UNO_X0 + UNO_W - 9])
                box(rx, HY0 - 0.5, HZ0 - 0.5, rx + 3, HY0 + IN_Y + 0.5, HZ0 + UNO_RIB_H);
            accel_rails();
        }
        // sensor + cable groove down the front of the shaft
        box(CX - GROOVE_SW / 2, -1, SHAFT_T - GROOVE_SD, CX + GROOVE_SW / 2, OY0 + W + 0.1, SHAFT_T + 1);
        // zip-tie slots either side of the sensor
        for (zy = [6, 34], zx = [CX - SHAFT_W / 2 + 1.0, CX + SHAFT_W / 2 - 3.5])
            box(zx, zy, -1, zx + 2.5, zy + 4.5, SHAFT_T + 1);
    }
}

module lid() {
    w = OX1 - OX0; h = OY1 - OY0; c = LID_CLEAR;
    difference() {
        union() {
            rbox(0, 0, 0, w, h, LID_T, 3.0);
            rbox(W + c, W + c, LID_T - 0.1, w - W - c, h - W - c, LID_T + LIP_H, 1.2);
        }
        rbox(W + c + LIP_W, W + c + LIP_W, LID_T, w - W - c - LIP_W, h - W - c - LIP_W, LID_T + LIP_H + 1, 0.6);
        // gap in the lip over the cable notch
        box(w / 2 - GROOVE_SW / 2, -1, LID_T, w / 2 + GROOVE_SW / 2, W + c + LIP_W + 1, LID_T + LIP_H + 1);
    }
}

if (PART == "stake") stake();
else if (PART == "lid") lid();
else { stake(); translate([OX1 + 8, OY0, 0]) lid(); }
