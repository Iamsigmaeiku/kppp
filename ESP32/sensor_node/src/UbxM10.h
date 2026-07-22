/**
 * u-blox M10-180C: UBX-CFG-VALSET bring-up + non-blocking NAV-PVT parser.
 */
#pragma once

#include <HardwareSerial.h>
#include <stddef.h>
#include <stdint.h>

struct UbxPvt {
  uint32_t itow;
  int32_t lat;
  int32_t lon;
  int32_t height;
  int32_t vel_n;
  int32_t vel_e;
  int32_t vel_d;
  int32_t g_speed;
  int32_t head_mot;
  uint32_t h_acc;
  uint32_t v_acc;
  uint32_t s_acc;
  uint8_t num_sv;
  uint8_t fix_type;
  bool valid;
};

class UbxM10 {
 public:
  explicit UbxM10(HardwareSerial &port) : ser_(port) {}

  /** Opens UART @9600 on rx/tx, VALSET→115200 UBX-PVT@10Hz. */
  void configure(int rx_pin, int tx_pin);

  bool poll(UbxPvt &out);

  /** Bytes read since last call (GPS UART activity). */
  uint32_t takeRxBytes();

 private:
  HardwareSerial &ser_;
  uint32_t rx_bytes_ = 0;
  int rx_ = 16;
  int tx_ = 17;
  uint8_t state_ = 0;
  uint8_t cls_ = 0, id_ = 0;
  uint16_t len_ = 0, idx_ = 0;
  uint8_t ck_a_ = 0, ck_b_ = 0;
  uint8_t payload_[92];

  void sendValSet(uint8_t layers, const uint32_t *keys, const uint32_t *vals,
                  size_t n);
  void sendBaud115200();
  void sendNavConfig();
  void resetParser();
  bool handlePayload(UbxPvt &out);
};
