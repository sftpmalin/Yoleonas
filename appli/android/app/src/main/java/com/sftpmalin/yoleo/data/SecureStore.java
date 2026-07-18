package com.sftpmalin.yoleo.data;

import android.content.ContentResolver;
import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

public final class SecureStore {
    private static final String PREFS = "yoleo_settings";
    private static final String KEY_ALIAS = "yoleo_android_secrets_v1";
    private static final String P12_FILE = "client.p12";

    private final Context context;
    private final SharedPreferences preferences;

    public SecureStore(Context context) {
        this.context = context.getApplicationContext();
        this.preferences = this.context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public AppSettings loadSettings() {
        AppSettings settings = new AppSettings();
        settings.serverUrl = preferences.getString("server_url", "");
        settings.username = preferences.getString("username", "");
        settings.p12DisplayName = preferences.getString("p12_display_name", "");
        settings.configured = preferences.getBoolean("configured", false);
        settings.pollIntervalMinutes = preferences.getInt("poll_interval_minutes", 15);
        settings.offlineFailureCount = preferences.getInt("offline_failure_count", 2);
        settings.notifyServerOffline = preferences.getBoolean("notify_server_offline", true);
        settings.notifyServerRecovery = preferences.getBoolean("notify_server_recovery", true);
        settings.notifyCpu = preferences.getBoolean("notify_cpu", true);
        settings.cpuThresholdPercent = preferences.getInt("cpu_threshold_percent", 90);
        settings.notifyRam = preferences.getBoolean("notify_ram", true);
        settings.ramThresholdPercent = preferences.getInt("ram_threshold_percent", 90);
        settings.notifyStorage = preferences.getBoolean("notify_storage", true);
        settings.storageThresholdPercent = preferences.getInt("storage_threshold_percent", 80);
        settings.notifyMountFailures = preferences.getBoolean("notify_mount_failures", true);
        settings.notifyDockerService = preferences.getBoolean("notify_docker_service", true);
        settings.notifyDockerContainers = preferences.getBoolean("notify_docker_containers", true);
        settings.notifySamba = preferences.getBoolean("notify_samba", true);
        settings.notifyTaskFailures = preferences.getBoolean("notify_task_failures", true);
        settings.notifyBuildPending = preferences.getBoolean("notify_build_pending", true);
        settings.notifyRegistryCleanup = preferences.getBoolean("notify_registry_cleanup", false);
        settings.registryReminderDay = preferences.getInt("registry_reminder_day", 1);
        settings.mountSelectionConfigured = preferences.getBoolean("mount_selection_configured", false);
        settings.displayedMountPaths = loadDisplayedMountPaths();
        settings.homeItems = loadHomeItems();
        settings.navigationOrder = loadNavigationOrder();
        settings.lastFilePath = normalizeFilePath(preferences.getString("last_file_path", "/mnt"));
        normalizeNotificationSettings(settings);
        return settings;
    }

    public void saveSettings(
            AppSettings settings,
            String p12Password,
            String accessToken) throws Exception {
        SharedPreferences.Editor editor = preferences.edit()
                .putString("server_url", clean(settings.serverUrl))
                .putString("username", clean(settings.username))
                .putString("p12_display_name", clean(settings.p12DisplayName))
                .putBoolean("configured", settings.configured)
                .putString("p12_password", encrypt(p12Password))
                .putString("access_token", encrypt(accessToken));
        writeNotificationSettings(editor, settings).apply();
    }

    public boolean saveNotificationSettings(AppSettings settings) {
        normalizeNotificationSettings(settings);
        return writeNotificationSettings(preferences.edit(), settings).commit();
    }

    public String loadP12Password() {
        return decryptSafely(preferences.getString("p12_password", ""));
    }

    public String loadAccessToken() {
        return decryptSafely(preferences.getString("access_token", ""));
    }

    public void saveAccessToken(String accessToken) throws Exception {
        preferences.edit().putString("access_token", encrypt(accessToken)).apply();
    }

    public void clearAccessToken() {
        preferences.edit().remove("access_token").apply();
    }

    public void saveLastFilePath(String path) {
        preferences.edit().putString("last_file_path", normalizeFilePath(path)).apply();
    }

    public File getP12File() {
        return new File(context.getFilesDir(), P12_FILE);
    }

    public boolean hasP12() {
        File file = getP12File();
        return file.isFile() && file.length() > 0;
    }

    public void importP12(ContentResolver resolver, Uri uri) throws Exception {
        File destination = getP12File();
        File temporary = new File(context.getFilesDir(), P12_FILE + ".tmp");
        try (InputStream input = resolver.openInputStream(uri);
             FileOutputStream output = new FileOutputStream(temporary, false)) {
            if (input == null) {
                throw new IllegalArgumentException("Impossible de lire le fichier P12 sélectionné.");
            }
            byte[] buffer = new byte[16 * 1024];
            int read;
            long total = 0;
            while ((read = input.read(buffer)) >= 0) {
                if (read == 0) {
                    continue;
                }
                total += read;
                if (total > 10L * 1024L * 1024L) {
                    throw new IllegalArgumentException("Le fichier P12 dépasse 10 Mio.");
                }
                output.write(buffer, 0, read);
            }
            output.getFD().sync();
        }
        if (temporary.length() == 0) {
            temporary.delete();
            throw new IllegalArgumentException("Le fichier P12 est vide.");
        }
        if (destination.exists() && !destination.delete()) {
            temporary.delete();
            throw new IllegalStateException("Impossible de remplacer l'ancien P12.");
        }
        if (!temporary.renameTo(destination)) {
            temporary.delete();
            throw new IllegalStateException("Impossible d'enregistrer le P12 dans le stockage privé.");
        }
    }

    private String encrypt(String rawValue) throws Exception {
        String value = rawValue == null ? "" : rawValue;
        if (value.isEmpty()) {
            return "";
        }
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey());
        byte[] encrypted = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
        byte[] iv = cipher.getIV();
        ByteBuffer packed = ByteBuffer.allocate(4 + iv.length + encrypted.length);
        packed.putInt(iv.length);
        packed.put(iv);
        packed.put(encrypted);
        return Base64.encodeToString(packed.array(), Base64.NO_WRAP);
    }

    private String decryptSafely(String encoded) {
        if (encoded == null || encoded.isEmpty()) {
            return "";
        }
        try {
            byte[] packedBytes = Base64.decode(encoded, Base64.NO_WRAP);
            ByteBuffer packed = ByteBuffer.wrap(packedBytes);
            int ivLength = packed.getInt();
            if (ivLength < 12 || ivLength > 32 || packed.remaining() <= ivLength) {
                return "";
            }
            byte[] iv = new byte[ivLength];
            packed.get(iv);
            byte[] encrypted = new byte[packed.remaining()];
            packed.get(encrypted);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), new GCMParameterSpec(128, iv));
            return new String(cipher.doFinal(encrypted), StandardCharsets.UTF_8);
        } catch (Exception ignored) {
            return "";
        }
    }

    private SecretKey getOrCreateKey() throws Exception {
        KeyStore keyStore = KeyStore.getInstance("AndroidKeyStore");
        keyStore.load(null);
        KeyStore.Entry existing = keyStore.getEntry(KEY_ALIAS, null);
        if (existing instanceof KeyStore.SecretKeyEntry) {
            return ((KeyStore.SecretKeyEntry) existing).getSecretKey();
        }
        KeyGenerator generator = KeyGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_AES,
                "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build());
        return generator.generateKey();
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    private static SharedPreferences.Editor writeNotificationSettings(
            SharedPreferences.Editor editor,
            AppSettings settings) {
        return editor
                .putInt("poll_interval_minutes", settings.pollIntervalMinutes)
                .putInt("offline_failure_count", settings.offlineFailureCount)
                .putBoolean("notify_server_offline", settings.notifyServerOffline)
                .putBoolean("notify_server_recovery", settings.notifyServerRecovery)
                .putBoolean("notify_cpu", settings.notifyCpu)
                .putInt("cpu_threshold_percent", settings.cpuThresholdPercent)
                .putBoolean("notify_ram", settings.notifyRam)
                .putInt("ram_threshold_percent", settings.ramThresholdPercent)
                .putBoolean("notify_storage", settings.notifyStorage)
                .putInt("storage_threshold_percent", settings.storageThresholdPercent)
                .putBoolean("notify_mount_failures", settings.notifyMountFailures)
                .putBoolean("notify_docker_service", settings.notifyDockerService)
                .putBoolean("notify_docker_containers", settings.notifyDockerContainers)
                .putBoolean("notify_samba", settings.notifySamba)
                .putBoolean("notify_task_failures", settings.notifyTaskFailures)
                .putBoolean("notify_build_pending", settings.notifyBuildPending)
                .putBoolean("notify_registry_cleanup", settings.notifyRegistryCleanup)
                .putInt("registry_reminder_day", settings.registryReminderDay)
                .putBoolean("mount_selection_configured", settings.mountSelectionConfigured)
                .putStringSet("displayed_mount_paths", new LinkedHashSet<>(settings.displayedMountPaths))
                .putStringSet("home_items", new LinkedHashSet<>(settings.homeItems))
                .putString("navigation_order", String.join(",", settings.navigationOrder))
                .putString("last_file_path", normalizeFilePath(settings.lastFilePath));
    }

    private LinkedHashSet<String> loadDisplayedMountPaths() {
        try {
            return new LinkedHashSet<>(preferences.getStringSet(
                    "displayed_mount_paths",
                    new LinkedHashSet<>()));
        } catch (ClassCastException incompatibleOldValue) {
            preferences.edit().remove("displayed_mount_paths").commit();
            return new LinkedHashSet<>();
        }
    }

    private LinkedHashSet<String> loadHomeItems() {
        try {
            return new LinkedHashSet<>(preferences.getStringSet(
                    "home_items",
                    AppSettings.defaultHomeItems()));
        } catch (ClassCastException incompatibleOldValue) {
            preferences.edit().remove("home_items").commit();
            return new LinkedHashSet<>(AppSettings.defaultHomeItems());
        }
    }

    private List<String> loadNavigationOrder() {
        String raw = preferences.getString("navigation_order", "");
        List<String> result = new ArrayList<>();
        if (raw != null) {
            for (String item : raw.split(",")) {
                String id = item.trim();
                if (AppSettings.defaultNavigationOrder().contains(id) && !result.contains(id)) {
                    result.add(id);
                }
            }
        }
        for (String required : AppSettings.defaultNavigationOrder()) {
            if (!result.contains(required)) {
                result.add(required);
            }
        }
        return result;
    }

    private static void normalizeNotificationSettings(AppSettings settings) {
        if (settings.pollIntervalMinutes != 15 &&
                settings.pollIntervalMinutes != 30 &&
                settings.pollIntervalMinutes != 60) {
            settings.pollIntervalMinutes = 15;
        }
        settings.offlineFailureCount = clamp(settings.offlineFailureCount, 1, 5);
        settings.cpuThresholdPercent = clamp(settings.cpuThresholdPercent, 1, 100);
        settings.ramThresholdPercent = clamp(settings.ramThresholdPercent, 1, 100);
        settings.storageThresholdPercent = clamp(settings.storageThresholdPercent, 1, 100);
        settings.registryReminderDay = clamp(settings.registryReminderDay, 1, 28);
        LinkedHashSet<String> cleaned = new LinkedHashSet<>();
        for (String raw : settings.displayedMountPaths) {
            String path = clean(raw);
            if (path.startsWith("/")) {
                cleaned.add(path.endsWith("/") && path.length() > 1
                        ? path.substring(0, path.length() - 1)
                        : path);
            }
        }
        settings.displayedMountPaths = cleaned;
        LinkedHashSet<String> home = new LinkedHashSet<>();
        for (String id : settings.homeItems) {
            if (AppSettings.defaultHomeItems().contains(id)) home.add(id);
        }
        settings.homeItems = home;
        List<String> order = new ArrayList<>();
        for (String id : settings.navigationOrder) {
            if (AppSettings.defaultNavigationOrder().contains(id) && !order.contains(id)) {
                order.add(id);
            }
        }
        for (String required : AppSettings.defaultNavigationOrder()) {
            if (!order.contains(required)) {
                order.add(required);
            }
        }
        settings.navigationOrder = order;
        settings.lastFilePath = normalizeFilePath(settings.lastFilePath);
    }

    private static String normalizeFilePath(String raw) {
        String path = clean(raw);
        if (!path.startsWith("/") || path.indexOf('\0') >= 0) {
            return "/mnt";
        }
        while (path.length() > 1 && path.endsWith("/")) {
            path = path.substring(0, path.length() - 1);
        }
        return path;
    }

    private static int clamp(int value, int minimum, int maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }
}
