package org.efootballrevival.bootstrap;

import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageInfo;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.lang.ref.WeakReference;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import org.json.JSONObject;

/** Call immediately after Activity.super.onCreate(). true means return from the original onCreate. */
public final class ResourceBootstrap {
    private static final Coordinator COORDINATOR = new Coordinator();
    private ResourceBootstrap() { }

    public static boolean intercept(Activity activity) {
        if (Looper.myLooper() != Looper.getMainLooper())
            throw new IllegalStateException("ResourceBootstrap must be attached on the main thread");
        if (COORDINATOR.passNextLaunch) {
            COORDINATOR.passNextLaunch = false;
            activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            return false;
        }
        Screen screen = new Screen(activity);
        COORDINATOR.screen = new WeakReference<Screen>(screen);
        screen.render(COORDINATOR.status);
        if (!COORDINATOR.running && !COORDINATOR.status.failed
                && !(COORDINATOR.resourcesReady && COORDINATOR.updateChoice))
            COORDINATOR.start(activity.getApplicationContext());
        return true;
    }

    private static final class Status {
        final String stage, detail;
        final long done, total;
        final boolean failed, complete;
        Status(String stage, String detail, long done, long total, boolean failed, boolean complete) {
            this.stage = stage; this.detail = detail; this.done = done; this.total = total;
            this.failed = failed; this.complete = complete;
        }
    }

    private static final class Coordinator implements ResourceInstaller.Listener {
        final Handler main = new Handler(Looper.getMainLooper());
        final ExecutorService worker = Executors.newSingleThreadExecutor(new ThreadFactory() {
            public Thread newThread(Runnable runnable) { return new Thread(runnable, "efootball-resource-bootstrap"); }
        });
        WeakReference<Screen> screen = new WeakReference<Screen>(null);
        volatile Status status = new Status("检查", "正在检查游戏资源…", 0, 0, false, false);
        boolean running, passNextLaunch;
        boolean resourcesReady, updateChoice;
        ResourceInstaller installer;
        ApkUpdater.Info offeredUpdate;
        File downloadedApk;
        Context application;
        private boolean paintQueued;
        private long lastPaint;

        void start(final Context context) {
            if (running) return;
            running = true;
            application = context;
            updateChoice = false;
            downloadedApk = null;
            offeredUpdate = null;
            status = new Status("检查", "正在读取资源清单…", 0, 0, false, false);
            paint();
            worker.execute(new Runnable() {
                public void run() {
                    Status result;
                    try {
                        ResourceInstaller.Manifest embedded = loadManifest(context);
                        PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
                        if (!embedded.packageName.equals(context.getPackageName()) || embedded.versionCode != info.getLongVersionCode())
                            throw new IOException("安装包与资源版本不一致，请安装匹配版本");
                        JSONObject config = new JSONObject(readAsset(context, "resource-bootstrap/config.json"));
                        try { ApkUpdater.cleanupInstalled(context, config.getLong("buildRevision")); }
                        catch (IOException cleanupFailure) { /* Cleanup must not block resource use. */ }
                        boolean online = hasInternet(context);
                        progress(online ? "检查更新" : "离线启动", online ? "正在检查资源和应用更新…" : "正在读取已准备的本地资源…", 0, 0);
                        UpdateChannel channel = new UpdateChannel(context.getFilesDir(), context.getPackageName(),
                                embedded.versionCode, config.getString("channelUrl"));
                        UpdateChannel.Selection selection = channel.resolve(embedded, config.getString("baseUrl"), online);
                        ApkUpdater.Info update = null;
                        try {
                            if (online && selection.apk != null) {
                                ApkUpdater.Info candidate = new ApkUpdater.Info(selection.apk);
                                if (candidate.buildRevision > config.getLong("buildRevision")) update = candidate;
                            }
                        } catch (Exception invalidApkMetadata) { /* Resource use must survive a malformed optional APK offer. */ }
                        installer = new ResourceInstaller(context.getFilesDir(), selection.manifest,
                                selection.baseUrl, Coordinator.this);
                        installer.install();
                        offeredUpdate = update;
                        resourcesReady = true;
                        updateChoice = update != null;
                        result = update == null
                                ? new Status("完成", "资源准备完成，正在进入游戏…", 1, 1, false, true)
                                : new Status("发现应用更新", "可更新至 " + update.versionName
                                    + "。更新会覆盖当前应用并保留数据，也可以先进入游戏。", 0, 0, false, false);
                    } catch (Exception error) {
                        String message = error.getMessage();
                        if (message == null || message.trim().isEmpty()) message = error.getClass().getSimpleName();
                        result = new Status("准备未完成", message, 0, 0, true, false);
                    }
                    final Status finished = result;
                    main.post(new Runnable() {
                        public void run() { running = false; status = finished; paint(); }
                    });
                }
            });
        }

        void downloadUpdate() {
            if (running || offeredUpdate == null || installer == null) return;
            running = true; updateChoice = false;
            downloadedApk = null;
            status = new Status("下载应用更新", "下载完成后将通过系统安装器覆盖更新", 0, 0, false, false);
            paint();
            worker.execute(new Runnable() {
                public void run() {
                    Status result;
                    try {
                        downloadedApk = ApkUpdater.downloadAndVerify(application, installer, offeredUpdate);
                        result = new Status("更新已下载", "点击“安装更新”覆盖当前应用。若系统要求授权，授权后返回此页面继续安装。", 1, 1, false, false);
                    } catch (Exception error) {
                        result = new Status("更新未完成", String.valueOf(error.getMessage()), 0, 0, true, false);
                    }
                    final Status finished = result;
                    main.post(new Runnable() {
                        public void run() { running = false; updateChoice = true; status = finished; paint(); }
                    });
                }
            });
        }

        void enterGame() {
            updateChoice = false;
            status = new Status("完成", "正在进入游戏…", 1, 1, false, true);
            paint();
        }

        public void progress(String stage, String detail, long done, long total) {
            status = new Status(stage, detail, done, total, false, false);
            synchronized (this) {
                if (paintQueued) return;
                paintQueued = true;
                long delay = Math.max(0, 160 - (SystemClock.uptimeMillis() - lastPaint));
                main.postDelayed(new Runnable() {
                    public void run() {
                        synchronized (Coordinator.this) { paintQueued = false; lastPaint = SystemClock.uptimeMillis(); }
                        paint();
                    }
                }, delay);
            }
        }

        void paint() {
            Screen current = screen.get();
            if (current == null || !current.alive()) return;
            current.render(status);
            if (status.complete && !current.entering) {
                current.entering = true;
                final WeakReference<Screen> expected = new WeakReference<Screen>(current);
                main.post(new Runnable() {
                    public void run() {
                        Screen target = expected.get();
                        if (target == null || screen.get() != target || !target.alive()) return;
                        passNextLaunch = true;
                        try { target.activity.recreate(); }
                        catch (RuntimeException error) {
                            passNextLaunch = false; target.entering = false;
                            status = new Status("准备未完成", "资源已准备好，请退出后重新打开游戏", 0, 0, true, false);
                            target.render(status);
                        }
                    }
                });
            }
        }
    }

    private static final class Screen {
        final Activity activity;
        final TextView title, statusText, detail, amount;
        final ProgressBar bar;
        final Button retry, update, play;
        boolean entering;
        Screen(final Activity activity) {
            this.activity = activity;
            activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            ScrollView scroll = new ScrollView(activity);
            scroll.setFillViewport(true);
            scroll.setBackgroundColor(Color.rgb(15, 21, 32));
            LinearLayout column = new LinearLayout(activity);
            column.setOrientation(LinearLayout.VERTICAL);
            column.setGravity(Gravity.CENTER_VERTICAL);
            int padding = dp(28);
            column.setPadding(padding, padding, padding, padding);
            scroll.addView(column, new ScrollView.LayoutParams(-1, -1));
            title = text("efootxi", 26, Color.WHITE);
            title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            column.addView(title, layout(0));
            TextView note = text("首次启动自动准备游戏资源；以后检查新资源并复用已下载文件。\n\n建议连接 Wi-Fi，并在下载期间保持此页面打开。", 16, Color.rgb(192, 204, 221));
            column.addView(note, layout(20));
            statusText = text("检查", 19, Color.rgb(94, 199, 250));
            column.addView(statusText, layout(30));
            detail = text("", 14, Color.rgb(192, 204, 221));
            detail.setTextIsSelectable(true);
            column.addView(detail, layout(10));
            bar = new ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal);
            bar.setMax(1000);
            column.addView(bar, layout(20));
            amount = text("", 14, Color.rgb(192, 204, 221));
            column.addView(amount, layout(8));
            retry = new Button(activity);
            retry.setText("重试");
            retry.setAllCaps(false);
            retry.setVisibility(View.GONE);
            retry.setOnClickListener(new View.OnClickListener() {
                public void onClick(View view) {
                    if (alive()) COORDINATOR.start(activity.getApplicationContext());
                }
            });
            column.addView(retry, layout(20));
            update = new Button(activity);
            update.setAllCaps(false);
            update.setVisibility(View.GONE);
            update.setOnClickListener(new View.OnClickListener() {
                public void onClick(View view) {
                    if (!alive()) return;
                    if (COORDINATOR.downloadedApk == null) { COORDINATOR.downloadUpdate(); return; }
                    try {
                        boolean opened = ApkUpdater.requestInstall(activity, COORDINATOR.downloadedApk);
                        COORDINATOR.status = new Status("安装更新", opened
                                ? "请在系统安装器中确认更新；取消后仍可进入游戏。"
                                : "请允许此应用安装更新，然后返回点击“安装更新”。", 1, 1, false, false);
                    } catch (Exception error) {
                        COORDINATOR.status = new Status("安装未完成", String.valueOf(error.getMessage()), 0, 0, true, false);
                    }
                    COORDINATOR.paint();
                }
            });
            column.addView(update, layout(12));
            play = new Button(activity);
            play.setText("先进入游戏");
            play.setVisibility(View.GONE);
            play.setOnClickListener(new View.OnClickListener() {
                public void onClick(View view) { if (alive()) COORDINATOR.enterGame(); }
            });
            column.addView(play, layout(8));
            activity.setContentView(scroll);
        }
        boolean alive() { return !activity.isFinishing() && !activity.isDestroyed(); }
        void render(Status status) {
            if (!alive()) return;
            statusText.setText(status.stage);
            detail.setText(status.detail);
            boolean choosing = COORDINATOR.updateChoice && COORDINATOR.resourcesReady && !COORDINATOR.running;
            update.setVisibility(choosing ? View.VISIBLE : View.GONE);
            play.setVisibility(choosing ? View.VISIBLE : View.GONE);
            update.setText(COORDINATOR.downloadedApk == null ? "下载并更新应用" : "安装更新");
            retry.setVisibility(status.failed && !choosing ? View.VISIBLE : View.GONE);
            bar.setVisibility(status.failed || choosing ? View.GONE : View.VISIBLE);
            bar.setIndeterminate(status.total <= 0 && !status.failed);
            if (status.total > 0) {
                int progress = (int) Math.min(1000d, Math.max(0d, 1000d * status.done / status.total));
                bar.setProgress(progress);
                if (status.total > 1024)
                    amount.setText(String.format(Locale.SIMPLIFIED_CHINESE, "%.1f%%  ·  %.1f / %.1f MB", progress / 10d, status.done / 1e6, status.total / 1e6));
                else amount.setText(String.format(Locale.SIMPLIFIED_CHINESE, "%.1f%%", progress / 10d));
            } else amount.setText(status.failed ? "已完成的资源会保留；重新连接网络或释放空间后重试。" : "请稍候…");
            if (status.failed || status.complete || choosing)
                activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            else activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
        private TextView text(String value, float size, int color) {
            TextView view = new TextView(activity);
            view.setText(value); view.setTextSize(size); view.setTextColor(color);
            view.setLineSpacing(dp(3), 1f);
            return view;
        }
        private LinearLayout.LayoutParams layout(int top) {
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
            params.topMargin = dp(top); return params;
        }
        private int dp(int value) { return (int) (value * activity.getResources().getDisplayMetrics().density + 0.5f); }
    }

    private static ResourceInstaller.Manifest loadManifest(Context context) throws Exception {
        return UpdateChannel.parseManifest(readAsset(context, "resource-bootstrap/manifest.json").getBytes(StandardCharsets.UTF_8));
    }

    private static boolean hasInternet(Context context) {
        try {
            ConnectivityManager manager = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            Network network = manager == null ? null : manager.getActiveNetwork();
            NetworkCapabilities capabilities = network == null ? null : manager.getNetworkCapabilities(network);
            return capabilities != null && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                    && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
        } catch (RuntimeException unavailable) { return false; }
    }

    private static String readAsset(Context context, String name) throws IOException {
        try (InputStream input = context.getAssets().open(name); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192]; int read;
            while ((read = input.read(buffer)) != -1) {
                if (output.size() + read > 512 * 1024) throw new IOException("资源配置文件过大");
                output.write(buffer, 0, read);
            }
            return new String(output.toByteArray(), StandardCharsets.UTF_8);
        }
    }
}
