# SmartKart GRU 圈速預測

離線 / Jetson 可重跑管線：InfluxDB → geofence 切圈 → partial-lap prefix dataset → 小型 GRU → checkpoint → CUDA 推論。

**目標是驗證 pipeline + 抓 overfit，不是產出能上場的模型。**

## 真實 Schema（2026-07-19 discovery）

| measurement | 用途 | 重點 fields / tags |
|---|---|---|
| `kart_telemetry` | 主特徵 | `ax,ay,az,gx,gy,gz,a_lat,a_lon,hall_hz,gps_*`；tag `device_id` |
| `dr_position` | ESP32 ESKF 融合位置（短） | `lat_dr,lon_dr,heading_deg,speed_mps` |
| `position_est` | Pi dead-reckoning | `lat_est,lon_est,heading_deg,speed_mps` |
| `decoder_raw_events` | MyLaps 過線 | `last_lap_time`；tag `session_id` |
| `session_archive` | 場次摘要 | `lap_history_json,best_lap_time` |
| `attitude` | roll/pitch | 非主訓練特徵 |

取樣：IMU/`kart_telemetry` median ≈ **25 Hz**；`dr_position` ≈ **50 Hz**。  
**沒有四輪輪速**——只有 `hall_hz`。  
連線：`.env` 的 `INFLUX_*`；Windows 經 Tailscale 會 rewrite 到 `http://100.102.122.104:8086`。

完整輸出：`outputs/schema_snapshot.json`。

## 起跑線

```yaml
# model/config.yaml
start_line:
  confirmed: true
  source: auto_gps_density_hotspot
  lat: 22.741676
  lon: 120.3220
  radius_m: 15.0
```

來源：GPS `gps_fix` 密度熱點（decoder 過線時間與 ESP32 GPS 無可靠對齊）。改座標後把 `confirmed` 設回 `true` 再跑。

## 快速開始

```bash
cd smartkart-lstm
pip install -r requirements.txt
# x86 本機另裝 PyTorch；Jetson 見下方

python data/discover_schema.py
python data/build_dataset.py
python train.py
python infer_jetson.py --device cpu   # 或 cuda（Jetson）
python incremental_retrain.py --checkpoint checkpoints/latest.pt
```

## Jetson Orin（已確認：Orin Nano Super, L4T R36.4.4 / CUDA 12.6）

**不要** `pip install torch` 裝到 x86 wheel。JP6.2 官方 wheel 缺口，用 Jetson AI Lab：

```bash
python3 -m venv ~/smartkart-lstm/.venv && source ~/smartkart-lstm/.venv/bin/activate
pip install -U pip "numpy<2" PyYAML
pip install torch==2.8.0 --index-url https://pypi.jetson-ai-lab.io/jp6/cu126
# 缺 runtime 時：
sudo apt-get install -y cuda-cudart-12-6 libcublas-12-6 cudnn9-cuda-12-6 \
  libcufile-12-6 cuda-cupti-12-6 cuda-nvtx-12-6 libnpp-12-6 libnvjitlink-12-6
export LD_LIBRARY_PATH=/usr/local/cuda-12.6/targets/aarch64-linux/lib:$LD_LIBRARY_PATH
python infer_jetson.py --device cuda --n 200
```

本機同步+benchmark：`python scripts/sync_and_bench_jetson.py`

現階段不做 TensorRT（GRU RNN op 常有雷）。`infer_jetson.py` 留了 ONNX stub TODO。

## 樣本語意

每個 sample = **一圈起點 → 時間 t 的完整 prefix**，重採樣成固定 `(50, F)`，label = 該圈最終圈速（秒）。  
Train/val **按 session 切**，禁止 random split。

## 檔案

```
smartkart-lstm/
├── data/discover_schema.py
├── data/influx_query.py
├── data/segment_laps.py
├── data/build_dataset.py
├── model/gru_laptime.py
├── model/config.yaml
├── train.py
├── incremental_retrain.py
├── infer_jetson.py
├── checkpoints/
└── outputs/
```
