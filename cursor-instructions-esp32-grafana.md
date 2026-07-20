# Cursor 指令包 — 雙 ESP32 韌體 + Grafana 排版/霍爾清除

> 我沒有動任何程式碼。這份文件是：(1) 我依 datasheet 排好、你直接照接的接線表，(2) 三份可以直接複製貼進 Cursor 的指令。
> 調查基礎（讀過的既有檔案）：`ESP32/platformio.ini`、`ESP32/src/main.cpp`、`ESP32/src/Icm42688Spi.h`、`ESP32/README.md`、`scripts/_gen_esp_grafana_dashboards.py`、`infra/grafana/dashboards/kart-telemetry.json`、`services/webapp/telemetry.py`、`services/webapp/templates/telemetry.html`。

---

## 0. 先講幾個會影響接線/指令的既有事實

1. 專案已經有 **ICM42688 SPI** 的驗證接線（`esp32dev` env）：`SCK=18 / MISO=19 / MOSI=23 / CS=5`，開機 log 會印 `WHO_AM_I=0x47` 做驗證。板子 #1 直接沿用，不要換腳位。
2. 專案已經有 **M10-180C** 的實戰紀錄（`esp32-dual-gps` env），但那邊是接 **UART0（GPIO1/3，跟燒錄共用）**，因為同時要接第二顆 NEO-6M 占用 UART2。你這次只有一顆 GPS，**不需要**佔用 UART0 — 我把 M10-180C 改配到 UART1（GPIO16/17），跟現有 `esp32dev` 的 GPS 腳位習慣一致，燒錄時不用拔線。
3. 專案舊的「第二顆 ESP32」現在是 `esp32-gps-hall` env（霍爾 GPIO36 + NEO-6M，`DEVICE_ID=esp32-kart-02`）。你要的 GY-85+NEO-6M-0-001+GY-521 板子**就是要取代這顆**，不是新增第三顆。
4. Grafana 看板 `infra/grafana/dashboards/kart-telemetry.json` 是由 `scripts/_gen_esp_grafana_dashboards.py` **產生**出來的，不是手改的。排版跟刪霍爾都要改 generator 再重跑，直接改 json 下次重跑會被蓋掉。
5. 霍爾字樣目前散落在 6 個地方：`ESP32/src/main.cpp`、`ESP32/platformio.ini`、`ESP32/README.md`、`scripts/_gen_esp_grafana_dashboards.py`、`infra/grafana/dashboards/kart-telemetry.json`、`services/webapp/telemetry.py`、`services/webapp/templates/telemetry.html`。三份指令會分工把這些清掉。

---

## 1. 接線表（照這個接，Cursor 不用管接線）

### 板子 #1：ESP32-WROOM-32 + M10-180C（GPS）+ ICM42688（SPI）

| 模組 | 腳位 | ESP32 | 備註 |
|---|---|---|---|
| **ICM42688** | VDD | 3V3 | |
| | GND | GND | |
| | SCLK | **GPIO18** | VSPI SCK |
| | SDI（MOSI，ESP32→IMU） | **GPIO23** | VSPI MOSI |
| | SDO（MISO，IMU→ESP32） | **GPIO19** | VSPI MISO |
| | CS / nCS | **GPIO5** | **不可浮接**；4 線 SPI 模式 |
| | AP_AD0（若板子有獨立 AD0 腳） | GND | 見下方註 ⚠️ |
| | INT1 / FSYNC | 不接 | 專案用輪詢（每 20ms 讀一次），不用中斷腳 |
| **M10-180C** | VCC | 5V | 模組吃 5V 較穩，board 上有自帶 LDO |
| | GND | GND | |
| | TXD（模組→ESP32） | **GPIO16**（UART1 RX） | |
| | RXD（ESP32→模組） | **GPIO17**（UART1 TX） | 設定 UBX 指令用 |
| （若保留 DHT11） | DATA | GPIO15（+4.7k 上拉） | 沿用既有 `esp32dev` 接法，非必要 |

⚠️ **ICM42688 的 AD0/SDO 腳位坑**：查 TDK 官方 datasheet（DS-000347），矽晶片上 Pin1 實際名稱是 `AP_SDO/AP_AD0` 共用腳 — 在 4 線 SPI 模式下這支腳的功能是 **SDO（也就是 MISO 訊號）**，理論上該接 ESP32 的 MISO，不是接地。但你們專案的 `esp32dev` 已經用「AD0→GND」的接法驗證過 `WHO_AM_I=0x47`，代表你買的那批 breakout 板把 AD0 選址腳跟 SDO 各自獨立引出成兩支不同腳位（中國賣場常見做法，仿 MPU9250 系列 pin-out）。結論：**照你板子上的絲印/既有接法接**（AD0 另外接地，SDO 接 MISO=19），開機後看 log 有沒有印 `WHO_AM_I=0x47`；如果你的板子只有 6 支腳、沒有獨立 AD0，就代表 AD0 跟 SDO 是同一支腳 → 那支只能接 MISO，不能接地。

Datasheet：[ICM-42688-P DS-000347](https://product.tdk.com/system/files/dam/doc/product/sensor/mortion-inertial/imu/data_sheet/ds-000347-icm-42688-p-v1.6.pdf)

---

### 板子 #2：ESP32-WROOM-32 + GY-85 + NEO-6M-0-001（GPS）+ GY-521（MPU6050）

| 模組 | 腳位 | ESP32 | 備註 |
|---|---|---|---|
| **GY-85**（ADXL345+ITG3205+HMC5883L，I2C） | VCC | **3V3** | 見下方電壓註 |
| | GND | GND | |
| | SDA | **GPIO21** | 與 GY-521 共用 I2C bus |
| | SCL | **GPIO22** | |
| **GY-521**（MPU6050，I2C） | VCC | **3V3** | |
| | GND | GND | |
| | SDA | **GPIO21** | 同一條 bus |
| | SCL | **GPIO22** | 同一條 bus |
| | **AD0** | **接 3V3（不要接 GND）** | ⚠️ 位址衝突見下方，必接 |
| | INT | 不接 | 輪詢，不用中斷 |
| **NEO-6M-0-001** | VCC | 5V | |
| | GND | GND | |
| | TXD | **GPIO16**（UART1 RX） | 跟板子 #1 同慣例 |
| | RXD | **GPIO17**（UART1 TX） | |

⚠️ **I2C 位址衝突，這個一定要接對**：GY-85 上的 ITG3205 陀螺儀預設位址是 `0x68`（AD0 腳在板上內部已接地）；GY-521 的 MPU6050 預設位址**也是** `0x68`。兩顆同時掛在同一條 I2C bus 會打架。解法：**GY-521 的 AD0 腳接 3.3V**，把它的位址改成 `0x69`，跟 ITG3205 的 `0x68` 錯開。

最終 I2C 位址表：

| 晶片 | 位址 | 來源 |
|---|---|---|
| ADXL345（GY-85 加速度計） | `0x53` | 固定，ALT ADDRESS 腳浮接 |
| ITG3205（GY-85 陀螺儀） | `0x68` | GY-85 板上預設 |
| HMC5883L（GY-85 磁力計） | `0x1E` | 固定，無選址腳 |
| MPU6050（GY-521） | `0x69` | **AD0 接 3.3V 才是這個值**，不接或接地會是 0x68 撞 ITG3205 |

上電後務必先跑一次 I2C bus scan（`Wire.h` 標準範例）確認掃到 `0x53 / 0x68 / 0x1E / 0x69` 四顆，再進下一步。

電壓與 I2C 電平註：GY-85 / GY-521 兩塊板子上都有自己的穩壓/上拉電阻，多數版本 VCC 接 3.3~5V 都能動，但為了讓 I2C 電平乾淨對齊 ESP32 的 3.3V 邏輯、且不用另外查你手上那批板子是否有做 5V→3.3V 電平轉換，**兩顆都直接用 ESP32 的 3V3 供電**最省事。若 HMC5883L 量測雜訊偏大再考慮改 5V 供電（電平不變，只是穩壓前級電壓）。兩塊板子的 I2C 上拉電阻會並聯（約 2.3k 等效），在這個線長/速率下沒問題，若 bus 不穩再考慮拔掉其中一塊的上拉。

參考來源：
- [GY-85 pinout / 位址 (Jungletronics)](https://medium.com/jungletronics/gy-85-a-quick-datasheet-study-79019bb36fbf)
- [GY-521 / MPU-6050 AD0 說明](https://mschoeffler.com/2017/10/05/tutorial-how-to-use-the-gy-521-module-mpu-6050-breakout-board-with-the-arduino-uno/)
- [MPU-6050 register map & AD0](https://lastminuteengineers.com/mpu6050-accel-gyro-arduino-tutorial/)

---

## 2. 三份 Cursor 指令（複製貼上即可，不用改）

每份指令都是獨立、完整的，貼哪份就先讓 Cursor 讀哪份提到的檔案再動手。建議順序：Prompt A → Prompt B → Prompt C（C 依賴 B 產生的新欄位名）。

### Prompt A — 韌體 #1：ESP32-WROOM + M10-180C + ICM42688(SPI)

```
你在 kpp 這個卡丁車遙測 monorepo 裡工作。先讀這些檔案再動手，不要用你自己的慣例覆蓋既有風格：
ESP32/platformio.ini、ESP32/src/main.cpp、ESP32/src/Icm42688Spi.h、ESP32/src/Icm42688Spi.cpp、
ESP32/src/GpsImuEskf.h、ESP32/src/GpsImuEskf.cpp、ESP32/README.md。

目標：讓現有 [env:esp32dev]（ICM42688 SPI + NEO-6M UART1 + 2D ESKF）改成搭配 M10-180C GPS 模組，
其餘架構（ring buffer、core1 sample task、ESKF、HTTP ingest）維持不變。

接線已經定案，直接照這個改程式碼裡的註解與腳位常數，不要改腳位本身：
- ICM42688 SPI：SCK=18 / MISO=19 / MOSI=23 / CS=5（不變)
- M10-180C GPS：接 UART1，TXD→GPIO16（PIN_GPS_RX）、RXD→GPIO17（PIN_GPS_TX），
  不是接 UART0/GPIO1/3（那是 esp32-dual-gps env 的用法，這裡不要那樣接）

要做的事：
1. 確認 M10-180C 用的是 u-blox M10 晶片。main.cpp 裡 gpsConfigure() 目前送的是舊版 UBX legacy
   訊息（UBX-CFG-RATE、UBX-CFG-NAV5、UBX-CFG-PRT、per-message CFG-MSG rate），這是給 NEO-6M（M8
   以前的韌體）用的。查一下 u-blox M10 Interface Description（protocol version 34.x，
   https://content.u-blox.com/sites/default/files/u-blox-M10-SPG-5.10_InterfaceDescription_UBX-21035062.pdf）
   確認這些 legacy 訊息在 M10 上是否還相容（文件裡有 "Legacy UBX message fields reference" 章節，
   通常相容但欄位語意可能有差）。如果相容就保留現有 UBX 封包只調整必要欄位；如果不完全相容，
   改用 M10 建議的 UBX-CFG-VALSET（設定 CFG-RATE-MEAS、CFG-NAVSPG-DYNMODEL、
   CFG-UART1-BAUDRATE、CFG-MSGOUT-UBX_NMEA_ID_GGA_UART1、CFG-MSGOUT-UBX_NMEA_ID_RMC_UART1
   這幾個 key）取代舊的 binary blob。兩種做法都要在 code comment 寫清楚選了哪個、為什麼。
2. 開機序列跟現有一樣：先用預設 9600 baud 送設定，成功後切到較高 baud（可沿用現有 38400，
   或依 M10 spec 評估是否能再拉高），失敗要 fallback 回 9600（沿用現有邏輯，不要砍掉容錯）。
3. main.cpp / README.md 裡所有寫死「NEO-6M」的註解、log 字串、變數命名，改成明確標示這是
   M10-180C（或用更中性的命名例如 GPS_MODULE，避免以後又要改一次）。
4. platformio.ini：確認 [env:esp32dev] 的 lib_deps／build_flags 是否需要為 M10 調整
   （TinyGPSPlus 純解 NMEA，理論上不用換 lib）。在檔頭註解更新硬體描述（目前寫
   "esp32dev — ICM-42688 (VSPI) + DHT11 + NEO-6M + 2D ESKF"，改成 M10-180C）。
5. DHT11 保留原樣不要動（GPIO15），除非你發現它跟這次改動有衝突。
6. ESKF（GpsImuEskf）邏輯不要動，它吃的是 GpsFix struct，跟換了哪顆 GPS 模組無關。
7. README.md 的腳位表（"腳位（esp32dev）" 那段）更新成 M10-180C，並補一句提醒：
   M10-180C 走 UART1（16/17），不要跟 esp32-dual-gps 的 UART0 接法搞混。

驗收：
- pio run -e esp32dev 要能編譯過
- 開機 log 要看到 WHO_AM_I=0x47、GPS UBX 設定成功或 fallback 訊息、以及每 2 秒一次的 [stat] 行
  裡有 gps sats/hdop 數字在動
- 不要留下任何暗示「這板子只能接 NEO-6M」的註解或變數名
```

### Prompt B — 韌體 #2：取代 esp32-gps-hall，換成 GY-85 + NEO-6M-0-001 + GY-521

```
你在 kpp 這個卡丁車遙測 monorepo 裡工作。先讀這些檔案：
ESP32/platformio.ini、ESP32/src/main.cpp（特別是所有 #ifdef PROFILE_GPS_HALL 區塊）、
ESP32/src/main_dual_gps.cpp（參考它「獨立 .cpp 檔 + 專屬 platformio env」的寫法）、
ESP32/README.md、services/webapp/telemetry.py、services/webapp/templates/telemetry.html。

背景：現有 [env:esp32-gps-hall] 是「霍爾 GPIO36 + NEO-6M，DEVICE_ID=esp32-kart-02」的第二顆
ESP32韌體，霍爾感測器已經不用了。這次要做的不是加第三顆板子，是把這顆板子換成新感測器組合。

新硬體（接線已定案，直接用這些腳位，不要改）：
- GY-85（ADXL345 0x53 + ITG3205 0x68 + HMC5883L 0x1E）：I2C，SDA=GPIO21，SCL=GPIO22
- GY-521 / MPU6050：I2C 同一條 bus，SDA=GPIO21，SCL=GPIO22，AD0 腳硬體上接 3.3V，
  所以位址是 0x69（不是預設的 0x68，因為要跟 ITG3205 的 0x68 錯開）
- NEO-6M-0-001 GPS：UART1，TXD→GPIO16，RXD→GPIO17（跟 esp32dev/M10 板子同慣例）
- DEVICE_ID 維持 "esp32-kart-02" 不要改 —— Grafana 的 device 模板變數已經寫死認這個字串，
  改了名字要另外去 scripts/_gen_esp_grafana_dashboards.py 的 KART_02 常數同步改，非必要不要動

要做的事：
1. 新開一個 ESP32/src/main_imu2.cpp（仿 main_dual_gps.cpp 的獨立檔案模式，不要塞進
   main.cpp 用第三種 #ifdef，main.cpp 現在已經有 PROFILE_GPS_HALL 一種分支，太多分支會難維護）。
2. main_imu2.cpp 內容：
   - setup() 開機先跑一次 I2C bus scan（Wire.beginTransmission 逐一 try 0x53/0x68/0x1E/0x69），
     log 印出哪些位址有回應，方便帶回板子後第一時間確認接線對不對
   - ADXL345/ITG3205/HMC5883L：用暫存器層級直接讀（風格比照 ESP32/src/Icm42688Spi.cpp 那種
     手刻 driver，不要為了三顆各自拉一個重量級 Arduino library 進來），或如果你評估某顆用
     現成 lib（例如 Adafruit ADXL345/HMC5883 系列）明顯比較省事穩定，可以用，但要在
     platformio.ini lib_deps 註明，並確認授權/星數OK
   - MPU6050（GY-521, 0x69）：同上，暫存器讀 accel+gyro+temp，或用穩定的現成 lib，一樣要記錄取捨
   - GPS：直接沿用 main.cpp 裡 gpsPoll()/gpsConfigure()/TinyGPSPlus 那套邏輯（NEO-6M-0-001 是
     legacy protocol，現有 UBX legacy CFG 封包不用改，這點跟 Prompt A 的 M10 不一樣）
   - 取樣、ring buffer、HTTP POST ingest 邏輯照抄 main.cpp 現有模式（不用重新發明，複製調整）
3. Sample 資料結構新增欄位，命名不要跟現有 ax/ay/az/gx/gy/gz（那是 ICM42688 專用）撞名，
   建議用帶感測器代號的前綴，例如 gy85_ax/gy85_ay/gy85_az/gy85_gx/gy85_gy/gy85_gz/
   gy85_mx/gy85_my/gy85_mz（ADXL345+ITG3205+HMC5883L 三顆合起來的 9 軸）、
   mpu_ax/mpu_ay/mpu_az/mpu_gx/mpu_gy/mpu_gz/mpu_temp_c（GY-521）—— 實際命名你可以調整，
   但要在 main_imu2.cpp 開頭註解寫一份完整欄位表，因為下一步要跟後端對齊。
4. platformio.ini：
   - 移除 [env:esp32-gps-hall] 這個 env，改成新的 [env:esp32-imu2-gps]（或你覺得更好的名字），
     build_src_filter 指向 main_imu2.cpp，排除 main.cpp/main_dual_gps.cpp/其他不相干檔案
   - build_flags 保留 '-DDEVICE_ID="esp32-kart-02"'
   - lib_deps 依你第 2 步的選擇調整
5. ESP32/src/main.cpp：整個 #ifdef PROFILE_GPS_HALL / #else 分支拔掉，霍爾相關的
   PIN_HALL / hallUpdate() / hallSnapshot() / HALL_* 常數 / Sample 裡的 hall_adc/hall_hz 欄位
   全部刪除，main.cpp 只保留 esp32dev（Prompt A 那顆）的邏輯，不再是多 profile 共用檔。
6. ESP32/README.md：刪掉「腳位（esp32-gps-hall）」整段，改成「腳位（esp32-imu2-gps）」，
   列出新接線表（可以直接抄這份文件第 1 節的表格）。Profiles 總表那三行也要更新。
7. services/webapp/telemetry.py：
   - TelemetrySample 裡的 hall_adc / hall_hz 兩個欄位刪掉
   - 依第 3 步定案的欄位名，在 TelemetrySample 加對應欄位，並在 _sample_to_point() 的
     fields dict 裡加進去（不要漏了、也不要跟 ax/ay/az 等既有 ICM42688 欄位混在一起判斷
     skip_imu 那段邏輯 —— 這批新欄位是獨立感測器，不要被 _is_fake_zero_imu() 誤判濾掉）
8. services/webapp/templates/telemetry.html：找到目前顯示霍爾 ADC/Hz 的那個 x-show 區塊，
   刪掉，改成顯示新欄位裡你覺得對操作者最有用的 1-2 個數字（例如姿態或磁向），格式比照
   現有其他數值的 fmt() 用法。

驗收：
- pio run -e esp32-imu2-gps 要編譯過
- 開機 log 印出 I2C scan 結果，四個位址都要出現
- grep -ri "hall" ESP32/ services/webapp/ 應該完全找不到東西了（除了這份指令文件本身）
- POST 到 /api/telemetry/ingest 的 payload 裡看得到新欄位名，且 InfluxDB 裡 kart_telemetry
  measurement 有寫入這些 field
```

### Prompt C — Grafana 排版放大 + 收尾清霍爾

```
你在 kpp 這個卡丁車遙測 monorepo 裡工作。先讀 scripts/_gen_esp_grafana_dashboards.py 全檔，
這是 infra/grafana/dashboards/kart-telemetry.json 的唯一產生器 —— 所有修改都改這支 .py，
改完用 python scripts/_gen_esp_grafana_dashboards.py 重新產生 json，不要手改 json。
產生器最後會跑 _validate_dashboard()，改完要確保它還能過。

前提：ESP32/src/main_imu2.cpp（取代原本霍爾板子的新韌體）已經在另一個改動裡把 hall_adc/hall_hz
換成新欄位名（去讀 ESP32/src/main_imu2.cpp 開頭註解的欄位表，或 services/webapp/telemetry.py
裡 TelemetrySample 目前有哪些欄位，用那個做準）。

要做的事：

1. 刪霍爾（generator 裡目前的位置，行號僅供參考，實際以檔案內容為準）：
   - THR_HALL 這個 threshold 常數，如果刪完沒人用了就整個拿掉
   - panel_stat(10, "霍爾 ADC", "hall_adc", ...)
   - panel_stat(11, "霍爾 Hz", "hall_hz", ...)
   - panel_ts(13, "霍爾 ADC", ["hall_adc"], ...)
   - panel_ts(14, "霍爾 Hz", ["hall_hz"], ...)
   把這 4 個 panel 從 make_kart_telemetry() 的 panels list 裡拿掉。

2. 排版放大，現況問題：
   - "Overview" row（y=0~4）目前是 8 個 stat panel，每個 w=3 h=4，剛好填滿 24 欄，數字看起來很小
   - 其他 timeseries panel（Accel XYZ、Gyro XYZ、|a|/accel_dyn、Yaw rate、G lateral/longitudinal）
     目前多半是 w=12 h=8

   目標：整體看起來更大更好讀，不是隨便加大到超出 24 欄格線。具體做法：
   - Overview 8 個 stat panel 改成兩排、每排 4 個，w=6 h=6（兩排共高度 12，比現在的
     單排 h=4 明顯大很多，數字放大模式 colorMode=background 效果會更好看）
   - panel_stat()/panel_ts() 這兩個 helper 的預設值目前是 w=3,h=4 跟 w=12,h=8，
     順手把預設值也調大一點（例如 stat 預設 h=5、ts 預設 h=9），這樣以後新增 panel
     不會又縮回去
   - 因為刪了 hall 的 2 個 stat + 2 個 timeseries，"Telemetry" row 空出來的版面，
     把原本 w=16 的 Speed timeseries 加大到 w=24（整排）或視覺上更合理的寬度，
     再把 Prompt B 新韌體送上來的欄位（GY-85/GY-521 相關）加 1-2 個新 timeseries panel
     填進這排空間，不要留大片空白
   - 其他 timeseries panel（Accel XYZ / Gyro XYZ / |a| 、accel_dyn / Yaw rate /
     G lateral-longitudinal）h 從 8 拉到 10~12，w 維持 12（兩個一排），高度加大即可，
     不用改寬度排版邏輯
   - 每改一個 panel 的 gridPos，順手檢查同一個 y 高度的其他 panel x+w 有沒有超過 24
     或互相重疊，改完後面所有 panel 的 y 值要跟著往下順移，不要疊在一起
   - panel_row() 的 y 值（Overview/Telemetry/Motion/Track/Fleet 這幾個分段標題列）
     要跟著上面的高度變化重新算過，維持由上到下不重疊

3. 收尾：全專案（排除 .git/ .pio/ .venv/ node_modules/）grep -ri "hall" 一次，
   列出所有還剩的檔案。ESP32/ 跟 services/webapp/ 裡不應該再有任何 hall 相關字樣
   （這兩塊如果還有殘留，可能是另一個改動漏改，要指出來但不用你自己去改韌體檔案，
   只要在這次 commit 的說明裡列清楚）。

驗收：
- python scripts/_gen_esp_grafana_dashboards.py 執行成功，_validate_dashboard 沒噴錯
- 產生的 infra/grafana/dashboards/kart-telemetry.json 裡搜不到 "hall"
- git diff 看一下 gridPos 改動，確認沒有任何兩個 panel 在同一個 y 範圍內 x+w 超過 24
- 貼一張改動前後的 panel 數量/尺寸對照（幾個 panel 從多大改成多大）在 PR 說明裡
```

---

## 3. 執行順序建議

1. 先照第 1 節接線表把兩塊板子接好（板子 #1 先接，因為它改動小，風險低）。
2. Prompt A 貼給 Cursor，燒錄板子 #1，確認 `WHO_AM_I=0x47` + GPS 有 sats 在跳。
3. Prompt B 貼給 Cursor，燒錄板子 #2，先看開機 log 的 I2C scan 結果對不對（0x53/0x68/0x1E/0x69 都要在），再確認資料真的有寫進 InfluxDB。
4. 確認 Prompt B 定案的欄位名之後，再貼 Prompt C，讓 Grafana 看板跟上新欄位、順便把排版跟殘留霍爾字樣一次清乾淨。
