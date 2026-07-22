# KPP 賽道 Kiosk（Flutter Hybrid）

Android 手機／平板 App：原生即時圈速、賽道地圖、排行榜；登入／場次明細／AI 教練走 WebView。連本場 Pi 上的 FastAPI（預設 `:8000`），**不**在平板跑 decoder／Influx。

## 後端準備

在 Pi／Windows 的 `.env`：

```env
KIOSK_TOKEN=換成夠長的隨機字串
```

重啟 `decoder_ingest --with-dashboard`。帶 header `X-Kiosk-Token` 可讀：

- `WS /ws/laps`、`WS /ws/positions`
- `GET /api/leaderboard`、`GET /api/sessions`

寫入（歸檔、綁車、AI）仍要 Google／dev 登入。

## 建 APK

```bash
cd mobile/kpp_kiosk
flutter pub get
flutter build apk --release
# 產物：build/app/outputs/flutter-apk/app-release.apk
```

側載：`adb install -r build/app/outputs/flutter-apk/app-release.apk`

首次開啟填：

- Server URL：`http://<Pi>:8000`
- KIOSK_TOKEN：與後端相同
- 解鎖 PIN（預設 `2468`）— 進設定用

「測試 /health」確認網路通再儲存。

## 賽道平板 Kiosk

1. **亮屏／橫向／開機啟動**：Manifest 已設 landscape、`BOOT_COMPLETED`、`KEEP_SCREEN_ON`。
2. **Lock Task（螢幕固定）**  
   - 簡易：設定開啟「螢幕固定」後，由 App 呼叫 `startLockTask`（設定頁開關）。  
   - 正式場邊：設成 **device owner** 並 whitelist 本 package，Home／Overview 會被鎖住。

```bash
# 僅示範：需先 factory reset 且無帳號，再設 device owner（危險，現場平板專用）
adb shell dpm set-device-owner com.kpp.kpp_kiosk/.DeviceAdminReceiver
```

目前專案以 `startLockTask`／系統「螢幕固定」為主；若要完整 device-owner，需再加 `DeviceAdminReceiver` 與政策 XML（可依現場 MDM 另做）。

3. **解鎖設定**：長按頂欄「KPP」或齒輪 → 輸入 PIN → 可暫時 `stopLockTask`、改 URL。

## 架構對照

| 畫面 | 實作 |
|------|------|
| 即時面板 | 原生 ← `/ws/laps` |
| 賽道地圖 | 原生 ← `/ws/positions` + 內建 track PNG |
| 排行榜 | 原生 ← `/api/leaderboard` |
| 登入／場次／profile／AI | WebView 同源 cookie |

寬螢幕（≥900px）在「即時」分頁採左 timing／右 map 雙欄。
