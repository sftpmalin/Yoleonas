using System.Text.Json;

namespace YoleoAgent;

internal static class ConnectionDiagnostics
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
    };

    public static async Task<int> RunAsync()
    {
        var resultPath = Environment.GetEnvironmentVariable("YOLEO_TEST_RESULT_PATH");
        var stages = new List<string>();
        YoleoApiClient? client = null;
        string accessToken = "";

        try
        {
            var p12Path = RequiredEnvironmentVariable("YOLEO_P12_PATH");
            var p12Password = RequiredEnvironmentVariable("YOLEO_P12_PASSWORD");
            var serverPassword = RequiredEnvironmentVariable("YOLEO_SERVER_PASSWORD");
            var settings = new AgentSettings
            {
                ServerUrl = Environment.GetEnvironmentVariable("YOLEO_SERVER_URL") ?? "",
                P12Path = p12Path,
                Username = Environment.GetEnvironmentVariable("YOLEO_USERNAME") ?? "",
            };

            client = new YoleoApiClient(settings, p12Password);
            stages.Add("p12_ok");

            await client.GetHealthAsync();
            stages.Add("https_ok");

            var authentication = await client.LoginAsync(settings.Username, serverPassword);
            accessToken = authentication.AccessToken;
            stages.Add("login_ok");

            await client.GetIdentityAsync(accessToken);
            stages.Add("identity_ok");

            var snapshot = await client.GetMonitoringSnapshotAsync(accessToken);
            stages.Add("monitoring_ok");

            WriteResult(resultPath, new
            {
                ok = true,
                stages,
                certificate = client.CertificateSummary,
                cpu_percent = snapshot.System.CpuPercent,
                ram_percent = snapshot.System.RamPercent,
                mount_count = snapshot.Storage.Mounts.Count,
                media0 = snapshot.Storage.Mounts
                    .Where(mount => string.Equals(mount.Path, "/mnt/media0", StringComparison.OrdinalIgnoreCase))
                    .Select(mount => new
                    {
                        mount.Path,
                        mount.IsMount,
                        mount.Status,
                        mount.Percent,
                    })
                    .FirstOrDefault(),
            });
            return 0;
        }
        catch (Exception exception)
        {
            WriteResult(resultPath, new
            {
                ok = false,
                stages,
                exception = ExceptionDetails(exception),
            });
            return 1;
        }
        finally
        {
            if (client is not null && !string.IsNullOrWhiteSpace(accessToken))
            {
                try
                {
                    await client.LogoutAsync(accessToken);
                }
                catch
                {
                    // Le diagnostic est déjà terminé ; le jeton expirera côté serveur.
                }
            }
            client?.Dispose();
        }
    }

    private static string RequiredEnvironmentVariable(string name)
    {
        var value = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrWhiteSpace(value)
            ? throw new InvalidOperationException($"Variable de diagnostic absente : {name}.")
            : value;
    }

    private static string[] ExceptionDetails(Exception exception)
    {
        var details = new List<string>();
        for (Exception? current = exception; current is not null; current = current.InnerException)
        {
            details.Add($"{current.GetType().FullName}: {current.Message}");
        }
        return details.ToArray();
    }

    private static void WriteResult(string? path, object result)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return;
        }
        File.WriteAllText(path, JsonSerializer.Serialize(result, JsonOptions));
    }
}
