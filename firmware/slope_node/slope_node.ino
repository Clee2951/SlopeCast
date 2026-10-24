// SlopeCast slope node firmware
// Board   : Arduino Uno R4 (Minima/WiFi) or Uno R3, with Grove Base Shield v2
// Sensors : Grove Moisture Sensor on A0
//           Grove 3-axis accelerometer on any I2C port. Auto-detected at boot:
//             ADXL345 (Grove 3-Axis Digital Accelerometer +-16g)  at 0x53 or 0x1D
//             LIS3DHTR (Grove 3-Axis Digital Accelerometer LIS3DHTR) at 0x19 or 0x18
//             MMA7660 (Grove +-1.5g) is detected but reported as unsupported: its
//             ~3 degree steps are too coarse to see slow tilt.
// Output  : newline-delimited JSON over USB serial, 115200 baud, one "reading" per second.
//           The exact format is in docs/serial-protocol.md. Keep the two in sync.
// Input   : "?\n" re-sends the "hello" record (the R4 does not reset when the port opens,
//           so the gateway asks for it on connect).
//
// This firmware sends raw averaged values only. Wetness, tilt and calibration are
// computed on the laptop (backend/sensors.py) so raw readings are always kept.

#include <Wire.h>

#define NODE_ID "N01"          // stable ID; must match config/site.json and config/calibration.json
#define FW_VERSION "1.0.0"
#define PROTOCOL_VERSION 1

const int MOIST_PIN = A0;
const int SEED_PIN = A3;        // unconnected analog pin, used only to seed the boot ID
const int SAMPLES = 20;         // samples averaged per reading
const int SAMPLE_GAP_MS = 10;   // 20 x 10 ms = 200 ms of averaging each second
const unsigned long REPORT_MS = 1000;
const unsigned long REDETECT_MS = 5000;
const int MOIST_UNSTABLE_SPREAD = 120;  // raw counts; a floating (unplugged) input wanders this much
const int MOIST_RAIL_HIGH = 1020;       // pinned at the top of the ADC range: likely a wiring fault

enum AccelModel { ACC_NONE, ACC_ADXL345, ACC_LIS3DH, ACC_MMA7660 };
AccelModel accelModel = ACC_NONE;
uint8_t accelAddr = 0;

long bootId = 0;
unsigned long seq = 0;
unsigned long lastReport = 0;
unsigned long lastDetect = 0;
char cmd[16];
uint8_t cmdLen = 0;

// ---------------------------------------------------------------- I2C helpers
bool writeReg(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool readRegs(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(addr, n) != n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

bool present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

const char *modelName() {
  switch (accelModel) {
    case ACC_ADXL345: return "ADXL345";
    case ACC_LIS3DH: return "LIS3DHTR";
    case ACC_MMA7660: return "MMA7660";
    default: return "none";
  }
}

// ---------------------------------------------------------------- accelerometer
void detectAccel() {
  accelModel = ACC_NONE;
  accelAddr = 0;
  uint8_t id;
  const uint8_t adxl[] = {0x53, 0x1D};
  for (uint8_t i = 0; i < 2; i++) {
    if (readRegs(adxl[i], 0x00, &id, 1) && id == 0xE5) {          // DEVID
      accelAddr = adxl[i];
      writeReg(accelAddr, 0x2C, 0x0A);   // BW_RATE: 100 Hz
      writeReg(accelAddr, 0x31, 0x08);   // DATA_FORMAT: full resolution, +-2 g (3.9 mg/LSB)
      writeReg(accelAddr, 0x2D, 0x08);   // POWER_CTL: measure
      accelModel = ACC_ADXL345;
      return;
    }
  }
  const uint8_t lis[] = {0x19, 0x18};
  for (uint8_t i = 0; i < 2; i++) {
    if (readRegs(lis[i], 0x0F, &id, 1) && id == 0x33) {           // WHO_AM_I
      accelAddr = lis[i];
      writeReg(accelAddr, 0x20, 0x57);   // CTRL_REG1: 100 Hz, X/Y/Z on
      writeReg(accelAddr, 0x23, 0x88);   // CTRL_REG4: block update, +-2 g, high resolution
      accelModel = ACC_LIS3DH;
      return;
    }
  }
  if (present(0x4C)) {
    accelAddr = 0x4C;
    accelModel = ACC_MMA7660;
  }
}

// Returns acceleration in g. False on any I2C failure (never a fake zero).
bool readAccel(float &x, float &y, float &z) {
  uint8_t b[6];
  if (accelModel == ACC_ADXL345) {
    if (!readRegs(accelAddr, 0x32, b, 6)) return false;
    x = (int16_t)(b[0] | (b[1] << 8)) * 0.0039;
    y = (int16_t)(b[2] | (b[3] << 8)) * 0.0039;
    z = (int16_t)(b[4] | (b[5] << 8)) * 0.0039;
    return true;
  }
  if (accelModel == ACC_LIS3DH) {
    if (!readRegs(accelAddr, 0x28 | 0x80, b, 6)) return false;    // 0x80 = auto-increment
    x = ((int16_t)(b[0] | (b[1] << 8)) >> 4) * 0.001;              // 12-bit, 1 mg/LSB at +-2 g
    y = ((int16_t)(b[2] | (b[3] << 8)) >> 4) * 0.001;
    z = ((int16_t)(b[4] | (b[5] << 8)) >> 4) * 0.001;
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- output
void printHello() {
  Serial.print(F("{\"v\":")); Serial.print(PROTOCOL_VERSION);
  Serial.print(F(",\"type\":\"hello\",\"node\":\"")); Serial.print(NODE_ID);
  Serial.print(F("\",\"boot\":")); Serial.print(bootId);
  Serial.print(F(",\"up_ms\":")); Serial.print(millis());
  Serial.print(F(",\"fw\":\"")); Serial.print(FW_VERSION);
  Serial.print(F("\",\"acc_model\":\"")); Serial.print(modelName());
  Serial.print(F("\",\"acc_addr\":")); Serial.print(accelAddr);
  Serial.println(F("}"));
}

void printFloatOrNull(bool ok, float v) {
  if (ok) Serial.print(v, 4); else Serial.print(F("null"));
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      cmd[cmdLen] = 0;
      if (cmdLen == 1 && cmd[0] == '?') printHello();
      cmdLen = 0;
    } else if (cmdLen < sizeof(cmd) - 1) {
      cmd[cmdLen++] = c;
    }
  }
}

// ---------------------------------------------------------------- main
void setup() {
  Serial.begin(115200);
  Wire.begin();
  randomSeed(analogRead(SEED_PIN) ^ micros());
  bootId = random(1, 2147483647L);
  detectAccel();
  lastDetect = millis();
  printHello();
}

void loop() {
  handleSerial();

  unsigned long now = millis();
  if (accelModel == ACC_NONE && now - lastDetect >= REDETECT_MS) {
    detectAccel();
    lastDetect = now;
    if (accelModel != ACC_NONE) printHello();   // tell the gateway what came back
  }
  if (now - lastReport < REPORT_MS) return;
  lastReport = now;

  float sx = 0, sy = 0, sz = 0;
  int accOk = 0;
  long mSum = 0;
  int mMin = 1023, mMax = 0;
  for (int i = 0; i < SAMPLES; i++) {
    float x, y, z;
    if (readAccel(x, y, z)) { sx += x; sy += y; sz += z; accOk++; }
    int m = analogRead(MOIST_PIN);
    mSum += m;
    if (m < mMin) mMin = m;
    if (m > mMax) mMax = m;
    delay(SAMPLE_GAP_MS);
  }
  bool haveAcc = accOk > 0;
  if (haveAcc) { sx /= accOk; sy /= accOk; sz /= accOk; }
  int mAvg = mSum / SAMPLES;

  // health flags: explicit, so a fault is never mistaken for a calm slope
  bool fAccMissing = accelModel == ACC_NONE;
  bool fAccUnsupported = accelModel == ACC_MMA7660;
  bool fAccRead = (accelModel == ACC_ADXL345 || accelModel == ACC_LIS3DH) && !haveAcc;
  bool fAccPartial = haveAcc && accOk < SAMPLES;
  bool fMoistUnstable = (mMax - mMin) > MOIST_UNSTABLE_SPREAD;
  bool fMoistRail = mAvg >= MOIST_RAIL_HIGH;
  if (fAccRead) { accelModel = ACC_NONE; lastDetect = now; }   // re-detect from next loop

  seq++;
  Serial.print(F("{\"v\":")); Serial.print(PROTOCOL_VERSION);
  Serial.print(F(",\"type\":\"reading\",\"node\":\"")); Serial.print(NODE_ID);
  Serial.print(F("\",\"boot\":")); Serial.print(bootId);
  Serial.print(F(",\"seq\":")); Serial.print(seq);
  Serial.print(F(",\"up_ms\":")); Serial.print(now);
  Serial.print(F(",\"moist_raw\":")); Serial.print(mAvg);
  Serial.print(F(",\"moist_min\":")); Serial.print(mMin);
  Serial.print(F(",\"moist_max\":")); Serial.print(mMax);
  Serial.print(F(",\"ax\":")); printFloatOrNull(haveAcc, sx);
  Serial.print(F(",\"ay\":")); printFloatOrNull(haveAcc, sy);
  Serial.print(F(",\"az\":")); printFloatOrNull(haveAcc, sz);
  Serial.print(F(",\"acc_units\":\"g\",\"acc_model\":\"")); Serial.print(fAccRead ? "none" : modelName());
  Serial.print(F("\",\"acc_n\":")); Serial.print(accOk);
  Serial.print(F(",\"fw\":\"")); Serial.print(FW_VERSION);
  Serial.print(F("\",\"flags\":["));
  bool first = true;
  const char *names[] = {"accel_missing", "accel_unsupported_model", "accel_read_failed",
                         "accel_partial", "moisture_unstable", "moisture_rail_high"};
  bool on[] = {fAccMissing, fAccUnsupported, fAccRead, fAccPartial, fMoistUnstable, fMoistRail};
  for (uint8_t i = 0; i < 6; i++) {
    if (!on[i]) continue;
    if (!first) Serial.print(',');
    Serial.print('"'); Serial.print(names[i]); Serial.print('"');
    first = false;
  }
  Serial.println(F("]}"));
}
