package com.sftpmalin.yoleo.monitoring;

import android.content.Context;
import android.content.SharedPreferences;

import com.sftpmalin.yoleo.data.AppSettings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.LocalDate;
import java.time.YearMonth;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Locale;
import java.util.Set;

public final class MonitoringState {
    private static final String PREFS = "yoleo_monitoring_state";
    private static final String SNAPSHOT = "baseline_snapshot";
    private static final String SERVER_KNOWN = "server_known";
    private static final String SERVER_ONLINE = "server_online";
    private static final String CONSECUTIVE_FAILURES = "consecutive_failures";
    private static final String LAST_REGISTRY_MONTH = "last_registry_month";
    private static final String LAST_BACKGROUND_CHECK = "last_background_check";
    private static final String LAST_BACKGROUND_RESULT = "last_background_result";
    private static final String BACKGROUND_CONFIRMED = "background_confirmed_v067";
    private static final String SOURCE_FINGERPRINT = "transition_source_fingerprint";
    private static final String TRANSITION_VERSION = "transition_version";
    private static final int CURRENT_TRANSITION_VERSION = 1;
    private static final String CPU_HIGH = "transition_cpu_high";
    private static final String RAM_HIGH = "transition_ram_high";
    private static final String STORAGE_KNOWN = "transition_storage_known";
    private static final String HIGH_STORAGE_PATHS = "transition_high_storage_paths";
    private static final String MOUNT_STATES = "transition_mount_states";
    private static final String DOCKER_SERVICE_KNOWN = "transition_docker_service_known";
    private static final String DOCKER_SERVICE_ACTIVE = "transition_docker_service_active";
    private static final String DOCKER_STATES = "transition_docker_states";
    private static final String SAMBA_KNOWN = "transition_samba_known";
    private static final String SAMBA_OK = "transition_samba_ok";
    private static final String TASK_EVENTS = "transition_task_events";
    private static final String BUILD_KNOWN = "transition_build_known";
    private static final String LAST_BUILD_PENDING = "transition_last_build_pending";

    private MonitoringState() {
    }

    public static void recordBaseline(Context context, JSONObject snapshot) {
        preferences(context).edit()
                .putString(SNAPSHOT, snapshot == null ? "" : snapshot.toString())
                .apply();
    }

    private static void recordSuccessfulCheck(
            Context context,
            JSONObject snapshot,
            boolean serverOnline) {
        preferences(context).edit()
                .putString(SNAPSHOT, snapshot == null ? "" : snapshot.toString())
                .putBoolean(SERVER_KNOWN, true)
                .putBoolean(SERVER_ONLINE, serverOnline)
                .putInt(CONSECUTIVE_FAILURES, 0)
                .apply();
    }

    private static void resetForSource(
            SharedPreferences preferences,
            String sourceFingerprint) {
        preferences.edit()
                .remove(SNAPSHOT)
                .remove(SERVER_KNOWN)
                .remove(SERVER_ONLINE)
                .remove(CONSECUTIVE_FAILURES)
                .remove(TRANSITION_VERSION)
                .remove(CPU_HIGH)
                .remove(RAM_HIGH)
                .remove(STORAGE_KNOWN)
                .remove(HIGH_STORAGE_PATHS)
                .remove(MOUNT_STATES)
                .remove(DOCKER_SERVICE_KNOWN)
                .remove(DOCKER_SERVICE_ACTIVE)
                .remove(DOCKER_STATES)
                .remove(SAMBA_KNOWN)
                .remove(SAMBA_OK)
                .remove(TASK_EVENTS)
                .remove(BUILD_KNOWN)
                .remove(LAST_BUILD_PENDING)
                .putString(SOURCE_FINGERPRINT, sourceFingerprint)
                .apply();
    }

    public static synchronized void evaluateSuccess(
            Context context,
            JSONObject snapshot,
            AppSettings settings) {
        SharedPreferences preferences = preferences(context);
        String sourceFingerprint = sourceFingerprint(settings);
        if (!sourceFingerprint.equals(preferences.getString(SOURCE_FINGERPRINT, ""))) {
            resetForSource(preferences, sourceFingerprint);
        }
        boolean known = preferences.getBoolean(SERVER_KNOWN, false);
        boolean wasOnline = preferences.getBoolean(SERVER_ONLINE, true);
        boolean keepRecoveryPending = false;
        if (known && !wasOnline && settings.notifyServerRecovery) {
            keepRecoveryPending = !notifyTransition(
                    context,
                    "server_recovered",
                    "Yoleo est de nouveau en ligne",
                    "Le serveur répond à nouveau aux contrôles Android.",
                    "home");
        }

        boolean transitionsReady = preferences.getInt(
                TRANSITION_VERSION,
                0) == CURRENT_TRANSITION_VERSION;
        SharedPreferences.Editor transitionEditor = preferences.edit();
        if (!hasSectionError(snapshot, "system")) {
            evaluateResources(
                    context,
                    snapshot,
                    settings,
                    preferences,
                    transitionEditor,
                    transitionsReady);
        }
        if (!hasSectionError(snapshot, "storage")) {
            evaluateStorage(
                    context,
                    snapshot,
                    settings,
                    preferences,
                    transitionEditor,
                    transitionsReady);
        }
        if (!hasSectionError(snapshot, "docker")) {
            evaluateDocker(
                    context,
                    snapshot,
                    settings,
                    preferences,
                    transitionEditor,
                    transitionsReady);
        }
        if (!hasSectionError(snapshot, "samba")) {
            evaluateSamba(
                    context,
                    snapshot,
                    settings,
                    preferences,
                    transitionEditor,
                    transitionsReady);
        }
        if (!hasSectionError(snapshot, "tasks")) {
            evaluateTasks(
                    context,
                    snapshot,
                    settings,
                    preferences,
                    transitionEditor,
                    transitionsReady);
        }
        if (!hasSectionError(snapshot, "build") &&
                !hasSectionError(snapshot, "system")) {
            evaluateBuild(
                    context,
                    snapshot,
                    settings,
                    preferences,
                    transitionEditor,
                    transitionsReady);
        }
        transitionEditor
                .putInt(TRANSITION_VERSION, CURRENT_TRANSITION_VERSION)
                .apply();
        if (transitionsReady) {
            evaluateRegistryReminder(context, settings);
        }
        recordSuccessfulCheck(context, snapshot, !keepRecoveryPending);
    }

    public static synchronized void recordFailure(Context context, AppSettings settings) {
        SharedPreferences preferences = preferences(context);
        String sourceFingerprint = sourceFingerprint(settings);
        if (!sourceFingerprint.equals(preferences.getString(SOURCE_FINGERPRINT, ""))) {
            resetForSource(preferences, sourceFingerprint);
        }
        int failures = preferences.getInt(CONSECUTIVE_FAILURES, 0) + 1;
        boolean consideredOffline = failures >= settings.offlineFailureCount;
        boolean wasOnline = preferences.getBoolean(SERVER_ONLINE, true);
        boolean keepOfflinePending = false;
        if (settings.notifyServerOffline && consideredOffline && wasOnline) {
            keepOfflinePending = !notifyTransition(
                    context,
                    "server_offline",
                    "Serveur Yoleo hors ligne",
                    "Le contrôle Android a échoué " + failures + " fois de suite.",
                    "home");
        }
        preferences.edit()
                .putBoolean(SERVER_KNOWN, true)
                .putBoolean(SERVER_ONLINE, !consideredOffline || keepOfflinePending)
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
            AppSettings settings,
            SharedPreferences preferences,
            SharedPreferences.Editor editor,
            boolean allowNotifications) {
        JSONObject system = snapshot.optJSONObject("system");
        if (system == null) {
            return;
        }
        double cpu = system.optDouble("cpu_percent", Double.NaN);
        if (Double.isFinite(cpu)) {
            boolean cpuKnown = allowNotifications && preferences.contains(CPU_HIGH);
            boolean cpuWasHigh = preferences.getBoolean(CPU_HIGH, false);
            boolean cpuShouldNotify = settings.notifyCpu &&
                    MonitoringTransitions.shouldNotifyHigh(
                            cpuKnown,
                            cpuWasHigh,
                            cpu,
                            settings.cpuThresholdPercent);
            boolean cpuDelivered = true;
            if (cpuShouldNotify) {
                cpuDelivered = notifyTransition(
                        context,
                        "cpu_high",
                        "CPU Yoleo très occupé",
                        formatPercent(cpu) + " utilisés (seuil " +
                                settings.cpuThresholdPercent + " %).",
                        "home");
            }
            boolean cpuHigh = MonitoringTransitions.updatedHighState(
                    cpuWasHigh,
                    cpu,
                    settings.cpuThresholdPercent);
            if (cpuShouldNotify && !cpuDelivered) {
                cpuHigh = cpuWasHigh;
            }
            editor.putBoolean(CPU_HIGH, cpuHigh);
        }

        double ram = system.optDouble("ram_percent", Double.NaN);
        if (Double.isFinite(ram)) {
            boolean ramKnown = allowNotifications && preferences.contains(RAM_HIGH);
            boolean ramWasHigh = preferences.getBoolean(RAM_HIGH, false);
            boolean ramShouldNotify = settings.notifyRam &&
                    MonitoringTransitions.shouldNotifyHigh(
                            ramKnown,
                            ramWasHigh,
                            ram,
                            settings.ramThresholdPercent);
            boolean ramDelivered = true;
            if (ramShouldNotify) {
                ramDelivered = notifyTransition(
                        context,
                        "ram_high",
                        "Mémoire Yoleo très occupée",
                        formatPercent(ram) + " utilisés (seuil " +
                                settings.ramThresholdPercent + " %).",
                        "home");
            }
            boolean ramHigh = MonitoringTransitions.updatedHighState(
                    ramWasHigh,
                    ram,
                    settings.ramThresholdPercent);
            if (ramShouldNotify && !ramDelivered) {
                ramHigh = ramWasHigh;
            }
            editor.putBoolean(RAM_HIGH, ramHigh);
        }
    }

    private static void evaluateStorage(
            Context context,
            JSONObject snapshot,
            AppSettings settings,
            SharedPreferences preferences,
            SharedPreferences.Editor editor,
            boolean allowNotifications) {
        JSONObject storage = snapshot.optJSONObject("storage");
        JSONArray mounts = storage == null ? null : storage.optJSONArray("mounts");
        if (mounts == null) {
            return;
        }
        boolean storageKnown = allowNotifications &&
                preferences.getBoolean(STORAGE_KNOWN, false);
        Set<String> highPaths = new HashSet<>(preferences.getStringSet(
                HIGH_STORAGE_PATHS,
                new HashSet<>()));
        JSONObject previousMountStates = readObject(preferences, MOUNT_STATES);
        JSONObject currentMountStates = new JSONObject();
        Set<String> seen = new HashSet<>();
        for (int index = 0; index < mounts.length(); index++) {
            JSONObject mount = mounts.optJSONObject(index);
            if (mount == null) {
                continue;
            }
            String path = mount.optString("path", "").trim();
            if (path.isEmpty() || !seen.add(path)) {
                continue;
            }
            Boolean mountedValue = strictBoolean(mount, "is_mount");
            if (mountedValue == null) {
                if (previousMountStates.has(path)) {
                    put(
                            currentMountStates,
                            path,
                            previousMountStates.optString(path, ""));
                }
                continue;
            }
            String label = first(mount.optString("label", ""), path);
            boolean mounted = mountedValue;
            double percent = mount.optDouble("percent", Double.NaN);
            boolean wasHigh = highPaths.contains(path);
            boolean storageShouldNotify = settings.notifyStorage &&
                    selectedMount(settings, path) &&
                    mounted &&
                    MonitoringTransitions.shouldNotifyHigh(
                            storageKnown,
                            wasHigh,
                            percent,
                            settings.storageThresholdPercent);
            boolean storageDelivered = true;
            if (storageShouldNotify) {
                storageDelivered = notifyTransition(
                        context,
                        "storage_" + path,
                        "Stockage Yoleo presque plein",
                        label + " est occupé à " + formatPercent(percent) +
                                " (seuil " + settings.storageThresholdPercent + " %).",
                        "storage");
            }
            boolean isHigh = mounted && MonitoringTransitions.updatedHighState(
                    wasHigh,
                    percent,
                    settings.storageThresholdPercent);
            if (storageShouldNotify && !storageDelivered) {
                isHigh = wasHigh;
            }
            if (isHigh) {
                highPaths.add(path);
            } else {
                highPaths.remove(path);
            }

            String currentMountState = mounted
                    ? "mounted"
                    : first(mount.optString("status", ""), "folder");
            String previousMountState = previousMountStates.optString(path, "");
            boolean mountShouldNotify = settings.notifyMountFailures &&
                    selectedMount(settings, path) &&
                    allowNotifications &&
                    "mounted".equalsIgnoreCase(previousMountState) &&
                    !mounted;
            boolean mountDelivered = true;
            if (mountShouldNotify) {
                String status = first(mount.optString("status_label", ""), "n'est plus monté");
                mountDelivered = notifyTransition(
                        context,
                        "mount_" + path,
                        label + " n'est plus un montage disque",
                        path + " : " + status + ".",
                        "storage");
            }
            put(
                    currentMountStates,
                    path,
                    mountShouldNotify && !mountDelivered
                            ? previousMountState
                            : currentMountState);
        }
        Set<String> missingCandidates = new HashSet<>();
        if (settings.mountSelectionConfigured) {
            missingCandidates.addAll(settings.displayedMountPaths);
        } else {
            Iterator<String> previousPaths = previousMountStates.keys();
            while (previousPaths.hasNext()) {
                missingCandidates.add(previousPaths.next());
            }
        }
        for (String selectedPath : missingCandidates) {
            if (!seen.contains(selectedPath)) {
                String previousMountState = previousMountStates.optString(selectedPath, "");
                boolean mountShouldNotify = settings.notifyMountFailures &&
                        allowNotifications &&
                        "mounted".equalsIgnoreCase(previousMountState);
                boolean mountDelivered = true;
                if (mountShouldNotify) {
                    mountDelivered = notifyTransition(
                            context,
                            "mount_" + selectedPath,
                            selectedPath + " a disparu",
                            "Le point de montage sélectionné n'existe plus dans le cliché du NAS.",
                            "storage");
                }
                highPaths.remove(selectedPath);
                put(
                        currentMountStates,
                        selectedPath,
                        mountShouldNotify && !mountDelivered
                                ? previousMountState
                                : "missing");
            }
        }
        highPaths.retainAll(seen);
        editor
                .putBoolean(STORAGE_KNOWN, true)
                .putStringSet(HIGH_STORAGE_PATHS, new HashSet<>(highPaths))
                .putString(MOUNT_STATES, currentMountStates.toString());
    }

    private static void evaluateDocker(
            Context context,
            JSONObject snapshot,
            AppSettings settings,
            SharedPreferences preferences,
            SharedPreferences.Editor editor,
            boolean allowNotifications) {
        JSONObject docker = snapshot.optJSONObject("docker");
        Boolean dockerAvailable = strictBoolean(docker, "available");
        if (!Boolean.TRUE.equals(dockerAvailable)) {
            return;
        }
        JSONObject service = docker.optJSONObject("service");
        Boolean activeValue = strictBoolean(service, "active");
        if (activeValue == null) {
            return;
        }
        boolean serviceKnown = allowNotifications &&
                preferences.getBoolean(DOCKER_SERVICE_KNOWN, false);
        boolean wasActive = preferences.getBoolean(DOCKER_SERVICE_ACTIVE, false);
        boolean active = activeValue;
        boolean serviceShouldNotify = settings.notifyDockerService &&
                MonitoringTransitions.becameFalse(
                    serviceKnown,
                    wasActive,
                    active);
        boolean serviceDelivered = true;
        if (serviceShouldNotify) {
            serviceDelivered = notifyTransition(
                    context,
                    "docker_service",
                    "Service Docker arrêté",
                    first(service.optString("label", ""), "Le service Docker n'est plus actif."),
                    "docker");
        }
        editor
                .putBoolean(DOCKER_SERVICE_KNOWN, true)
                .putBoolean(
                        DOCKER_SERVICE_ACTIVE,
                        serviceShouldNotify && !serviceDelivered
                                ? wasActive
                                : active);
        if (!active) {
            return;
        }

        JSONArray containers = docker.optJSONArray("containers");
        if (containers == null) {
            return;
        }
        boolean containersKnown = allowNotifications && preferences.contains(DOCKER_STATES);
        JSONObject previousStates = readObject(preferences, DOCKER_STATES);
        JSONObject currentStates = new JSONObject();
        Set<String> currentNames = new HashSet<>();
        for (int index = 0; index < containers.length(); index++) {
            JSONObject container = containers.optJSONObject(index);
            if (container == null) {
                continue;
            }
            String name = first(
                    container.optString("name", ""),
                    container.optString("id", ""));
            if (name.isEmpty() || !currentNames.add(name)) {
                continue;
            }
            Object rawState = container.opt("state");
            String state = rawState instanceof String
                    ? ((String) rawState).trim()
                    : "";
            String previousState = previousStates.has(name)
                    ? previousStates.optString(name, "unknown")
                    : null;
            if (state.isEmpty()) {
                if (previousState != null) {
                    put(currentStates, name, previousState);
                }
                continue;
            }
            boolean stoppedShouldNotify = settings.notifyDockerContainers &&
                    containersKnown &&
                    MonitoringTransitions.stopped(previousState, state);
            boolean stoppedDelivered = true;
            if (stoppedShouldNotify) {
                stoppedDelivered = notifyTransition(
                        context,
                        "docker_" + name,
                        "Conteneur Docker arrêté",
                        name + " est passé de running à " + state + ".",
                        "docker");
            }
            put(
                    currentStates,
                    name,
                    stoppedShouldNotify && !stoppedDelivered
                            ? previousState
                            : state);
        }
        if (settings.notifyDockerContainers && containersKnown) {
            Iterator<String> previousNames = previousStates.keys();
            while (previousNames.hasNext()) {
                String name = previousNames.next();
                if (!currentNames.contains(name) && MonitoringTransitions.disappeared(
                        previousStates.optString(name, "unknown"))) {
                    boolean delivered = notifyTransition(
                            context,
                            "docker_" + name,
                            "Conteneur Docker absent",
                            name + " n'apparaît plus dans l'inventaire Docker.",
                            "docker");
                    if (!delivered) {
                        put(
                                currentStates,
                                name,
                                previousStates.optString(name, "running"));
                    }
                }
            }
        }
        editor.putString(DOCKER_STATES, currentStates.toString());
    }

    private static void evaluateSamba(
            Context context,
            JSONObject snapshot,
            AppSettings settings,
            SharedPreferences preferences,
            SharedPreferences.Editor editor,
            boolean allowNotifications) {
        JSONObject samba = snapshot.optJSONObject("samba");
        Boolean sambaAvailable = strictBoolean(samba, "available");
        if (!Boolean.TRUE.equals(sambaAvailable)) {
            return;
        }
        Boolean okValue = strictBoolean(samba, "ok");
        if (okValue == null) {
            return;
        }
        boolean known = allowNotifications && preferences.getBoolean(SAMBA_KNOWN, false);
        boolean wasOk = preferences.getBoolean(SAMBA_OK, false);
        boolean ok = okValue;
        boolean shouldNotify = settings.notifySamba && MonitoringTransitions.becameFalse(
                known,
                wasOk,
                ok);
        boolean delivered = true;
        if (shouldNotify) {
            delivered = notifyTransition(
                    context,
                    "samba",
                    "Partage Samba à vérifier",
                    "Un service Samba ou WSDD n'est plus actif.",
                    "home");
        }
        editor
                .putBoolean(SAMBA_KNOWN, true)
                .putBoolean(SAMBA_OK, shouldNotify && !delivered ? wasOk : ok);
    }

    private static void evaluateTasks(
            Context context,
            JSONObject snapshot,
            AppSettings settings,
            SharedPreferences preferences,
            SharedPreferences.Editor editor,
            boolean allowNotifications) {
        JSONArray tasks = snapshot.optJSONArray("tasks");
        if (tasks == null) {
            return;
        }
        boolean eventsKnown = allowNotifications && preferences.contains(TASK_EVENTS);
        JSONObject previousEvents = readObject(preferences, TASK_EVENTS);
        JSONObject currentEvents = new JSONObject();
        for (int index = 0; index < tasks.length(); index++) {
            JSONObject task = tasks.optJSONObject(index);
            if (task == null) {
                continue;
            }
            if (!task.has("id") || task.isNull("id")) {
                continue;
            }
            String id = task.optString("id", "").trim();
            if (id.isEmpty()) {
                continue;
            }
            String signature = taskSignature(task);
            String previousSignature = previousEvents.has(id)
                    ? previousEvents.optString(id, "")
                    : null;
            boolean taskShouldNotify = settings.notifyTaskFailures &&
                    eventsKnown &&
                    MonitoringTransitions.failedEventChanged(
                            previousSignature,
                            signature,
                            taskFailed(task));
            boolean taskDelivered = true;
            if (taskShouldNotify) {
                String title = first(task.optString("title", ""), "Tâche Yoleo");
                String message = first(
                        task.optString("last_message", ""),
                        "La dernière exécution a échoué.");
                taskDelivered = notifyTransition(
                        context,
                        "task_" + id,
                        "Tâche Yoleo en erreur",
                        title + " : " + trim(message, 180),
                        "tasks");
            }
            put(
                    currentEvents,
                    id,
                    taskShouldNotify && !taskDelivered
                            ? previousSignature
                            : signature);
        }
        editor.putString(TASK_EVENTS, currentEvents.toString());
    }

    private static void evaluateBuild(
            Context context,
            JSONObject snapshot,
            AppSettings settings,
            SharedPreferences preferences,
            SharedPreferences.Editor editor,
            boolean allowNotifications) {
        JSONObject build = snapshot.optJSONObject("build");
        Boolean buildAvailable = strictBoolean(build, "available");
        if (!Boolean.TRUE.equals(buildAvailable)) {
            return;
        }
        Integer toBuildValue = nonNegativeInteger(build, "to_build");
        Integer toPushValue = nonNegativeInteger(build, "to_push");
        if (toBuildValue == null || toPushValue == null) {
            return;
        }
        int toBuild = toBuildValue;
        int toPush = toPushValue;
        int pending = (int) Math.min(Integer.MAX_VALUE, (long) toBuild + toPush);
        boolean known = allowNotifications && preferences.getBoolean(BUILD_KNOWN, false);
        int previous = preferences.getInt(LAST_BUILD_PENDING, 0);
        boolean shouldNotify = settings.notifyBuildPending &&
                MonitoringTransitions.pendingIncreased(
                known,
                previous,
                pending);
        boolean delivered = true;
        if (shouldNotify) {
            delivered = notifyTransition(
                    context,
                    "build_pending",
                    "Travail Docker en attente",
                    toBuild + " élément(s) à builder et " + toPush + " à envoyer.",
                    "home");
        }
        editor
                .putBoolean(BUILD_KNOWN, true)
                .putInt(
                        LAST_BUILD_PENDING,
                        shouldNotify && !delivered ? previous : pending);
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

    private static boolean notifyTransition(
            Context context,
            String key,
            String title,
            String message,
            String tab) {
        return MonitoringNotifier.show(context, key, title, message, tab);
    }

    private static boolean hasSectionError(JSONObject snapshot, String section) {
        JSONArray errors = snapshot == null ? null : snapshot.optJSONArray("errors");
        if (errors == null) {
            return false;
        }
        for (int index = 0; index < errors.length(); index++) {
            JSONObject error = errors.optJSONObject(index);
            if (error != null && section.equalsIgnoreCase(
                    error.optString("section", "").trim())) {
                return true;
            }
        }
        return false;
    }

    private static String taskSignature(JSONObject task) {
        return task.optString("updated_at", "") + '\u001f' +
                task.optString("last_end", "") + '\u001f' +
                task.optString("result", "") + '\u001f' +
                task.optString("status", "");
    }

    private static JSONObject readObject(SharedPreferences preferences, String key) {
        try {
            return new JSONObject(preferences.getString(key, "{}"));
        } catch (Exception ignored) {
            return new JSONObject();
        }
    }

    private static void put(JSONObject target, String key, String value) {
        try {
            target.put(key, value);
        } catch (Exception ignored) {
            // Une entrée invalide est ignorée ; le prochain contrôle reconstruira l'état.
        }
    }

    private static Boolean strictBoolean(JSONObject object, String key) {
        if (object == null || !object.has(key) || object.isNull(key)) {
            return null;
        }
        Object value = object.opt(key);
        return value instanceof Boolean ? (Boolean) value : null;
    }

    private static Integer nonNegativeInteger(JSONObject object, String key) {
        if (object == null || !object.has(key) || object.isNull(key)) {
            return null;
        }
        Object value = object.opt(key);
        if (!(value instanceof Number)) {
            return null;
        }
        double number = ((Number) value).doubleValue();
        if (!Double.isFinite(number) || number < 0 ||
                number > Integer.MAX_VALUE || number != Math.rint(number)) {
            return null;
        }
        return (int) number;
    }

    private static String sourceFingerprint(AppSettings settings) {
        if (settings == null) {
            return "\u001f";
        }
        String serverUrl = settings.serverUrl == null ? "" : settings.serverUrl.trim();
        while (serverUrl.endsWith("/")) {
            serverUrl = serverUrl.substring(0, serverUrl.length() - 1);
        }
        if (serverUrl.endsWith("/api/v1")) {
            serverUrl = serverUrl.substring(0, serverUrl.length() - "/api/v1".length());
        }
        String username = settings.username == null ? "" : settings.username.trim();
        return serverUrl + '\u001f' + username;
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
