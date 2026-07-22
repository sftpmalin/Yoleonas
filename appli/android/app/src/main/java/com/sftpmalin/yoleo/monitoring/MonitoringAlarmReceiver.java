package com.sftpmalin.yoleo.monitoring;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public final class MonitoringAlarmReceiver extends BroadcastReceiver {
    public static final String ACTION_WATCHDOG = "com.sftpmalin.yoleo.MONITORING_WATCHDOG";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? "" : intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(action) ||
                Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
            MonitoringScheduler.restoreAfterBootOrUpdate(context);
            return;
        }
        if (ACTION_WATCHDOG.equals(action)) {
            MonitoringScheduler.onWatchdogAlarm(context);
        }
    }
}
