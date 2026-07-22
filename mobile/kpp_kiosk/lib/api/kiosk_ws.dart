import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../config/app_settings.dart';

typedef JsonHandler = void Function(Map<String, dynamic> msg);

/// 自動重連 WebSocket，帶 X-Kiosk-Token。
class KioskWsClient {
  KioskWsClient({
    required this.settings,
    required this.path,
    required this.onMessage,
    this.onStatus,
  });

  final AppSettings settings;
  final String path;
  final JsonHandler onMessage;
  final void Function(bool connected)? onStatus;

  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  Timer? _ping;
  Timer? _reconnect;
  int _backoffSec = 1;
  bool _disposed = false;
  bool _wanted = false;

  void start() {
    _wanted = true;
    _connect();
  }

  void stop() {
    _wanted = false;
    _reconnect?.cancel();
    _ping?.cancel();
    _sub?.cancel();
    _channel?.sink.close();
    _channel = null;
    onStatus?.call(false);
  }

  void dispose() {
    _disposed = true;
    stop();
  }

  Future<void> _connect() async {
    if (_disposed || !_wanted) return;
    _ping?.cancel();
    _sub?.cancel();
    try {
      await _channel?.sink.close();
    } catch (_) {}

    try {
      final uri = settings.wsUri(path);
      final channel = IOWebSocketChannel.connect(
        uri,
        headers: {'X-Kiosk-Token': settings.kioskToken},
      );
      _channel = channel;
      _sub = channel.stream.listen(
        (raw) {
          try {
            final msg = jsonDecode(raw as String) as Map<String, dynamic>;
            onMessage(msg);
          } catch (_) {}
        },
        onDone: _scheduleReconnect,
        onError: (_) => _scheduleReconnect(),
      );
      _backoffSec = 1;
      onStatus?.call(true);
      _ping = Timer.periodic(const Duration(seconds: 25), (_) {
        try {
          _channel?.sink.add('ping');
        } catch (_) {}
      });
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    onStatus?.call(false);
    if (_disposed || !_wanted) return;
    _reconnect?.cancel();
    final wait = _backoffSec;
    _backoffSec = (_backoffSec * 2).clamp(1, 10);
    _reconnect = Timer(Duration(seconds: wait), _connect);
  }
}
