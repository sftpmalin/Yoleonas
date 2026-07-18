namespace YoleoAgent;

internal sealed class AuthenticationForm : Form
{
    private readonly TextBox _serverUrl = new();
    private readonly TextBox _p12Path = new();
    private readonly TextBox _p12Password = new();
    private readonly TextBox _username = new();
    private readonly TextBox _password = new();
    private readonly Button _browseButton = new();
    private readonly Button _testButton = new();
    private readonly Button _okButton = new();
    private readonly Button _cancelButton = new();
    private readonly Label _p12Status = StatusLabel("Certificat P12 : non testé");
    private readonly Label _httpsStatus = StatusLabel("Connexion HTTPS : non testée");
    private readonly Label _credentialsStatus = StatusLabel("Authentification serveur : non testée");
    private readonly Label _tokenStatus = StatusLabel("Jeton API : non testé");
    private readonly CancellationTokenSource _closingCancellation = new();
    private readonly string _initialConnectionFingerprint;
    private readonly string _initialAccessToken;

    private bool _updating;
    private string _testedFingerprint = "";

    public AgentSettings SelectedSettings { get; private set; }
    public SecretSettings SelectedSecrets { get; private set; }
    public string ServerPassword { get; private set; } = "";

    public AuthenticationForm(AgentSettings settings, SecretSettings secrets, Icon appIcon)
    {
        SelectedSettings = settings.Copy();
        SelectedSecrets = secrets.Copy();
        _initialConnectionFingerprint = ConnectionFingerprint(settings, secrets.P12Password);
        _initialAccessToken = secrets.AccessToken;

        Text = "Authentification Yoleo";
        Icon = appIcon;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.Sizable;
        MaximizeBox = true;
        MinimizeBox = false;
        SizeGripStyle = SizeGripStyle.Show;
        ShowInTaskbar = true;
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(1050, 720);
        MinimumSize = new Size(850, 620);

        _serverUrl.Text = settings.ServerUrl;
        _p12Path.Text = settings.P12Path;
        _p12Password.Text = secrets.P12Password;
        _p12Password.UseSystemPasswordChar = true;
        _username.Text = settings.Username;
        _password.UseSystemPasswordChar = true;

        _browseButton.Text = "Parcourir…";
        _browseButton.AutoSize = true;
        _browseButton.Click += BrowseP12;

        _testButton.Text = "Tester";
        _testButton.AutoSize = true;
        _testButton.MinimumSize = new Size(110, 36);
        _testButton.Click += async (_, _) => await TestConnectionAsync();

        _okButton.Text = "OK";
        _okButton.AutoSize = true;
        _okButton.MinimumSize = new Size(90, 36);
        _okButton.Click += (_, _) =>
        {
            CaptureSelection();
            DialogResult = DialogResult.OK;
            Close();
        };

        _cancelButton.Text = "Annuler";
        _cancelButton.AutoSize = true;
        _cancelButton.MinimumSize = new Size(90, 36);
        _cancelButton.DialogResult = DialogResult.Cancel;

        AcceptButton = _okButton;
        CancelButton = _cancelButton;

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(18, 18, 18, 6),
            ColumnCount = 3,
            RowCount = 7,
            AutoScroll = false,
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 170));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

        AddRow(layout, 0, "Adresse HTTPS du serveur", _serverUrl, span: 2);
        AddRow(layout, 1, "Fichier P12", _p12Path, _browseButton);
        AddRow(layout, 2, "Mot de passe du P12", _p12Password, span: 2);
        AddRow(layout, 3, "Nom d'utilisateur", _username, span: 2);
        AddRow(layout, 4, "Mot de passe serveur", _password, span: 2);

        var passwordNote = new Label
        {
            Text = "Le test est facultatif : OK enregistre toujours la configuration. " +
                   "Le mot de passe du compte serveur sert uniquement à créer le jeton et n'est pas enregistré. " +
                   "Le jeton et le mot de passe P12 sont chiffrés par Windows (DPAPI).",
            AutoSize = true,
            MaximumSize = new Size(860, 0),
            ForeColor = SystemColors.GrayText,
            Margin = new Padding(3, 8, 3, 8),
        };
        layout.Controls.Add(passwordNote, 0, 5);
        layout.SetColumnSpan(passwordNote, 3);
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var statusPanel = new FlowLayoutPanel
        {
            FlowDirection = FlowDirection.TopDown,
            WrapContents = false,
            AutoSize = true,
            Dock = DockStyle.Fill,
            Padding = new Padding(10),
        };
        statusPanel.Controls.AddRange([_p12Status, _httpsStatus, _credentialsStatus, _tokenStatus]);
        var statusGroup = new GroupBox
        {
            Text = "Résultat du test",
            Dock = DockStyle.Fill,
            AutoSize = true,
        };
        statusGroup.Controls.Add(statusPanel);
        layout.Controls.Add(statusGroup, 0, 6);
        layout.SetColumnSpan(statusGroup, 3);
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

        var buttons = new FlowLayoutPanel
        {
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill,
            AutoSize = true,
            Padding = new Padding(18, 8, 18, 18),
        };
        buttons.Controls.AddRange([_cancelButton, _okButton, _testButton]);

        var windowLayout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 2,
        };
        windowLayout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        windowLayout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        windowLayout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        windowLayout.Controls.Add(layout, 0, 0);
        windowLayout.Controls.Add(buttons, 0, 1);

        Controls.Add(windowLayout);

        foreach (var textBox in new[] { _serverUrl, _p12Path, _p12Password, _username, _password })
        {
            textBox.Dock = DockStyle.Fill;
            textBox.TextChanged += (_, _) => InvalidateTest();
        }

        FormClosing += (_, _) => _closingCancellation.Cancel();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _closingCancellation.Dispose();
        }
        base.Dispose(disposing);
    }

    private async Task TestConnectionAsync()
    {
        var stage = 0;
        SetBusy(true);
        ResetStatus();

        try
        {
            var candidate = SelectedSettings.Copy();
            candidate.ServerUrl = YoleoApiClient.NormalizeServerUrl(_serverUrl.Text);
            candidate.P12Path = _p12Path.Text.Trim();
            candidate.Username = _username.Text.Trim();
            candidate.Normalize();

            if (string.IsNullOrWhiteSpace(_password.Text))
            {
                throw new InvalidOperationException("Saisis le mot de passe du compte serveur.");
            }

            using var client = new YoleoApiClient(candidate, _p12Password.Text);
            stage = 1;
            SetSuccess(_p12Status, "Certificat P12 : OK — " + client.CertificateSummary);

            await client.GetHealthAsync(_closingCancellation.Token);
            stage = 2;
            SetSuccess(_httpsStatus, "Connexion HTTPS / mTLS : OK");

            var authentication = await client.LoginAsync(
                candidate.Username,
                _password.Text,
                _closingCancellation.Token);
            stage = 3;
            SetSuccess(_credentialsStatus, "Authentification au serveur : OK");

            var identity = await client.GetIdentityAsync(
                authentication.AccessToken,
                _closingCancellation.Token);
            stage = 4;
            SetSuccess(_tokenStatus, $"Jeton API : OK — utilisateur {identity.Username}");

            SelectedSettings = candidate;
            SelectedSecrets = new SecretSettings
            {
                P12Password = _p12Password.Text,
                AccessToken = authentication.AccessToken,
            };
            _testedFingerprint = CurrentFingerprint();
        }
        catch (OperationCanceledException) when (_closingCancellation.IsCancellationRequested)
        {
            // La fenêtre se ferme : aucun message supplémentaire.
        }
        catch (Exception exception)
        {
            var target = stage switch
            {
                0 => _p12Status,
                1 => _httpsStatus,
                2 => _credentialsStatus,
                _ => _tokenStatus,
            };
            SetFailure(target, "Échec : " + InnermostMessage(exception));
            _testedFingerprint = "";
        }
        finally
        {
            SetBusy(false);
        }
    }

    private void BrowseP12(object? sender, EventArgs eventArgs)
    {
        using var dialog = new OpenFileDialog
        {
            Title = "Choisir le certificat client Yoleo",
            Filter = "Certificats PKCS#12 (*.p12;*.pfx)|*.p12;*.pfx|Tous les fichiers (*.*)|*.*",
            CheckFileExists = true,
            Multiselect = false,
        };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            _p12Path.Text = dialog.FileName;
        }
    }

    private void InvalidateTest()
    {
        if (_updating)
        {
            return;
        }
        _testedFingerprint = "";
    }

    private void CaptureSelection()
    {
        var candidate = SelectedSettings.Copy();
        candidate.ServerUrl = (_serverUrl.Text ?? "").Trim().TrimEnd('/');
        candidate.P12Path = _p12Path.Text.Trim();
        candidate.Username = _username.Text.Trim();
        candidate.Normalize();

        var connectionFingerprint = ConnectionFingerprint(candidate, _p12Password.Text);
        var testStillMatches = _testedFingerprint == CurrentFingerprint();
        var accessToken = testStillMatches
            ? SelectedSecrets.AccessToken
            : connectionFingerprint == _initialConnectionFingerprint
                ? _initialAccessToken
                : "";

        SelectedSettings = candidate;
        SelectedSecrets = new SecretSettings
        {
            P12Password = _p12Password.Text,
            AccessToken = accessToken,
        };
        ServerPassword = _password.Text;
    }

    private static string ConnectionFingerprint(AgentSettings settings, string p12Password) => string.Join(
        "\u001f",
        (settings.ServerUrl ?? "").Trim().TrimEnd('/'),
        (settings.P12Path ?? "").Trim(),
        p12Password,
        (settings.Username ?? "").Trim());

    private static string InnermostMessage(Exception exception)
    {
        var current = exception;
        while (current.InnerException is not null)
        {
            current = current.InnerException;
        }
        return current.Message;
    }

    private string CurrentFingerprint() => string.Join(
        "\u001f",
        _serverUrl.Text.Trim(),
        _p12Path.Text.Trim(),
        _p12Password.Text,
        _username.Text.Trim(),
        _password.Text);

    private void SetBusy(bool busy)
    {
        UseWaitCursor = busy;
        _testButton.Enabled = !busy;
        _browseButton.Enabled = !busy;
        _cancelButton.Enabled = !busy;
        foreach (var control in new Control[] { _serverUrl, _p12Path, _p12Password, _username, _password })
        {
            control.Enabled = !busy;
        }
    }

    private void ResetStatus()
    {
        _updating = true;
        try
        {
            SetPending(_p12Status, "Certificat P12 : vérification en cours…");
            SetPending(_httpsStatus, "Connexion HTTPS : en attente");
            SetPending(_credentialsStatus, "Authentification serveur : en attente");
            SetPending(_tokenStatus, "Jeton API : en attente");
        }
        finally
        {
            _updating = false;
        }
    }

    private static Label StatusLabel(string text) => new()
    {
        Text = "• " + text,
        AutoSize = true,
        Margin = new Padding(4, 4, 4, 4),
    };

    private static void SetPending(Label label, string text)
    {
        label.ForeColor = SystemColors.GrayText;
        label.Text = "• " + text;
    }

    private static void SetSuccess(Label label, string text)
    {
        label.ForeColor = Color.ForestGreen;
        label.Text = "✓ " + text;
    }

    private static void SetFailure(Label label, string text)
    {
        label.ForeColor = Color.Firebrick;
        label.Text = "✗ " + text;
    }

    private static void AddRow(
        TableLayoutPanel layout,
        int row,
        string label,
        Control control,
        Control? lastControl = null,
        int span = 1)
    {
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var textLabel = new Label
        {
            Text = label,
            AutoSize = true,
            Anchor = AnchorStyles.Left,
            Margin = new Padding(3, 8, 8, 8),
        };
        control.Margin = new Padding(3, 5, 3, 5);
        layout.Controls.Add(textLabel, 0, row);
        layout.Controls.Add(control, 1, row);
        if (span > 1)
        {
            layout.SetColumnSpan(control, span);
        }
        else if (lastControl is not null)
        {
            lastControl.Margin = new Padding(6, 4, 3, 4);
            layout.Controls.Add(lastControl, 2, row);
        }
    }
}
