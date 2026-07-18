using Microsoft.Win32;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace YoleoAgent;

internal static class RegistryStore
{
    public const string KeyPath = @"Software\Sftpmalin\YoleoAgent";
    private const string ConfigurationSavedValue = "ConfigurationSaved";

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = false,
        PropertyNameCaseInsensitive = true,
    };

    public static AgentSettings LoadSettings()
    {
        using var key = Registry.CurrentUser.OpenSubKey(KeyPath);
        var settings = new AgentSettings
        {
            ServerUrl = ReadString(key, nameof(AgentSettings.ServerUrl), ""),
            P12Path = ReadString(key, nameof(AgentSettings.P12Path), ""),
            Username = ReadString(key, nameof(AgentSettings.Username), ""),
            PollIntervalMinutes = ReadInt(key, nameof(AgentSettings.PollIntervalMinutes), 5),
            OfflineFailureCount = ReadInt(key, nameof(AgentSettings.OfflineFailureCount), 2),
            NotifyServerOffline = ReadBool(key, nameof(AgentSettings.NotifyServerOffline), true),
            NotifyServerRecovery = ReadBool(key, nameof(AgentSettings.NotifyServerRecovery), true),
            NotifyCpu = ReadBool(key, nameof(AgentSettings.NotifyCpu), true),
            CpuThresholdPercent = ReadInt(key, nameof(AgentSettings.CpuThresholdPercent), 90),
            NotifyRam = ReadBool(key, nameof(AgentSettings.NotifyRam), true),
            RamThresholdPercent = ReadInt(key, nameof(AgentSettings.RamThresholdPercent), 90),
            NotifyStorage = ReadBool(key, nameof(AgentSettings.NotifyStorage), true),
            StorageThresholdPercent = ReadInt(key, nameof(AgentSettings.StorageThresholdPercent), 80),
            NotifyMountFailures = ReadBool(key, nameof(AgentSettings.NotifyMountFailures), true),
            MonitoredMountPaths = ReadStringList(key, "MonitoredMountPathsJson"),
            NotifyDockerService = ReadBool(key, nameof(AgentSettings.NotifyDockerService), true),
            NotifyDockerContainers = ReadBool(key, nameof(AgentSettings.NotifyDockerContainers), true),
            NotifySamba = ReadBool(key, nameof(AgentSettings.NotifySamba), true),
            NotifyTaskFailures = ReadBool(key, nameof(AgentSettings.NotifyTaskFailures), true),
            NotifyBuildPending = ReadBool(key, nameof(AgentSettings.NotifyBuildPending), true),
            NotifyRegistryCleanup = ReadBool(key, nameof(AgentSettings.NotifyRegistryCleanup), false),
            RegistryReminderDay = ReadInt(key, nameof(AgentSettings.RegistryReminderDay), 1),
        };
        settings.Normalize();
        return settings;
    }

    public static bool HasSavedConfiguration()
    {
        using var key = Registry.CurrentUser.OpenSubKey(KeyPath);
        return ReadBool(key, ConfigurationSavedValue, false);
    }

    public static void SaveSettings(AgentSettings settings)
    {
        settings.Normalize();
        using var key = Registry.CurrentUser.CreateSubKey(KeyPath, true);
        WriteString(key, nameof(settings.ServerUrl), settings.ServerUrl);
        WriteString(key, nameof(settings.P12Path), settings.P12Path);
        WriteString(key, nameof(settings.Username), settings.Username);
        WriteInt(key, nameof(settings.PollIntervalMinutes), settings.PollIntervalMinutes);
        WriteInt(key, nameof(settings.OfflineFailureCount), settings.OfflineFailureCount);
        WriteBool(key, nameof(settings.NotifyServerOffline), settings.NotifyServerOffline);
        WriteBool(key, nameof(settings.NotifyServerRecovery), settings.NotifyServerRecovery);
        WriteBool(key, nameof(settings.NotifyCpu), settings.NotifyCpu);
        WriteInt(key, nameof(settings.CpuThresholdPercent), settings.CpuThresholdPercent);
        WriteBool(key, nameof(settings.NotifyRam), settings.NotifyRam);
        WriteInt(key, nameof(settings.RamThresholdPercent), settings.RamThresholdPercent);
        WriteBool(key, nameof(settings.NotifyStorage), settings.NotifyStorage);
        WriteInt(key, nameof(settings.StorageThresholdPercent), settings.StorageThresholdPercent);
        WriteBool(key, nameof(settings.NotifyMountFailures), settings.NotifyMountFailures);
        WriteString(key, "MonitoredMountPathsJson", JsonSerializer.Serialize(settings.MonitoredMountPaths, JsonOptions));
        WriteBool(key, nameof(settings.NotifyDockerService), settings.NotifyDockerService);
        WriteBool(key, nameof(settings.NotifyDockerContainers), settings.NotifyDockerContainers);
        WriteBool(key, nameof(settings.NotifySamba), settings.NotifySamba);
        WriteBool(key, nameof(settings.NotifyTaskFailures), settings.NotifyTaskFailures);
        WriteBool(key, nameof(settings.NotifyBuildPending), settings.NotifyBuildPending);
        WriteBool(key, nameof(settings.NotifyRegistryCleanup), settings.NotifyRegistryCleanup);
        WriteInt(key, nameof(settings.RegistryReminderDay), settings.RegistryReminderDay);
        WriteBool(key, ConfigurationSavedValue, true);
    }

    public static SecretSettings LoadSecrets()
    {
        using var key = Registry.CurrentUser.OpenSubKey(KeyPath);
        return new SecretSettings
        {
            P12Password = Dpapi.Unprotect(ReadString(key, "P12PasswordProtected", "")),
            AccessToken = Dpapi.Unprotect(ReadString(key, "AccessTokenProtected", "")),
        };
    }

    public static void SaveSecrets(SecretSettings secrets)
    {
        using var key = Registry.CurrentUser.CreateSubKey(KeyPath, true);
        WriteString(key, "P12PasswordProtected", Dpapi.Protect(secrets.P12Password));
        WriteString(key, "AccessTokenProtected", Dpapi.Protect(secrets.AccessToken));
    }

    public static MonitoringState LoadState()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(KeyPath);
            var json = ReadString(key, "MonitoringStateJson", "");
            var state = string.IsNullOrWhiteSpace(json)
                ? new MonitoringState()
                : JsonSerializer.Deserialize<MonitoringState>(json, JsonOptions) ?? new MonitoringState();
            state.HighStoragePaths ??= [];
            state.MountStates = new Dictionary<string, string>(
                state.MountStates ?? [],
                StringComparer.OrdinalIgnoreCase);
            state.DockerStates = new Dictionary<string, string>(
                state.DockerStates ?? [],
                StringComparer.OrdinalIgnoreCase);
            state.TaskEvents ??= [];
            return state;
        }
        catch
        {
            return new MonitoringState();
        }
    }

    public static void SaveState(MonitoringState state)
    {
        using var key = Registry.CurrentUser.CreateSubKey(KeyPath, true);
        WriteString(key, "MonitoringStateJson", JsonSerializer.Serialize(state, JsonOptions));
    }

    private static string ReadString(RegistryKey? key, string name, string fallback) =>
        Convert.ToString(key?.GetValue(name, fallback)) ?? fallback;

    private static List<string> ReadStringList(RegistryKey? key, string name)
    {
        try
        {
            var json = ReadString(key, name, "[]");
            return JsonSerializer.Deserialize<List<string>>(json, JsonOptions) ?? [];
        }
        catch
        {
            return [];
        }
    }

    private static int ReadInt(RegistryKey? key, string name, int fallback)
    {
        try
        {
            return Convert.ToInt32(key?.GetValue(name, fallback));
        }
        catch
        {
            return fallback;
        }
    }

    private static bool ReadBool(RegistryKey? key, string name, bool fallback) =>
        ReadInt(key, name, fallback ? 1 : 0) != 0;

    private static void WriteString(RegistryKey key, string name, string value) =>
        key.SetValue(name, value ?? "", RegistryValueKind.String);

    private static void WriteInt(RegistryKey key, string name, int value) =>
        key.SetValue(name, value, RegistryValueKind.DWord);

    private static void WriteBool(RegistryKey key, string name, bool value) =>
        WriteInt(key, name, value ? 1 : 0);

    private static class Dpapi
    {
        private const int CryptProtectUiForbidden = 0x1;

        [StructLayout(LayoutKind.Sequential)]
        private struct DataBlob
        {
            public int Size;
            public IntPtr Data;
        }

        [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CryptProtectData(
            ref DataBlob dataIn,
            string description,
            IntPtr optionalEntropy,
            IntPtr reserved,
            IntPtr prompt,
            int flags,
            out DataBlob dataOut);

        [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CryptUnprotectData(
            ref DataBlob dataIn,
            out IntPtr description,
            IntPtr optionalEntropy,
            IntPtr reserved,
            IntPtr prompt,
            int flags,
            out DataBlob dataOut);

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);

        public static string Protect(string plainText)
        {
            if (string.IsNullOrEmpty(plainText))
            {
                return "";
            }

            var protectedBytes = Transform(
                Encoding.UTF8.GetBytes(plainText),
                protect: true);
            return Convert.ToBase64String(protectedBytes);
        }

        public static string Unprotect(string protectedBase64)
        {
            if (string.IsNullOrWhiteSpace(protectedBase64))
            {
                return "";
            }

            try
            {
                var clearBytes = Transform(Convert.FromBase64String(protectedBase64), protect: false);
                try
                {
                    return Encoding.UTF8.GetString(clearBytes);
                }
                finally
                {
                    CryptographicOperations.ZeroMemory(clearBytes);
                }
            }
            catch
            {
                return "";
            }
        }

        private static byte[] Transform(byte[] inputBytes, bool protect)
        {
            var input = new DataBlob
            {
                Size = inputBytes.Length,
                Data = Marshal.AllocHGlobal(inputBytes.Length),
            };
            var output = default(DataBlob);
            var description = IntPtr.Zero;

            try
            {
                Marshal.Copy(inputBytes, 0, input.Data, inputBytes.Length);
                var success = protect
                    ? CryptProtectData(
                        ref input,
                        "Yoleo Agent",
                        IntPtr.Zero,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        CryptProtectUiForbidden,
                        out output)
                    : CryptUnprotectData(
                        ref input,
                        out description,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        CryptProtectUiForbidden,
                        out output);

                if (!success)
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }

                var result = new byte[output.Size];
                Marshal.Copy(output.Data, result, 0, output.Size);
                return result;
            }
            finally
            {
                CryptographicOperations.ZeroMemory(inputBytes);
                if (input.Size > 0 && input.Data != IntPtr.Zero)
                {
                    Marshal.Copy(new byte[input.Size], 0, input.Data, input.Size);
                }
                if (input.Data != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(input.Data);
                }
                if (output.Data != IntPtr.Zero)
                {
                    LocalFree(output.Data);
                }
                if (description != IntPtr.Zero)
                {
                    LocalFree(description);
                }
            }
        }
    }
}
