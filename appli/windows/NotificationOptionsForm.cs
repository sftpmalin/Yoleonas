namespace YoleoAgent;

internal sealed class NotificationOptionsForm : Form
{
    private readonly ComboBox _interval = new();
    private readonly NumericUpDown _offlineFailures = NumberBox(1, 5);
    private readonly CheckBox _serverOffline = Check("Serveur hors ligne");
    private readonly CheckBox _serverRecovery = Check("Retour du serveur en ligne");
    private readonly CheckBox _cpu = Check("CPU supérieur ou égal à");
    private readonly NumericUpDown _cpuThreshold = NumberBox(1, 100);
    private readonly CheckBox _ram = Check("RAM supérieure ou égale à");
    private readonly NumericUpDown _ramThreshold = NumberBox(1, 100);
    private readonly CheckBox _storage = Check("Un point de montage sélectionné est occupé à");
    private readonly NumericUpDown _storageThreshold = NumberBox(1, 100);
    private readonly CheckBox _mounts = Check("Alerter si un point sélectionné devient un dossier ou disparaît");
    private readonly CheckedListBox _mountList = new();
    private readonly CheckBox _dockerService = Check("Le service Docker s'arrête");
    private readonly CheckBox _dockerContainers = Check("Un conteneur Docker qui tournait s'arrête");
    private readonly CheckBox _samba = Check("Samba ou WSDD s'arrête");
    private readonly CheckBox _tasks = Check("Une nouvelle exécution de tâche échoue");
    private readonly CheckBox _build = Check("Un nouvel élément est à builder ou à envoyer au registre");
    private readonly CheckBox _registryReminder = Check("Rappel mensuel pour nettoyer le registre Docker, le jour");
    private readonly NumericUpDown _registryDay = NumberBox(1, 28);

    public AgentSettings SelectedSettings { get; private set; }

    public NotificationOptionsForm(
        AgentSettings settings,
        Icon appIcon,
        IReadOnlyList<StorageVolume>? availableMounts = null)
    {
        SelectedSettings = settings.Copy();
        Text = "Options de notification Yoleo";
        Icon = appIcon;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.Sizable;
        MaximizeBox = true;
        MinimizeBox = false;
        SizeGripStyle = SizeGripStyle.Show;
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(1100, 720);
        MinimumSize = new Size(900, 620);

        LoadValues(settings);
        PopulateMounts(settings, availableMounts ?? []);

        var tabs = new TabControl
        {
            Dock = DockStyle.Fill,
            Padding = new Point(14, 5),
        };
        tabs.TabPages.Add(CreateGeneralTab());
        tabs.TabPages.Add(CreateStorageTab());
        tabs.TabPages.Add(CreateServicesTab());

        var ok = new Button { Text = "OK", AutoSize = true, MinimumSize = new Size(90, 34) };
        ok.Click += (_, _) => SaveAndClose();
        var cancel = new Button
        {
            Text = "Annuler",
            AutoSize = true,
            MinimumSize = new Size(90, 34),
            DialogResult = DialogResult.Cancel,
        };
        var buttons = new FlowLayoutPanel
        {
            FlowDirection = FlowDirection.RightToLeft,
            AutoSize = true,
            Dock = DockStyle.Fill,
            Padding = new Padding(12, 8, 12, 12),
        };
        buttons.Controls.AddRange([cancel, ok]);

        var root = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 2,
            Padding = new Padding(10),
        };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.Controls.Add(tabs, 0, 0);
        root.Controls.Add(buttons, 0, 1);
        Controls.Add(root);

        AcceptButton = ok;
        CancelButton = cancel;
        WireEnabledStates();
    }

    private void LoadValues(AgentSettings settings)
    {
        _interval.DropDownStyle = ComboBoxStyle.DropDownList;
        _interval.Items.AddRange([1, 5, 10, 15, 30, 60]);
        _interval.SelectedItem = settings.PollIntervalMinutes;
        if (_interval.SelectedIndex < 0)
        {
            _interval.SelectedItem = 5;
        }

        _offlineFailures.Value = settings.OfflineFailureCount;
        _serverOffline.Checked = settings.NotifyServerOffline;
        _serverRecovery.Checked = settings.NotifyServerRecovery;
        _cpu.Checked = settings.NotifyCpu;
        _cpuThreshold.Value = settings.CpuThresholdPercent;
        _ram.Checked = settings.NotifyRam;
        _ramThreshold.Value = settings.RamThresholdPercent;
        _storage.Checked = settings.NotifyStorage;
        _storageThreshold.Value = settings.StorageThresholdPercent;
        _mounts.Checked = settings.NotifyMountFailures;
        _dockerService.Checked = settings.NotifyDockerService;
        _dockerContainers.Checked = settings.NotifyDockerContainers;
        _samba.Checked = settings.NotifySamba;
        _tasks.Checked = settings.NotifyTaskFailures;
        _build.Checked = settings.NotifyBuildPending;
        _registryReminder.Checked = settings.NotifyRegistryCleanup;
        _registryDay.Value = settings.RegistryReminderDay;
    }

    private void PopulateMounts(AgentSettings settings, IReadOnlyList<StorageVolume> availableMounts)
    {
        _mountList.CheckOnClick = true;
        _mountList.Dock = DockStyle.Fill;
        _mountList.IntegralHeight = false;
        _mountList.HorizontalScrollbar = false;
        _mountList.BorderStyle = BorderStyle.FixedSingle;

        var selected = new HashSet<string>(settings.MonitoredMountPaths, StringComparer.OrdinalIgnoreCase);
        var candidates = availableMounts
            .Where(item => !string.IsNullOrWhiteSpace(item.Path))
            .GroupBy(item => item.Path, StringComparer.OrdinalIgnoreCase)
            .Select(group => group.First())
            .OrderBy(item => item.Path, StringComparer.OrdinalIgnoreCase)
            .ToList();

        foreach (var missingPath in selected.Where(path =>
                     candidates.All(item => !string.Equals(item.Path, path, StringComparison.OrdinalIgnoreCase))))
        {
            candidates.Add(new StorageVolume
            {
                Path = missingPath,
                Label = MountName(missingPath),
                Status = "unknown",
                StatusLabel = "Non reçu lors du dernier contrôle",
            });
        }

        foreach (var candidate in candidates.OrderBy(item => item.Path, StringComparer.OrdinalIgnoreCase))
        {
            var choice = new MountChoice(candidate);
            var index = _mountList.Items.Add(choice);
            _mountList.SetItemChecked(index, selected.Contains(choice.Path));
        }
    }

    private TabPage CreateGeneralTab()
    {
        var layout = CreateTable();
        var row = 0;
        AddTitle(layout, ref row, "Fréquence et disponibilité");
        AddControlRow(
            layout,
            ref row,
            new Label { Text = "Une seule vérification complète toutes les", AutoSize = true },
            _interval,
            new Label { Text = "minute(s)", AutoSize = true });
        AddControlRow(
            layout,
            ref row,
            _serverOffline,
            _offlineFailures,
            new Label { Text = "échec(s) consécutif(s)", AutoSize = true });
        AddWide(layout, ref row, _serverRecovery);

        AddTitle(layout, ref row, "Ressources système");
        AddPercentRow(layout, ref row, _cpu, _cpuThreshold);
        AddPercentRow(layout, ref row, _ram, _ramThreshold);
        AddWide(layout, ref row, Note(
            "Les seuils se déclenchent une seule fois, puis doivent redescendre d'au moins 5 % pour se réarmer."));
        return CreateTab("Général", layout);
    }

    private TabPage CreateStorageTab()
    {
        var layout = CreateTable();
        var row = 0;
        AddTitle(layout, ref row, "Occupation des baies et disques");
        AddPercentRow(layout, ref row, _storage, _storageThreshold);
        AddWide(layout, ref row, _mounts);
        AddWide(layout, ref row, Note(
            "Coche les points à surveiller. À chaque contrôle, Yoleo vérifie avec findmnt qu'ils restent de vrais montages et ne deviennent pas de simples dossiers."));

        if (_mountList.Items.Count == 0)
        {
            AddWide(layout, ref row, Note(
                "Aucun point de montage n'a été reçu. Lance Vérifier maintenant, puis rouvre cette fenêtre."));
        }
        else
        {
            AddFillWide(layout, ref row, _mountList);
        }
        return CreateTab("Stockage et baies", layout);
    }

    private TabPage CreateServicesTab()
    {
        var layout = CreateTable();
        var row = 0;
        AddTitle(layout, ref row, "Services");
        AddWide(layout, ref row, _dockerService);
        AddWide(layout, ref row, _dockerContainers);
        AddWide(layout, ref row, _samba);

        AddTitle(layout, ref row, "Travaux et entretien");
        AddWide(layout, ref row, _tasks);
        AddWide(layout, ref row, _build);
        AddControlRow(
            layout,
            ref row,
            _registryReminder,
            _registryDay,
            new Label { Text = "du mois", AutoSize = true });
        AddWide(layout, ref row, Note(
            "Un clic sur chaque notification ouvre directement la page Yoleo correspondante."));
        return CreateTab("Services et travaux", layout);
    }

    private void WireEnabledStates()
    {
        _cpu.CheckedChanged += (_, _) => _cpuThreshold.Enabled = _cpu.Checked;
        _ram.CheckedChanged += (_, _) => _ramThreshold.Enabled = _ram.Checked;
        _storage.CheckedChanged += (_, _) => UpdateStorageEnabledState();
        _mounts.CheckedChanged += (_, _) => UpdateStorageEnabledState();
        _serverOffline.CheckedChanged += (_, _) => _offlineFailures.Enabled = _serverOffline.Checked;
        _registryReminder.CheckedChanged += (_, _) => _registryDay.Enabled = _registryReminder.Checked;
        _cpuThreshold.Enabled = _cpu.Checked;
        _ramThreshold.Enabled = _ram.Checked;
        _offlineFailures.Enabled = _serverOffline.Checked;
        _registryDay.Enabled = _registryReminder.Checked;
        UpdateStorageEnabledState();
    }

    private void UpdateStorageEnabledState()
    {
        _storageThreshold.Enabled = _storage.Checked;
        _mountList.Enabled = _storage.Checked || _mounts.Checked;
    }

    private void SaveAndClose()
    {
        SelectedSettings.PollIntervalMinutes = (int)(_interval.SelectedItem ?? 5);
        SelectedSettings.OfflineFailureCount = (int)_offlineFailures.Value;
        SelectedSettings.NotifyServerOffline = _serverOffline.Checked;
        SelectedSettings.NotifyServerRecovery = _serverRecovery.Checked;
        SelectedSettings.NotifyCpu = _cpu.Checked;
        SelectedSettings.CpuThresholdPercent = (int)_cpuThreshold.Value;
        SelectedSettings.NotifyRam = _ram.Checked;
        SelectedSettings.RamThresholdPercent = (int)_ramThreshold.Value;
        SelectedSettings.NotifyStorage = _storage.Checked;
        SelectedSettings.StorageThresholdPercent = (int)_storageThreshold.Value;
        SelectedSettings.NotifyMountFailures = _mounts.Checked;
        SelectedSettings.MonitoredMountPaths = _mountList.CheckedItems
            .OfType<MountChoice>()
            .Select(choice => choice.Path)
            .ToList();
        SelectedSettings.NotifyDockerService = _dockerService.Checked;
        SelectedSettings.NotifyDockerContainers = _dockerContainers.Checked;
        SelectedSettings.NotifySamba = _samba.Checked;
        SelectedSettings.NotifyTaskFailures = _tasks.Checked;
        SelectedSettings.NotifyBuildPending = _build.Checked;
        SelectedSettings.NotifyRegistryCleanup = _registryReminder.Checked;
        SelectedSettings.RegistryReminderDay = (int)_registryDay.Value;
        SelectedSettings.Normalize();
        DialogResult = DialogResult.OK;
        Close();
    }

    private static TabPage CreateTab(string text, Control content)
    {
        var page = new TabPage(text) { Padding = new Padding(4) };
        page.Controls.Add(content);
        return page;
    }

    private static TableLayoutPanel CreateTable()
    {
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(16),
            ColumnCount = 3,
            AutoScroll = false,
            GrowStyle = TableLayoutPanelGrowStyle.AddRows,
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        return layout;
    }

    private static Label Note(string text) => new()
    {
        Text = text,
        AutoSize = true,
        MaximumSize = new Size(980, 0),
        ForeColor = SystemColors.GrayText,
        Margin = new Padding(3, 12, 3, 8),
    };

    private static CheckBox Check(string text) => new()
    {
        Text = text,
        AutoSize = true,
        Anchor = AnchorStyles.Left,
        Margin = new Padding(3, 5, 3, 5),
    };

    private static NumericUpDown NumberBox(int minimum, int maximum) => new()
    {
        Minimum = minimum,
        Maximum = maximum,
        Width = 64,
        TextAlign = HorizontalAlignment.Right,
        Anchor = AnchorStyles.Left,
    };

    private static void AddTitle(TableLayoutPanel layout, ref int row, string text)
    {
        var label = new Label
        {
            Text = text,
            Font = new Font(SystemFonts.MessageBoxFont!, FontStyle.Bold),
            AutoSize = true,
            Margin = new Padding(3, row == 0 ? 2 : 16, 3, 7),
        };
        AddWide(layout, ref row, label);
    }

    private static void AddPercentRow(TableLayoutPanel layout, ref int row, Control label, Control value) =>
        AddControlRow(layout, ref row, label, value, new Label { Text = "%", AutoSize = true });

    private static void AddControlRow(
        TableLayoutPanel layout,
        ref int row,
        Control first,
        Control second,
        Control third)
    {
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        first.Anchor = AnchorStyles.Left;
        second.Anchor = AnchorStyles.Left;
        third.Anchor = AnchorStyles.Left;
        first.Margin = new Padding(3, 5, 8, 5);
        second.Margin = new Padding(3, 4, 5, 4);
        third.Margin = new Padding(3, 7, 3, 5);
        layout.Controls.Add(first, 0, row);
        layout.Controls.Add(second, 1, row);
        layout.Controls.Add(third, 2, row);
        row++;
    }

    private static void AddWide(TableLayoutPanel layout, ref int row, Control control)
    {
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.Controls.Add(control, 0, row);
        layout.SetColumnSpan(control, 3);
        row++;
    }

    private static void AddFillWide(TableLayoutPanel layout, ref int row, Control control)
    {
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        control.Dock = DockStyle.Fill;
        layout.Controls.Add(control, 0, row);
        layout.SetColumnSpan(control, 3);
        row++;
    }

    private static string MountName(string path)
    {
        var clean = (path ?? "").Trim().TrimEnd('/');
        var slash = clean.LastIndexOf('/');
        return slash >= 0 && slash < clean.Length - 1 ? clean[(slash + 1)..] : clean;
    }

    private sealed class MountChoice
    {
        private readonly StorageVolume _mount;

        public MountChoice(StorageVolume mount)
        {
            _mount = mount;
        }

        public string Path => _mount.Path;

        public override string ToString()
        {
            var name = string.IsNullOrWhiteSpace(_mount.Label) ? MountName(Path) : _mount.Label;
            var state = _mount.IsMount
                ? $"Monté — {_mount.Percent:0.#} %"
                : string.IsNullOrWhiteSpace(_mount.StatusLabel) ? "État inconnu" : _mount.StatusLabel;
            var device = _mount.IsMount && !string.IsNullOrWhiteSpace(_mount.Source)
                ? $" — {_mount.Source} ({_mount.FileSystem})"
                : "";
            return $"{name} — {Path} — {state}{device}";
        }
    }
}
