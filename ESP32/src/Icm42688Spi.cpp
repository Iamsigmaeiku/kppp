#include "Icm42688Spi.h"

#include <Arduino.h>
#include <SPI.h>
#include <math.h>

namespace {

constexpr uint8_t kWhoAmIReg = 0x75;
constexpr uint8_t kWhoAmIVal = 0x47;
constexpr uint8_t kPwrMgmt0 = 0x4E;
constexpr uint8_t kGyroConfig0 = 0x4F;
constexpr uint8_t kAccelConfig0 = 0x50;
constexpr uint8_t kGyroAccelConfig0 = 0x52;
constexpr uint8_t kTempData1 = 0x1D;
constexpr uint8_t kUiFiltLowLatency = 0xFF;

constexpr float kAccelSens = 2048.0f;  // ±16g
constexpr float kGyroSens = 16.4f;     // ±2000 dps

SPIClass *spiBus() {
  // VSPI on classic ESP32
  return &SPI;
}

int16_t be16(const uint8_t *p) {
  return (int16_t)((p[0] << 8) | p[1]);
}

}  // namespace

bool Icm42688Spi::writeReg(uint8_t reg, uint8_t val) {
  spiBus()->beginTransaction(SPISettings(ICM_SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(ICM_SPI_CS, LOW);
  spiBus()->transfer(reg & 0x7F);
  spiBus()->transfer(val);
  digitalWrite(ICM_SPI_CS, HIGH);
  spiBus()->endTransaction();
  return true;
}

bool Icm42688Spi::readRegs(uint8_t reg, uint8_t *buf, size_t len) {
  if (!buf || len == 0) return false;
  spiBus()->beginTransaction(SPISettings(ICM_SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(ICM_SPI_CS, LOW);
  spiBus()->transfer(reg | 0x80);
  for (size_t i = 0; i < len; i++) {
    buf[i] = spiBus()->transfer(0x00);
  }
  digitalWrite(ICM_SPI_CS, HIGH);
  spiBus()->endTransaction();
  return true;
}

bool Icm42688Spi::writeRetry(uint8_t reg, uint8_t val, int tries) {
  for (int i = 0; i < tries; i++) {
    if (writeReg(reg, val)) {
      delay(2);
      return true;
    }
    delay(10);
  }
  return false;
}

bool Icm42688Spi::probe() {
  uint8_t who = 0;
  if (!readRegs(kWhoAmIReg, &who, 1)) return false;
  if (who != kWhoAmIVal) {
    Serial.printf("[icm-spi] unexpected WHO_AM_I=0x%02X (want 0x%02X)\n", who,
                  kWhoAmIVal);
    return false;
  }
  Serial.printf("[icm-spi] WHO_AM_I=0x%02X CS=GPIO%d\n", who, ICM_SPI_CS);
  return true;
}

bool Icm42688Spi::begin() {
  pinMode(ICM_SPI_CS, OUTPUT);
  digitalWrite(ICM_SPI_CS, HIGH);
  spiBus()->begin(ICM_SPI_SCK, ICM_SPI_MISO, ICM_SPI_MOSI, ICM_SPI_CS);
  delay(10);

  if (!probe()) return false;

  // Soft path: LN accel+gyro
  if (!writeRetry(kPwrMgmt0, 0x0F)) return false;
  delay(45);
  // ODR=200Hz, ±16g / ±2000dps; UI filt = low-latency
  if (!writeRetry(kGyroConfig0, 0x07)) return false;
  if (!writeRetry(kAccelConfig0, 0x07)) return false;
  if (!writeRetry(kGyroAccelConfig0, kUiFiltLowLatency)) return false;
  delay(20);

  IcmSample probeS{};
  if (!readSample(probeS) || !probeS.ok) {
    Serial.println("[icm-spi] first read failed / implausible |a|");
    return false;
  }
  Serial.printf("[icm-spi] ok |a|=%.2f T=%.1f (SPI %luHz)\n", probeS.accel_mag,
                probeS.imu_temp_c, (unsigned long)ICM_SPI_HZ);
  return true;
}

bool Icm42688Spi::readSample(IcmSample &out) {
  uint8_t raw[14];
  if (!readRegs(kTempData1, raw, 14)) {
    out.ok = false;
    return false;
  }

  const int16_t temp = be16(&raw[0]);
  const int16_t ax = be16(&raw[2]);
  const int16_t ay = be16(&raw[4]);
  const int16_t az = be16(&raw[6]);
  const int16_t gx = be16(&raw[8]);
  const int16_t gy = be16(&raw[10]);
  const int16_t gz = be16(&raw[12]);

  out.imu_temp_c = (temp / 132.48f) + 25.0f;
  out.ax = ax / kAccelSens;
  out.ay = ay / kAccelSens;
  out.az = az / kAccelSens;
  out.gx = gx / kGyroSens;
  out.gy = gy / kGyroSens;
  out.gz = gz / kGyroSens;
  out.accel_mag = sqrtf(out.ax * out.ax + out.ay * out.ay + out.az * out.az);
  out.ok = out.accel_mag >= ICM_MIN_ACCEL_MAG && out.accel_mag <= ICM_MAX_ACCEL_MAG;
  return out.ok;
}
