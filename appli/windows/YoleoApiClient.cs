using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;

namespace YoleoAgent;

internal sealed class YoleoApiClient : IDisposable
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        PropertyNameCaseInsensitive = true,
    };

    private readonly X509Certificate2 _certificate;
    private readonly HttpClient _httpClient;

    public string CertificateSummary =>
        $"{_certificate.GetNameInfo(X509NameType.SimpleName, false)} — valable jusqu'au {_certificate.NotAfter:dd/MM/yyyy}";

    public YoleoApiClient(AgentSettings settings, string p12Password)
    {
        var serverUrl = NormalizeServerUrl(settings.ServerUrl);
        _certificate = LoadCertificate(settings.P12Path, p12Password);

        var handler = new HttpClientHandler
        {
            ClientCertificateOptions = ClientCertificateOption.Manual,
            CheckCertificateRevocationList = true,
            SslProtocols = SslProtocols.Tls12 | SslProtocols.Tls13,
        };
        handler.ClientCertificates.Add(_certificate);

        _httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri(serverUrl + "/api/v1/", UriKind.Absolute),
            Timeout = TimeSpan.FromSeconds(30),
        };
        _httpClient.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
    }

    public static string NormalizeServerUrl(string rawUrl)
    {
        if (!Uri.TryCreate((rawUrl ?? "").Trim(), UriKind.Absolute, out var uri) ||
            uri.Scheme != Uri.UriSchemeHttps ||
            string.IsNullOrWhiteSpace(uri.Host))
        {
            throw new ArgumentException("L'adresse du serveur doit commencer par https://.");
        }

        return uri.GetLeftPart(UriPartial.Authority).TrimEnd('/');
    }

    public static string WebsiteUrl(string rawUrl, string relativePath = "/index")
    {
        var path = string.IsNullOrWhiteSpace(relativePath) ? "/index" : relativePath.Trim();
        if (!path.StartsWith('/'))
        {
            path = "/" + path;
        }
        return NormalizeServerUrl(rawUrl) + path;
    }

    public async Task<HealthEnvelope> GetHealthAsync(CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, "health");
        var result = await SendAsync<HealthEnvelope>(request, cancellationToken);
        if (!result.Ok)
        {
            throw new ApiException("health_failed", "Le serveur n'a pas confirmé son état de santé.");
        }
        return result;
    }

    public async Task<AuthenticationPayload> LoginAsync(
        string username,
        string password,
        CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "auth/login")
        {
            Content = JsonContent.Create(new
            {
                username,
                password,
                device_name = Environment.MachineName,
                platform = "windows",
            }),
        };
        var result = await SendAsync<LoginEnvelope>(request, cancellationToken);
        if (!result.Ok || result.Authentication is null || string.IsNullOrWhiteSpace(result.Authentication.AccessToken))
        {
            throw new ApiException("login_failed", "Le serveur n'a pas délivré de jeton.");
        }
        return result.Authentication;
    }

    public async Task<IdentityPayload> GetIdentityAsync(
        string accessToken,
        CancellationToken cancellationToken = default)
    {
        using var request = AuthorizedRequest(HttpMethod.Get, "me", accessToken);
        var result = await SendAsync<IdentityEnvelope>(request, cancellationToken);
        if (!result.Ok || result.Identity is null)
        {
            throw new ApiException("identity_failed", "Le jeton n'a pas pu être vérifié.");
        }
        return result.Identity;
    }

    public async Task<MonitoringSnapshot> GetMonitoringSnapshotAsync(
        string accessToken,
        CancellationToken cancellationToken = default)
    {
        using var request = AuthorizedRequest(HttpMethod.Get, "monitoring/snapshot", accessToken);
        var result = await SendAsync<MonitoringEnvelope>(request, cancellationToken);
        if (!result.Ok || result.Monitoring is null)
        {
            throw new ApiException("monitoring_failed", "Le serveur n'a pas renvoyé le cliché de surveillance.");
        }
        return result.Monitoring;
    }

    public async Task LogoutAsync(string accessToken, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(accessToken))
        {
            return;
        }
        using var request = AuthorizedRequest(HttpMethod.Post, "auth/logout", accessToken);
        await SendAsync<JsonElement>(request, cancellationToken);
    }

    public void Dispose()
    {
        _httpClient.Dispose();
        _certificate.Dispose();
    }

    private static X509Certificate2 LoadCertificate(string path, string password)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            throw new FileNotFoundException("Le fichier P12 est introuvable.", path);
        }

        var certificate = new X509Certificate2(
            path,
            password,
            X509KeyStorageFlags.UserKeySet);

        if (!certificate.HasPrivateKey)
        {
            certificate.Dispose();
            throw new InvalidOperationException("Le P12 ne contient pas de clé privée utilisable.");
        }
        if (DateTime.Now < certificate.NotBefore || DateTime.Now > certificate.NotAfter)
        {
            certificate.Dispose();
            throw new InvalidOperationException("Le certificat du P12 n'est pas actuellement valide.");
        }
        return certificate;
    }

    private static HttpRequestMessage AuthorizedRequest(HttpMethod method, string path, string token)
    {
        var request = new HttpRequestMessage(method, path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        return request;
    }

    private async Task<T> SendAsync<T>(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        using var response = await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            var error = TryReadError(body);
            var message = error?.Message;
            if (string.IsNullOrWhiteSpace(message))
            {
                message = response.StatusCode == HttpStatusCode.BadRequest
                    ? "Le serveur HTTPS a refusé le certificat client ou la requête."
                    : $"Le serveur a répondu HTTP {(int)response.StatusCode}.";
            }
            throw new ApiException(error?.Code ?? $"http_{(int)response.StatusCode}", message, response.StatusCode);
        }

        try
        {
            return JsonSerializer.Deserialize<T>(body, JsonOptions)
                ?? throw new JsonException("Réponse JSON vide.");
        }
        catch (JsonException exception)
        {
            throw new ApiException("invalid_server_json", "La réponse du serveur n'est pas un JSON Yoleo valide.", response.StatusCode, exception);
        }
    }

    private static ApiError? TryReadError(string body)
    {
        try
        {
            return JsonSerializer.Deserialize<ApiErrorEnvelope>(body, JsonOptions)?.Error;
        }
        catch
        {
            return null;
        }
    }
}

internal sealed class ApiException : Exception
{
    public string Code { get; }
    public HttpStatusCode? StatusCode { get; }

    public ApiException(
        string code,
        string message,
        HttpStatusCode? statusCode = null,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
        StatusCode = statusCode;
    }
}
