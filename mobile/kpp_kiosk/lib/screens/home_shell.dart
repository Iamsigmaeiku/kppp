import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../config/app_settings.dart';
import 'leaderboard_screen.dart';
import 'live_map_screen.dart';
import 'live_timing_screen.dart';
import 'settings_screen.dart';
import 'webview_host.dart';

class HomeShell extends StatefulWidget {
  const HomeShell({
    super.key,
    required this.settings,
    required this.onSettingsChanged,
  });

  final AppSettings settings;
  final Future<void> Function() onSettingsChanged;

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  static const _channel = MethodChannel('com.kpp.kiosk/lock_task');
  int _tab = 0;

  late final _timing = LiveTimingScreen(settings: widget.settings);
  late final _map = LiveMapScreen(settings: widget.settings);
  late final _board = LeaderboardScreen(
    settings: widget.settings,
    onOpenSession: (id) => _openWeb('/sessions/$id', '場次明細'),
  );

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _maybeLockTask(true));
  }

  Future<void> _maybeLockTask(bool enable) async {
    if (!widget.settings.lockTaskEnabled) return;
    try {
      await _channel.invokeMethod(enable ? 'startLockTask' : 'stopLockTask');
    } catch (_) {}
  }

  Future<bool> _confirmPin() async {
    final ctrl = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => AlertDialog(
        title: const Text('輸入解鎖 PIN'),
        content: TextField(
          controller: ctrl,
          obscureText: true,
          keyboardType: TextInputType.number,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'PIN'),
          onSubmitted: (_) => Navigator.pop(ctx, true),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('確認'),
          ),
        ],
      ),
    );
    if (ok != true) return false;
    return ctrl.text.trim() == widget.settings.unlockPin;
  }

  Future<void> _openSettings() async {
    if (!await _confirmPin()) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('PIN 錯誤')),
        );
      }
      return;
    }
    await _maybeLockTask(false);
    if (!mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => SettingsScreen(
          settings: widget.settings,
          onSaved: widget.onSettingsChanged,
        ),
      ),
    );
    await widget.onSettingsChanged();
    await _maybeLockTask(true);
  }

  Future<void> _openWeb(String path, String title) async {
    if (!mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => WebViewHost(
          settings: widget.settings,
          initialPath: path,
          title: title,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      child: Scaffold(
        body: SafeArea(
          child: Column(
            children: [
              _TopBar(
                tab: _tab,
                onTab: (i) => setState(() => _tab = i),
                onSettings: _openSettings,
                onProfile: () => _openWeb('/profile', '我的資料'),
                onSessions: () => _openWeb('/sessions', '場次紀錄'),
                onLogin: () => _openWeb('/login', '登入'),
              ),
              Expanded(
                child: LayoutBuilder(
                  builder: (context, box) {
                    final wide = box.maxWidth >= 900;
                    if (_tab == 0 && wide) {
                      return Row(
                        children: [
                          Expanded(flex: 5, child: _timing),
                          const VerticalDivider(width: 1),
                          Expanded(flex: 4, child: _map),
                        ],
                      );
                    }
                    switch (_tab) {
                      case 1:
                        return _map;
                      case 2:
                        return _board;
                      default:
                        return _timing;
                    }
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _TopBar extends StatelessWidget {
  const _TopBar({
    required this.tab,
    required this.onTab,
    required this.onSettings,
    required this.onProfile,
    required this.onSessions,
    required this.onLogin,
  });

  final int tab;
  final ValueChanged<int> onTab;
  final VoidCallback onSettings;
  final VoidCallback onProfile;
  final VoidCallback onSessions;
  final VoidCallback onLogin;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: const Color(0xFF111318),
      child: SizedBox(
        height: 48,
        child: Row(
          children: [
            const SizedBox(width: 12),
            GestureDetector(
              onLongPress: onSettings,
              child: const Text(
                'KPP',
                style: TextStyle(
                  fontWeight: FontWeight.w800,
                  letterSpacing: 1.2,
                  color: Color(0xFFE10600),
                ),
              ),
            ),
            const SizedBox(width: 16),
            _Chip(label: '即時', selected: tab == 0, onTap: () => onTab(0)),
            _Chip(label: '地圖', selected: tab == 1, onTap: () => onTab(1)),
            _Chip(label: '排行', selected: tab == 2, onTap: () => onTab(2)),
            const Spacer(),
            TextButton(onPressed: onSessions, child: const Text('場次')),
            TextButton(onPressed: onProfile, child: const Text('我的')),
            TextButton(onPressed: onLogin, child: const Text('登入')),
            IconButton(
              tooltip: '設定（需 PIN）',
              onPressed: onSettings,
              icon: const Icon(Icons.settings),
            ),
          ],
        ),
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: ChoiceChip(
        label: Text(label),
        selected: selected,
        onSelected: (_) => onTap(),
        visualDensity: VisualDensity.compact,
      ),
    );
  }
}
