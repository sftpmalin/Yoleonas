package com.sftpmalin.yoleo.ui;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.graphics.Typeface;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import com.sftpmalin.yoleo.data.AppSettings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Set;

@SuppressLint("SetTextI18n")
public final class SettingsView {
    public interface Listener {
        void onSave(AppSettings settings);

        void onCancel();

    }

    private final Activity activity;
    private final AppSettings draft;
    private final LinearLayout root;
    private final LinearLayout content;
    private final Button[] tabButtons = new Button[5];
    private final LinearLayout[] panels = new LinearLayout[5];
    private final Map<String, CheckBox> mountChoices = new LinkedHashMap<>();
    private final Map<String, CheckBox> homeChoices = new LinkedHashMap<>();
    private final List<String> navigationOrder;
    private final List<String> homeOrder;
    private LinearLayout navigationList;
    private LinearLayout homeList;

    private final EditText interval;
    private final EditText offlineFailures;
    private final CheckBox serverOffline;
    private final CheckBox serverRecovery;
    private final CheckBox cpu;
    private final EditText cpuThreshold;
    private final CheckBox ram;
    private final EditText ramThreshold;
    private final CheckBox storage;
    private final EditText storageThreshold;
    private final CheckBox mountFailures;
    private final CheckBox dockerService;
    private final CheckBox dockerContainers;
    private final CheckBox samba;
    private final CheckBox taskFailures;
    private final CheckBox buildPending;
    private final CheckBox registryCleanup;
    private final EditText registryDay;

    public SettingsView(
            Activity activity,
            AppSettings settings,
            JSONArray availableMounts,
            Listener listener) {
        this.activity = activity;
        draft = settings.copy();
        navigationOrder = new ArrayList<>(draft.navigationOrder);
        homeOrder = new ArrayList<>(draft.homeOrder);

        root = new LinearLayout(activity);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Ui.BACKGROUND);

        LinearLayout heading = new LinearLayout(activity);
        heading.setOrientation(LinearLayout.VERTICAL);
        heading.setPadding(Ui.dp(activity, 18), Ui.dp(activity, 14), Ui.dp(activity, 18), Ui.dp(activity, 10));
        heading.setBackgroundColor(Ui.SURFACE);
        heading.addView(Ui.title(activity, "Réglages Yoleo", 22));
        heading.addView(Ui.text(
                activity,
                "Affichage du stockage et notifications locales Android",
                13,
                Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 3, 0, 0));
        root.addView(heading);

        HorizontalScrollView tabScroll = new HorizontalScrollView(activity);
        tabScroll.setHorizontalScrollBarEnabled(false);
        tabScroll.setBackgroundColor(Ui.SURFACE);
        LinearLayout tabs = new LinearLayout(activity);
        tabs.setOrientation(LinearLayout.HORIZONTAL);
        tabs.setPadding(Ui.dp(activity, 8), Ui.dp(activity, 8), Ui.dp(activity, 8), Ui.dp(activity, 6));
        tabs.setBackgroundColor(Ui.SURFACE);
        tabScroll.addView(tabs, new HorizontalScrollView.LayoutParams(
                HorizontalScrollView.LayoutParams.WRAP_CONTENT,
                HorizontalScrollView.LayoutParams.WRAP_CONTENT));
        root.addView(tabScroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        addTab(tabs, 0, "Général");
        addTab(tabs, 1, "Stockage");
        addTab(tabs, 2, "Services");
        addTab(tabs, 3, "Onglets");
        addTab(tabs, 4, "Accueil");

        ScrollView scroll = new ScrollView(activity);
        scroll.setFillViewport(true);
        content = new LinearLayout(activity);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(Ui.dp(activity, 18), Ui.dp(activity, 14), Ui.dp(activity, 18), Ui.dp(activity, 24));
        scroll.addView(content, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT));
        root.addView(scroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1));

        interval = numberField(String.valueOf(draft.pollIntervalMinutes));
        offlineFailures = numberField(String.valueOf(draft.offlineFailureCount));
        serverOffline = check("Serveur hors ligne", draft.notifyServerOffline);
        serverRecovery = check("Retour du serveur en ligne", draft.notifyServerRecovery);
        cpu = check("CPU supérieur ou égal au seuil", draft.notifyCpu);
        cpuThreshold = numberField(String.valueOf(draft.cpuThresholdPercent));
        ram = check("RAM supérieure ou égale au seuil", draft.notifyRam);
        ramThreshold = numberField(String.valueOf(draft.ramThresholdPercent));
        storage = check("Stockage sélectionné supérieur ou égal au seuil", draft.notifyStorage);
        storageThreshold = numberField(String.valueOf(draft.storageThresholdPercent));
        mountFailures = check(
                "Alerter si un montage sélectionné devient un dossier ou disparaît",
                draft.notifyMountFailures);
        dockerService = check("Le service Docker s'arrête", draft.notifyDockerService);
        dockerContainers = check(
                "Un conteneur Docker qui tournait s'arrête",
                draft.notifyDockerContainers);
        samba = check("Samba ou WSDD s'arrête", draft.notifySamba);
        taskFailures = check("Une nouvelle exécution de tâche échoue", draft.notifyTaskFailures);
        buildPending = check(
                "Un nouvel élément est à builder ou à envoyer",
                draft.notifyBuildPending);
        registryCleanup = check(
                "Rappel mensuel pour vérifier le registre Docker",
                draft.notifyRegistryCleanup);
        registryDay = numberField(String.valueOf(draft.registryReminderDay));

        panels[0] = createGeneralPanel();
        panels[1] = createStoragePanel(availableMounts);
        panels[2] = createServicesPanel();
        panels[3] = createNavigationPanel();
        panels[4] = createHomePanel();
        showTab(0);

        LinearLayout actions = new LinearLayout(activity);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setPadding(Ui.dp(activity, 12), Ui.dp(activity, 8), Ui.dp(activity, 12), Ui.dp(activity, 12));
        actions.setBackgroundColor(Ui.SURFACE);
        Button cancel = Ui.button(activity, "Annuler", false);
        Button save = Ui.button(activity, "Enregistrer", true);
        actions.addView(cancel, Ui.weighted(activity, 1, 4));
        actions.addView(save, Ui.weighted(activity, 1, 4));
        cancel.setOnClickListener(view -> listener.onCancel());
        save.setOnClickListener(view -> listener.onSave(readSettings()));
        root.addView(actions);
    }

    public View getView() {
        return root;
    }

    private LinearLayout createGeneralPanel() {
        LinearLayout panel = panel();
        panel.addView(section("Fréquence des contrôles"));
        LinearLayout timing = Ui.card(activity);
        timing.addView(Ui.text(
                activity,
                "Android effectue une seule vérification complète. Le mode économie d'énergie peut la retarder.",
                13,
                Ui.GREEN));
        addNumber(timing, "Intervalle : 15, 30 ou 60 minutes", interval, "min");
        addNumber(timing, "Alerte hors ligne après", offlineFailures, "échec(s)");
        panel.addView(timing);

        panel.addView(section("Serveur et ressources"));
        LinearLayout alerts = Ui.card(activity);
        alerts.addView(serverOffline);
        alerts.addView(serverRecovery);
        alerts.addView(cpu);
        addNumber(alerts, "Seuil CPU", cpuThreshold, "%");
        alerts.addView(ram);
        addNumber(alerts, "Seuil RAM", ramThreshold, "%");
        panel.addView(alerts);
        return panel;
    }

    private LinearLayout createStoragePanel(JSONArray availableMounts) {
        LinearLayout panel = panel();
        panel.addView(section("Alertes stockage"));
        LinearLayout alertCard = Ui.card(activity);
        alertCard.addView(storage);
        addNumber(alertCard, "Seuil d'occupation", storageThreshold, "%");
        alertCard.addView(mountFailures);
        panel.addView(alertCard);

        panel.addView(section("Montages affichés et surveillés"));
        LinearLayout choices = Ui.card(activity);
        choices.addView(Ui.text(
                activity,
                "Tous sont cochés par défaut. Cette sélection décide à la fois de ce qui apparaît dans Stockage et des montages surveillés.",
                13,
                Ui.GREEN));
        Set<String> seen = new LinkedHashSet<>();
        if (availableMounts != null) {
            for (int index = 0; index < availableMounts.length(); index++) {
                JSONObject mount = availableMounts.optJSONObject(index);
                if (mount == null) {
                    continue;
                }
                String path = mount.optString("path", "").trim();
                if (path.isEmpty() || !seen.add(path)) {
                    continue;
                }
                String label = mount.optString("label", "").trim();
                String title = label.isEmpty() ? path : label + " — " + path;
                addMountChoice(choices, path, title);
            }
        }
        for (String selected : draft.displayedMountPaths) {
            if (seen.add(selected)) {
                addMountChoice(choices, selected, selected + " — indisponible dans le dernier cliché");
            }
        }
        if (mountChoices.isEmpty()) {
            choices.addView(Ui.text(
                    activity,
                    "Aucun montage n'a encore été reçu. Actualise le NAS puis reviens ici.",
                    14,
                    Ui.MUTED), Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 12, 0, 0));
        }
        panel.addView(choices);
        return panel;
    }

    private LinearLayout createServicesPanel() {
        LinearLayout panel = panel();
        panel.addView(section("Docker et partages"));
        LinearLayout services = Ui.card(activity);
        services.addView(dockerService);
        services.addView(dockerContainers);
        services.addView(samba);
        panel.addView(services);

        panel.addView(section("Tâches et entretien"));
        LinearLayout work = Ui.card(activity);
        work.addView(taskFailures);
        work.addView(buildPending);
        work.addView(registryCleanup);
        addNumber(work, "Jour du rappel mensuel", registryDay, "du mois");
        panel.addView(work);
        return panel;
    }

    private LinearLayout createNavigationPanel() {
        LinearLayout panel = panel();
        panel.addView(section("Ordre du menu inférieur"));
        LinearLayout card = Ui.card(activity);
        card.addView(Ui.text(
                activity,
                "Utilise les flèches pour choisir l'ordre des rubriques. VM peut ainsi rester définitivement en dernier.",
                13,
                Ui.GREEN));
        navigationList = new LinearLayout(activity);
        navigationList.setOrientation(LinearLayout.VERTICAL);
        card.addView(navigationList, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 12, 0, 0));
        renderNavigationOrder();
        panel.addView(card);
        return panel;
    }

    private LinearLayout createHomePanel() {
        LinearLayout panel = panel();
        panel.addView(section("Informations de la page d'accueil"));
        LinearLayout card = Ui.card(activity);
        card.addView(Ui.text(
                activity,
                "Coche ce que tu veux voir, puis utilise les flèches pour choisir l'ordre exact de l'accueil. Une information absente du NAS sera masquée automatiquement.",
                13,
                Ui.GREEN));
        homeList = new LinearLayout(activity);
        homeList.setOrientation(LinearLayout.VERTICAL);
        card.addView(homeList, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 12, 0, 0));
        renderHomeOrder();
        panel.addView(card);
        return panel;
    }

    private void renderHomeOrder() {
        if (homeList == null) {
            return;
        }
        homeList.removeAllViews();
        homeChoices.clear();
        for (int index = 0; index < homeOrder.size(); index++) {
            String id = homeOrder.get(index);
            LinearLayout row = new LinearLayout(activity);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            CheckBox choice = check((index + 1) + ".  " + homeLabel(id), draft.homeItems.contains(id));
            homeChoices.put(id, choice);
            row.addView(choice, new LinearLayout.LayoutParams(
                    0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
            Button up = Ui.button(activity, "↑", false);
            Button down = Ui.button(activity, "↓", false);
            up.setTextSize(18);
            down.setTextSize(18);
            up.setEnabled(index > 0);
            down.setEnabled(index < homeOrder.size() - 1);
            up.setAlpha(up.isEnabled() ? 1f : 0.3f);
            down.setAlpha(down.isEnabled() ? 1f : 0.3f);
            final int position = index;
            up.setOnClickListener(view -> moveHome(position, position - 1));
            down.setOnClickListener(view -> moveHome(position, position + 1));
            row.addView(up, new LinearLayout.LayoutParams(Ui.dp(activity, 52), Ui.dp(activity, 44)));
            row.addView(down, Ui.margins(
                    Ui.dp(activity, 52), Ui.dp(activity, 44), activity, 6, 0, 0, 0));
            homeList.addView(row, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 4, 0, 4));
        }
    }

    private void renderNavigationOrder() {
        if (navigationList == null) {
            return;
        }
        navigationList.removeAllViews();
        for (int index = 0; index < navigationOrder.size(); index++) {
            String id = navigationOrder.get(index);
            LinearLayout row = new LinearLayout(activity);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            TextView label = Ui.title(activity, (index + 1) + ".  " + navigationLabel(id), 15);
            row.addView(label, new LinearLayout.LayoutParams(
                    0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
            Button up = Ui.button(activity, "↑", false);
            Button down = Ui.button(activity, "↓", false);
            up.setTextSize(18);
            down.setTextSize(18);
            up.setEnabled(index > 0);
            down.setEnabled(index < navigationOrder.size() - 1);
            up.setAlpha(up.isEnabled() ? 1f : 0.3f);
            down.setAlpha(down.isEnabled() ? 1f : 0.3f);
            final int position = index;
            up.setOnClickListener(view -> moveNavigation(position, position - 1));
            down.setOnClickListener(view -> moveNavigation(position, position + 1));
            row.addView(up, new LinearLayout.LayoutParams(Ui.dp(activity, 52), Ui.dp(activity, 44)));
            row.addView(down, Ui.margins(
                    Ui.dp(activity, 52), Ui.dp(activity, 44), activity, 6, 0, 0, 0));
            navigationList.addView(row, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, 4, 0, 4));
        }
    }

    private void moveNavigation(int from, int to) {
        if (from < 0 || to < 0 || from >= navigationOrder.size() || to >= navigationOrder.size()) {
            return;
        }
        Collections.swap(navigationOrder, from, to);
        renderNavigationOrder();
    }

    private void moveHome(int from, int to) {
        if (from < 0 || to < 0 || from >= homeOrder.size() || to >= homeOrder.size()) {
            return;
        }
        captureHomeChoices();
        Collections.swap(homeOrder, from, to);
        renderHomeOrder();
    }

    private void captureHomeChoices() {
        if (homeChoices.isEmpty()) {
            return;
        }
        draft.homeItems.clear();
        for (Map.Entry<String, CheckBox> entry : homeChoices.entrySet()) {
            if (entry.getValue().isChecked()) {
                draft.homeItems.add(entry.getKey());
            }
        }
    }

    private static String homeLabel(String id) {
        if ("cpu".equals(id)) return "Utilisation CPU";
        if ("ram".equals(id)) return "Utilisation RAM";
        if ("storage".equals(id)) return "Stockage principal";
        if ("temperatures".equals(id)) return "Températures";
        if ("fans".equals(id)) return "Ventilateurs";
        if ("gpus".equals(id)) return "Cartes graphiques Intel / NVIDIA";
        if ("host".equals(id)) return "Détails de l'hôte";
        if ("network".equals(id)) return "Adresse IP et réseau local";
        if ("uptime".equals(id)) return "Durée d'activité";
        if ("services".equals(id)) return "Services systemd actifs";
        if ("docker".equals(id)) return "Résumé Docker";
        if ("samba".equals(id)) return "Partages Samba";
        if ("tasks".equals(id)) return "Résumé des tâches";
        if ("vms".equals(id)) return "Résumé des VM";
        if ("build".equals(id)) return "Builds en attente";
        return id;
    }

    private static String navigationLabel(String id) {
        if ("home".equals(id)) return "Accueil";
        if ("docker".equals(id)) return "Docker";
        if ("storage".equals(id)) return "Stockage";
        if ("tasks".equals(id)) return "Tâches";
        if ("backup".equals(id)) return "Backup";
        if ("files".equals(id)) return "Fichiers";
        if ("vms".equals(id)) return "VM";
        return id;
    }

    private void addMountChoice(LinearLayout parent, String path, String label) {
        boolean checked = !draft.mountSelectionConfigured || draft.displayedMountPaths.contains(path);
        CheckBox choice = check(label, checked);
        choice.setTag(path);
        mountChoices.put(path, choice);
        parent.addView(choice, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 8, 0, 0));
    }

    private AppSettings readSettings() {
        draft.pollIntervalMinutes = normalizedInterval(readInt(interval, 15));
        draft.offlineFailureCount = clamp(readInt(offlineFailures, 2), 1, 5);
        draft.notifyServerOffline = serverOffline.isChecked();
        draft.notifyServerRecovery = serverRecovery.isChecked();
        draft.notifyCpu = cpu.isChecked();
        draft.cpuThresholdPercent = clamp(readInt(cpuThreshold, 90), 1, 100);
        draft.notifyRam = ram.isChecked();
        draft.ramThresholdPercent = clamp(readInt(ramThreshold, 90), 1, 100);
        draft.notifyStorage = storage.isChecked();
        draft.storageThresholdPercent = clamp(readInt(storageThreshold, 80), 1, 100);
        draft.notifyMountFailures = mountFailures.isChecked();
        draft.notifyDockerService = dockerService.isChecked();
        draft.notifyDockerContainers = dockerContainers.isChecked();
        draft.notifySamba = samba.isChecked();
        draft.notifyTaskFailures = taskFailures.isChecked();
        draft.notifyBuildPending = buildPending.isChecked();
        draft.notifyRegistryCleanup = registryCleanup.isChecked();
        draft.registryReminderDay = clamp(readInt(registryDay, 1), 1, 28);
        if (!mountChoices.isEmpty()) {
            draft.mountSelectionConfigured = true;
            draft.displayedMountPaths.clear();
            for (Map.Entry<String, CheckBox> entry : mountChoices.entrySet()) {
                if (entry.getValue().isChecked()) {
                    draft.displayedMountPaths.add(entry.getKey());
                }
            }
        }
        draft.navigationOrder = new ArrayList<>(navigationOrder);
        captureHomeChoices();
        draft.homeOrder = new ArrayList<>(homeOrder);
        return draft;
    }

    private void addTab(LinearLayout tabs, int index, String label) {
        Button button = Ui.button(activity, label, false);
        button.setTextSize(13);
        button.setOnClickListener(view -> showTab(index));
        tabButtons[index] = button;
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                Ui.dp(activity, 104), Ui.dp(activity, 46));
        params.setMargins(Ui.dp(activity, 3), 0, Ui.dp(activity, 3), 0);
        tabs.addView(button, params);
    }

    private void showTab(int index) {
        content.removeAllViews();
        content.addView(panels[index], new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        for (int item = 0; item < tabButtons.length; item++) {
            boolean active = item == index;
            tabButtons[item].setTextColor(active ? Ui.BACKGROUND : Ui.TEXT);
            tabButtons[item].setBackground(Ui.rounded(
                    active ? Ui.CYAN : Ui.SURFACE_ALT,
                    active ? Ui.CYAN : Ui.BORDER,
                    13,
                    activity));
        }
    }

    private LinearLayout panel() {
        LinearLayout panel = new LinearLayout(activity);
        panel.setOrientation(LinearLayout.VERTICAL);
        return panel;
    }

    private TextView section(String value) {
        TextView title = Ui.title(activity, value, 18);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        title.setPadding(0, Ui.dp(activity, 10), 0, Ui.dp(activity, 9));
        return title;
    }

    private CheckBox check(String label, boolean checked) {
        CheckBox box = new CheckBox(activity);
        box.setText(label);
        box.setTextColor(Ui.TEXT);
        box.setTextSize(14);
        box.setChecked(checked);
        box.setGravity(Gravity.CENTER_VERTICAL);
        box.setPadding(0, Ui.dp(activity, 5), 0, Ui.dp(activity, 5));
        return box;
    }

    private EditText numberField(String value) {
        EditText field = Ui.field(
                activity,
                "",
                InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL);
        field.setText(value);
        field.setSelectAllOnFocus(true);
        return field;
    }

    private void addNumber(LinearLayout parent, String label, EditText field, String suffix) {
        TextView title = Ui.text(activity, label, 13, Ui.MUTED);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        parent.addView(title, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 10, 0, 5));
        LinearLayout row = new LinearLayout(activity);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.addView(field, new LinearLayout.LayoutParams(0, Ui.dp(activity, 50), 1));
        row.addView(Ui.text(activity, suffix, 13, Ui.MUTED), Ui.margins(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 10, 0, 2, 0));
        parent.addView(row);
    }

    private static int readInt(EditText field, int fallback) {
        try {
            return Integer.parseInt(field.getText().toString().trim());
        } catch (Exception ignored) {
            return fallback;
        }
    }

    private static int normalizedInterval(int value) {
        if (value >= 45) {
            return 60;
        }
        if (value >= 23) {
            return 30;
        }
        return 15;
    }

    private static int clamp(int value, int minimum, int maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }
}
