package com.sftpmalin.yoleo.monitoring;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;

import com.sftpmalin.yoleo.data.AppSettings;
import com.sftpmalin.yoleo.data.SecureStore;

public final class MonitoringScheduler {
    private static final int PERIODIC_JOB_ID = 925301;
    private static final int IMMEDIATE_JOB_ID = 925302;
    private static final int WATCHDOG_REQUEST_ID = 925303;
    private MonitoringScheduler() {
    }

    public static void schedule(Context context) {
        Context app = context.getApplicationContext();
        JobScheduler scheduler = app.getSystemService(JobScheduler.class);
        if (scheduler == null) {
            return;
        }
        long period = monitoringPeriod(app);
        boolean matchingPeriodicJob = false;
        for (JobInfo job : scheduler.getAllPendingJobs()) {
            if (job.getId() == PERIODIC_JOB_ID) {
                if (job.getIntervalMillis() == period) {
                    matchingPeriodicJob = true;
                    break;
                }
                scheduler.cancel(PERIODIC_JOB_ID);
                break;
            }
        }
        if (!matchingPeriodicJob) {
            JobInfo job = new JobInfo.Builder(
                    PERIODIC_JOB_ID,
                    new ComponentName(app, MonitoringJobService.class))
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setPeriodic(period)
                    .setPersisted(true)
                    .build();
            scheduler.schedule(job);
        }
        scheduleWatchdogAlarm(app, period);
    }

    public static void scheduleImmediate(Context context) {
        Context app = context.getApplicationContext();
        JobScheduler scheduler = app.getSystemService(JobScheduler.class);
        if (scheduler == null || scheduler.getPendingJob(IMMEDIATE_JOB_ID) != null) {
            return;
        }
        JobInfo job = new JobInfo.Builder(
                IMMEDIATE_JOB_ID,
                new ComponentName(app, MonitoringJobService.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setMinimumLatency(0)
                .build();
        scheduler.schedule(job);
    }

    public static void scheduleWatchdogAlarm(Context context) {
        Context app = context.getApplicationContext();
        scheduleWatchdogAlarm(app, monitoringPeriod(app));
    }

    private static void scheduleWatchdogAlarm(Context app, long delay) {
        AlarmManager alarms = app.getSystemService(AlarmManager.class);
        if (alarms == null) {
            return;
        }
        alarms.setAndAllowWhileIdle(
                AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + delay,
                watchdogIntent(app));
    }

    public static void reschedule(Context context) {
        JobScheduler scheduler = context.getApplicationContext().getSystemService(JobScheduler.class);
        if (scheduler != null) {
            scheduler.cancel(PERIODIC_JOB_ID);
            scheduler.cancel(IMMEDIATE_JOB_ID);
        }
        AlarmManager alarms = context.getApplicationContext().getSystemService(AlarmManager.class);
        if (alarms != null) {
            alarms.cancel(watchdogIntent(context.getApplicationContext()));
        }
        schedule(context);
    }

    private static long monitoringPeriod(Context context) {
        AppSettings settings = new SecureStore(context).loadSettings();
        return Math.max(15, settings.pollIntervalMinutes) * 60L * 1000L;
    }

    private static PendingIntent watchdogIntent(Context context) {
        Intent intent = new Intent(context, MonitoringAlarmReceiver.class)
                .setAction(MonitoringAlarmReceiver.ACTION_WATCHDOG);
        return PendingIntent.getBroadcast(
                context,
                WATCHDOG_REQUEST_ID,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }
}
