/**
 * Dual-ESP32 inter-board + UDP binary framing.
 * Frame: [0xAA 0x55][type u8][len u8][payload][crc16-ccitt u16 LE]
 * CRC covers type..payload (poly 0x1021, init 0xFFFF).
 */
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define KPP_SYNC0 0xAAu
#define KPP_SYNC1 0x55u

#define KPP_TYPE_IMU 0x01u   /* ICM42688 */
#define KPP_TYPE_GPS 0x02u
#define KPP_TYPE_FUSED 0x03u
#define KPP_TYPE_MPU 0x04u   /* MPU6050 / GY-521 */
#define KPP_TYPE_DBG 0x05u   /* sensor → wifi 診斷（不上雲） */

#define KPP_FUSED_FLAG_INIT (1u << 0)
#define KPP_FUSED_FLAG_ZUPT (1u << 1)
#define KPP_FUSED_FLAG_GPS (1u << 2)
#define KPP_FUSED_FLAG_IMU_FAULT (1u << 3)
#define KPP_FUSED_FLAG_MPU_ACTIVE (1u << 4)
#define KPP_FUSED_FLAG_MPU_REJECTED (1u << 5)

#define KPP_IMU_SAMPLE_BYTES 18u
#define KPP_IMU_MAX_SAMPLES 5u
/* 0x04 uses same KppImuSample layout as 0x01 */
#define KPP_MAX_PAYLOAD 255u
#define KPP_MAX_FRAME (2u + 1u + 1u + KPP_MAX_PAYLOAD + 2u)

typedef struct __attribute__((packed)) {
  uint32_t ts_us;
  int16_t ax, ay, az;
  int16_t gx, gy, gz;
  int16_t temp;
} KppImuSample;

typedef struct __attribute__((packed)) {
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
} KppGpsPayload;

/*
 * GPS payload v2.  The frame type remains 0x02; receivers distinguish the
 * payload by length and the leading version byte.  Keeping KppGpsPayload
 * above is intentional so old firmware and captures remain decodable.
 *
 * UTC fields are the NAV-PVT measurement epoch. sensor_time_us is the
 * ESP monotonic receive/capture epoch, never wall-clock or UDP receive time.
 */
#define KPP_GPS_PAYLOAD_VERSION_2 2u
typedef struct __attribute__((packed)) {
  uint8_t version;
  uint8_t valid; /* NAV-PVT byte 11: validDate/validTime/fullyResolved */
  uint16_t year;
  uint8_t month, day, hour, minute, second;
  uint8_t fix_type, num_sv, flags, flags2;
  int32_t nano;
  uint32_t itow;
  uint32_t t_acc;
  uint32_t h_acc;
  uint32_t v_acc;
  uint32_t s_acc;
  uint32_t head_acc;
  int32_t lat;
  int32_t lon;
  int32_t height;
  int32_t vel_n;
  int32_t vel_e;
  int32_t vel_d;
  int32_t g_speed;
  int32_t head_mot;
  uint64_t sensor_time_us;
  uint32_t packet_seq;
  uint64_t pps_time_us;
  uint32_t pps_seq;
  uint32_t pps_age_ms;
} KppGpsPayloadV2;

typedef struct __attribute__((packed)) {
  uint16_t gps_rx_bps;
  uint8_t pvt_hz;
  uint8_t fix_type;
  uint8_t num_sv;
  uint8_t reserved;
} KppDbgPayload;

#define KPP_DBG_PAYLOAD_VERSION_2 2u
typedef struct __attribute__((packed)) {
  uint8_t version;
  uint8_t pvt_hz;
  uint8_t fix_type;
  uint8_t num_sv;
  uint32_t gps_rx_bps;
  uint32_t gps_queue_drops;
  uint32_t imu_queue_drops;
  uint32_t mpu_queue_drops;
  uint32_t ring_drops;
  uint32_t gps_ring_drops;
  uint32_t gps_parser_errors;
  uint32_t gps_uart_overflows;
} KppDbgPayloadV2;

typedef struct __attribute__((packed)) {
  uint32_t ts_us;
  int32_t lat;
  int32_t lon;
  int32_t height;
  int16_t vel_n;
  int16_t vel_e;
  int16_t vel_d;
  int16_t yaw;
  int16_t pitch;
  int16_t roll;
  uint16_t pos_std_cm;
  uint8_t flags;
} KppFusedPayload;

static inline uint16_t kpp_crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFFu;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) {
      if (crc & 0x8000u) {
        crc = (uint16_t)((crc << 1) ^ 0x1021u);
      } else {
        crc = (uint16_t)(crc << 1);
      }
    }
  }
  return crc;
}

static inline size_t kpp_frame_encode(uint8_t type, const uint8_t *payload,
                                      uint8_t len, uint8_t *out,
                                      size_t out_cap) {
  const size_t need = 2u + 1u + 1u + (size_t)len + 2u;
  if (!out || need > out_cap || (!payload && len > 0)) return 0;
  out[0] = KPP_SYNC0;
  out[1] = KPP_SYNC1;
  out[2] = type;
  out[3] = len;
  if (len && payload) {
    memcpy(out + 4, payload, len);
  }
  const uint16_t crc = kpp_crc16_ccitt(out + 2, 2u + (size_t)len);
  out[4 + len] = (uint8_t)(crc & 0xFFu);
  out[5 + len] = (uint8_t)((crc >> 8) & 0xFFu);
  return need;
}

struct KppParser {
  uint8_t state = 0;
  uint8_t type = 0;
  uint8_t len = 0;
  uint16_t idx = 0;
  uint8_t buf[KPP_MAX_FRAME]{};

  void reset() {
    state = 0;
    type = 0;
    len = 0;
    idx = 0;
  }

  /** @return 1 ok, -1 crc fail, 0 need more */
  int feed(uint8_t b, uint8_t *out, size_t *out_len) {
    switch (state) {
      case 0:
        if (b == KPP_SYNC0) {
          buf[0] = b;
          state = 1;
        }
        return 0;
      case 1:
        if (b == KPP_SYNC1) {
          buf[1] = b;
          state = 2;
        } else if (b != KPP_SYNC0) {
          state = 0;
        }
        return 0;
      case 2:
        type = b;
        buf[2] = b;
        state = 3;
        return 0;
      case 3:
        len = b;
        buf[3] = b;
        idx = 0;
        state = 4;
        return 0;
      case 4: {
        buf[4 + idx] = b;
        idx++;
        if (idx < (uint16_t)len + 2u) return 0;
        const uint16_t got =
            (uint16_t)buf[4 + len] | ((uint16_t)buf[5 + len] << 8);
        const uint16_t expect = kpp_crc16_ccitt(buf + 2, 2u + (size_t)len);
        const size_t flen = 4u + (size_t)len + 2u;
        if (got != expect) {
          reset();
          return -1;
        }
        if (out && out_len) {
          memcpy(out, buf, flen);
          *out_len = flen;
        }
        reset();
        return 1;
      }
      default:
        reset();
        return 0;
    }
  }
};
