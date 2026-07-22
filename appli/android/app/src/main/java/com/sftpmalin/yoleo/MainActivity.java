package com.sftpmalin.yoleo;

import android.annotation.SuppressLint;
import android.annotation.TargetApi;
import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentValues;
import android.content.Intent;
import android.content.ClipData;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.provider.Settings;
import android.util.Log;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import com.sftpmalin.yoleo.data.ApiClient;
import com.sftpmalin.yoleo.data.AppSettings;
import com.sftpmalin.yoleo.data.IconCache;
import com.sftpmalin.yoleo.data.SecureStore;
import com.sftpmalin.yoleo.monitoring.MonitoringNotifier;
import com.sftpmalin.yoleo.monitoring.MonitoringScheduler;
import com.sftpmalin.yoleo.monitoring.MonitoringState;
import com.sftpmalin.yoleo.ui.DashboardView;
import com.sftpmalin.yoleo.ui.SettingsView;
import com.sftpmalin.yoleo.ui.SetupView;
import com.sftpmalin.yoleo.ui.Ui;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.URI;
import java.net.URLConnection;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final String TAG = "YoleoMain";
    private static final int REQUEST_P12 = 1201;
    private static final int REQUEST_NOTIFICATIONS = 1202;
    private static final int REQUEST_FILE_UPLOAD = 1203;
    private static final long LIVE_REFRESH_DELAY_MS = 3_000L;
    private static final long LIVE_START_GRACE_MS = 15_000L;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final ExecutorService iconExecutor = Executors.newFixedThreadPool(3);
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    private SecureStore secureStore;
    private IconCache iconCache;
    private SetupView setupView;
    private SettingsView settingsView;
    private DashboardView dashboardView;
    private Uri selectedP12Uri;
    private String selectedP12Name = "";
    private String pendingAccessToken = "";
    private String testedFingerprint = "";
    private String transientServerPassword = "";
    private JSONObject capabilities;
    private JSONObject lastSnapshot;
    private String capabilitiesFingerprint = "";
    private boolean showingSetup;
    private boolean showingOptions;
    private boolean setupCanGoBack;
    private boolean actionInProgress;
    private int refreshGeneration;
    private long lastForegroundRefresh;
    private volatile ApiClient dashboardClient;
    private String pendingTab = "";
    private boolean foreground;
    private boolean backgroundPermissionDialogShowing;
    private String liveWatchTab = "";
    private String liveWatchKey = "";
    private boolean liveWatchObservedRunning;
    private long liveWatchStartedAt;
    private String pendingUploadDirectory = "";
    private int fileGeneration;
    private final Runnable liveRefreshRunnable = () -> {
        if (!shouldAutoRefreshCurrentTab()) {
            return;
        }
        refreshDashboard(false, false);
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        secureStore = new SecureStore(this);
        iconCache = new IconCache(this);
        pendingTab = readRequestedTab(getIntent());
        Window window = getWindow();
        window.setStatusBarColor(Ui.BACKGROUND);
        window.setNavigationBarColor(Ui.BACKGROUND);
        window.getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerPredictiveBack();
        }

        try {
            AppSettings settings = secureStore.loadSettings();
            if (settings.configured) {
                showDashboard();
            } else {
                showSetup(false);
            }
        } catch (Throwable error) {
            showStartupFailure(error);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        foreground = true;
        AppSettings settings = secureStore == null ? new AppSettings() : secureStore.loadSettings();
        if (settings.configured && !showingSetup && !showingOptions) {
            try {
                MonitoringScheduler.schedule(this);
            } catch (Throwable error) {
                Log.e(TAG, "Planification de la surveillance impossible", error);
            }
            mainHandler.post(() -> {
                try {
                    requestNotificationPermissionOnce();
                } catch (Throwable error) {
                    Log.e(TAG, "Demande de permission de notification impossible", error);
                }
            });
            mainHandler.postDelayed(() -> {
                try {
                    requestBackgroundMonitoringPermissionOnce();
                } catch (Throwable error) {
                    Log.e(TAG, "Demande d'autorisation d'arrière-plan impossible", error);
                }
            }, 1_200L);
            try {
                long now = System.currentTimeMillis();
                if (now - lastForegroundRefresh > 1_500L) {
                    lastForegroundRefresh = now;
                    refreshDashboard();
                }
            } catch (Throwable error) {
                Log.e(TAG, "Actualisation initiale impossible", error);
                if (dashboardView != null) {
                    dashboardView.showError("Erreur de démarrage : " + innermostMessage(error));
                }
            }
            scheduleLiveRefresh();
        }
    }

    @Override
    protected void onPause() {
        foreground = false;
        mainHandler.removeCallbacks(liveRefreshRunnable);
        super.onPause();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String requested = readRequestedTab(intent);
        if (!requested.isEmpty()) {
            pendingTab = requested;
            if (dashboardView != null && !showingSetup && !showingOptions) {
                dashboardView.selectTabById(requested);
                if ("files".equals(requested)) {
                    loadFileDirectory(secureStore.loadSettings().lastFilePath, true);
                }
                scheduleLiveRefresh();
                pendingTab = "";
            }
        }
    }

    @Override
    protected void onDestroy() {
        refreshGeneration++;
        mainHandler.removeCallbacks(liveRefreshRunnable);
        executor.shutdownNow();
        iconExecutor.shutdownNow();
        super.onDestroy();
    }

    @Override
    @SuppressLint("GestureBackNavigation")
    public void onBackPressed() {
        if (!handleBackNavigation()) {
            super.onBackPressed();
        }
    }

    private boolean handleBackNavigation() {
        if (showingOptions) {
            showDashboard();
            showLastSnapshotOrRefresh();
            return true;
        }
        if (showingSetup && setupCanGoBack) {
            showDashboard();
            refreshDashboard();
            return true;
        }
        if (!showingSetup && dashboardView != null && !"home".equals(dashboardView.getCurrentTab())) {
            dashboardView.selectHome();
            scheduleLiveRefresh();
            return true;
        }
        return false;
    }

    @TargetApi(Build.VERSION_CODES.TIRAMISU)
    private void registerPredictiveBack() {
        getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT,
                () -> {
                    if (!handleBackNavigation()) {
                        finishAfterTransition();
                    }
                });
    }

    private void showSetup(boolean allowBack) {
        refreshGeneration++;
        showingSetup = true;
        showingOptions = false;
        setupCanGoBack = allowBack;
        AppSettings settings = secureStore.loadSettings();
        if (selectedP12Name.isEmpty()) {
            selectedP12Name = settings.p12DisplayName;
        }
        setupView = new SetupView(
                this,
                settings,
                secureStore.loadP12Password(),
                secureStore.hasP12(),
                allowBack,
                new SetupView.Listener() {
                    @Override
                    public void onChooseP12() {
                        chooseP12();
                    }

                    @Override
                    public void onTest(SetupView.FormData data) {
                        testConnection(data);
                    }

                    @Override
                    public void onSave(SetupView.FormData data) {
                        saveConfiguration(data);
                    }

                    @Override
                    public void onBack() {
                        showDashboard();
                        refreshDashboard();
                    }
                });
        if (!selectedP12Name.isEmpty()) {
            setupView.setP12Name(selectedP12Name, secureStore.hasP12());
        }
        setRootView(setupView.getView());
    }

    private void chooseP12() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("application/octet-stream");
        intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
                "application/x-pkcs12",
                "application/pkcs12",
                "application/x-pkcs7-certificates",
                "application/octet-stream"
        });
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
        startActivityForResult(intent, REQUEST_P12);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null) {
            return;
        }
        if (requestCode == REQUEST_P12 && data.getData() != null) {
            selectedP12Uri = data.getData();
            try {
                getContentResolver().takePersistableUriPermission(
                        selectedP12Uri,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION);
            } catch (Exception ignored) {
                // Le fichier sera copié immédiatement dans le stockage privé au test ou à l'enregistrement.
            }
            selectedP12Name = displayName(selectedP12Uri);
            pendingAccessToken = "";
            testedFingerprint = "";
            if (setupView != null) {
                setupView.setP12Name(selectedP12Name, false);
            }
        } else if (requestCode == REQUEST_FILE_UPLOAD) {
            uploadSelectedFiles(data);
        }
    }

    private void testConnection(SetupView.FormData data) {
        SetupView target = setupView;
        if (target == null) {
            return;
        }
        pendingAccessToken = "";
        testedFingerprint = "";
        target.resetStatuses();
        target.setBusy(true);

        executor.execute(() -> {
            int stage = 0;
            try {
                importSelectedP12IfNeeded();
                AppSettings candidate = candidateSettings(data);
                ApiClient client = new ApiClient(candidate, secureStore.getP12File(), data.p12Password);
                postSetup(target, () -> target.setStatus(0, SetupView.Status.SUCCESS, "Certificat client chargé"));

                stage = 1;
                client.health();
                postSetup(target, () -> target.setStatus(1, SetupView.Status.SUCCESS, "Certificat P12 / connexion HTTPS : OK"));

                stage = 2;
                String token = client.login(data.username, data.serverPassword);
                postSetup(target, () -> target.setStatus(2, SetupView.Status.SUCCESS, "Identifiants : OK"));

                stage = 3;
                JSONObject identity = client.me(token);
                String verifiedUser = identity == null ? data.username : identity.optString("username", data.username);
                pendingAccessToken = token;
                testedFingerprint = data.fingerprint(selectedNameFor(candidate));
                postSetup(target, () -> target.setStatus(
                        3,
                        SetupView.Status.SUCCESS,
                        "Jeton API : OK — " + verifiedUser));
            } catch (Exception error) {
                int failedStage = stage;
                String message = innermostMessage(error);
                postSetup(target, () -> target.setStatus(
                        failedStage,
                        SetupView.Status.FAILURE,
                        "Échec : " + message));
            } finally {
                postSetup(target, () -> target.setBusy(false));
            }
        });
    }

    private void saveConfiguration(SetupView.FormData data) {
        SetupView target = setupView;
        if (target == null) {
            return;
        }
        target.setBusy(true);
        executor.execute(() -> {
            try {
                importSelectedP12IfNeeded();
                AppSettings settings = candidateSettings(data);
                settings.configured = true;
                String fingerprint = data.fingerprint(selectedNameFor(settings));
                String token = fingerprint.equals(testedFingerprint) ? pendingAccessToken : "";
                secureStore.saveSettings(settings, data.p12Password, token);
                transientServerPassword = data.serverPassword;
                capabilities = null;
                capabilitiesFingerprint = "";
                mainHandler.post(() -> {
                    Toast.makeText(this, "Configuration enregistrée", Toast.LENGTH_SHORT).show();
                    showDashboard();
                    refreshDashboard();
                });
            } catch (Exception error) {
                postSetup(target, () -> {
                    target.setStatus(0, SetupView.Status.FAILURE, "Échec : " + innermostMessage(error));
                    target.setBusy(false);
                });
            }
        });
    }

    private void showDashboard() {
        showingSetup = false;
        showingOptions = false;
        setupCanGoBack = false;
        AppSettings settings = secureStore.loadSettings();
        dashboardView = new DashboardView(this, serverHost(settings.serverUrl), settings, new DashboardView.Listener() {
            @Override
            public void onRefresh() {
                refreshDashboard();
            }

            @Override
            public void onSettings() {
                showSetup(true);
            }

            @Override
            public void onNotificationSettings() {
                showNotificationSettings();
            }

            @Override
            public void onDockerAction(String containerId, String containerName, String action) {
                confirmDockerAction(containerId, containerName, action);
            }

            @Override
            public void onVmAction(String vmName, String action) {
                confirmVmAction(vmName, action);
            }

            @Override
            public void onTaskAction(int taskId, String taskName, String action) {
                confirmTaskAction(taskId, taskName, action);
            }

            @Override
            public void onBackupAction(String filename, String title, String action) {
                confirmBackupAction(filename, title, action);
            }

            @Override
            public void onFileNavigate(String path) {
                loadFileDirectory(path, true);
            }

            @Override
            public void onFileCreateFolder(String directory) {
                promptCreateFolder(directory);
            }

            @Override
            public void onFileUpload(String directory) {
                chooseFilesToUpload(directory);
            }

            @Override
            public void onFileDownload(String path, String name, boolean directory) {
                downloadToYoleoFolder(path, name, directory);
            }

            @Override
            public void onFileRename(String path, String name) {
                promptRenameFile(path, name);
            }

            @Override
            public void onFileDelete(String path, String name, boolean directory) {
                confirmDeleteFile(path, name, directory);
            }

            @Override
            public void onFilePaste(String source, String destination, boolean move) {
                runFileAction(move ? "move" : "copy", source, destination, "");
            }

            @Override
            public void onTabChanged(String tabId) {
                mainHandler.removeCallbacks(liveRefreshRunnable);
                if ("files".equals(tabId)) {
                    loadFileDirectory(secureStore.loadSettings().lastFilePath, true);
                }
                scheduleLiveRefresh();
            }

            @Override
            public void onLoadDockerIcon(String iconUrl, ImageView target) {
                loadDockerIcon(iconUrl, target);
            }
        });
        if (!pendingTab.isEmpty()) {
            dashboardView.selectTabById(pendingTab);
            if ("files".equals(pendingTab)) {
                loadFileDirectory(settings.lastFilePath, true);
            }
            pendingTab = "";
        }
        setRootView(dashboardView.getView());
    }

    private void refreshDashboard() {
        refreshDashboard(true, true);
    }

    private void refreshDashboard(boolean showLoading) {
        refreshDashboard(showLoading, false);
    }

    private void refreshDashboard(boolean showLoading, boolean evaluateAlerts) {
        DashboardView target = dashboardView;
        if (target == null || showingSetup || showingOptions) {
            return;
        }
        int generation = ++refreshGeneration;
        if (showLoading) {
            target.showLoading("Lecture des capacités et du cliché de surveillance…");
        }
        String passwordForLogin = transientServerPassword;
        transientServerPassword = "";

        executor.execute(() -> {
            try {
                AppSettings settings = secureStore.loadSettings();
                String p12Password = secureStore.loadP12Password();
                ApiClient client = new ApiClient(settings, secureStore.getP12File(), p12Password);
                dashboardClient = client;
                String token = secureStore.loadAccessToken();
                if (token.isEmpty()) {
                    if (passwordForLogin == null || passwordForLogin.isEmpty()) {
                        throw new IllegalStateException(
                                "Aucun jeton n'est disponible. Ouvre Réglages et saisis le mot de passe serveur.");
                    }
                    token = client.login(settings.username, passwordForLogin);
                    client.me(token);
                    secureStore.saveAccessToken(token);
                }

                String connectionFingerprint = settings.serverUrl + "\u001f" + settings.username;
                if (capabilities == null || !connectionFingerprint.equals(capabilitiesFingerprint)) {
                    capabilities = client.capabilities(token);
                    capabilitiesFingerprint = connectionFingerprint;
                }
                if (!capabilities.optBoolean("monitoring_snapshot", false)) {
                    throw new IllegalStateException("Ce serveur n'annonce pas la fonction monitoring_snapshot.");
                }
                JSONObject snapshot = client.monitoringSnapshot(token);
                lastSnapshot = snapshot;
                if (evaluateAlerts) {
                    MonitoringState.evaluateSuccess(this, snapshot, settings);
                } else {
                    MonitoringState.recordBaseline(this, snapshot);
                }
                mainHandler.post(() -> {
                    if (generation == refreshGeneration && dashboardView == target && !showingSetup && !showingOptions) {
                        try {
                            target.showSnapshot(snapshot);
                        } catch (Throwable renderError) {
                            Log.e(TAG, "Affichage du cliché impossible", renderError);
                            target.showError(
                                    "Erreur d'affichage : " + innermostMessage(renderError));
                        }
                        updateLiveWatch(snapshot);
                    }
                });
            } catch (Exception error) {
                if (error instanceof ApiClient.ApiException && ((ApiClient.ApiException) error).statusCode == 401) {
                    secureStore.clearAccessToken();
                }
                String message = innermostMessage(error);
                mainHandler.post(() -> {
                    if (generation == refreshGeneration && dashboardView == target && !showingSetup && !showingOptions) {
                        target.showError(message);
                    }
                });
            }
        });
    }

    private void loadFileDirectory(String requestedPath, boolean showLoading) {
        DashboardView target = dashboardView;
        if (target == null || showingSetup || showingOptions) {
            return;
        }
        String path = requestedPath == null || requestedPath.trim().isEmpty()
                ? secureStore.loadSettings().lastFilePath
                : requestedPath.trim();
        int generation = ++fileGeneration;
        if (showLoading) {
            target.showFilesLoading(path);
        }
        executor.execute(() -> {
            try {
                ApiClient client = newApiClient();
                JSONObject listing = client.filesList(requireAccessToken(), path);
                String current = listing.optString("current", path);
                secureStore.saveLastFilePath(current);
                mainHandler.post(() -> {
                    if (generation == fileGeneration && dashboardView == target &&
                            !showingSetup && !showingOptions) {
                        target.showFileListing(listing);
                    }
                });
            } catch (Exception error) {
                if (error instanceof ApiClient.ApiException &&
                        ((ApiClient.ApiException) error).statusCode == 401) {
                    secureStore.clearAccessToken();
                }
                String message = innermostMessage(error);
                mainHandler.post(() -> {
                    if (generation == fileGeneration && dashboardView == target) {
                        target.showFileError(message);
                    }
                });
            }
        });
    }

    private void promptCreateFolder(String directory) {
        EditText input = fileNameInput("");
        new AlertDialog.Builder(this)
                .setTitle("Nouveau dossier")
                .setView(input)
                .setNegativeButton("Annuler", null)
                .setPositiveButton("Créer", (dialog, which) -> {
                    String name = input.getText().toString().trim();
                    if (!name.isEmpty()) {
                        runFileAction("mkdir", "", directory, name);
                    }
                })
                .show();
    }

    private void promptRenameFile(String path, String currentName) {
        EditText input = fileNameInput(currentName);
        new AlertDialog.Builder(this)
                .setTitle("Renommer")
                .setView(input)
                .setNegativeButton("Annuler", null)
                .setPositiveButton("Renommer", (dialog, which) -> {
                    String name = input.getText().toString().trim();
                    if (!name.isEmpty() && !name.equals(currentName)) {
                        runFileAction("rename", path, "", name);
                    }
                })
                .show();
        input.selectAll();
    }

    private EditText fileNameInput(String value) {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setText(value == null ? "" : value);
        input.setTextColor(Color.WHITE);
        input.setHintTextColor(Ui.MUTED);
        input.setHint("Nom");
        int horizontal = Ui.dp(this, 18);
        input.setPadding(horizontal, Ui.dp(this, 10), horizontal, Ui.dp(this, 10));
        return input;
    }

    private void confirmDeleteFile(String path, String name, boolean directory) {
        new AlertDialog.Builder(this)
                .setTitle(directory ? "Supprimer le dossier" : "Supprimer le fichier")
                .setMessage("« " + name + " » sera supprimé" +
                        (directory ? " avec tout son contenu." : "."))
                .setNegativeButton("Annuler", null)
                .setPositiveButton("Supprimer", (dialog, which) ->
                        runFileAction("delete", path, "", ""))
                .show();
    }

    private void runFileAction(
            String action,
            String source,
            String destination,
            String name) {
        if (actionInProgress) {
            return;
        }
        actionInProgress = true;
        Toast.makeText(this, "Opération fichier en cours…", Toast.LENGTH_SHORT).show();
        executor.execute(() -> {
            try {
                ApiClient client = newApiClient();
                JSONObject result = client.fileAction(
                        requireAccessToken(), action, source, destination, name);
                String message = result.optString("message", "Opération terminée.");
                mainHandler.post(() -> {
                    actionInProgress = false;
                    if (dashboardView != null) {
                        dashboardView.clearFileClipboard();
                    }
                    Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
                    loadFileDirectory(secureStore.loadSettings().lastFilePath, false);
                });
            } catch (Exception error) {
                handleFileFailure(error);
            }
        });
    }

    private void chooseFilesToUpload(String directory) {
        pendingUploadDirectory = directory;
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        startActivityForResult(intent, REQUEST_FILE_UPLOAD);
    }

    private void uploadSelectedFiles(Intent data) {
        List<Uri> uris = new ArrayList<>();
        ClipData clip = data.getClipData();
        if (clip != null) {
            for (int index = 0; index < clip.getItemCount(); index++) {
                Uri uri = clip.getItemAt(index).getUri();
                if (uri != null) uris.add(uri);
            }
        } else if (data.getData() != null) {
            uris.add(data.getData());
        }
        if (uris.isEmpty()) {
            return;
        }
        String directory = pendingUploadDirectory;
        actionInProgress = true;
        Toast.makeText(this, "Envoi de " + uris.size() + " fichier(s)…", Toast.LENGTH_SHORT).show();
        executor.execute(() -> {
            try {
                ApiClient client = newApiClient();
                String token = requireAccessToken();
                for (Uri uri : uris) {
                    try (InputStream input = getContentResolver().openInputStream(uri)) {
                        client.uploadFile(token, directory, documentName(uri), input);
                    }
                }
                mainHandler.post(() -> {
                    actionInProgress = false;
                    Toast.makeText(this, "Envoi terminé", Toast.LENGTH_SHORT).show();
                    loadFileDirectory(directory, false);
                });
            } catch (Exception error) {
                handleFileFailure(error);
            }
        });
    }

    private void downloadToYoleoFolder(String path, String name, boolean directory) {
        if (path == null || path.isEmpty()) {
            return;
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            Toast.makeText(
                    this,
                    "Le téléchargement automatique nécessite Android 10 ou plus récent.",
                    Toast.LENGTH_LONG).show();
            return;
        }

        String outputName = directory ? name + ".zip" : name;
        String mimeType = directory ? "application/zip" : URLConnection.guessContentTypeFromName(outputName);
        if (mimeType == null || mimeType.isEmpty()) {
            mimeType = "application/octet-stream";
        }
        String finalMimeType = mimeType;
        actionInProgress = true;
        Toast.makeText(this, "Téléchargement de " + outputName + "…", Toast.LENGTH_SHORT).show();
        executor.execute(() -> {
            Uri destination = null;
            try {
                ContentValues values = new ContentValues();
                values.put(MediaStore.MediaColumns.DISPLAY_NAME, outputName);
                values.put(MediaStore.MediaColumns.MIME_TYPE, finalMimeType);
                values.put(
                        MediaStore.MediaColumns.RELATIVE_PATH,
                        Environment.DIRECTORY_DOWNLOADS + "/Yoleo/");
                values.put(MediaStore.MediaColumns.IS_PENDING, 1);
                destination = getContentResolver().insert(
                        MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                        values);
                if (destination == null) {
                    throw new IllegalStateException("Android n'a pas créé le fichier de destination.");
                }
                try (OutputStream output = getContentResolver().openOutputStream(destination, "w")) {
                    if (output == null) {
                        throw new IllegalStateException("Le dossier Download/Yoleo est inaccessible.");
                    }
                    ApiClient client = newApiClient();
                    client.downloadFile(requireAccessToken(), path, directory, output);
                }

                ContentValues completed = new ContentValues();
                completed.put(MediaStore.MediaColumns.IS_PENDING, 0);
                getContentResolver().update(destination, completed, null, null);
                mainHandler.post(() -> {
                    actionInProgress = false;
                    Toast.makeText(
                            this,
                            outputName + " enregistré dans Download/Yoleo",
                            Toast.LENGTH_LONG).show();
                });
            } catch (Exception error) {
                if (destination != null) {
                    try {
                        getContentResolver().delete(destination, null, null);
                    } catch (Exception ignored) {
                        // Le fournisseur de stockage supprimera aussi les fichiers incomplets restés pending.
                    }
                }
                handleFileFailure(error);
            }
        });
    }

    private void handleFileFailure(Exception error) {
        if (error instanceof ApiClient.ApiException &&
                ((ApiClient.ApiException) error).statusCode == 401) {
            secureStore.clearAccessToken();
        }
        String message = innermostMessage(error);
        mainHandler.post(() -> {
            actionInProgress = false;
            Toast.makeText(this, "Échec : " + message, Toast.LENGTH_LONG).show();
        });
    }

    private String documentName(Uri uri) {
        String name = displayName(uri);
        return "certificat.p12".equals(name) ? "fichier" : name;
    }

    private void loadDockerIcon(String iconUrl, ImageView target) {
        ApiClient client = dashboardClient;
        if (client == null || iconUrl == null || iconUrl.trim().isEmpty()) {
            return;
        }
        String expected = iconUrl.trim();
        iconExecutor.execute(() -> {
            try {
                Bitmap bitmap = iconCache.load(expected, client);
                if (bitmap != null) {
                    mainHandler.post(() -> {
                        Object tag = target.getTag();
                        if (tag != null && expected.equals(tag.toString())) {
                            target.setImageBitmap(bitmap);
                        }
                    });
                }
            } catch (Exception ignored) {
                // L'icône Yoleo intégrée reste affichée si la source distante échoue.
            }
        });
    }

    private void confirmDockerAction(String containerId, String containerName, String action) {
        String verb = "start".equals(action) ? "démarrer" :
                "restart".equals(action) ? "redémarrer" : "arrêter";
        if ("start".equals(action)) {
            runDockerAction(containerId, containerName, action);
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("Docker")
                .setMessage("Voulez-vous " + verb + " « " + containerName + " » ?")
                .setNegativeButton("Annuler", null)
                .setPositiveButton("Continuer", (dialog, which) ->
                        runDockerAction(containerId, containerName, action))
                .show();
    }

    private void confirmVmAction(String vmName, String action) {
        if ("start".equals(action)) {
            runVmAction(vmName, action);
            return;
        }
        String message;
        if ("destroy".equals(action)) {
            message = "Forcer l'arrêt de « " + vmName +
                    " » ? Les données non enregistrées dans la VM seront perdues.";
        } else if ("reboot".equals(action)) {
            message = "Redémarrer proprement « " + vmName + " » ?";
        } else {
            message = "Demander l'arrêt propre de « " + vmName + " » ?";
        }
        new AlertDialog.Builder(this)
                .setTitle("Machine virtuelle")
                .setMessage(message)
                .setNegativeButton("Annuler", null)
                .setPositiveButton("Continuer", (dialog, which) -> runVmAction(vmName, action))
                .show();
    }

    private void confirmTaskAction(int taskId, String taskName, String action) {
        if ("start".equals(action)) {
            runTaskAction(taskId, taskName, action);
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("Tâche")
                .setMessage("Arrêter la tâche « " + taskName + " » ?")
                .setNegativeButton("Annuler", null)
                .setPositiveButton("Continuer", (dialog, which) ->
                        runTaskAction(taskId, taskName, action))
                .show();
    }

    private void confirmBackupAction(String filename, String title, String action) {
        if ("start".equals(action)) {
            runBackupAction(filename, title, action);
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("Backup")
                .setMessage("Arrêter de force « " + title + " » ?")
                .setNegativeButton("Annuler", null)
                .setPositiveButton("Continuer", (dialog, which) ->
                        runBackupAction(filename, title, action))
                .show();
    }

    private void runDockerAction(String containerId, String containerName, String action) {
        if (actionInProgress) {
            return;
        }
        actionInProgress = true;
        Toast.makeText(this, "Commande Docker en cours…", Toast.LENGTH_SHORT).show();
        executor.execute(() -> {
            try {
                ApiClient client = newApiClient();
                String token = requireAccessToken();
                JSONObject result = client.dockerAction(token, containerId, action);
                String message = result.optString("message", "Commande Docker exécutée.");
                mainHandler.post(() -> {
                    actionInProgress = false;
                    Toast.makeText(this, message, Toast.LENGTH_LONG).show();
                    refreshDashboard();
                });
            } catch (Exception error) {
                handleActionFailure(error);
            }
        });
    }

    private void runVmAction(String vmName, String action) {
        if (actionInProgress) {
            return;
        }
        actionInProgress = true;
        Toast.makeText(this, "Commande VM en cours…", Toast.LENGTH_SHORT).show();
        executor.execute(() -> {
            try {
                ApiClient client = newApiClient();
                String token = requireAccessToken();
                JSONObject result = client.vmAction(token, vmName, action);
                String message = result.optString("message", "Commande VM exécutée.");
                mainHandler.post(() -> {
                    actionInProgress = false;
                    Toast.makeText(this, message, Toast.LENGTH_LONG).show();
                    refreshDashboard();
                });
            } catch (Exception error) {
                handleActionFailure(error);
            }
        });
    }

    private void runTaskAction(int taskId, String taskName, String action) {
        if (actionInProgress) {
            return;
        }
        actionInProgress = true;
        if (dashboardView != null) {
            dashboardView.showTaskActionPending(taskId, action);
        }
        beginLiveWatch("tasks", String.valueOf(taskId), "stop".equals(action));
        Toast.makeText(this, "Commande de tâche en cours…", Toast.LENGTH_SHORT).show();
        executor.execute(() -> {
            try {
                ApiClient client = newApiClient();
                String token = requireAccessToken();
                JSONObject result = client.taskAction(token, taskId, action);
                String message = result.optString("message", "Commande de tâche exécutée.");
                mainHandler.post(() -> {
                    actionInProgress = false;
                    Toast.makeText(this, message, Toast.LENGTH_LONG).show();
                    refreshDashboard(false);
                });
            } catch (Exception error) {
                stopLiveWatch();
                handleActionFailure(error);
            }
        });
    }

    private void runBackupAction(String filename, String title, String action) {
        if (actionInProgress) {
            return;
        }
        actionInProgress = true;
        if (dashboardView != null) {
            dashboardView.showBackupActionPending(filename, action);
        }
        beginLiveWatch("backup", filename, "stop".equals(action));
        Toast.makeText(this, "Commande Backup en cours…", Toast.LENGTH_SHORT).show();
        executor.execute(() -> {
            try {
                ApiClient client = newApiClient();
                String token = requireAccessToken();
                JSONObject result = client.backupAction(token, filename, action);
                String message = result.optString("message", "Commande Backup exécutée.");
                mainHandler.post(() -> {
                    actionInProgress = false;
                    Toast.makeText(this, message, Toast.LENGTH_LONG).show();
                    refreshDashboard(false);
                });
            } catch (Exception error) {
                stopLiveWatch();
                handleActionFailure(error);
            }
        });
    }

    private void beginLiveWatch(String tab, String key, boolean alreadyObservedRunning) {
        liveWatchTab = tab;
        liveWatchKey = key;
        liveWatchObservedRunning = alreadyObservedRunning;
        liveWatchStartedAt = System.currentTimeMillis();
        mainHandler.removeCallbacks(liveRefreshRunnable);
        scheduleLiveRefresh();
    }

    private void updateLiveWatch(JSONObject snapshot) {
        if (!liveWatchTab.isEmpty() && !liveWatchKey.isEmpty()) {
            boolean running = watchedItemRunning(snapshot);
            if (running) {
                liveWatchObservedRunning = true;
            } else {
                long elapsed = System.currentTimeMillis() - liveWatchStartedAt;
                if (liveWatchObservedRunning || elapsed >= LIVE_START_GRACE_MS) {
                    clearLiveWatchState();
                }
            }
        }
        scheduleLiveRefresh();
    }

    private boolean watchedItemRunning(JSONObject snapshot) {
        if ("tasks".equals(liveWatchTab)) {
            JSONArray tasks = snapshot.optJSONArray("tasks");
            if (tasks != null) {
                int wantedId;
                try {
                    wantedId = Integer.parseInt(liveWatchKey);
                } catch (NumberFormatException ignored) {
                    return false;
                }
                for (int index = 0; index < tasks.length(); index++) {
                    JSONObject task = tasks.optJSONObject(index);
                    if (task != null && task.optInt("id", 0) == wantedId) {
                        return task.optBoolean("running", false);
                    }
                }
            }
            return false;
        }
        if ("backup".equals(liveWatchTab)) {
            JSONObject backup = snapshot.optJSONObject("backup");
            JSONArray scripts = backup == null ? null : backup.optJSONArray("scripts");
            if (scripts != null) {
                for (int index = 0; index < scripts.length(); index++) {
                    JSONObject script = scripts.optJSONObject(index);
                    if (script != null && liveWatchKey.equals(script.optString("filename", ""))) {
                        return script.optBoolean("running", false);
                    }
                }
            }
        }
        return false;
    }

    private void scheduleLiveRefresh() {
        mainHandler.removeCallbacks(liveRefreshRunnable);
        if (shouldAutoRefreshCurrentTab()) {
            mainHandler.postDelayed(liveRefreshRunnable, LIVE_REFRESH_DELAY_MS);
        }
    }

    private boolean shouldAutoRefreshCurrentTab() {
        if (!foreground || dashboardView == null || showingSetup || showingOptions) {
            return false;
        }
        String tab = dashboardView.getCurrentTab();
        if ("home".equals(tab)) {
            return true;
        }
        if (!"tasks".equals(tab) && !"backup".equals(tab)) {
            return false;
        }
        if (tab.equals(liveWatchTab)) {
            return true;
        }
        return anyItemRunning(lastSnapshot, tab);
    }

    private static boolean anyItemRunning(JSONObject snapshot, String tab) {
        if (snapshot == null) {
            return false;
        }
        if ("tasks".equals(tab)) {
            JSONArray tasks = snapshot.optJSONArray("tasks");
            if (tasks != null) {
                for (int index = 0; index < tasks.length(); index++) {
                    JSONObject task = tasks.optJSONObject(index);
                    if (task != null && task.optBoolean("running", false)) {
                        return true;
                    }
                }
            }
        } else if ("backup".equals(tab)) {
            JSONObject backup = snapshot.optJSONObject("backup");
            JSONArray scripts = backup == null ? null : backup.optJSONArray("scripts");
            if (scripts != null) {
                for (int index = 0; index < scripts.length(); index++) {
                    JSONObject script = scripts.optJSONObject(index);
                    if (script != null && script.optBoolean("running", false)) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    private void stopLiveWatch() {
        mainHandler.removeCallbacks(liveRefreshRunnable);
        clearLiveWatchState();
    }

    private void clearLiveWatchState() {
        liveWatchTab = "";
        liveWatchKey = "";
        liveWatchObservedRunning = false;
        liveWatchStartedAt = 0L;
    }


    private void showNotificationSettings() {
        refreshGeneration++;
        showingSetup = false;
        showingOptions = true;
        JSONArray mounts = new JSONArray();
        JSONObject snapshot = lastSnapshot;
        if (snapshot != null) {
            mounts = snapshot.optJSONObject("storage") == null
                    ? new JSONArray()
                    : snapshot.optJSONObject("storage").optJSONArray("mounts");
            if (mounts == null) {
                mounts = new JSONArray();
            }
        }
        settingsView = new SettingsView(
                this,
                secureStore.loadSettings(),
                mounts,
                new SettingsView.Listener() {
                    @Override
                    public void onSave(AppSettings settings) {
                        saveNotificationOptions(settings);
                    }

                    @Override
                    public void onCancel() {
                        showDashboard();
                        showLastSnapshotOrRefresh();
                    }
                });
        setRootView(settingsView.getView());
    }

    private void saveNotificationOptions(AppSettings settings) {
        try {
            if (!secureStore.saveNotificationSettings(settings)) {
                throw new IllegalStateException("Android n'a pas validé l'écriture des préférences.");
            }
        } catch (Throwable error) {
            Log.e(TAG, "Enregistrement des réglages impossible", error);
            Toast.makeText(
                    this,
                    "Réglages non enregistrés : " + innermostMessage(error),
                    Toast.LENGTH_LONG).show();
            return;
        }

        try {
            MonitoringScheduler.reschedule(this);
        } catch (Throwable error) {
            Log.e(TAG, "Reprogrammation de la surveillance impossible", error);
        }

        try {
            Toast.makeText(this, "Réglages enregistrés", Toast.LENGTH_SHORT).show();
            showDashboard();
            // Les transitions doivent toujours être évaluées sur un cliché frais :
            // le cache peut appartenir à l'ancien serveur ou précéder les nouveaux réglages.
            refreshDashboard(true, true);
        } catch (Throwable error) {
            Log.e(TAG, "Retour au tableau de bord impossible après enregistrement", error);
            Toast.makeText(
                    this,
                    "Réglages enregistrés. Ferme puis rouvre Yoleo pour actualiser l'écran.",
                    Toast.LENGTH_LONG).show();
        }
    }

    private void showLastSnapshotOrRefresh() {
        JSONObject snapshot = lastSnapshot;
        if (snapshot != null && dashboardView != null) {
            dashboardView.showSnapshot(snapshot);
            scheduleLiveRefresh();
        } else {
            refreshDashboard();
        }
    }

    private ApiClient newApiClient() throws Exception {
        AppSettings settings = secureStore.loadSettings();
        return new ApiClient(settings, secureStore.getP12File(), secureStore.loadP12Password());
    }

    private String requireAccessToken() {
        String token = secureStore.loadAccessToken();
        if (token.isEmpty()) {
            throw new IllegalStateException(
                    "Jeton absent. Ouvre Réglages et teste à nouveau l'authentification.");
        }
        return token;
    }

    private void handleActionFailure(Exception error) {
        if (error instanceof ApiClient.ApiException &&
                ((ApiClient.ApiException) error).statusCode == 401) {
            secureStore.clearAccessToken();
        }
        String message = innermostMessage(error);
        mainHandler.post(() -> {
            actionInProgress = false;
            Toast.makeText(this, "Échec : " + message, Toast.LENGTH_LONG).show();
            refreshDashboard(false);
        });
    }

    private void requestNotificationPermissionOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) {
            return;
        }
        boolean alreadyAsked = getSharedPreferences("yoleo_runtime", MODE_PRIVATE)
                .getBoolean("notification_permission_asked", false);
        if (!alreadyAsked) {
            getSharedPreferences("yoleo_runtime", MODE_PRIVATE)
                    .edit()
                    .putBoolean("notification_permission_asked", true)
                    .apply();
            requestPermissions(
                    new String[]{Manifest.permission.POST_NOTIFICATIONS},
                    REQUEST_NOTIFICATIONS);
        }
    }

    @SuppressLint("BatteryLife")
    private void requestBackgroundMonitoringPermissionOnce() {
        if (backgroundPermissionDialogShowing || !foreground) {
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
                        PackageManager.PERMISSION_GRANTED) {
            return;
        }
        PowerManager power = getSystemService(PowerManager.class);
        if (power == null || power.isIgnoringBatteryOptimizations(getPackageName())) {
            return;
        }
        boolean alreadyAsked = getSharedPreferences("yoleo_runtime", MODE_PRIVATE)
                .getBoolean("battery_optimization_prompt_067", false);
        if (alreadyAsked) {
            return;
        }

        backgroundPermissionDialogShowing = true;
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("Autoriser la surveillance Yoleo")
                .setMessage(
                        "Android et Samsung peuvent bloquer totalement les contrôles lorsque " +
                                "l'application est fermée. Autorise Yoleo à fonctionner en arrière-plan " +
                                "pour recevoir les alertes du NAS. Aucun service permanent ne sera lancé.")
                .setPositiveButton("Autoriser", (ignored, which) -> {
                    markBackgroundPermissionPrompted();
                    openBatteryOptimizationRequest();
                })
                .setNegativeButton("Plus tard", (ignored, which) -> markBackgroundPermissionPrompted())
                .create();
        dialog.setOnDismissListener(ignored -> backgroundPermissionDialogShowing = false);
        dialog.show();
    }

    private void markBackgroundPermissionPrompted() {
        getSharedPreferences("yoleo_runtime", MODE_PRIVATE)
                .edit()
                .putBoolean("battery_optimization_prompt_067", true)
                .apply();
    }

    @SuppressLint("BatteryLife")
    private void openBatteryOptimizationRequest() {
        Intent request = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                .setData(Uri.parse("package:" + getPackageName()));
        try {
            startActivity(request);
        } catch (Exception unavailable) {
            try {
                startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Exception ignored) {
                Toast.makeText(
                        this,
                        "Ouvre les réglages Batterie de Yoleo et choisis Sans restriction.",
                        Toast.LENGTH_LONG).show();
            }
        }
    }

    private void showStartupFailure(Throwable error) {
        Log.e(TAG, "Démarrage de l'interface impossible", error);
        showingSetup = true;
        LinearLayout fallback = new LinearLayout(this);
        fallback.setOrientation(LinearLayout.VERTICAL);
        fallback.setPadding(
                Ui.dp(this, 22),
                Ui.dp(this, 40),
                Ui.dp(this, 22),
                Ui.dp(this, 30));
        fallback.addView(Ui.title(this, "Yoleo n'a pas pu afficher l'interface", 23));
        fallback.addView(Ui.text(
                this,
                error.getClass().getSimpleName() + " : " + innermostMessage(error),
                15,
                Ui.RED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                this, 0, 14, 0, 0));
        Button configuration = Ui.button(this, "Ouvrir la configuration", true);
        configuration.setOnClickListener(view -> {
            try {
                showSetup(false);
            } catch (Throwable setupError) {
                Toast.makeText(
                        this,
                        "Configuration inaccessible : " + innermostMessage(setupError),
                        Toast.LENGTH_LONG).show();
            }
        });
        fallback.addView(configuration, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                this, 0, 20, 0, 0));
        setRootView(fallback);
    }

    private static String readRequestedTab(Intent intent) {
        if (intent == null) {
            return "";
        }
        String value = intent.getStringExtra(MonitoringNotifier.EXTRA_TAB);
        intent.removeExtra(MonitoringNotifier.EXTRA_TAB);
        return value == null ? "" : value.trim();
    }

    private AppSettings candidateSettings(SetupView.FormData data) {
        AppSettings settings = secureStore.loadSettings();
        settings.serverUrl = data.serverUrl == null ? "" : data.serverUrl.trim();
        settings.username = data.username == null ? "" : data.username.trim();
        settings.p12DisplayName = selectedP12Name.isEmpty()
                ? secureStore.loadSettings().p12DisplayName
                : selectedP12Name;
        settings.configured = true;
        return settings;
    }

    private void importSelectedP12IfNeeded() throws Exception {
        Uri uri = selectedP12Uri;
        if (uri != null) {
            secureStore.importP12(getContentResolver(), uri);
        }
    }

    private String selectedNameFor(AppSettings settings) {
        return selectedP12Name.isEmpty() ? settings.p12DisplayName : selectedP12Name;
    }

    private void postSetup(SetupView target, Runnable action) {
        mainHandler.post(() -> {
            if (showingSetup && setupView == target) {
                action.run();
            }
        });
    }

    private void setRootView(View view) {
        view.setBackgroundColor(Ui.BACKGROUND);
        view.setOnApplyWindowInsetsListener((target, insets) -> {
            target.setPadding(
                    insets.getSystemWindowInsetLeft(),
                    insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(),
                    insets.getSystemWindowInsetBottom());
            return insets;
        });
        setContentView(view);
        view.requestApplyInsets();
    }

    private String displayName(Uri uri) {
        String name = "certificat.p12";
        try (Cursor cursor = getContentResolver().query(uri, null, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (index >= 0) {
                    String value = cursor.getString(index);
                    if (value != null && !value.trim().isEmpty()) {
                        name = value.trim();
                    }
                }
            }
        } catch (Exception ignored) {
            // Le nom d'affichage n'est pas une donnée de sécurité.
        }
        return name;
    }

    private static String serverHost(String rawUrl) {
        try {
            URI uri = new URI(rawUrl == null ? "" : rawUrl.trim());
            String host = uri.getHost();
            return host == null || host.isEmpty() ? "Yoleo NAS" : host;
        } catch (Exception ignored) {
            return "Yoleo NAS";
        }
    }

    private static String innermostMessage(Throwable error) {
        Throwable current = error;
        while (current.getCause() != null) {
            current = current.getCause();
        }
        String message = current.getMessage();
        return message == null || message.trim().isEmpty()
                ? current.getClass().getSimpleName()
                : message.trim();
    }
}
