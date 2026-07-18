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
        Context app = context.getApplicationContext();
        if (!RUNNING.compareAndSet(false, true)) {
            return;
        }
        try {
            SecureStore store = new SecureStore(app);
            AppSettings settings = store.loadSettings();
            String token = store.loadAccessToken();
            if (!settings.configured || token.isEmpty() || !store.hasP12()) {
                return;
            }
            try {
                ApiClient client = new ApiClient(
                        settings,
                        store.getP12File(),
                        store.loadP12Password());
                JSONObject snapshot = client.monitoringSnapshot(token);
                MonitoringState.evaluateSuccess(app, snapshot, settings);
                MonitoringState.recordBackgroundSuccess(app);
            } catch (Exception error) {
                if (error instanceof ApiClient.ApiException &&
                        ((ApiClient.ApiException) error).statusCode == 401) {
                    store.clearAccessToken();
                    MonitoringState.recordBackgroundFailure(app, "Authentification expirée");
                    MonitoringNotifier.show(
                            app,
                            "background_authentication",
                            "Yoleo doit être réauthentifié",
                            "Ouvre l'application pour renouveler l'authentification du serveur.",
                            "home");
                    return;
                }
                MonitoringState.recordBackgroundFailure(app, error.getClass().getSimpleName());
                MonitoringState.recordFailure(app, settings);
            }
        } finally {
            RUNNING.set(false);
        }
    }
}
