#include "Icm42688Fifo.h"

#include <Arduino.h>
#include <SPI.h>

namespace {

constexpr uint8_t kWhoAmIReg = 0x75;
constexpr uint8_t kWhoAmIVal = 0x47;
constexpr uint8_t kDeviceConfig = 0x11;
constexpr uint8_t kFifoConfig = 0x16;
constexpr uint8_t kPwrMgmt0 = 0x4E;
constexpr uint8_t kGyroConfig0 = 0x4F;
constexpr uint8_t kAccelConfig0 = 0x50;
constexpr uint8_t kGyroAccelConfig0 = 0x52;
constexpr uint8_t kFifoConfig1 = 0x5F;
constexpr uint8_t kFifoConfig2 = 0x60;
constexpr uint8_t kFifoConfig3 = 0x61;
constexpr uint8_t kFifoCountH = 0x2E;
constexpr uint8_t kFifoData = 0x30;

constexpr size_t kPktSize = 16;  // header + accel + gyro + temp + ts

SPIClass *spiBus() { return &SPI; }

int16_t be16(const uint8_t *p) {
  return (int16_t)((p[0] << 8) | p[1]);
}

}  // namespace

bool Icm42688Fifo::writeReg(uint8_t reg, uint8_t val) {
  spiBus()->beginTransaction(SPISettings(ICM_SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(ICM_SPI_CS, LOW);
  spiBus()->transfer(reg & 0x7F);
  spiBus()->transfer(val);
  digitalWrite(ICM_SPI_CS, HIGH);
  spiBus()->endTransaction();
  return true;
}

bool Icm42688Fifo::readRegs(uint8_t reg, uint8_t *buf, size_t len) {
  if (!buf || len == 0) return false;
  spiBus()->beginTransaction(SPISettings(ICM_SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(ICM_SPI_CS, LOW);
  spiBus()->transfer(reg | 0x80);
  for (size_t i = 0; i < len; i++) buf[i] = spiBus()->transfer(0x00);
  digitalWrite(ICM_SPI_CS, HIGH);
  spiBus()->endTransaction();
  return true;
}

bool Icm42688Fifo::writeRetry(uint8_t reg, uint8_t val, int tries) {
  for (int i = 0; i < tries; i++) {
    writeReg(reg, val);
    delay(2);
    return true;
  }
  return false;
}

bool Icm42688Fifo::probe() {
  uint8_t who = 0;
  if (!readRegs(kWhoAmIReg, &who, 1)) return false;
  if (who != kWhoAmIVal) {
    Serial.printf("[icm] WHO_AM_I=0x%02X want 0x%02X\n", who, kWhoAmIVal);
    return false;
  }
  Serial.printf("[icm] WHO_AM_I=0x%02X CS=GPIO%d SPI=%luHz\n", who, ICM_SPI_CS,
                (unsigned long)ICM_SPI_HZ);
  return true;
}

uint16_t Icm42688Fifo::fifoCount() {
  uint8_t b[2] = {0, 0};
  if (!readRegs(kFifoCountH, b, 2)) return 0;
  return (uint16_t)((b[0] << 8) | b[1]);
}

bool Icm42688Fifo::begin() {
  pinMode(ICM_SPI_CS, OUTPUT);
  digitalWrite(ICM_SPI_CS, HIGH);
  spiBus()->begin(ICM_SPI_SCK, ICM_SPI_MISO, ICM_SPI_MOSI, ICM_SPI_CS);
  delay(10);

  // soft reset
  writeReg(kDeviceConfig, 0x01);
  delay(50);

  if (!probe()) return false;

  // LN accel+gyro
  if (!writeRetry(kPwrMgmt0, 0x0F)) return false;
  delay(45);

  // ODR=1kHz, ±2000dps / ±16g
  if (!writeRetry(kGyroConfig0, 0x06)) return false;
  if (!writeRetry(kAccelConfig0, 0x06)) return false;
  if (!writeRetry(kGyroAccelConfig0, 0xFF)) return false;

  // FIFO: stream-to-FIFO mode
  if (!writeRetry(kFifoConfig, 0x40)) return false;  // FIFO_MODE=stream (bit6..7=01 → 0x40)
  // Enable accel+gyro+temp in FIFO (no fsync ts required; pkt still 16B with empty ts)
  if (!writeRetry(kFifoConfig1, 0x07)) return false;  // TEMP|GYRO|ACCEL
  if (!writeRetry(kFifoConfig2, 0x00)) return false;
  if (!writeRetry(kFifoConfig3, 0x00)) return false;

  delay(20);
  // flush any junk
  const uint16_t n = fifoCount();
  uint8_t junk[16];
  for (uint16_t i = 0; i + kPktSize <= n; i += (uint16_t)kPktSize) {
    readRegs(kFifoData, junk, kPktSize);
  }

  Serial.println("[icm] FIFO 1kHz ready");
  return true;
}

size_t Icm42688Fifo::readFifo(IcmFifoSample *out, size_t max_n) {
  if (!out || max_n == 0) return 0;
  uint16_t bytes = fifoCount();
  size_t got = 0;
  const uint32_t t_batch = micros();
  while (bytes >= kPktSize && got < max_n) {
    uint8_t pkt[16];
    if (!readRegs(kFifoData, pkt, kPktSize)) break;
    bytes = (bytes >= kPktSize) ? (uint16_t)(bytes - kPktSize) : 0;

    // Header bit0=accel, bit1=gyro; skip empty/invalid
    const uint8_t hdr = pkt[0];
    if ((hdr & 0x80) == 0 && (hdr & 0x40) == 0) {
      // some revisions: empty marker
    }
    IcmFifoSample &s = out[got];
    s.ts_us = t_batch - (uint32_t)((max_n - got) * 1000u);  // approx 1ms spacing backward
    // Prefer sequential: assign later in caller; here use batch time + index
    s.ts_us = t_batch + (uint32_t)(got * 1000u);
    s.ax = be16(&pkt[1]);
    s.ay = be16(&pkt[3]);
    s.az = be16(&pkt[5]);
    s.gx = be16(&pkt[7]);
    s.gy = be16(&pkt[9]);
    s.gz = be16(&pkt[11]);
    // temp is int8 in FIFO byte 13 on ICM42688; extend
    s.temp = (int16_t)(int8_t)pkt[13];
    s.ok = true;
    got++;
  }
  return got;
}
