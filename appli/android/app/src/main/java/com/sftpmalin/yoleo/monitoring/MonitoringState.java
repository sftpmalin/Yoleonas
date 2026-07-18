package com.sftpmalin.yoleo.monitoring;

import android.content.Context;
import android.content.SharedPreferences;

import com.sftpmalin.yoleo.data.AppSettings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.LocalDate;
import java.time.YearMonth;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

public final class MonitoringState {
    private static final String PREFS = "yoleo_monitoring_state";
    private static final String SNAPSHOT = "baseline_snapshot";
    private static final String SERVER_KNOWN = "server_known";
    private static final String SERVER_ONLINE = "server_online";
    private static final String CONSECUTIVE_FAILURES = "consecutive_failures";
    private static final String LAST_NOTIFICATIONS = "last_notification_times";
    private static final String LAST_REGISTRY_MONTH = "last_registry_month";
    private static final String LAST_BACKGROUND_CHECK = "last_background_check";
    private static final String LAST_BACKGROUND_RESULT = "last_background_result";
    private static final String BACKGROUND_CONFIRMED = "background_confirmed_v067";
    private static final long REPEAT_DELAY_MS = 24L * 60L * 60L * 1000L;

    private MonitoringState() {
    }

    public static void recordBaseline(Context context, JSONObject snapshot, boolean serverOnline) {
        preferences(context).edit()
                .putString(SNAPSHOT, snapshot == null ? "" : snapshot.toString())
                .putBoolean(SERVER_KNOWN, true)
                .putBoolean(SERVER_ONLINE, serverOnline)
                .putInt(CONSECUTIVE_FAILURES, 0)
                .apply();
    }

    public static void evaluateSuccess(
            Context context,
            JSONObject snapshot,
            AppSettings settings) {
        SharedPreferences preferences = preferences(context);
        boolean known = preferences.getBoolean(SERVER_KNOWN, false);
        boolean wasOnline = preferences.getBoolean(SERVER_ONLINE, true);
        if (known && !wasOnline && settings.notifyServerRecovery) {
            notifyWithCooldown(
                    context,
                    "server_recovered",
                    "Yoleo est de nouveau en ligne",
                    "Le serveur répond à nouveau aux contrôles Android.",
                    "home");
        }

        evaluateResources(context, snapshot, settings);
        evaluateStorage(context, snapshot, settings);
        evaluateDocker(context, snapshot, settings);
        evaluateSamba(context, snapshot, settings);
        evaluateTasks(context, snapshot, settings);
        evaluateBuild(context, snapshot, settings);
        evaluateRegistryReminder(context, settings);
        recordBaseline(context, snapshot, true);
    }

    public static void recordFailure(Context context, AppSettings settings) {
        SharedPreferences preferences = preferences(context);
        int failures = preferences.getInt(CONSECUTIVE_FAILURES, 0) + 1;
        boolean consideredOffline = failures >= settings.offlineFailureCount;
        if (settings.notifyServerOffline && consideredOffline) {
            notifyWithCooldown(
                    context,
                    "server_offline",
                    "Serveur Yoleo hors ligne",
                    "Le contrôle Android a échoué " + failures + " fois de suite.",
                    "home");
        }
        preferences.edit()
                .putBoolean(SERVER_KNOWN, true)
                .putBoolean(SERVER_ONLINE, !consideredOffline)
                .putInt(CONSECUTIVE_FAILURES, failures)
                .apply();
    }

    public static void recordBackgroundSuccess(Context context) {
        SharedPreferences preferences = preferences(context);
        preferences.edit()
                .putLong(LAST_BACKGROUND_CHECK, System.currentTimeMillis())
                .putString(LAST_BACKGROUND_RESULT, "ok")
                .apply();
        if (preferences.getBoolean(BACKGROUND_CONFIRMED, false)) {
            return;
        }
        if (MonitoringNotifier.show(
                context,
                "background_monitoring_active",
                "Surveillance Yoleo active",
                "Le premier contrôle en arrière-plan a réussi.",
                "home")) {
            preferences.edit().putBoolean(BACKGROUND_CONFIRMED, true).apply();
        }
    }

    public static void recordBackgroundFailure(Context context, String result) {
        preferences(context).edit()
                .putLong(LAST_BACKGROUND_CHECK, System.currentTimeMillis())
                .putString(LAST_BACKGROUND_RESULT, first(result, "échec"))
                .apply();
    }

    private static void evaluateResources(
            Context context,
            JSONObject snapshot,
            AppSettings settings) {
        JSONObject system = snapshot.optJSONObject("system");
        if (system == null) {
            return;
        }
        double cpu = system.optDouble("cpu_percent", 0);
        if (settings.notifyCpu && cpu >= settings.cpuThresholdPercent) {
            notifyWithCooldown(
                    context,
                    "cpu_high",
                    "CPU Yoleo très occupé",
                    formatPercent(cpu) + " utilisés (seuil " + settings.cpuThresholdPercent + " %).",
                    "home");
        }
        double ram = system.optDouble("ram_percent", 0);
        if (settings.notifyRam && ram >= settings.ramThresholdPercent) {
            notifyWithCooldown(
                    context,
                    "ram_high",
                    "Mémoire Yoleo très occupée",
                    formatPercent(ram) + " utilisés (seuil " + settings.ramThresholdPercent + " %).",
                    "home");
        }
    }

    private static void evaluateStorage(
            Context context,
            JSONObject snapshot,
            AppSettings settings) {
        JSONObject storage = snapshot.optJSONObject("storage");
        JSONArray mounts = storage == null ? null : storage.optJSONArray("mounts");
        if (mounts == null) {
            return;
        }
        Set<String> seen = new HashSet<>();
        for (int index = 0; index < mounts.length(); index++) {
            JSONObject mount = mounts.optJSONObject(index);
            if (mount == null) {
                continue;
            }
            String path = mount.optString("path", "").trim();
            if (path.isEmpty() || !seen.add(path) || !selectedMount(settings, path)) {
                continue;
            }
            String label = first(mount.optString("label", ""), path);
            boolean mounted = mount.optBoolean("is_mount", false);
            double percent = mount.optDouble("percent", 0);
            if (settings.notifyStorage && mounted && percent >= settings.storageThresholdPercent) {
                notifyWithCooldown(
                        context,
                        "storage_" + path,
                        "Stockage Yoleo presque plein",
                        label + " est occupé à " + formatPercent(percent) +
                                " (seuil " + settings.storageThresholdPercent + " %).",
                        "storage");
            }
            if (settings.notifyMountFailures && !mounted) {
                String status = first(mount.optString("status_label", ""), "n'est plus monté");
                notifyWithCooldown(
                        context,
                        "mount_" + path,
                        label + " n'est plus un montage disque",
                        path + " : " + status + ".",
                        "storage");
            }
        }
        if (settings.notifyMountFailures && settings.mountSelectionConfigured) {
            for (String selectedPath : settings.displayedMountPaths) {
                if (!seen.contains(selectedPath)) {
                    notifyWithCooldown(
                            context,
                            "mount_" + selectedPath,
                            selectedPath + " a disparu",
                            "Le point de montage sélectionné n'existe plus dans le cliché du NAS.",
                            "storage");
                }
            }
        }
    }

    private static void evaluateDocker(
            Context context,
            JSONObject snapshot,
            AppSettings settings) {
        JSONObject docker = snapshot.optJSONObject("docker");
        if (docker == null) {
            return;
        }
        JSONObject service = docker.optJSONObject("service");
        if (settings.notifyDockerService && service != null &&
                !service.optBoolean("active", false)) {
            notifyWithCooldown(
                    context,
                    "docker_service",
                    "Service Docker arrêté",
                    first(service.optString("label", ""), "Le service Docker n'est plus actif."),
                    "docker");
        }
        if (!settings.notifyDockerContainers) {
            return;
        }
        JSONArray containers = docker.optJSONArray("containers");
        if (containers == null) {
            return;
        }
        for (int index = 0; index < containers.length(); index++) {
            JSONObject container = containers.optJSONObject(index);
            if (container == null) {
                continue;
            }
            String state = container.optString("state", "unknown");
            if (!"running".equalsIgnoreCase(state)) {
                String name = first(container.optString("name", ""), "Conteneur Docker");
                String key = first(container.optString("id", ""), name);
                notifyWithCooldown(
                        context,
                        "docker_" + key,
                        "Conteneur Docker à vérifier",
                        name + " est actuellement " + state + ".",
                        "docker");
            }
        }
    }

    private static void evaluateSamba(
            Context context,
            JSONObject snapshot,
            AppSettings settings) {
        JSONObject samba = snapshot.optJSONObject("samba");
        if (settings.notifySamba && samba != null &&
                samba.optBoolean("available", false) &&
                !samba.optBoolean("ok", false)) {
            notifyWithCooldown(
                    context,
                    "samba",
                    "Partage Samba à vérifier",
                    "Un service Samba ou WSDD n'est plus actif.",
                    "home");
        }
    }

    private static void evaluateTasks(
            Context context,
            JSONObject snapshot,
            AppSettings settings) {
        if (!settings.notifyTaskFailures) {
            return;
        }
        JSONArray tasks = snapshot.optJSONArray("tasks");
        if (tasks == null) {
            return;
        }
        for (int index = 0; index < tasks.length(); index++) {
            JSONObject task = tasks.optJSONObject(index);
            if (task == null || !taskFailed(task)) {
                continue;
            }
            String id = String.valueOf(task.opt("id"));
            String title = first(task.optString("title", ""), "Tâche Yoleo");
            String message = first(task.optString("last_message", ""), "La dernière exécution a échoué.");
            notifyWithCooldown(
                    context,
                    "task_" + id,
                    "Tâche Yoleo en erreur",
                    title + " : " + trim(message, 180),
                    "tasks");
        }
    }

    private static void evaluateBuild(
            Context context,
            JSONObject snapshot,
            AppSettings settings) {
        JSONObject build = snapshot.optJSONObject("build");
        if (!settings.notifyBuildPending || build == null ||
                !build.optBoolean("available", false)) {
            return;
        }
        int toBuild = Math.max(0, build.optInt("to_build", 0));
        int toPush = Math.max(0, build.optInt("to_push", 0));
        if (toBuild + toPush > 0) {
            notifyWithCooldown(
                    context,
                    "build_pending",
                    "Travail Docker en attente",
                    toBuild + " élément(s) à builder et " + toPush + " à envoyer.",
                    "home");
        }
    }

    private static void evaluateRegistryReminder(Context context, AppSettings settings) {
        if (!settings.notifyRegistryCleanup) {
            return;
        }
        LocalDate now = LocalDate.now();
        if (now.getDayOfMonth() < settings.registryReminderDay) {
            return;
        }
        SharedPreferences preferences = preferences(context);
        String month = YearMonth.from(now).toString();
        if (month.equals(preferences.getString(LAST_REGISTRY_MONTH, ""))) {
            return;
        }
        boolean shown = MonitoringNotifier.show(
                context,
                "registry_" + month,
                "Rappel d'entretien Yoleo",
                "Pense à vérifier et nettoyer le registre Docker.",
                "home");
        if (shown) {
            preferences.edit().putString(LAST_REGISTRY_MONTH, month).apply();
        }
    }

    private static void notifyWithCooldown(
            Context context,
            String key,
            String title,
            String message,
            String tab) {
        SharedPreferences preferences = preferences(context);
        JSONObject times;
        try {
            times = new JSONObject(preferences.getString(LAST_NOTIFICATIONS, "{}"));
        } catch (Exception ignored) {
            times = new JSONObject();
        }
        long now = System.currentTimeMillis();
        if (now - times.optLong(key, 0L) < REPEAT_DELAY_MS) {
            return;
        }
        if (!MonitoringNotifier.show(context, key, title, message, tab)) {
            return;
        }
        try {
            times.put(key, now);
            preferences.edit().putString(LAST_NOTIFICATIONS, times.toString()).apply();
        } catch (Exception ignored) {
            // Une notification non mémorisée pourra seulement être répétée au prochain contrôle.
        }
    }

    private static boolean selectedMount(AppSettings settings, String path) {
        return !settings.mountSelectionConfigured || settings.displayedMountPaths.contains(path);
    }

    private static boolean taskFailed(JSONObject task) {
        String value = (task.optString("result", "") + " " + task.optString("status", ""))
                .toLowerCase(Locale.ROOT);
        return value.contains("erreur") || value.contains("échec") ||
                value.contains("echec") || value.contains("failed") ||
                value.contains("failure");
    }

    private static String formatPercent(double value) {
        return String.format(Locale.FRANCE, "%.1f %%", value);
    }

    private static String first(String value, String fallback) {
        return value == null || value.trim().isEmpty() ? fallback : value.trim();
    }

    private static String trim(String value, int maximum) {
        String clean = first(value, "Aucun détail supplémentaire.");
        return clean.length() <= maximum ? clean : clean.substring(0, maximum) + "…";
    }

    private static SharedPreferences preferences(Context context) {
        return context.getApplicationContext()
                .getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
