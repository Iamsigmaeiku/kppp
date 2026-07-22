import 'dart:async';

import 'package:flutter/material.dart';

import '../api/kiosk_ws.dart';
import '../config/app_settings.dart';

class LiveTimingScreen extends StatefulWidget {
  const LiveTimingScreen({super.key, required this.settings});

  final AppSettings settings;

  @override
  State<LiveTimingScreen> createState() => _LiveTimingScreenState();
}

class _LiveTimingScreenState extends State<LiveTimingScreen> {
  late final KioskWsClient _ws;
  final Map<String, Map<String, dynamic>> _cars = {};
  bool _connected = false;
  bool _decoderConnected = false;
  String? _sessionId;
  int? _sessionNumber;
  String? _sessionDate;
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    _ws = KioskWsClient(
      settings: widget.settings,
      path: '/ws/laps',
      onStatus: (ok) {
        if (mounted) setState(() => _connected = ok);
      },
      onMessage: _onMsg,
    );
    _ws.start();
    _tick = Timer.periodic(const Duration(milliseconds: 50), (_) {
      if (!mounted) return;
      // 本圈計時由伺服器推 elapsed；本地只觸發重繪讓 UI 感覺活著
      setState(() {});
    });
  }

  @override
  void dispose() {
    _tick?.cancel();
    _ws.dispose();
    super.dispose();
  }

  void _onMsg(Map<String, dynamic> msg) {
    final type = msg['type'];
    if (type == 'lap') {
      final tid = msg['transponder_id'] as String?;
      if (tid == null) return;
      _cars[tid] = msg;
      if (msg['decoder_connected'] is bool) {
        _decoderConnected = msg['decoder_connected'] as bool;
      }
    } else if (type == 'decoder_status' || msg.containsKey('connected')) {
      if (msg['connected'] is bool) {
        _decoderConnected = msg['connected'] as bool;
      }
    } else if (type == 'session_info') {
      _sessionId = msg['session_id'] as String?;
      _sessionNumber = msg['session_number'] as int?;
      _sessionDate = msg['session_date'] as String?;
    } else if (type == 'session_reset') {
      _cars.clear();
      _sessionNumber = null;
      _sessionDate = null;
    }
    if (mounted) setState(() {});
  }

  String _fmt(dynamic sec) {
    if (sec == null) return '—';
    final v = (sec is num) ? sec.toDouble() : double.tryParse('$sec');
    if (v == null || v.isNaN) return '—';
    return '${v.toStringAsFixed(3)}s';
  }

  String get _sessionLabel {
    if (_sessionNumber != null) {
      final d = _sessionDate != null ? '（$_sessionDate）' : '';
      return '第 $_sessionNumber 節$d';
    }
    return _sessionId ?? '本節載入中…';
  }

  List<Map<String, dynamic>> get _sorted {
    final list = _cars.values.toList();
    list.sort((a, b) {
      final ra = (a['rank'] as num?)?.toInt() ?? 9999;
      final rb = (b['rank'] as num?)?.toInt() ?? 9999;
      return ra.compareTo(rb);
    });
    return list;
  }

  @override
  Widget build(BuildContext context) {
    final statusColor = !_connected
        ? Colors.red
        : (_decoderConnected ? Colors.greenAccent : Colors.amber);

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
          child: Row(
            children: [
              Text(_sessionLabel, style: const TextStyle(fontWeight: FontWeight.w600)),
              const Spacer(),
              Icon(Icons.circle, size: 10, color: statusColor),
              const SizedBox(width: 6),
              Text(
                !_connected
                    ? '重連中…'
                    : (_decoderConnected ? '已連線' : '已連線 · 計時暫停'),
                style: TextStyle(color: statusColor, fontSize: 13),
              ),
            ],
          ),
        ),
        Expanded(
          child: SingleChildScrollView(
            child: DataTable(
              headingRowHeight: 36,
              dataRowMinHeight: 40,
              dataRowMaxHeight: 48,
              columns: const [
                DataColumn(label: Text('排名')),
                DataColumn(label: Text('車號')),
                DataColumn(label: Text('圈數')),
                DataColumn(label: Text('上圈')),
                DataColumn(label: Text('最佳')),
                DataColumn(label: Text('本圈')),
              ],
              rows: [
                for (final s in _sorted)
                  DataRow(
                    cells: [
                      DataCell(Text('${s['rank'] ?? '—'}')),
                      DataCell(Text('${s['car_number'] ?? s['transponder_id']}')),
                      DataCell(Text('${s['lap_count'] ?? 0}')),
                      DataCell(Text(_fmt(s['last_lap_time']))),
                      DataCell(Text(_fmt(s['best_lap_time']))),
                      DataCell(
                        Text(
                          _fmt(s['current_lap_elapsed']),
                          style: TextStyle(
                            color: (s['timer_active'] == false)
                                ? Colors.white54
                                : Colors.white,
                            fontFeatures: const [FontFeature.tabularFigures()],
                          ),
                        ),
                      ),
                    ],
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}
