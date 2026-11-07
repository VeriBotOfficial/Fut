package org.efootballrevival.bootstrap;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.widget.Toast;

/** Opens the Fut project issue tracker from the startup error screen. */
public final class FeedbackContact {
    public static final String ISSUES_URL = "https://github.com/VeriBotOfficial/Fut/issues";
    private FeedbackContact() { }

    public static void open(Activity activity) {
        try {
            activity.startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(ISSUES_URL)));
        } catch (android.content.ActivityNotFoundException unavailable) {
            Toast.makeText(activity, "无法打开反馈页面，请访问：" + ISSUES_URL, Toast.LENGTH_LONG).show();
        }
    }
}
