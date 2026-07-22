import 'package:dio/dio.dart';

import '../config/app_settings.dart';

class KppApi {
  KppApi(this.settings) : dio = Dio() {
    dio.options.connectTimeout = const Duration(seconds: 8);
    dio.options.receiveTimeout = const Duration(seconds: 15);
    dio.options.headers['X-Kiosk-Token'] = settings.kioskToken;
  }

  final AppSettings settings;
  final Dio dio;

  Future<bool> pingHealth() async {
    final r = await dio.getUri(settings.httpUri('/health'));
    return r.statusCode == 200;
  }

  Future<Map<String, dynamic>> leaderboard() async {
    final r = await dio.getUri(settings.httpUri('/api/leaderboard'));
    return Map<String, dynamic>.from(r.data as Map);
  }

  Future<Map<String, dynamic>> sessions() async {
    final r = await dio.getUri(settings.httpUri('/api/sessions'));
    return Map<String, dynamic>.from(r.data as Map);
  }
}
