/**
 * ESP32-WROOM-32 → HTTP ingest（esp32dev）
 *
 *   ICM-42688-P (SPI) + DHT11 + M10-180C GPS + 2D ESKF
 *
 * Pins:
 *   ICM42688 SPI: 3V3, GND, SCK=18, MISO=19, MOSI=23, CS=5 (AD0→GND)
 *   DHT11:        VCC, GND, DATA=GPIO15
 *   M10-180C:     VCC(3V3/5V), GND, TXD→GPIO16(UART1 RX), RXD→GPIO17(UART1 TX)
 *                 走 UART1（16/17）；不要接 GPIO1/3 — 那是 esp32-dual-gps 的 UART0
 *
 * Offline: RAM ring；NTP 非阻塞；採樣在 core1，HTTP 在 loop（core0）。
 * 第二顆板（GY-85+MPU+NEO）見 main_imu2.cpp / env esp32-imu2-gps。
 */

#include <Arduino.h>
#include <DHT.h>
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
#include "Icm42688Spi.h"
#include "GpsImuEskf.h"

#ifndef INGEST_URL_FALLBACK
#define INGEST_URL_FALLBACK ""
#endif

// HTTPS + 大 JSON 在 loop 預設 8KB stack 會爆 canary；拉高再跑 postBatch
SET_LOOP_TASK_STACK_SIZE(24 * 1024);

#ifndef WIFI_SSID2
#define WIFI_SSID2 WIFI_SSID
#define WIFI_PASS2 WIFI_PASS
#endif

// ---- Pins ----
// GPIO15 是 strapping pin；若 DHT 一直 nan，可在 secrets.h 加 #define PIN_DHT 4 並改接線
#ifndef PIN_DHT
#define PIN_DHT 15
#endif
static constexpr int PIN_GPS_RX = 16;  // ESP32 RX ← GPS TXD（esp32dev: M10-180C）
static constexpr int PIN_GPS_TX = 17;  // ESP32 TX → GPS RXD
static constexpr uint32_t GPS_BAUD = 9600;
// 新鮮 fix（狀態列「已定位」）：<2s；地圖/ingest 持續上報 hold：<20s（短暫樹蔭丟星仍畫軌）
static constexpr uint32_t GPS_FIX_FRESH_MS = 2000;
static constexpr uint32_t GPS_FIX_HOLD_MS = 20000;

static constexpr uint32_t IMU_PERIOD_MS = 20;  // 50 Hz（抓彎道尖峰）
static constexpr uint32_t POST_PERIOD_MS = 200;
static constexpr uint32_t POST_BURST_MS = 20;
static constexpr uint32_t DHT_PERIOD_MS = 2000;
static constexpr uint32_t NTP_RESYNC_MS = 3600000;
static constexpr uint32_t NTP_RETRY_MS = 15000;
static constexpr size_t POST_CHUNK = 25;  // LAN 大口；放 stack 不佔 BSS
static constexpr size_t RING_CAP = 600;  // ~24s @25Hz（省 BSS；靠 LAN 抽乾）
static constexpr uint32_t INGEST_PRIMARY_RETRY_MS = 300000;

static DHT dht(PIN_DHT, DHT11);
static Icm42688Spi icm;
static GpsImuEskf eskf;

static HardwareSerial gpsSerial(1);
static TinyGPSPlus gps;

struct Sample {
  float ax, ay, az, gx, gy, gz, imu_temp_c, accel_mag;
  float accel_dyn;  // |a - g_lp|：去重力後動態加速度（彎道/煞車看得見）
  float a_lon, a_lat;  // 水平面動態（本板重力在 +Y → lon≈X, lat≈Z）
  float dht_temp_c, dht_humidity;
  bool has_dht;
  bool has_imu;  // false：不要送 ax=0 假零
  float gps_lat, gps_lon, gps_speed_mps, gps_course_deg, gps_alt_m, gps_hdop;
  uint32_t gps_satellites;
  bool has_gps;
  bool gps_fresh;  // age < GPS_FIX_FRESH_MS
  float lat_dr, lon_dr, heading_dr_deg, speed_dr_mps;
  bool has_dr;
  uint32_t millis_at;
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
      if (gps.location.isValid() && gps.location.age() < GPS_FIX_HOLD_MS) {
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

static constexpr uint32_t GPS_BAUD_FAST = 38400;

// M10-180C (u-blox M10, protver 34.x): use UBX-CFG-VALSET, not legacy
// CFG-RATE / CFG-NAV5 / CFG-PRT / CFG-MSG. Interface Description §1.3 strongly
// prefers the Configuration interface; §4.10 maps some legacy fields but notes
// incomplete / non-1:1 availability. UBX-CFG-MSG is absent from §4.10 — message
// rates must use CFG-MSGOUT-NMEA_ID_*_UART1 keys.
// Ref: u-blox-M10-SPG-5.10 Interface Description UBX-21035062.

static constexpr uint8_t UBX_LAYER_RAM_BBR = 0x03;  // bit0 RAM | bit1 BBR

static void ubxFletcher(const uint8_t *payload, size_t len, uint8_t &ckA,
                        uint8_t &ckB) {
  ckA = 0;
  ckB = 0;
  for (size_t i = 0; i < len; i++) {
    ckA = (uint8_t)(ckA + payload[i]);
    ckB = (uint8_t)(ckB + ckA);
  }
}

// Key size from bits [30:28] of key ID (u-blox Configuration interface).
static size_t ubxCfgValueSize(uint32_t keyId) {
  switch ((keyId >> 28) & 0x07u) {
    case 1:
      return 1;  // one bit / L stored as U1
    case 2:
      return 1;  // U1 / E1 / X1
    case 3:
      return 2;  // U2 / E2 / X2
    case 4:
      return 4;  // U4 / E4 / X4 / R4
    case 5:
      return 8;  // U8 / R8 / X8
    default:
      return 1;
  }
}

static void ubxSendValSet(uint8_t layers, const uint32_t *keys,
                          const uint32_t *vals, size_t n) {
  // Header(4) + version/layers/reserved(4) + up to n*(4+8) + CK(2)
  uint8_t pkt[8 + 12 * 12 + 2];
  size_t off = 0;
  pkt[off++] = 0xB5;
  pkt[off++] = 0x62;
  pkt[off++] = 0x06;
  pkt[off++] = 0x8A;  // UBX-CFG-VALSET
  const size_t lenPos = off;
  pkt[off++] = 0;  // length lo (fill later)
  pkt[off++] = 0;  // length hi
  pkt[off++] = 0x00;  // version 0 (transactionless)
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
  gpsSerial.write(pkt, off);
  gpsSerial.flush();
}

static void gpsSendConfigSuite() {
  // Rate + dynModel + NMEA filter (no baud — that is sent separately before
  // ESP switches UART speed).
  static const uint32_t kKeys[] = {
      0x30210001u,  // CFG-RATE-MEAS (U2, 0.001 s)
      0x30210002u,  // CFG-RATE-NAV (U2)
      0x20110021u,  // CFG-NAVSPG-DYNMODEL (E1) AUTOMOTIVE=4
      0x209100bbu,  // CFG-MSGOUT-NMEA_ID_GGA_UART1
      0x209100acu,  // CFG-MSGOUT-NMEA_ID_RMC_UART1
      0x209100cau,  // CFG-MSGOUT-NMEA_ID_GLL_UART1
      0x209100c0u,  // CFG-MSGOUT-NMEA_ID_GSA_UART1
      0x209100c5u,  // CFG-MSGOUT-NMEA_ID_GSV_UART1
      0x209100b1u,  // CFG-MSGOUT-NMEA_ID_VTG_UART1
  };
  static const uint32_t kVals[] = {
      200u,  // 200 ms → 5 Hz
      1u,    // one nav per measurement
      4u,    // Automotive
      1u,    // GGA on
      1u,    // RMC on
      0u,    // GLL off
      0u,    // GSA off
      0u,    // GSV off
      0u,    // VTG off
  };
  ubxSendValSet(UBX_LAYER_RAM_BBR, kKeys, kVals,
                sizeof(kKeys) / sizeof(kKeys[0]));
  delay(40);
}

static void gpsSendBaudFast() {
  static const uint32_t kKeys[] = {0x40520001u};  // CFG-UART1-BAUDRATE (U4)
  static const uint32_t kVals[] = {GPS_BAUD_FAST};
  ubxSendValSet(UBX_LAYER_RAM_BBR, kKeys, kVals, 1);
}

static void gpsConfigure() {
  // 先在 9600 送 VALSET，再切 38400；失敗則退回 9600（模組可能預設 9600 或已在 38400）
  gpsSendConfigSuite();
  delay(40);
  gpsSendBaudFast();
  delay(120);
  gpsSerial.end();
  delay(40);
  gpsSerial.begin(GPS_BAUD_FAST, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  delay(80);
  gpsSendConfigSuite();  // 若已在 38400（BBR），第一輪 9600 會打空，這裡補打

  const uint32_t t0 = millis();
  bool got = false;
  while (millis() - t0 < 1500) {
    if (gpsSerial.available()) {
      got = true;
      break;
    }
    delay(10);
  }
  if (!got) {
    Serial.println("[gps] no UART @38400 → fallback 9600");
    gpsSerial.end();
    delay(40);
    gpsSerial.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
    delay(80);
    gpsSendConfigSuite();
  } else {
    Serial.println("[gps] M10 VALSET: 5Hz + Automotive + GGA/RMC @38400");
  }
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
  return (millis() - out.updated_at_ms) < GPS_FIX_HOLD_MS;
}

static bool gpsIsFresh(const GpsFix &fix) {
  return fix.valid && (millis() - fix.updated_at_ms) < GPS_FIX_FRESH_MS;
}

static Sample ring[RING_CAP];
static size_t ringHead = 0;
static size_t ringCount = 0;
static uint32_t ringDropped = 0;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

static volatile float lastDhtTemp = NAN;
static volatile float lastDhtHum = NAN;
static volatile bool dhtOk = false;
static volatile uint32_t dhtFailStreak = 0;
static volatile uint32_t imuOkCount = 0;
static volatile uint32_t imuFailCount = 0;

static bool dhtTryRead(bool force) {
  // 單次回線：先 force read，再用 cache 取 T/H（避免連續兩次 bitbang）
  if (!dht.read(force)) {
    dhtFailStreak++;
    return false;
  }
  const float t = dht.readTemperature(false);
  const float h = dht.readHumidity(false);
  if (isnan(t) || isnan(h)) {
    dhtFailStreak++;
    return false;
  }
  lastDhtTemp = t;
  lastDhtHum = h;
  dhtOk = true;
  dhtFailStreak = 0;
  return true;
}

static volatile bool timeOk = false;
static volatile int64_t millisToUnixOffset = 0;

static bool ntpKicked = false;
static uint32_t ntpKickAt = 0;

// true = 暫時打 FALLBACK；逾時後試回 PRIMARY
static bool ingestUseFallback = false;
static uint32_t ingestFallbackSince = 0;

static const char *activeIngestUrl() {
  if (INGEST_URL_FALLBACK[0] == '\0') return INGEST_URL;
  if (ingestUseFallback) return INGEST_URL_FALLBACK;
  return INGEST_URL;
}

static void ingestPreferFallback(const char *why) {
  if (INGEST_URL_FALLBACK[0] == '\0') return;
  if (!ingestUseFallback) {
    Serial.printf("[http] switch → FALLBACK (%s)\n", why);
  }
  ingestUseFallback = true;
  ingestFallbackSince = millis();
}

static void ingestMaybeRetryPrimary() {
  if (!ingestUseFallback || INGEST_URL_FALLBACK[0] == '\0') return;
  if (millis() - ingestFallbackSince < INGEST_PRIMARY_RETRY_MS) return;
  Serial.println("[http] retry PRIMARY after fallback hold");
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
  out.gps_fresh = false;
  if (out.has_gps) {
    out.gps_lat = fix.lat;
    out.gps_lon = fix.lon;
    out.gps_speed_mps = fix.speed_mps;
    out.gps_course_deg = fix.course_deg;
    out.gps_alt_m = fix.alt_m;
    out.gps_hdop = fix.hdop;
    out.gps_satellites = fix.satellites;
    out.gps_fresh = gpsIsFresh(fix);
  }
}

static bool icmReadSample(Sample &out);

static bool icmInit() { return icm.begin(); }

static bool icmReadSample(Sample &out) {
  IcmSample raw{};
  if (!icm.readSample(raw)) {
    out.has_imu = false;
    return false;
  }

  out.imu_temp_c = raw.imu_temp_c;
  out.ax = raw.ax;
  out.ay = raw.ay;
  out.az = raw.az;
  out.gx = raw.gx;
  out.gy = raw.gy;
  out.gz = raw.gz;
  out.accel_mag = raw.accel_mag;

  // 慢速 LPF 估重力，dyn = |a - g| → 彎道/煞車尖峰不會被 1g 蓋掉
  static float gax = 0, gay = 0, gaz = 0;
  static bool gWarm = false;
  constexpr float kGAlpha = 0.02f;  // ~1s 時間常數 @50Hz
  if (!gWarm) {
    gax = out.ax;
    gay = out.ay;
    gaz = out.az;
    gWarm = true;
  } else {
    gax += kGAlpha * (out.ax - gax);
    gay += kGAlpha * (out.ay - gay);
    gaz += kGAlpha * (out.az - gaz);
  }
  const float lx = out.ax - gax;
  const float ly = out.ay - gay;
  const float lz = out.az - gaz;
  out.accel_dyn = sqrtf(lx * lx + ly * ly + lz * lz);
  // 此安裝重力在 +Y（直立≈1g），水平動態 = X/Z，不要拿 ay 當 lateral
  out.a_lon = lx;
  out.a_lat = lz;

  out.millis_at = millis();
  out.has_imu = true;
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
          eskf.onGpsFix(fix.updated_at_ms, fix.lat, fix.lon, fix.speed_mps,
                       fix.course_deg, fix.hdop, fix.satellites);
        }

        const EskfOutput dr =
            eskf.tick(s.millis_at, s.ax, s.ay, s.az, s.gx, s.gy, s.gz);
        s.has_dr = dr.valid;
        if (dr.valid) {
          s.lat_dr = dr.lat_dr;
          s.lon_dr = dr.lon_dr;
          s.heading_dr_deg = dr.heading_deg;
          s.speed_dr_mps = dr.speed_mps;
        }

        if (dr.state == EskfState::CALIBRATING) {
          if (now - lastCalibLog > 1000) {
            lastCalibLog = now;
            Serial.printf("[eskf] CALIBRATING %u/%d (keep vehicle still)\n",
                          (unsigned)dr.bias_samples, ESKF_GYRO_BIAS_SAMPLES);
          }
        } else if (!loggedRunning) {
          loggedRunning = true;
          Serial.printf("[eskf] RUNNING bias_gz=%.4f dps\n", eskf.gyroBiasDps());
        }

        ringPush(s);
      } else {
        imuFailCount++;
        if (imuFailCount == 1 || imuFailCount % 50 == 0) {
          Serial.printf("[icm] SPI read fail x%lu\n",
                        (unsigned long)imuFailCount);
        }
        // IMU 掛了仍送 DHT/GPS，但不要灌 ax=0 假零
        if (dhtOk && now - lastDhtFallback >= DHT_PERIOD_MS) {
          Sample d{};
          d.has_imu = false;
          d.has_dht = true;
          d.dht_temp_c = lastDhtTemp;
          d.dht_humidity = lastDhtHum;
          d.millis_at = now;
          d.imu_temp_c = NAN;
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

  ingestMaybeRetryPrimary();

  Sample chunk[POST_CHUNK];
  const size_t n = ringSnapshot(chunk, POST_CHUNK);
  const char *url = activeIngestUrl();

  Serial.printf("[http] posting %u/%u → %s\n",
                (unsigned)n, (unsigned)pending, url);

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
    if (s.has_imu) {
      o["ax"] = s.ax;
      o["ay"] = s.ay;
      o["az"] = s.az;
      o["gx"] = s.gx;
      o["gy"] = s.gy;
      o["gz"] = s.gz;
      o["imu_temp_c"] = s.imu_temp_c;
      o["accel_mag"] = s.accel_mag;
      o["accel_dyn"] = s.accel_dyn;
      o["a_lon"] = s.a_lon;
      o["a_lat"] = s.a_lat;
    }
    if (s.has_dht && !isnan(s.dht_temp_c)) {
      o["dht_temp_c"] = s.dht_temp_c;
      o["dht_humidity"] = s.dht_humidity;
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
    if (s.has_dr) {
      o["lat_dr"] = s.lat_dr;
      o["lon_dr"] = s.lon_dr;
      o["dr_heading_deg"] = s.heading_dr_deg;
      o["dr_speed_mps"] = s.speed_dr_mps;
    }
  }

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  const bool isHttps = String(url).startsWith("https://");
  // 區網 HTTP：reuse + 短 timeout；HTTPS 隧道較慢
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
    Serial.println("[http] begin failed");
    if (!ingestUseFallback) ingestPreferFallback("begin");
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
  // 連線失敗 / 5xx → 切公開站；401 留下排錯
  if (!ingestUseFallback && (code < 0 || code >= 500)) {
    ingestPreferFallback(code < 0 ? "conn" : "5xx");
  }
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[boot] ESP32 ICM42688(SPI)+DHT11+GPS+ESKF (ring+NTP+core1 sample)");
  Serial.println("[boot] ESKF: keep still ~4s for gyro bias (200 samples @50Hz)");
  Serial.printf("[boot] device_id=%s ring cap=%u (~%us @25Hz) heap=%u\n",
                DEVICE_ID, (unsigned)RING_CAP, (unsigned)(RING_CAP / 25),
                (unsigned)ESP.getFreeHeap());

  connectWifi();

  gpsSerial.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  Serial.printf("[gps] M10-180C UART1 @ %lu baud, rx=%d tx=%d\n",
                (unsigned long)GPS_BAUD, PIN_GPS_RX, PIN_GPS_TX);
  delay(200);
  gpsConfigure();

  // DHT11 需要外部 4.7k pull-up；內部上拉當保險
  pinMode(PIN_DHT, INPUT_PULLUP);
  dht.begin();
  delay(1500);
  Serial.printf("[dht] probe pin=GPIO%d ...\n", PIN_DHT);
  bool dhtReady = false;
  for (int i = 0; i < 5; i++) {
    if (dhtTryRead(true)) {
      dhtReady = true;
      Serial.printf("[dht] ok T=%.1f H=%.1f\n", (double)lastDhtTemp,
                    (double)lastDhtHum);
      break;
    }
    Serial.printf("[dht] read fail try=%d\n", i + 1);
    delay(2200);
  }
  if (!dhtReady) {
    Serial.println("[dht] FAILED — check VCC/GND/DATA + 4.7k pull-up "
                   "(or #define PIN_DHT 4 in secrets.h + rewire)");
  }

  Serial.printf("[icm] SPI SCK=%d MISO=%d MOSI=%d CS=%d\n", ICM_SPI_SCK,
                ICM_SPI_MISO, ICM_SPI_MOSI, ICM_SPI_CS);
  if (!icmInit()) {
    Serial.println("[icm] init FAILED — check SPI wiring / CS not floating");
  } else {
    Serial.println("[icm] ready ±16g / ±2000dps (SPI)");
  }

  Serial.printf("[http] primary=%s\n", INGEST_URL);
  if (INGEST_URL_FALLBACK[0] != '\0') {
    Serial.printf("[http] fallback=%s\n", INGEST_URL_FALLBACK);
  }

  xTaskCreatePinnedToCore(sampleTask, "sample", 8192, nullptr, 1, nullptr, 1);
}

void loop() {
  static uint32_t lastPost = 0;
  static uint32_t lastStat = 0;
  static uint32_t lastWifi = 0;
  static uint32_t lastNtpResync = 0;
  static uint32_t lastImuOk = 0;
  static uint32_t lastImuFail = 0;

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

    dhtTryRead(dhtFailStreak > 0);

    const uint32_t ok = imuOkCount;
    const uint32_t fail = imuFailCount;
    const uint32_t okDelta = ok - lastImuOk;
    const uint32_t failDelta = fail - lastImuFail;
    lastImuOk = ok;
    lastImuFail = fail;
    Serial.printf("[stat] dht=%.1f/%.1f ok=%d fail=%lu ring=%u imu_ok=%lu/2s fail=%lu ntp=%d "
                  "gps=%d sats=%lu/%lu hdop=%.1f sentences=%lu chars=%lu badck=%lu "
                  "locValid=%d age=%lu\n",
                  (double)lastDhtTemp, (double)lastDhtHum, dhtOk ? 1 : 0,
                  (unsigned long)dhtFailStreak,
                  (unsigned)ringSize(),
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
    // 積壓時連發多包把 ring 抽乾（LAN ~幾十 ms/包）
    const int bursts = (pending > POST_CHUNK * 3) ? 5 : (pending > POST_CHUNK ? 2 : 1);
    for (int i = 0; i < bursts; i++) {
      if (!postBatch()) break;
      if (ringSize() == 0) break;
    }
    lastPost = millis();
  }
}
