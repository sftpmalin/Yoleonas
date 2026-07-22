package com.sftpmalin.yoleo.ui;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.graphics.Typeface;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.HorizontalScrollView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.ScrollView;
import android.widget.TextView;

import com.sftpmalin.yoleo.R;
import com.sftpmalin.yoleo.data.AppSettings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

@SuppressLint("SetTextI18n")
public final class DashboardView {
    public interface Listener {
        void onRefresh();

        void onSettings();

        void onNotificationSettings();

        void onDockerAction(String containerId, String containerName, String action);

        void onVmAction(String vmName, String action);

        void onTaskAction(int taskId, String taskName, String action);

        void onBackupAction(String filename, String title, String action);

        void onFileNavigate(String path);

        void onFileCreateFolder(String directory);

        void onFileUpload(String directory);

        void onFileDownload(String path, String name, boolean directory);

        void onFileRename(String path, String name);

        void onFileDelete(String path, String name, boolean directory);

        void onFilePaste(String source, String destination, boolean move);

        void onTabChanged(String tabId);

        void onLoadDockerIcon(String iconUrl, ImageView target);
    }

    private static final String HOME = "home";
    private static final String DOCKER = "docker";
    private static final String STORAGE = "storage";
    private static final String TASKS = "tasks";
    private static final String VMS = "vms";
    private static final String BACKUP = "backup";
    private static final String FILES = "files";
    private final Activity activity;
    private final Listener listener;
    private final AppSettings settings;
    private final LinearLayout root;
    private final LinearLayout content;
    private final LinearLayout fileToolbar;
    private final TextView connectionStatus;
    private final Map<String, NavItem> navigation = new LinkedHashMap<>();
    private JSONObject monitoring;
    private String currentTab = HOME;
    private String serverLabel = "Yoleo NAS";
    private String backupFilter = "backup";
    private JSONObject fileListing;
    private String fileClipboardPath = "";
    private String fileClipboardName = "";
    private boolean fileClipboardMove;

    public DashboardView(
            Activity activity,
            String serverLabel,
            AppSettings settings,
            Listener listener) {
        this.activity = activity;
        this.listener = listener;
        this.settings = settings.copy();
        if (serverLabel != null && !serverLabel.trim().isEmpty()) {
            this.serverLabel = serverLabel.trim();
        }

        root = new LinearLayout(activity);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Ui.BACKGROUND);

        LinearLayout header = new LinearLayout(activity);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(Ui.dp(activity, 16), Ui.dp(activity, 7), Ui.dp(activity, 10), Ui.dp(activity, 7));
        header.setBackgroundColor(Ui.SURFACE);
        root.addView(header, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        ImageView logo = new ImageView(activity);
        logo.setImageResource(R.drawable.logo);
        logo.setContentDescription("Logo Yoleo");
        logo.setScaleType(ImageView.ScaleType.CENTER_CROP);
        header.addView(logo, new LinearLayout.LayoutParams(Ui.dp(activity, 42), Ui.dp(activity, 42)));

        LinearLayout titles = new LinearLayout(activity);
        titles.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams titlesParams = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
        titlesParams.setMargins(Ui.dp(activity, 12), 0, Ui.dp(activity, 8), 0);
        header.addView(titles, titlesParams);
        LinearLayout titleRow = new LinearLayout(activity);
        titleRow.setOrientation(LinearLayout.HORIZONTAL);
        titleRow.setGravity(Gravity.CENTER_VERTICAL);
        titleRow.addView(Ui.title(activity, "Yoleo NAS", 20));
        connectionStatus = Ui.text(activity, "●", 17, Ui.AMBER);
        connectionStatus.setContentDescription("Connexion en cours");
        titleRow.addView(connectionStatus, Ui.margins(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 8, 0, 0, 0));
        titles.addView(titleRow);
        titles.addView(Ui.text(activity, this.serverLabel, 12, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 1, 0, 0));

        Button menuButton = Ui.button(activity, "⋮", false);
        menuButton.setTextSize(25);
        menuButton.setContentDescription("Menu Yoleo");
        menuButton.setPadding(0, 0, 0, 0);
        menuButton.setOnClickListener(view -> showTopMenu(menuButton));
        header.addView(menuButton, new LinearLayout.LayoutParams(Ui.dp(activity, 46), Ui.dp(activity, 42)));

        fileToolbar = new LinearLayout(activity);
        fileToolbar.setOrientation(LinearLayout.HORIZONTAL);
        fileToolbar.setPadding(
                Ui.dp(activity, 8), Ui.dp(activity, 6),
                Ui.dp(activity, 8), Ui.dp(activity, 6));
        fileToolbar.setBackgroundColor(Ui.SURFACE);
        fileToolbar.setVisibility(View.GONE);
        root.addView(fileToolbar, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        ScrollView scroll = new ScrollView(activity);
        scroll.setFillViewport(true);
        content = new LinearLayout(activity);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(Ui.dp(activity, 18), Ui.dp(activity, 18), Ui.dp(activity, 18), Ui.dp(activity, 26));
        scroll.addView(content, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT));
        root.addView(scroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1));

        root.addView(Ui.divider(activity), new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                Ui.dp(activity, 1)));

        HorizontalScrollView navScroll = new HorizontalScrollView(activity);
        navScroll.setHorizontalScrollBarEnabled(false);
        navScroll.setFillViewport(false);
        navScroll.setBackgroundColor(Ui.SURFACE);
        LinearLayout nav = new LinearLayout(activity);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setPadding(Ui.dp(activity, 6), Ui.dp(activity, 5), Ui.dp(activity, 6), Ui.dp(activity, 6));
        navScroll.addView(nav, new HorizontalScrollView.LayoutParams(
                HorizontalScrollView.LayoutParams.WRAP_CONTENT,
                HorizontalScrollView.LayoutParams.WRAP_CONTENT));
        root.addView(navScroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        for (String id : this.settings.navigationOrder) {
            addConfiguredNavigation(nav, id);
        }
        updateNavigation();
    }

    public View getView() {
        return root;
    }

    public String getCurrentTab() {
        return currentTab;
    }

    public void selectHome() {
        selectTab(HOME);
    }

    public void selectTabById(String id) {
        if (HOME.equals(id) || DOCKER.equals(id) || STORAGE.equals(id) ||
                TASKS.equals(id) || VMS.equals(id) || BACKUP.equals(id) || FILES.equals(id)) {
            selectTab(id);
        }
    }

    public void showLoading(String message) {
        connectionStatus.setText("●");
        connectionStatus.setTextColor(Ui.AMBER);
        connectionStatus.setContentDescription("Connexion en cours");
        content.removeAllViews();
        LinearLayout loading = Ui.card(activity);
        loading.addView(Ui.title(activity, "Actualisation", 20));
        loading.addView(Ui.text(activity, message, 15, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 8, 0, 0));
        content.addView(loading, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
    }

    public void showError(String message) {
        connectionStatus.setText("●");
        connectionStatus.setTextColor(Ui.RED);
        connectionStatus.setContentDescription("NAS hors ligne");
        content.removeAllViews();

        LinearLayout error = Ui.card(activity);
        error.setBackground(Ui.rounded(Ui.SURFACE, Ui.RED, 18, activity));
        error.addView(Ui.title(activity, "Impossible de joindre le NAS", 21));
        error.addView(Ui.text(activity, safe(message, "Erreur de connexion inconnue."), 15, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 10, 0, 0));
        Button retry = Ui.button(activity, "Réessayer", true);
        retry.setOnClickListener(view -> listener.onRefresh());
        error.addView(retry, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 16, 0, 0));
        Button settings = Ui.button(activity, "Modifier la configuration", false);
        settings.setOnClickListener(view -> listener.onSettings());
        error.addView(settings, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 9, 0, 0));
        content.addView(error, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
    }

    public void showSnapshot(JSONObject monitoring) {
        this.monitoring = monitoring;
        connectionStatus.setText("●");
        connectionStatus.setTextColor(Ui.GREEN);
        connectionStatus.setContentDescription("NAS en ligne");
        renderCurrentTab();
    }

    public void showFilesLoading(String path) {
        if (!FILES.equals(currentTab)) {
            return;
        }
        content.removeAllViews();
        renderFileToolbar();
        content.addView(Ui.title(activity, "Gestionnaire de fichiers", 23));
        addEmpty("Lecture en cours…");
    }

    public void showFileListing(JSONObject listing) {
        fileListing = listing;
        if (FILES.equals(currentTab)) {
            content.removeAllViews();
            renderFileToolbar();
            renderFiles();
        }
    }

    public void showFileError(String message) {
        if (!FILES.equals(currentTab)) {
            return;
        }
        content.removeAllViews();
        renderFileToolbar();
        content.addView(Ui.title(activity, "Gestionnaire de fichiers", 23));
        LinearLayout card = Ui.card(activity);
        card.setBackground(Ui.rounded(Ui.SURFACE, Ui.RED, 18, activity));
        card.addView(Ui.text(activity, safe(message, "Lecture du dossier impossible."), 14, Ui.RED));
        Button retry = Ui.button(activity, "Réessayer", true);
        retry.setOnClickListener(view -> listener.onFileNavigate(currentFilePath()));
        card.addView(retry, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 13, 0, 0));
        content.addView(card, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 16, 0, 0));
    }

    public void clearFileClipboard() {
        fileClipboardPath = "";
        fileClipboardName = "";
        fileClipboardMove = false;
        if (FILES.equals(currentTab)) {
            renderFiles();
        }
    }

    private void showTopMenu(View anchor) {
        PopupMenu menu = new PopupMenu(activity, anchor);
        menu.getMenu().add(0, 1, 0, "Actualiser");
        menu.getMenu().add(0, 2, 1, "Authentification");
        menu.getMenu().add(0, 3, 2, "Réglages");
        menu.setOnMenuItemClickListener(item -> {
            if (item.getItemId() == 1) {
                listener.onRefresh();
            } else if (item.getItemId() == 2) {
                listener.onSettings();
            } else if (item.getItemId() == 3) {
                listener.onNotificationSettings();
            }
            return true;
        });
        menu.show();
    }

    private void addNavigation(LinearLayout parent, String id, String label, int iconResource) {
        LinearLayout item = new LinearLayout(activity);
        item.setOrientation(LinearLayout.VERTICAL);
        item.setGravity(Gravity.CENTER);
        item.setPadding(Ui.dp(activity, 4), Ui.dp(activity, 4), Ui.dp(activity, 4), Ui.dp(activity, 3));
        item.setClickable(true);
        item.setFocusable(true);
        item.setContentDescription(label);

        ImageView icon = new ImageView(activity);
        icon.setImageResource(iconResource);
        icon.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
        item.addView(icon, new LinearLayout.LayoutParams(Ui.dp(activity, 36), Ui.dp(activity, 36)));

        TextView text = Ui.text(activity, label, 10, Ui.MUTED);
        text.setGravity(Gravity.CENTER);
        text.setMaxLines(1);
        item.addView(text, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 1, 0, 0));

        item.setOnClickListener(view -> {
            selectTab(id);
            listener.onTabChanged(id);
        });
        navigation.put(id, new NavItem(item, icon, text));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                Ui.dp(activity, 74), Ui.dp(activity, 66));
        params.setMargins(Ui.dp(activity, 2), 0, Ui.dp(activity, 2), 0);
        parent.addView(item, params);
    }

    private void addConfiguredNavigation(LinearLayout parent, String id) {
        if (HOME.equals(id)) {
            addNavigation(parent, HOME, "Accueil", R.drawable.nav_home);
        } else if (DOCKER.equals(id)) {
            addNavigation(parent, DOCKER, "Docker", R.drawable.nav_docker);
        } else if (STORAGE.equals(id)) {
            addNavigation(parent, STORAGE, "Stockage", R.drawable.nav_storage);
        } else if (TASKS.equals(id)) {
            addNavigation(parent, TASKS, "Tâches", R.drawable.nav_tasks);
        } else if (BACKUP.equals(id)) {
            addNavigation(parent, BACKUP, "Backup", R.drawable.nav_backup);
        } else if (FILES.equals(id)) {
            addNavigation(parent, FILES, "Fichiers", R.drawable.nav_files);
        } else if (VMS.equals(id)) {
            addNavigation(parent, VMS, "VM", R.drawable.nav_vm);
        }
    }

    private void selectTab(String id) {
        currentTab = id;
        updateNavigation();
        renderFileToolbar();
        if (monitoring == null) {
            showLoading("Chargement des données du NAS…");
        } else {
            renderCurrentTab();
        }
    }

    private void updateNavigation() {
        for (Map.Entry<String, NavItem> entry : navigation.entrySet()) {
            boolean active = entry.getKey().equals(currentTab);
            NavItem item = entry.getValue();
            item.label.setTextColor(active ? Ui.CYAN : Ui.MUTED);
            item.label.setTypeface(Typeface.DEFAULT, active ? Typeface.BOLD : Typeface.NORMAL);
            item.icon.setAlpha(active ? 1f : 0.72f);
            item.root.setBackground(Ui.rounded(
                    active ? Ui.SURFACE_ALT : Ui.SURFACE,
                    active ? Ui.BORDER : Ui.SURFACE,
                    13,
                    activity));
        }
    }

    private void renderCurrentTab() {
        content.removeAllViews();
        renderFileToolbar();
        if (monitoring == null) {
            return;
        }
        switch (currentTab) {
            case DOCKER:
                renderDocker();
                break;
            case STORAGE:
                renderStorage();
                break;
            case TASKS:
                renderTasks();
                break;
            case VMS:
                renderVms();
                break;
            case BACKUP:
                renderBackup();
                break;
            case FILES:
                renderFiles();
                break;
            default:
                renderHome();
                break;
        }
    }

    private void renderHome() {
        JSONObject system = monitoring.optJSONObject("system");
        JSONObject storage = monitoring.optJSONObject("storage");
        JSONObject mainStorage = storage == null ? null : storage.optJSONObject("main");
        JSONArray temperatures = system == null ? null : system.optJSONArray("temperatures");
        JSONObject fanData = system == null ? null : system.optJSONObject("fans");
        JSONArray fans = fanData == null ? null : fanData.optJSONArray("rows");
        JSONArray gpus = system == null ? null : system.optJSONArray("gpus");
        JSONObject host = system == null ? null : system.optJSONObject("host");
        JSONObject network = system == null ? null : system.optJSONObject("network");
        JSONObject services = system == null ? null : system.optJSONObject("services");
        JSONObject docker = monitoring.optJSONObject("docker");
        JSONObject dockerStats = docker == null ? null : docker.optJSONObject("stats");
        JSONObject dockerService = docker == null ? null : docker.optJSONObject("service");
        JSONObject samba = monitoring.optJSONObject("samba");
        JSONObject build = monitoring.optJSONObject("build");
        JSONObject vms = monitoring.optJSONObject("vms");
        JSONObject vmSummary = vms == null ? null : vms.optJSONObject("summary");
        JSONArray tasks = monitoring.optJSONArray("tasks");

        String generatedAt = monitoring.optString("generated_at", "");
        content.addView(Ui.title(activity, "Vue d'ensemble", 27));
        content.addView(Ui.text(
                activity,
                generatedAt.isEmpty() ? "Données reçues" : "Dernier cliché : " + frenchTime(generatedAt),
                14,
                Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                 LinearLayout.LayoutParams.WRAP_CONTENT,
                 activity, 0, 5, 0, 0));

        for (String itemId : settings.homeOrder) {
            if (!home(itemId)) {
                continue;
            }
            switch (itemId) {
                case "cpu":
                    addHomeCard(metricCard(
                            "CPU",
                            system == null ? 0 : system.optDouble("cpu_percent", 0),
                            "Utilisation du processeur"));
                    break;
                case "ram":
                    addHomeCard(metricCard(
                            "RAM",
                            system == null ? 0 : system.optDouble("ram_percent", 0),
                            "Mémoire utilisée"));
                    break;
                case "storage":
                    addHomeCard(metricCard(
                            "Stockage principal",
                            mainStorage == null ? 0 : mainStorage.optDouble("percent", 0),
                            mainStorage == null ? "/mnt/user" :
                                    safe(mainStorage.optString("used", ""), "—") + " / " +
                                            safe(mainStorage.optString("total", ""), "—")));
                    break;
                case "temperatures":
                    if (temperatures != null && temperatures.length() > 0) {
                        JSONObject cpuTemperature = selectTemperature(temperatures, true);
                        JSONObject boardTemperature = selectTemperature(temperatures, false);
                        if (cpuTemperature != null || boardTemperature != null) {
                            addHardwareCaption("Températures");
                            addTemperatureRow(cpuTemperature, boardTemperature);
                        }
                    }
                    break;
                case "fans":
                    if (fans != null && fans.length() > 0) {
                        addHardwareCaption("Ventilateurs");
                        addFanGrid(fans);
                    }
                    break;
                case "gpus":
                    if (gpus != null && gpus.length() > 0) {
                        addHardwareCaption("Cartes graphiques");
                        for (int index = 0; index < gpus.length(); index++) {
                            JSONObject item = gpus.optJSONObject(index);
                            if (item == null) continue;
                            String detail = safe(item.optString("label", "GPU"), "GPU") +
                                    " · " + valueWithUnit(item.optString("temp", ""), "°C") +
                                    " · " + valueWithUnit(item.optString("fan", ""), "% ventilateur") +
                                    " · " + valueWithUnit(item.optString("power", ""), "W");
                            addListCard(
                                    safe(item.optString("name", "Carte graphique"), "Carte graphique"),
                                    detail,
                                    valueWithUnit(item.optString("load", ""), "%"),
                                    Ui.CYAN);
                        }
                    }
                    break;
                case "host":
                    if (host != null) {
                        addListCard(
                                safe(host.optString("hostname", "Serveur Yoleo"), "Serveur Yoleo"),
                                safe(host.optString("os", ""), "Système Linux") + " · " +
                                        safe(host.optString("cpu_model", ""), "CPU non renseigné"),
                                safe(host.optString("kernel", ""), "Linux"),
                                Ui.CYAN);
                    }
                    break;
                case "network":
                    if (network != null) {
                        addListCard(
                                "Réseau local",
                                safe(network.optString("iface", "Interface"), "Interface") +
                                        " · passerelle " + safe(network.optString("gateway", ""), "—") +
                                        " · " + safe(network.optString("speed", ""), "vitesse inconnue"),
                                safe(network.optString("ip", ""), "IP inconnue"),
                                "up".equalsIgnoreCase(network.optString("state")) ? Ui.GREEN : Ui.CYAN);
                    }
                    break;
                case "uptime":
                    addListCard(
                            "Durée d'activité",
                            host == null ? "Depuis le dernier démarrage" :
                                    "Démarré le " + safe(host.optString("boot_time", ""), "—"),
                            system == null ? "—" : safe(system.optString("uptime", ""), "—"),
                            Ui.GREEN);
                    break;
                case "services":
                    if (services != null) {
                        int failed = services.optInt("failed", 0);
                        addListCard(
                                "Services systemd",
                                services.optInt("active", 0) + " actif(s) sur " + services.optInt("total", 0) +
                                        " · " + services.optInt("enabled", 0) + " activé(s) au démarrage",
                                failed == 0 ? "Aucun échec" : failed + " en échec",
                                failed == 0 ? Ui.GREEN : Ui.RED);
                    }
                    break;
                case "docker":
                    boolean dockerOk = dockerService != null && dockerService.optBoolean("active", false);
                    String dockerDetail = dockerStats == null ? "Indisponible" :
                            dockerStats.optInt("running", 0) + " / " + dockerStats.optInt("total", 0) + " actifs";
                    addHomeCard(summaryCard(
                            "Docker", dockerOk ? "Actif" : "Arrêté", dockerDetail, dockerOk));
                    break;
                case "samba":
                    boolean sambaOk = samba != null && samba.optBoolean("ok", false);
                    addHomeCard(summaryCard(
                            "Partages", sambaOk ? "Disponibles" : "À vérifier", "Samba / WSDD", sambaOk));
                    break;
                case "tasks":
                    int failedTasks = countFailedTasks(tasks);
                    addHomeCard(summaryCard(
                            "Tâches",
                            failedTasks == 0 ? "Aucune erreur" : failedTasks + " en erreur",
                            (tasks == null ? 0 : tasks.length()) + " tâche(s)",
                            failedTasks == 0));
                    break;
                case "vms":
                    int runningVms = vmSummary == null ? 0 : vmSummary.optInt("running", 0);
                    int totalVms = vmSummary == null ? 0 : vmSummary.optInt("total", 0);
                    boolean vmAvailable = vms != null && vms.optBoolean("available", false);
                    addHomeCard(summaryCard(
                            "Machines virtuelles",
                            vmAvailable ? runningVms + " active(s)" : "Indisponibles",
                            totalVms + " VM déclarée(s)",
                            vmAvailable));
                    break;
                case "build":
                    int toBuild = build == null ? 0 : build.optInt("to_build", 0);
                    int toPush = build == null ? 0 : build.optInt("to_push", 0);
                    addHomeCard(summaryCard(
                            "Build",
                            toBuild + " à builder",
                            toPush + " à envoyer",
                            toBuild == 0 && toPush == 0));
                    break;
                default:
                    break;
            }
        }

        JSONArray errors = monitoring.optJSONArray("errors");
        if (errors != null && errors.length() > 0) {
            addSectionTitle("Informations partielles");
            for (int index = 0; index < errors.length(); index++) {
                JSONObject error = errors.optJSONObject(index);
                if (error != null) {
                    addListCard(
                            safe(error.optString("section", "Section"), "Section"),
                            safe(error.optString("message", "Indisponible"), "Indisponible"),
                            "À vérifier",
                            Ui.AMBER);
                }
            }
        }
    }

    private void addHomeCard(View card) {
        content.addView(card, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 8, 0, 0));
    }

    private void renderDocker() {
        addPageHeader("Docker", "Icônes en cache et commandes essentielles");
        JSONObject docker = monitoring.optJSONObject("docker");
        if (docker == null || !docker.optBoolean("available", false)) {
            addEmpty("Docker est indisponible dans le dernier cliché.");
            return;
        }
        JSONObject service = docker.optJSONObject("service");
        boolean active = service != null && service.optBoolean("active", false);
        content.addView(summaryCard(
                "Service Docker",
                active ? "Actif" : "Arrêté",
                service == null ? "État inconnu" : safe(service.optString("label", ""), "État inconnu"),
                active), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 16, 0, 0));

        JSONObject stats = docker.optJSONObject("stats");
        content.addView(Ui.text(
                activity,
                stats == null
                        ? "Conteneurs"
                        : stats.optInt("running", 0) + " actifs sur " + stats.optInt("total", 0),
                14,
                Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 16, 0, 4));
        JSONArray containers = docker.optJSONArray("containers");
        if (containers == null || containers.length() == 0) {
            addEmpty("Aucun conteneur n'a été reçu.");
            return;
        }
        Map<String, JSONArray> stacks = new LinkedHashMap<>();
        for (int index = 0; index < containers.length(); index++) {
            JSONObject container = containers.optJSONObject(index);
            if (container != null) {
                String stack = safe(container.optString("stack", "Sans stack"), "Sans stack");
                JSONArray group = stacks.get(stack);
                if (group == null) {
                    group = new JSONArray();
                    stacks.put(stack, group);
                }
                group.put(container);
            }
        }
        for (Map.Entry<String, JSONArray> entry : stacks.entrySet()) {
            addSectionTitle(entry.getKey());
            JSONArray group = entry.getValue();
            for (int index = 0; index < group.length(); index++) {
                JSONObject container = group.optJSONObject(index);
                if (container != null) {
                    addDockerCard(container);
                }
            }
        }
    }

    private void addDockerCard(JSONObject container) {
        String id = container.optString("id", "");
        String name = safe(container.optString("name", "Conteneur"), "Conteneur");
        String stack = safe(container.optString("stack", "Sans stack"), "Sans stack");
        String state = container.optString("state", "unknown");
        String iconUrl = container.optString("icon", "");
        boolean running = "running".equalsIgnoreCase(state);
        boolean restarting = "restarting".equalsIgnoreCase(state) ||
                state.toLowerCase(Locale.ROOT).contains("restart");

        LinearLayout card = Ui.card(activity);
        LinearLayout header = new LinearLayout(activity);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        card.addView(header, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        ImageView icon = new ImageView(activity);
        icon.setImageResource(R.drawable.nav_docker);
        icon.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
        icon.setContentDescription("Icône " + name);
        icon.setTag(iconUrl);
        header.addView(icon, new LinearLayout.LayoutParams(Ui.dp(activity, 54), Ui.dp(activity, 54)));
        if (!iconUrl.trim().isEmpty()) {
            listener.onLoadDockerIcon(iconUrl, icon);
        }

        LinearLayout texts = new LinearLayout(activity);
        texts.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams textParams = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
        textParams.setMargins(Ui.dp(activity, 12), 0, Ui.dp(activity, 8), 0);
        header.addView(texts, textParams);
        texts.addView(Ui.title(activity, name, 17));
        texts.addView(Ui.text(activity, stack, 12, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 4, 0, 0));
        header.addView(statusDot(restarting ? Ui.AMBER : running ? Ui.GREEN : Ui.RED, state));

        if (!id.isEmpty()) {
            LinearLayout actions = new LinearLayout(activity);
            actions.setOrientation(LinearLayout.HORIZONTAL);
            card.addView(actions, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 13, 0, 0));
            Button start = commandButton("▶", "Démarrer " + name);
            start.setOnClickListener(view -> listener.onDockerAction(id, name, "start"));
            setLogicalEnabled(start, !running && !restarting);
            actions.addView(start, Ui.weighted(activity, 1, 4));

            Button restart = commandButton("↻", "Redémarrer " + name);
            restart.setOnClickListener(view -> listener.onDockerAction(id, name, "restart"));
            setLogicalEnabled(restart, running);
            actions.addView(restart, Ui.weighted(activity, 1, 4));

            Button stop = commandButton("■", "Arrêter " + name);
            stop.setOnClickListener(view -> listener.onDockerAction(id, name, "stop"));
            setLogicalEnabled(stop, running || restarting);
            actions.addView(stop, Ui.weighted(activity, 1, 4));
        }
        content.addView(card, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 0, 0, 9));
    }

    private void renderStorage() {
        addPageHeader("Stockage", "Baies, volumes et vrais points de montage");
        JSONObject storage = monitoring.optJSONObject("storage");
        if (storage == null) {
            addEmpty("Aucune donnée de stockage n'a été reçue.");
            return;
        }
        JSONArray mounts = storage.optJSONArray("mounts");
        addSectionTitle("Points de montage");
        if (mounts == null || mounts.length() == 0) {
            addEmpty("Aucun point de montage détecté.");
            return;
        }
        int visible = 0;
        for (int index = 0; index < mounts.length(); index++) {
            JSONObject mount = mounts.optJSONObject(index);
            if (mount == null) {
                continue;
            }
            String path = mount.optString("path", "").trim();
            if (settings.mountSelectionConfigured && !settings.displayedMountPaths.contains(path)) {
                continue;
            }
            visible++;
            boolean mounted = mount.optBoolean("is_mount", false);
            String label = safe(mount.optString("label", ""), path);
            String details = path + " — " +
                    formatPercent(mount.optDouble("percent", 0));
            addListCard(
                    label,
                    details,
                    mounted ? "Monté" : safe(mount.optString("status_label", ""), "Non monté"),
                    mounted ? Ui.GREEN : Ui.RED);
        }
        if (visible == 0) {
            addEmpty("Aucun disque ou point de montage utile n'a été reçu.");
        }
    }

    private void renderTasks() {
        addPageHeader("Tâches", "Dernier état connu des automatisations");
        JSONArray tasks = monitoring.optJSONArray("tasks");
        if (tasks == null || tasks.length() == 0) {
            addEmpty("Aucune tâche n'a été reçue.");
            return;
        }
        for (int index = 0; index < tasks.length(); index++) {
            JSONObject task = tasks.optJSONObject(index);
            if (task == null) {
                continue;
            }
            addTaskCard(task);
        }
    }

    private void addTaskCard(JSONObject task) {
        int id = task.optInt("id", 0);
        String name = safe(task.optString("title", "Tâche"), "Tâche");
        boolean running = task.optBoolean("running", false);
        String result = task.optString("result", "");
        String status = running ? "En cours" :
                safe(result, task.optString("status", "État inconnu"));
        boolean ok = running || !isFailure(status);
        String detail = safe(task.optString("last_run", ""), "Jamais exécutée");

        LinearLayout card = Ui.card(activity);
        LinearLayout heading = new LinearLayout(activity);
        heading.setOrientation(LinearLayout.HORIZONTAL);
        heading.setGravity(Gravity.CENTER_VERTICAL);
        card.addView(heading);
        LinearLayout texts = new LinearLayout(activity);
        texts.setOrientation(LinearLayout.VERTICAL);
        heading.addView(texts, new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        texts.addView(Ui.title(activity, name, 16));
        texts.addView(Ui.text(activity, detail, 12, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 4, 8, 0));
        heading.addView(badge(status, running ? Ui.AMBER : ok ? Ui.GREEN : Ui.RED));

        if (id > 0) {
            LinearLayout actions = new LinearLayout(activity);
            actions.setOrientation(LinearLayout.HORIZONTAL);
            card.addView(actions, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 12, 0, 0));
            Button start = commandButton("▶", "Démarrer " + name);
            start.setOnClickListener(view -> listener.onTaskAction(id, name, "start"));
            setLogicalEnabled(start, !running);
            actions.addView(start, Ui.weighted(activity, 1, 4));
            Button stop = commandButton("■", "Arrêter " + name);
            stop.setOnClickListener(view -> listener.onTaskAction(id, name, "stop"));
            setLogicalEnabled(stop, running);
            actions.addView(stop, Ui.weighted(activity, 1, 4));
        }
        content.addView(card, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 0, 0, 9));
    }

    public void showTaskActionPending(int taskId, String action) {
        JSONArray tasks = monitoring == null ? null : monitoring.optJSONArray("tasks");
        if (tasks == null) {
            return;
        }
        for (int index = 0; index < tasks.length(); index++) {
            JSONObject task = tasks.optJSONObject(index);
            if (task == null || task.optInt("id", 0) != taskId) {
                continue;
            }
            try {
                if ("start".equals(action)) {
                    task.put("running", true);
                    task.put("status", "En cours");
                    task.put("result", "En cours");
                } else {
                    task.put("status", "Arrêt demandé");
                    task.put("result", "En cours");
                }
            } catch (Exception ignored) {
                return;
            }
            if (TASKS.equals(currentTab)) {
                renderCurrentTab();
            }
            return;
        }
    }

    private void renderVms() {
        addPageHeader("Machines virtuelles", "Pilotage simple, sans création ni modification");
        JSONObject vms = monitoring.optJSONObject("vms");
        if (vms == null || !vms.optBoolean("available", false)) {
            addEmpty("Les machines virtuelles sont indisponibles dans le dernier cliché.");
            return;
        }
        JSONObject summary = vms.optJSONObject("summary");
        if (summary != null) {
            content.addView(summaryCard(
                    "Libvirt",
                    summary.optInt("running", 0) + " VM active(s)",
                    summary.optInt("total", 0) + " VM déclarée(s)",
                    true), Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 16, 0, 0));
        }
        addSectionTitle("Machines");
        JSONArray machines = vms.optJSONArray("machines");
        if (machines == null || machines.length() == 0) {
            addEmpty("Aucune machine virtuelle n'est déclarée.");
            return;
        }
        for (int index = 0; index < machines.length(); index++) {
            JSONObject machine = machines.optJSONObject(index);
            if (machine != null) {
                addVmCard(machine);
            }
        }
    }

    private void addVmCard(JSONObject machine) {
        String name = safe(machine.optString("name", "Machine virtuelle"), "Machine virtuelle");
        String state = safe(machine.optString("state", "État inconnu"), "État inconnu");
        String stateClass = machine.optString("state_class", "unknown");
        boolean running = "running".equalsIgnoreCase(stateClass);
        boolean stopped = "stopped".equalsIgnoreCase(stateClass);

        LinearLayout card = Ui.card(activity);
        LinearLayout header = new LinearLayout(activity);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        card.addView(header);

        ImageView icon = new ImageView(activity);
        icon.setImageResource(R.drawable.nav_vm);
        icon.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
        icon.setContentDescription("Machine virtuelle " + name);
        header.addView(icon, new LinearLayout.LayoutParams(Ui.dp(activity, 54), Ui.dp(activity, 54)));

        LinearLayout texts = new LinearLayout(activity);
        texts.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams textParams = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
        textParams.setMargins(Ui.dp(activity, 12), 0, Ui.dp(activity, 8), 0);
        header.addView(texts, textParams);
        texts.addView(Ui.title(activity, name, 17));
        texts.addView(Ui.text(activity, state, 12, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 4, 0, 0));
        header.addView(statusDot(running ? Ui.GREEN : stopped ? Ui.RED : Ui.AMBER, state));

        LinearLayout actions = new LinearLayout(activity);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        card.addView(actions, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 13, 0, 0));
        Button start = Ui.button(activity, "▶ Démarrer", false);
        start.setTextSize(12);
        start.setOnClickListener(view -> listener.onVmAction(name, "start"));
        setLogicalEnabled(start, stopped);
        actions.addView(start, Ui.weighted(activity, 1, 3));

        Button shutdown = Ui.button(activity, "Arrêt", false);
        shutdown.setTextSize(12);
        shutdown.setOnClickListener(view -> listener.onVmAction(name, "shutdown"));
        setLogicalEnabled(shutdown, running);
        actions.addView(shutdown, Ui.weighted(activity, 1, 3));

        Button destroy = Ui.button(activity, "■ Stop", false);
        destroy.setTextSize(12);
        destroy.setOnClickListener(view -> listener.onVmAction(name, "destroy"));
        setLogicalEnabled(destroy, !stopped);
        actions.addView(destroy, Ui.weighted(activity, 1, 3));
        content.addView(card, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 0, 0, 9));
    }

    private void renderFiles() {
        content.addView(Ui.title(activity, "Gestionnaire de fichiers", 23));
        if (fileListing == null) {
            addEmpty("Chargement du dernier dossier…");
            return;
        }
        String current = currentFilePath();
        content.addView(Ui.text(activity, current, 12, Ui.CYAN), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 7, 0, 0));
        renderFileItems();
    }

    private void renderFileToolbar() {
        fileToolbar.removeAllViews();
        if (!FILES.equals(currentTab)) {
            fileToolbar.setVisibility(View.GONE);
            return;
        }
        fileToolbar.setVisibility(View.VISIBLE);
        String current = currentFilePath();
        Button up = Ui.button(activity, "↑", false);
        up.setContentDescription("Dossier parent");
        String parent = fileListing == null ? current : fileListing.optString("parent", current);
        up.setOnClickListener(view -> listener.onFileNavigate(parent));
        setLogicalEnabled(up, !parent.equals(current));
        fileToolbar.addView(up, Ui.weighted(activity, 1, 5));
        Button roots = Ui.button(activity, "Racines", false);
        roots.setTextSize(12);
        roots.setOnClickListener(view -> showFileRoots(roots));
        fileToolbar.addView(roots, Ui.weighted(activity, 2, 5));
        Button refresh = Ui.button(activity, "↻", false);
        refresh.setContentDescription("Actualiser ce dossier");
        refresh.setOnClickListener(view -> listener.onFileNavigate(current));
        fileToolbar.addView(refresh, Ui.weighted(activity, 1, 5));
        Button mkdir = Ui.button(activity, "+ Dossier", false);
        mkdir.setTextSize(12);
        mkdir.setOnClickListener(view -> listener.onFileCreateFolder(current));
        fileToolbar.addView(mkdir, Ui.weighted(activity, 2, 5));
        Button upload = Ui.button(activity, "Envoyer", true);
        upload.setTextSize(12);
        upload.setOnClickListener(view -> listener.onFileUpload(current));
        fileToolbar.addView(upload, Ui.weighted(activity, 2, 5));

        if (fileListing == null) {
            setLogicalEnabled(up, false);
            setLogicalEnabled(roots, false);
            setLogicalEnabled(refresh, false);
            setLogicalEnabled(mkdir, false);
            setLogicalEnabled(upload, false);
        }
    }

    private void renderFileItems() {
        String current = currentFilePath();

        if (!fileClipboardPath.isEmpty()) {
            LinearLayout clipboard = Ui.card(activity);
            clipboard.addView(Ui.text(
                    activity,
                    (fileClipboardMove ? "Déplacer : " : "Copier : ") + fileClipboardName,
                    13,
                    Ui.MUTED));
            LinearLayout actions = new LinearLayout(activity);
            actions.setOrientation(LinearLayout.HORIZONTAL);
            Button paste = Ui.button(activity, "Coller ici", true);
            paste.setOnClickListener(view -> listener.onFilePaste(
                    fileClipboardPath,
                    current,
                    fileClipboardMove));
            actions.addView(paste, Ui.weighted(activity, 2, 4));
            Button cancel = Ui.button(activity, "Annuler", false);
            cancel.setOnClickListener(view -> clearFileClipboard());
            actions.addView(cancel, Ui.weighted(activity, 1, 4));
            clipboard.addView(actions, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 10, 0, 0));
            content.addView(clipboard, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 12, 0, 0));
        }

        JSONArray items = fileListing.optJSONArray("items");
        if (items == null || items.length() == 0) {
            addEmpty("Ce dossier est vide.");
            return;
        }
        addSectionTitle(items.length() + " élément(s)");
        for (int index = 0; index < items.length(); index++) {
            JSONObject item = items.optJSONObject(index);
            if (item != null) {
                addFileCard(item);
            }
        }
        if (fileListing.optBoolean("truncated", false)) {
            addEmpty("La liste est limitée aux 2 000 premiers éléments.");
        }
    }

    private void showFileRoots(View anchor) {
        JSONArray roots = fileListing == null ? null : fileListing.optJSONArray("roots");
        if (roots == null || roots.length() == 0) {
            return;
        }
        PopupMenu menu = new PopupMenu(activity, anchor);
        for (int index = 0; index < roots.length(); index++) {
            String path = roots.optString(index, "");
            if (!path.isEmpty()) {
                menu.getMenu().add(0, index + 1, index, path);
            }
        }
        menu.setOnMenuItemClickListener(item -> {
            listener.onFileNavigate(item.getTitle().toString());
            return true;
        });
        menu.show();
    }

    private void addFileCard(JSONObject item) {
        String path = item.optString("path", "");
        String name = safe(item.optString("name", ""), "Élément");
        boolean directory = item.optBoolean("is_dir", false);
        boolean link = item.optBoolean("is_symlink", false);
        LinearLayout card = Ui.card(activity);
        LinearLayout row = new LinearLayout(activity);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        card.addView(row);

        TextView icon = Ui.text(activity, directory ? "📁" : "📄", 25, Ui.CYAN);
        icon.setGravity(Gravity.CENTER);
        row.addView(icon, new LinearLayout.LayoutParams(Ui.dp(activity, 45), Ui.dp(activity, 48)));
        LinearLayout texts = new LinearLayout(activity);
        texts.setOrientation(LinearLayout.VERTICAL);
        row.addView(texts, new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        texts.addView(Ui.title(activity, name, 15));
        String detail = link
                ? "Lien symbolique"
                : directory
                ? "Dossier"
                : formatBytes(item.optLong("size", 0));
        texts.addView(Ui.text(activity, detail + "  ·  " + formatFileTime(item.optLong("mtime", 0)), 11, Ui.MUTED));
        Button more = Ui.button(activity, "⋮", false);
        more.setContentDescription("Actions pour " + name);
        more.setOnClickListener(view -> showFileMenu(more, path, name, directory, link));
        row.addView(more, new LinearLayout.LayoutParams(Ui.dp(activity, 46), Ui.dp(activity, 45)));
        if (directory && !link) {
            card.setClickable(true);
            card.setFocusable(true);
            card.setOnClickListener(view -> listener.onFileNavigate(path));
        } else if (!directory && !link) {
            card.setOnClickListener(view -> listener.onFileDownload(path, name, false));
        }
        content.addView(card, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 0, 0, 8));
    }

    private void showFileMenu(
            View anchor,
            String path,
            String name,
            boolean directory,
            boolean link) {
        PopupMenu menu = new PopupMenu(activity, anchor);
        if (!link) {
            menu.getMenu().add(0, 1, 0, "Télécharger");
        }
        if (!link) {
            menu.getMenu().add(0, 2, 1, "Copier");
            menu.getMenu().add(0, 3, 2, "Couper");
        }
        menu.getMenu().add(0, 4, 3, "Renommer");
        menu.getMenu().add(0, 5, 4, "Supprimer");
        menu.setOnMenuItemClickListener(item -> {
            if (item.getItemId() == 1) {
                listener.onFileDownload(path, name, directory);
            } else if (item.getItemId() == 2 || item.getItemId() == 3) {
                fileClipboardPath = path;
                fileClipboardName = name;
                fileClipboardMove = item.getItemId() == 3;
                renderFiles();
            } else if (item.getItemId() == 4) {
                listener.onFileRename(path, name);
            } else if (item.getItemId() == 5) {
                listener.onFileDelete(path, name, directory);
            }
            return true;
        });
        menu.show();
    }

    private String currentFilePath() {
        return fileListing == null ? settings.lastFilePath :
                safe(fileListing.optString("current", settings.lastFilePath), settings.lastFilePath);
    }

    private static String formatBytes(long value) {
        if (value < 1024) return value + " o";
        double size = value;
        String[] units = {"Kio", "Mio", "Gio", "Tio"};
        int unit = -1;
        while (size >= 1024 && unit < units.length - 1) {
            size /= 1024;
            unit++;
        }
        return String.format(Locale.FRANCE, size >= 10 ? "%.0f %s" : "%.1f %s", size, units[unit]);
    }

    private static String formatFileTime(long epochSeconds) {
        if (epochSeconds <= 0) return "Date inconnue";
        return DateTimeFormatter.ofPattern("dd/MM/yyyy HH:mm", Locale.FRANCE)
                .withZone(ZoneId.systemDefault())
                .format(Instant.ofEpochSecond(epochSeconds));
    }

    private void renderBackup() {
        addPageHeader("Backup", "Scripts de sauvegarde du NAS");
        LinearLayout filters = new LinearLayout(activity);
        filters.setOrientation(LinearLayout.HORIZONTAL);
        content.addView(filters, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 16, 0, 0));
        addBackupFilter(filters, "backup", "Backups");
        addBackupFilter(filters, "archive", "Archives");
        addBackupFilter(filters, "cache", "Cache");
        JSONObject backup = monitoring.optJSONObject("backup");
        if (backup == null || !backup.optBoolean("available", false)) {
            addEmpty("Le module Backup est indisponible dans le dernier cliché.");
            return;
        }
        JSONArray scripts = backup.optJSONArray("scripts");
        if (scripts == null || scripts.length() == 0) {
            addEmpty("Aucun script Backup n'est déclaré.");
            return;
        }
        String[] modes = "archive".equals(backupFilter)
                ? new String[]{"archive"}
                : "cache".equals(backupFilter)
                ? new String[]{"cache"}
                : new String[]{"backup", "mirror"};
        int visible = 0;
        for (String mode : modes) {
            boolean headingAdded = false;
            for (int index = 0; index < scripts.length(); index++) {
                JSONObject script = scripts.optJSONObject(index);
                if (script == null || !mode.equalsIgnoreCase(script.optString("mode", "backup"))) {
                    continue;
                }
                if (!headingAdded) {
                    addSectionTitle(backupModeLabel(mode));
                    headingAdded = true;
                }
                addBackupCard(script);
                visible++;
            }
        }
        if (visible == 0) {
            addEmpty("Aucun script dans cette catégorie.");
        }
    }

    private void addBackupFilter(LinearLayout parent, String id, String label) {
        boolean active = id.equals(backupFilter);
        Button button = Ui.button(activity, label, active);
        button.setTextSize(11);
        button.setOnClickListener(view -> {
            backupFilter = id;
            renderCurrentTab();
        });
        parent.addView(button, Ui.weighted(activity, 1, 3));
    }

    private void addBackupCard(JSONObject script) {
        String filename = script.optString("filename", "");
        String title = safe(script.optString("title", "Backup"), "Backup");
        boolean running = script.optBoolean("running", false);
        String result = safe(script.optString("result", "Jamais lancé"), "Jamais lancé");
        String source = script.optString("source", "");
        String target = script.optString("target", "");
        String detail = source.isEmpty() && target.isEmpty()
                ? filename
                : safe(source, "—") + " → " + safe(target, "—");
        String progress = script.optString("progress_text", "");
        String message = script.optString("message", "");

        LinearLayout card = Ui.card(activity);
        LinearLayout heading = new LinearLayout(activity);
        heading.setOrientation(LinearLayout.HORIZONTAL);
        heading.setGravity(Gravity.CENTER_VERTICAL);
        card.addView(heading);

        ImageView icon = new ImageView(activity);
        icon.setImageResource(R.drawable.nav_backup);
        icon.setScaleType(ImageView.ScaleType.CENTER_INSIDE);
        icon.setContentDescription("Backup " + title);
        heading.addView(icon, new LinearLayout.LayoutParams(Ui.dp(activity, 50), Ui.dp(activity, 50)));

        LinearLayout texts = new LinearLayout(activity);
        texts.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams textParams = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
        textParams.setMargins(Ui.dp(activity, 11), 0, Ui.dp(activity, 8), 0);
        heading.addView(texts, textParams);
        texts.addView(Ui.title(activity, title, 16));
        texts.addView(Ui.text(activity, detail, 12, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 4, 0, 0));

        int color = running ? Ui.AMBER : isFailure(result) ? Ui.RED : Ui.GREEN;
        heading.addView(statusDot(color, running ? "En cours" : result));
        String liveDetail = running
                ? safe(progress, safe(message, "En cours"))
                : safe(message, result);
        card.addView(Ui.text(activity, liveDetail, 12, running ? Ui.CYAN : Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 9, 0, 0));

        if (!filename.isEmpty()) {
            LinearLayout actions = new LinearLayout(activity);
            actions.setOrientation(LinearLayout.HORIZONTAL);
            card.addView(actions, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 12, 0, 0));
            Button start = commandButton("▶", "Démarrer " + title);
            start.setOnClickListener(view -> listener.onBackupAction(filename, title, "start"));
            setLogicalEnabled(start, !running);
            actions.addView(start, Ui.weighted(activity, 1, 4));
            Button stop = commandButton("■", "Arrêter " + title);
            stop.setOnClickListener(view -> listener.onBackupAction(filename, title, "stop"));
            setLogicalEnabled(stop, running);
            actions.addView(stop, Ui.weighted(activity, 1, 4));
        }
        content.addView(card, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 0, 0, 9));
    }

    public void showBackupActionPending(String filename, String action) {
        JSONObject backup = monitoring == null ? null : monitoring.optJSONObject("backup");
        JSONArray scripts = backup == null ? null : backup.optJSONArray("scripts");
        if (scripts == null) {
            return;
        }
        for (int index = 0; index < scripts.length(); index++) {
            JSONObject script = scripts.optJSONObject(index);
            if (script == null || !filename.equals(script.optString("filename", ""))) {
                continue;
            }
            try {
                if ("start".equals(action)) {
                    script.put("running", true);
                    script.put("result", "En cours");
                    script.put("message", "Démarrage demandé…");
                } else {
                    script.put("message", "Arrêt demandé…");
                }
            } catch (Exception ignored) {
                return;
            }
            if (BACKUP.equals(currentTab)) {
                renderCurrentTab();
            }
            return;
        }
    }

    private static String backupModeLabel(String mode) {
        if ("mirror".equals(mode)) {
            return "Miroir";
        }
        if ("archive".equals(mode)) {
            return "Archive";
        }
        if ("cache".equals(mode)) {
            return "Cache";
        }
        return "Backup";
    }

    private TextView badge(String text, int color) {
        TextView badge = Ui.text(activity, text, 12, color);
        badge.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        badge.setGravity(Gravity.CENTER);
        badge.setPadding(Ui.dp(activity, 10), Ui.dp(activity, 7), Ui.dp(activity, 10), Ui.dp(activity, 7));
        badge.setBackground(Ui.rounded(Ui.SURFACE_ALT, color, 12, activity));
        return badge;
    }

    private TextView statusDot(int color, String description) {
        TextView dot = Ui.text(activity, "●", 22, color);
        dot.setGravity(Gravity.CENTER);
        dot.setContentDescription(description);
        dot.setPadding(Ui.dp(activity, 8), 0, Ui.dp(activity, 4), 0);
        return dot;
    }

    private Button commandButton(String symbol, String description) {
        Button button = Ui.button(activity, symbol, false);
        button.setTextSize(20);
        button.setContentDescription(description);
        return button;
    }

    private static void setLogicalEnabled(Button button, boolean enabled) {
        button.setEnabled(enabled);
        button.setAlpha(enabled ? 1f : 0.32f);
    }

    private void addPageHeader(String title, String subtitle) {
        content.addView(Ui.title(activity, title, 27));
        content.addView(Ui.text(activity, subtitle, 14, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 5, 0, 0));
    }

    private void addSectionTitle(String value) {
        TextView title = Ui.title(activity, value, 18);
        content.addView(title, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 22, 0, 10));
    }

    private void addHardwareCaption(String value) {
        TextView title = Ui.text(activity, value, 13, Ui.MUTED);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        content.addView(title, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 3, 8, 0, 7));
    }

    private void addFanGrid(JSONArray fans) {
        LinearLayout row = null;
        int columns = 0;
        for (int index = 0; index < fans.length(); index++) {
            JSONObject item = fans.optJSONObject(index);
            if (item == null) continue;
            if (row == null) {
                row = new LinearLayout(activity);
                row.setOrientation(LinearLayout.HORIZONTAL);
            }
            String value = safe(item.optString("rpm_label", ""), item.optInt("rpm", 0) + " RPM");
            int color = "fault".equals(item.optString("status")) ? Ui.RED : Ui.GREEN;
            row.addView(compactHardwareCard(
                    safe(item.optString("label", "Ventilateur"), "Ventilateur"),
                    value,
                    color), Ui.weighted(activity, 1, 3));
            columns++;
            if (columns == 3) {
                content.addView(row, Ui.margins(
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT,
                        activity, 0, 0, 0, 7));
                row = null;
                columns = 0;
            }
        }
        if (row != null) {
            while (columns < 3) {
                row.addView(new View(activity), Ui.weighted(activity, 1, 3));
                columns++;
            }
            content.addView(row, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 0, 0, 7));
        }
    }

    private void addTemperatureRow(JSONObject cpu, JSONObject board) {
        LinearLayout row = new LinearLayout(activity);
        row.setOrientation(LinearLayout.HORIZONTAL);
        int count = 0;
        if (cpu != null) {
            double value = cpu.optDouble("current", 0);
            row.addView(compactHardwareCard(
                    "CPU",
                    String.format(Locale.FRANCE, "%.1f °C", value),
                    value >= 85 ? Ui.RED : Ui.CYAN), Ui.weighted(activity, 1, 3));
            count++;
        }
        if (board != null) {
            double value = board.optDouble("current", 0);
            row.addView(compactHardwareCard(
                    "Carte mère",
                    String.format(Locale.FRANCE, "%.1f °C", value),
                    value >= 85 ? Ui.RED : Ui.CYAN), Ui.weighted(activity, 1, 3));
            count++;
        }
        while (count < 2) {
            row.addView(new View(activity), Ui.weighted(activity, 1, 3));
            count++;
        }
        content.addView(row, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 0, 0, 7));
    }

    private LinearLayout compactHardwareCard(String title, String value, int color) {
        LinearLayout card = Ui.card(activity);
        card.setGravity(Gravity.CENTER);
        card.setPadding(
                Ui.dp(activity, 9),
                Ui.dp(activity, 10),
                Ui.dp(activity, 9),
                Ui.dp(activity, 10));
        TextView titleView = Ui.text(activity, title, 11, Ui.MUTED);
        titleView.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        titleView.setGravity(Gravity.CENTER);
        titleView.setMaxLines(2);
        card.addView(titleView);
        TextView valueView = Ui.title(activity, value, 16);
        valueView.setTextColor(color);
        valueView.setGravity(Gravity.CENTER);
        card.addView(valueView, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 5, 0, 0));
        return card;
    }

    private static JSONObject selectTemperature(JSONArray temperatures, boolean cpu) {
        JSONObject selected = null;
        int selectedScore = Integer.MIN_VALUE;
        for (int index = 0; index < temperatures.length(); index++) {
            JSONObject item = temperatures.optJSONObject(index);
            if (item == null || !item.has("current") || item.isNull("current")) continue;
            String label = item.optString("label", "").toLowerCase(Locale.ROOT);
            String chip = item.optString("chip", "").toLowerCase(Locale.ROOT);
            int score = temperatureScore(label, chip, cpu);
            if (score > selectedScore) {
                selected = item;
                selectedScore = score;
            }
        }
        return selectedScore >= 50 ? selected : null;
    }

    private static int temperatureScore(String label, String chip, boolean cpu) {
        if (label.contains("gpu") || chip.contains("gpu")) return -1000;
        if (cpu) {
            int score = 0;
            if (label.contains("package")) score += 140;
            else if (label.contains("tctl")) score += 130;
            else if (label.contains("tdie")) score += 120;
            else if (label.contains("cpu")) score += 105;
            if (chip.contains("coretemp") || chip.contains("k10temp") ||
                    chip.contains("zenpower") || chip.contains("cpu_thermal")) score += 45;
            if (label.startsWith("core ") || label.matches("core\\s*\\d+")) score -= 90;
            return score;
        }

        int score = 0;
        if (label.contains("motherboard") || label.contains("mainboard")) score += 150;
        else if (label.contains("system")) score += 125;
        else if (label.contains("systin")) score += 120;
        else if (label.contains("temp1")) score += 90;
        else if (label.contains("pch")) score += 75;
        if (chip.contains("nct") || chip.contains("it87") || chip.contains("acpitz")) score += 35;
        if (label.contains("cpu") || label.contains("package") || label.contains("core") ||
                label.contains("tctl") || label.contains("tdie")) score -= 150;
        return score;
    }

    private LinearLayout metricCard(String title, double percent, String detail) {
        LinearLayout card = Ui.card(activity);
        TextView label = Ui.text(activity, title, 13, Ui.MUTED);
        label.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        card.addView(label);
        TextView value = Ui.title(activity, formatPercent(percent), 25);
        value.setTextColor(metricColor(percent));
        card.addView(value, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 6, 0, 8));
        card.addView(Ui.progress(activity, (int) Math.round(percent), metricColor(percent)), new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                Ui.dp(activity, 7)));
        if (detail != null && !detail.isEmpty()) {
            card.addView(Ui.text(activity, detail, 12, Ui.MUTED), Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 8, 0, 0));
        }
        return card;
    }

    private LinearLayout summaryCard(String title, String status, String detail, boolean ok) {
        LinearLayout card = Ui.card(activity);
        TextView titleView = Ui.text(activity, title, 13, Ui.MUTED);
        titleView.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        card.addView(titleView);
        TextView statusView = Ui.title(activity, status, 17);
        statusView.setTextColor(ok ? Ui.GREEN : Ui.RED);
        card.addView(statusView, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 7, 0, 0));
        card.addView(Ui.text(activity, detail, 12, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 4, 0, 0));
        return card;
    }

    private void addListCard(String title, String subtitle, String badge, int badgeColor) {
        LinearLayout card = Ui.card(activity);
        card.setOrientation(LinearLayout.HORIZONTAL);
        card.setGravity(Gravity.CENTER_VERTICAL);

        LinearLayout texts = new LinearLayout(activity);
        texts.setOrientation(LinearLayout.VERTICAL);
        card.addView(texts, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        texts.addView(Ui.title(activity, title, 16));
        texts.addView(Ui.text(activity, subtitle, 12, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 5, 8, 0));
        card.addView(badge(badge, badgeColor));
        content.addView(card, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 0, 0, 8));
    }

    private void addEmpty(String message) {
        LinearLayout empty = Ui.card(activity);
        empty.addView(Ui.text(activity, message, 15, Ui.MUTED));
        content.addView(empty, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 16, 0, 0));
    }

    private static String frenchTime(String utcValue) {
        try {
            return DateTimeFormatter.ofPattern("dd/MM/yyyy 'à' HH:mm", Locale.FRANCE)
                    .withZone(ZoneId.of("Europe/Paris"))
                    .format(Instant.parse(utcValue));
        } catch (Exception ignored) {
            return utcValue;
        }
    }

    private static int metricColor(double percent) {
        if (percent >= 90) {
            return Ui.RED;
        }
        if (percent >= 75) {
            return Ui.AMBER;
        }
        return Ui.CYAN;
    }

    private static String formatPercent(double value) {
        return String.format(Locale.FRANCE, "%.1f %%", value);
    }

    private static int countFailedTasks(JSONArray tasks) {
        if (tasks == null) {
            return 0;
        }
        int count = 0;
        for (int index = 0; index < tasks.length(); index++) {
            JSONObject task = tasks.optJSONObject(index);
            if (task != null && isFailure(task.optString("result", task.optString("status", "")))) {
                count++;
            }
        }
        return count;
    }

    private static boolean isFailure(String value) {
        String normalized = value == null ? "" : value.toLowerCase(Locale.ROOT);
        return normalized.contains("erreur") || normalized.contains("échec") ||
                normalized.contains("echec") || normalized.contains("failed") ||
                normalized.contains("failure");
    }

    private static String safe(String value, String fallback) {
        return value == null || value.trim().isEmpty() ? fallback : value.trim();
    }

    private boolean home(String id) {
        return settings.homeItems.contains(id);
    }

    private static String valueWithUnit(String value, String unit) {
        String clean = value == null ? "" : value.trim();
        if (clean.isEmpty() || "-".equals(clean) || "n/a".equalsIgnoreCase(clean)) return "—";
        if (unit == null || unit.isEmpty() || clean.toLowerCase(Locale.ROOT).contains(
                unit.toLowerCase(Locale.ROOT))) return clean;
        return clean + " " + unit;
    }

    private static final class NavItem {
        final LinearLayout root;
        final ImageView icon;
        final TextView label;

        NavItem(LinearLayout root, ImageView icon, TextView label) {
            this.root = root;
            this.icon = icon;
            this.label = label;
        }
    }
}
