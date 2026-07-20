/**
 * PROFILE_IMU2_GPS — GY-85 + MPU6050(GY-521) + NEO-6M
 * → POST /api/telemetry/ingest（DEVICE_ID=esp32-kart-02）
 *
 * Pins（已定案，勿改）：
 *   I2C SDA=GPIO21 / SCL=GPIO22
 *   GY-85: ADXL345 0x53 + ITG3205 0x68 + HMC5883L 0x1E
 *   GY-521 MPU6050: AD0→3.3V → 0x69
 *   NEO-6M: TXD→GPIO16, RXD→GPIO17（UART1，legacy UBX-CFG）
 *
 * Sample 欄位表（給後端 / Grafana 對齊）：
 *   gy85_ax/ay/az       ADXL345 @0x53     g（full-res ±16g，256 LSB/g）
 *   gy85_gx/gy/gz       ITG3205 @0x68     dps（14.375 LSB/dps）
 *   gy85_mx/my/mz       HMC5883L @0x1E    µT（±1.3 Ga，1090 LSB/Ga）
 *   gy85_heading_deg    atan2(my, mx)     deg 0–360
 *   mpu_ax/ay/az        MPU6050 @0x69     g（±2g，16384 LSB/g）
 *   mpu_gx/gy/gz        MPU6050           dps（±250，131 LSB/dps）
 *   mpu_temp_c          MPU6050           °C
 *   gps_* / gps_fresh   NEO-6M            同既有 ingest
 *
 * 不送 ICM 的 ax/ay/az — 避免跟 esp32dev 撞名、也被 skip_imu 誤判。
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TinyGPSPlus.h>
#include <math.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <secrets.h>

#ifndef INGEST_URL_FALLBACK
#define INGEST_URL_FALLBACK ""
#endif

SET_LOOP_TASK_STACK_SIZE(16 * 1024);

#ifndef WIFI_SSID2
#define WIFI_SSID2 WIFI_SSID
#define WIFI_PASS2 WIFI_PASS
#endif

// ---- Pins ----
static constexpr int PIN_SDA = 21;
static constexpr int PIN_SCL = 22;
static constexpr int PIN_GPS_RX = 16;  // ESP RX ← NEO TXD
static constexpr int PIN_GPS_TX = 17;  // ESP TX → NEO RXD
static constexpr uint32_t GPS_BAUD = 9600;
static constexpr uint32_t GPS_BAUD_FAST = 38400;
static constexpr uint32_t GPS_FIX_FRESH_MS = 2000;
static constexpr uint32_t GPS_FIX_HOLD_MS = 20000;

// I2C addresses
static constexpr uint8_t ADDR_ADXL345 = 0x53;
static constexpr uint8_t ADDR_ITG3205 = 0x68;
static constexpr uint8_t ADDR_HMC5883 = 0x1E;
static constexpr uint8_t ADDR_MPU6050 = 0x69;

// ADXL345
static constexpr uint8_t ADXL_DEVID = 0x00;
static constexpr uint8_t ADXL_DEVID_VAL = 0xE5;
static constexpr uint8_t ADXL_POWER_CTL = 0x2D;
static constexpr uint8_t ADXL_DATA_FORMAT = 0x31;
static constexpr uint8_t ADXL_DATAX0 = 0x32;
static constexpr float ADXL_SENS = 256.0f;  // full-res LSB/g
// 合理性檢查範圍：擋 I2C 斷線/全 0（下界）與雜訊/接觸不良造成的離譜尖峰
// （上界，遠低於 ±16g 滿量程，但遠高於卡丁車真實動態峰值）。
static constexpr float ADXL_MIN_ACCEL_MAG = 0.2f;
static constexpr float ADXL_MAX_ACCEL_MAG = 8.0f;

// ITG3205
static constexpr uint8_t ITG_WHO = 0x00;
static constexpr uint8_t ITG_SMPLRT = 0x15;
static constexpr uint8_t ITG_DLPF = 0x16;
static constexpr uint8_t ITG_TEMP = 0x1B;
static constexpr uint8_t ITG_GYRO = 0x1D;
static constexpr uint8_t ITG_PWR = 0x3E;
static constexpr float ITG_SENS = 14.375f;  // LSB/dps

// HMC5883L
static constexpr uint8_t HMC_CFG_A = 0x00;
static constexpr uint8_t HMC_CFG_B = 0x01;
static constexpr uint8_t HMC_MODE = 0x02;
static constexpr uint8_t HMC_DATA = 0x03;
static constexpr uint8_t HMC_ID_A = 0x0A;
static constexpr float HMC_LSB_PER_GA = 1090.0f;  // ±1.3 Ga

// MPU6050
static constexpr uint8_t MPU_WHO = 0x75;
static constexpr uint8_t MPU_WHO_VAL = 0x68;
static constexpr uint8_t MPU_PWR1 = 0x6B;
static constexpr uint8_t MPU_ACCEL_CFG = 0x1C;
static constexpr uint8_t MPU_GYRO_CFG = 0x1B;
static constexpr uint8_t MPU_ACCEL_OUT = 0x3B;
static constexpr float MPU_ACCEL_SENS = 16384.0f;  // ±2g
static constexpr float MPU_GYRO_SENS = 131.0f;     // ±250 dps
// MPU 設成 ±2g，暫存器本身就會夾在這個範圍，這裡只擋斷線/全 0。
static constexpr float MPU_MIN_ACCEL_MAG = 0.2f;
static constexpr float MPU_MAX_ACCEL_MAG = 2.5f;

static constexpr uint32_t IMU_PERIOD_MS = 40;  // 25 Hz
static constexpr uint32_t POST_PERIOD_MS = 200;
static constexpr uint32_t POST_BURST_MS = 20;
static constexpr uint32_t NTP_RESYNC_MS = 3600000;
static constexpr uint32_t NTP_RETRY_MS = 15000;
static constexpr size_t POST_CHUNK = 25;
static constexpr size_t RING_CAP = 200;
static constexpr uint32_t INGEST_PRIMARY_RETRY_MS = 300000;

static HardwareSerial gpsSerial(1);
static TinyGPSPlus gps;

struct Sample {
  float gy85_ax, gy85_ay, gy85_az;
  float gy85_gx, gy85_gy, gy85_gz;
  float gy85_mx, gy85_my, gy85_mz;
  float gy85_heading_deg;
  bool has_gy85;

  float mpu_ax, mpu_ay, mpu_az;
  float mpu_gx, mpu_gy, mpu_gz;
  float mpu_temp_c;
  bool has_mpu;

  float gps_lat, gps_lon, gps_speed_mps, gps_course_deg, gps_alt_m, gps_hdop;
  uint32_t gps_satellites;
  bool has_gps;
  bool gps_fresh;
  uint32_t millis_at;
};

struct GpsFix {
  float lat, lon, speed_mps, course_deg, alt_m, hdop;
  uint32_t satellites;
  uint32_t updated_at_ms;
  bool valid;
};

static volatile GpsFix gpsFix{};
static portMUX_TYPE gpsMux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t gpsSentenceCount = 0;
static char gpsLastSentence[128];
static portMUX_TYPE gpsSentMux = portMUX_INITIALIZER_UNLOCKED;

static Sample ring[RING_CAP];
static size_t ringHead = 0;
static size_t ringCount = 0;
static uint32_t ringDropped = 0;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

static volatile bool timeOk = false;
static volatile int64_t millisToUnixOffset = 0;
static bool ntpKicked = false;
static uint32_t ntpKickAt = 0;
static bool ingestUseFallback = false;
static uint32_t ingestFallbackSince = 0;

static bool gy85Ok = false;
static bool mpuOk = false;
static volatile uint32_t gy85OkCount = 0;
static volatile uint32_t gy85FailCount = 0;
static volatile uint32_t mpuOkCount = 0;
static volatile uint32_t mpuFailCount = 0;

// ---- I2C helpers ----
static bool i2cWrite(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool i2cWriteRetry(uint8_t addr, uint8_t reg, uint8_t val, int tries = 5) {
  for (int i = 0; i < tries; i++) {
    if (i2cWrite(addr, reg, val)) return true;
    delay(10);
  }
  return false;
}

static bool i2cRead(uint8_t addr, uint8_t reg, uint8_t *buf, size_t len) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  size_t n = Wire.requestFrom(addr, (uint8_t)len);
  if (n != len) return false;
  for (size_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

static int16_t be16(const uint8_t *p) {
  return (int16_t)((p[0] << 8) | p[1]);
}

static int16_t le16(const uint8_t *p) {
  return (int16_t)((p[1] << 8) | p[0]);
}

static void i2cScan() {
  static const uint8_t kAddrs[] = {ADDR_ADXL345, ADDR_ITG3205, ADDR_HMC5883,
                                   ADDR_MPU6050};
  Serial.println("[i2c] scan expected: 0x53 ADXL345, 0x68 ITG3205, "
                 "0x1E HMC5883L, 0x69 MPU6050");
  for (uint8_t addr : kAddrs) {
    Wire.beginTransmission(addr);
    const uint8_t err = Wire.endTransmission();
    Serial.printf("[i2c] 0x%02X %s\n", addr, err == 0 ? "ACK" : "---");
  }
}

// ---- GY-85 ----
static bool adxlInit() {
  uint8_t id = 0;
  if (!i2cRead(ADDR_ADXL345, ADXL_DEVID, &id, 1) || id != ADXL_DEVID_VAL) {
    Serial.printf("[gy85] ADXL DEVID=0x%02X fail\n", id);
    return false;
  }
  if (!i2cWriteRetry(ADDR_ADXL345, ADXL_POWER_CTL, 0x08)) return false;
  // full-res + ±16g
  if (!i2cWriteRetry(ADDR_ADXL345, ADXL_DATA_FORMAT, 0x0B)) return false;
  Serial.println("[gy85] ADXL345 ok");
  return true;
}

static bool itgInit() {
  uint8_t who = 0;
  if (!i2cRead(ADDR_ITG3205, ITG_WHO, &who, 1)) {
    Serial.println("[gy85] ITG WHO read fail");
    return false;
  }
  // bits[6:1] should be 0x34 → WHO register reads ~0x68
  if ((who & 0x7E) != 0x68) {
    Serial.printf("[gy85] ITG unexpected WHO=0x%02X\n", who);
    // still try init — some clones vary
  }
  if (!i2cWriteRetry(ADDR_ITG3205, ITG_PWR, 0x00)) return false;
  delay(10);
  if (!i2cWriteRetry(ADDR_ITG3205, ITG_SMPLRT, 0x07)) return false;  // 1kHz/(7+1)
  if (!i2cWriteRetry(ADDR_ITG3205, ITG_DLPF, 0x1A)) return false;    // ±2000, DLPF~42Hz
  Serial.printf("[gy85] ITG3205 ok WHO=0x%02X\n", who);
  return true;
}

static bool hmcInit() {
  uint8_t id[3] = {};
  if (!i2cRead(ADDR_HMC5883, HMC_ID_A, id, 3)) {
    Serial.println("[gy85] HMC ID read fail");
    return false;
  }
  if (id[0] != 'H' || id[1] != '4' || id[2] != '3') {
    Serial.printf("[gy85] HMC unexpected ID=%c%c%c\n", id[0], id[1], id[2]);
  }
  if (!i2cWriteRetry(ADDR_HMC5883, HMC_CFG_A, 0x70)) return false;  // 8-avg, 15Hz
  if (!i2cWriteRetry(ADDR_HMC5883, HMC_CFG_B, 0x20)) return false;  // ±1.3 Ga
  if (!i2cWriteRetry(ADDR_HMC5883, HMC_MODE, 0x00)) return false;   // continuous
  delay(10);
  Serial.println("[gy85] HMC5883L ok");
  return true;
}

static bool gy85Init() {
  bool ok = true;
  if (!adxlInit()) ok = false;
  if (!itgInit()) ok = false;
  if (!hmcInit()) ok = false;
  return ok;
}

static bool gy85Read(Sample &out) {
  uint8_t araw[6], graw[6], mraw[6];
  if (!i2cRead(ADDR_ADXL345, ADXL_DATAX0, araw, 6)) return false;
  if (!i2cRead(ADDR_ITG3205, ITG_GYRO, graw, 6)) return false;
  if (!i2cRead(ADDR_HMC5883, HMC_DATA, mraw, 6)) return false;

  out.gy85_ax = le16(&araw[0]) / ADXL_SENS;
  out.gy85_ay = le16(&araw[2]) / ADXL_SENS;
  out.gy85_az = le16(&araw[4]) / ADXL_SENS;

  const float accel_mag = sqrtf(out.gy85_ax * out.gy85_ax +
                                 out.gy85_ay * out.gy85_ay +
                                 out.gy85_az * out.gy85_az);
  if (accel_mag < ADXL_MIN_ACCEL_MAG || accel_mag > ADXL_MAX_ACCEL_MAG) {
    return false;
  }

  out.gy85_gx = be16(&graw[0]) / ITG_SENS;
  out.gy85_gy = be16(&graw[2]) / ITG_SENS;
  out.gy85_gz = be16(&graw[4]) / ITG_SENS;

  // HMC order: X, Z, Y (big-endian)
  const float mx = be16(&mraw[0]) / HMC_LSB_PER_GA * 100.0f;  // µT
  const float mz = be16(&mraw[2]) / HMC_LSB_PER_GA * 100.0f;
  const float my = be16(&mraw[4]) / HMC_LSB_PER_GA * 100.0f;
  out.gy85_mx = mx;
  out.gy85_my = my;
  out.gy85_mz = mz;

  float hd = atan2f(my, mx) * (180.0f / (float)M_PI);
  if (hd < 0) hd += 360.0f;
  out.gy85_heading_deg = hd;
  out.has_gy85 = true;
  return true;
}

// ---- MPU6050 ----
static bool mpuInit() {
  uint8_t who = 0;
  if (!i2cRead(ADDR_MPU6050, MPU_WHO, &who, 1) || who != MPU_WHO_VAL) {
    Serial.printf("[mpu] WHO_AM_I=0x%02X fail (want 0x68 @0x69)\n", who);
    return false;
  }
  if (!i2cWriteRetry(ADDR_MPU6050, MPU_PWR1, 0x00)) return false;
  delay(50);
  if (!i2cWriteRetry(ADDR_MPU6050, MPU_ACCEL_CFG, 0x00)) return false;  // ±2g
  if (!i2cWriteRetry(ADDR_MPU6050, MPU_GYRO_CFG, 0x00)) return false;   // ±250
  Serial.println("[mpu] MPU6050 @0x69 ok");
  return true;
}

static bool mpuRead(Sample &out) {
  uint8_t raw[14];
  if (!i2cRead(ADDR_MPU6050, MPU_ACCEL_OUT, raw, 14)) return false;

  const int16_t ax = be16(&raw[0]);
  const int16_t ay = be16(&raw[2]);
  const int16_t az = be16(&raw[4]);
  const int16_t temp = be16(&raw[6]);
  const int16_t gx = be16(&raw[8]);
  const int16_t gy = be16(&raw[10]);
  const int16_t gz = be16(&raw[12]);

  out.mpu_ax = ax / MPU_ACCEL_SENS;
  out.mpu_ay = ay / MPU_ACCEL_SENS;
  out.mpu_az = az / MPU_ACCEL_SENS;

  const float accel_mag = sqrtf(out.mpu_ax * out.mpu_ax +
                                 out.mpu_ay * out.mpu_ay +
                                 out.mpu_az * out.mpu_az);
  if (accel_mag < MPU_MIN_ACCEL_MAG || accel_mag > MPU_MAX_ACCEL_MAG) {
    return false;
  }

  out.mpu_gx = gx / MPU_GYRO_SENS;
  out.mpu_gy = gy / MPU_GYRO_SENS;
  out.mpu_gz = gz / MPU_GYRO_SENS;
  out.mpu_temp_c = (temp / 340.0f) + 36.53f;
  out.has_mpu = true;
  return true;
}

// ---- GPS (legacy NEO-6M) ----
static void gpsPoll() {
  static char lineBuf[128];
  static size_t lineLen = 0;
  while (gpsSerial.available()) {
    const char c = (char)gpsSerial.read();
    if (c != '\r' && c != '\n' && lineLen + 1 < sizeof(lineBuf)) {
      lineBuf[lineLen++] = c;
    }
    if (gps.encode(c)) {
      gpsSentenceCount++;
      if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        portENTER_CRITICAL(&gpsSentMux);
        strncpy(gpsLastSentence, lineBuf, sizeof(gpsLastSentence) - 1);
        gpsLastSentence[sizeof(gpsLastSentence) - 1] = '\0';
        portEXIT_CRITICAL(&gpsSentMux);
      }
      lineLen = 0;
      if (gps.location.isValid() && gps.location.age() < GPS_FIX_HOLD_MS) {
        portENTER_CRITICAL(&gpsMux);
        gpsFix.lat = (float)gps.location.lat();
        gpsFix.lon = (float)gps.location.lng();
        gpsFix.speed_mps = gps.speed.isValid() ? (float)gps.speed.mps() : NAN;
        gpsFix.course_deg = gps.course.isValid() ? (float)gps.course.deg() : NAN;
        gpsFix.alt_m = gps.altitude.isValid() ? (float)gps.altitude.meters() : NAN;
        gpsFix.hdop = gps.hdop.isValid() ? (float)gps.hdop.hdop() : NAN;
        gpsFix.satellites =
            gps.satellites.isValid() ? (uint32_t)gps.satellites.value() : 0;
        gpsFix.updated_at_ms = millis();
        gpsFix.valid = true;
        portEXIT_CRITICAL(&gpsMux);
      }
    }
    if (c == '\n') lineLen = 0;
  }
}

static const uint8_t UBX_CFG_RATE_5HZ[] = {
    0xB5, 0x62, 0x06, 0x08, 0x06, 0x00, 0xC8, 0x00, 0x01, 0x00, 0x01, 0x00,
    0xDE, 0x6A,
};

static const uint8_t UBX_CFG_NAV5_AUTO_FULL[] = {
    0xB5, 0x62, 0x06, 0x24, 0x24, 0x00, 0xFF, 0xFF, 0x04, 0x03, 0x00, 0x00,
    0x00, 0x00, 0x10, 0x27, 0x00, 0x00, 0x05, 0x00, 0xFA, 0x00, 0xFA, 0x00,
    0x64, 0x00, 0x2C, 0x01, 0x00, 0x3C, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x56, 0xA0,
};

static void ubxSetNmeaRate(uint8_t msgId, uint8_t rate) {
  uint8_t pkt[] = {
      0xB5, 0x62, 0x06, 0x01, 0x08, 0x00, 0xF0, msgId, 0x00, rate,
      0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
  };
  uint8_t ckA = 0, ckB = 0;
  for (size_t i = 2; i < 14; i++) {
    ckA = (uint8_t)(ckA + pkt[i]);
    ckB = (uint8_t)(ckB + ckA);
  }
  pkt[14] = ckA;
  pkt[15] = ckB;
  gpsSerial.write(pkt, sizeof(pkt));
}

static const uint8_t UBX_CFG_PRT_38400[] = {
    0xB5, 0x62, 0x06, 0x00, 0x14, 0x00, 0x01, 0x00, 0x00, 0x00, 0xD0, 0x08,
    0x00, 0x00, 0x00, 0x96, 0x00, 0x00, 0x07, 0x00, 0x03, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x93, 0x90,
};

static void gpsConfigure() {
  auto sendSuite = []() {
    gpsSerial.write(UBX_CFG_RATE_5HZ, sizeof(UBX_CFG_RATE_5HZ));
    gpsSerial.flush();
    delay(40);
    gpsSerial.write(UBX_CFG_NAV5_AUTO_FULL, sizeof(UBX_CFG_NAV5_AUTO_FULL));
    gpsSerial.flush();
    delay(40);
    ubxSetNmeaRate(0x00, 1);  // GGA
    ubxSetNmeaRate(0x04, 1);  // RMC
    ubxSetNmeaRate(0x01, 0);
    ubxSetNmeaRate(0x02, 0);
    ubxSetNmeaRate(0x03, 0);
    ubxSetNmeaRate(0x05, 0);
    gpsSerial.flush();
  };

  sendSuite();
  delay(40);
  gpsSerial.write(UBX_CFG_PRT_38400, sizeof(UBX_CFG_PRT_38400));
  gpsSerial.flush();
  delay(120);
  gpsSerial.end();
  delay(40);
  gpsSerial.begin(GPS_BAUD_FAST, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  delay(80);
  sendSuite();

  const uint32_t t0 = millis();
  bool got = false;
  while (millis() - t0 < 1500) {
    if (gpsSerial.available()) {
      got = true;
      break;
    }
    delay(10);
  }
  if (!got) {
    Serial.println("[gps] no UART @38400 → fallback 9600");
    gpsSerial.end();
    delay(40);
    gpsSerial.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
    delay(80);
    sendSuite();
  } else {
    Serial.println("[gps] UBX legacy: 5Hz + Automotive + GGA/RMC @38400");
  }
}

static bool gpsSnapshot(GpsFix &out) {
  portENTER_CRITICAL(&gpsMux);
  out.lat = gpsFix.lat;
  out.lon = gpsFix.lon;
  out.speed_mps = gpsFix.speed_mps;
  out.course_deg = gpsFix.course_deg;
  out.alt_m = gpsFix.alt_m;
  out.hdop = gpsFix.hdop;
  out.satellites = gpsFix.satellites;
  out.updated_at_ms = gpsFix.updated_at_ms;
  out.valid = gpsFix.valid;
  portEXIT_CRITICAL(&gpsMux);
  if (!out.valid) return false;
  return (millis() - out.updated_at_ms) < GPS_FIX_HOLD_MS;
}

static bool gpsIsFresh(const GpsFix &fix) {
  return fix.valid && (millis() - fix.updated_at_ms) < GPS_FIX_FRESH_MS;
}

static void attachGpsToSample(Sample &out) {
  GpsFix fix{};
  out.has_gps = gpsSnapshot(fix);
  out.gps_fresh = false;
  if (out.has_gps) {
    out.gps_lat = fix.lat;
    out.gps_lon = fix.lon;
    out.gps_speed_mps = fix.speed_mps;
    out.gps_course_deg = fix.course_deg;
    out.gps_alt_m = fix.alt_m;
    out.gps_hdop = fix.hdop;
    out.gps_satellites = fix.satellites;
    out.gps_fresh = gpsIsFresh(fix);
  }
}

// ---- ring / time / wifi / http ----
static const char *activeIngestUrl() {
  if (INGEST_URL_FALLBACK[0] == '\0') return INGEST_URL;
  if (ingestUseFallback) return INGEST_URL_FALLBACK;
  return INGEST_URL;
}

static void ingestPreferFallback(const char *why) {
  if (INGEST_URL_FALLBACK[0] == '\0') return;
  if (!ingestUseFallback) {
    Serial.printf("[http] switch → FALLBACK (%s)\n", why);
  }
  ingestUseFallback = true;
  ingestFallbackSince = millis();
}

static void ingestMaybeRetryPrimary() {
  if (!ingestUseFallback || INGEST_URL_FALLBACK[0] == '\0') return;
  if (millis() - ingestFallbackSince < INGEST_PRIMARY_RETRY_MS) return;
  Serial.println("[http] retry PRIMARY after fallback hold");
  ingestUseFallback = false;
}

static void ringPush(const Sample &s) {
  portENTER_CRITICAL(&ringMux);
  ring[ringHead] = s;
  ringHead = (ringHead + 1) % RING_CAP;
  if (ringCount < RING_CAP) {
    ringCount++;
  } else {
    ringDropped++;
  }
  portEXIT_CRITICAL(&ringMux);
}

static size_t ringSnapshot(Sample *out, size_t maxN) {
  portENTER_CRITICAL(&ringMux);
  const size_t n = ringCount < maxN ? ringCount : maxN;
  const size_t start = (ringHead + RING_CAP - ringCount) % RING_CAP;
  for (size_t i = 0; i < n; i++) {
    out[i] = ring[(start + i) % RING_CAP];
  }
  portEXIT_CRITICAL(&ringMux);
  return n;
}

static void ringPopFront(size_t n) {
  portENTER_CRITICAL(&ringMux);
  if (n > ringCount) n = ringCount;
  ringCount -= n;
  portEXIT_CRITICAL(&ringMux);
}

static size_t ringSize() {
  portENTER_CRITICAL(&ringMux);
  size_t n = ringCount;
  portEXIT_CRITICAL(&ringMux);
  return n;
}

static uint64_t toUnixMs(uint32_t millisAt) {
  return (uint64_t)((int64_t)millisAt + millisToUnixOffset);
}

static bool applyUnixTime(time_t unixSec, const char *src) {
  if (unixSec < 1700000000) return false;
  uint32_t m = millis();
  millisToUnixOffset = (int64_t)unixSec * 1000LL - (int64_t)m;
  timeOk = true;
  timeval tv{};
  tv.tv_sec = unixSec;
  tv.tv_usec = 0;
  settimeofday(&tv, nullptr);
  Serial.printf("[time] ok via %s unix=%ld\n", src, (long)unixSec);
  return true;
}

static bool parseHttpDate(const char *date, time_t *out) {
  int day, year, hour, min, sec;
  char mon[4] = {};
  if (sscanf(date, "%*[^,], %d %3s %d %d:%d:%d", &day, mon, &year, &hour, &min,
             &sec) != 6) {
    return false;
  }
  static const char kMonths[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
  const char *p = strstr(kMonths, mon);
  if (!p || (p - kMonths) % 3 != 0) return false;
  struct tm t = {};
  t.tm_year = year - 1900;
  t.tm_mon = (int)(p - kMonths) / 3;
  t.tm_mday = day;
  t.tm_hour = hour;
  t.tm_min = min;
  t.tm_sec = sec;
  setenv("TZ", "GMT0", 1);
  tzset();
  time_t v = mktime(&t);
  if (v < 0) return false;
  *out = v;
  return true;
}

static String ingestOrigin() {
  String u = INGEST_URL;
  const int scheme = u.indexOf("://");
  if (scheme < 0) return u;
  const int path = u.indexOf('/', scheme + 3);
  if (path < 0) return u + "/";
  return u.substring(0, path + 1);
}

static bool syncTimeFromHttpUrl(const String &url) {
  static WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(2500);
  HTTPClient http;
  http.setTimeout(3000);
  http.setConnectTimeout(3000);
  const char *keys[] = {"Date"};
  http.collectHeaders(keys, 1);
  if (!http.begin(client, url)) return false;
  int code = http.GET();
  bool ok = false;
  if (code > 0 && http.hasHeader("Date")) {
    const String date = http.header("Date");
    time_t unixSec = 0;
    if (parseHttpDate(date.c_str(), &unixSec)) {
      ok = applyUnixTime(unixSec, "http-date");
    }
  }
  http.end();
  return ok;
}

static bool syncTimeFromHttp() {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (syncTimeFromHttpUrl(ingestOrigin())) return true;
  return syncTimeFromHttpUrl("https://www.google.com/");
}

static void kickNtp() {
  if (WiFi.status() != WL_CONNECTED) return;
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.nist.gov");
  ntpKicked = true;
  ntpKickAt = millis();
  Serial.println("[ntp] kicked");
}

static void pollTime() {
  if (timeOk) return;
  if (WiFi.status() != WL_CONNECTED) return;
  time_t now = time(nullptr);
  if (now > 1700000000) {
    applyUnixTime(now, "sntp");
    return;
  }
  if (!ntpKicked) {
    kickNtp();
    return;
  }
  static uint32_t lastHttpTry = 0;
  if (millis() - ntpKickAt >= 10000 && millis() - lastHttpTry >= NTP_RETRY_MS) {
    lastHttpTry = millis();
    if (syncTimeFromHttp()) return;
  }
  if (millis() - ntpKickAt >= NTP_RETRY_MS) kickNtp();
}

static void sampleTask(void *) {
  uint32_t lastSample = 0;
  uint32_t lastDropLog = 0;

  for (;;) {
    uint32_t now = millis();
    gpsPoll();

    if (now - lastSample >= IMU_PERIOD_MS) {
      lastSample = now;
      Sample s{};
      s.millis_at = now;
      s.has_gy85 = false;
      s.has_mpu = false;

      if (gy85Ok) {
        if (gy85Read(s)) {
          gy85OkCount++;
        } else {
          gy85FailCount++;
          s.has_gy85 = false;
        }
      }
      if (mpuOk) {
        if (mpuRead(s)) {
          mpuOkCount++;
        } else {
          mpuFailCount++;
          s.has_mpu = false;
        }
      }
      attachGpsToSample(s);
      if (s.has_gy85 || s.has_mpu || s.has_gps) ringPush(s);
    }

    if (ringDropped > 0 && now - lastDropLog > 2000) {
      lastDropLog = now;
      Serial.printf("[ring] dropped=%lu size=%u\n", (unsigned long)ringDropped,
                    (unsigned)ringSize());
    }
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

struct WifiCred {
  const char *ssid;
  const char *pass;
};

static const WifiCred WIFI_NETS[] = {
    {WIFI_SSID, WIFI_PASS},
};
static constexpr size_t WIFI_NET_COUNT = sizeof(WIFI_NETS) / sizeof(WIFI_NETS[0]);
static size_t wifiNetIdx = 0;

static bool tryConnectWifi(size_t idx, uint32_t timeoutMs = 20000) {
  const WifiCred &c = WIFI_NETS[idx % WIFI_NET_COUNT];
  WiFi.disconnect(true, true);
  delay(200);
  WiFi.setSleep(false);
  WiFi.begin(c.ssid, c.pass);
  Serial.printf("[wifi] connecting to %s", c.ssid);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    wifiNetIdx = idx % WIFI_NET_COUNT;
    Serial.printf("[wifi] ok ssid=%s ip=%s rssi=%d\n", c.ssid,
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
    timeOk = false;
    ntpKicked = false;
    kickNtp();
    return true;
  }
  Serial.printf("[wifi] fail %s status=%d\n", c.ssid, (int)WiFi.status());
  return false;
}

static void connectWifi() {
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.mode(WIFI_OFF);
  delay(200);
  WiFi.mode(WIFI_STA);
  delay(100);
  for (size_t i = 0; i < WIFI_NET_COUNT; i++) {
    if (tryConnectWifi((wifiNetIdx + i) % WIFI_NET_COUNT)) return;
  }
  Serial.println("[wifi] FAILED — will keep retrying");
}

static void reconnectWifi() {
  wifiNetIdx = (wifiNetIdx + 1) % WIFI_NET_COUNT;
  Serial.printf("[wifi] reconnect → %s\n", WIFI_NETS[wifiNetIdx].ssid);
  tryConnectWifi(wifiNetIdx, 10000);
}

static bool postBatch() {
  const size_t pending = ringSize();
  if (pending == 0) return true;
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[http] skip (wifi down) ring=%u\n", (unsigned)pending);
    return false;
  }
  if (!timeOk) {
    static uint32_t lastSkipLog = 0;
    if (millis() - lastSkipLog > 5000) {
      lastSkipLog = millis();
      Serial.printf("[http] posting without time sync ring=%u\n",
                    (unsigned)pending);
    }
  }

  ingestMaybeRetryPrimary();

  Sample chunk[POST_CHUNK];
  const size_t n = ringSnapshot(chunk, POST_CHUNK);
  const char *url = activeIngestUrl();
  Serial.printf("[http] posting %u/%u → %s\n", (unsigned)n, (unsigned)pending,
                url);

  static WiFiClientSecure secureClient;
  static WiFiClient plainClient;
  static JsonDocument doc;
  doc.clear();
  doc["device_id"] = DEVICE_ID;
  if (CAR_ID[0] != '\0') doc["car_id"] = CAR_ID;
  JsonArray samples = doc["samples"].to<JsonArray>();

  for (size_t i = 0; i < n; i++) {
    const Sample &s = chunk[i];
    JsonObject o = samples.add<JsonObject>();
    if (timeOk) o["ts_ms"] = toUnixMs(s.millis_at);

    if (s.has_gy85) {
      o["gy85_ax"] = s.gy85_ax;
      o["gy85_ay"] = s.gy85_ay;
      o["gy85_az"] = s.gy85_az;
      o["gy85_gx"] = s.gy85_gx;
      o["gy85_gy"] = s.gy85_gy;
      o["gy85_gz"] = s.gy85_gz;
      o["gy85_mx"] = s.gy85_mx;
      o["gy85_my"] = s.gy85_my;
      o["gy85_mz"] = s.gy85_mz;
      o["gy85_heading_deg"] = s.gy85_heading_deg;
    }
    if (s.has_mpu) {
      o["mpu_ax"] = s.mpu_ax;
      o["mpu_ay"] = s.mpu_ay;
      o["mpu_az"] = s.mpu_az;
      o["mpu_gx"] = s.mpu_gx;
      o["mpu_gy"] = s.mpu_gy;
      o["mpu_gz"] = s.mpu_gz;
      o["mpu_temp_c"] = s.mpu_temp_c;
    }
    if (s.has_gps) {
      o["gps_lat"] = s.gps_lat;
      o["gps_lon"] = s.gps_lon;
      if (!isnan(s.gps_speed_mps)) o["gps_speed_mps"] = s.gps_speed_mps;
      if (!isnan(s.gps_course_deg)) o["gps_course_deg"] = s.gps_course_deg;
      if (!isnan(s.gps_alt_m)) o["gps_alt_m"] = s.gps_alt_m;
      if (!isnan(s.gps_hdop)) o["gps_hdop"] = s.gps_hdop;
      o["gps_satellites"] = s.gps_satellites;
      o["gps_fresh"] = s.gps_fresh ? 1 : 0;
    }
  }

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  const bool isHttps = String(url).startsWith("https://");
  http.setTimeout(isHttps ? 15000 : 3000);
  http.setConnectTimeout(isHttps ? 8000 : 1500);
  http.setReuse(!isHttps);

  bool began = false;
  if (isHttps) {
    secureClient.setInsecure();
    began = http.begin(secureClient, url);
  } else {
    began = http.begin(plainClient, url);
  }
  if (!began) {
    Serial.println("[http] begin failed");
    if (!ingestUseFallback) ingestPreferFallback("begin");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + INGEST_TOKEN);

  int code = http.POST(body);
  String resp = http.getString();
  http.end();

  if (code >= 200 && code < 300) {
    ringPopFront(n);
    Serial.printf("[http] ok %d wrote=%u left=%u\n", code, (unsigned)n,
                  (unsigned)ringSize());
    return true;
  }
  Serial.printf("[http] fail code=%d body=%s (kept ring=%u)\n", code,
                resp.c_str(), (unsigned)ringSize());
  if (!ingestUseFallback && (code < 0 || code >= 500)) {
    ingestPreferFallback(code < 0 ? "conn" : "5xx");
  }
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[boot] ESP32 GY-85+MPU6050+NEO (esp32-imu2-gps)");
  Serial.printf("[boot] device_id=%s ring cap=%u heap=%u\n", DEVICE_ID,
                (unsigned)RING_CAP, (unsigned)ESP.getFreeHeap());

  connectWifi();

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
  Serial.printf("[i2c] SDA=%d SCL=%d\n", PIN_SDA, PIN_SCL);
  i2cScan();

  gy85Ok = gy85Init();
  if (!gy85Ok) Serial.println("[gy85] init FAILED — check wiring / addresses");
  mpuOk = mpuInit();
  if (!mpuOk) Serial.println("[mpu] init FAILED — check AD0→3.3V / 0x69");

  gpsSerial.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  Serial.printf("[gps] NEO-6M UART1 @ %lu baud, rx=%d tx=%d\n",
                (unsigned long)GPS_BAUD, PIN_GPS_RX, PIN_GPS_TX);
  delay(200);
  gpsConfigure();

  Serial.printf("[http] primary=%s\n", INGEST_URL);
  if (INGEST_URL_FALLBACK[0] != '\0') {
    Serial.printf("[http] fallback=%s\n", INGEST_URL_FALLBACK);
  }

  xTaskCreatePinnedToCore(sampleTask, "sample", 6144, nullptr, 1, nullptr, 1);
}

void loop() {
  static uint32_t lastPost = 0;
  static uint32_t lastStat = 0;
  static uint32_t lastWifi = 0;
  static uint32_t lastNtpResync = 0;

  uint32_t now = millis();
  pollTime();

  if (WiFi.status() != WL_CONNECTED && now - lastWifi > 5000) {
    lastWifi = now;
    reconnectWifi();
  }

  if (WiFi.status() == WL_CONNECTED && timeOk &&
      now - lastNtpResync > NTP_RESYNC_MS) {
    lastNtpResync = now;
    timeOk = false;
    ntpKicked = false;
    kickNtp();
  }

  if (now - lastStat >= 2000) {
    lastStat = now;
    GpsFix fix{};
    bool gpsOk = gpsSnapshot(fix);
    Serial.printf(
        "[stat] ring=%u ntp=%d gy85=%lu/%lu mpu=%lu/%lu "
        "gps=%d sats=%lu hdop=%.1f sentences=%lu\n",
        (unsigned)ringSize(), timeOk ? 1 : 0, (unsigned long)gy85OkCount,
        (unsigned long)gy85FailCount, (unsigned long)mpuOkCount,
        (unsigned long)mpuFailCount, gpsOk ? 1 : 0,
        (unsigned long)fix.satellites,
        gps.hdop.isValid() ? (double)gps.hdop.hdop() : -1.0,
        (unsigned long)gpsSentenceCount);
  }

  const size_t pending = ringSize();
  const uint32_t postEvery =
      (pending > POST_CHUNK * 2) ? POST_BURST_MS : POST_PERIOD_MS;
  if (now - lastPost >= postEvery && pending > 0) {
    const int bursts =
        (pending > POST_CHUNK * 3) ? 5 : (pending > POST_CHUNK ? 2 : 1);
    for (int i = 0; i < bursts; i++) {
      if (!postBatch()) break;
      if (ringSize() == 0) break;
    }
    lastPost = millis();
  }
}
