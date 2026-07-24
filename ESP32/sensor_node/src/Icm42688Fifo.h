/**
 * ICM-42688-P VSPI + internal FIFO (accel+gyro+temp @ 1 kHz).
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifndef ICM_SPI_SCK
#define ICM_SPI_SCK 18
#endif
#ifndef ICM_SPI_MISO
#define ICM_SPI_MISO 19
#endif
#ifndef ICM_SPI_MOSI
#define ICM_SPI_MOSI 23
#endif
#ifndef ICM_SPI_CS
#define ICM_SPI_CS 5
#endif
#ifndef ICM_SPI_HZ
#define ICM_SPI_HZ 10000000u
#endif

struct IcmFifoSample {
  uint32_t ts_us;
  int16_t ax, ay, az;
  int16_t gx, gy, gz;
  int16_t temp;
  bool ok;
};

class Icm42688Fifo {
 public:
  bool begin();
  /** Drain FIFO into out[]; returns count (max max_n). Assigns ts_us via micros(). */
  size_t readFifo(IcmFifoSample *out, size_t max_n);
  /** 執行期健康檢查（WHO_AM_I）；線鬆 / SPI 掛了回 false。 */
  bool whoamiOk();

  static constexpr float kAccelSens = 2048.0f;  // ±16g
  static constexpr float kGyroSens = 16.4f;     // ±2000 dps

 private:
  bool writeReg(uint8_t reg, uint8_t val);
  bool readRegs(uint8_t reg, uint8_t *buf, size_t len);
  bool writeRetry(uint8_t reg, uint8_t val, int tries = 5);
  bool probe();
  uint16_t fifoCount();
};
