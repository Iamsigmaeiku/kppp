"""Interactive apex/curb marker for the TKS satellite track image."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.image as mpimg
import matplotlib.pyplot as plt

from coord_transform import local_m_to_latlng, px_to_local_m

_ROOT = Path(__file__).resolve().parent
IMG_PATH = _ROOT / "data" / "tks_qiaotou_track.png"
OUT_PATH = _ROOT / "output" / "track_features_qiaotou.json"


def main() -> None:
    if not IMG_PATH.is_file():
        raise SystemExit(f"missing track image: {IMG_PATH}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    img = mpimg.imread(IMG_PATH)
    fig, ax = plt.subplots(figsize=(14, 14))
    ax.imshow(img)
    ax.set_title("依序點擊每個彎道: apex -> curb_start -> curb_end，全部完成按Enter")
    plt.tight_layout()

    points = plt.ginput(n=-1, timeout=0)
    plt.close()

    labels = ["apex", "curb_start", "curb_end"]
    features = []
    for i, (px, py) in enumerate(points):
        corner_id = i // 3 + 1
        ftype = labels[i % 3]
        x_m, y_m = px_to_local_m(px, py)
        lat, lng = local_m_to_latlng(x_m, y_m)
        features.append(
            {
                "corner_id": corner_id,
                "type": ftype,
                "px": round(px, 1),
                "py": round(py, 1),
                "local_x_m": round(x_m, 2),
                "local_y_m": round(y_m, 2),
                "lat": round(lat, 7),
                "lng": round(lng, 7),
            }
        )

    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(features, f, ensure_ascii=False, indent=2)

    print(f"共 {len(points)} 個點，已存到 {OUT_PATH}")
    if len(points) % 3 != 0:
        print(f"警告: 點數不是 3 的倍數（缺完整 apex/curb_start/curb_end）")


if __name__ == "__main__":
    main()
