#pragma once
/**
 * 共用 ArduinoOTA helper。
 * OTA_ENABLE=0 時全部 no-op（比賽可關）。
 */
#ifndef OTA_ENABLE
#define OTA_ENABLE 1
#endif

#if OTA_ENABLE

#include <ArduinoOTA.h>

inline bool &kppOtaStartedFlag() {
  static bool started = false;
  return started;
}

inline void kppOtaBegin(const char *hostname, const char *password) {
  if (kppOtaStartedFlag()) return;
  if (hostname && hostname[0]) {
    ArduinoOTA.setHostname(hostname);
  }
  if (password && password[0]) {
    ArduinoOTA.setPassword(password);
  }
  ArduinoOTA.onStart([]() { Serial.println("[ota] start"); });
  ArduinoOTA.onEnd([]() { Serial.println("\n[ota] end"); });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    static unsigned last_pct = 999;
    const unsigned pct = total ? (progress * 100 / total) : 0;
    if (pct != last_pct && (pct % 10 == 0 || pct == 100)) {
      last_pct = pct;
      Serial.printf("[ota] %u%%\n", pct);
    }
  });
  ArduinoOTA.onError([](ota_error_t err) {
    Serial.printf("[ota] err %u\n", (unsigned)err);
  });
  ArduinoOTA.begin();
  kppOtaStartedFlag() = true;
  Serial.printf("[ota] ready hostname=%s\n", hostname ? hostname : "?");
}

inline void kppOtaLoop() {
  if (kppOtaStartedFlag()) {
    ArduinoOTA.handle();
  }
}

inline void kppOtaReset() { kppOtaStartedFlag() = false; }

#else  // !OTA_ENABLE

inline void kppOtaBegin(const char *, const char *) {}
inline void kppOtaLoop() {}
inline void kppOtaReset() {}

#endif
