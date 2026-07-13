# Track feature marking (TKS)

衛星圖標記 apex / curb，輸出 JSON 給未來 GPS pipeline。

座標常數對齊根目錄 `gettrack.py`（`22.7423…, 120.3217…`），PNG = 1280×1280，MPP≈0.1377。

## 用法

```bash
cd tools/track_mapping
pip install -r requirements.txt
python mark_features.py
```

依序點每個彎：`apex → curb_start → curb_end`，全部完成按 Enter。

輸出：`output/track_features_qiaotou.json`
