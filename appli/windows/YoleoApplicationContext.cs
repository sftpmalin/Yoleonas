using System.Diagnostics;
using System.Reflection;

namespace YoleoAgent;

internal sealed class YoleoApplicationContext : ApplicationContext
{
    private readonly Icon _appIcon;
    private readonly NotifyIcon _notifyIcon;
    private readonly ContextMenuStrip _menu;
    private readonly System.Windows.Forms.Timer _pollTimer = new();
    private readonly Queue<AgentNotification> _notificationQueue = new();

    private AgentSettings _settings;
    private SecretSettings _secrets;
    private MonitoringState _state;
    private MonitoringEngine _monitoringEngine;
    private YoleoApiClient? _client;
    private bool _checking;
    private bool _authenticationProblemNotified;
    private bool _hasSavedConfiguration;
    private MonitoringSnapshot? _lastSnapshot;
    private AgentNotification? _activeNotification;

    public YoleoApplicationContext()
    {
        _settings = RegistryStore.LoadSettings();
        _secrets = RegistryStore.LoadSecrets();
        _hasSavedConfiguration = RegistryStore.HasSavedConfiguration();
        _state = RegistryStore.LoadState();
        _monitoringEngine = new MonitoringEngine(_settings, _state);
        _appIcon = LoadApplicationIcon();

        _menu = new ContextMenuStrip();
        var openItem = new ToolStripMenuItem("Ouvrir Yoleo");
        openItem.Font = new Font(openItem.Font, FontStyle.Bold);
        openItem.Click += (_, _) => OpenWebsite();

        var authenticationItem = new ToolStripMenuItem("Authentification…");
        authenticationItem.Click += async (_, _) => await ShowAuthenticationAsync();

        var optionsItem = new ToolStripMenuItem("Options de notification…");
        optionsItem.Click += async (_, _) => await ShowNotificationOptionsAsync();

        var checkItem = new ToolStripMenuItem("Vérifier maintenant");
        checkItem.Click += async (_, _) => await CheckNowAsync(showSuccess: true);

        var quitItem = new ToolStripMenuItem("Quitter");
        quitItem.Click += (_, _) => ExitThread();

        _menu.Items.AddRange([
            openItem,
            new ToolStripSeparator(),
            authenticationItem,
            optionsItem,
            checkItem,
            new ToolStripSeparator(),
            quitItem,
        ]);

        _notifyIcon = new NotifyIcon
        {
            Icon = _appIcon,
            Text = "Yoleo Agent — démarrage",
            ContextMenuStrip = _menu,
            Visible = true,
        };
        _notifyIcon.DoubleClick += (_, _) => OpenWebsite();
        _notifyIcon.BalloonTipClicked += (_, _) =>
            OpenWebsite(_activeNotification?.DestinationPath ?? "/index");
        _notifyIcon.BalloonTipClosed += (_, _) => CompleteActiveNotification();

        _pollTimer.Tick += async (_, _) => await CheckNowAsync(showSuccess: false);
        ApplyPollingInterval();
        _pollTimer.Start();

        Application.Idle += StartOnFirstIdle;
    }

    protected override void ExitThreadCore()
    {
        Application.Idle -= StartOnFirstIdle;
        _pollTimer.Stop();
        _pollTimer.Dispose();
        _client?.Dispose();
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
        _menu.Dispose();
        _appIcon.Dispose();
        base.ExitThreadCore();
    }

    private async void StartOnFirstIdle(object? sender, EventArgs eventArgs)
    {
        Application.Idle -= StartOnFirstIdle;
        if (!_hasSavedConfiguration)
        {
            await ShowAuthenticationAsync();
            return;
        }
        await CheckNowAsync(showSuccess: false);
    }

    private bool IsReady() =>
        _settings.HasConnectionConfiguration &&
        File.Exists(_settings.P12Path) &&
        !string.IsNullOrWhiteSpace(_secrets.AccessToken);

    private async Task ShowAuthenticationAsync()
    {
        _pollTimer.Stop();
        var oldSettings = _settings.Copy();
        var oldSecrets = _secrets.Copy();

        using var form = new AuthenticationForm(_settings, _secrets, _appIcon);
        var result = form.ShowDialog();
        if (result == DialogResult.OK)
        {
            try
            {
                if (!string.IsNullOrWhiteSpace(oldSecrets.AccessToken) &&
                    !string.Equals(oldSecrets.AccessToken, form.SelectedSecrets.AccessToken, StringComparison.Ordinal) &&
                    oldSettings.HasConnectionConfiguration &&
                    File.Exists(oldSettings.P12Path))
                {
                    using var oldClient = new YoleoApiClient(oldSettings, oldSecrets.P12Password);
                    using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                    await oldClient.LogoutAsync(oldSecrets.AccessToken, cancellation.Token);
                }
            }
            catch
            {
                // Le nouveau jeton est valide. L'ancien expirera côté serveur
                // même si sa révocation immédiate est momentanément impossible.
            }

            _settings = form.SelectedSettings;
            _secrets = form.SelectedSecrets;
            RegistryStore.SaveSettings(_settings);
            RegistryStore.SaveSecrets(_secrets);
            _hasSavedConfiguration = true;

            _state = new MonitoringState();
            RegistryStore.SaveState(_state);
            _monitoringEngine = new MonitoringEngine(_settings, _state);
            ResetClient();
            _lastSnapshot = null;
            _authenticationProblemNotified = false;
            ApplyPollingInterval();

            var automaticAuthenticationAttempted = false;
            if (string.IsNullOrWhiteSpace(_secrets.AccessToken) &&
                !string.IsNullOrWhiteSpace(form.ServerPassword))
            {
                automaticAuthenticationAttempted = true;
                await TryAuthenticateAfterSaveAsync(form.ServerPassword);
            }

            if (IsReady())
            {
                await CheckNowAsync(showSuccess: true);
            }
            else if (!automaticAuthenticationAttempted)
            {
                SetTrayText("Yoleo Agent — configuration enregistrée, connexion non établie");
                ShowNotification(new AgentNotification(
                    "Configuration Yoleo enregistrée",
                    "Le test est facultatif. La connexion n'est pas établie pour le moment.",
                    NotificationLevel.Warning));
            }
        }

        _pollTimer.Start();
    }

    private async Task<bool> TryAuthenticateAfterSaveAsync(string password)
    {
        YoleoApiClient? authenticatedClient = null;
        SetTrayText("Yoleo Agent — connexion en cours");

        try
        {
            authenticatedClient = new YoleoApiClient(_settings, _secrets.P12Password);
            var authentication = await authenticatedClient.LoginAsync(_settings.Username, password);
            await authenticatedClient.GetIdentityAsync(authentication.AccessToken);

            _secrets.AccessToken = authentication.AccessToken;
            RegistryStore.SaveSecrets(_secrets);
            ResetClient();
            _client = authenticatedClient;
            authenticatedClient = null;
            return true;
        }
        catch (Exception exception)
        {
            _secrets.AccessToken = "";
            RegistryStore.SaveSecrets(_secrets);
            ResetClient();
            SetTrayText("Yoleo Agent — configuration enregistrée, connexion échouée");
            ShowNotification(new AgentNotification(
                "Connexion Yoleo impossible",
                "La configuration a bien été enregistrée. " + exception.Message,
                NotificationLevel.Warning));
            return false;
        }
        finally
        {
            authenticatedClient?.Dispose();
        }
    }

    private async Task ShowNotificationOptionsAsync()
    {
        if (_lastSnapshot is null && IsReady())
        {
            try
            {
                _client ??= new YoleoApiClient(_settings, _secrets.P12Password);
                _lastSnapshot = await _client.GetMonitoringSnapshotAsync(_secrets.AccessToken);
            }
            catch
            {
                // La fenêtre reste accessible avec les chemins déjà enregistrés.
            }
        }

        using var form = new NotificationOptionsForm(
            _settings,
            _appIcon,
            _lastSnapshot?.Storage.Mounts ?? []);
        if (form.ShowDialog() != DialogResult.OK)
        {
            return;
        }

        _settings = form.SelectedSettings;
        RegistryStore.SaveSettings(_settings);
        _monitoringEngine.UpdateSettings(_settings);
        RegistryStore.SaveState(_state);
        ApplyPollingInterval();
        await CheckNowAsync(showSuccess: false);
    }

    private async Task CheckNowAsync(bool showSuccess)
    {
        if (_checking)
        {
            return;
        }
        if (!IsReady())
        {
            SetTrayText("Yoleo Agent — authentification requise");
            if (showSuccess)
            {
                await ShowAuthenticationAsync();
            }
            return;
        }

        _checking = true;
        SetTrayText("Yoleo Agent — vérification en cours");
        try
        {
            _client ??= new YoleoApiClient(_settings, _secrets.P12Password);
            var snapshot = await _client.GetMonitoringSnapshotAsync(_secrets.AccessToken);
            _lastSnapshot = snapshot;
            foreach (var notification in _monitoringEngine.EvaluateSuccess(snapshot, DateTime.Now))
            {
                ShowNotification(notification);
            }
            RegistryStore.SaveState(_state);
            _authenticationProblemNotified = false;
            SetTrayText($"Yoleo Agent — OK à {DateTime.Now:HH:mm}");

            if (showSuccess)
            {
                ShowNotification(new AgentNotification(
                    "Vérification Yoleo terminée",
                    $"Serveur en ligne — CPU {snapshot.System.CpuPercent:0.#} %, RAM {snapshot.System.RamPercent:0.#} %.",
                    NotificationLevel.Info));
            }
        }
        catch (ApiException exception) when (exception.Code == "authentication_required")
        {
            SetTrayText("Yoleo Agent — authentification expirée");
            if (!_authenticationProblemNotified)
            {
                ShowNotification(new AgentNotification(
                    "Authentification Yoleo requise",
                    "Le jeton a expiré ou a été révoqué. Ouvre Authentification… depuis l'icône.",
                    NotificationLevel.Warning));
                _authenticationProblemNotified = true;
            }
            ResetClient();
        }
        catch (Exception exception)
        {
            foreach (var notification in _monitoringEngine.EvaluateFailure(exception.Message))
            {
                ShowNotification(notification);
            }
            RegistryStore.SaveState(_state);
            SetTrayText("Yoleo Agent — serveur injoignable");
            ResetClient();
        }
        finally
        {
            _checking = false;
        }
    }

    private void ApplyPollingInterval()
    {
        _settings.Normalize();
        _pollTimer.Interval = checked((int)TimeSpan.FromMinutes(_settings.PollIntervalMinutes).TotalMilliseconds);
    }

    private void ResetClient()
    {
        _client?.Dispose();
        _client = null;
    }

    private void OpenWebsite(string relativePath = "/index")
    {
        try
        {
            Process.Start(new ProcessStartInfo(YoleoApiClient.WebsiteUrl(_settings.ServerUrl, relativePath))
            {
                UseShellExecute = true,
            });
        }
        catch (Exception exception)
        {
            MessageBox.Show(
                "Impossible d'ouvrir l'interface Yoleo.\n\n" + exception.Message,
                "Yoleo Agent",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
        }
    }

    private void ShowNotification(AgentNotification notification)
    {
        _notificationQueue.Enqueue(notification);
        ShowNextNotification();
    }

    private void ShowNextNotification()
    {
        if (_activeNotification is not null || _notificationQueue.Count == 0)
        {
            return;
        }

        var notification = _notificationQueue.Dequeue();
        _activeNotification = notification;
        _notifyIcon.BalloonTipTitle = notification.Title;
        _notifyIcon.BalloonTipText = notification.Message;
        _notifyIcon.BalloonTipIcon = notification.Level switch
        {
            NotificationLevel.Error => ToolTipIcon.Error,
            NotificationLevel.Warning => ToolTipIcon.Warning,
            _ => ToolTipIcon.Info,
        };
        _notifyIcon.ShowBalloonTip(8_000);
    }

    private void CompleteActiveNotification()
    {
        if (_activeNotification is null)
        {
            return;
        }
        _activeNotification = null;
        ShowNextNotification();
    }

    private void SetTrayText(string text)
    {
        _notifyIcon.Text = text.Length <= 63 ? text : text[..63];
    }

    private static Icon LoadApplicationIcon()
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("YoleoAgent.App.ico")
            ?? throw new InvalidOperationException("L'icône Yoleo intégrée est introuvable.");
        using var source = new Icon(stream);
        return (Icon)source.Clone();
    }
}
