import 'package:flutter/material.dart';

import '../api/kpp_api.dart';
import '../config/app_settings.dart';

class LeaderboardScreen extends StatefulWidget {
  const LeaderboardScreen({
    super.key,
    required this.settings,
    required this.onOpenSession,
  });

  final AppSettings settings;
  final ValueChanged<String> onOpenSession;

  @override
  State<LeaderboardScreen> createState() => _LeaderboardScreenState();
}

class _LeaderboardScreenState extends State<LeaderboardScreen> {
  late final KppApi _api = KppApi(widget.settings);
  Map<String, dynamic>? _data;
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data = await _api.leaderboard();
      if (!mounted) return;
      setState(() {
        _data = data;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = '$e';
        _loading = false;
      });
    }
  }

  Widget _table(String title, List entries) {
    return Expanded(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.all(8),
            child: Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
          ),
          Expanded(
            child: ListView.builder(
              itemCount: entries.length,
              itemBuilder: (context, i) {
                final e = Map<String, dynamic>.from(entries[i] as Map);
                final name = e['driver_name'] ?? e['car_number'] ?? e['name'];
                final sid = e['session_id'] as String?;
                return ListTile(
                  dense: true,
                  leading: CircleAvatar(
                    radius: 14,
                    child: Text('${i + 1}', style: const TextStyle(fontSize: 12)),
                  ),
                  title: Text('#${e['car_number']}  $name'),
                  subtitle: Text('${e['time_label'] ?? '—'}'),
                  trailing: sid == null
                      ? null
                      : IconButton(
                          icon: const Icon(Icons.open_in_new, size: 18),
                          onPressed: () => widget.onOpenSession(sid),
                        ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_error!, textAlign: TextAlign.center),
            const SizedBox(height: 12),
            FilledButton(onPressed: _reload, child: const Text('重試')),
          ],
        ),
      );
    }
    final alltime = (_data?['alltime_entries'] as List?) ?? const [];
    final session = (_data?['session_entries'] as List?) ?? const [];
    final unavailable = _data?['influx_unavailable'] == true;

    return Column(
      children: [
        if (unavailable)
          const MaterialBanner(
            content: Text('歷史資料庫目前無法連線'),
            actions: [SizedBox.shrink()],
          ),
        Align(
          alignment: Alignment.centerRight,
          child: IconButton(onPressed: _reload, icon: const Icon(Icons.refresh)),
        ),
        Expanded(
          child: Row(
            children: [
              _table('全站最佳', alltime),
              const VerticalDivider(width: 1),
              _table('最近有成績的節次', session),
            ],
          ),
        ),
      ],
    );
  }
}
