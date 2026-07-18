namespace YoleoAgent;

internal static class SelfTest
{
    public static int Run()
    {
        try
        {
            AssertBareDistributionDefaults();
            AssertAuthenticationTestIsOptional();

            var settings = new AgentSettings
            {
                OfflineFailureCount = 2,
                CpuThresholdPercent = 90,
                NotifyRegistryCleanup = false,
                MonitoredMountPaths = ["/mnt/media0"],
            };
            var state = new MonitoringState();
            var engine = new MonitoringEngine(settings, state);
            var snapshot = HealthySnapshot();
            AssertNotificationOptionsTabs(settings, snapshot.Storage.Mounts);

            Assert(engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14)).Count == 0, "baseline");

            snapshot.System.CpuPercent = 95;
            var cpuNotifications = engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14));
            Assert(cpuNotifications.Count == 1, "cpu crossing");
            Assert(cpuNotifications[0].DestinationPath == "/index", "cpu destination");
            Assert(engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14)).Count == 0, "cpu duplicate");
            snapshot.System.CpuPercent = 70;
            engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14));
            snapshot.System.CpuPercent = 95;
            Assert(engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14)).Count == 1, "cpu rearmed");

            snapshot.System.CpuPercent = 20;
            snapshot.Docker.Containers[0].State = "exited";
            var dockerNotifications = engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14));
            Assert(dockerNotifications.Count == 1, "docker transition");
            Assert(dockerNotifications[0].DestinationPath == "/docker/containers", "docker destination");
            Assert(engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14)).Count == 0, "docker duplicate");

            snapshot.Tasks[0].Result = "Erreur";
            snapshot.Tasks[0].Status = "Erreur";
            snapshot.Tasks[0].UpdatedAt = "2026-07-14 12:00:00";
            var taskNotifications = engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14));
            Assert(taskNotifications.Count == 1, "task failure");
            Assert(taskNotifications[0].DestinationPath == "/system/task", "task destination");
            Assert(engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14)).Count == 0, "task duplicate");

            snapshot.Storage.Mounts[0].IsMount = false;
            snapshot.Storage.Mounts[0].Status = "folder";
            snapshot.Storage.Mounts[0].StatusLabel = "Dossier";
            var mountNotifications = engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14));
            Assert(mountNotifications.Count == 1, "mount became folder");
            Assert(mountNotifications[0].Title.Contains("media0"), "mount title");
            Assert(mountNotifications[0].DestinationPath == "/disk/general", "mount destination");
            Assert(engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14)).Count == 0, "mount duplicate");
            snapshot.Storage.Mounts[0].IsMount = true;
            snapshot.Storage.Mounts[0].Status = "ok";
            engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14));
            snapshot.Storage.Mounts[0].IsMount = false;
            snapshot.Storage.Mounts[0].Status = "folder";
            Assert(engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14)).Count == 1, "mount rearmed");

            Assert(engine.EvaluateFailure("timeout").Count == 0, "offline debounce one");
            Assert(engine.EvaluateFailure("timeout").Count == 1, "offline debounce two");
            Assert(engine.EvaluateSuccess(snapshot, new DateTime(2026, 7, 14)).Count == 1, "online recovery");

            var registrySettings = settings.Copy();
            registrySettings.NotifyRegistryCleanup = true;
            registrySettings.RegistryReminderDay = 1;
            var registryEngine = new MonitoringEngine(registrySettings, new MonitoringState());
            var registryNotifications = registryEngine.EvaluateSuccess(HealthySnapshot(), new DateTime(2026, 7, 14));
            Assert(registryNotifications.Count == 1, "registry reminder");
            Assert(registryNotifications[0].DestinationPath == "/build/registry", "registry destination");
            return 0;
        }
        catch
        {
            return 1;
        }
    }

    private static void AssertBareDistributionDefaults()
    {
        var settings = new AgentSettings();
        settings.Normalize();
        Assert(string.IsNullOrEmpty(settings.ServerUrl), "bare default server URL");
        Assert(string.IsNullOrEmpty(settings.P12Path), "bare default P12 path");
        Assert(string.IsNullOrEmpty(settings.Username), "bare default username");
        var secrets = new SecretSettings();
        Assert(string.IsNullOrEmpty(secrets.P12Password), "bare default P12 password");
        Assert(string.IsNullOrEmpty(secrets.AccessToken), "bare default access token");
    }

    private static void AssertAuthenticationTestIsOptional()
    {
        using var icon = (Icon)SystemIcons.Application.Clone();
        using var form = new AuthenticationForm(new AgentSettings(), new SecretSettings(), icon);
        var okButton = Descendants(form)
            .OfType<Button>()
            .SingleOrDefault(button => string.Equals(button.Text, "OK", StringComparison.Ordinal));

        Assert(okButton is not null, "authentication OK button present");
        Assert(okButton!.Enabled, "authentication OK button enabled without test");
        Assert(ReferenceEquals(form.AcceptButton, okButton), "authentication OK is default action");
        Assert(form.FormBorderStyle == FormBorderStyle.Sizable, "authentication window resizable");
        Assert(form.ClientSize.Width >= 1000 && form.ClientSize.Height >= 700, "authentication window size");
        Assert(
            Descendants(form).OfType<TableLayoutPanel>().All(layout => !layout.AutoScroll),
            "authentication window has no artificial scroll");
    }

    private static IEnumerable<Control> Descendants(Control parent)
    {
        foreach (Control child in parent.Controls)
        {
            yield return child;
            foreach (var descendant in Descendants(child))
            {
                yield return descendant;
            }
        }
    }

    private static void AssertNotificationOptionsTabs(
        AgentSettings settings,
        IReadOnlyList<StorageVolume> mounts)
    {
        using var icon = (Icon)SystemIcons.Application.Clone();
        using var form = new NotificationOptionsForm(settings, icon, mounts);
        var tabs = Descendants(form).OfType<TabControl>().SingleOrDefault();
        Assert(tabs is not null, "notification tabs present");
        Assert(tabs!.TabPages.Count == 3, "notification tab count");
        Assert(tabs.TabPages.Cast<TabPage>().Any(page => page.Text == "Stockage et baies"), "storage tab present");
        Assert(form.FormBorderStyle == FormBorderStyle.Sizable, "notification window resizable");
        Assert(form.ClientSize.Width >= 1000 && form.ClientSize.Height >= 700, "notification window size");
        Assert(
            Descendants(form).OfType<TableLayoutPanel>().All(layout => !layout.AutoScroll),
            "notification window has no artificial scroll");
        Assert(
            Descendants(form).OfType<CheckedListBox>().All(list => !list.HorizontalScrollbar),
            "mount list has no horizontal scrollbar");
    }

    private static MonitoringSnapshot HealthySnapshot() => new()
    {
        System = new SystemSnapshot { CpuPercent = 20, RamPercent = 30 },
        Storage = new StorageSnapshot
        {
            Main = new StorageVolume { Path = "/mnt/user", Percent = 50 },
            Mounts =
            [
                new StorageVolume
                {
                    Path = "/mnt/media0",
                    Label = "media0",
                    Exists = true,
                    IsMount = true,
                    Source = "/dev/sdb1",
                    FileSystem = "xfs",
                    Percent = 50,
                    Status = "ok",
                    StatusLabel = "OK",
                    Ok = true,
                },
            ],
            MountState = "ok",
            MountLabel = "OK",
        },
        Docker = new DockerSnapshot
        {
            Available = true,
            Service = new DockerServiceSnapshot { Active = true, State = "active", Label = "Actif" },
            Containers = [new DockerContainerSnapshot { Id = "abc", Name = "media", State = "running" }],
        },
        Samba = new SambaSnapshot { Available = true, Ok = true },
        Tasks =
        [
            new TaskSnapshot
            {
                Id = 1,
                Title = "Sauvegarde",
                Result = "Succès",
                Status = "Terminé",
                UpdatedAt = "2026-07-14 10:00:00",
            },
        ],
        Build = new BuildSnapshot { Available = true, ToBuild = 0, ToPush = 0 },
    };

    private static void Assert(bool condition, string name)
    {
        if (!condition)
        {
            throw new InvalidOperationException("Self-test failed: " + name);
        }
    }
}
