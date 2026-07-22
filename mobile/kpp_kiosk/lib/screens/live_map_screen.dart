import 'package:flutter/material.dart';

import '../api/kiosk_ws.dart';
import '../config/app_settings.dart';
import '../config/track_coords.dart';

class _Kart {
  _Kart({
    required this.deviceId,
    required this.px,
    required this.py,
    this.label,
  });

  final String deviceId;
  double px;
  double py;
  String? label;
  bool stale = false;
  final List<Offset> trail = [];
}

class LiveMapScreen extends StatefulWidget {
  const LiveMapScreen({super.key, required this.settings});

  final AppSettings settings;

  @override
  State<LiveMapScreen> createState() => _LiveMapScreenState();
}

class _LiveMapScreenState extends State<LiveMapScreen> {
  late final KioskWsClient _ws;
  final Map<String, _Kart> _karts = {};
  bool _connected = false;

  static const _colors = [
    Color(0xFFE10600),
    Color(0xFF00D2BE),
    Color(0xFF1E5BC6),
    Color(0xFFF596C8),
    Color(0xFFFFff00),
    Color(0xFFFF8700),
    Color(0xFFFFFFFF),
    Color(0xFF52E252),
  ];

  @override
  void initState() {
    super.initState();
    _ws = KioskWsClient(
      settings: widget.settings,
      path: '/ws/positions',
      onStatus: (ok) {
        if (mounted) setState(() => _connected = ok);
      },
      onMessage: _onMsg,
    );
    _ws.start();
  }

  @override
  void dispose() {
    _ws.dispose();
    super.dispose();
  }

  void _upsert(Map<String, dynamic> msg) {
    final lat = (msg['lat'] as num?)?.toDouble();
    final lon = (msg['lon'] as num?)?.toDouble();
    final id = msg['device_id'] as String?;
    if (lat == null || lon == null || id == null) return;
    final (px, py) = TrackCoords.latLngToPx(lat, lon);
    final label = (msg['display_name'] as String?) ??
        (msg['car_id'] != null ? '#${msg['car_id']}' : id);
    final kart = _karts.putIfAbsent(
      id,
      () => _Kart(deviceId: id, px: px, py: py, label: label),
    );
    kart.px = px;
    kart.py = py;
    kart.label = label;
    kart.stale = false;
    kart.trail.add(Offset(px, py));
    if (kart.trail.length > 40) {
      kart.trail.removeRange(0, kart.trail.length - 40);
    }
  }

  String _initial(String? label) {
    final s = (label ?? '?').trim();
    if (s.isEmpty) return '?';
    return s.substring(0, 1).toUpperCase();
  }

  void _onMsg(Map<String, dynamic> msg) {
    final type = msg['type'];
    if (type == 'snapshot' && msg['positions'] is List) {
      for (final p in msg['positions'] as List) {
        if (p is Map) _upsert(Map<String, dynamic>.from(p));
      }
    } else if (type == 'position') {
      _upsert(msg);
    }
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
          child: Row(
            children: [
              const Text('賽道地圖', style: TextStyle(fontWeight: FontWeight.w600)),
              const Spacer(),
              Icon(
                Icons.circle,
                size: 10,
                color: _connected ? Colors.greenAccent : Colors.red,
              ),
              const SizedBox(width: 6),
              Text(_connected ? '位置串流中' : '重連中…', style: const TextStyle(fontSize: 13)),
            ],
          ),
        ),
        Expanded(
          child: LayoutBuilder(
            builder: (context, box) {
              final scale = (box.maxWidth / TrackCoords.imgW)
                  .clamp(0.0, box.maxHeight / TrackCoords.imgH);
              final drawW = TrackCoords.imgW * scale;
              final drawH = TrackCoords.imgH * scale;
              final left = (box.maxWidth - drawW) / 2;
              final top = (box.maxHeight - drawH) / 2;

              return Stack(
                children: [
                  Positioned(
                    left: left,
                    top: top,
                    width: drawW,
                    height: drawH,
                    child: Image.asset(
                      'assets/tracks/tks_qiaotou_track.png',
                      fit: BoxFit.fill,
                    ),
                  ),
                  ..._karts.values.toList().asMap().entries.map((e) {
                    final i = e.key;
                    final k = e.value;
                    final color = _colors[i % _colors.length]
                        .withOpacity(k.stale ? 0.35 : 1);
                    final x = left + k.px * scale;
                    final y = top + k.py * scale;
                    return Positioned(
                      left: x - 14,
                      top: y - 14,
                      child: Container(
                        width: 28,
                        height: 28,
                        alignment: Alignment.center,
                        decoration: BoxDecoration(
                          color: color,
                          shape: BoxShape.circle,
                          border: Border.all(color: Colors.black87, width: 2),
                        ),
                        child: Text(
                          _initial(k.label),
                          style: const TextStyle(
                            color: Colors.black,
                            fontWeight: FontWeight.bold,
                            fontSize: 12,
                          ),
                        ),
                      ),
                    );
                  }),
                ],
              );
            },
          ),
        ),
      ],
    );
  }
}
