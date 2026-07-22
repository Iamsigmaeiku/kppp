import 'dart:math' as math;

/// TKS 橋頭賽道座標（對齊 services/webapp/track_coords.py）。
class TrackCoords {
  static const centerLat = 22.742304850060208;
  static const centerLng = 120.32173316061305;
  static const mpp = 0.1377;
  static const imgW = 1280.0;
  static const imgH = 1280.0;

  static (double px, double py) latLngToPx(double lat, double lng) {
    final yM = (lat - centerLat) * 111320.0;
    final xM =
        (lng - centerLng) * (111320.0 * math.cos(centerLat * math.pi / 180.0));
    final px = imgW / 2 + xM / mpp;
    final py = imgH / 2 - yM / mpp;
    return (px, py);
  }
}
