package com.sftpmalin.yoleo.monitoring;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Build;
import android.util.Log;

import com.sftpmalin.yoleo.MainActivity;
import com.sftpmalin.yoleo.R;

public final class MonitoringNotifier {
    private static final String TAG = "YoleoMonitoring";
    public static final String EXTRA_TAB = "com.sftpmalin.yoleo.OPEN_TAB";
    private static final String CHANNEL_ID = "yoleo_monitoring";

    private MonitoringNotifier() {
    }

    public static void createChannel(Context context) {
        Context app = context.getApplicationContext();
        NotificationManager manager = app.getSystemService(NotificationManager.class);
        if (manager == null) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Surveillance Yoleo",
                NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("Alertes du NAS détectées lors des contrôles Android");
        manager.createNotificationChannel(channel);
    }

    public static boolean show(Context context, String key, String title, String message, String tab) {
        try {
            Context app = context.getApplicationContext();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                    app.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
                            PackageManager.PERMISSION_GRANTED) {
                return false;
            }
            NotificationManager manager = app.getSystemService(NotificationManager.class);
            if (manager == null) {
                return false;
            }
            createChannel(app);
            if (!manager.areNotificationsEnabled()) {
                return false;
            }
            NotificationChannel channel = manager.getNotificationChannel(CHANNEL_ID);
            if (channel == null ||
                    channel.getImportance() == NotificationManager.IMPORTANCE_NONE) {
                return false;
            }

            Intent open = new Intent(app, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra(EXTRA_TAB, tab == null ? "home" : tab);
            int requestCode = key == null ? 0 : key.hashCode();
            PendingIntent pending = PendingIntent.getActivity(
                    app,
                    requestCode,
                    open,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

            Notification notification = new Notification.Builder(app, CHANNEL_ID)
                    .setSmallIcon(R.drawable.ic_notification)
                    .setColor(Color.rgb(26, 215, 255))
                    .setContentTitle(title)
                    .setContentText(message)
                    .setStyle(new Notification.BigTextStyle().bigText(message))
                    .setCategory(Notification.CATEGORY_STATUS)
                    .setAutoCancel(true)
                    .setContentIntent(pending)
                    .build();
            manager.notify(requestCode, notification);
            return true;
        } catch (RuntimeException error) {
            Log.e(TAG, "Impossible d'afficher la notification " + key, error);
            return false;
        }
    }
}
