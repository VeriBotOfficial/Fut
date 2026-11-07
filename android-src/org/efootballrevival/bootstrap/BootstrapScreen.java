package org.efootballrevival.bootstrap;

import android.app.ActionBar;
import android.app.Activity;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import java.util.Locale;

/** Minimal launcher presentation. All installation and launch work belongs to the coordinator. */
public final class BootstrapScreen {
    public interface Listener {
        void onPause();
        void onResume();
        void onRetry();
        void onDownloadResources();
        void onDownloadUpdate();
        void onInstallUpdate();
        void onEnterGame();
    }

    public enum Stage {
        CHECKING, VERIFYING, DOWNLOADING, DEPLOYING, READY, PAUSED, ERROR,
        UPDATE_AVAILABLE, INSTALL_READY, NEEDS_DOWNLOAD
    }
    public enum ProgressUnit { BYTES, FILES, NONE }

    public static final class Model {
        public final Stage stage;
        public final String versionName, resourceVersion, title, message, taskLabel;
        public final String details, errorDetails, enterGameLabel;
        public final long buildRevision, done, total;
        public final ProgressUnit unit;
        public final double bytesPerSecond;
        public final boolean online, resourcesReady;
        public final boolean canPause, canResume, canRetry, canDownloadResources;
        public final boolean canDownloadUpdate, canInstallUpdate, canEnterGame;

        private Model(Builder b) {
            stage = b.stage;
            versionName = clean(b.versionName);
            resourceVersion = clean(b.resourceVersion);
            title = clean(b.title);
            message = clean(b.message);
            taskLabel = clean(b.taskLabel);
            details = clean(b.details);
            errorDetails = clean(b.errorDetails);
            enterGameLabel = clean(b.enterGameLabel);
            buildRevision = b.buildRevision;
            done = Math.max(0, b.done);
            total = b.total;
            unit = b.unit == null ? ProgressUnit.NONE : b.unit;
            bytesPerSecond = Double.isNaN(b.bytesPerSecond) || Double.isInfinite(b.bytesPerSecond)
                    ? 0 : Math.max(0, b.bytesPerSecond);
            online = b.online;
            resourcesReady = b.resourcesReady;
            canPause = b.canPause;
            canResume = b.canResume;
            canRetry = b.canRetry;
            canDownloadResources = b.canDownloadResources;
            canDownloadUpdate = b.canDownloadUpdate;
            canInstallUpdate = b.canInstallUpdate;
            canEnterGame = b.canEnterGame && b.resourcesReady;
        }

        public static final class Builder {
            private final Stage stage;
            private String versionName = "", resourceVersion = "", title = "", message = "", taskLabel = "";
            private String details = "", errorDetails = "", enterGameLabel = "Play";
            private long buildRevision, done, total;
            private ProgressUnit unit = ProgressUnit.NONE;
            private double bytesPerSecond;
            private boolean online, resourcesReady, canPause, canResume, canRetry;
            private boolean canDownloadResources, canDownloadUpdate, canInstallUpdate, canEnterGame;

            public Builder(Stage stage) { this.stage = stage == null ? Stage.CHECKING : stage; }
            public Builder version(String name, long revision) { versionName = name; buildRevision = revision; return this; }
            public Builder resourceVersion(String value) { resourceVersion = value; return this; }
            public Builder network(boolean value) { online = value; return this; }
            public Builder resourcesReady(boolean value) { resourcesReady = value; return this; }
            public Builder text(String heading, String explanation) { title = heading; message = explanation; return this; }
            public Builder progress(String label, long completed, long expected, ProgressUnit progressUnit) {
                taskLabel = label; done = completed; total = expected; unit = progressUnit; return this;
            }
            public Builder speed(double value) { bytesPerSecond = value; return this; }
            public Builder details(String value) { details = value; return this; }
            public Builder errorDetails(String value) { errorDetails = value; return this; }
            public Builder canPause(boolean value) { canPause = value; return this; }
            public Builder canResume(boolean value) { canResume = value; return this; }
            public Builder canRetry(boolean value) { canRetry = value; return this; }
            public Builder canDownloadResources(boolean value) { canDownloadResources = value; return this; }
            public Builder canDownloadUpdate(boolean value) { canDownloadUpdate = value; return this; }
            public Builder canInstallUpdate(boolean value) { canInstallUpdate = value; return this; }
            public Builder canEnterGame(boolean value) { canEnterGame = value; return this; }
            public Builder enterGameLabel(String value) { enterGameLabel = value; return this; }
            public Model build() { return new Model(this); }
        }
    }

    private static final int BG = Color.rgb(247, 248, 250);
    private static final int INK = Color.rgb(24, 28, 35);
    private static final int MUTED = Color.rgb(112, 119, 130);
    private static final int ACCENT = Color.rgb(34, 91, 190);
    private static final int LINE = Color.rgb(228, 231, 236);

    private final Activity activity;
    private final Listener listener;
    private final LinearLayout root, content;
    private final TextView title, message, status, progressText, errorText;
    private final ProgressBar progress;
    private final Button primary, secondary, errorToggle;
    private Model model;
    private boolean detailsExpanded;
    private String lastError = "";

    public BootstrapScreen(Activity activity, Listener listener) {
        if (activity == null || listener == null) throw new IllegalArgumentException("Activity and listener are required");
        this.activity = activity;
        this.listener = listener;
        try { activity.requestWindowFeature(Window.FEATURE_NO_TITLE); }
        catch (RuntimeException ignored) { }
        ActionBar bar = activity.getActionBar();
        if (bar != null) bar.hide();

        Window window = activity.getWindow();
        window.setStatusBarColor(BG);
        window.setNavigationBarColor(BG);
        window.clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
        window.setDecorFitsSystemWindows(true);
        WindowInsetsController controller = window.getInsetsController();
        if (controller != null) {
            controller.show(WindowInsets.Type.systemBars());
            controller.setSystemBarsAppearance(WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                            | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS,
                    WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                            | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS);
        }

        root = new LinearLayout(activity);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setBackgroundColor(BG);
        root.setPadding(dp(26), dp(28), dp(26), dp(24));

        content = new LinearLayout(activity);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setGravity(Gravity.CENTER_HORIZONTAL);
        content.setPadding(dp(22), dp(26), dp(22), dp(24));
        content.setBackground(shape(Color.WHITE, 24, LINE));
        content.setElevation(dp(2));
        root.addView(content, new LinearLayout.LayoutParams(-1, -2));

        TextView brand = text("Fut", 38, INK, true);
        brand.setGravity(Gravity.CENTER);
        content.addView(brand, matchWrap(0));

        TextView tagline = text("Your game. Ready when you are.", 15, MUTED, false);
        tagline.setGravity(Gravity.CENTER);
        content.addView(tagline, margin(7));

        title = text("Checking your game", 23, INK, true);
        title.setGravity(Gravity.CENTER);
        content.addView(title, margin(34));

        message = text("Checking installed files and updates.", 15, MUTED, false);
        message.setGravity(Gravity.CENTER);
        content.addView(message, margin(9));

        progress = new ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(1000);
        progress.setProgressTintList(ColorStateList.valueOf(ACCENT));
        progress.setProgressBackgroundTintList(ColorStateList.valueOf(LINE));
        LinearLayout.LayoutParams progressParams = new LinearLayout.LayoutParams(-1, dp(7));
        progressParams.topMargin = dp(25);
        content.addView(progress, progressParams);

        progressText = text("", 13, MUTED, false);
        progressText.setGravity(Gravity.CENTER);
        content.addView(progressText, margin(10));

        status = text("", 13, MUTED, false);
        status.setGravity(Gravity.CENTER);
        content.addView(status, margin(18));

        primary = button("Download game data", true);
        primary.setOnClickListener(v -> {
            if (model == null) return;
            if (model.canDownloadResources) listener.onDownloadResources();
            else if (model.canEnterGame) listener.onEnterGame();
            else if (model.canInstallUpdate) listener.onInstallUpdate();
            else if (model.canDownloadUpdate) listener.onDownloadUpdate();
            else if (model.canRetry) listener.onRetry();
            else if (model.canResume) listener.onResume();
        });
        LinearLayout.LayoutParams primaryParams = new LinearLayout.LayoutParams(-1, dp(62));
        primaryParams.topMargin = dp(26);
        content.addView(primary, primaryParams);

        secondary = button("Pause", false);
        secondary.setOnClickListener(v -> {
            if (model == null) return;
            if (model.canPause) listener.onPause();
            else if (model.canResume) listener.onResume();
        });
        content.addView(secondary, margin(10));

        errorToggle = button("Show error details", false);
        errorToggle.setOnClickListener(v -> {
            detailsExpanded = !detailsExpanded;
            updateError();
        });
        content.addView(errorToggle, margin(8));

        errorText = text("", 12, MUTED, false);
        errorText.setTypeface(Typeface.MONOSPACE);
        errorText.setPadding(dp(12), dp(12), dp(12), dp(12));
        errorText.setBackground(shape(BG, 10, LINE));
        content.addView(errorText, margin(8));

        TextView footer = text("Initial download required · Updates checked automatically", 12, MUTED, false);
        footer.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams footerParams = margin(22);
        root.addView(footer, footerParams);

        activity.setContentView(root);
        render(new Model.Builder(Stage.CHECKING).build());
    }

    public boolean alive() { return !activity.isFinishing() && !activity.isDestroyed(); }

    public void render(Model next) {
        if (!alive() || next == null) return;
        model = next;
        title.setText(next.title.isEmpty() ? defaultTitle(next.stage) : next.title);
        message.setText(next.message.isEmpty() ? defaultMessage(next.stage) : next.message);

        boolean active = next.stage == Stage.CHECKING || next.stage == Stage.VERIFYING
                || next.stage == Stage.DOWNLOADING || next.stage == Stage.DEPLOYING;
        boolean showProgress = active || next.stage == Stage.PAUSED;
        progress.setVisibility(showProgress ? View.VISIBLE : View.GONE);
        progressText.setVisibility(showProgress ? View.VISIBLE : View.GONE);
        progress.setIndeterminate(active && next.total <= 0);
        if (next.total > 0) {
            double ratio = Math.min(1d, (double) next.done / (double) next.total);
            progress.setProgress((int) (ratio * 1000d));
            String amount = next.unit == ProgressUnit.BYTES
                    ? bytes(next.done) + " / " + bytes(next.total)
                    : next.done + " / " + next.total + " files";
            String speed = next.stage == Stage.DOWNLOADING && next.bytesPerSecond > 0
                    ? " · " + bytes((long) next.bytesPerSecond) + "/s" : "";
            progressText.setText(String.format(Locale.ROOT, "%.0f%% · %s%s", ratio * 100d, amount, speed));
        } else {
            progressText.setText(next.taskLabel.isEmpty() ? "Working…" : next.taskLabel);
        }

        status.setText(next.resourcesReady ? "Game data is ready"
                : next.online ? "Internet connection available" : "Offline");
        status.setVisibility(next.stage == Stage.NEEDS_DOWNLOAD || next.stage == Stage.READY ? View.VISIBLE : View.GONE);

        String actionLabel = "Download game data";
        boolean showPrimary = false;
        if (next.canDownloadResources) { actionLabel = "Download game data"; showPrimary = true; }
        else if (next.canEnterGame) { actionLabel = "Play"; showPrimary = true; }
        else if (next.canInstallUpdate) { actionLabel = "Install app update"; showPrimary = true; }
        else if (next.canDownloadUpdate) { actionLabel = "Download app update"; showPrimary = true; }
        else if (next.canRetry) { actionLabel = "Try again"; showPrimary = true; }
        else if (next.canResume) { actionLabel = "Resume download"; showPrimary = true; }
        primary.setText(actionLabel);
        primary.setVisibility(showPrimary ? View.VISIBLE : View.GONE);
        primary.setEnabled(showPrimary);
        primary.setAlpha(showPrimary ? 1f : 0.55f);

        boolean showSecondary = next.canPause || next.canResume;
        secondary.setVisibility(showSecondary ? View.VISIBLE : View.GONE);
        secondary.setText(next.canPause ? "Pause download" : "Resume download");

        if (!next.errorDetails.equals(lastError)) {
            lastError = next.errorDetails;
            detailsExpanded = false;
        }
        errorText.setText(next.errorDetails);
        updateError();
    }

    private void updateError() {
        boolean available = model != null && !model.errorDetails.isEmpty();
        errorToggle.setVisibility(available ? View.VISIBLE : View.GONE);
        errorToggle.setText(detailsExpanded ? "Hide error details" : "Show error details");
        errorText.setVisibility(available && detailsExpanded ? View.VISIBLE : View.GONE);
    }

    private Button button(String label, boolean filled) {
        Button result = new Button(activity);
        result.setText(label);
        result.setAllCaps(false);
        result.setTextSize(17);
        result.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        result.setGravity(Gravity.CENTER);
        result.setMinHeight(dp(58));
        result.setPadding(dp(14), dp(10), dp(14), dp(10));
        result.setStateListAnimator(null);
        result.setTextColor(filled ? Color.WHITE : ACCENT);
        GradientDrawable bg = shape(filled ? ACCENT : Color.WHITE, 15, filled ? ACCENT : LINE);
        result.setBackground(new RippleDrawable(ColorStateList.valueOf(filled
                ? Color.argb(38, 255, 255, 255) : Color.argb(20, 34, 91, 190)), bg, shape(Color.WHITE, 15, Color.WHITE)));
        return result;
    }

    private TextView text(String value, float size, int color, boolean bold) {
        TextView view = new TextView(activity);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setTypeface(Typeface.DEFAULT, bold ? Typeface.BOLD : Typeface.NORMAL);
        view.setLineSpacing(dp(3), 1f);
        return view;
    }

    private GradientDrawable shape(int color, int radius, int stroke) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radius));
        drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private LinearLayout.LayoutParams margin(int top) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.topMargin = dp(top);
        return params;
    }
    private LinearLayout.LayoutParams matchWrap(int top) { return margin(top); }
    private int dp(int value) {
        return (int) (value * activity.getResources().getDisplayMetrics().density + 0.5f);
    }
    private static String clean(String value) { return value == null ? "" : value.trim(); }
    private static String bytes(long value) {
        if (value >= 1000000000L) return String.format(Locale.ROOT, "%.2f GB", value / 1e9);
        if (value >= 1000000L) return String.format(Locale.ROOT, "%.1f MB", value / 1e6);
        if (value >= 1000L) return String.format(Locale.ROOT, "%.1f KB", value / 1e3);
        return value + " B";
    }
    private static String defaultTitle(Stage stage) {
        switch (stage) {
            case NEEDS_DOWNLOAD: return "Ready to download";
            case VERIFYING: return "Checking game data";
            case DOWNLOADING: return "Downloading game data";
            case DEPLOYING: return "Preparing your game";
            case READY: return "Starting game";
            case PAUSED: return "Download paused";
            case ERROR: return "Something went wrong";
            case INSTALL_READY: return "Update ready";
            default: return "Checking your game";
        }
    }
    private static String defaultMessage(Stage stage) {
        switch (stage) {
            case NEEDS_DOWNLOAD: return "Download the required files once. You can play offline afterwards.";
            case VERIFYING: return "Checking local files. This should only take a moment.";
            case DOWNLOADING: return "Keep the app open while your files are downloaded.";
            case DEPLOYING: return "Finishing the installation.";
            case READY: return "Launching…";
            case PAUSED: return "Your downloaded progress has been saved.";
            case ERROR: return "The game data could not be prepared. Your existing files are safe.";
            case INSTALL_READY: return "The app update has been downloaded.";
            default: return "Checking installed files and available updates.";
        }
    }
}
