/**
 * GY-521 / MPU6050 over Wire @ 0x68, 400 kHz.
 * ±16g / ±2000 dps to match ICM42688 raw scale.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifndef MPU_I2C_SDA
#define MPU_I2C_SDA 21
#endif
#ifndef MPU_I2C_SCL
#define MPU_I2C_SCL 22
#endif
#ifndef MPU_I2C_ADDR
#define MPU_I2C_ADDR 0x68
#endif

struct MpuSample {
  uint32_t ts_us;
  int16_t ax, ay, az;
  int16_t gx, gy, gz;
  int16_t temp;
  bool ok;
};

class Mpu6050 {
 public:
  bool begin();
  bool readSample(MpuSample &out);

  static constexpr float kAccelSens = 2048.0f;  // ±16g
  static constexpr float kGyroSens = 16.4f;     // ±2000 dps

 private:
  bool writeReg(uint8_t reg, uint8_t val);
  bool readRegs(uint8_t reg, uint8_t *buf, size_t len);
};
