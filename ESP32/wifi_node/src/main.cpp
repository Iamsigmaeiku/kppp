/**
 * Board #2 wifi_node: UART2 RX framed packets → WiFi upload (HTTP or UDP).
 * No sensors.
 */
#include <Arduino.h>
#include <WiFi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "ByteRing.h"
#include "packet.h"
#include "secrets.h"

#if TELEMETRY_USE_HTTP
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#else
#include <WiFiUdp.h>
#endif

static constexpr int PIN_LINK_RX = 16;
static constexpr int PIN_LINK_TX = 17;
static constexpr int PIN_LED = 2;
static constexpr uint32_t LINK_BAUD = 921600;
static constexpr size_t kRingCap = 16384;
static constexpr size_t kBatchMtu = 1400;
static constexpr uint32_t kFlushMs = 20;

static HardwareSerial LinkSerial(2);

#if TELEMETRY_USE_HTTP
static WiFiClientSecure tls;
#else
static WiFiUDP udp;
#endif

static uint8_t ringStorage[kRingCap];
static ByteRing ring(ringStorage, kRingCap);
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

static volatile uint32_t g_rx_ok = 0, g_crc_err = 0, g_tx_bytes = 0;
static volatile uint32_t g_imu = 0, g_gps = 0, g_fused = 0, g_mpu = 0;
static volatile uint16_t g_dbg_gps_rx_bps = 0;
static volatile uint8_t g_dbg_pvt_hz = 0, g_dbg_fix_type = 0, g_dbg_num_sv = 0;
static volatile bool g_wifi_ok = false;
static volatile bool g_tx_blink = false;

static void ringWrite(const uint8_t *data, size_t n) {
  portENTER_CRITICAL(&ringMux);
  ring.write(data, n);
  portEXIT_CRITICAL(&ringMux);
}

static size_t ringRead(uint8_t *out, size_t n) {
  portENTER_CRITICAL(&ringMux);
  const size_t got = ring.read(out, n);
  portEXIT_CRITICAL(&ringMux);
  return got;
}

static size_t ringSize() {
  portENTER_CRITICAL(&ringMux);
  const size_t s = ring.size();
  portEXIT_CRITICAL(&ringMux);
  return s;
}

static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    g_wifi_ok = true;
    return;
  }
  g_wifi_ok = false;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  const uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) {
    delay(50);
  }
  g_wifi_ok = (WiFi.status() == WL_CONNECTED);
  if (g_wifi_ok) {
    Serial.printf("[wifi] ok %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[wifi] connect fail — retry later");
  }
}

#if TELEMETRY_USE_HTTP
static bool sendBatchHttp(const uint8_t *data, size_t n) {
  if (n == 0) return false;
  tls.setInsecure();
  HTTPClient http;
  if (!http.begin(tls, INGEST_FRAME_URL)) {
    return false;
  }
  http.setTimeout(8000);
  http.addHeader("Content-Type", "application/octet-stream");
  http.addHeader("Authorization", "Bearer " INGEST_TOKEN);
  const int code = http.POST((uint8_t *)data, n);
  http.end();
  return code >= 200 && code < 300;
}
#endif

static void uartTask(void *) {
  KppParser parser;
  uint8_t frame[KPP_MAX_FRAME];
  for (;;) {
    while (LinkSerial.available()) {
      const uint8_t b = (uint8_t)LinkSerial.read();
      size_t flen = 0;
      const int rc = parser.feed(b, frame, &flen);
      if (rc < 0) {
        g_crc_err++;
        continue;
      }
      if (rc == 1 && flen > 0) {
        g_rx_ok++;
        const uint8_t type = frame[2];
        if (type == KPP_TYPE_IMU) g_imu++;
        else if (type == KPP_TYPE_GPS) g_gps++;
        else if (type == KPP_TYPE_FUSED) g_fused++;
        else if (type == KPP_TYPE_MPU) g_mpu++;
        else if (type == KPP_TYPE_DBG && flen >= (4 + sizeof(KppDbgPayload) + 2)) {
          const KppDbgPayload *db =
              reinterpret_cast<const KppDbgPayload *>(frame + 4);
          g_dbg_gps_rx_bps = db->gps_rx_bps;
          g_dbg_pvt_hz = db->pvt_hz;
          g_dbg_fix_type = db->fix_type;
          g_dbg_num_sv = db->num_sv;
        }

#if TELEMETRY_USE_HTTP
        // HTTPS 吞吐遠低於 921600 UART：優先 GPS/FUSED/DBG，IMU/MPU 抽樣上傳
        if (type == KPP_TYPE_IMU) {
          static uint8_t icm_skip = 0;
          if (++icm_skip < 10) continue;  // ~1/10
          icm_skip = 0;
        } else if (type == KPP_TYPE_MPU) {
          static uint8_t mpu_skip = 0;
          if (++mpu_skip < 5) continue;  // ~1/5
          mpu_skip = 0;
        }
#endif
        // ring 快滿時仍優先保證 GPS / fused / dbg
        if (ringSize() > (kRingCap * 3 / 4) &&
            (type == KPP_TYPE_IMU || type == KPP_TYPE_MPU)) {
          continue;
        }
        ringWrite(frame, flen);
      }
    }
    taskYIELD();
  }
}

static void uploadTask(void *) {
  uint8_t batch[kBatchMtu];
  size_t batch_n = 0;
  uint32_t last_flush = millis();

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      g_wifi_ok = false;
      ensureWifi();
      delay(100);
      continue;
    }
    g_wifi_ok = true;

    while (batch_n < kBatchMtu) {
      const size_t room = kBatchMtu - batch_n;
      const size_t got = ringRead(batch + batch_n, room);
      if (got == 0) break;
      batch_n += got;
    }

    const uint32_t now = millis();
    const bool full = batch_n >= kBatchMtu;
    const bool timed = batch_n > 0 && (now - last_flush) >= kFlushMs;
    if (full || timed) {
      bool ok = false;
#if TELEMETRY_USE_HTTP
      ok = sendBatchHttp(batch, batch_n);
#else
      if (udp.beginPacket(SERVER_IP, SERVER_PORT)) {
        udp.write(batch, batch_n);
        ok = udp.endPacket();
      }
#endif
      if (ok) {
        g_tx_bytes += (uint32_t)batch_n;
        g_tx_blink = true;
      }
      batch_n = 0;
      last_flush = now;
    }
    taskYIELD();
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  delay(200);
#if TELEMETRY_USE_HTTP
  Serial.printf("[wifi_node] boot HTTP → %s id=%s\n", INGEST_FRAME_URL, DEVICE_ID);
#else
  Serial.printf("[wifi_node] boot UDP → %s:%d id=%s\n", SERVER_IP, SERVER_PORT,
                DEVICE_ID);
#endif

  LinkSerial.begin(LINK_BAUD, SERIAL_8N1, PIN_LINK_RX, PIN_LINK_TX);
  ensureWifi();

  xTaskCreatePinnedToCore(uartTask, "uart", 4096, nullptr, 3, nullptr, 0);
  xTaskCreatePinnedToCore(uploadTask, "upload", 12288, nullptr, 2, nullptr, 1);
}

void loop() {
  static uint32_t last = 0;
  static uint32_t last_imu = 0, last_gps = 0, last_fused = 0, last_mpu = 0;
  static bool led_on = false;

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
    const uint32_t imu = g_imu, gps = g_gps, fused = g_fused, mpu = g_mpu;
    Serial.printf(
        "[stat] rx_ok=%u crc_err=%u tx_B=%u icm_Hz=%u mpu_Hz=%u gps_Hz=%u "
        "fused_Hz=%u ring=%u wifi=%d gps_uartB=%u pvt=%u fix=%u sv=%u\n",
        (unsigned)g_rx_ok, (unsigned)g_crc_err, (unsigned)g_tx_bytes,
        (unsigned)(imu - last_imu), (unsigned)(mpu - last_mpu),
        (unsigned)(gps - last_gps), (unsigned)(fused - last_fused),
        (unsigned)ringSize(), (int)g_wifi_ok, (unsigned)g_dbg_gps_rx_bps,
        (unsigned)g_dbg_pvt_hz, (unsigned)g_dbg_fix_type, (unsigned)g_dbg_num_sv);
    last_imu = imu;
    last_mpu = mpu;
    last_gps = gps;
    last_fused = fused;
    g_rx_ok = 0;
    g_crc_err = 0;
    g_tx_bytes = 0;

    if (WiFi.status() != WL_CONNECTED) ensureWifi();
  }
}
