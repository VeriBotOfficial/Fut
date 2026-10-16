package org.efootballrevival.bootstrap;

import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageInfo;
import android.graphics.Color;
import android.graphics.Typeface;
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
import java.io.IOException;
import java.io.InputStream;
import java.lang.ref.WeakReference;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import org.json.JSONArray;
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
        if (!COORDINATOR.running && !COORDINATOR.status.failed)
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
        private boolean paintQueued;
        private long lastPaint;

        void start(final Context context) {
            if (running) return;
            running = true;
            status = new Status("检查", "正在读取资源清单…", 0, 0, false, false);
            paint();
            worker.execute(new Runnable() {
                public void run() {
                    Status result;
                    try {
                        ResourceInstaller.Manifest manifest = loadManifest(context);
                        PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
                        if (!manifest.packageName.equals(context.getPackageName()) || manifest.versionCode != info.getLongVersionCode())
                            throw new IOException("安装包与资源版本不一致，请安装匹配版本");
                        JSONObject config = new JSONObject(readAsset(context, "resource-bootstrap/config.json"));
                        ResourceInstaller installer = new ResourceInstaller(context.getFilesDir(), manifest,
                                config.getString("baseUrl"), Coordinator.this);
                        installer.install();
                        result = new Status("完成", "资源准备完成，正在进入游戏…", 1, 1, false, true);
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
        final Button retry;
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
            title = text("准备游戏资源", 26, Color.WHITE);
            title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
            column.addView(title, layout(0));
            TextView note = text("首次启动需要准备约 3 GB 资源。准备完成后会自动进入游戏。\n\n建议连接 Wi-Fi，并在准备期间保持此页面打开。退出后再次打开可以继续下载。", 16, Color.rgb(192, 204, 221));
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
            activity.setContentView(scroll);
        }
        boolean alive() { return !activity.isFinishing() && !activity.isDestroyed(); }
        void render(Status status) {
            if (!alive()) return;
            statusText.setText(status.stage);
            detail.setText(status.detail);
            retry.setVisibility(status.failed ? View.VISIBLE : View.GONE);
            bar.setVisibility(status.failed ? View.GONE : View.VISIBLE);
            bar.setIndeterminate(status.total <= 0 && !status.failed);
            if (status.total > 0) {
                int progress = (int) Math.min(1000d, Math.max(0d, 1000d * status.done / status.total));
                bar.setProgress(progress);
                if (status.total > 1024)
                    amount.setText(String.format(Locale.SIMPLIFIED_CHINESE, "%.1f%%  ·  %.1f / %.1f MB", progress / 10d, status.done / 1e6, status.total / 1e6));
                else amount.setText(String.format(Locale.SIMPLIFIED_CHINESE, "%.1f%%", progress / 10d));
            } else amount.setText(status.failed ? "已完成的资源会保留；重新连接网络或释放空间后重试。" : "请稍候…");
            if (status.failed || status.complete)
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
        JSONObject root = new JSONObject(readAsset(context, "resource-bootstrap/manifest.json"));
        if (root.getInt("schemaVersion") != 1) throw new IOException("不支持的资源清单版本");
        JSONArray files = root.getJSONArray("files");
        ArrayList<ResourceInstaller.Pack> packs = new ArrayList<ResourceInstaller.Pack>();
        for (int i = 0; i < files.length(); i++) {
            JSONObject file = files.getJSONObject(i);
            JSONArray members = file.getJSONArray("entries");
            ArrayList<ResourceInstaller.Entry> entries = new ArrayList<ResourceInstaller.Entry>();
            long expanded = 0;
            for (int j = 0; j < members.length(); j++) {
                JSONObject member = members.getJSONObject(j);
                ResourceInstaller.Entry entry = new ResourceInstaller.Entry(member.getString("path"),
                        member.getLong("bytes"), member.getString("sha256"));
                entries.add(entry); expanded = Math.addExact(expanded, entry.bytes);
            }
            if (file.getLong("expandedBytes") != expanded) throw new IOException("资源清单展开大小不一致");
            packs.add(new ResourceInstaller.Pack(file.getString("name"), file.getLong("bytes"), file.getString("sha256"), entries));
        }
        return new ResourceInstaller.Manifest(root.getString("packageName"), root.getLong("versionCode"),
                root.getString("resourceVersion"), packs);
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
