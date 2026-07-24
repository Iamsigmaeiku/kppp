/**
 * sensor_node: ICM42688 + MPU6050 + M10 + 15-state ESKF + WiFi upload.
 * Core0: sensors. Core1: ESKF encode → upload ring + HTTPS/UDP flush.
 * No inter-board UART (legacy dual-ESP path was wifi_node).
 *
 * HTTP mode keeps GPS/FUSED; downsamples raw IMU (HTTPS << sensor rate).
 * UDP (LAN) uploads full rate.
 */
#include <Arduino.h>
#include <ArduinoOTA.h>
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <esp_timer.h>
#include <esp_task_wdt.h>
#include <math.h>
#include <string.h>

#include "ByteRing.h"
#include "GpsImuEskf.h"
#include "Icm42688Fifo.h"
#include "KppOta.h"
#include "Mpu6050.h"
#include "UbxM10.h"
#include "packet.h"
#include "secrets.h"

#if TELEMETRY_USE_HTTP
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#else
#include <WiFiUdp.h>
#endif

static constexpr int PIN_GPS_RX = 16;
static constexpr int PIN_GPS_TX = 17;
static constexpr int PIN_LED = 2;
static constexpr uint32_t SENSOR_PERIOD_US = 5000;  // 200 Hz
static constexpr uint32_t FUSED_PERIOD_US = 20000;  // 50 Hz

// 雙 ring：GPS 永不被 IMU 擠掉；滿了只清 imuRing
static constexpr size_t kGpsRingCap = 12288;   // ~10Hz×20s GPS frames
static constexpr size_t kImuRingCap = 16384;
#ifndef KPP_HTTP_BATCH_BYTES
#define KPP_HTTP_BATCH_BYTES 16384
#endif
#if TELEMETRY_USE_HTTP
static constexpr size_t kBatchBytes = KPP_HTTP_BATCH_BYTES;
#else
static constexpr size_t kBatchBytes = 1400;
#endif
#ifndef KPP_HTTP_FLUSH_MS
#define KPP_HTTP_FLUSH_MS 100
#endif
#if TELEMETRY_USE_HTTP
static constexpr uint32_t kFlushMs = KPP_HTTP_FLUSH_MS;
#else
static constexpr uint32_t kFlushMs = 20;
#endif

/* Roll back scheduler behavior independently if field testing finds a regression. */
#ifndef KPP_LEGACY_TASK_PRIORITIES
#define KPP_LEGACY_TASK_PRIORITIES 0
#endif

/** Dual-IMU blend: ICM dominates (lower noise density). */
#ifndef DUAL_IMU_W_ICM
#define DUAL_IMU_W_ICM 0.85f
#endif
#ifndef DUAL_IMU_W_MPU
#define DUAL_IMU_W_MPU 0.15f
#endif
/* Legacy fixed blend rollback. Default uses ICM only and MPU only on failover. */
#ifndef DUAL_IMU_FIXED_BLEND_ENABLE
#define DUAL_IMU_FIXED_BLEND_ENABLE 0
#endif
/* Measured installation: sensor +Y points up, +X longitudinal, +Z lateral. */
#ifndef IMU_MOUNT_Y_UP
#define IMU_MOUNT_Y_UP 1
#endif

/* Roll back with -DKPP_GPS_PACKET_V2=0 without changing the frame type. */
#ifndef KPP_GPS_PACKET_V2
#define KPP_GPS_PACKET_V2 1
#endif

/* Explicitly set to an unused ESP32-S3 input pin after wiring TIMEPULSE. */
#ifndef GNSS_PPS_PIN
#define GNSS_PPS_PIN -1
#endif
#ifndef GNSS_PPS_EDGE_FALLING
#define GNSS_PPS_EDGE_FALLING 1
#endif
/** Consistency gates (phys units, after bias). */
#ifndef DUAL_IMU_GYRO_MAX_RPS
#define DUAL_IMU_GYRO_MAX_RPS 0.50f
#endif
#ifndef DUAL_IMU_ACCEL_MAX_G
#define DUAL_IMU_ACCEL_MAX_G 0.60f
#endif

static HardwareSerial GpsSerial(1);
static Icm42688Fifo icm;
static Mpu6050 mpu;
static UbxM10 gps(GpsSerial);
static GpsImuEskf eskf;

#if TELEMETRY_USE_HTTP
static WiFiClientSecure tls;
static HTTPClient telemetryHttp;
static bool telemetryHttpBegun = false;
#else
static WiFiUDP udp;
#endif

static uint8_t gpsRingStorage[kGpsRingCap];
static uint8_t imuRingStorage[kImuRingCap];
static ByteRing gpsRing(gpsRingStorage, kGpsRingCap);
static ByteRing imuRing(imuRingStorage, kImuRingCap);
static portMUX_TYPE uploadMux = portMUX_INITIALIZER_UNLOCKED;

struct ImuMsg {
  IcmFifoSample s;
};

struct MpuMsg {
  MpuSample s;
};

struct GpsMsg {
  UbxPvt p;
};

static QueueHandle_t imuQ = nullptr;
static QueueHandle_t mpuQ = nullptr;
static QueueHandle_t gpsQ = nullptr;

static volatile uint32_t g_tx_imu = 0, g_tx_mpu = 0, g_tx_gps = 0, g_tx_fused = 0;
static uint32_t g_gps_packet_seq = 0;
static volatile uint8_t g_last_gps_fix = 0, g_last_gps_sv = 0;
static volatile uint32_t g_drop_imu = 0, g_drop_mpu = 0, g_drop_gps = 0, g_ring_drop = 0;
static volatile uint32_t g_gps_ring_drop = 0;  // 僅 gpsRing 真的滿到不得不丟最舊 GPS
static portMUX_TYPE g_pps_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint64_t g_pps_time_us = 0;
static volatile uint32_t g_pps_seq = 0;

static void IRAM_ATTR onGnssPps() {
  const uint64_t now = (uint64_t)esp_timer_get_time();
  portENTER_CRITICAL_ISR(&g_pps_mux);
  g_pps_time_us = now;
  g_pps_seq++;
  portEXIT_CRITICAL_ISR(&g_pps_mux);
}
static volatile uint32_t g_up_bytes = 0, g_up_fail = 0;
static volatile int g_last_http_code = 0;
static volatile bool g_imu_fault = false;
static volatile bool g_mpu_active = false;
static volatile bool g_mpu_rejected = false;
static volatile bool g_mpu_ok = false;
static volatile bool g_icm_ok = false;
static volatile bool g_wifi_ok = false;
static volatile bool g_tx_blink = false;
static volatile uint32_t g_icm_reinit_n = 0;
static volatile uint32_t g_last_icm_sample_us = 0;

// MPU static bias from 2 s window (parallel to ESKF calib)
static float g_mpu_bg[3] = {0, 0, 0};
static float g_mpu_ba[3] = {0, 0, 0};
static bool g_mpu_bias_ready = false;
static double g_mpu_bg_sum[3] = {0, 0, 0};
static double g_mpu_ba_sum[3] = {0, 0, 0};
static uint32_t g_mpu_bias_n = 0;
static uint32_t g_mpu_bias_t0 = 0;

static size_t ringSize() {
  portENTER_CRITICAL(&uploadMux);
  const size_t s = gpsRing.size() + imuRing.size();
  portEXIT_CRITICAL(&uploadMux);
  return s;
}

/** 從 ring head 丟掉一個完整 frame；失敗（無 sync）丟 1 byte。回傳丟掉的 bytes。 */
static size_t dropOneFrameUnlocked(ByteRing &ring) {
  if (ring.empty()) return 0;
  // resync
  while (ring.size() >= 2) {
    const int b0 = ring.peek(0);
    const int b1 = ring.peek(1);
    if (b0 == (int)KPP_SYNC0 && b1 == (int)KPP_SYNC1) break;
    uint8_t dump;
    ring.read(&dump, 1);
  }
  if (ring.size() < 4) {
    const size_t n = ring.size();
    ring.clear();
    return n;
  }
  const int len = ring.peek(3);
  if (len < 0) return 0;
  const size_t frame_n = 4u + (size_t)len + 2u;
  if (frame_n > ring.size()) {
    // 殘缺 frame：丟掉到能重新 sync
    uint8_t dump;
    ring.read(&dump, 1);
    return 1;
  }
  uint8_t tmp[KPP_MAX_FRAME];
  return ring.read(tmp, frame_n);
}

static void ringWriteGps(const uint8_t *data, size_t n) {
  portENTER_CRITICAL(&uploadMux);
  if (n > kGpsRingCap) {
    portEXIT_CRITICAL(&uploadMux);
    return;
  }
  // GPS 優先：只在 gpsRing 真滿時丟最舊 GPS frame（不碰 imuRing、不清整環）
  while (gpsRing.free() < n) {
    if (dropOneFrameUnlocked(gpsRing) == 0) {
      gpsRing.clear();
      break;
    }
    g_gps_ring_drop++;
  }
  gpsRing.write(data, n);
  portEXIT_CRITICAL(&uploadMux);
}

static void ringWriteImu(const uint8_t *data, size_t n) {
  portENTER_CRITICAL(&uploadMux);
  if (n > kImuRingCap) {
    portEXIT_CRITICAL(&uploadMux);
    return;
  }
  // 滿了只清 IMU ring，絕不連坐 GPS
  if (imuRing.free() < n) {
    imuRing.clear();
    g_ring_drop++;
  }
  imuRing.write(data, n);
  portEXIT_CRITICAL(&uploadMux);
}

static bool enqueueFrame(uint8_t type, const uint8_t *payload, uint8_t len) {
#if TELEMETRY_USE_HTTP
  // HTTPS 仍需抽樣；UDP 全送。GPS 永不抽樣。
  if (type == KPP_TYPE_IMU) {
    static uint8_t icm_skip = 0;
    if (++icm_skip < 8) return false;  // ~1/8（原 1/20）
    icm_skip = 0;
  } else if (type == KPP_TYPE_MPU) {
    static uint8_t mpu_skip = 0;
    if (++mpu_skip < 5) return false;  // ~1/5（原 1/10）
    mpu_skip = 0;
  } else if (type == KPP_TYPE_FUSED) {
    static uint8_t fused_skip = 0;
    if (++fused_skip < 2) return false;  // 50→25 Hz
    fused_skip = 0;
  }
#endif
  // IMU ring 將滿：拒 IMU/MPU/FUSED/DBG，GPS 走獨立 ring 不受影響
  if (type != KPP_TYPE_GPS) {
    portENTER_CRITICAL(&uploadMux);
    const bool imu_hot = imuRing.size() > (kImuRingCap * 3 / 4);
    portEXIT_CRITICAL(&uploadMux);
    if (imu_hot && (type == KPP_TYPE_IMU || type == KPP_TYPE_MPU)) {
      return false;
    }
  }

  uint8_t frame[KPP_MAX_FRAME];
  const size_t n = kpp_frame_encode(type, payload, len, frame, sizeof(frame));
  if (!n) return false;
  if (type == KPP_TYPE_GPS) {
    ringWriteGps(frame, n);
  } else {
    ringWriteImu(frame, n);
  }
  return true;
}

static void enqueueImuBatch(uint8_t type, const KppImuSample *samples, size_t n,
                            volatile uint32_t &counter) {
  if (n == 0) return;
  uint8_t payload[1 + KPP_IMU_MAX_SAMPLES * sizeof(KppImuSample)];
  payload[0] = (uint8_t)n;
  memcpy(payload + 1, samples, n * sizeof(KppImuSample));
  if (enqueueFrame(type, payload, (uint8_t)(1 + n * sizeof(KppImuSample)))) {
    counter += (uint32_t)n;
  }
}

static void printDiag();

static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    g_wifi_ok = true;
    kppOtaBegin(OTA_HOSTNAME, OTA_PASSWORD);
    return;
  }
  kppOtaReset();
  g_wifi_ok = false;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  const uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) {
    delay(50);
  }
  g_wifi_ok = (WiFi.status() == WL_CONNECTED);
  if (g_wifi_ok) {
    Serial.printf("[wifi] ok %s\n", WiFi.localIP().toString().c_str());
    kppOtaBegin(OTA_HOSTNAME, OTA_PASSWORD);
  } else {
    Serial.println("[wifi] connect fail — retry later");
  }
}

#if TELEMETRY_USE_HTTP
static bool sendBatchHttp(const uint8_t *data, size_t n, int *out_code) {
  if (n == 0) return false;
  if (out_code) *out_code = -1;
  if (!telemetryHttpBegun) {
    tls.setInsecure();
    if (!telemetryHttp.begin(tls, INGEST_FRAME_URL)) {
      return false;
    }
    telemetryHttp.setReuse(true);
    telemetryHttp.useHTTP10(false);
    telemetryHttp.setTimeout(4000);
    telemetryHttpBegun = true;
  }
  telemetryHttp.addHeader("Content-Type", "application/octet-stream", true);
  telemetryHttp.addHeader("Authorization", "Bearer " INGEST_TOKEN, true);
  const int code = telemetryHttp.POST((uint8_t *)data, n);
  if (code < 0) {
    telemetryHttp.end();
    telemetryHttpBegun = false;
  }
  if (out_code) *out_code = code;
  return code >= 200 && code < 300;
}
#endif

static void sensorTask(void *) {
  esp_task_wdt_delete(nullptr);

  IcmFifoSample batch[KPP_IMU_MAX_SAMPLES];
  uint32_t next = micros();
  uint32_t last_icm_watch = micros();
  uint32_t last_icm_reinit_try = 0;
  // 無有效 FIFO 樣本多久 → 當 ICM 死（線鬆 / SPI 掛）
  constexpr uint32_t kIcmStallUs = 500000u;       // 0.5 s
  constexpr uint32_t kIcmReinitPeriodUs = 2000000u;  // 2 s 重試
  constexpr uint32_t kIcmWhoamiPeriodUs = 1000000u;  // 1 s 巡檢

  for (;;) {
    const uint32_t now = micros();
    if ((int32_t)(now - next) < 0) {
      vTaskDelay(1);
      continue;
    }
    next += SENSOR_PERIOD_US;
    if ((int32_t)(now - next) > (int32_t)(SENSOR_PERIOD_US * 4)) {
      next = now + SENSOR_PERIOD_US;
    }

    if (g_icm_ok) {
      const size_t n = icm.readFifo(batch, KPP_IMU_MAX_SAMPLES);
      for (size_t i = 0; i < n; i++) {
        ImuMsg m{};
        m.s = batch[i];
        if (xQueueSend(imuQ, &m, 0) != pdTRUE) {
          ImuMsg dump;
          xQueueReceive(imuQ, &dump, 0);
          xQueueSend(imuQ, &m, 0);
          g_drop_imu++;
        }
      }
      if (n > 0) {
        g_last_icm_sample_us = now;
      }

      // 巡檢 WHOAMI / stall → 標死，下一輪 reinit
      if ((now - last_icm_watch) >= kIcmWhoamiPeriodUs) {
        last_icm_watch = now;
        const bool stalled =
            (g_last_icm_sample_us != 0) &&
            ((now - g_last_icm_sample_us) > kIcmStallUs);
        if (stalled || !icm.whoamiOk()) {
          g_icm_ok = false;
          g_imu_fault = true;
          Serial.printf(
              "[icm] DEAD stall=%d whoami=%d — will reinit\n", (int)stalled,
              (int)icm.whoamiOk());
        }
      }
    } else {
      // 復活路徑：週期 soft-reset + begin
      if ((now - last_icm_reinit_try) >= kIcmReinitPeriodUs) {
        last_icm_reinit_try = now;
        Serial.println("[icm] reinit…");
        if (icm.begin()) {
          g_icm_ok = true;
          g_last_icm_sample_us = now;
          g_icm_reinit_n++;
          // 清掉可能殘留的舊 ICM queue
          ImuMsg dump;
          while (xQueueReceive(imuQ, &dump, 0) == pdTRUE) {
          }
          Serial.printf("[icm] REVIVED n=%u\n", (unsigned)g_icm_reinit_n);
        } else {
          Serial.println("[icm] reinit FAIL");
        }
      }
    }

    if (g_mpu_ok) {
      MpuSample ms{};
      if (mpu.readSample(ms) && ms.ok) {
        MpuMsg mm{};
        mm.s = ms;
        if (xQueueSend(mpuQ, &mm, 0) != pdTRUE) {
          MpuMsg dump;
          xQueueReceive(mpuQ, &dump, 0);
          xQueueSend(mpuQ, &mm, 0);
          g_drop_mpu++;
        }
      }
    }

    UbxPvt pvt{};
    if (gps.poll(pvt)) {
      GpsMsg gm{};
      gm.p = pvt;
      if (xQueueSend(gpsQ, &gm, 0) != pdTRUE) {
        GpsMsg dump;
        xQueueReceive(gpsQ, &dump, 0);
        xQueueSend(gpsQ, &gm, 0);
        g_drop_gps++;
      }
    }
  }
}

static int16_t sat_i16(float v) {
  if (v > 32767.0f) return 32767;
  if (v < -32768.0f) return -32768;
  return (int16_t)v;
}

static void sensorToBody(float &x, float &y, float &z) {
#if IMU_MOUNT_Y_UP
  // Proper right-handed rotation: sensor (X,Y,Z) -> body (X,Z,-Y).
  const float sy = y;
  y = z;
  z = -sy;
#endif
}

static void updateMpuBias(const MpuSample &s) {
  if (g_mpu_bias_ready) return;
  float ax = s.ax / Mpu6050::kAccelSens;
  float ay = s.ay / Mpu6050::kAccelSens;
  float az = s.az / Mpu6050::kAccelSens;
  float gx = (s.gx / Mpu6050::kGyroSens) * (PI / 180.0f);
  float gy = (s.gy / Mpu6050::kGyroSens) * (PI / 180.0f);
  float gz = (s.gz / Mpu6050::kGyroSens) * (PI / 180.0f);
  sensorToBody(ax, ay, az);
  sensorToBody(gx, gy, gz);
  if (g_mpu_bias_n == 0) g_mpu_bias_t0 = s.ts_us;
  g_mpu_bg_sum[0] += gx;
  g_mpu_bg_sum[1] += gy;
  g_mpu_bg_sum[2] += gz;
  g_mpu_ba_sum[0] += ax;
  g_mpu_ba_sum[1] += ay;
  g_mpu_ba_sum[2] += az;
  g_mpu_bias_n++;
  if ((s.ts_us - g_mpu_bias_t0) >= 2000000u && g_mpu_bias_n > 10) {
    for (int i = 0; i < 3; i++) {
      g_mpu_bg[i] = (float)(g_mpu_bg_sum[i] / (double)g_mpu_bias_n);
      g_mpu_ba[i] = (float)(g_mpu_ba_sum[i] / (double)g_mpu_bias_n);
    }
    // At rest in body x-forward/y-right/z-down, specific force is [0,0,-1g].
    // Subtract only sensor bias, never the gravity vector itself.
    g_mpu_ba[2] += 1.0f;
    g_mpu_bias_ready = true;
    Serial.printf("[mpu] bias ready n=%u bg=%.4f,%.4f,%.4f\n",
                  (unsigned)g_mpu_bias_n, g_mpu_bg[0], g_mpu_bg[1], g_mpu_bg[2]);
  }
}

static void fusionTxTask(void *) {
  uint32_t next_fused = micros();
  KppImuSample icm_acc[KPP_IMU_MAX_SAMPLES];
  KppImuSample mpu_acc[KPP_IMU_MAX_SAMPLES];
  size_t icm_acc_n = 0, mpu_acc_n = 0;
  uint32_t last_icm_flush = 0, last_mpu_flush = 0;

  bool have_mpu = false;
  float mpu_ax = 0, mpu_ay = 0, mpu_az = 0;
  float mpu_gx = 0, mpu_gy = 0, mpu_gz = 0;

  for (;;) {
    bool did_work = false;

    MpuMsg mm;
    while (xQueueReceive(mpuQ, &mm, 0) == pdTRUE) {
      did_work = true;
      updateMpuBias(mm.s);
      float ax = mm.s.ax / Mpu6050::kAccelSens;
      float ay = mm.s.ay / Mpu6050::kAccelSens;
      float az = mm.s.az / Mpu6050::kAccelSens;
      float gx = (mm.s.gx / Mpu6050::kGyroSens) * (PI / 180.0f);
      float gy = (mm.s.gy / Mpu6050::kGyroSens) * (PI / 180.0f);
      float gz = (mm.s.gz / Mpu6050::kGyroSens) * (PI / 180.0f);
      sensorToBody(ax, ay, az);
      sensorToBody(gx, gy, gz);
      if (g_mpu_bias_ready) {
        mpu_ax = ax - g_mpu_ba[0];
        mpu_ay = ay - g_mpu_ba[1];
        mpu_az = az - g_mpu_ba[2];
        mpu_gx = gx - g_mpu_bg[0];
        mpu_gy = gy - g_mpu_bg[1];
        mpu_gz = gz - g_mpu_bg[2];
      } else {
        mpu_ax = ax;
        mpu_ay = ay;
        mpu_az = az;
        mpu_gx = gx;
        mpu_gy = gy;
        mpu_gz = gz;
      }
      have_mpu = true;

      // ICM 死掉時用 MPU 餵 ESKF，避免融合整段停擺
      if (!g_icm_ok) {
        EskfImuIn in{};
        in.ts_us = mm.s.ts_us;
        in.ax_g = mpu_ax;
        in.ay_g = mpu_ay;
        in.az_g = mpu_az;
        in.gx_rps = mpu_gx;
        in.gy_rps = mpu_gy;
        in.gz_rps = mpu_gz;
        eskf.onImu(in);
        g_imu_fault = true;
        g_mpu_active = true;
        g_mpu_rejected = false;
      }

      if (mpu_acc_n < KPP_IMU_MAX_SAMPLES) {
        KppImuSample &s = mpu_acc[mpu_acc_n++];
        s.ts_us = mm.s.ts_us;
        s.ax = mm.s.ax;
        s.ay = mm.s.ay;
        s.az = mm.s.az;
        s.gx = mm.s.gx;
        s.gy = mm.s.gy;
        s.gz = mm.s.gz;
        s.temp = mm.s.temp;
      }
      if (mpu_acc_n >= KPP_IMU_MAX_SAMPLES) {
        // 只走 0x04；禁止把 MPU 鏡成 0x01（ICM 掛了就讓 ax 缺，不要造假）
        enqueueImuBatch(KPP_TYPE_MPU, mpu_acc, mpu_acc_n, g_tx_mpu);
        mpu_acc_n = 0;
        last_mpu_flush = micros();
      }
    }

    ImuMsg im;
    while (xQueueReceive(imuQ, &im, 0) == pdTRUE) {
      did_work = true;
      float ax = im.s.ax / Icm42688Fifo::kAccelSens;
      float ay = im.s.ay / Icm42688Fifo::kAccelSens;
      float az = im.s.az / Icm42688Fifo::kAccelSens;
      float gx = (im.s.gx / Icm42688Fifo::kGyroSens) * (PI / 180.0f);
      float gy = (im.s.gy / Icm42688Fifo::kGyroSens) * (PI / 180.0f);
      float gz = (im.s.gz / Icm42688Fifo::kGyroSens) * (PI / 180.0f);
      sensorToBody(ax, ay, az);
      sensorToBody(gx, gy, gz);

      bool fault = false;
      if (have_mpu && g_mpu_bias_ready &&
          eskf.phase() == EskfPhase::RUNNING) {
        const EskfOutput eo = eskf.output();
        const float icm_gx = gx - eo.bg[0];
        const float icm_gy = gy - eo.bg[1];
        const float icm_gz = gz - eo.bg[2];
        const float icm_ax = ax - eo.ba[0];
        const float icm_ay = ay - eo.ba[1];
        const float icm_az = az - eo.ba[2];

        const float dg = sqrtf((icm_gx - mpu_gx) * (icm_gx - mpu_gx) +
                               (icm_gy - mpu_gy) * (icm_gy - mpu_gy) +
                               (icm_gz - mpu_gz) * (icm_gz - mpu_gz));
        const float da = sqrtf((icm_ax - mpu_ax) * (icm_ax - mpu_ax) +
                               (icm_ay - mpu_ay) * (icm_ay - mpu_ay) +
                               (icm_az - mpu_az) * (icm_az - mpu_az));
        fault = (dg > DUAL_IMU_GYRO_MAX_RPS) || (da > DUAL_IMU_ACCEL_MAX_G);

#if DUAL_IMU_FIXED_BLEND_ENABLE
        if (!fault) {
          const float wi = DUAL_IMU_W_ICM;
          const float wm = DUAL_IMU_W_MPU;
          ax = wi * icm_ax + wm * mpu_ax + eo.ba[0];
          ay = wi * icm_ay + wm * mpu_ay + eo.ba[1];
          az = wi * icm_az + wm * mpu_az + eo.ba[2];
          gx = wi * icm_gx + wm * mpu_gx + eo.bg[0];
          gy = wi * icm_gy + wm * mpu_gy + eo.bg[1];
          gz = wi * icm_gz + wm * mpu_gz + eo.bg[2];
          g_mpu_active = true;
        }
#else
        // Until per-sensor Allan/noise covariance is calibrated, mixing the
        // noisier MPU into every ICM sample adds no defensible information.
        g_mpu_active = false;
#endif
        g_mpu_rejected = fault;
      }
      g_imu_fault = fault;

      EskfImuIn in{};
      in.ts_us = im.s.ts_us;
      in.ax_g = ax;
      in.ay_g = ay;
      in.az_g = az;
      in.gx_rps = gx;
      in.gy_rps = gy;
      in.gz_rps = gz;
      eskf.onImu(in);

      if (icm_acc_n < KPP_IMU_MAX_SAMPLES) {
        KppImuSample &s = icm_acc[icm_acc_n++];
        s.ts_us = im.s.ts_us;
        s.ax = im.s.ax;
        s.ay = im.s.ay;
        s.az = im.s.az;
        s.gx = im.s.gx;
        s.gy = im.s.gy;
        s.gz = im.s.gz;
        s.temp = im.s.temp;
      }
      if (icm_acc_n >= KPP_IMU_MAX_SAMPLES) {
        enqueueImuBatch(KPP_TYPE_IMU, icm_acc, icm_acc_n, g_tx_imu);
        icm_acc_n = 0;
        last_icm_flush = micros();
      }
    }

    const uint32_t now = micros();
    if (icm_acc_n > 0 && (now - last_icm_flush) > 10000u) {
      enqueueImuBatch(KPP_TYPE_IMU, icm_acc, icm_acc_n, g_tx_imu);
      icm_acc_n = 0;
      last_icm_flush = now;
    }
    if (mpu_acc_n > 0 && (now - last_mpu_flush) > 10000u) {
      enqueueImuBatch(KPP_TYPE_MPU, mpu_acc, mpu_acc_n, g_tx_mpu);
      mpu_acc_n = 0;
      last_mpu_flush = now;
    }

    GpsMsg gm;
    while (xQueueReceive(gpsQ, &gm, 0) == pdTRUE) {
      did_work = true;
      EskfGpsIn gin{};
      gin.lat_deg = gm.p.lat * 1e-7f;
      gin.lon_deg = gm.p.lon * 1e-7f;
      gin.height_m = gm.p.height * 1e-3f;
      gin.vn = gm.p.vel_n * 1e-3f;
      gin.ve = gm.p.vel_e * 1e-3f;
      gin.vd = gm.p.vel_d * 1e-3f;
      gin.g_speed = gm.p.g_speed * 1e-3f;
      gin.head_mot_deg = gm.p.head_mot * 1e-5f;
      gin.h_acc_m = gm.p.h_acc * 1e-3f;
      gin.v_acc_m = gm.p.v_acc * 1e-3f;
      gin.s_acc_mps = gm.p.s_acc * 1e-3f;
      gin.fix_type = gm.p.fix_type;
      gin.num_sv = gm.p.num_sv;
      eskf.onGps(gin);

 #if KPP_GPS_PACKET_V2
      KppGpsPayloadV2 gp{};
      gp.version = KPP_GPS_PAYLOAD_VERSION_2;
      gp.valid = gm.p.valid_flags;
      gp.year = gm.p.year;
      gp.month = gm.p.month;
      gp.day = gm.p.day;
      gp.hour = gm.p.hour;
      gp.minute = gm.p.minute;
      gp.second = gm.p.second;
      gp.fix_type = gm.p.fix_type;
      gp.num_sv = gm.p.num_sv;
      gp.flags = gm.p.flags;
      gp.flags2 = gm.p.flags2;
      gp.nano = gm.p.nano;
      gp.itow = gm.p.itow;
      gp.t_acc = gm.p.t_acc;
      gp.h_acc = gm.p.h_acc;
      gp.v_acc = gm.p.v_acc;
      gp.s_acc = gm.p.s_acc;
      gp.head_acc = gm.p.head_acc;
      gp.lat = gm.p.lat;
      gp.lon = gm.p.lon;
      gp.height = gm.p.height;
      gp.vel_n = gm.p.vel_n;
      gp.vel_e = gm.p.vel_e;
      gp.vel_d = gm.p.vel_d;
      gp.g_speed = gm.p.g_speed;
      gp.head_mot = gm.p.head_mot;
      gp.sensor_time_us = gm.p.sensor_time_us;
      gp.packet_seq = g_gps_packet_seq++;
      portENTER_CRITICAL(&g_pps_mux);
      gp.pps_time_us = g_pps_time_us;
      gp.pps_seq = g_pps_seq;
      portEXIT_CRITICAL(&g_pps_mux);
      gp.pps_age_ms =
          gp.pps_time_us > 0 && gp.sensor_time_us >= gp.pps_time_us
              ? (uint32_t)((gp.sensor_time_us - gp.pps_time_us) / 1000u)
              : UINT32_MAX;
 #else
      KppGpsPayload gp{};
      gp.itow = gm.p.itow;
      gp.lat = gm.p.lat;
      gp.lon = gm.p.lon;
      gp.height = gm.p.height;
      gp.vel_n = gm.p.vel_n;
      gp.vel_e = gm.p.vel_e;
      gp.vel_d = gm.p.vel_d;
      gp.g_speed = gm.p.g_speed;
      gp.head_mot = gm.p.head_mot;
      gp.h_acc = gm.p.h_acc;
      gp.v_acc = gm.p.v_acc;
      gp.s_acc = gm.p.s_acc;
      gp.num_sv = gm.p.num_sv;
      gp.fix_type = gm.p.fix_type;
 #endif
      g_last_gps_fix = gm.p.fix_type;
      g_last_gps_sv = gm.p.num_sv;
      enqueueFrame(KPP_TYPE_GPS, (const uint8_t *)&gp, sizeof(gp));
      g_tx_gps++;
    }

    if ((int32_t)(now - next_fused) >= 0) {
      did_work = true;
      next_fused += FUSED_PERIOD_US;
      const EskfOutput o = eskf.output();
      KppFusedPayload fp{};
      fp.ts_us = now;
      fp.lat = (int32_t)(o.lat_deg * 1e7f);
      fp.lon = (int32_t)(o.lon_deg * 1e7f);
      fp.height = (int32_t)(o.height_m * 1000.0f);
      fp.vel_n = sat_i16(o.vn * 100.0f);
      fp.vel_e = sat_i16(o.ve * 100.0f);
      fp.vel_d = sat_i16(o.vd * 100.0f);
      fp.yaw = sat_i16(o.yaw_deg * 100.0f);
      fp.pitch = sat_i16(o.pitch_deg * 100.0f);
      fp.roll = sat_i16(o.roll_deg * 100.0f);
      fp.pos_std_cm = (uint16_t)constrain(o.pos_std_m * 100.0f, 0.0f, 65535.0f);
      fp.flags = 0;
      if (o.valid) fp.flags |= KPP_FUSED_FLAG_INIT;
      if (o.zupt_active) fp.flags |= KPP_FUSED_FLAG_ZUPT;
      if (o.gps_valid) fp.flags |= KPP_FUSED_FLAG_GPS;
      if (g_imu_fault) fp.flags |= KPP_FUSED_FLAG_IMU_FAULT;
      if (g_mpu_active) fp.flags |= KPP_FUSED_FLAG_MPU_ACTIVE;
      if (g_mpu_rejected) fp.flags |= KPP_FUSED_FLAG_MPU_REJECTED;
      enqueueFrame(KPP_TYPE_FUSED, (const uint8_t *)&fp, sizeof(fp));
      g_tx_fused++;
    }

    if (!did_work) {
      vTaskDelay(1);
    } else {
      taskYIELD();
    }
  }
}

static void uploadTask(void *) {
  // HTTPS POST 會 block 數秒；勿讓 TWDT 因 IDLE 餓死而 reboot
  esp_task_wdt_delete(nullptr);

  // Static: an 8 KiB HTTPS body must not consume most of the task stack.
  static uint8_t batch[kBatchBytes];
  size_t batch_n = 0;
  uint32_t last_flush = millis();
  uint32_t last_diag = millis();
  uint8_t fail_streak = 0;

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      g_wifi_ok = false;
      batch_n = 0;
      fail_streak = 0;
      ensureWifi();
      delay(100);
      continue;
    }
    g_wifi_ok = true;

    while (batch_n < kBatchBytes) {
      const size_t room = kBatchBytes - batch_n;
      size_t got = 0;
      portENTER_CRITICAL(&uploadMux);
      // GPS 優先 drain
      got = gpsRing.read(batch + batch_n, room);
      if (got == 0) {
        got = imuRing.read(batch + batch_n, room);
      }
      portEXIT_CRITICAL(&uploadMux);
      if (got == 0) break;
      batch_n += got;
    }

    const uint32_t now = millis();
    const uint32_t flush_ms =
        ringSize() > ((kGpsRingCap + kImuRingCap) / 2) ? 10u : kFlushMs;
    const bool full = batch_n >= kBatchBytes;
    const bool timed = batch_n > 0 && (now - last_flush) >= flush_ms;
    if (full || timed) {
      bool ok = false;
#if TELEMETRY_USE_HTTP
      int http_code = -1;
      ok = sendBatchHttp(batch, batch_n, &http_code);
      g_last_http_code = http_code;
      if (!ok && (http_code < 0)) {
        WiFi.disconnect(true);
        g_wifi_ok = false;
      }
#else
      if (udp.beginPacket(SERVER_IP, SERVER_PORT)) {
        udp.write(batch, batch_n);
        ok = udp.endPacket();
      }
#endif
      if (ok) {
        fail_streak = 0;
        g_up_bytes += (uint32_t)batch_n;
        g_tx_blink = true;
        batch_n = 0;
        last_flush = now;
      } else {
        g_up_fail++;
        fail_streak++;
#if TELEMETRY_USE_HTTP
        if (g_up_fail <= 3 || (g_up_fail % 20) == 0) {
          Serial.printf("[upload] HTTP fail code=%d batch=%u fail=%u streak=%u\n",
                        g_last_http_code, (unsigned)batch_n, (unsigned)g_up_fail,
                        (unsigned)fail_streak);
        }
#endif
        if (fail_streak >= 3) {
          // 丟棄卡住的 batch；只清 IMU ring，GPS ring 保留
          batch_n = 0;
          fail_streak = 0;
          portENTER_CRITICAL(&uploadMux);
          if (imuRing.size() > (kImuRingCap * 3 / 4)) {
            imuRing.clear();
            g_ring_drop++;
          }
          portEXIT_CRITICAL(&uploadMux);
        }
        delay(50);
        last_flush = now;
      }
    }

    if (now - last_diag >= 1000) {
      last_diag = now;
      printDiag();
    }

    vTaskDelay(1);
  }
}

static void printDiag() {
  const EskfOutput o = eskf.output();
  const char *ph = "?";
  switch (o.phase) {
    case EskfPhase::CALIBRATING:
      ph = "CALIB";
      break;
    case EskfPhase::WAIT_GPS:
      ph = "WAIT_GPS";
      break;
    case EskfPhase::RUNNING:
      ph = "RUN";
      break;
  }
  Serial.printf(
      "[eskf] %s init=%d zupt=%d gps=%d fault=%d pos_std=%.2fm "
      "innov_p=%.2f innov_v=%.2f bg=%.4f,%.4f,%.4f zupt_n=%u\n",
      ph, (int)o.valid, (int)o.zupt_active, (int)o.gps_valid, (int)g_imu_fault,
      o.pos_std_m, o.innov_pos_m, o.innov_vel_mps, o.bg[0], o.bg[1], o.bg[2],
      (unsigned)o.zupt_count);
  Serial.printf(
      "[tx] icm=%u mpu=%u gps=%u fused=%u drop_i=%u drop_m=%u imu_ovf=%u "
      "gps_ovf=%u ring_g=%u ring_i=%u up_B=%u up_fail=%u http=%d wifi=%d "
      "icm_ok=%d reinit=%u\n",
      (unsigned)g_tx_imu, (unsigned)g_tx_mpu, (unsigned)g_tx_gps,
      (unsigned)g_tx_fused, (unsigned)g_drop_imu, (unsigned)g_drop_mpu,
      (unsigned)g_ring_drop, (unsigned)g_gps_ring_drop,
      (unsigned)gpsRing.size(), (unsigned)imuRing.size(), (unsigned)g_up_bytes,
      (unsigned)g_up_fail, g_last_http_code, (int)g_wifi_ok, (int)g_icm_ok,
      (unsigned)g_icm_reinit_n);
  const uint32_t gps_rx_bytes = gps.takeRxBytes();
  KppDbgPayloadV2 db{};
  db.version = KPP_DBG_PAYLOAD_VERSION_2;
  db.gps_rx_bps = gps_rx_bytes;
  db.pvt_hz = (uint8_t)(g_tx_gps > 255 ? 255 : g_tx_gps);
  db.fix_type = g_last_gps_fix;
  db.num_sv = g_last_gps_sv;
  db.gps_queue_drops = g_drop_gps;
  db.imu_queue_drops = g_drop_imu;
  db.mpu_queue_drops = g_drop_mpu;
  db.ring_drops = g_ring_drop;
  db.gps_ring_drops = g_gps_ring_drop;
  db.gps_parser_errors = gps.takeParserErrors();
  db.gps_uart_overflows = 0;  // Arduino HardwareSerial exposes no overflow count.
  enqueueFrame(KPP_TYPE_DBG, (const uint8_t *)&db, sizeof(db));
  g_tx_imu = g_tx_mpu = g_tx_gps = g_tx_fused = 0;
  g_drop_imu = g_drop_mpu = g_drop_gps = g_ring_drop = 0;
  g_gps_ring_drop = 0;
  g_up_bytes = 0;
  g_up_fail = 0;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  delay(200);
#if TELEMETRY_USE_HTTP
  Serial.printf("[sensor_node] boot HTTP → %s id=%s\n", INGEST_FRAME_URL,
                DEVICE_ID);
#else
  Serial.printf("[sensor_node] boot UDP → %s:%d id=%s\n", SERVER_IP, SERVER_PORT,
                DEVICE_ID);
#endif

  g_icm_ok = icm.begin();
  if (!g_icm_ok) {
    Serial.println("[sensor_node] ICM init FAIL — 會持續 reinit；暫用 MPU→ESKF");
  } else {
    g_last_icm_sample_us = micros();
  }
  g_mpu_ok = mpu.begin();
  if (!g_mpu_ok) {
    Serial.println("[sensor_node] MPU init FAIL — continuing ICM-only");
  }
  if (!g_icm_ok && !g_mpu_ok) {
    Serial.println("[sensor_node] FATAL: no IMU available");
  }
  gps.configure(PIN_GPS_RX, PIN_GPS_TX);
#if GNSS_PPS_PIN >= 0
  pinMode(GNSS_PPS_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(GNSS_PPS_PIN), onGnssPps,
                  GNSS_PPS_EDGE_FALLING ? FALLING : RISING);
  Serial.printf("[gps] TIMEPULSE capture GPIO%d %s enabled\n", GNSS_PPS_PIN,
                GNSS_PPS_EDGE_FALLING ? "falling" : "rising");
#else
  Serial.println("[gps] TIMEPULSE capture disabled (GNSS_PPS_PIN=-1)");
#endif

  imuQ = xQueueCreate(64, sizeof(ImuMsg));
  mpuQ = xQueueCreate(32, sizeof(MpuMsg));
  gpsQ = xQueueCreate(8, sizeof(GpsMsg));
  eskf.reset();

  ensureWifi();

  xTaskCreatePinnedToCore(sensorTask, "sensor", 8192, nullptr, 3, nullptr, 0);
#if KPP_LEGACY_TASK_PRIORITIES
  xTaskCreatePinnedToCore(uploadTask, "upload", 12288, nullptr, 3, nullptr, 1);
  xTaskCreatePinnedToCore(fusionTxTask, "fusion", 12288, nullptr, 2, nullptr, 1);
#else
  // TLS may remain runnable for hundreds of ms.  Fusion must drain sensor
  // queues first or the old priority order loses most IMU samples.
  xTaskCreatePinnedToCore(uploadTask, "upload", 12288, nullptr, 2, nullptr, 1);
  xTaskCreatePinnedToCore(fusionTxTask, "fusion", 12288, nullptr, 4, nullptr, 1);
#endif

  Serial.println("[sensor_node] tasks up — cmds: d=diag s=status r=reset");
}

void loop() {
  static uint32_t last = 0;
  static bool led_on = false;

  kppOtaLoop();

  if (!g_wifi_ok) {
    digitalWrite(PIN_LED, LOW);
  } else if (g_tx_blink) {
    g_tx_blink = false;
    led_on = !led_on;
    digitalWrite(PIN_LED, led_on ? HIGH : LOW);
  } else {
    digitalWrite(PIN_LED, HIGH);
  }

  if (millis() - last >= 1000) {
    last = millis();
    if (WiFi.status() != WL_CONNECTED) ensureWifi();
  }
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == 'r' || c == 'R') {
      eskf.reset();
      g_mpu_bias_ready = false;
      g_mpu_bias_n = 0;
      for (int i = 0; i < 3; i++) {
        g_mpu_bg_sum[i] = g_mpu_ba_sum[i] = 0;
        g_mpu_bg[i] = g_mpu_ba[i] = 0;
      }
      g_imu_fault = false;
      Serial.println("[eskf] reset");
    } else if (c == 'd' || c == 'D' || c == 's' || c == 'S') {
      printDiag();
      const EskfOutput o = eskf.output();
      Serial.printf("[eskf] lat=%.7f lon=%.7f h=%.2f yaw=%.1f\n", o.lat_deg,
                    o.lon_deg, o.height_m, o.yaw_deg);
    }
  }
}
