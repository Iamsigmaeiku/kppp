#include "UbxM10.h"

#include <Arduino.h>
#include <esp_timer.h>

namespace {

constexpr uint8_t kLayerRamBbr = 0x03;

#ifndef GNSS_CONFIGURE_TIMEPULSE
#define GNSS_CONFIGURE_TIMEPULSE 0
#endif

size_t ubxCfgValueSize(uint32_t keyId) {
  switch ((keyId >> 28) & 0x07u) {
    case 1:
    case 2:
      return 1;
    case 3:
      return 2;
    case 4:
      return 4;
    case 5:
      return 8;
    default:
      return 1;
  }
}

void ubxFletcher(const uint8_t *payload, size_t len, uint8_t &ckA, uint8_t &ckB) {
  ckA = 0;
  ckB = 0;
  for (size_t i = 0; i < len; i++) {
    ckA = (uint8_t)(ckA + payload[i]);
    ckB = (uint8_t)(ckB + ckA);
  }
}

int32_t le32(const uint8_t *p) {
  return (int32_t)((uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
                   ((uint32_t)p[3] << 24));
}

uint32_t leu32(const uint8_t *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
         ((uint32_t)p[3] << 24);
}

}  // namespace

void UbxM10::resetParser() {
  state_ = 0;
  cls_ = id_ = 0;
  len_ = idx_ = 0;
  ck_a_ = ck_b_ = 0;
}

void UbxM10::sendValSet(uint8_t layers, const uint32_t *keys, const uint32_t *vals,
                        size_t n) {
  uint8_t pkt[8 + 12 * 16 + 2];
  size_t off = 0;
  pkt[off++] = 0xB5;
  pkt[off++] = 0x62;
  pkt[off++] = 0x06;
  pkt[off++] = 0x8A;
  const size_t lenPos = off;
  pkt[off++] = 0;
  pkt[off++] = 0;
  pkt[off++] = 0x00;
  pkt[off++] = layers;
  pkt[off++] = 0x00;
  pkt[off++] = 0x00;
  for (size_t i = 0; i < n; i++) {
    const uint32_t key = keys[i];
    const size_t vsz = ubxCfgValueSize(key);
    pkt[off++] = (uint8_t)(key & 0xFF);
    pkt[off++] = (uint8_t)((key >> 8) & 0xFF);
    pkt[off++] = (uint8_t)((key >> 16) & 0xFF);
    pkt[off++] = (uint8_t)((key >> 24) & 0xFF);
    const uint32_t v = vals[i];
    for (size_t b = 0; b < vsz; b++) {
      pkt[off++] = (uint8_t)((v >> (8 * b)) & 0xFF);
    }
  }
  const uint16_t payloadLen = (uint16_t)(off - 6);
  pkt[lenPos] = (uint8_t)(payloadLen & 0xFF);
  pkt[lenPos + 1] = (uint8_t)((payloadLen >> 8) & 0xFF);
  uint8_t ckA = 0, ckB = 0;
  ubxFletcher(pkt + 2, off - 2, ckA, ckB);
  pkt[off++] = ckA;
  pkt[off++] = ckB;
  ser_.write(pkt, off);
  ser_.flush();
}

void UbxM10::sendBaud115200() {
  static const uint32_t kKeys[] = {0x40520001u};
  static const uint32_t kVals[] = {115200u};
  sendValSet(kLayerRamBbr, kKeys, kVals, 1);
}

void UbxM10::sendNavConfig() {
  static const uint32_t kKeys[] = {
      0x30210001u, 0x30210002u, 0x20110021u, 0x10740001u, 0x10740002u,
      0x20910007u, 0x209100bbu, 0x209100acu, 0x209100cau, 0x209100c0u,
      0x209100c5u, 0x209100b1u,
  };
  static const uint32_t kVals[] = {
      100u, 1u, 4u, 1u, 0u, 1u, 0u, 0u, 0u, 0u, 0u, 0u,
  };
  sendValSet(kLayerRamBbr, kKeys, kVals, sizeof(kKeys) / sizeof(kKeys[0]));
}

void UbxM10::sendTimePulseConfig() {
#if GNSS_CONFIGURE_TIMEPULSE
  // u-blox M10 CFG-TP (SPG 5.20):
  // - TP1 enabled, UTC/GNSS disciplined and aligned to top-of-second
  // - falling edge is the epoch edge because this carrier board's PPS LED is
  //   active-low (a rising/positive pulse makes it look continuously lit)
  // - no unlocked pulse; after valid GNSS time, 1 Hz with 100 ms low pulse
  // Clock-quality logic must still reject epochs whose NAV-PVT time flags are
  // invalid; seeing a pulse alone never means UTC is fully resolved.
  static const uint32_t kKeys[] = {
      0x20050023u,  // CFG-TP-PULSE_DEF = PERIOD
      0x20050030u,  // CFG-TP-PULSE_LENGTH_DEF = LENGTH
      0x40050002u,  // CFG-TP-PERIOD_TP1 [us]
      0x40050003u,  // CFG-TP-PERIOD_LOCK_TP1 [us]
      0x40050004u,  // CFG-TP-LEN_TP1 [us]
      0x40050005u,  // CFG-TP-LEN_LOCK_TP1 [us]
      0x10050007u,  // CFG-TP-TP1_ENA
      0x10050008u,  // CFG-TP-SYNC_GNSS_TP1
      0x10050009u,  // CFG-TP-USE_LOCKED_TP1
      0x1005000au,  // CFG-TP-ALIGN_TO_TOW_TP1
      0x1005000bu,  // CFG-TP-POL_TP1: falling edge at top-of-second
      0x2005000cu,  // CFG-TP-TIMEGRID_TP1: UTC
  };
  static const uint32_t kVals[] = {
      0u, 1u, 1000000u, 1000000u, 0u, 100000u,
      1u, 1u, 1u,       1u,       0u, 0u,
  };
  resetParser();
  ack_seen_ = false;
  sendValSet(kLayerRamBbr, kKeys, kVals, sizeof(kKeys) / sizeof(kKeys[0]));
  const bool accepted = waitForAck(0x06, 0x8A, 800);
  Serial.printf("[gps] CFG-TP VALSET %s\n", accepted ? "ACK" : "NO-ACK/NAK");
#endif
}

bool UbxM10::waitForAck(uint8_t cls, uint8_t id, uint32_t timeout_ms) {
  const uint32_t started = millis();
  while (millis() - started < timeout_ms) {
    UbxPvt ignored{};
    (void)poll(ignored);
    if (ack_seen_ && ack_cls_ == cls && ack_id_ == id) {
      const bool ok = ack_ok_;
      ack_seen_ = false;
      return ok;
    }
    delay(1);
  }
  return false;
}

void UbxM10::configure(int rx_pin, int tx_pin) {
  rx_ = rx_pin;
  tx_ = tx_pin;
  static const uint32_t kTryBauds[] = {38400u, 9600u, 115200u};
  uint32_t found = 0;
  for (size_t i = 0; i < sizeof(kTryBauds) / sizeof(kTryBauds[0]); i++) {
    const uint32_t b = kTryBauds[i];
    ser_.begin(b, SERIAL_8N1, rx_, tx_);
    delay(80);
    resetParser();
    rx_bytes_ = 0;
    const uint32_t t0 = millis();
    while (millis() - t0 < 400) {
      UbxPvt p{};
      if (poll(p)) {
        found = b;
        break;
      }
      if (rx_bytes_ > 40) {
        found = b;
        break;
      }
      delay(2);
    }
    if (found) {
      Serial.printf("[gps] UART activity @ %lu baud (rx=%u)\n", (unsigned long)found,
                    (unsigned)takeRxBytes());
      break;
    }
    ser_.end();
    delay(20);
  }
  if (!found) {
    Serial.println("[gps] WARN: no UART bytes — check TX/RX + 5V");
    found = 38400u;
    ser_.begin(found, SERIAL_8N1, rx_, tx_);
    delay(80);
  }
  sendNavConfig();
  delay(40);
  sendBaud115200();
  delay(150);
  ser_.end();
  delay(40);
  ser_.begin(115200, SERIAL_8N1, rx_, tx_);
  delay(80);
  sendNavConfig();
  delay(40);
  sendTimePulseConfig();
  resetParser();
  Serial.printf("[gps] M10 VALSET: UBX-NAV-PVT @10Hz 115200, TP1=%s\n",
                GNSS_CONFIGURE_TIMEPULSE ? "1Hz UTC falling(active-low)"
                                         : "unchanged");
}

uint32_t UbxM10::takeRxBytes() {
  const uint32_t n = rx_bytes_;
  rx_bytes_ = 0;
  return n;
}

uint32_t UbxM10::takeParserErrors() {
  const uint32_t n = parser_errors_;
  parser_errors_ = 0;
  return n;
}

bool UbxM10::handlePayload(UbxPvt &out) {
  if (cls_ != 0x01 || id_ != 0x07 || len_ < 92) return false;
  const uint8_t *p = payload_;
  out.itow = leu32(&p[0]);
  out.year = (uint16_t)p[4] | ((uint16_t)p[5] << 8);
  out.month = p[6];
  out.day = p[7];
  out.hour = p[8];
  out.minute = p[9];
  out.second = p[10];
  out.valid_flags = p[11];
  out.t_acc = leu32(&p[12]);
  out.nano = le32(&p[16]);
  out.fix_type = p[20];
  out.flags = p[21];
  out.flags2 = p[22];
  out.num_sv = p[23];
  out.lon = le32(&p[24]);
  out.lat = le32(&p[28]);
  out.height = le32(&p[32]);
  out.h_acc = leu32(&p[40]);
  out.v_acc = leu32(&p[44]);
  out.vel_n = le32(&p[48]);
  out.vel_e = le32(&p[52]);
  out.vel_d = le32(&p[56]);
  out.g_speed = le32(&p[60]);
  out.head_mot = le32(&p[64]);
  out.s_acc = leu32(&p[68]);
  out.head_acc = leu32(&p[72]);
  out.sensor_time_us = (uint64_t)esp_timer_get_time();
  out.valid = (out.fix_type >= 2);
  return true;
}

bool UbxM10::poll(UbxPvt &out) {
  while (ser_.available()) {
    const uint8_t b = (uint8_t)ser_.read();
    rx_bytes_++;
    switch (state_) {
      case 0:
        if (b == 0xB5) state_ = 1;
        break;
      case 1:
        state_ = (b == 0x62) ? 2 : 0;
        break;
      case 2:
        cls_ = b;
        ck_a_ = b;
        ck_b_ = b;
        state_ = 3;
        break;
      case 3:
        id_ = b;
        ck_a_ = (uint8_t)(ck_a_ + b);
        ck_b_ = (uint8_t)(ck_b_ + ck_a_);
        state_ = 4;
        break;
      case 4:
        len_ = b;
        ck_a_ = (uint8_t)(ck_a_ + b);
        ck_b_ = (uint8_t)(ck_b_ + ck_a_);
        state_ = 5;
        break;
      case 5:
        len_ |= (uint16_t)b << 8;
        ck_a_ = (uint8_t)(ck_a_ + b);
        ck_b_ = (uint8_t)(ck_b_ + ck_a_);
        idx_ = 0;
        if (len_ > sizeof(payload_)) {
          parser_errors_++;
          resetParser();
        } else {
          state_ = (len_ == 0) ? 7 : 6;
        }
        break;
      case 6:
        if (idx_ < sizeof(payload_)) payload_[idx_] = b;
        ck_a_ = (uint8_t)(ck_a_ + b);
        ck_b_ = (uint8_t)(ck_b_ + ck_a_);
        idx_++;
        if (idx_ >= len_) state_ = 7;
        break;
      case 7:
        if (b != ck_a_) {
          parser_errors_++;
          resetParser();
          break;
        }
        state_ = 8;
        break;
      case 8:
        if (b == ck_b_ && cls_ == 0x05 && len_ == 2 &&
            (id_ == 0x00 || id_ == 0x01)) {
          ack_cls_ = payload_[0];
          ack_id_ = payload_[1];
          ack_ok_ = (id_ == 0x01);
          ack_seen_ = true;
          resetParser();
          break;
        }
        if (b == ck_b_ && handlePayload(out)) {
          resetParser();
          return true;
        }
        if (b != ck_b_) parser_errors_++;
        resetParser();
        break;
      default:
        resetParser();
        break;
    }
  }
  return false;
}
