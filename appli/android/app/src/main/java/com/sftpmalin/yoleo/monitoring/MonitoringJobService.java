package com.sftpmalin.yoleo.monitoring;

import android.app.job.JobParameters;
import android.app.job.JobService;
import android.util.Log;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;

public final class MonitoringJobService extends JobService {
    private static final String TAG = "YoleoMonitoring";
    private static final MonitoringRunGate RUN_GATE = new MonitoringRunGate();
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final ConcurrentHashMap<Integer, RunningJob> runningJobs = new ConcurrentHashMap<>();

    @Override
    public boolean onStartJob(JobParameters params) {
        int jobId = params.getJobId();
        MonitoringRunGate.Token token = RUN_GATE.tryAcquire();
        if (token == null) {
            Log.i(TAG, "Contrôle " + jobId + " fusionné avec le contrôle déjà en cours");
            return false;
        }

        RunningJob run = new RunningJob(params, token);
        if (runningJobs.putIfAbsent(jobId, run) != null) {
            token.cancel();
            token.release();
            Log.i(TAG, "Contrôle " + jobId + " déjà en cours");
            return false;
        }
        try {
            executor.execute(() -> execute(run));
            return true;
        } catch (RejectedExecutionException error) {
            runningJobs.remove(jobId, run);
            token.cancel();
            token.release();
            Log.e(TAG, "Exécuteur indisponible pour le contrôle " + jobId, error);
            safeScheduleWatchdog();
            return false;
        }
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        RunningJob run = runningJobs.get(params.getJobId());
        if (run != null) {
            run.cancel();
        }
        safeScheduleWatchdog();
        return true;
    }

    @Override
    public void onDestroy() {
        for (RunningJob run : runningJobs.values()) {
            run.cancel();
        }
        // Les tâches annulées traversent encore leur finally pour libérer le
        // garde global. Leur jeton interdit désormais toute mutation.
        executor.shutdown();
        super.onDestroy();
    }

    private void execute(RunningJob run) {
        run.attachWorker(Thread.currentThread());
        try {
            if (!run.isStopped() && !run.token.isCancelled()) {
                MonitoringCheck.run(getApplicationContext(), run.token);
            }
        } catch (RuntimeException error) {
            Log.e(TAG, "Échec inattendu du contrôle " + run.jobId, error);
        } finally {
            finishRun(run);
        }
    }

    private void finishRun(RunningJob run) {
        boolean activeCompletion = run.beginFinish();
        boolean ownsJob = runningJobs.remove(run.jobId, run);
        boolean shouldFinish = ownsJob && activeCompletion;
        try {
            safeScheduleWatchdog();
        } finally {
            try {
                run.token.release();
            } finally {
                if (shouldFinish) {
                    jobFinished(run.params, false);
                }
            }
        }
    }

    private void safeScheduleWatchdog() {
        try {
            MonitoringScheduler.scheduleWatchdogAlarm(getApplicationContext());
        } catch (RuntimeException error) {
            Log.e(TAG, "Impossible de réarmer l'alarme de secours", error);
        }
    }

    private static final class RunningJob {
        final JobParameters params;
        final int jobId;
        final MonitoringRunGate.Token token;
        private Thread worker;
        private boolean stopped;
        private boolean finished;

        RunningJob(JobParameters params, MonitoringRunGate.Token token) {
            this.params = params;
            this.jobId = params.getJobId();
            this.token = token;
        }

        synchronized void attachWorker(Thread current) {
            worker = current;
            if (stopped) {
                current.interrupt();
            }
        }

        synchronized boolean isStopped() {
            return stopped;
        }

        void cancel() {
            Thread current;
            synchronized (this) {
                if (stopped || finished) {
                    return;
                }
                stopped = true;
                current = worker;
            }
            // cancel() attend une éventuelle mutation déjà commencée. Après son
            // retour, MonitoringCheck ne peut plus en démarrer une nouvelle.
            token.cancel();
            if (current != null) {
                current.interrupt();
            }
        }

        synchronized boolean beginFinish() {
            if (finished) {
                return false;
            }
            finished = true;
            worker = null;
            return !stopped;
        }
    }
}
