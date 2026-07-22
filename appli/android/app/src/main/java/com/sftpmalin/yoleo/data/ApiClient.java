package com.sftpmalin.yoleo.data;

import android.os.Build;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.SecureRandom;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;

public final class ApiClient {
    private final String baseUrl;
    private final SSLContext sslContext;

    public ApiClient(AppSettings settings, File p12File, String p12Password) throws Exception {
        baseUrl = normalizeServerUrl(settings.serverUrl);
        sslContext = createSslContext(p12File, p12Password == null ? "" : p12Password);
    }

    public JSONObject health() throws Exception {
        JSONObject response = request("GET", "/api/v1/health", "", null);
        requireOk(response, "Le serveur n'a pas confirmé son état.");
        return response;
    }

    public String login(String username, String password) throws Exception {
        JSONObject body = new JSONObject();
        body.put("username", username == null ? "" : username.trim());
        body.put("password", password == null ? "" : password);
        body.put("device_name", deviceName());
        body.put("platform", "android");
        JSONObject response = request("POST", "/api/v1/auth/login", "", body);
        requireOk(response, "L'authentification a échoué.");
        JSONObject authentication = response.optJSONObject("authentication");
        String token = authentication == null ? "" : authentication.optString("access_token", "");
        if (token.isEmpty()) {
            throw new ApiException(500, "La réponse ne contient aucun jeton API.");
        }
        return token;
    }

    public JSONObject me(String accessToken) throws Exception {
        JSONObject response = request("GET", "/api/v1/me", accessToken, null);
        requireOk(response, "La vérification du jeton a échoué.");
        return response.optJSONObject("identity");
    }

    public JSONObject capabilities(String accessToken) throws Exception {
        JSONObject response = request("GET", "/api/v1/capabilities", accessToken, null);
        requireOk(response, "Les capacités du serveur sont indisponibles.");
        JSONObject capabilities = response.optJSONObject("capabilities");
        return capabilities == null ? new JSONObject() : capabilities;
    }

    public JSONObject monitoringSnapshot(String accessToken) throws Exception {
        JSONObject response = request("GET", "/api/v1/monitoring/snapshot", accessToken, null);
        requireOk(response, "Le cliché de surveillance est indisponible.");
        JSONObject monitoring = response.optJSONObject("monitoring");
        if (monitoring == null) {
            throw new ApiException(500, "La réponse ne contient aucun cliché de surveillance.");
        }
        return monitoring;
    }

    public JSONObject dockerAction(String accessToken, String containerId, String action) throws Exception {
        JSONObject body = new JSONObject();
        body.put("container_id", containerId == null ? "" : containerId.trim());
        body.put("action", action == null ? "" : action.trim());
        JSONObject response = request("POST", "/api/v1/docker/actions", accessToken, body);
        requireOk(response, "L'action Docker a échoué.");
        return response;
    }

    public JSONObject vmAction(String accessToken, String vmName, String action) throws Exception {
        JSONObject body = new JSONObject();
        body.put("name", vmName == null ? "" : vmName.trim());
        body.put("action", action == null ? "" : action.trim());
        JSONObject response = request("POST", "/api/v1/vm/actions", accessToken, body);
        requireOk(response, "L'action sur la machine virtuelle a échoué.");
        return response;
    }

    public JSONObject taskAction(String accessToken, int taskId, String action) throws Exception {
        JSONObject body = new JSONObject();
        body.put("task_id", taskId);
        body.put("action", action == null ? "" : action.trim());
        JSONObject response = request("POST", "/api/v1/task/actions", accessToken, body);
        requireOk(response, "L'action sur la tâche a échoué.");
        JSONObject result = response.optJSONObject("task_action");
        return result == null ? new JSONObject() : result;
    }

    public JSONObject backupAction(
            String accessToken,
            String filename,
            String action) throws Exception {
        JSONObject body = new JSONObject();
        body.put("filename", filename == null ? "" : filename.trim());
        body.put("action", action == null ? "" : action.trim());
        JSONObject response = request("POST", "/api/v1/backup/actions", accessToken, body);
        requireOk(response, "L'action Backup a échoué.");
        JSONObject result = response.optJSONObject("backup_action");
        return result == null ? new JSONObject() : result;
    }

    public JSONObject filesList(String accessToken, String path) throws Exception {
        JSONObject body = new JSONObject();
        body.put("path", path == null ? "" : path.trim());
        JSONObject response = request("POST", "/api/v1/files/list", accessToken, body);
        requireOk(response, "Le dossier est indisponible.");
        JSONObject files = response.optJSONObject("files");
        if (files == null) {
            throw new ApiException(500, "La réponse ne contient aucune liste de fichiers.");
        }
        return files;
    }

    public JSONObject fileCatalog(String accessToken, String path) throws Exception {
        JSONObject body = new JSONObject();
        body.put("path", path == null ? "" : path.trim());
        JSONObject response = request(
                "POST", "/api/v1/files/catalog", accessToken, body, 30 * 60_000);
        requireOk(response, "Le catalogue SHA-256 du NAS est indisponible.");
        JSONObject catalog = response.optJSONObject("catalog");
        if (catalog == null || catalog.optBoolean("truncated", false)) {
            throw new ApiException(503, "Le catalogue SHA-256 du NAS est incomplet.");
        }
        return catalog;
    }

    public JSONObject fileAction(
            String accessToken,
            String action,
            String source,
            String destination,
            String name) throws Exception {
        JSONObject body = new JSONObject();
        body.put("action", action == null ? "" : action.trim());
        if (source != null && !source.trim().isEmpty()) {
            body.put("source", source.trim());
        }
        if (destination != null && !destination.trim().isEmpty()) {
            if ("mkdir".equals(action)) {
                body.put("directory", destination.trim());
            } else {
                body.put("destination", destination.trim());
            }
        }
        if (name != null && !name.trim().isEmpty()) {
            body.put("name", name.trim());
        }
        JSONObject response = request("POST", "/api/v1/files/actions", accessToken, body);
        requireOk(response, "L'opération sur le fichier a échoué.");
        return response;
    }

    public JSONObject uploadFile(
            String accessToken,
            String directory,
            String filename,
            InputStream input) throws Exception {
        return uploadFile(accessToken, directory, filename, input, false);
    }

    public JSONObject uploadFile(
            String accessToken,
            String directory,
            String filename,
            InputStream input,
            boolean overwrite) throws Exception {
        return uploadFile(accessToken, directory, filename, input, overwrite, "");
    }

    public JSONObject uploadFile(
            String accessToken,
            String directory,
            String filename,
            InputStream input,
            boolean overwrite,
            String expectedSha256) throws Exception {
        if (input == null) {
            throw new IllegalArgumentException("Fichier Android illisible.");
        }
        String boundary = "YoleoAndroid" + System.nanoTime();
        HttpsURLConnection connection = (HttpsURLConnection) new URL(baseUrl + "/api/v1/files/upload").openConnection();
        prepareConnection(connection, "POST", accessToken);
        connection.setReadTimeout(30 * 60_000);
        connection.setDoOutput(true);
        connection.setChunkedStreamingMode(64 * 1024);
        connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
        String safeName = (filename == null ? "fichier" : filename)
                .replace("\r", " ").replace("\n", " ").replace('"', '\'');
        try (OutputStream output = connection.getOutputStream()) {
            writeUtf8(output, "--" + boundary + "\r\n" +
                    "Content-Disposition: form-data; name=\"path\"\r\n\r\n" +
                    (directory == null ? "" : directory) + "\r\n");
            writeUtf8(output, "--" + boundary + "\r\n" +
                    "Content-Disposition: form-data; name=\"overwrite\"\r\n\r\n" +
                    (overwrite ? "true" : "false") + "\r\n");
            if (expectedSha256 != null && !expectedSha256.trim().isEmpty()) {
                writeUtf8(output, "--" + boundary + "\r\n" +
                        "Content-Disposition: form-data; name=\"sha256\"\r\n\r\n" +
                        expectedSha256.trim().toLowerCase(java.util.Locale.ROOT) + "\r\n");
            }
            writeUtf8(output, "--" + boundary + "\r\n" +
                    "Content-Disposition: form-data; name=\"file\"; filename=\"" + safeName + "\"\r\n" +
                    "Content-Type: application/octet-stream\r\n\r\n");
            copy(input, output);
            writeUtf8(output, "\r\n--" + boundary + "--\r\n");
        }
        JSONObject response = readJsonResponse(connection);
        requireOk(response, "L'envoi du fichier a échoué.");
        return response;
    }

    public void downloadFile(
            String accessToken,
            String path,
            boolean directory,
            OutputStream output) throws Exception {
        if (output == null) {
            throw new IllegalArgumentException("Destination Android inaccessible.");
        }
        String encoded = URLEncoder.encode(path == null ? "" : path, StandardCharsets.UTF_8.name());
        HttpsURLConnection connection = (HttpsURLConnection) new URL(
                baseUrl + "/api/v1/files/download?path=" + encoded +
                        (directory ? "&archive=zip" : "")).openConnection();
        prepareConnection(connection, "GET", accessToken);
        connection.setReadTimeout(30 * 60_000);
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) {
            String text = connection.getErrorStream() == null ? "" : readText(connection.getErrorStream());
            connection.disconnect();
            try {
                throw new ApiException(status, errorMessage(new JSONObject(text), status));
            } catch (ApiException expected) {
                throw expected;
            } catch (Exception invalidJson) {
                throw new ApiException(status, "Téléchargement impossible (HTTP " + status + ").");
            }
        }
        try (InputStream input = connection.getInputStream()) {
            copy(input, output);
            output.flush();
        } finally {
            connection.disconnect();
        }
    }

    public byte[] downloadIcon(String rawUrl) throws Exception {
        String value = rawUrl == null ? "" : rawUrl.trim();
        if (value.isEmpty()) {
            return new byte[0];
        }
        URL server = new URL(baseUrl + "/");
        URL iconUrl = new URL(server, value);
        if (!"https".equalsIgnoreCase(iconUrl.getProtocol())) {
            throw new IllegalArgumentException("Seules les icônes HTTPS sont acceptées.");
        }

        HttpsURLConnection connection = (HttpsURLConnection) iconUrl.openConnection();
        if (sameOrigin(server, iconUrl)) {
            connection.setSSLSocketFactory(sslContext.getSocketFactory());
        }
        connection.setConnectTimeout(12_000);
        connection.setReadTimeout(20_000);
        connection.setInstanceFollowRedirects(false);
        connection.setRequestProperty("Accept", "image/png,image/webp,image/jpeg,image/*;q=0.8");
        connection.setRequestProperty("User-Agent", "Yoleo-Android/0.6.9");
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) {
            connection.disconnect();
            throw new ApiException(status, "Icône indisponible (HTTP " + status + ").");
        }
        try (InputStream input = connection.getInputStream()) {
            return readBytes(input, 2 * 1024 * 1024);
        } finally {
            connection.disconnect();
        }
    }

    private JSONObject request(
            String method,
            String path,
            String accessToken,
            JSONObject body) throws Exception {
        return request(method, path, accessToken, body, 60_000);
    }

    private JSONObject request(
            String method,
            String path,
            String accessToken,
            JSONObject body,
            int readTimeout) throws Exception {
        HttpsURLConnection connection = (HttpsURLConnection) new URL(baseUrl + path).openConnection();
        prepareConnection(connection, method, accessToken);
        connection.setReadTimeout(readTimeout);
        if (body != null) {
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(bytes.length);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            try (OutputStream output = connection.getOutputStream()) {
                output.write(bytes);
            }
        }

        return readJsonResponse(connection);
    }

    private void prepareConnection(
            HttpsURLConnection connection,
            String method,
            String accessToken) throws Exception {
        connection.setSSLSocketFactory(sslContext.getSocketFactory());
        connection.setConnectTimeout(15_000);
        connection.setReadTimeout(60_000);
        connection.setInstanceFollowRedirects(false);
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("User-Agent", "Yoleo-Android/0.6.9");
        if (accessToken != null && !accessToken.isEmpty()) {
            connection.setRequestProperty("Authorization", "Bearer " + accessToken);
        }
    }

    private static JSONObject readJsonResponse(HttpsURLConnection connection) throws Exception {
        int status = connection.getResponseCode();
        InputStream stream = status >= 200 && status < 400
                ? connection.getInputStream()
                : connection.getErrorStream();
        String text = stream == null ? "" : readText(stream);
        connection.disconnect();

        JSONObject json;
        try {
            json = text.isEmpty() ? new JSONObject() : new JSONObject(text);
        } catch (Exception invalidJson) {
            throw new ApiException(status, "Réponse HTTP " + status + " non JSON.");
        }
        if (status < 200 || status >= 300) {
            throw new ApiException(status, errorMessage(json, status));
        }
        return json;
    }

    private static void writeUtf8(OutputStream output, String value) throws Exception {
        output.write(value.getBytes(StandardCharsets.UTF_8));
    }

    private static void copy(InputStream input, OutputStream output) throws Exception {
        byte[] buffer = new byte[64 * 1024];
        int read;
        while ((read = input.read(buffer)) >= 0) {
            if (read > 0) {
                output.write(buffer, 0, read);
            }
        }
    }

    private static SSLContext createSslContext(File p12File, String password) throws Exception {
        if (p12File == null || !p12File.isFile() || p12File.length() == 0) {
            throw new IllegalArgumentException("Sélectionne un fichier P12 valide.");
        }
        char[] passwordChars = password.toCharArray();
        KeyStore clientStore = KeyStore.getInstance("PKCS12");
        try (FileInputStream input = new FileInputStream(p12File)) {
            clientStore.load(input, passwordChars);
        }
        KeyManagerFactory keyManagers = KeyManagerFactory.getInstance(
                KeyManagerFactory.getDefaultAlgorithm());
        keyManagers.init(clientStore, passwordChars);

        TrustManagerFactory trustManagers = TrustManagerFactory.getInstance(
                TrustManagerFactory.getDefaultAlgorithm());
        trustManagers.init((KeyStore) null);

        SSLContext context = SSLContext.getInstance("TLS");
        context.init(keyManagers.getKeyManagers(), trustManagers.getTrustManagers(), new SecureRandom());
        return context;
    }

    private static String normalizeServerUrl(String raw) {
        String value = raw == null ? "" : raw.trim();
        while (value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        if (value.endsWith("/api/v1")) {
            value = value.substring(0, value.length() - "/api/v1".length());
        }
        if (!value.startsWith("https://")) {
            throw new IllegalArgumentException("L'adresse du serveur doit commencer par https://");
        }
        return value;
    }

    private static void requireOk(JSONObject response, String fallback) throws ApiException {
        if (!response.optBoolean("ok", false)) {
            throw new ApiException(500, fallback);
        }
    }

    private static String errorMessage(JSONObject json, int status) {
        JSONObject error = json.optJSONObject("error");
        if (error != null) {
            String message = error.optString("message", "");
            if (!message.isEmpty()) {
                return message;
            }
            String code = error.optString("code", "");
            if (!code.isEmpty()) {
                return code;
            }
        }
        String message = json.optString("message", "");
        return message.isEmpty() ? "Erreur HTTP " + status : message;
    }

    private static String readText(InputStream input) throws Exception {
        try (InputStream stream = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8 * 1024];
            int read;
            while ((read = stream.read(buffer)) >= 0) {
                if (read > 0) {
                    output.write(buffer, 0, read);
                }
            }
            return output.toString(StandardCharsets.UTF_8.name());
        }
    }

    private static byte[] readBytes(InputStream input, int maximum) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8 * 1024];
        int read;
        int total = 0;
        while ((read = input.read(buffer)) >= 0) {
            if (read == 0) {
                continue;
            }
            total += read;
            if (total > maximum) {
                throw new IllegalArgumentException("L'icône téléchargée est trop volumineuse.");
            }
            output.write(buffer, 0, read);
        }
        return output.toByteArray();
    }

    private static boolean sameOrigin(URL first, URL second) {
        return first.getProtocol().equalsIgnoreCase(second.getProtocol()) &&
                first.getHost().equalsIgnoreCase(second.getHost()) &&
                effectivePort(first) == effectivePort(second);
    }

    private static int effectivePort(URL url) {
        return url.getPort() >= 0 ? url.getPort() : url.getDefaultPort();
    }

    private static String deviceName() {
        String manufacturer = Build.MANUFACTURER == null ? "Android" : Build.MANUFACTURER.trim();
        String model = Build.MODEL == null ? "" : Build.MODEL.trim();
        String name = (manufacturer + " " + model).trim();
        return name.isEmpty() ? "Appareil Android" : name;
    }

    public static final class ApiException extends Exception {
        public final int statusCode;

        public ApiException(int statusCode, String message) {
            super(message);
            this.statusCode = statusCode;
        }
    }
}
