/**
 * Board #1 sensor_node: ICM42688 + MPU6050 + M10 + 15-state ESKF.
 * Core0: sensors. Core1: ESKF + UART2 TX. NO WiFi.
 *
 * UART budget @921600 (~90 KB/s usable):
 *   0x01 ICM FIFO ~1 kHz batched×5 ≈ 20 KB/s
 *   0x04 MPU 200 Hz batched×5 ≈ 4 KB/s
 *   0x02 GPS 10 Hz ≈ 0.6 KB/s
 *   0x03 fused 50 Hz ≈ 2 KB/s
 *   total ≈ 27 KB/s — OK with multi-sample packing.
 */
#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <math.h>
#include <string.h>

#include "ByteRing.h"
#include "GpsImuEskf.h"
#include "Icm42688Fifo.h"
#include "Mpu6050.h"
#include "UbxM10.h"
#include "packet.h"

static constexpr int PIN_GPS_RX = 16;
static constexpr int PIN_GPS_TX = 17;
static constexpr int PIN_LINK_TX = 25;
static constexpr int PIN_LINK_RX = 26;
static constexpr uint32_t LINK_BAUD = 921600;
static constexpr uint32_t SENSOR_PERIOD_US = 5000;  // 200 Hz
static constexpr uint32_t FUSED_PERIOD_US = 20000;  // 50 Hz

/** Dual-IMU blend: ICM dominates (lower noise density). */
#ifndef DUAL_IMU_W_ICM
#define DUAL_IMU_W_ICM 0.85f
#endif
#ifndef DUAL_IMU_W_MPU
#define DUAL_IMU_W_MPU 0.15f
#endif
/** Consistency gates (phys units, after bias). */
#ifndef DUAL_IMU_GYRO_MAX_RPS
#define DUAL_IMU_GYRO_MAX_RPS 0.50f
#endif
#ifndef DUAL_IMU_ACCEL_MAX_G
#define DUAL_IMU_ACCEL_MAX_G 0.60f
#endif

static HardwareSerial GpsSerial(1);
static HardwareSerial LinkSerial(2);
static Icm42688Fifo icm;
static Mpu6050 mpu;
static UbxM10 gps(GpsSerial);
static GpsImuEskf eskf;

static constexpr size_t kUartRingCap = 12288;
static uint8_t uartRingStorage[kUartRingCap];
static ByteRing uartRing(uartRingStorage, kUartRingCap);
static portMUX_TYPE uartMux = portMUX_INITIALIZER_UNLOCKED;

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
static volatile uint8_t g_last_gps_fix = 0, g_last_gps_sv = 0;
static volatile uint32_t g_drop_imu = 0, g_drop_mpu = 0, g_ring_drop = 0;
static volatile bool g_imu_fault = false;
static volatile bool g_mpu_ok = false;

// MPU static bias from 2 s window (parallel to ESKF calib)
static float g_mpu_bg[3] = {0, 0, 0};
static float g_mpu_ba[3] = {0, 0, 0};
static bool g_mpu_bias_ready = false;
static double g_mpu_bg_sum[3] = {0, 0, 0};
static double g_mpu_ba_sum[3] = {0, 0, 0};
static uint32_t g_mpu_bias_n = 0;
static uint32_t g_mpu_bias_t0 = 0;

static void ringWrite(const uint8_t *data, size_t n) {
  portENTER_CRITICAL(&uartMux);
  const size_t before = uartRing.free();
  uartRing.write(data, n);
  if (before < n) g_ring_drop++;
  portEXIT_CRITICAL(&uartMux);
}

static void enqueueFrame(uint8_t type, const uint8_t *payload, uint8_t len) {
  uint8_t frame[KPP_MAX_FRAME];
  const size_t n = kpp_frame_encode(type, payload, len, frame, sizeof(frame));
  if (n) ringWrite(frame, n);
}

static void enqueueImuBatch(uint8_t type, const KppImuSample *samples, size_t n,
                            volatile uint32_t &counter) {
  if (n == 0) return;
  uint8_t payload[1 + KPP_IMU_MAX_SAMPLES * sizeof(KppImuSample)];
  payload[0] = (uint8_t)n;
  memcpy(payload + 1, samples, n * sizeof(KppImuSample));
  enqueueFrame(type, payload, (uint8_t)(1 + n * sizeof(KppImuSample)));
  counter += (uint32_t)n;
}

static void sensorTask(void *) {
  IcmFifoSample batch[KPP_IMU_MAX_SAMPLES];
  uint32_t next = micros();
  for (;;) {
    const uint32_t now = micros();
    if ((int32_t)(now - next) < 0) {
      taskYIELD();
      continue;
    }
    next += SENSOR_PERIOD_US;
    if ((int32_t)(now - next) > (int32_t)(SENSOR_PERIOD_US * 4)) {
      next = now + SENSOR_PERIOD_US;
    }

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

    // MPU6050: one sample per 200 Hz tick, own timestamp
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
      }
    }
  }
}

static int16_t sat_i16(float v) {
  if (v > 32767.0f) return 32767;
  if (v < -32768.0f) return -32768;
  return (int16_t)v;
}

static void updateMpuBias(const MpuSample &s) {
  if (g_mpu_bias_ready) return;
  const float ax = s.ax / Mpu6050::kAccelSens;
  const float ay = s.ay / Mpu6050::kAccelSens;
  const float az = s.az / Mpu6050::kAccelSens;
  const float gx = (s.gx / Mpu6050::kGyroSens) * (PI / 180.0f);
  const float gy = (s.gy / Mpu6050::kGyroSens) * (PI / 180.0f);
  const float gz = (s.gz / Mpu6050::kGyroSens) * (PI / 180.0f);
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
    // accel bias = mean - gravity direction: keep horizontal axes only approx
    // For blend we subtract full mean accel and re-add unit gravity later via ESKF;
    // store mean as ba so (a - ba) ≈ 0 at rest on each axis relative to calib pose.
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

  // Latest MPU in physical units (bias-corrected when ready)
  bool have_mpu = false;
  float mpu_ax = 0, mpu_ay = 0, mpu_az = 0;
  float mpu_gx = 0, mpu_gy = 0, mpu_gz = 0;

  for (;;) {
    MpuMsg mm;
    while (xQueueReceive(mpuQ, &mm, 0) == pdTRUE) {
      updateMpuBias(mm.s);
      const float ax = mm.s.ax / Mpu6050::kAccelSens;
      const float ay = mm.s.ay / Mpu6050::kAccelSens;
      const float az = mm.s.az / Mpu6050::kAccelSens;
      const float gx = (mm.s.gx / Mpu6050::kGyroSens) * (PI / 180.0f);
      const float gy = (mm.s.gy / Mpu6050::kGyroSens) * (PI / 180.0f);
      const float gz = (mm.s.gz / Mpu6050::kGyroSens) * (PI / 180.0f);
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
        enqueueImuBatch(KPP_TYPE_MPU, mpu_acc, mpu_acc_n, g_tx_mpu);
        mpu_acc_n = 0;
        last_mpu_flush = micros();
      }
    }

    ImuMsg im;
    while (xQueueReceive(imuQ, &im, 0) == pdTRUE) {
      float ax = im.s.ax / Icm42688Fifo::kAccelSens;
      float ay = im.s.ay / Icm42688Fifo::kAccelSens;
      float az = im.s.az / Icm42688Fifo::kAccelSens;
      float gx = (im.s.gx / Icm42688Fifo::kGyroSens) * (PI / 180.0f);
      float gy = (im.s.gy / Icm42688Fifo::kGyroSens) * (PI / 180.0f);
      float gz = (im.s.gz / Icm42688Fifo::kGyroSens) * (PI / 180.0f);

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

        // Bias-corrected weighted average, then restore ICM ba/bg for onImu
        const float wi = DUAL_IMU_W_ICM;
        const float wm = DUAL_IMU_W_MPU;
        ax = wi * icm_ax + wm * mpu_ax + eo.ba[0];
        ay = wi * icm_ay + wm * mpu_ay + eo.ba[1];
        az = wi * icm_az + wm * mpu_az + eo.ba[2];
        gx = wi * icm_gx + wm * mpu_gx + eo.bg[0];
        gy = wi * icm_gy + wm * mpu_gy + eo.bg[1];
        gz = wi * icm_gz + wm * mpu_gz + eo.bg[2];
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
      g_last_gps_fix = gm.p.fix_type;
      g_last_gps_sv = gm.p.num_sv;
      enqueueFrame(KPP_TYPE_GPS, (const uint8_t *)&gp, sizeof(gp));
      g_tx_gps++;
    }

    if ((int32_t)(now - next_fused) >= 0) {
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
      enqueueFrame(KPP_TYPE_FUSED, (const uint8_t *)&fp, sizeof(fp));
      g_tx_fused++;
    }

    uint8_t chunk[256];
    size_t avail = 0;
    portENTER_CRITICAL(&uartMux);
    avail = uartRing.size();
    portEXIT_CRITICAL(&uartMux);
    while (avail > 0 && LinkSerial.availableForWrite() > 0) {
      const size_t room = (size_t)LinkSerial.availableForWrite();
      const size_t take = room < sizeof(chunk) ? room : sizeof(chunk);
      size_t n = 0;
      portENTER_CRITICAL(&uartMux);
      n = uartRing.read(chunk, take);
      avail = uartRing.size();
      portEXIT_CRITICAL(&uartMux);
      if (n == 0) break;
      LinkSerial.write(chunk, n);
    }

    taskYIELD();
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
      "[tx] icm=%u mpu=%u gps=%u fused=%u drop_i=%u drop_m=%u ring_ovf=%u "
      "ring=%u\n",
      (unsigned)g_tx_imu, (unsigned)g_tx_mpu, (unsigned)g_tx_gps,
      (unsigned)g_tx_fused, (unsigned)g_drop_imu, (unsigned)g_drop_mpu,
      (unsigned)g_ring_drop, (unsigned)uartRing.size());
  const uint32_t gps_rx_bytes = gps.takeRxBytes();
  KppDbgPayload db{};
  db.gps_rx_bps = (uint16_t)(gps_rx_bytes > 65535 ? 65535 : gps_rx_bytes);
  db.pvt_hz = (uint8_t)(g_tx_gps > 255 ? 255 : g_tx_gps);
  db.fix_type = g_last_gps_fix;
  db.num_sv = g_last_gps_sv;
  enqueueFrame(KPP_TYPE_DBG, (const uint8_t *)&db, sizeof(db));
  g_tx_imu = g_tx_mpu = g_tx_gps = g_tx_fused = 0;
  g_drop_imu = g_drop_mpu = g_ring_drop = 0;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("[sensor_node] boot (ICM+MPU+M10)");

  if (!icm.begin()) {
    Serial.println("[sensor_node] ICM init FAIL");
  }
  g_mpu_ok = mpu.begin();
  if (!g_mpu_ok) {
    Serial.println("[sensor_node] MPU init FAIL — continuing ICM-only");
  }
  gps.configure(PIN_GPS_RX, PIN_GPS_TX);
  LinkSerial.begin(LINK_BAUD, SERIAL_8N1, PIN_LINK_RX, PIN_LINK_TX);

  imuQ = xQueueCreate(64, sizeof(ImuMsg));
  mpuQ = xQueueCreate(32, sizeof(MpuMsg));
  gpsQ = xQueueCreate(8, sizeof(GpsMsg));
  eskf.reset();

  xTaskCreatePinnedToCore(sensorTask, "sensor", 8192, nullptr, 3, nullptr, 0);
  xTaskCreatePinnedToCore(fusionTxTask, "fusion", 12288, nullptr, 2, nullptr, 1);

  Serial.println("[sensor_node] tasks up — cmds: d=diag s=status r=reset");
}

void loop() {
  static uint32_t last = 0;
  if (millis() - last >= 1000) {
    last = millis();
    printDiag();
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
