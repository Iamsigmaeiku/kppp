#include "Mpu6050.h"

#include <Arduino.h>
#include <Wire.h>

namespace {

constexpr uint8_t kWhoAmI = 0x75;
constexpr uint8_t kWhoVal = 0x68;
constexpr uint8_t kPwrMgmt1 = 0x6B;
constexpr uint8_t kSMPLRT_DIV = 0x19;
constexpr uint8_t kConfig = 0x1A;
constexpr uint8_t kGyroConfig = 0x1B;
constexpr uint8_t kAccelConfig = 0x1C;
constexpr uint8_t kAccelXoutH = 0x3B;

int16_t be16(const uint8_t *p) {
  return (int16_t)((p[0] << 8) | p[1]);
}

}  // namespace

bool Mpu6050::writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_I2C_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool Mpu6050::readRegs(uint8_t reg, uint8_t *buf, size_t len) {
  if (!buf || len == 0) return false;
  Wire.beginTransmission(MPU_I2C_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  const size_t n = Wire.requestFrom((int)MPU_I2C_ADDR, (int)len);
  if (n != len) return false;
  for (size_t i = 0; i < len; i++) buf[i] = (uint8_t)Wire.read();
  return true;
}

bool Mpu6050::begin() {
  Wire.begin(MPU_I2C_SDA, MPU_I2C_SCL);
  Wire.setClock(400000);

  delay(50);
  uint8_t who = 0;
  if (!readRegs(kWhoAmI, &who, 1) || who != kWhoVal) {
    Serial.printf("[mpu] WHO_AM_I=0x%02X want 0x%02X\n", who, kWhoVal);
    return false;
  }

  // wake
  if (!writeReg(kPwrMgmt1, 0x00)) return false;
  delay(10);
  // DLPF ~42Hz, sample div → internal ~200Hz when gyro output rate 1kHz
  if (!writeReg(kConfig, 0x03)) return false;
  if (!writeReg(kSMPLRT_DIV, 4)) return false;  // 1k/(1+4)=200Hz
  if (!writeReg(kGyroConfig, 0x18)) return false;   // ±2000 dps
  if (!writeReg(kAccelConfig, 0x18)) return false;  // ±16g
  delay(20);

  MpuSample s{};
  if (!readSample(s) || !s.ok) {
    Serial.println("[mpu] first read fail");
    return false;
  }
  Serial.printf("[mpu] ok WHO=0x%02X |a|=%.2f SDA=%d SCL=%d\n", who,
                sqrtf((s.ax / kAccelSens) * (s.ax / kAccelSens) +
                      (s.ay / kAccelSens) * (s.ay / kAccelSens) +
                      (s.az / kAccelSens) * (s.az / kAccelSens)),
                MPU_I2C_SDA, MPU_I2C_SCL);
  return true;
}

bool Mpu6050::readSample(MpuSample &out) {
  uint8_t raw[14];
  if (!readRegs(kAccelXoutH, raw, 14)) {
    out.ok = false;
    return false;
  }
  out.ts_us = micros();
  out.ax = be16(&raw[0]);
  out.ay = be16(&raw[2]);
  out.az = be16(&raw[4]);
  out.temp = be16(&raw[6]);
  out.gx = be16(&raw[8]);
  out.gy = be16(&raw[10]);
  out.gz = be16(&raw[12]);
  out.ok = true;
  return true;
}
