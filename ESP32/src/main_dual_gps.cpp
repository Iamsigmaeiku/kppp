/**
 * PROFILE_DUAL_GPS — M10(UART0) + NEO-6M(UART2) + ICM42688(I2C)
 * → POST /api/telemetry/ingest（Hybrid：後端寫 gps_track）
 *
 * ICM 沿用既有板子接線：SDA=21 / SCL=22（不是 SPI）。
 * UART0 = Serial @ 9600 → M10；正式 build 無 Serial Monitor。
 * 燒錄前拔掉 M10 TX/RX。-DDUAL_GPS_DEBUG 才印 log（勿同時接 M10）。
 */

#include <Arduino.h>
#include <Wire.h>
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

#ifndef INGEST_URL_FALLBACK
#define INGEST_URL_FALLBACK ""
#endif

SET_LOOP_TASK_STACK_SIZE(24 * 1024);

#ifndef WIFI_SSID2
#define WIFI_SSID2 WIFI_SSID
#define WIFI_PASS2 WIFI_PASS
#endif

#ifdef DUAL_GPS_DEBUG
#define LOGF(...) Serial.printf(__VA_ARGS__)
#define LOGLN(s) Serial.println(s)
#else
#define LOGF(...) \
  do {            \
  } while (0)
#define LOGLN(s) \
  do {           \
  } while (0)
#endif

// ---- Pins（ICM = 既有 I2C；雙 UART GPS）----
static constexpr int PIN_SDA = 21;
static constexpr int PIN_SCL = 22;
static constexpr int PIN_NEO_RX = 16;  // ESP RX ← NEO TX
static constexpr int PIN_NEO_TX = 17;  // ESP TX → NEO RX
static constexpr uint32_t GPS_BAUD = 9600;
static constexpr uint32_t GPS_FIX_STALE_MS = 5000;

// ---- ICM-42688-P (Bank 0, I2C) — 與 esp32dev 相同 ----
static constexpr uint8_t ICM_ADDR_LOW = 0x68;
static constexpr uint8_t ICM_ADDR_HIGH = 0x69;
static constexpr uint8_t ICM_WHO_AM_I_REG = 0x75;
static constexpr uint8_t ICM_WHO_AM_I_VAL = 0x47;
static constexpr uint8_t ICM_PWR_MGMT0 = 0x4E;
static constexpr uint8_t ICM_GYRO_CONFIG0 = 0x4F;
static constexpr uint8_t ICM_ACCEL_CONFIG0 = 0x50;
static constexpr uint8_t ICM_TEMP_DATA1 = 0x1D;
static constexpr float ACCEL_SENS = 2048.0f;  // ±16g
static constexpr float GYRO_SENS = 16.4f;     // ±2000 dps
static constexpr float IMU_MIN_ACCEL_MAG = 0.2f;  // 靜止應 ~1g；全 0 = 假讀

static constexpr uint32_t IMU_PERIOD_MS = 40;  // 25 Hz sample → ring
static constexpr uint32_t POST_PERIOD_MS = 200;
static constexpr uint32_t POST_BURST_MS = 20;
static constexpr uint32_t NTP_RESYNC_MS = 3600000;
static constexpr uint32_t NTP_RETRY_MS = 15000;
static constexpr size_t POST_CHUNK = 25;
static constexpr size_t RING_CAP = 400;
static constexpr uint32_t INGEST_PRIMARY_RETRY_MS = 300000;

static uint8_t icmAddr = ICM_ADDR_LOW;
static TinyGPSPlus gpsM10;
static TinyGPSPlus gpsNeo;
static HardwareSerial &m10Serial = Serial;  // UART0
static HardwareSerial neoSerial(2);         // UART2

struct GpsFix {
  float lat, lon, speed_mps, course_deg, alt_m, hdop;
  uint32_t satellites;
  uint32_t updated_at_ms;
  bool valid;
};

struct Sample {
  float ax, ay, az, gx, gy, gz, imu_temp_c, accel_mag;
  bool has_imu;
  float gps_lat, gps_lon, gps_speed_mps, gps_course_deg, gps_alt_m, gps_hdop;
  uint32_t gps_satellites;
  bool has_gps;  // primary (NEO) for kart_telemetry.gps_*
  bool gps_fresh;
  bool has_m10;
  bool has_neo;
  bool m10_fresh;
  bool neo_fresh;
  GpsFix m10;
  GpsFix neo;
  uint32_t millis_at;
};

static volatile GpsFix m10Fix{};
static volatile GpsFix neoFix{};
static portMUX_TYPE m10Mux = portMUX_INITIALIZER_UNLOCKED;
static portMUX_TYPE neoMux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool m10UpdatedFlag = false;
static volatile bool neoUpdatedFlag = false;

static Sample ring[RING_CAP];
static size_t ringHead = 0;
static size_t ringCount = 0;
static uint32_t ringDropped = 0;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

static volatile bool timeOk = false;
static volatile int64_t millisToUnixOffset = 0;
static bool ntpKicked = false;
static uint32_t ntpKickAt = 0;
static bool ingestUseFallback = false;
static uint32_t ingestFallbackSince = 0;
static volatile uint32_t imuOkCount = 0;
static volatile uint32_t imuFailCount = 0;

static void pollOneGps(HardwareSerial &ser, TinyGPSPlus &parser, volatile GpsFix &fix,
                       portMUX_TYPE &mux, volatile bool &updatedFlag) {
  while (ser.available()) {
    const char c = (char)ser.read();
    if (parser.encode(c)) {
      if (parser.location.isUpdated() && parser.location.isValid() &&
          parser.location.age() < GPS_FIX_STALE_MS) {
        portENTER_CRITICAL(&mux);
        fix.lat = (float)parser.location.lat();
        fix.lon = (float)parser.location.lng();
        fix.speed_mps = parser.speed.isValid() ? (float)parser.speed.mps() : NAN;
        fix.course_deg = parser.course.isValid() ? (float)parser.course.deg() : NAN;
        fix.alt_m = parser.altitude.isValid() ? (float)parser.altitude.meters() : NAN;
        fix.hdop = parser.hdop.isValid() ? (float)parser.hdop.hdop() : NAN;
        fix.satellites =
            parser.satellites.isValid() ? (uint32_t)parser.satellites.value() : 0;
        fix.updated_at_ms = millis();
        fix.valid = true;
        portEXIT_CRITICAL(&mux);
        updatedFlag = true;
      }
    }
  }
}

static void gpsPollBoth() {
#ifndef DUAL_GPS_DEBUG
  pollOneGps(m10Serial, gpsM10, m10Fix, m10Mux, m10UpdatedFlag);
#endif
  pollOneGps(neoSerial, gpsNeo, neoFix, neoMux, neoUpdatedFlag);
}

static bool snapshotFix(volatile GpsFix &src, portMUX_TYPE &mux, GpsFix &out) {
  portENTER_CRITICAL(&mux);
  out.lat = src.lat;
  out.lon = src.lon;
  out.speed_mps = src.speed_mps;
  out.course_deg = src.course_deg;
  out.alt_m = src.alt_m;
  out.hdop = src.hdop;
  out.satellites = src.satellites;
  out.updated_at_ms = src.updated_at_ms;
  out.valid = src.valid;
  portEXIT_CRITICAL(&mux);
  if (!out.valid) return false;
  return (millis() - out.updated_at_ms) < GPS_FIX_STALE_MS;
}

static const char *activeIngestUrl() {
  if (INGEST_URL_FALLBACK[0] == '\0') return INGEST_URL;
  if (ingestUseFallback) return INGEST_URL_FALLBACK;
  return INGEST_URL;
}

static void ingestPreferFallback(const char *why) {
  if (INGEST_URL_FALLBACK[0] == '\0') return;
  if (!ingestUseFallback) LOGF("[http] switch → FALLBACK (%s)\n", why);
  ingestUseFallback = true;
  ingestFallbackSince = millis();
}

static void ingestMaybeRetryPrimary() {
  if (!ingestUseFallback || INGEST_URL_FALLBACK[0] == '\0') return;
  if (millis() - ingestFallbackSince < INGEST_PRIMARY_RETRY_MS) return;
  LOGLN("[http] retry PRIMARY after fallback hold");
  ingestUseFallback = false;
}

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
  LOGF("[time] ok via %s unix=%ld\n", src, (long)unixSec);
  return true;
}

static bool parseHttpDate(const char *date, time_t *out) {
  int day, year, hour, min, sec;
  char mon[4] = {};
  if (sscanf(date, "%*[^,], %d %3s %d %d:%d:%d", &day, mon, &year, &hour, &min,
             &sec) != 6) {
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
  int code = http.GET();
  bool ok = false;
  if (code > 0 && http.hasHeader("Date")) {
    const String date = http.header("Date");
    time_t unixSec = 0;
    if (parseHttpDate(date.c_str(), &unixSec)) {
      ok = applyUnixTime(unixSec, "http-date");
    }
  }
  http.end();
  return ok;
}

static bool syncTimeFromHttp() {
  if (WiFi.status() != WL_CONNECTED) return false;
  if (syncTimeFromHttpUrl(ingestOrigin())) return true;
  return syncTimeFromHttpUrl("https://www.google.com/");
}

static void kickNtp() {
  if (WiFi.status() != WL_CONNECTED) return;
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.nist.gov");
  ntpKicked = true;
  ntpKickAt = millis();
  LOGLN("[ntp] kicked");
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
  if (millis() - ntpKickAt >= 10000 && millis() - lastHttpTry >= NTP_RETRY_MS) {
    lastHttpTry = millis();
    if (syncTimeFromHttp()) return;
  }
  if (millis() - ntpKickAt >= NTP_RETRY_MS) kickNtp();
}

/** GPS UTC → unix if NTP not yet ready */
static bool tryTimeFromGps(TinyGPSPlus &g) {
  if (timeOk) return true;
  if (!g.date.isValid() || !g.time.isValid()) return false;
  if (g.date.year() < 2024) return false;
  struct tm t = {};
  t.tm_year = g.date.year() - 1900;
  t.tm_mon = g.date.month() - 1;
  t.tm_mday = g.date.day();
  t.tm_hour = g.time.hour();
  t.tm_min = g.time.minute();
  t.tm_sec = g.time.second();
  setenv("TZ", "GMT0", 1);
  tzset();
  time_t v = mktime(&t);
  return applyUnixTime(v, "gps-utc");
}

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
      LOGF("[icm] WHO_AM_I=0x%02X @ 0x%02X\n", who, addr);
      return true;
    }
    LOGF("[icm] unexpected WHO_AM_I=0x%02X @ 0x%02X\n", who, addr);
  }
  return false;
}

static bool icmWriteRetry(uint8_t reg, uint8_t val, int tries = 5) {
  for (int i = 0; i < tries; i++) {
    if (icmWrite(reg, val)) return true;
    delay(10);
  }
  return false;
}

static bool icmReadSample(Sample &out);

static bool icmInit() {
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);
  if (!icmProbe()) return false;
  if (!icmWriteRetry(ICM_PWR_MGMT0, 0x0F)) return false;
  delay(45);
  if (!icmWriteRetry(ICM_GYRO_CONFIG0, 0x08)) return false;
  if (!icmWriteRetry(ICM_ACCEL_CONFIG0, 0x08)) return false;
  delay(20);
  Sample probe{};
  if (!icmReadSample(probe) || !probe.has_imu) {
    LOGLN("[icm] first read failed / implausible");
    return false;
  }
  LOGF("[icm] I2C ok |a|=%.2f T=%.1f\n", probe.accel_mag, probe.imu_temp_c);
  return true;
}

static bool icmReadSample(Sample &out) {
  uint8_t raw[14];
  if (!icmRead(ICM_TEMP_DATA1, raw, 14)) {
    out.has_imu = false;
    return false;
  }

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

  // SPI 浮接 / 全 0 會出現 temp≈25、|a|≈0 — 當失敗，別灌假零進 Influx
  out.has_imu = out.accel_mag >= IMU_MIN_ACCEL_MAG;
  return out.has_imu;
}

static void attachGpsToSample(Sample &out) {
  const bool m10Fresh = m10UpdatedFlag;
  const bool neoFresh = neoUpdatedFlag;
  m10UpdatedFlag = false;
  neoUpdatedFlag = false;

  out.m10_fresh = m10Fresh;
  out.neo_fresh = neoFresh;
  out.has_m10 = snapshotFix(m10Fix, m10Mux, out.m10);
  out.has_neo = snapshotFix(neoFix, neoMux, out.neo);

  // 主 gps_* 用 NEO（相容舊 status UI）；沒有就退 M10；一律帶 gps_fresh
  if (out.has_neo) {
    out.has_gps = true;
    out.gps_lat = out.neo.lat;
    out.gps_lon = out.neo.lon;
    out.gps_speed_mps = out.neo.speed_mps;
    out.gps_course_deg = out.neo.course_deg;
    out.gps_alt_m = out.neo.alt_m;
    out.gps_hdop = out.neo.hdop;
    out.gps_satellites = out.neo.satellites;
    out.gps_fresh = neoFresh;
  } else if (out.has_m10) {
    out.has_gps = true;
    out.gps_lat = out.m10.lat;
    out.gps_lon = out.m10.lon;
    out.gps_speed_mps = out.m10.speed_mps;
    out.gps_course_deg = out.m10.course_deg;
    out.gps_alt_m = out.m10.alt_m;
    out.gps_hdop = out.m10.hdop;
    out.gps_satellites = out.m10.satellites;
    out.gps_fresh = m10Fresh;
  } else {
    out.has_gps = false;
    out.gps_fresh = false;
  }
}

static void sampleTask(void *) {
  uint32_t lastImu = 0;
  uint32_t lastDropLog = 0;

  for (;;) {
    uint32_t now = millis();
    gpsPollBoth();
    tryTimeFromGps(gpsNeo);
    tryTimeFromGps(gpsM10);

    if (now - lastImu >= IMU_PERIOD_MS) {
      lastImu = now;
      Sample s{};
      s.has_imu = false;
      if (icmReadSample(s)) {
        imuOkCount++;
        attachGpsToSample(s);
        ringPush(s);
      } else {
        imuFailCount++;
        // 只推 GPS；不要帶 ax=0 去騙 Grafana
        Sample g{};
        g.has_imu = false;
        g.imu_temp_c = NAN;
        g.accel_mag = 0;
        g.millis_at = now;
        attachGpsToSample(g);
        if (g.has_m10 || g.has_neo) ringPush(g);
      }
    }

    if (ringDropped > 0 && now - lastDropLog > 2000) {
      lastDropLog = now;
      LOGF("[ring] dropped=%lu size=%u\n", (unsigned long)ringDropped,
           (unsigned)ringSize());
    }
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

struct WifiCred {
  const char *ssid;
  const char *pass;
};

static const WifiCred WIFI_NETS[] = {
    {WIFI_SSID, WIFI_PASS},
};
static constexpr size_t WIFI_NET_COUNT = sizeof(WIFI_NETS) / sizeof(WIFI_NETS[0]);
static size_t wifiNetIdx = 0;

static bool tryConnectWifi(size_t idx, uint32_t timeoutMs = 20000) {
  const WifiCred &c = WIFI_NETS[idx % WIFI_NET_COUNT];
  WiFi.disconnect(true, true);
  delay(200);
  WiFi.setSleep(false);
  WiFi.begin(c.ssid, c.pass);
  LOGF("[wifi] connecting to %s", c.ssid);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
    delay(400);
  }
  if (WiFi.status() == WL_CONNECTED) {
    wifiNetIdx = idx % WIFI_NET_COUNT;
    LOGF("[wifi] ok ssid=%s ip=%s\n", c.ssid, WiFi.localIP().toString().c_str());
    timeOk = false;
    ntpKicked = false;
    kickNtp();
    return true;
  }
  return false;
}

static void connectWifi() {
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.mode(WIFI_OFF);
  delay(200);
  WiFi.mode(WIFI_STA);
  delay(100);
  for (size_t i = 0; i < WIFI_NET_COUNT; i++) {
    if (tryConnectWifi((wifiNetIdx + i) % WIFI_NET_COUNT)) return;
  }
  LOGLN("[wifi] FAILED — will keep retrying");
}

static void reconnectWifi() {
  wifiNetIdx = (wifiNetIdx + 1) % WIFI_NET_COUNT;
  tryConnectWifi(wifiNetIdx, 10000);
}

static void appendTrack(JsonArray tracks, const char *device, const GpsFix &fix,
                        bool include) {
  if (!include || !fix.valid) return;
  JsonObject t = tracks.add<JsonObject>();
  t["device"] = device;
  t["lat"] = fix.lat;
  t["lon"] = fix.lon;
  if (!isnan(fix.alt_m)) t["alt"] = fix.alt_m;
  if (!isnan(fix.speed_mps)) t["speed_mps"] = fix.speed_mps;
  if (!isnan(fix.course_deg)) t["course_deg"] = fix.course_deg;
  if (!isnan(fix.hdop)) t["hdop"] = fix.hdop;
  t["sats"] = fix.satellites;
}

static bool postBatch() {
  const size_t pending = ringSize();
  if (pending == 0) return true;
  if (WiFi.status() != WL_CONNECTED) return false;

  ingestMaybeRetryPrimary();

  Sample chunk[POST_CHUNK];
  const size_t n = ringSnapshot(chunk, POST_CHUNK);
  const char *url = activeIngestUrl();

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

    // 只送有效 IMU（避免 SPI 浮接 / 假零把 Grafana 壓成 0g / 25°C）
    if (s.has_imu) {
      o["ax"] = s.ax;
      o["ay"] = s.ay;
      o["az"] = s.az;
      o["gx"] = s.gx;
      o["gy"] = s.gy;
      o["gz"] = s.gz;
      if (!isnan(s.imu_temp_c)) o["imu_temp_c"] = s.imu_temp_c;
      o["accel_mag"] = s.accel_mag;
    }

    if (s.has_gps) {
      o["gps_lat"] = s.gps_lat;
      o["gps_lon"] = s.gps_lon;
      if (!isnan(s.gps_speed_mps)) o["gps_speed_mps"] = s.gps_speed_mps;
      if (!isnan(s.gps_course_deg)) o["gps_course_deg"] = s.gps_course_deg;
      if (!isnan(s.gps_alt_m)) o["gps_alt_m"] = s.gps_alt_m;
      if (!isnan(s.gps_hdop)) o["gps_hdop"] = s.gps_hdop;
      o["gps_satellites"] = s.gps_satellites;
      o["gps_fresh"] = s.gps_fresh ? 1 : 0;
    }

    // 有新 fix 才寫 gps_tracks（避免每 40ms 重複灌）
    JsonArray tracks = o["gps_tracks"].to<JsonArray>();
    appendTrack(tracks, "m10180c", s.m10, s.has_m10 && s.m10_fresh);
    appendTrack(tracks, "neo6m", s.neo, s.has_neo && s.neo_fresh);
    if (tracks.size() == 0) o.remove("gps_tracks");
  }

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  const bool isHttps = String(url).startsWith("https://");
  http.setTimeout(isHttps ? 15000 : 3000);
  http.setConnectTimeout(isHttps ? 8000 : 1500);
  http.setReuse(!isHttps);

  bool began = false;
  if (isHttps) {
    secureClient.setInsecure();
    began = http.begin(secureClient, url);
  } else {
    began = http.begin(plainClient, url);
  }
  if (!began) {
    if (!ingestUseFallback) ingestPreferFallback("begin");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + INGEST_TOKEN);

  int code = http.POST(body);
  http.getString();
  http.end();

  if (code >= 200 && code < 300) {
    ringPopFront(n);
    LOGF("[http] ok %d wrote=%u left=%u\n", code, (unsigned)n,
         (unsigned)ringSize());
    return true;
  }
  if (!ingestUseFallback && (code < 0 || code >= 500)) {
    ingestPreferFallback(code < 0 ? "conn" : "5xx");
  }
  return false;
}

void setup() {
#ifdef DUAL_GPS_DEBUG
  Serial.begin(115200);
  delay(200);
  LOGLN("\n[boot] ESP32 DUAL_GPS DEBUG (M10 勿接 UART0)");
#else
  // UART0 @ 9600 for M10 — no debug prints
  m10Serial.begin(GPS_BAUD);
  delay(50);
#endif

  neoSerial.begin(GPS_BAUD, SERIAL_8N1, PIN_NEO_RX, PIN_NEO_TX);

  connectWifi();

  if (!icmInit()) {
    LOGLN("[icm] init FAILED");
  }

  xTaskCreatePinnedToCore(sampleTask, "sample", 4096, nullptr, 1, nullptr, 1);
}

void loop() {
  static uint32_t lastPost = 0;
  static uint32_t lastWifi = 0;
  static uint32_t lastNtpResync = 0;

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

  const size_t pending = ringSize();
  const uint32_t postEvery =
      (pending > POST_CHUNK * 2) ? POST_BURST_MS : POST_PERIOD_MS;
  if (now - lastPost >= postEvery && pending > 0) {
    const int bursts =
        (pending > POST_CHUNK * 3) ? 5 : (pending > POST_CHUNK ? 2 : 1);
    for (int i = 0; i < bursts; i++) {
      if (!postBatch()) break;
      if (ringSize() == 0) break;
    }
    lastPost = millis();
  }
}
