namespace YoleoAgent;

internal sealed class MonitoringEngine
{
    private const double HysteresisPercent = 5.0;
    private const string HomePage = "/index";
    private const string DiskPage = "/disk/general";
    private const string DockerPage = "/docker/containers";
    private const string SambaPage = "/partage/samba";
    private const string TasksPage = "/system/task";
    private const string BuildPage = "/build/main";
    private const string RegistryPage = "/build/registry";

    private AgentSettings _settings;
    private readonly MonitoringState _state;

    public MonitoringEngine(AgentSettings settings, MonitoringState state)
    {
        _settings = settings;
        _state = state;
    }

    public void UpdateSettings(AgentSettings settings)
    {
        _settings = settings;
        _state.CpuHigh = false;
        _state.RamHigh = false;
        _state.HighStoragePaths.Clear();
        var selected = new HashSet<string>(_settings.MonitoredMountPaths, StringComparer.OrdinalIgnoreCase);
        _state.MountStates = _state.MountStates
            .Where(pair => selected.Contains(pair.Key))
            .ToDictionary(pair => pair.Key, pair => pair.Value, StringComparer.OrdinalIgnoreCase);
    }

    public IReadOnlyList<AgentNotification> EvaluateFailure(string reason)
    {
        var notifications = new List<AgentNotification>();
        _state.ConsecutiveFailures++;

        if (_settings.NotifyServerOffline &&
            !_state.OfflineNotified &&
            _state.ConsecutiveFailures >= _settings.OfflineFailureCount)
        {
            _state.OfflineNotified = true;
            notifications.Add(new AgentNotification(
                "Serveur Yoleo hors ligne",
                $"Le contrôle a échoué {_state.ConsecutiveFailures} fois. {Trim(reason, 180)}",
                NotificationLevel.Error,
                HomePage));
        }

        return notifications;
    }

    public IReadOnlyList<AgentNotification> EvaluateSuccess(MonitoringSnapshot snapshot, DateTime now)
    {
        var notifications = new List<AgentNotification>();

        if (_state.OfflineNotified && _settings.NotifyServerRecovery)
        {
            notifications.Add(new AgentNotification(
                "Serveur Yoleo de nouveau en ligne",
                "La connexion HTTPS, le certificat P12 et l'API répondent à nouveau.",
                NotificationLevel.Info,
                HomePage));
        }

        _state.WasOnline = true;
        _state.OfflineNotified = false;
        _state.ConsecutiveFailures = 0;
        _state.LastSuccessfulCheckUtc = DateTime.UtcNow.ToString("O");

        if (!_state.HasBaseline)
        {
            SeedTransitionHistory(snapshot);
            _state.HasBaseline = true;
        }

        if (!HasSectionError(snapshot, "system"))
        {
            EvaluateCpu(snapshot.System.CpuPercent, notifications);
            EvaluateRam(snapshot.System.RamPercent, notifications);
            EvaluateBuild(snapshot.Build, notifications);
        }
        if (!HasSectionError(snapshot, "storage"))
        {
            EvaluateStorage(snapshot.Storage, notifications);
            EvaluateMounts(snapshot.Storage, notifications);
        }

        EvaluateDocker(snapshot, notifications);
        EvaluateSamba(snapshot, notifications);
        EvaluateTasks(snapshot, notifications);
        EvaluateRegistryReminder(now, notifications);
        return notifications;
    }

    private void SeedTransitionHistory(MonitoringSnapshot snapshot)
    {
        _state.DockerStates = snapshot.Docker.Containers
            .Where(container => !string.IsNullOrWhiteSpace(container.Name))
            .ToDictionary(container => container.Name, container => container.State, StringComparer.OrdinalIgnoreCase);
        _state.TaskEvents = snapshot.Tasks.ToDictionary(
            task => task.Id.ToString(),
            TaskSignature,
            StringComparer.OrdinalIgnoreCase);

        // Les seuils et services déjà en erreur doivent produire une seule
        // notification au premier contrôle. Les conteneurs et anciennes tâches
        // sont seulement mémorisés pour ne pas annoncer tout l'historique.
        _state.CpuHigh = false;
        _state.RamHigh = false;
        _state.HighStoragePaths.Clear();
        _state.MountsHealthy = true;
        _state.MountStates.Clear();
        _state.DockerServiceActive = true;
        _state.SambaOk = true;
        _state.LastBuildPending = 0;
    }

    private void EvaluateCpu(double percent, List<AgentNotification> notifications)
    {
        if (_settings.NotifyCpu && percent >= _settings.CpuThresholdPercent && !_state.CpuHigh)
        {
            notifications.Add(new AgentNotification(
                "CPU Yoleo très occupé",
                $"Utilisation CPU : {percent:0.#} % (seuil {_settings.CpuThresholdPercent} %).",
                NotificationLevel.Warning,
                HomePage));
            _state.CpuHigh = true;
        }
        else if (percent < _settings.CpuThresholdPercent - HysteresisPercent)
        {
            _state.CpuHigh = false;
        }
    }

    private void EvaluateRam(double percent, List<AgentNotification> notifications)
    {
        if (_settings.NotifyRam && percent >= _settings.RamThresholdPercent && !_state.RamHigh)
        {
            notifications.Add(new AgentNotification(
                "Mémoire Yoleo très occupée",
                $"Utilisation RAM : {percent:0.#} % (seuil {_settings.RamThresholdPercent} %).",
                NotificationLevel.Warning,
                HomePage));
            _state.RamHigh = true;
        }
        else if (percent < _settings.RamThresholdPercent - HysteresisPercent)
        {
            _state.RamHigh = false;
        }
    }

    private void EvaluateStorage(StorageSnapshot storage, List<AgentNotification> notifications)
    {
        var selectedPaths = new HashSet<string>(
            _settings.MonitoredMountPaths,
            StringComparer.OrdinalIgnoreCase);
        if (selectedPaths.Count == 0)
        {
            _state.HighStoragePaths.Clear();
            return;
        }

        var highPaths = new HashSet<string>(_state.HighStoragePaths, StringComparer.OrdinalIgnoreCase);
        var hasDetailedMounts = storage.Mounts.Count > 0;
        var volumes = hasDetailedMounts
            ? new List<StorageVolume>(storage.Mounts)
            : new List<StorageVolume>(storage.Volumes);
        if (!hasDetailedMounts && !string.IsNullOrWhiteSpace(storage.Main.Path))
        {
            volumes.Add(storage.Main);
        }

        foreach (var volume in volumes
                     .Where(item =>
                         !string.IsNullOrWhiteSpace(item.Path) &&
                         selectedPaths.Contains(item.Path))
                     .GroupBy(item => item.Path, StringComparer.OrdinalIgnoreCase)
                     .Select(group => group.First()))
        {
            if (_settings.NotifyStorage &&
                (!hasDetailedMounts || volume.IsMount) &&
                volume.Percent >= _settings.StorageThresholdPercent &&
                !highPaths.Contains(volume.Path))
            {
                notifications.Add(new AgentNotification(
                    "Stockage Yoleo presque plein",
                    $"{volume.Path} est occupé à {volume.Percent:0.#} % (seuil {_settings.StorageThresholdPercent} %).",
                    NotificationLevel.Warning,
                    DiskPage));
                highPaths.Add(volume.Path);
            }
            else if ((hasDetailedMounts && !volume.IsMount) ||
                     volume.Percent < _settings.StorageThresholdPercent - HysteresisPercent)
            {
                highPaths.Remove(volume.Path);
            }
        }

        _state.HighStoragePaths = highPaths
            .Where(selectedPaths.Contains)
            .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
            .ToList();
    }

    private void EvaluateMounts(StorageSnapshot storage, List<AgentNotification> notifications)
    {
        var selectedPaths = new HashSet<string>(
            _settings.MonitoredMountPaths,
            StringComparer.OrdinalIgnoreCase);
        if (selectedPaths.Count == 0)
        {
            _state.MountStates.Clear();
            return;
        }

        var available = storage.Mounts
            .Where(item => !string.IsNullOrWhiteSpace(item.Path))
            .GroupBy(item => item.Path, StringComparer.OrdinalIgnoreCase)
            .ToDictionary(group => group.Key, group => group.First(), StringComparer.OrdinalIgnoreCase);

        // Compatibilité avec un serveur API plus ancien, sans storage.mounts.
        if (available.Count == 0)
        {
            foreach (var volume in storage.Volumes.Where(item => !string.IsNullOrWhiteSpace(item.Path)))
            {
                volume.IsMount = volume.Ok;
                available[volume.Path] = volume;
            }
        }

        var currentStates = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var path in selectedPaths)
        {
            available.TryGetValue(path, out var mount);
            var currentState = mount is null
                ? "missing"
                : mount.IsMount
                    ? "mounted"
                    : string.IsNullOrWhiteSpace(mount.Status) ? "folder" : mount.Status;
            currentStates[path] = currentState;

            var previousState = _state.MountStates.TryGetValue(path, out var previous)
                ? previous
                : "mounted";
            if (!_settings.NotifyMountFailures ||
                !string.Equals(previousState, "mounted", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(currentState, "mounted", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var label = MountLabel(path, mount?.Label);
            var detail = currentState switch
            {
                "folder_with_data" => $"{path} existe encore, mais c'est maintenant un dossier local contenant des données.",
                "folder" => $"{path} existe encore, mais c'est maintenant un simple dossier local.",
                "missing" => $"{path} n'existe plus sur le serveur.",
                _ => $"{path} n'est plus reconnu comme un vrai point de montage.",
            };
            notifications.Add(new AgentNotification(
                $"{label} n'est plus un montage disque",
                detail,
                NotificationLevel.Error,
                DiskPage));
        }

        _state.MountStates = currentStates;
    }

    private void EvaluateDocker(MonitoringSnapshot snapshot, List<AgentNotification> notifications)
    {
        if (HasSectionError(snapshot, "docker"))
        {
            return;
        }

        var active = snapshot.Docker.Service.Active;
        if (active.HasValue)
        {
            if (_settings.NotifyDockerService && _state.DockerServiceActive == true && !active.Value)
            {
                notifications.Add(new AgentNotification(
                    "Service Docker arrêté",
                    $"État actuel : {snapshot.Docker.Service.Label}.",
                    NotificationLevel.Error,
                    DockerPage));
            }
            _state.DockerServiceActive = active;
        }

        if (active != true)
        {
            return;
        }

        var previous = new Dictionary<string, string>(_state.DockerStates, StringComparer.OrdinalIgnoreCase);
        var current = snapshot.Docker.Containers
            .Where(container => !string.IsNullOrWhiteSpace(container.Name))
            .ToDictionary(container => container.Name, container => container.State, StringComparer.OrdinalIgnoreCase);

        if (_settings.NotifyDockerContainers)
        {
            foreach (var pair in current)
            {
                if (previous.TryGetValue(pair.Key, out var oldState) &&
                    IsRunning(oldState) &&
                    !IsRunning(pair.Value))
                {
                    notifications.Add(new AgentNotification(
                        "Conteneur Docker arrêté",
                        $"{pair.Key} est passé de running à {pair.Value}.",
                        NotificationLevel.Error,
                        DockerPage));
                }
            }

            foreach (var pair in previous)
            {
                if (IsRunning(pair.Value) && !current.ContainsKey(pair.Key))
                {
                    notifications.Add(new AgentNotification(
                        "Conteneur Docker absent",
                        $"{pair.Key} n'apparaît plus dans l'inventaire Docker.",
                        NotificationLevel.Error,
                        DockerPage));
                }
            }
        }

        _state.DockerStates = current;
    }

    private void EvaluateSamba(MonitoringSnapshot snapshot, List<AgentNotification> notifications)
    {
        if (HasSectionError(snapshot, "samba") || !snapshot.Samba.Available)
        {
            return;
        }

        if (_settings.NotifySamba && _state.SambaOk == true && !snapshot.Samba.Ok)
        {
            var stopped = snapshot.Samba.Services
                .Where(service => !service.Ok)
                .Select(service => service.Name)
                .ToArray();
            notifications.Add(new AgentNotification(
                "Partage Samba arrêté",
                stopped.Length == 0
                    ? "Un service Samba/WSDD n'est plus actif."
                    : "Services non actifs : " + string.Join(", ", stopped),
                NotificationLevel.Error,
                SambaPage));
        }
        _state.SambaOk = snapshot.Samba.Ok;
    }

    private void EvaluateTasks(MonitoringSnapshot snapshot, List<AgentNotification> notifications)
    {
        if (HasSectionError(snapshot, "tasks"))
        {
            return;
        }

        var current = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var task in snapshot.Tasks)
        {
            var key = task.Id.ToString();
            var signature = TaskSignature(task);
            current[key] = signature;

            if (_settings.NotifyTaskFailures &&
                IsTaskError(task) &&
                _state.TaskEvents.TryGetValue(key, out var previousSignature) &&
                !string.Equals(previousSignature, signature, StringComparison.Ordinal))
            {
                notifications.Add(new AgentNotification(
                    "Tâche Yoleo en erreur",
                    $"{task.Title} : {Trim(task.LastMessage, 180)}",
                    NotificationLevel.Error,
                    TasksPage));
            }
        }
        _state.TaskEvents = current;
    }

    private void EvaluateBuild(BuildSnapshot build, List<AgentNotification> notifications)
    {
        if (!build.Available)
        {
            return;
        }

        var pending = Math.Max(0, build.ToBuild) + Math.Max(0, build.ToPush);
        if (_settings.NotifyBuildPending && pending > 0 && pending > _state.LastBuildPending)
        {
            notifications.Add(new AgentNotification(
                "Travail Docker en attente",
                $"{build.ToBuild} élément(s) à builder et {build.ToPush} à envoyer au registre.",
                NotificationLevel.Warning,
                BuildPage));
        }
        _state.LastBuildPending = pending;
    }

    private void EvaluateRegistryReminder(DateTime now, List<AgentNotification> notifications)
    {
        if (!_settings.NotifyRegistryCleanup || now.Day < _settings.RegistryReminderDay)
        {
            return;
        }

        var month = now.ToString("yyyy-MM");
        if (string.Equals(month, _state.LastRegistryReminderMonth, StringComparison.Ordinal))
        {
            return;
        }

        notifications.Add(new AgentNotification(
            "Rappel d'entretien Yoleo",
            "Pense à vérifier et nettoyer le registre Docker.",
            NotificationLevel.Info,
            RegistryPage));
        _state.LastRegistryReminderMonth = month;
    }

    private static bool HasSectionError(MonitoringSnapshot snapshot, string section) =>
        snapshot.Errors.Any(error => string.Equals(error.Section, section, StringComparison.OrdinalIgnoreCase));

    private static bool IsRunning(string state) =>
        string.Equals(state, "running", StringComparison.OrdinalIgnoreCase);

    private static bool IsTaskError(TaskSnapshot task) =>
        task.Result.Contains("erreur", StringComparison.OrdinalIgnoreCase) ||
        task.Status.Contains("erreur", StringComparison.OrdinalIgnoreCase);

    private static string TaskSignature(TaskSnapshot task) =>
        $"{task.UpdatedAt}|{task.LastEnd}|{task.Result}|{task.Status}";

    private static string MountLabel(string path, string? apiLabel)
    {
        if (!string.IsNullOrWhiteSpace(apiLabel))
        {
            return apiLabel.Trim();
        }
        var clean = (path ?? "").Trim().TrimEnd('/');
        var slash = clean.LastIndexOf('/');
        return slash >= 0 && slash < clean.Length - 1 ? clean[(slash + 1)..] : clean;
    }

    private static string Trim(string value, int maximum)
    {
        var clean = string.IsNullOrWhiteSpace(value) ? "Aucun détail supplémentaire." : value.Trim();
        return clean.Length <= maximum ? clean : clean[..maximum] + "…";
    }
}
