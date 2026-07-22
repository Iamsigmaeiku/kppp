import 'package:shared_preferences/shared_preferences.dart';

class AppSettings {
  AppSettings({
    required this.baseUrl,
    required this.kioskToken,
    required this.unlockPin,
    required this.lockTaskEnabled,
  });

  final String baseUrl;
  final String kioskToken;
  final String unlockPin;
  final bool lockTaskEnabled;

  static const _kBaseUrl = 'base_url';
  static const _kToken = 'kiosk_token';
  static const _kPin = 'unlock_pin';
  static const _kLockTask = 'lock_task';

  /// 預設指到場邊常見 Pi 位址；現場可在設定改。
  static const defaultBaseUrl = 'http://100.102.122.104:8000';
  static const defaultPin = '2468';

  static Future<AppSettings> load() async {
    final p = await SharedPreferences.getInstance();
    return AppSettings(
      baseUrl: (p.getString(_kBaseUrl) ?? defaultBaseUrl).trim(),
      kioskToken: (p.getString(_kToken) ?? '').trim(),
      unlockPin: (p.getString(_kPin) ?? defaultPin).trim(),
      lockTaskEnabled: p.getBool(_kLockTask) ?? true,
    );
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString(_kBaseUrl, baseUrl.trim());
    await p.setString(_kToken, kioskToken.trim());
    await p.setString(_kPin, unlockPin.trim().isEmpty ? defaultPin : unlockPin.trim());
    await p.setBool(_kLockTask, lockTaskEnabled);
  }

  AppSettings copyWith({
    String? baseUrl,
    String? kioskToken,
    String? unlockPin,
    bool? lockTaskEnabled,
  }) {
    return AppSettings(
      baseUrl: baseUrl ?? this.baseUrl,
      kioskToken: kioskToken ?? this.kioskToken,
      unlockPin: unlockPin ?? this.unlockPin,
      lockTaskEnabled: lockTaskEnabled ?? this.lockTaskEnabled,
    );
  }

  Uri httpUri(String path) {
    final root = baseUrl.replaceAll(RegExp(r'/+$'), '');
    final p = path.startsWith('/') ? path : '/$path';
    return Uri.parse('$root$p');
  }

  Uri wsUri(String path) {
    final http = httpUri(path);
    final scheme = http.scheme == 'https' ? 'wss' : 'ws';
    return http.replace(scheme: scheme);
  }
}
