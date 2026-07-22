import 'package:flutter_test/flutter_test.dart';

import 'package:kpp_kiosk/config/track_coords.dart';

void main() {
  test('track center projects near image center', () {
    final (px, py) = TrackCoords.latLngToPx(
      TrackCoords.centerLat,
      TrackCoords.centerLng,
    );
    expect(px, closeTo(TrackCoords.imgW / 2, 0.5));
    expect(py, closeTo(TrackCoords.imgH / 2, 0.5));
  });
}
