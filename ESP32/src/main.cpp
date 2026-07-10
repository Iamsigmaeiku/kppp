/**
 * ESP32-WROOM-32 + ICM-42688-P (I2C) + DHT11 → HTTP ingest
 *
 * Pins (per project wiring):
 *   ICM42688: 3V3, GND, SDA=GPIO21, SCL=GPIO22
 *   DHT11:    VCC, GND, DATA=GPIO15
 *
 * Datasheet refs:
 *   ICM-42688-P DS-000347: WHO_AM_I=0x47 @0x75, TEMP/ACCEL/GYRO @0x1D..,
 *   PWR_MGMT0 @0x4E, °C = TEMP/132.48 + 25
 *   DHT11: single-bus, ≤1 Hz
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <math.h>
#include <secrets.h>

// ---- Pins ----
static constexpr int PIN_SDA = 21;
static constexpr int PIN_SCL = 22;
static constexpr int PIN_DHT = 15;

// ---- ICM-42688-P (Bank 0) ----
static constexpr uint8_t ICM_ADDR_LOW = 0x68;
static constexpr uint8_t ICM_ADDR_HIGH = 0x69;
static constexpr uint8_t ICM_WHO_AM_I_REG = 0x75;
static constexpr uint8_t ICM_WHO_AM_I_VAL = 0x47;
static constexpr uint8_t ICM_DEVICE_CONFIG = 0x11;
static constexpr uint8_t ICM_PWR_MGMT0 = 0x4E;
static constexpr uint8_t ICM_GYRO_CONFIG0 = 0x4F;
static constexpr uint8_t ICM_ACCEL_CONFIG0 = 0x50;
static constexpr uint8_t ICM_TEMP_DATA1 = 0x1D;

// ±16g → 2048 LSB/g ; ±2000 dps → 16.4 LSB/(dps)
static constexpr float ACCEL_SENS = 2048.0f;
static constexpr float GYRO_SENS = 16.4f;

static constexpr uint32_t IMU_PERIOD_MS = 40;      // 25 Hz sample
static constexpr uint32_t POST_PERIOD_MS = 400;    // batch upload
static constexpr uint32_t DHT_PERIOD_MS = 2000;    // DHT11 min ~1s
static constexpr size_t MAX_BATCH = 12;

static DHT dht(PIN_DHT, DHT11);
static uint8_t icmAddr = ICM_ADDR_LOW;

struct Sample {
  float ax, ay, az, gx, gy, gz, imu_temp_c, accel_mag;
  float dht_temp_c, dht_humidity;
  bool has_dht;
  uint32_t ts_ms;
};

static Sample batch[MAX_BATCH];
static size_t batchCount = 0;
static float lastDhtTemp = NAN;
static float lastDhtHum = NAN;
static bool dhtOk = false;

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

static bool icmReadSample(Sample &out);  // forward decl — used by icmInit

static bool icmInit() {
  if (!icmProbe()) return false;

  // 不做 soft reset：部分模組 reset 後 I2C 位址/匯流排會短暫亂掉
  // （你之前看到 0x69 → lost → 0x68）

  // gyro LN + accel LN → 0x0F
  if (!icmWriteRetry(ICM_PWR_MGMT0, 0x0F)) return false;
  delay(45);

  // ODR 100Hz、FS ±2000 dps / ±16g
  if (!icmWriteRetry(ICM_GYRO_CONFIG0, 0x08)) return false;
  if (!icmWriteRetry(ICM_ACCEL_CONFIG0, 0x08)) return false;
  delay(20);

  // 立刻讀一包確認
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
  out.ts_ms = millis();
  out.has_dht = dhtOk;
  out.dht_temp_c = lastDhtTemp;
  out.dht_humidity = lastDhtHum;
  return true;
}

static void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[wifi] connecting to %s", WIFI_SSID);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] ok ip=%s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[wifi] FAILED — will keep retrying");
  }
}

static bool postBatch() {
  if (batchCount == 0) {
    Serial.println("[http] skip (no samples)");
    return true;
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[http] skip (wifi down)");
    WiFi.reconnect();
    return false;
  }

  Serial.printf("[http] posting %u sample(s) → %s\n", (unsigned)batchCount, INGEST_URL);

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  if (CAR_ID[0] != '\0') doc["car_id"] = CAR_ID;
  JsonArray samples = doc["samples"].to<JsonArray>();

  for (size_t i = 0; i < batchCount; i++) {
    const Sample &s = batch[i];
    JsonObject o = samples.add<JsonObject>();
    o["ax"] = s.ax;
    o["ay"] = s.ay;
    o["az"] = s.az;
    o["gx"] = s.gx;
    o["gy"] = s.gy;
    o["gz"] = s.gz;
    o["imu_temp_c"] = s.imu_temp_c;
    o["accel_mag"] = s.accel_mag;
    // 不送 ts_ms：millis() 不是 Unix epoch，交給伺服器用接收時間戳
    if (s.has_dht && !isnan(s.dht_temp_c)) {
      o["dht_temp_c"] = s.dht_temp_c;
      o["dht_humidity"] = s.dht_humidity;
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
    secureClient.setInsecure();  // 場地用；正式可改釘選 CA
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
    Serial.printf("[http] ok %d wrote~%u\n", code, (unsigned)batchCount);
    batchCount = 0;
    return true;
  }
  Serial.printf("[http] fail code=%d body=%s\n", code, resp.c_str());
  // 失敗也清掉，避免 batch 無限堆、一直重送舊資料
  batchCount = 0;
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[boot] ESP32 ICM42688+DHT11 telemetry");

  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(400000);

  dht.begin();
  delay(1000);  // DHT11 power-up settle

  if (!icmInit()) {
    Serial.println("[icm] init FAILED — check wiring / I2C addr");
  } else {
    Serial.println("[icm] ready ±16g / ±2000dps");
  }

  connectWifi();
}

void loop() {
  static uint32_t lastImu = 0;
  static uint32_t lastPost = 0;
  static uint32_t lastDht = 0;
  static uint32_t lastWifi = 0;
  static uint32_t icmFailStreak = 0;

  uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED && now - lastWifi > 5000) {
    lastWifi = now;
    Serial.println("[wifi] reconnect…");
    WiFi.reconnect();
  }

  if (now - lastDht >= DHT_PERIOD_MS) {
    lastDht = now;
    float h = dht.readHumidity();
    float t = dht.readTemperature();
    if (!isnan(h) && !isnan(t)) {
      lastDhtHum = h;
      lastDhtTemp = t;
      dhtOk = true;
      Serial.printf("[dht] T=%.1f H=%.1f\n", t, h);
    } else {
      Serial.println("[dht] read fail");
    }
  }

  if (now - lastImu >= IMU_PERIOD_MS) {
    lastImu = now;
    if (batchCount < MAX_BATCH) {
      Sample s{};
      if (icmReadSample(s)) {
        icmFailStreak = 0;
        batch[batchCount++] = s;
      } else {
        icmFailStreak++;
        if (icmFailStreak == 1 || icmFailStreak % 25 == 0) {
          Serial.printf("[icm] read fail x%lu (addr=0x%02X)\n",
                        (unsigned long)icmFailStreak, icmAddr);
        }
        // IMU 掛了也至少把 DHT 送上去，否則永遠看不到 [http]
        if (dhtOk && batchCount == 0) {
          Sample d{};
          d.has_dht = true;
          d.dht_temp_c = lastDhtTemp;
          d.dht_humidity = lastDhtHum;
          d.ts_ms = now;
          batch[batchCount++] = d;
        }
      }
    }
  }

  if (now - lastPost >= POST_PERIOD_MS || batchCount >= MAX_BATCH) {
    lastPost = now;
    postBatch();
  }
}
