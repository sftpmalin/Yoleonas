package com.sftpmalin.yoleo.data;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

public final class IconCache {
    private final File directory;

    public IconCache(Context context) {
        directory = new File(context.getApplicationContext().getFilesDir(), "docker_icons");
        if (!directory.isDirectory()) {
            directory.mkdirs();
        }
    }

    public synchronized Bitmap load(String iconUrl, ApiClient client) throws Exception {
        String value = iconUrl == null ? "" : iconUrl.trim();
        if (value.isEmpty()) {
            return null;
        }
        File cached = new File(directory, sha256(value) + ".img");
        Bitmap bitmap = cached.isFile() ? BitmapFactory.decodeFile(cached.getAbsolutePath()) : null;
        if (bitmap != null) {
            return bitmap;
        }
        if (cached.exists()) {
            cached.delete();
        }

        byte[] bytes = client.downloadIcon(value);
        bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.length);
        if (bitmap == null) {
            throw new IllegalArgumentException("Le fichier reçu n'est pas une icône reconnue.");
        }

        File temporary = new File(directory, cached.getName() + ".tmp");
        try (FileOutputStream output = new FileOutputStream(temporary, false)) {
            output.write(bytes);
            output.getFD().sync();
        }
        if (cached.exists()) {
            cached.delete();
        }
        if (!temporary.renameTo(cached)) {
            temporary.delete();
        }
        return bitmap;
    }

    private static String sha256(String value) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(value.getBytes(StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder(digest.length * 2);
        for (byte item : digest) {
            hex.append(String.format("%02x", item & 0xff));
        }
        return hex.toString();
    }
}
