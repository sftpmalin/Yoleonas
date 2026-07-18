package com.sftpmalin.yoleo.monitoring;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public final class MonitoringAlarmReceiver extends BroadcastReceiver {
    public static final String ACTION_WATCHDOG = "com.sftpmalin.yoleo.MONITORING_WATCHDOG";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? "" : intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(action)) {
            MonitoringScheduler.schedule(context);
            MonitoringScheduler.scheduleImmediate(context);
            return;
        }
        if (ACTION_WATCHDOG.equals(action)) {
            MonitoringScheduler.scheduleWatchdogAlarm(context);
            PendingResult pending = goAsync();
            Context app = context.getApplicationContext();
            new Thread(() -> {
                try {
                    MonitoringCheck.run(app);
                } finally {
                    pending.finish();
                }
            }, "yoleo-monitoring-alarm").start();
        }
    }
}
