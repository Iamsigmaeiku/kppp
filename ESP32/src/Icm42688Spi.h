/**
 * ICM-42688-P over VSPI (Mode 0).
 * Default pins: SCK=18, MISO=19, MOSI=23, CS=5.
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

/** Reject SPI float / all-zero reads (temp≈25°C, |a|≈0). */
#ifndef ICM_MIN_ACCEL_MAG
#define ICM_MIN_ACCEL_MAG 0.2f
#endif

/**
 * Reject implausible |a| spikes (SPI glitch / bad contact returning noise
 * anywhere in the ±16g register range). Real kart dynamics (cornering,
 * braking, curb strikes) stay well under this; sensor is rated to ±16g.
 */
#ifndef ICM_MAX_ACCEL_MAG
#define ICM_MAX_ACCEL_MAG 8.0f
#endif

struct IcmSample {
  float ax, ay, az;       // g
  float gx, gy, gz;       // dps
  float imu_temp_c;
  float accel_mag;
  bool ok;
};

class Icm42688Spi {
 public:
  bool begin();
  bool readSample(IcmSample &out);

 private:
  bool writeReg(uint8_t reg, uint8_t val);
  bool readRegs(uint8_t reg, uint8_t *buf, size_t len);
  bool writeRetry(uint8_t reg, uint8_t val, int tries = 5);
  bool probe();
};
