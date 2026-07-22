package com.kpp.kpp_kiosk

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private val channelName = "com.kpp.kiosk/lock_task"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channelName)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "startLockTask" -> {
                        try {
                            startLockTask()
                            result.success(true)
                        } catch (e: Exception) {
                            result.error("LOCK_TASK", e.message, null)
                        }
                    }
                    "stopLockTask" -> {
                        try {
                            stopLockTask()
                            result.success(true)
                        } catch (e: Exception) {
                            result.error("LOCK_TASK", e.message, null)
                        }
                    }
                    "isInLockTask" -> {
                        val am = getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
                        val mode = am.lockTaskModeState
                        result.success(
                            mode == ActivityManager.LOCK_TASK_MODE_LOCKED ||
                                mode == ActivityManager.LOCK_TASK_MODE_PINNED
                        )
                    }
                    else -> result.notImplemented()
                }
            }
    }
}

/** 開機自動啟動 kiosk App。 */
class BootReceiver : android.content.BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action != Intent.ACTION_BOOT_COMPLETED) return
        val launch = Intent(context, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(launch)
    }
}
