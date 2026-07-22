package com.sftpmalin.yoleo;

import android.app.Application;
import android.util.Log;

import com.sftpmalin.yoleo.monitoring.MonitoringNotifier;
import com.sftpmalin.yoleo.monitoring.MonitoringScheduler;

public final class YoleoApplication extends Application {
    private static final String TAG = "YoleoMonitoring";

    @Override
    public void onCreate() {
        super.onCreate();
        try {
            MonitoringNotifier.createChannel(this);
        } catch (RuntimeException error) {
            Log.e(TAG, "Impossible de créer le canal de notification", error);
        }
        try {
            MonitoringScheduler.schedule(this);
        } catch (RuntimeException error) {
            Log.e(TAG, "Impossible d'initialiser la surveillance", error);
        }
    }
}
