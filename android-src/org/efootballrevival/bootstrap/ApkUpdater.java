package org.efootballrevival.bootstrap;

import android.app.Activity;
import android.content.ClipData;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.net.URL;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Enumeration;
import java.util.Locale;
import java.util.Properties;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import org.json.JSONException;
import org.json.JSONObject;

/** Downloads an in-place update; the Android installer retains final user confirmation. */
public final class ApkUpdater {
    static final String FILE_NAME = "efootxi-update.apk";
    static final String URI_PATH = "/update.apk";
    static final String AUTHORITY_SUFFIX = ".efootxi.update";
    static final String MIME = "application/vnd.android.package-archive";

    private ApkUpdater() { }

    public static final class Info {
        public final long buildRevision, versionCode, bytes;
        public final int minSdk;
        public final String url, sha256, signerCertificateSha256, abi, versionName;

        public Info(JSONObject value) throws IOException {
            try {
                buildRevision = positiveInteger(value, "buildRevision");
                versionCode = positiveInteger(value, "versionCode");
                bytes = positiveInteger(value, "bytes");
                long sdk = positiveInteger(value, "minSdk");
                if (sdk > Integer.MAX_VALUE || bytes >= 2L * 1024 * 1024 * 1024)
                    throw new IOException("应用更新大小或系统版本无效");
                minSdk = (int) sdk;
                url = value.getString("url");
                sha256 = hashValue(value, "sha256");
                signerCertificateSha256 = hashValue(value, "signerCertificateSha256");
                abi = value.getString("abi");
                versionName = value.getString("versionName");
                if (!"arm64-v8a".equals(abi) || versionName.isEmpty() || versionName.length() > 100)
                    throw new IOException("应用更新版本或架构无效");
                URL address = new URL(url);
                ResourceInstaller.validateUrl(address);
                if (!"github.com".equalsIgnoreCase(address.getHost())
                        || address.getUserInfo() != null || address.getQuery() != null
                        || address.getRef() != null || (address.getPort() != -1 && address.getPort() != 443)
                        || !address.getPath().matches("/[^/]+/[^/]+/releases/download/[^/]+/[^/]+\\.apk"))
                    throw new IOException("应用更新地址必须是 GitHub Release APK");
            } catch (JSONException error) {
                throw new IOException("应用更新清单不完整", error);
            }
        }

        private static long positiveInteger(JSONObject value, String key) throws JSONException, IOException {
            Object number = value.get(key);
            if (!(number instanceof Integer) && !(number instanceof Long))
                throw new IOException("应用更新数值无效：" + key);
            long result = ((Number) number).longValue();
            if (result <= 0) throw new IOException("应用更新数值无效：" + key);
            return result;
        }

        private static String hashValue(JSONObject value, String key) throws JSONException, IOException {
            String hash = value.getString(key).toLowerCase(Locale.ROOT);
            if (!hash.matches("[0-9a-f]{64}")) throw new IOException("应用更新校验值无效：" + key);
            return hash;
        }
    }

    /** Call on a worker thread; both the trusted digest and actual APK identity must match. */
    public static synchronized File downloadAndVerify(Context context, ResourceInstaller installer, Info info)
            throws IOException {
        if (Build.VERSION.SDK_INT < info.minSdk || !supportsAbi(info.abi))
            throw new IOException("这个更新不适用于当前手机");
        PackageManager manager = context.getPackageManager();
        PackageInfo installed;
        try {
            installed = manager.getPackageInfo(context.getPackageName(), PackageManager.GET_SIGNING_CERTIFICATES);
        } catch (PackageManager.NameNotFoundException error) {
            throw new IOException("无法读取当前应用信息", error);
        }
        if (info.versionCode < installed.getLongVersionCode())
            throw new IOException("更新版本低于已安装版本");
        String installedSigner = signer(installed);
        if (!installedSigner.equals(info.signerCertificateSha256))
            throw new IOException("更新签名与当前应用不一致，已停止更新");

        File file = installer.downloadArtifact(FILE_NAME, info.bytes, info.sha256, info.url);
        if (!file.getCanonicalFile().equals(updateFile(context)) || !file.isFile() || file.length() != info.bytes)
            throw new IOException("应用更新文件位置或大小不正确");
        PackageInfo candidate = manager.getPackageArchiveInfo(file.getAbsolutePath(), PackageManager.GET_SIGNING_CERTIFICATES);
        if (candidate == null || !context.getPackageName().equals(candidate.packageName))
            throw new IOException("下载的安装包不是当前应用");
        if (candidate.getLongVersionCode() != info.versionCode
                || candidate.getLongVersionCode() < installed.getLongVersionCode()
                || !info.versionName.equals(candidate.versionName))
            throw new IOException("下载的安装包版本与更新说明不一致");
        if (candidate.applicationInfo == null || candidate.applicationInfo.minSdkVersion != info.minSdk
                || candidate.applicationInfo.minSdkVersion > Build.VERSION.SDK_INT)
            throw new IOException("下载的安装包不支持当前系统");
        if (!installedSigner.equals(signer(candidate)))
            throw new IOException("下载的安装包签名与当前应用不一致");
        boolean containsAbi = false;
        try (ZipFile archive = new ZipFile(file)) {
            Enumeration<? extends ZipEntry> entries = archive.entries();
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                if (!entry.isDirectory() && entry.getName().startsWith("lib/" + info.abi + "/")
                        && entry.getName().endsWith(".so")) { containsAbi = true; break; }
            }
        }
        if (!containsAbi) throw new IOException("下载的安装包缺少手机所需架构");
        recordVerifiedDownload(file, info);
        return file;
    }

    /** Best-effort maintenance on a worker thread, only after this APK's build is installed. */
    public static synchronized void cleanupInstalled(Context context, long currentBuildRevision) throws IOException {
        cleanupInstalledFile(updateFile(context), currentBuildRevision);
    }

    static void cleanupInstalledFile(File file, long currentBuildRevision) throws IOException {
        File receipt = receiptFile(file, "efootxi-update.receipt");
        if (currentBuildRevision <= 0 || !receipt.isFile() || receipt.length() > 4096) return;
        Properties properties = new Properties();
        try (FileInputStream input = new FileInputStream(receipt)) { properties.load(input); }
        long revision, bytes;
        String sha = properties.getProperty("sha256", "");
        try {
            revision = Long.parseLong(properties.getProperty("buildRevision", ""));
            bytes = Long.parseLong(properties.getProperty("bytes", ""));
        } catch (NumberFormatException malformed) { return; }
        if (revision <= 0 || bytes <= 0 || !sha.matches("[0-9a-f]{64}")
                || !"1".equals(properties.getProperty("schemaVersion"))
                || revision > currentBuildRevision) return;
        // A new download may have replaced the fixed filename before its receipt was committed.
        // Never delete that newer candidate, nor any .part/.resume files belonging to a later update.
        if (file.isFile() && file.length() == bytes && sha.equals(hashFile(file)))
            Files.deleteIfExists(file.toPath());
        Files.deleteIfExists(receipt.toPath());
    }

    private static void recordVerifiedDownload(File file, Info info) throws IOException {
        Properties receipt = new Properties();
        receipt.setProperty("schemaVersion", "1");
        receipt.setProperty("buildRevision", Long.toString(info.buildRevision));
        receipt.setProperty("bytes", Long.toString(info.bytes));
        receipt.setProperty("sha256", info.sha256);
        File destination = receiptFile(file, "efootxi-update.receipt");
        File temporary = receiptFile(file, "efootxi-update.receipt.tmp");
        try (FileOutputStream output = new FileOutputStream(temporary)) {
            receipt.store(output, "Written only after APK identity and signing certificate verification");
            output.getFD().sync();
        }
        Files.move(temporary.toPath(), destination.toPath(),
                StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }

    private static File receiptFile(File update, String name) throws IOException {
        File directory = update.getParentFile().getCanonicalFile();
        File expected = new File(directory, name);
        if (!expected.equals(expected.getCanonicalFile())) throw new IOException("更新记录路径无效");
        return expected;
    }

    private static String hashFile(File file) throws IOException {
        try {
            MessageDigest hash = MessageDigest.getInstance("SHA-256");
            try (FileInputStream input = new FileInputStream(file)) {
                byte[] buffer = new byte[128 * 1024];
                int count;
                while ((count = input.read(buffer)) != -1) hash.update(buffer, 0, count);
            }
            return ResourceInstaller.hex(hash.digest());
        } catch (NoSuchAlgorithmException error) {
            throw new IOException("系统不支持更新文件校验", error);
        }
    }

    /** false: permission settings opened; true: the system update installer opened. */
    public static boolean requestInstall(Activity activity, File file) throws IOException {
        if (!file.getCanonicalFile().equals(updateFile(activity)) || !file.isFile() || file.length() == 0)
            throw new IOException("更新文件不存在，请重新下载更新");
        try {
            if (!activity.getPackageManager().canRequestPackageInstalls()) {
                Intent permission = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:" + activity.getPackageName()));
                activity.startActivity(permission);
                return false;
            }
            Uri uri = new Uri.Builder().scheme("content")
                    .authority(activity.getPackageName() + AUTHORITY_SUFFIX).path(URI_PATH).build();
            Intent install = new Intent(Intent.ACTION_VIEW).setDataAndType(uri, MIME)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            install.setClipData(ClipData.newRawUri("Fut 更新", uri));
            activity.startActivity(install);
            return true;
        } catch (RuntimeException error) {
            throw new IOException("无法打开系统安装器，请检查允许安装应用的设置", error);
        }
    }

    static File updateFile(Context context) throws IOException {
        File root = context.getFilesDir().getCanonicalFile();
        File expected = new File(new File(root, "resource-bootstrap-state"), FILE_NAME);
        File resolved = expected.getCanonicalFile();
        if (!resolved.equals(expected)) throw new IOException("更新文件路径无效");
        return resolved;
    }

    private static boolean supportsAbi(String abi) {
        for (String supported : Build.SUPPORTED_ABIS) if (abi.equals(supported)) return true;
        return false;
    }

    private static String signer(PackageInfo info) throws IOException {
        Signature[] signatures = info.signingInfo == null ? null : info.signingInfo.getApkContentsSigners();
        if (signatures == null || signatures.length != 1)
            throw new IOException("无法核实应用签名");
        try {
            return ResourceInstaller.hex(MessageDigest.getInstance("SHA-256").digest(signatures[0].toByteArray()));
        } catch (NoSuchAlgorithmException error) {
            throw new IOException("系统不支持签名校验", error);
        }
    }
}
