using System.Text.Json.Serialization;

namespace YoleoAgent;

internal sealed class AgentSettings
{
    public string ServerUrl { get; set; } = "";
    public string P12Path { get; set; } = "";
    public string Username { get; set; } = "";
    public int PollIntervalMinutes { get; set; } = 5;
    public int OfflineFailureCount { get; set; } = 2;
    public bool NotifyServerOffline { get; set; } = true;
    public bool NotifyServerRecovery { get; set; } = true;
    public bool NotifyCpu { get; set; } = true;
    public int CpuThresholdPercent { get; set; } = 90;
    public bool NotifyRam { get; set; } = true;
    public int RamThresholdPercent { get; set; } = 90;
    public bool NotifyStorage { get; set; } = true;
    public int StorageThresholdPercent { get; set; } = 80;
    public bool NotifyMountFailures { get; set; } = true;
    public List<string> MonitoredMountPaths { get; set; } = [];
    public bool NotifyDockerService { get; set; } = true;
    public bool NotifyDockerContainers { get; set; } = true;
    public bool NotifySamba { get; set; } = true;
    public bool NotifyTaskFailures { get; set; } = true;
    public bool NotifyBuildPending { get; set; } = true;
    public bool NotifyRegistryCleanup { get; set; }
    public int RegistryReminderDay { get; set; } = 1;

    [JsonIgnore]
    public bool HasConnectionConfiguration =>
        Uri.TryCreate(ServerUrl, UriKind.Absolute, out var uri) &&
        uri.Scheme == Uri.UriSchemeHttps &&
        !string.IsNullOrWhiteSpace(P12Path) &&
        !string.IsNullOrWhiteSpace(Username);

    public void Normalize()
    {
        ServerUrl = (ServerUrl ?? "").Trim().TrimEnd('/');
        P12Path = (P12Path ?? "").Trim();
        Username = (Username ?? "").Trim();
        PollIntervalMinutes = PollIntervalMinutes is 1 or 5 or 10 or 15 or 30 or 60
            ? PollIntervalMinutes
            : 5;
        OfflineFailureCount = Math.Clamp(OfflineFailureCount, 1, 5);
        CpuThresholdPercent = Math.Clamp(CpuThresholdPercent, 1, 100);
        RamThresholdPercent = Math.Clamp(RamThresholdPercent, 1, 100);
        StorageThresholdPercent = Math.Clamp(StorageThresholdPercent, 1, 100);
        RegistryReminderDay = Math.Clamp(RegistryReminderDay, 1, 28);
        MonitoredMountPaths = (MonitoredMountPaths ?? [])
            .Select(path => (path ?? "").Trim().TrimEnd('/'))
            .Where(path => path.StartsWith('/'))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
            .ToList();
    }

    public AgentSettings Copy()
    {
        var copy = (AgentSettings)MemberwiseClone();
        copy.MonitoredMountPaths = [.. MonitoredMountPaths];
        return copy;
    }
}

internal sealed class SecretSettings
{
    public string P12Password { get; set; } = "";
    public string AccessToken { get; set; } = "";

    public SecretSettings Copy() => new()
    {
        P12Password = P12Password,
        AccessToken = AccessToken,
    };
}

internal sealed class MonitoringState
{
    public bool HasBaseline { get; set; }
    public bool WasOnline { get; set; }
    public bool OfflineNotified { get; set; }
    public int ConsecutiveFailures { get; set; }
    public bool CpuHigh { get; set; }
    public bool RamHigh { get; set; }
    public List<string> HighStoragePaths { get; set; } = [];
    public bool? MountsHealthy { get; set; }
    public Dictionary<string, string> MountStates { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public bool? DockerServiceActive { get; set; }
    public Dictionary<string, string> DockerStates { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public bool? SambaOk { get; set; }
    public Dictionary<string, string> TaskEvents { get; set; } = [];
    public int LastBuildPending { get; set; }
    public string LastRegistryReminderMonth { get; set; } = "";
    public string LastSuccessfulCheckUtc { get; set; } = "";
}

internal enum NotificationLevel
{
    Info,
    Warning,
    Error,
}

internal sealed record AgentNotification(
    string Title,
    string Message,
    NotificationLevel Level,
    string DestinationPath = "/index");

internal sealed class ApiErrorEnvelope
{
    [JsonPropertyName("error")]
    public ApiError? Error { get; set; }
}

internal sealed class ApiError
{
    [JsonPropertyName("code")]
    public string Code { get; set; } = "";

    [JsonPropertyName("message")]
    public string Message { get; set; } = "";
}

internal sealed class HealthEnvelope
{
    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("service")]
    public string Service { get; set; } = "";

    [JsonPropertyName("server_time")]
    public string ServerTime { get; set; } = "";
}

internal sealed class LoginEnvelope
{
    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("authentication")]
    public AuthenticationPayload? Authentication { get; set; }
}

internal sealed class AuthenticationPayload
{
    [JsonPropertyName("access_token")]
    public string AccessToken { get; set; } = "";

    [JsonPropertyName("expires_at")]
    public string ExpiresAt { get; set; } = "";

    [JsonPropertyName("username")]
    public string Username { get; set; } = "";
}

internal sealed class IdentityEnvelope
{
    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("identity")]
    public IdentityPayload? Identity { get; set; }
}

internal sealed class IdentityPayload
{
    [JsonPropertyName("username")]
    public string Username { get; set; } = "";
}

internal sealed class MonitoringEnvelope
{
    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("monitoring")]
    public MonitoringSnapshot? Monitoring { get; set; }
}

internal sealed class MonitoringSnapshot
{
    [JsonPropertyName("generated_at")]
    public string GeneratedAt { get; set; } = "";

    [JsonPropertyName("system")]
    public SystemSnapshot System { get; set; } = new();

    [JsonPropertyName("storage")]
    public StorageSnapshot Storage { get; set; } = new();

    [JsonPropertyName("docker")]
    public DockerSnapshot Docker { get; set; } = new();

    [JsonPropertyName("samba")]
    public SambaSnapshot Samba { get; set; } = new();

    [JsonPropertyName("tasks")]
    public List<TaskSnapshot> Tasks { get; set; } = [];

    [JsonPropertyName("build")]
    public BuildSnapshot Build { get; set; } = new();

    [JsonPropertyName("errors")]
    public List<MonitoringSectionError> Errors { get; set; } = [];
}

internal sealed class SystemSnapshot
{
    [JsonPropertyName("cpu_percent")]
    public double CpuPercent { get; set; }

    [JsonPropertyName("ram_percent")]
    public double RamPercent { get; set; }

    [JsonPropertyName("uptime")]
    public string Uptime { get; set; } = "";
}

internal sealed class StorageSnapshot
{
    [JsonPropertyName("main")]
    public StorageVolume Main { get; set; } = new();

    [JsonPropertyName("volumes")]
    public List<StorageVolume> Volumes { get; set; } = [];

    [JsonPropertyName("mounts")]
    public List<StorageVolume> Mounts { get; set; } = [];

    [JsonPropertyName("mount_state")]
    public string MountState { get; set; } = "";

    [JsonPropertyName("mount_label")]
    public string MountLabel { get; set; } = "";
}

internal sealed class StorageVolume
{
    [JsonPropertyName("path")]
    public string Path { get; set; } = "";

    [JsonPropertyName("label")]
    public string Label { get; set; } = "";

    [JsonPropertyName("exists")]
    public bool Exists { get; set; }

    [JsonPropertyName("is_mount")]
    public bool IsMount { get; set; }

    [JsonPropertyName("source")]
    public string Source { get; set; } = "";

    [JsonPropertyName("fstype")]
    public string FileSystem { get; set; } = "";

    [JsonPropertyName("percent")]
    public double Percent { get; set; }

    [JsonPropertyName("used")]
    public string Used { get; set; } = "";

    [JsonPropertyName("free")]
    public string Free { get; set; } = "";

    [JsonPropertyName("total")]
    public string Total { get; set; } = "";

    [JsonPropertyName("status")]
    public string Status { get; set; } = "";

    [JsonPropertyName("status_label")]
    public string StatusLabel { get; set; } = "";

    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("home_selected")]
    public bool HomeSelected { get; set; }

    [JsonPropertyName("home_usage_selected")]
    public bool HomeUsageSelected { get; set; }
}

internal sealed class DockerSnapshot
{
    [JsonPropertyName("available")]
    public bool Available { get; set; }

    [JsonPropertyName("service")]
    public DockerServiceSnapshot Service { get; set; } = new();

    [JsonPropertyName("stats")]
    public DockerStatsSnapshot Stats { get; set; } = new();

    [JsonPropertyName("containers")]
    public List<DockerContainerSnapshot> Containers { get; set; } = [];
}

internal sealed class DockerServiceSnapshot
{
    [JsonPropertyName("active")]
    public bool? Active { get; set; }

    [JsonPropertyName("state")]
    public string State { get; set; } = "unknown";

    [JsonPropertyName("label")]
    public string Label { get; set; } = "Inconnu";
}

internal sealed class DockerStatsSnapshot
{
    [JsonPropertyName("total")]
    public int Total { get; set; }

    [JsonPropertyName("running")]
    public int Running { get; set; }

    [JsonPropertyName("stopped")]
    public int Stopped { get; set; }
}

internal sealed class DockerContainerSnapshot
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("state")]
    public string State { get; set; } = "unknown";

    [JsonPropertyName("stack")]
    public string Stack { get; set; } = "";
}

internal sealed class SambaSnapshot
{
    [JsonPropertyName("available")]
    public bool Available { get; set; }

    [JsonPropertyName("ok")]
    public bool Ok { get; set; }

    [JsonPropertyName("services")]
    public List<SambaServiceSnapshot> Services { get; set; } = [];
}

internal sealed class SambaServiceSnapshot
{
    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("active")]
    public string Active { get; set; } = "unknown";

    [JsonPropertyName("ok")]
    public bool Ok { get; set; }
}

internal sealed class TaskSnapshot
{
    [JsonPropertyName("id")]
    public int Id { get; set; }

    [JsonPropertyName("title")]
    public string Title { get; set; } = "";

    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; }

    [JsonPropertyName("running")]
    public bool Running { get; set; }

    [JsonPropertyName("status")]
    public string Status { get; set; } = "";

    [JsonPropertyName("result")]
    public string Result { get; set; } = "";

    [JsonPropertyName("last_end")]
    public string LastEnd { get; set; } = "";

    [JsonPropertyName("last_message")]
    public string LastMessage { get; set; } = "";

    [JsonPropertyName("updated_at")]
    public string UpdatedAt { get; set; } = "";
}

internal sealed class BuildSnapshot
{
    [JsonPropertyName("available")]
    public bool Available { get; set; }

    [JsonPropertyName("to_build")]
    public int ToBuild { get; set; }

    [JsonPropertyName("to_push")]
    public int ToPush { get; set; }

    [JsonPropertyName("label")]
    public string Label { get; set; } = "";
}

internal sealed class MonitoringSectionError
{
    [JsonPropertyName("section")]
    public string Section { get; set; } = "";

    [JsonPropertyName("code")]
    public string Code { get; set; } = "";
}
