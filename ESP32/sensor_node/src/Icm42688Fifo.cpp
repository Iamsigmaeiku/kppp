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
  // 先慢速探，WHO=0x00 常見於上電未穩 / 線鬆
  const uint32_t speeds[] = {1000000u, 4000000u, ICM_SPI_HZ};
  for (uint32_t hz : speeds) {
    for (int attempt = 0; attempt < 3; attempt++) {
      uint8_t who = 0;
      spiBus()->beginTransaction(SPISettings(hz, MSBFIRST, SPI_MODE0));
      digitalWrite(ICM_SPI_CS, LOW);
      spiBus()->transfer(kWhoAmIReg | 0x80);
      who = spiBus()->transfer(0x00);
      digitalWrite(ICM_SPI_CS, HIGH);
      spiBus()->endTransaction();
      if (who == kWhoAmIVal) {
        Serial.printf("[icm] WHO_AM_I=0x%02X CS=GPIO%d SPI=%luHz\n", who,
                      ICM_SPI_CS, (unsigned long)hz);
        return true;
      }
      Serial.printf("[icm] WHO_AM_I=0x%02X want 0x%02X (hz=%lu try=%d)\n", who,
                    kWhoAmIVal, (unsigned long)hz, attempt + 1);
      delay(20);
    }
  }
  return false;
}

bool Icm42688Fifo::whoamiOk() {
  uint8_t who = 0;
  // 健康檢查用低速，避開線鬆時高速讀到假陽性
  spiBus()->beginTransaction(SPISettings(1000000u, MSBFIRST, SPI_MODE0));
  digitalWrite(ICM_SPI_CS, LOW);
  spiBus()->transfer(kWhoAmIReg | 0x80);
  who = spiBus()->transfer(0x00);
  digitalWrite(ICM_SPI_CS, HIGH);
  spiBus()->endTransaction();
  return who == kWhoAmIVal;
}

uint16_t Icm42688Fifo::fifoCount() {
  uint8_t b[2] = {0, 0};
  if (!readRegs(kFifoCountH, b, 2)) return 0;
  return (uint16_t)((b[0] << 8) | b[1]);
}

bool Icm42688Fifo::begin() {
  pinMode(ICM_SPI_CS, OUTPUT);
  digitalWrite(ICM_SPI_CS, HIGH);
  spiBus()->begin(ICM_SPI_SCK, ICM_SPI_MISO, ICM_SPI_MOSI, -1);
  delay(50);

  // soft reset（失敗也不中止，後面 probe 會判）
  writeReg(kDeviceConfig, 0x01);
  delay(80);

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
  // 防 SPI 浮接回 0xFFFF 時死循環
  const uint16_t n_flush = (n > 2048) ? 2048 : n;
  for (uint16_t i = 0; i + kPktSize <= n_flush; i += (uint16_t)kPktSize) {
    readRegs(kFifoData, junk, kPktSize);
  }

  Serial.println("[icm] FIFO 1kHz ready");
  return true;
}

size_t Icm42688Fifo::readFifo(IcmFifoSample *out, size_t max_n) {
  if (!out || max_n == 0) return 0;
  uint16_t bytes = fifoCount();
  // 線鬆時常讀到 0xFFFF；當異常捨棄本輪
  if (bytes > 2048) {
    return 0;
  }
  size_t got = 0;
  const uint32_t t_batch = micros();
  while (bytes >= kPktSize && got < max_n) {
    uint8_t pkt[16];
    if (!readRegs(kFifoData, pkt, kPktSize)) break;
    bytes = (bytes >= kPktSize) ? (uint16_t)(bytes - kPktSize) : 0;

    // ICM-42688 FIFO header bit7=HEADER_MSG（空包）；其餘內容位各版本文檔不一致，
    // 只丟空包 + bus 垃圾，避免誤殺導致「永遠無樣本→狂 reinit」。
    const uint8_t hdr = pkt[0];
    if ((hdr & 0x80) != 0) {
      continue;
    }

    IcmFifoSample &s = out[got];
    s.ts_us = t_batch + (uint32_t)(got * 1000u);
    s.ax = be16(&pkt[1]);
    s.ay = be16(&pkt[3]);
    s.az = be16(&pkt[5]);
    s.gx = be16(&pkt[7]);
    s.gy = be16(&pkt[9]);
    s.gz = be16(&pkt[11]);
    // temp is int8 in FIFO byte 13 on ICM42688; extend
    s.temp = (int16_t)(int8_t)pkt[13];
    // 全 0xFF → 典型 SPI 浮接
    if (s.ax == -1 && s.ay == -1 && s.az == -1 && s.gx == -1 && s.gy == -1 &&
        s.gz == -1) {
      continue;
    }
    s.ok = true;
    got++;
  }
  return got;
}
