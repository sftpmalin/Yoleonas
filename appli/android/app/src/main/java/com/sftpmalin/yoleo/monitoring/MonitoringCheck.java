package com.sftpmalin.yoleo.monitoring;

import android.content.Context;

import com.sftpmalin.yoleo.data.ApiClient;
import com.sftpmalin.yoleo.data.AppSettings;
import com.sftpmalin.yoleo.data.SecureStore;

import org.json.JSONObject;

import java.util.concurrent.atomic.AtomicBoolean;

public final class MonitoringCheck {
    private static final AtomicBoolean RUNNING = new AtomicBoolean(false);

    private MonitoringCheck() {
    }

    public static void run(Context context) {
        run(context, null);
    }

    static void run(Context context, MonitoringRunGate.Token cancellation) {
        if (isCancelled(cancellation)) {
            return;
        }
        Context app = context.getApplicationContext();
        if (!RUNNING.compareAndSet(false, true)) {
            return;
        }
        try {
            if (isCancelled(cancellation)) {
                return;
            }
            SecureStore store = new SecureStore(app);
            AppSettings settings = store.loadSettings();
            String token = store.loadAccessToken();
            if (isCancelled(cancellation) ||
                    !settings.configured || token.isEmpty() || !store.hasP12()) {
                return;
            }
            JSONObject snapshot;
            try {
                ApiClient client = new ApiClient(
                        settings,
                        store.getP12File(),
                        store.loadP12Password());
                if (isCancelled(cancellation)) {
                    return;
                }
                snapshot = client.monitoringSnapshot(token);
            } catch (Exception error) {
                if (isCancelled(cancellation)) {
                    return;
                }
                if (error instanceof ApiClient.ApiException &&
                        ((ApiClient.ApiException) error).statusCode == 401) {
                    runMutation(cancellation, () -> {
                        store.clearAccessToken();
                        MonitoringState.recordBackgroundFailure(app, "Authentification expirée");
                        MonitoringNotifier.show(
                                app,
                                "background_authentication",
                                "Yoleo doit être réauthentifié",
                                "Ouvre l'application pour renouveler l'authentification du serveur.",
                                "home");
                    });
                    return;
                }
                runMutation(cancellation, () -> {
                    MonitoringState.recordBackgroundFailure(
                            app,
                            error.getClass().getSimpleName());
                    MonitoringState.recordFailure(app, settings);
                });
                return;
            }
            if (isCancelled(cancellation)) {
                return;
            }
            // Les erreurs locales d'évaluation/persistance ne doivent jamais être
            // requalifiées en panne serveur par le catch réseau ci-dessus.
            runMutation(cancellation, () -> {
                MonitoringState.evaluateSuccess(app, snapshot, settings);
                MonitoringState.recordBackgroundSuccess(app);
            });
        } finally {
            RUNNING.set(false);
        }
    }

    private static boolean isCancelled(MonitoringRunGate.Token cancellation) {
        return Thread.currentThread().isInterrupted() ||
                (cancellation != null && cancellation.isCancelled());
    }

    private static boolean runMutation(
            MonitoringRunGate.Token cancellation,
            Runnable mutation) {
        if (Thread.currentThread().isInterrupted()) {
            return false;
        }
        if (cancellation == null) {
            mutation.run();
            return true;
        }
        return cancellation.runIfActive(mutation);
    }
}
