    /**
 * ESP32-WROOM-32 + ICM-42688-P (I2C) + DHT11 + NEO-6M GPS (UART1) → HTTP ingest
 *
 * Pins (per project wiring):
 *   ICM42688: 3V3, GND, SDA=GPIO21, SCL=GPIO22
 *   DHT11:    VCC, GND, DATA=GPIO15
 *   NEO-6M:   VCC(3V3/5V per module), GND, TXD→GPIO16(UART1 RX), RXD→GPIO17(UART1 TX)
 *
 * Offline: RAM ring；NTP 非阻塞；IMU+GPS 採樣在 core1，HTTP 在 loop（core0），
 * 避免 HTTPS 卡住時丟 25Hz 資料或吃滿 GPS UART buffer。
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <TinyGPSPlus.h>
#include <math.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <secrets.h>

#ifndef WIFI_SSID2
#define WIFI_SSID2 WIFI_SSID
#define WIFI_PASS2 WIFI_PASS
#endif

// ---- Pins ----
static constexpr int PIN_SDA = 21;
static constexpr int PIN_SCL = 22;
static constexpr int PIN_DHT = 15;
static constexpr int PIN_GPS_RX = 16;  // ESP32 RX ← NEO-6M TXD
static constexpr int PIN_GPS_TX = 17;  // ESP32 TX → NEO-6M RXD
static constexpr uint32_t GPS_BAUD = 9600;
static constexpr uint32_t GPS_FIX_STALE_MS = 5000;  // 超過視為沒訊號，不隨包送出

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

static constexpr uint32_t IMU_PERIOD_MS = 40;       // 25 Hz
static constexpr uint32_t POST_PERIOD_MS = 400;
static constexpr uint32_t POST_BURST_MS = 50;
static constexpr uint32_t DHT_PERIOD_MS = 2000;
static constexpr uint32_t NTP_RESYNC_MS = 3600000;
static constexpr uint32_t NTP_RETRY_MS = 15000;
static constexpr size_t POST_CHUNK = 50;
static constexpr size_t RING_CAP = 900;             // ~36s @25Hz（Sample 加 GPS 欄位變大，縮小筆數維持原本 DRAM 用量）

static DHT dht(PIN_DHT, DHT11);
static uint8_t icmAddr = ICM_ADDR_LOW;

static HardwareSerial gpsSerial(1);  // UART1，腳位在 setup() 用 begin() remap 到 16/17
static TinyGPSPlus gps;

struct Sample {
  float ax, ay, az, gx, gy, gz, imu_temp_c, accel_mag;
  float dht_temp_c, dht_humidity;
  bool has_dht;
  float gps_lat, gps_lon, gps_speed_mps, gps_course_deg, gps_alt_m, gps_hdop;
  uint32_t gps_satellites;
  bool has_gps;
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

static void gpsPoll() {
  while (gpsSerial.available()) {
    if (gps.encode(gpsSerial.read())) {
      gpsSentenceCount++;
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
  return (millis() - out.updated_at_ms) < GPS_FIX_STALE_MS;
}

static Sample ring[RING_CAP];
static size_t ringHead = 0;
static size_t ringCount = 0;
static uint32_t ringDropped = 0;
static portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

static volatile float lastDhtTemp = NAN;
static volatile float lastDhtHum = NAN;
static volatile bool dhtOk = false;

static volatile bool timeOk = false;
static volatile int64_t millisToUnixOffset = 0;

static volatile uint32_t imuOkCount = 0;
static volatile uint32_t imuFailCount = 0;

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

/** RFC1123: "Wed, 10 Jul 2026 13:05:00 GMT" */
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

/** iPhone 熱點常擋 UDP/123；改走 HTTPS Date（跟 ingest 同一條路） */
static bool syncTimeFromHttpUrl(const String &url) {
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  http.setTimeout(6000);
  const char *keys[] = {"Date"};
  http.collectHeaders(keys, 1);

  if (!http.begin(client, url)) return false;

  int code = http.sendRequest("HEAD");
  if (code < 0 || !http.hasHeader("Date")) {
    http.end();
    if (!http.begin(client, url)) return false;
    http.collectHeaders(keys, 1);
    code = http.GET();
  }

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

  // SNTP 3s 還沒好 → HTTPS Date（熱點常見）
  static uint32_t lastHttpTry = 0;
  if (millis() - ntpKickAt >= 3000 && millis() - lastHttpTry >= NTP_RETRY_MS) {
    lastHttpTry = millis();
    if (syncTimeFromHttp()) return;
  }

  if (millis() - ntpKickAt >= NTP_RETRY_MS) {
    kickNtp();
  }
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
  return true;
}

/** core1：只負責 IMU→ring，不被 HTTP/NTP 堵住 */
static void sampleTask(void *) {
  uint32_t lastImu = 0;
  uint32_t lastDhtFallback = 0;
  uint32_t lastDropLog = 0;

  for (;;) {
    uint32_t now = millis();

    gpsPoll();

    if (now - lastImu >= IMU_PERIOD_MS) {
      lastImu = now;
      Sample s{};
      if (icmReadSample(s)) {
        imuOkCount++;
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
    {WIFI_SSID2, WIFI_PASS2},
};
static constexpr size_t WIFI_NET_COUNT = sizeof(WIFI_NETS) / sizeof(WIFI_NETS[0]);
static size_t wifiNetIdx = 0;

static bool tryConnectWifi(size_t idx, uint32_t timeoutMs = 15000) {
  const WifiCred &c = WIFI_NETS[idx % WIFI_NET_COUNT];
  WiFi.disconnect(false, true);
  delay(100);
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
    Serial.printf("[wifi] ok ssid=%s ip=%s\n", c.ssid, WiFi.localIP().toString().c_str());
    timeOk = false;
    ntpKicked = false;
    kickNtp();
    return true;
  }
  Serial.printf("[wifi] fail %s\n", c.ssid);
  return false;
}

static void connectWifi() {
  WiFi.mode(WIFI_STA);
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
  if (!timeOk) {
    static uint32_t lastSkipLog = 0;
    if (millis() - lastSkipLog > 2000) {
      lastSkipLog = millis();
      Serial.printf("[http] wait time sync ring=%u\n", (unsigned)pending);
    }
    return false;
  }

  Sample chunk[POST_CHUNK];
  const size_t n = ringSnapshot(chunk, POST_CHUNK);

  Serial.printf("[http] posting %u/%u → %s\n",
                (unsigned)n, (unsigned)pending, INGEST_URL);

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  if (CAR_ID[0] != '\0') doc["car_id"] = CAR_ID;
  JsonArray samples = doc["samples"].to<JsonArray>();

  for (size_t i = 0; i < n; i++) {
    const Sample &s = chunk[i];
    JsonObject o = samples.add<JsonObject>();
    o["ax"] = s.ax;
    o["ay"] = s.ay;
    o["az"] = s.az;
    o["gx"] = s.gx;
    o["gy"] = s.gy;
    o["gz"] = s.gz;
    o["imu_temp_c"] = s.imu_temp_c;
    o["accel_mag"] = s.accel_mag;
    o["ts_ms"] = toUnixMs(s.millis_at);
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
    }
  }

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.setTimeout(8000);

  const bool isHttps = String(INGEST_URL).startsWith("https://");
  WiFiClientSecure secureClient;
  WiFiClient plainClient;

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
  Serial.println("\n[boot] ESP32 ICM42688+DHT11 (ring+NTP+core1 sample)");
  Serial.printf("[boot] ring cap=%u (~%us @25Hz)\n",
                (unsigned)RING_CAP, (unsigned)(RING_CAP / 25));

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);

  gpsSerial.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  Serial.printf("[gps] UART1 @ %lu baud, rx=%d tx=%d\n",
                (unsigned long)GPS_BAUD, PIN_GPS_RX, PIN_GPS_TX);

  dht.begin();
  delay(1000);

  if (!icmInit()) {
    Serial.println("[icm] init FAILED — check wiring / I2C addr");
  } else {
    Serial.println("[icm] ready ±16g / ±2000dps");
  }

  // 先開採樣再連 WiFi：連線/NTP 期間資料進 ring
  xTaskCreatePinnedToCore(sampleTask, "sample", 4096, nullptr, 1, nullptr, 1);

  connectWifi();
}

void loop() {
  static uint32_t lastPost = 0;
  static uint32_t lastDht = 0;
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

  if (now - lastDht >= DHT_PERIOD_MS) {
    lastDht = now;
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
    GpsFix fix{};
    bool gpsOk = gpsSnapshot(fix);
    Serial.printf("[stat] dht=%.1f/%.1f ring=%u imu_ok=%lu/2s fail=%lu ntp=%d "
                  "gps=%d sats=%lu hdop=%.1f sentences=%lu\n",
                  t, h, (unsigned)ringSize(),
                  (unsigned long)okDelta, (unsigned long)failDelta,
                  timeOk ? 1 : 0, gpsOk ? 1 : 0,
                  (unsigned long)fix.satellites, fix.hdop,
                  (unsigned long)gpsSentenceCount);
  }

  const size_t pending = ringSize();
  const uint32_t postEvery =
      (pending > POST_CHUNK * 2) ? POST_BURST_MS : POST_PERIOD_MS;
  if (now - lastPost >= postEvery && pending > 0) {
    postBatch();
    lastPost = millis();  // 用 POST 結束後的時間，避免立刻再送只帶 1 筆
  }
}
