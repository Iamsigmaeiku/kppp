/**
 * ESP32-WROOM-32 → HTTP ingest
 *
 * Profiles:
 *   (default)         ICM-42688-P + DHT11 + NEO-6M GPS
 *   PROFILE_GPS_HALL  霍爾(GPIO36/ADC1_0) + NEO-6M GPS（第二顆 esp32-kart-02）
 *
 * Pins:
 *   ICM42688: 3V3, GND, SDA=GPIO21, SCL=GPIO22
 *   DHT11:    VCC, GND, DATA=GPIO15
 *   Hall:     Vcc=3V3, GND, S=GPIO36 (ADC1_CH0)
 *   NEO-6M:   VCC(3V3/5V), GND, TXD→GPIO16(UART1 RX), RXD→GPIO17(UART1 TX)
 *             不要接 GPIO1/3 — USB 燒錄腳
 *
 * Offline: RAM ring；NTP 非阻塞；採樣在 core1，HTTP 在 loop（core0）。
 */

#include <Arduino.h>
#ifndef PROFILE_GPS_HALL
#include <Wire.h>
#include <DHT.h>
#endif
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TinyGPSPlus.h>
#include <math.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <secrets.h>
#ifndef PROFILE_GPS_HALL
#include "DeadReckoner.h"
#endif

// HTTPS + 大 JSON 在 loop 預設 8KB stack 會爆 canary；拉高再跑 postBatch
#ifdef PROFILE_GPS_HALL
SET_LOOP_TASK_STACK_SIZE(12 * 1024);  // GPS+Hall JSON 較小，留 DRAM 給 WiFi
#else
SET_LOOP_TASK_STACK_SIZE(24 * 1024);
#endif

#ifndef WIFI_SSID2
#define WIFI_SSID2 WIFI_SSID
#define WIFI_PASS2 WIFI_PASS
#endif

// ---- Pins ----
#ifndef PROFILE_GPS_HALL
static constexpr int PIN_SDA = 21;
static constexpr int PIN_SCL = 22;
static constexpr int PIN_DHT = 15;
#endif
static constexpr int PIN_HALL = 36;  // ADC1_CH0 / VP
static constexpr int PIN_GPS_RX = 16;  // ESP32 RX ← NEO-6M TXD
static constexpr int PIN_GPS_TX = 17;  // ESP32 TX → NEO-6M RXD
static constexpr uint32_t GPS_BAUD = 9600;
static constexpr uint32_t GPS_FIX_STALE_MS = 5000;

// 數位霍爾：ADC 滯回（未知模組先當開關型；raw 一併上報）
static constexpr int HALL_ADC_HIGH = 2500;
static constexpr int HALL_ADC_LOW = 1500;
static constexpr uint32_t HALL_HZ_WINDOW_MS = 1000;
static constexpr size_t HALL_PULSE_CAP = 64;

#ifndef PROFILE_GPS_HALL
// ---- ICM-42688-P (Bank 0) ----
static constexpr uint8_t ICM_ADDR_LOW = 0x68;
static constexpr uint8_t ICM_ADDR_HIGH = 0x69;
static constexpr uint8_t ICM_WHO_AM_I_REG = 0x75;
static constexpr uint8_t ICM_WHO_AM_I_VAL = 0x47;
static constexpr uint8_t ICM_PWR_MGMT0 = 0x4E;
static constexpr uint8_t ICM_GYRO_CONFIG0 = 0x4F;
static constexpr uint8_t ICM_ACCEL_CONFIG0 = 0x50;
static constexpr uint8_t ICM_TEMP_DATA1 = 0x1D;

static constexpr float ACCEL_SENS = 2048.0f;
static constexpr float GYRO_SENS = 16.4f;
#endif

static constexpr uint32_t IMU_PERIOD_MS = 40;  // 25 Hz
static constexpr uint32_t POST_PERIOD_MS = 400;
static constexpr uint32_t POST_BURST_MS = 80;
#ifndef PROFILE_GPS_HALL
static constexpr uint32_t DHT_PERIOD_MS = 2000;
#endif
static constexpr uint32_t NTP_RESYNC_MS = 3600000;
static constexpr uint32_t NTP_RETRY_MS = 15000;
static constexpr size_t POST_CHUNK = 10;
#ifdef PROFILE_GPS_HALL
static constexpr size_t RING_CAP = 150;  // ~6s @25Hz；省 DRAM 給 WiFi/TLS
#else
static constexpr size_t RING_CAP = 900;  // ~36s @25Hz
#endif

#ifndef PROFILE_GPS_HALL
static DHT dht(PIN_DHT, DHT11);
static uint8_t icmAddr = ICM_ADDR_LOW;
static DeadReckoner deadReckoner;
#endif

static HardwareSerial gpsSerial(1);
static TinyGPSPlus gps;

struct Sample {
#ifdef PROFILE_GPS_HALL
  float gps_lat, gps_lon, gps_speed_mps, gps_course_deg, gps_alt_m, gps_hdop;
  uint32_t gps_satellites;
  bool has_gps;
  int hall_adc;
  float hall_hz;
  bool has_hall;
  uint32_t millis_at;
#else
  float ax, ay, az, gx, gy, gz, imu_temp_c, accel_mag;
  float dht_temp_c, dht_humidity;
  bool has_dht;
  float gps_lat, gps_lon, gps_speed_mps, gps_course_deg, gps_alt_m, gps_hdop;
  uint32_t gps_satellites;
  bool has_gps;
  float lat_dr, lon_dr, heading_dr_deg, speed_dr_mps;
  bool has_dr;
  uint32_t millis_at;
#endif
};

struct GpsFix {
  float lat, lon, speed_mps, course_deg, alt_m, hdop;
  uint32_t satellites;
  uint32_t updated_at_ms;
  bool valid;
};

static volatile GpsFix gpsFix{};
static portMUX_TYPE gpsMux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t gpsSentenceCount = 0;
static char gpsLastSentence[128];
static portMUX_TYPE gpsSentMux = portMUX_INITIALIZER_UNLOCKED;

static volatile int lastHallAdc = 0;
static volatile float lastHallHz = 0.0f;
static bool hallWasHigh = false;
static uint32_t hallPulseMs[HALL_PULSE_CAP];
static size_t hallPulseHead = 0;
static size_t hallPulseCount = 0;
static portMUX_TYPE hallMux = portMUX_INITIALIZER_UNLOCKED;

static void hallUpdate() {
  const int adc = analogRead(PIN_HALL);
  const uint32_t now = millis();

  portENTER_CRITICAL(&hallMux);
  lastHallAdc = adc;

  if (!hallWasHigh && adc >= HALL_ADC_HIGH) {
    hallWasHigh = true;
    hallPulseMs[hallPulseHead] = now;
    hallPulseHead = (hallPulseHead + 1) % HALL_PULSE_CAP;
    if (hallPulseCount < HALL_PULSE_CAP) hallPulseCount++;
  } else if (hallWasHigh && adc <= HALL_ADC_LOW) {
    hallWasHigh = false;
  }

  // 滑窗：過去 HALL_HZ_WINDOW_MS 內脈衝數 → Hz
  size_t n = 0;
  for (size_t i = 0; i < hallPulseCount; i++) {
    const size_t idx =
        (hallPulseHead + HALL_PULSE_CAP - hallPulseCount + i) % HALL_PULSE_CAP;
    if (now - hallPulseMs[idx] <= HALL_HZ_WINDOW_MS) n++;
  }
  lastHallHz = (float)n * (1000.0f / (float)HALL_HZ_WINDOW_MS);
  portEXIT_CRITICAL(&hallMux);
}

static void hallSnapshot(int &adc, float &hz) {
  portENTER_CRITICAL(&hallMux);
  adc = lastHallAdc;
  hz = lastHallHz;
  portEXIT_CRITICAL(&hallMux);
}

static void gpsPoll() {
  static char lineBuf[128];
  static size_t lineLen = 0;
  while (gpsSerial.available()) {
    const char c = (char)gpsSerial.read();
    if (c != '\r' && c != '\n' && lineLen + 1 < sizeof(lineBuf)) {
      lineBuf[lineLen++] = c;
    }
    if (gps.encode(c)) {
      gpsSentenceCount++;
      if (lineLen > 0) {
        lineBuf[lineLen] = '\0';
        portENTER_CRITICAL(&gpsSentMux);
        strncpy(gpsLastSentence, lineBuf, sizeof(gpsLastSentence) - 1);
        gpsLastSentence[sizeof(gpsLastSentence) - 1] = '\0';
        portEXIT_CRITICAL(&gpsSentMux);
      }
      lineLen = 0;
      if (gps.location.isValid() && gps.location.age() < GPS_FIX_STALE_MS) {
        portENTER_CRITICAL(&gpsMux);
        gpsFix.lat = (float)gps.location.lat();
        gpsFix.lon = (float)gps.location.lng();
        gpsFix.speed_mps = gps.speed.isValid() ? (float)gps.speed.mps() : NAN;
        gpsFix.course_deg = gps.course.isValid() ? (float)gps.course.deg() : NAN;
        gpsFix.alt_m = gps.altitude.isValid() ? (float)gps.altitude.meters() : NAN;
        gpsFix.hdop = gps.hdop.isValid() ? (float)gps.hdop.hdop() : NAN;
        gpsFix.satellites = gps.satellites.isValid() ? (uint32_t)gps.satellites.value() : 0;
        gpsFix.updated_at_ms = millis();
        gpsFix.valid = true;
        portEXIT_CRITICAL(&gpsMux);
      }
    }
    if (c == '\n') lineLen = 0;
  }
}

static const uint8_t UBX_CFG_RATE_5HZ[] = {
    0xB5, 0x62, 0x06, 0x08, 0x06, 0x00,
    0xC8, 0x00, 0x01, 0x00, 0x01, 0x00,
    0xDE, 0x6A,
};

static void gpsSetRate5Hz() {
  gpsSerial.write(UBX_CFG_RATE_5HZ, sizeof(UBX_CFG_RATE_5HZ));
  gpsSerial.flush();
  Serial.println("[gps] sent UBX-CFG-RATE 5Hz");
}

static bool gpsSnapshot(GpsFix &out) {
  portENTER_CRITICAL(&gpsMux);
  out.lat = gpsFix.lat;
  out.lon = gpsFix.lon;
  out.speed_mps = gpsFix.speed_mps;
  out.course_deg = gpsFix.course_deg;
  out.alt_m = gpsFix.alt_m;
  out.hdop = gpsFix.hdop;
  out.satellites = gpsFix.satellites;
  out.updated_at_ms = gpsFix.updated_at_ms;
  out.valid = gpsFix.valid;
  portEXIT_CRITICAL(&gpsMux);
  if (!out.valid) return false;
  return (millis() - out.updated_at_ms) < GPS_FIX_STALE_MS;
}

static Sample ring[RING_CAP];
static size_t ringHead = 0;
static size_t ringCount = 0;
static uint32_t ringDropped = 0;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

#ifndef PROFILE_GPS_HALL
static volatile float lastDhtTemp = NAN;
static volatile float lastDhtHum = NAN;
static volatile bool dhtOk = false;
static volatile uint32_t imuOkCount = 0;
static volatile uint32_t imuFailCount = 0;
#endif

static volatile bool timeOk = false;
static volatile int64_t millisToUnixOffset = 0;

static bool ntpKicked = false;
static uint32_t ntpKickAt = 0;

static void ringPush(const Sample &s) {
  portENTER_CRITICAL(&ringMux);
  ring[ringHead] = s;
  ringHead = (ringHead + 1) % RING_CAP;
  if (ringCount < RING_CAP) {
    ringCount++;
  } else {
    ringDropped++;
  }
  portEXIT_CRITICAL(&ringMux);
}

static size_t ringSnapshot(Sample *out, size_t maxN) {
  portENTER_CRITICAL(&ringMux);
  const size_t n = ringCount < maxN ? ringCount : maxN;
  const size_t start = (ringHead + RING_CAP - ringCount) % RING_CAP;
  for (size_t i = 0; i < n; i++) {
    out[i] = ring[(start + i) % RING_CAP];
  }
  portEXIT_CRITICAL(&ringMux);
  return n;
}

static void ringPopFront(size_t n) {
  portENTER_CRITICAL(&ringMux);
  if (n > ringCount) n = ringCount;
  ringCount -= n;
  portEXIT_CRITICAL(&ringMux);
}

static size_t ringSize() {
  portENTER_CRITICAL(&ringMux);
  size_t n = ringCount;
  portEXIT_CRITICAL(&ringMux);
  return n;
}

static uint64_t toUnixMs(uint32_t millisAt) {
  return (uint64_t)((int64_t)millisAt + millisToUnixOffset);
}

static bool applyUnixTime(time_t unixSec, const char *src) {
  if (unixSec < 1700000000) return false;
  uint32_t m = millis();
  millisToUnixOffset = (int64_t)unixSec * 1000LL - (int64_t)m;
  timeOk = true;
  timeval tv{};
  tv.tv_sec = unixSec;
  tv.tv_usec = 0;
  settimeofday(&tv, nullptr);
  Serial.printf("[time] ok via %s unix=%ld offset=%lld\n",
                src, (long)unixSec, (long long)millisToUnixOffset);
  return true;
}

static bool parseHttpDate(const char *date, time_t *out) {
  int day, year, hour, min, sec;
  char mon[4] = {};
  if (sscanf(date, "%*[^,], %d %3s %d %d:%d:%d",
             &day, mon, &year, &hour, &min, &sec) != 6) {
    return false;
  }
  static const char kMonths[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
  const char *p = strstr(kMonths, mon);
  if (!p || (p - kMonths) % 3 != 0) return false;

  struct tm t = {};
  t.tm_year = year - 1900;
  t.tm_mon = (int)(p - kMonths) / 3;
  t.tm_mday = day;
  t.tm_hour = hour;
  t.tm_min = min;
  t.tm_sec = sec;
  setenv("TZ", "GMT0", 1);
  tzset();
  time_t v = mktime(&t);
  if (v < 0) return false;
  *out = v;
  return true;
}

static String ingestOrigin() {
  String u = INGEST_URL;
  const int scheme = u.indexOf("://");
  if (scheme < 0) return u;
  const int path = u.indexOf('/', scheme + 3);
  if (path < 0) return u + "/";
  return u.substring(0, path + 1);
}

static bool syncTimeFromHttpUrl(const String &url) {
  static WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(2500);
  HTTPClient http;
  http.setTimeout(3000);
  http.setConnectTimeout(3000);
  const char *keys[] = {"Date"};
  http.collectHeaders(keys, 1);

  if (!http.begin(client, url)) return false;

  // 只做 GET：部分 CDN/熱點對 HEAD 會掛到 WDT
  int code = http.GET();

  bool ok = false;
  if (code > 0 && http.hasHeader("Date")) {
    const String date = http.header("Date");
    time_t unixSec = 0;
    if (parseHttpDate(date.c_str(), &unixSec)) {
      ok = applyUnixTime(unixSec, "http-date");
    } else {
      Serial.printf("[time] bad Date: %s\n", date.c_str());
    }
  } else {
    Serial.printf("[time] http-date fail url=%s code=%d\n", url.c_str(), code);
  }
  http.end();
  return ok;
}

static bool syncTimeFromHttp() {
  if (WiFi.status() != WL_CONNECTED) return false;
  const String origin = ingestOrigin();
  Serial.printf("[time] try http-date %s\n", origin.c_str());
  if (syncTimeFromHttpUrl(origin)) return true;
  Serial.println("[time] try http-date https://www.google.com/");
  return syncTimeFromHttpUrl("https://www.google.com/");
}

static void kickNtp() {
  if (WiFi.status() != WL_CONNECTED) return;
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.nist.gov");
  ntpKicked = true;
  ntpKickAt = millis();
  Serial.println("[ntp] kicked");
}

static void pollTime() {
  if (timeOk) return;
  if (WiFi.status() != WL_CONNECTED) return;

  time_t now = time(nullptr);
  if (now > 1700000000) {
    applyUnixTime(now, "sntp");
    return;
  }

  if (!ntpKicked) {
    kickNtp();
    return;
  }

  static uint32_t lastHttpTry = 0;
  // SSL 太早打會卡 WDT；給 SNTP 多一點時間
  if (millis() - ntpKickAt >= 10000 && millis() - lastHttpTry >= NTP_RETRY_MS) {
    lastHttpTry = millis();
    if (syncTimeFromHttp()) return;
  }

  if (millis() - ntpKickAt >= NTP_RETRY_MS) {
    kickNtp();
  }
}

static void attachGpsToSample(Sample &out) {
  GpsFix fix{};
  out.has_gps = gpsSnapshot(fix);
  if (out.has_gps) {
    out.gps_lat = fix.lat;
    out.gps_lon = fix.lon;
    out.gps_speed_mps = fix.speed_mps;
    out.gps_course_deg = fix.course_deg;
    out.gps_alt_m = fix.alt_m;
    out.gps_hdop = fix.hdop;
    out.gps_satellites = fix.satellites;
  }
}

#ifdef PROFILE_GPS_HALL
static void attachHallToSample(Sample &out) {
  int adc = 0;
  float hz = 0;
  hallSnapshot(adc, hz);
  out.hall_adc = adc;
  out.hall_hz = hz;
  out.has_hall = true;
}

static void sampleTask(void *) {
  uint32_t lastSample = 0;
  uint32_t lastDropLog = 0;

  for (;;) {
    uint32_t now = millis();
    gpsPoll();
    hallUpdate();

    if (now - lastSample >= IMU_PERIOD_MS) {
      lastSample = now;
      Sample s{};
      s.millis_at = now;
      attachGpsToSample(s);
      attachHallToSample(s);
      ringPush(s);
    }

    if (ringDropped > 0 && now - lastDropLog > 2000) {
      lastDropLog = now;
      Serial.printf("[ring] dropped=%lu size=%u\n",
                    (unsigned long)ringDropped, (unsigned)ringSize());
    }

    vTaskDelay(pdMS_TO_TICKS(1));
  }
}
#else
static bool icmWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(icmAddr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool icmRead(uint8_t reg, uint8_t *buf, size_t len) {
  Wire.beginTransmission(icmAddr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  size_t n = Wire.requestFrom(icmAddr, (uint8_t)len);
  if (n != len) return false;
  for (size_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

static int16_t be16(const uint8_t *p) {
  return (int16_t)((p[0] << 8) | p[1]);
}

static bool icmProbe() {
  for (uint8_t addr : {ICM_ADDR_LOW, ICM_ADDR_HIGH}) {
    icmAddr = addr;
    uint8_t who = 0;
    if (!icmRead(ICM_WHO_AM_I_REG, &who, 1)) continue;
    if (who == ICM_WHO_AM_I_VAL) {
      Serial.printf("[icm] WHO_AM_I=0x%02X @ 0x%02X\n", who, addr);
      return true;
    }
    Serial.printf("[icm] unexpected WHO_AM_I=0x%02X @ 0x%02X\n", who, addr);
  }
  return false;
}

static bool icmWriteRetry(uint8_t reg, uint8_t val, int tries = 5) {
  for (int i = 0; i < tries; i++) {
    if (icmWrite(reg, val)) return true;
    delay(10);
  }
  Serial.printf("[icm] write fail reg=0x%02X val=0x%02X\n", reg, val);
  return false;
}

static bool icmReadSample(Sample &out);

static bool icmInit() {
  if (!icmProbe()) return false;
  if (!icmWriteRetry(ICM_PWR_MGMT0, 0x0F)) return false;
  delay(45);
  if (!icmWriteRetry(ICM_GYRO_CONFIG0, 0x08)) return false;
  if (!icmWriteRetry(ICM_ACCEL_CONFIG0, 0x08)) return false;
  delay(20);

  Sample probe{};
  if (!icmReadSample(probe)) {
    Serial.println("[icm] first read failed");
    return false;
  }
  Serial.printf("[icm] sample ax=%.2f ay=%.2f az=%.2f |a|=%.2f T=%.1f\n",
                probe.ax, probe.ay, probe.az, probe.accel_mag, probe.imu_temp_c);
  return true;
}

static bool icmReadSample(Sample &out) {
  uint8_t raw[14];
  if (!icmRead(ICM_TEMP_DATA1, raw, 14)) return false;

  int16_t temp = be16(&raw[0]);
  int16_t ax = be16(&raw[2]);
  int16_t ay = be16(&raw[4]);
  int16_t az = be16(&raw[6]);
  int16_t gx = be16(&raw[8]);
  int16_t gy = be16(&raw[10]);
  int16_t gz = be16(&raw[12]);

  out.imu_temp_c = (temp / 132.48f) + 25.0f;
  out.ax = ax / ACCEL_SENS;
  out.ay = ay / ACCEL_SENS;
  out.az = az / ACCEL_SENS;
  out.gx = gx / GYRO_SENS;
  out.gy = gy / GYRO_SENS;
  out.gz = gz / GYRO_SENS;
  out.accel_mag = sqrtf(out.ax * out.ax + out.ay * out.ay + out.az * out.az);
  out.millis_at = millis();
  out.has_dht = dhtOk;
  out.dht_temp_c = lastDhtTemp;
  out.dht_humidity = lastDhtHum;

  attachGpsToSample(out);
  return true;
}

static void sampleTask(void *) {
  uint32_t lastImu = 0;
  uint32_t lastDhtFallback = 0;
  uint32_t lastDropLog = 0;
  uint32_t lastGpsUpdatedAt = 0;
  uint32_t lastCalibLog = 0;
  bool loggedRunning = false;

  for (;;) {
    uint32_t now = millis();

    gpsPoll();

    if (now - lastImu >= IMU_PERIOD_MS) {
      lastImu = now;
      Sample s{};
      if (icmReadSample(s)) {
        imuOkCount++;

        GpsFix fix{};
        if (gpsSnapshot(fix) && fix.updated_at_ms != lastGpsUpdatedAt) {
          lastGpsUpdatedAt = fix.updated_at_ms;
          deadReckoner.onGpsFix(fix.updated_at_ms, fix.lat, fix.lon,
                               fix.speed_mps, fix.course_deg, fix.satellites);
        }

        const DrOutput dr = deadReckoner.tick(s.millis_at, s.ax, s.ay, s.az,
                                              s.gx, s.gy, s.gz);
        s.has_dr = dr.valid;
        if (dr.valid) {
          s.lat_dr = dr.lat_dr;
          s.lon_dr = dr.lon_dr;
          s.heading_dr_deg = dr.heading_deg;
          s.speed_dr_mps = dr.speed_mps;
        }

        if (dr.state == DrState::CALIBRATING) {
          if (now - lastCalibLog > 1000) {
            lastCalibLog = now;
            Serial.printf("[dr] CALIBRATING %u/%d (keep vehicle still)\n",
                          (unsigned)dr.bias_samples, DR_GYRO_BIAS_SAMPLES);
          }
        } else if (!loggedRunning) {
          loggedRunning = true;
          Serial.printf("[dr] RUNNING bias_gz=%.4f dps\n",
                        deadReckoner.gyroBiasDps());
        }

        ringPush(s);
      } else {
        imuFailCount++;
        if (imuFailCount == 1 || imuFailCount % 50 == 0) {
          Serial.printf("[icm] read fail x%lu (addr=0x%02X)\n",
                        (unsigned long)imuFailCount, icmAddr);
        }
        if (dhtOk && now - lastDhtFallback >= DHT_PERIOD_MS) {
          Sample d{};
          d.has_dht = true;
          d.dht_temp_c = lastDhtTemp;
          d.dht_humidity = lastDhtHum;
          d.millis_at = now;
          d.ax = d.ay = d.az = 0;
          d.gx = d.gy = d.gz = 0;
          d.imu_temp_c = NAN;
          d.accel_mag = 0;
          d.has_dr = false;
          attachGpsToSample(d);
          ringPush(d);
          lastDhtFallback = now;
        }
      }
    }

    if (ringDropped > 0 && now - lastDropLog > 2000) {
      lastDropLog = now;
      Serial.printf("[ring] dropped=%lu size=%u\n",
                    (unsigned long)ringDropped, (unsigned)ringSize());
    }

    vTaskDelay(pdMS_TO_TICKS(1));
  }
}
#endif

struct WifiCred {
  const char *ssid;
  const char *pass;
};

static const WifiCred WIFI_NETS[] = {
    {WIFI_SSID, WIFI_PASS},
    {WIFI_SSID2, WIFI_PASS2},
};
static constexpr size_t WIFI_NET_COUNT = sizeof(WIFI_NETS) / sizeof(WIFI_NETS[0]);
static size_t wifiNetIdx = 0;

static bool tryConnectWifi(size_t idx, uint32_t timeoutMs = 20000) {
  const WifiCred &c = WIFI_NETS[idx % WIFI_NET_COUNT];
  WiFi.disconnect(true, true);
  delay(200);
  WiFi.setSleep(false);
  WiFi.begin(c.ssid, c.pass);
  Serial.printf("[wifi] connecting to %s", c.ssid);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    wifiNetIdx = idx % WIFI_NET_COUNT;
    Serial.printf("[wifi] ok ssid=%s ip=%s rssi=%d\n", c.ssid,
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
    timeOk = false;
    ntpKicked = false;
    kickNtp();
    return true;
  }
  Serial.printf("[wifi] fail %s status=%d\n", c.ssid, (int)WiFi.status());
  return false;
}

static void connectWifi() {
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.mode(WIFI_OFF);
  delay(200);
  WiFi.mode(WIFI_STA);
  delay(100);
  Serial.printf("[wifi] free_heap=%u\n", (unsigned)ESP.getFreeHeap());
  for (size_t i = 0; i < WIFI_NET_COUNT; i++) {
    if (tryConnectWifi((wifiNetIdx + i) % WIFI_NET_COUNT)) return;
  }
  Serial.println("[wifi] FAILED — will keep retrying");
}

static void reconnectWifi() {
  wifiNetIdx = (wifiNetIdx + 1) % WIFI_NET_COUNT;
  Serial.printf("[wifi] reconnect → %s\n", WIFI_NETS[wifiNetIdx].ssid);
  tryConnectWifi(wifiNetIdx, 10000);
}

static bool postBatch() {
  const size_t pending = ringSize();
  if (pending == 0) return true;
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[http] skip (wifi down) ring=%u\n", (unsigned)pending);
    return false;
  }
  // 沒 NTP/HTTP Date 也照送；省略 ts_ms，後端用收到時間
  if (!timeOk) {
    static uint32_t lastSkipLog = 0;
    if (millis() - lastSkipLog > 5000) {
      lastSkipLog = millis();
      Serial.printf("[http] posting without time sync ring=%u\n", (unsigned)pending);
    }
  }

  static Sample chunk[POST_CHUNK];
  const size_t n = ringSnapshot(chunk, POST_CHUNK);

  Serial.printf("[http] posting %u/%u → %s\n",
                (unsigned)n, (unsigned)pending, INGEST_URL);

  static WiFiClientSecure secureClient;
  static WiFiClient plainClient;
  static JsonDocument doc;
  doc.clear();
  doc["device_id"] = DEVICE_ID;
  if (CAR_ID[0] != '\0') doc["car_id"] = CAR_ID;
  JsonArray samples = doc["samples"].to<JsonArray>();

  for (size_t i = 0; i < n; i++) {
    const Sample &s = chunk[i];
    JsonObject o = samples.add<JsonObject>();
    if (timeOk) o["ts_ms"] = toUnixMs(s.millis_at);
#ifdef PROFILE_GPS_HALL
    if (s.has_hall) {
      o["hall_adc"] = s.hall_adc;
      o["hall_hz"] = s.hall_hz;
    }
#else
    o["ax"] = s.ax;
    o["ay"] = s.ay;
    o["az"] = s.az;
    o["gx"] = s.gx;
    o["gy"] = s.gy;
    o["gz"] = s.gz;
    o["imu_temp_c"] = s.imu_temp_c;
    o["accel_mag"] = s.accel_mag;
    if (s.has_dht && !isnan(s.dht_temp_c)) {
      o["dht_temp_c"] = s.dht_temp_c;
      o["dht_humidity"] = s.dht_humidity;
    }
#endif
    if (s.has_gps) {
      o["gps_lat"] = s.gps_lat;
      o["gps_lon"] = s.gps_lon;
      if (!isnan(s.gps_speed_mps)) o["gps_speed_mps"] = s.gps_speed_mps;
      if (!isnan(s.gps_course_deg)) o["gps_course_deg"] = s.gps_course_deg;
      if (!isnan(s.gps_alt_m)) o["gps_alt_m"] = s.gps_alt_m;
      if (!isnan(s.gps_hdop)) o["gps_hdop"] = s.gps_hdop;
      o["gps_satellites"] = s.gps_satellites;
    }
#ifndef PROFILE_GPS_HALL
    if (s.has_dr) {
      o["lat_dr"] = s.lat_dr;
      o["lon_dr"] = s.lon_dr;
      o["dr_heading_deg"] = s.heading_dr_deg;
      o["dr_speed_mps"] = s.speed_dr_mps;
    }
#endif
  }

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.setTimeout(15000);
  http.setReuse(false);

  const bool isHttps = String(INGEST_URL).startsWith("https://");

  bool began = false;
  if (isHttps) {
    secureClient.setInsecure();
    began = http.begin(secureClient, INGEST_URL);
  } else {
    began = http.begin(plainClient, INGEST_URL);
  }
  if (!began) {
    Serial.println("[http] begin failed");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + INGEST_TOKEN);

  int code = http.POST(body);
  String resp = http.getString();
  http.end();

  if (code >= 200 && code < 300) {
    ringPopFront(n);
    Serial.printf("[http] ok %d wrote=%u left=%u\n",
                  code, (unsigned)n, (unsigned)ringSize());
    return true;
  }
  Serial.printf("[http] fail code=%d body=%s (kept ring=%u)\n",
                code, resp.c_str(), (unsigned)ringSize());
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(200);
#ifdef PROFILE_GPS_HALL
  Serial.println("\n[boot] ESP32 GPS+Hall (PROFILE_GPS_HALL)");
#else
  Serial.println("\n[boot] ESP32 ICM42688+DHT11+GPS+DR (ring+NTP+core1 sample)");
  Serial.println("[boot] DR: keep still ~8s for gyro bias (200 samples @25Hz)");
#endif
  Serial.printf("[boot] device_id=%s ring cap=%u (~%us @25Hz) heap=%u\n",
                DEVICE_ID, (unsigned)RING_CAP, (unsigned)(RING_CAP / 25),
                (unsigned)ESP.getFreeHeap());

  connectWifi();

  analogSetPinAttenuation(PIN_HALL, ADC_11db);
  pinMode(PIN_HALL, INPUT);

  gpsSerial.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  Serial.printf("[gps] UART1 @ %lu baud, rx=%d tx=%d\n",
                (unsigned long)GPS_BAUD, PIN_GPS_RX, PIN_GPS_TX);
  delay(100);
  gpsSetRate5Hz();

#ifndef PROFILE_GPS_HALL
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);

  dht.begin();
  delay(1000);

  if (!icmInit()) {
    Serial.println("[icm] init FAILED — check wiring / I2C addr");
  } else {
    Serial.println("[icm] ready ±16g / ±2000dps");
  }
#else
  Serial.printf("[hall] ADC GPIO%d high>=%d low<=%d\n",
                PIN_HALL, HALL_ADC_HIGH, HALL_ADC_LOW);
#endif

  xTaskCreatePinnedToCore(sampleTask, "sample", 4096, nullptr, 1, nullptr, 1);
}

void loop() {
  static uint32_t lastPost = 0;
  static uint32_t lastStat = 0;
  static uint32_t lastWifi = 0;
  static uint32_t lastNtpResync = 0;
#ifndef PROFILE_GPS_HALL
  static uint32_t lastImuOk = 0;
  static uint32_t lastImuFail = 0;
#endif

  uint32_t now = millis();

  pollTime();

  if (WiFi.status() != WL_CONNECTED && now - lastWifi > 5000) {
    lastWifi = now;
    reconnectWifi();
  }

  if (WiFi.status() == WL_CONNECTED && timeOk && now - lastNtpResync > NTP_RESYNC_MS) {
    lastNtpResync = now;
    timeOk = false;
    ntpKicked = false;
    kickNtp();
  }

  if (now - lastStat >= 2000) {
    lastStat = now;
    GpsFix fix{};
    bool gpsOk = gpsSnapshot(fix);
    int hallAdc = 0;
    float hallHz = 0;
    hallSnapshot(hallAdc, hallHz);

#ifdef PROFILE_GPS_HALL
    Serial.printf("[stat] ring=%u ntp=%d hall_adc=%d hall_hz=%.1f "
                  "gps=%d sats=%lu/%lu hdop=%.1f sentences=%lu chars=%lu badck=%lu "
                  "locValid=%d age=%lu\n",
                  (unsigned)ringSize(),
                  timeOk ? 1 : 0, hallAdc, hallHz,
                  gpsOk ? 1 : 0,
                  (unsigned long)fix.satellites,
                  (unsigned long)(gps.satellites.isValid() ? gps.satellites.value() : 0),
                  gps.hdop.isValid() ? (double)gps.hdop.hdop() : -1.0,
                  (unsigned long)gpsSentenceCount,
                  (unsigned long)gps.charsProcessed(),
                  (unsigned long)gps.failedChecksum(),
                  gps.location.isValid() ? 1 : 0,
                  (unsigned long)gps.location.age());
#else
    float h = dht.readHumidity();
    float t = dht.readTemperature();
    if (!isnan(h) && !isnan(t)) {
      lastDhtHum = h;
      lastDhtTemp = t;
      dhtOk = true;
    }

    const uint32_t ok = imuOkCount;
    const uint32_t fail = imuFailCount;
    const uint32_t okDelta = ok - lastImuOk;
    const uint32_t failDelta = fail - lastImuFail;
    lastImuOk = ok;
    lastImuFail = fail;
    Serial.printf("[stat] dht=%.1f/%.1f ring=%u imu_ok=%lu/2s fail=%lu ntp=%d "
                  "gps=%d sats=%lu/%lu hdop=%.1f sentences=%lu chars=%lu badck=%lu "
                  "locValid=%d age=%lu\n",
                  t, h, (unsigned)ringSize(),
                  (unsigned long)okDelta, (unsigned long)failDelta,
                  timeOk ? 1 : 0, gpsOk ? 1 : 0,
                  (unsigned long)fix.satellites,
                  (unsigned long)(gps.satellites.isValid() ? gps.satellites.value() : 0),
                  gps.hdop.isValid() ? (double)gps.hdop.hdop() : -1.0,
                  (unsigned long)gpsSentenceCount,
                  (unsigned long)gps.charsProcessed(),
                  (unsigned long)gps.failedChecksum(),
                  gps.location.isValid() ? 1 : 0,
                  (unsigned long)gps.location.age());
#endif

    static uint32_t lastNmeaDump = 0;
    if (now - lastNmeaDump > 10000) {
      lastNmeaDump = now;
      char snap[128];
      portENTER_CRITICAL(&gpsSentMux);
      strncpy(snap, gpsLastSentence, sizeof(snap) - 1);
      snap[sizeof(snap) - 1] = '\0';
      portEXIT_CRITICAL(&gpsSentMux);
      Serial.printf("[gps-raw] %s\n", snap[0] ? snap : "(none)");
    }
  }

  const size_t pending = ringSize();
  const uint32_t postEvery =
      (pending > POST_CHUNK * 2) ? POST_BURST_MS : POST_PERIOD_MS;
  if (now - lastPost >= postEvery && pending > 0) {
    postBatch();
    lastPost = millis();
  }
}
