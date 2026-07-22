import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'config/app_settings.dart';
import 'screens/home_shell.dart';
import 'screens/settings_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await SystemChrome.setPreferredOrientations(const [
    DeviceOrientation.landscapeLeft,
    DeviceOrientation.landscapeRight,
  ]);
  SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky);
  // 亮屏：MainActivity FLAG_KEEP_SCREEN_ON

  final settings = await AppSettings.load();
  runApp(KppKioskApp(settings: settings));
}

class KppKioskApp extends StatefulWidget {
  const KppKioskApp({super.key, required this.settings});

  final AppSettings settings;

  @override
  State<KppKioskApp> createState() => _KppKioskAppState();
}

class _KppKioskAppState extends State<KppKioskApp> {
  late AppSettings _settings = widget.settings;

  Future<void> _reload() async {
    final s = await AppSettings.load();
    setState(() => _settings = s);
  }

  @override
  Widget build(BuildContext context) {
    final configured = _settings.baseUrl.trim().isNotEmpty &&
        _settings.kioskToken.trim().isNotEmpty;

    return MaterialApp(
      title: 'KPP Kiosk',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFFE10600),
          brightness: Brightness.dark,
        ),
        useMaterial3: true,
      ),
      home: configured
          ? HomeShell(settings: _settings, onSettingsChanged: _reload)
          : SettingsScreen(
              settings: _settings,
              onSaved: _reload,
              requireComplete: true,
            ),
    );
  }
}
