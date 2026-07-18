package com.sftpmalin.yoleo.ui;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.graphics.Typeface;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import com.sftpmalin.yoleo.R;
import com.sftpmalin.yoleo.data.AppSettings;

@SuppressLint("SetTextI18n")
public final class SetupView {
    public interface Listener {
        void onChooseP12();

        void onTest(FormData data);

        void onSave(FormData data);

        void onBack();
    }

    public enum Status {
        IDLE,
        PENDING,
        SUCCESS,
        FAILURE
    }

    public static final class FormData {
        public final String serverUrl;
        public final String p12Password;
        public final String username;
        public final String serverPassword;

        private FormData(
                String serverUrl,
                String p12Password,
                String username,
                String serverPassword) {
            this.serverUrl = serverUrl;
            this.p12Password = p12Password;
            this.username = username;
            this.serverPassword = serverPassword;
        }

        public String fingerprint(String p12Name) {
            return clean(serverUrl) + "\u001f" + clean(username) + "\u001f" +
                    p12Password + "\u001f" + clean(p12Name);
        }

        private static String clean(String value) {
            return value == null ? "" : value.trim();
        }
    }

    private final Activity activity;
    private final Listener listener;
    private final LinearLayout root;
    private final EditText serverUrl;
    private final EditText p12Password;
    private final EditText username;
    private final EditText serverPassword;
    private final TextView p12Name;
    private final TextView[] statusLines = new TextView[4];
    private final Button chooseP12;
    private final Button test;
    private final Button save;
    private String selectedP12Name;

    public SetupView(
            Activity activity,
            AppSettings settings,
            String storedP12Password,
            boolean hasStoredP12,
            boolean allowBack,
            Listener listener) {
        this.activity = activity;
        this.listener = listener;
        selectedP12Name = settings.p12DisplayName == null ? "" : settings.p12DisplayName;

        root = new LinearLayout(activity);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Ui.BACKGROUND);

        ScrollView scroll = new ScrollView(activity);
        scroll.setFillViewport(true);
        LinearLayout content = new LinearLayout(activity);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(Ui.dp(activity, 22), Ui.dp(activity, 28), Ui.dp(activity, 22), Ui.dp(activity, 36));
        scroll.addView(content, new ScrollView.LayoutParams(
                ScrollView.LayoutParams.MATCH_PARENT,
                ScrollView.LayoutParams.WRAP_CONTENT));
        root.addView(scroll, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1));

        ImageView logo = new ImageView(activity);
        logo.setImageResource(R.drawable.logo);
        logo.setContentDescription("Logo Yoleo");
        logo.setScaleType(ImageView.ScaleType.CENTER_CROP);
        LinearLayout.LayoutParams logoParams = new LinearLayout.LayoutParams(
                Ui.dp(activity, 92), Ui.dp(activity, 92));
        logoParams.gravity = Gravity.CENTER_HORIZONTAL;
        content.addView(logo, logoParams);

        TextView title = Ui.title(activity, "Connexion à Yoleo", 28);
        title.setGravity(Gravity.CENTER);
        content.addView(title, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 14, 0, 0));

        TextView subtitle = Ui.text(
                activity,
                "HTTPS + certificat client P12 + compte serveur",
                14,
                Ui.MUTED);
        subtitle.setGravity(Gravity.CENTER);
        content.addView(subtitle, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 6, 0, 24));

        LinearLayout form = Ui.card(activity);
        content.addView(form, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));

        serverUrl = Ui.field(activity, "https://serveur.exemple.com", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        serverUrl.setText(settings.serverUrl);
        addField(form, "Adresse HTTPS du serveur", serverUrl);

        addLabel(form, "Fichier P12");
        p12Name = Ui.text(activity, "", 14, Ui.MUTED);
        p12Name.setPadding(Ui.dp(activity, 14), Ui.dp(activity, 12), Ui.dp(activity, 14), Ui.dp(activity, 12));
        p12Name.setBackground(Ui.rounded(Ui.SURFACE_ALT, Ui.BORDER, 12, activity));
        form.addView(p12Name, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
        chooseP12 = Ui.button(activity, "Parcourir…", false);
        chooseP12.setOnClickListener(view -> listener.onChooseP12());
        form.addView(chooseP12, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 8, 0, 0));
        setP12Name(selectedP12Name, hasStoredP12);

        p12Password = Ui.field(activity, "Mot de passe du certificat", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        p12Password.setText(storedP12Password == null ? "" : storedP12Password);
        addField(form, "Mot de passe du P12", p12Password);

        username = Ui.field(activity, "Nom d'utilisateur", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_NORMAL);
        username.setText(settings.username);
        addField(form, "Nom d'utilisateur", username);

        serverPassword = Ui.field(activity, "Mot de passe du compte", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        addField(form, "Mot de passe serveur", serverPassword);

        TextView security = Ui.text(
                activity,
                "Le mot de passe serveur n'est jamais enregistré. Le P12 reste dans le stockage privé de l'application ; son mot de passe et le jeton sont chiffrés par Android Keystore.",
                13,
                Ui.GREEN);
        content.addView(security, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 2, 16, 2, 0));

        LinearLayout results = Ui.card(activity);
        content.addView(results, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 18, 0, 0));
        TextView resultsTitle = Ui.title(activity, "Vérification facultative", 17);
        results.addView(resultsTitle);
        String[] initial = {
                "Certificat client : non testé",
                "Connexion HTTPS : non testée",
                "Identifiants : non testés",
                "Jeton API : non testé"
        };
        for (int index = 0; index < statusLines.length; index++) {
            statusLines[index] = Ui.text(activity, "• " + initial[index], 14, Ui.MUTED);
            results.addView(statusLines[index], Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 0, index == 0 ? 12 : 7, 0, 0));
        }

        LinearLayout actions = new LinearLayout(activity);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        content.addView(actions, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 0, 18, 0, 0));
        test = Ui.button(activity, "Tester", false);
        save = Ui.button(activity, "Enregistrer", true);
        actions.addView(test, Ui.weighted(activity, 1, 4));
        actions.addView(save, Ui.weighted(activity, 1, 4));
        test.setOnClickListener(view -> listener.onTest(readForm()));
        save.setOnClickListener(view -> listener.onSave(readForm()));

        if (allowBack) {
            Button back = Ui.button(activity, "Retour au tableau de bord", false);
            back.setOnClickListener(view -> listener.onBack());
            content.addView(back, Ui.margins(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    activity, 4, 12, 4, 0));
        }
    }

    public View getView() {
        return root;
    }

    public String getSelectedP12Name() {
        return selectedP12Name;
    }

    public void setP12Name(String name, boolean alreadyStored) {
        selectedP12Name = name == null ? "" : name.trim();
        if (!selectedP12Name.isEmpty()) {
            p12Name.setText("✓ " + selectedP12Name);
            p12Name.setTextColor(Ui.TEXT);
        } else if (alreadyStored) {
            p12Name.setText("✓ Certificat déjà enregistré");
            p12Name.setTextColor(Ui.TEXT);
        } else {
            p12Name.setText("Aucun fichier sélectionné");
            p12Name.setTextColor(Ui.MUTED);
        }
    }

    public void resetStatuses() {
        setStatus(0, Status.PENDING, "Chargement du certificat client…");
        setStatus(1, Status.IDLE, "Connexion HTTPS : en attente");
        setStatus(2, Status.IDLE, "Identifiants : en attente");
        setStatus(3, Status.IDLE, "Jeton API : en attente");
    }

    public void setStatus(int index, Status status, String message) {
        if (index < 0 || index >= statusLines.length) {
            return;
        }
        String marker;
        int color;
        switch (status) {
            case SUCCESS:
                marker = "✓ ";
                color = Ui.GREEN;
                break;
            case FAILURE:
                marker = "✕ ";
                color = Ui.RED;
                break;
            case PENDING:
                marker = "• ";
                color = Ui.CYAN;
                break;
            default:
                marker = "• ";
                color = Ui.MUTED;
        }
        statusLines[index].setText(marker + message);
        statusLines[index].setTextColor(color);
    }

    public void setBusy(boolean busy) {
        chooseP12.setEnabled(!busy);
        test.setEnabled(!busy);
        save.setEnabled(!busy);
        serverUrl.setEnabled(!busy);
        p12Password.setEnabled(!busy);
        username.setEnabled(!busy);
        serverPassword.setEnabled(!busy);
    }

    private FormData readForm() {
        return new FormData(
                serverUrl.getText().toString(),
                p12Password.getText().toString(),
                username.getText().toString(),
                serverPassword.getText().toString());
    }

    private void addField(LinearLayout parent, String label, EditText field) {
        addLabel(parent, label);
        parent.addView(field, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT));
    }

    private void addLabel(LinearLayout parent, String value) {
        TextView label = Ui.text(activity, value, 13, Ui.MUTED);
        label.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        parent.addView(label, Ui.margins(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
                activity, 2, parent.getChildCount() == 0 ? 0 : 14, 2, 6));
    }
}
