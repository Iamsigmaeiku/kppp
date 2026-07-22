import 'package:flutter/material.dart';

import '../api/kpp_api.dart';
import '../config/app_settings.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({
    super.key,
    required this.settings,
    required this.onSaved,
    this.requireComplete = false,
  });

  final AppSettings settings;
  final Future<void> Function() onSaved;
  final bool requireComplete;

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final _url = TextEditingController(text: widget.settings.baseUrl);
  late final _token = TextEditingController(text: widget.settings.kioskToken);
  late final _pin = TextEditingController(text: widget.settings.unlockPin);
  late bool _lockTask = widget.settings.lockTaskEnabled;
  String? _status;
  bool _busy = false;

  @override
  void dispose() {
    _url.dispose();
    _token.dispose();
    _pin.dispose();
    super.dispose();
  }

  Future<void> _ping() async {
    setState(() {
      _busy = true;
      _status = '測試連線…';
    });
    try {
      final draft = widget.settings.copyWith(
        baseUrl: _url.text.trim(),
        kioskToken: _token.text.trim(),
      );
      final ok = await KppApi(draft).pingHealth();
      setState(() => _status = ok ? 'Health OK' : 'Health 非 200');
    } catch (e) {
      setState(() => _status = '失敗: $e');
    } finally {
      setState(() => _busy = false);
    }
  }

  Future<void> _save() async {
    final url = _url.text.trim();
    final token = _token.text.trim();
    if (url.isEmpty || token.isEmpty) {
      setState(() => _status = 'baseUrl 與 KIOSK_TOKEN 必填');
      return;
    }
    final next = widget.settings.copyWith(
      baseUrl: url,
      kioskToken: token,
      unlockPin:
          _pin.text.trim().isEmpty ? AppSettings.defaultPin : _pin.text.trim(),
      lockTaskEnabled: _lockTask,
    );
    await next.save();
    await widget.onSaved();
    if (!mounted) return;
    if (!widget.requireComplete) {
      Navigator.of(context).pop();
    } else {
      setState(() => _status = '已儲存');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Kiosk 設定'),
        automaticallyImplyLeading: !widget.requireComplete,
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          TextField(
            controller: _url,
            decoration: const InputDecoration(
              labelText: 'Server base URL',
              hintText: 'http://100.x.x.x:8000',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _token,
            decoration: const InputDecoration(
              labelText: 'KIOSK_TOKEN（X-Kiosk-Token）',
              border: OutlineInputBorder(),
            ),
            obscureText: true,
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _pin,
            decoration: const InputDecoration(
              labelText: '解鎖 PIN',
              border: OutlineInputBorder(),
            ),
            obscureText: true,
            keyboardType: TextInputType.number,
          ),
          SwitchListTile(
            title: const Text('啟用 Lock Task（螢幕固定）'),
            value: _lockTask,
            onChanged: (v) => setState(() => _lockTask = v),
          ),
          if (_status != null) ...[
            const SizedBox(height: 8),
            Text(_status!),
          ],
          const SizedBox(height: 16),
          Row(
            children: [
              OutlinedButton(
                onPressed: _busy ? null : _ping,
                child: const Text('測試 /health'),
              ),
              const SizedBox(width: 12),
              FilledButton(
                onPressed: _busy ? null : _save,
                child: const Text('儲存'),
              ),
            ],
          ),
          const SizedBox(height: 24),
          const Text(
            '提示：後端 .env 需設相同 KIOSK_TOKEN。完整 kiosk 見 README（device owner / 螢幕固定）。',
            style: TextStyle(color: Colors.white54, fontSize: 12),
          ),
        ],
      ),
    );
  }
}
