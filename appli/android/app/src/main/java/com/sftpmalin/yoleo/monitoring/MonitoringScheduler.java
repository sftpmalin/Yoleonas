package com.sftpmalin.yoleo.monitoring;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.util.Log;

import com.sftpmalin.yoleo.data.AppSettings;
import com.sftpmalin.yoleo.data.SecureStore;

public final class MonitoringScheduler {
    private static final String TAG = "YoleoMonitoring";
    private static final String PREFS = "yoleo_monitoring_scheduler";
    private static final String NEXT_WATCHDOG_AT = "next_watchdog_at";
    private static final String WATCHDOG_INTERVAL = "watchdog_interval";
    private static final int PERIODIC_JOB_ID = 925301;
    private static final int IMMEDIATE_JOB_ID = 925302;
    private static final int WATCHDOG_REQUEST_ID = 925303;
    private static final long DEFAULT_PERIOD_MS = 15L * 60L * 1000L;
    private static final long IMMEDIATE_LATENCY_MS = 1_000L;

    private MonitoringScheduler() {
    }

    public static void schedule(Context context) {
        Context app = context.getApplicationContext();
        long period = monitoringPeriod(app);
        try {
            schedulePeriodicJob(app, period);
        } catch (RuntimeException error) {
            Log.e(TAG, "Impossible de programmer le contrôle périodique", error);
        } finally {
            // Le réveil de secours doit survivre à tout échec du JobScheduler.
            scheduleWatchdogAlarm(app, period, false);
        }
    }

    public static void scheduleImmediate(Context context) {
        Context app = context.getApplicationContext();
        try {
            JobScheduler scheduler = app.getSystemService(JobScheduler.class);
            if (scheduler == null) {
                Log.e(TAG, "JobScheduler indisponible pour le contrôle immédiat");
                return;
            }
            if (scheduler.getPendingJob(IMMEDIATE_JOB_ID) != null) {
                Log.d(TAG, "Contrôle immédiat déjà programmé");
                return;
            }
            JobInfo job = new JobInfo.Builder(
                    IMMEDIATE_JOB_ID,
                    new ComponentName(app, MonitoringJobService.class))
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setMinimumLatency(IMMEDIATE_LATENCY_MS)
                    .build();
            scheduleJob(scheduler, job, "immédiat");
        } catch (RuntimeException error) {
            Log.e(TAG, "Impossible de programmer le contrôle immédiat", error);
        }
    }

    public static void scheduleWatchdogAlarm(Context context) {
        Context app = context.getApplicationContext();
        scheduleWatchdogAlarm(app, monitoringPeriod(app), false);
    }

    public static void onWatchdogAlarm(Context context) {
        Context app = context.getApplicationContext();
        long period = monitoringPeriod(app);
        try {
            scheduleImmediate(app);
        } finally {
            // L'alarme est ponctuelle : chaque réception prépare explicitement la suivante.
            scheduleWatchdogAlarm(app, period, true);
        }
    }

    public static void reschedule(Context context) {
        Context app = context.getApplicationContext();
        cancelJobs(app);
        cancelWatchdogAlarm(app);
        schedule(app);
    }

    public static void restoreAfterBootOrUpdate(Context context) {
        Context app = context.getApplicationContext();
        cancelJobs(app);
        cancelWatchdogAlarm(app);
        try {
            schedule(app);
        } finally {
            scheduleImmediate(app);
        }
    }

    private static void schedulePeriodicJob(Context app, long period) {
        JobScheduler scheduler = app.getSystemService(JobScheduler.class);
        if (scheduler == null) {
            Log.e(TAG, "JobScheduler indisponible pour le contrôle périodique");
            return;
        }
        JobInfo existing = scheduler.getPendingJob(PERIODIC_JOB_ID);
        if (existing != null && existing.getIntervalMillis() == period) {
            Log.d(TAG, "Contrôle périodique déjà programmé");
            return;
        }
        if (existing != null) {
            scheduler.cancel(PERIODIC_JOB_ID);
        }
        JobInfo job = new JobInfo.Builder(
                PERIODIC_JOB_ID,
                new ComponentName(app, MonitoringJobService.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(period)
                .setPersisted(true)
                .build();
        scheduleJob(scheduler, job, "périodique");
    }

    private static boolean scheduleJob(JobScheduler scheduler, JobInfo job, String label) {
        int result = scheduler.schedule(job);
        if (result == JobScheduler.RESULT_SUCCESS) {
            Log.i(TAG, "Contrôle " + label + " programmé");
            return true;
        }
        Log.e(TAG, "JobScheduler a refusé le contrôle " + label + " (résultat " + result + ")");
        return false;
    }

    private static synchronized void scheduleWatchdogAlarm(Context app, long delay, boolean force) {
        SharedPreferences preferences = schedulerPreferences(app);
        long now = System.currentTimeMillis();
        long existing = preferences.getLong(NEXT_WATCHDOG_AT, 0L);
        long existingInterval = preferences.getLong(WATCHDOG_INTERVAL, 0L);
        boolean reuseStoredSchedule = !force && existingInterval == delay && existing > now;

        AlarmManager alarms = app.getSystemService(AlarmManager.class);
        if (alarms == null) {
            Log.e(TAG, "AlarmManager indisponible pour l'alarme de secours");
            return;
        }
        PendingIntent pendingIntent = watchdogIntent(app);
        // Réaffirmer aussi une échéance déjà mémorisée : AlarmManager peut avoir
        // perdu l'alarme sans que les préférences aient été effacées. Conserver
        // son timestamp d'origine évite de la repousser à chaque passage.
        long triggerAt = reuseStoredSchedule ? existing : now + delay;
        try {
            alarms.setAndAllowWhileIdle(
                    AlarmManager.RTC_WAKEUP,
                    triggerAt,
                    pendingIntent);
            if (!reuseStoredSchedule) {
                preferences.edit()
                        .putLong(NEXT_WATCHDOG_AT, triggerAt)
                        .putLong(WATCHDOG_INTERVAL, delay)
                        .apply();
            }
            Log.i(TAG, reuseStoredSchedule
                    ? "Alarme de secours réaffirmée à l'échéance mémorisée"
                    : "Alarme de secours programmée");
        } catch (RuntimeException error) {
            Log.e(TAG, "Impossible de programmer l'alarme de secours", error);
        }
    }

    private static void cancelJobs(Context app) {
        JobScheduler scheduler = app.getSystemService(JobScheduler.class);
        if (scheduler != null) {
            try {
                scheduler.cancel(PERIODIC_JOB_ID);
                scheduler.cancel(IMMEDIATE_JOB_ID);
            } catch (RuntimeException error) {
                Log.e(TAG, "Impossible d'annuler les contrôles programmés", error);
            }
        }
    }

    private static synchronized void cancelWatchdogAlarm(Context app) {
        try {
            AlarmManager alarms = app.getSystemService(AlarmManager.class);
            if (alarms != null) {
                alarms.cancel(watchdogIntent(app));
            }
        } catch (RuntimeException error) {
            Log.e(TAG, "Impossible d'annuler l'alarme de secours", error);
        } finally {
            clearWatchdogState(app);
        }
    }

    private static long monitoringPeriod(Context context) {
        try {
            AppSettings settings = new SecureStore(context).loadSettings();
            return Math.max(15, settings.pollIntervalMinutes) * 60L * 1000L;
        } catch (RuntimeException error) {
            Log.e(TAG, "Réglages de fréquence illisibles, période de 15 minutes utilisée", error);
            return DEFAULT_PERIOD_MS;
        }
    }

    private static PendingIntent watchdogIntent(Context context) {
        Intent intent = new Intent(context, MonitoringAlarmReceiver.class)
                .setAction(MonitoringAlarmReceiver.ACTION_WATCHDOG)
                .setPackage(context.getPackageName());
        return PendingIntent.getBroadcast(
                context,
                WATCHDOG_REQUEST_ID,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private static SharedPreferences schedulerPreferences(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static void clearWatchdogState(Context context) {
        schedulerPreferences(context).edit()
                .remove(NEXT_WATCHDOG_AT)
                .remove(WATCHDOG_INTERVAL)
                .apply();
    }
}
