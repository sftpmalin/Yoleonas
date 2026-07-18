package com.sftpmalin.yoleo.ui;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

public final class Ui {
    public static final int BACKGROUND = Color.rgb(6, 11, 22);
    public static final int SURFACE = Color.rgb(13, 24, 48);
    public static final int SURFACE_ALT = Color.rgb(18, 35, 66);
    public static final int CYAN = Color.rgb(26, 215, 255);
    public static final int BLUE = Color.rgb(23, 105, 255);
    public static final int GREEN = Color.rgb(46, 230, 166);
    public static final int TEXT = Color.rgb(243, 250, 255);
    public static final int MUTED = Color.rgb(143, 169, 199);
    public static final int RED = Color.rgb(255, 93, 115);
    public static final int AMBER = Color.rgb(255, 186, 74);
    public static final int BORDER = Color.rgb(30, 75, 118);

    private Ui() {
    }

    public static int dp(Context context, int value) {
        return Math.round(value * context.getResources().getDisplayMetrics().density);
    }

    public static TextView text(Context context, String value, float sp, int color) {
        TextView view = new TextView(context);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        view.setLineSpacing(0, 1.08f);
        return view;
    }

    public static TextView title(Context context, String value, float sp) {
        TextView view = text(context, value, sp, TEXT);
        view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return view;
    }

    public static EditText field(Context context, String hint, int inputType) {
        EditText field = new EditText(context);
        field.setHint(hint);
        field.setHintTextColor(MUTED);
        field.setTextColor(TEXT);
        field.setTextSize(16);
        field.setSingleLine(true);
        field.setInputType(inputType);
        field.setPadding(dp(context, 14), dp(context, 4), dp(context, 14), dp(context, 4));
        field.setMinHeight(dp(context, 52));
        field.setBackground(rounded(SURFACE_ALT, BORDER, 12, context));
        return field;
    }

    public static Button button(Context context, String value, boolean primary) {
        Button button = new Button(context);
        button.setText(value);
        button.setTextSize(15);
        button.setAllCaps(false);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setTextColor(primary ? BACKGROUND : TEXT);
        button.setGravity(Gravity.CENTER);
        button.setMinHeight(dp(context, 48));
        button.setPadding(dp(context, 14), 0, dp(context, 14), 0);
        button.setBackground(buttonBackground(context, primary));
        return button;
    }

    public static LinearLayout card(Context context) {
        LinearLayout card = new LinearLayout(context);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(context, 16), dp(context, 16), dp(context, 16), dp(context, 16));
        card.setBackground(rounded(SURFACE, BORDER, 18, context));
        return card;
    }

    public static ProgressBar progress(Context context, int value, int color) {
        ProgressBar progress = new ProgressBar(context, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setProgress(Math.max(0, Math.min(100, value)));
        progress.setProgressTintList(ColorStateList.valueOf(color));
        progress.setProgressBackgroundTintList(ColorStateList.valueOf(SURFACE_ALT));
        progress.setMinimumHeight(dp(context, 7));
        return progress;
    }

    public static View divider(Context context) {
        View divider = new View(context);
        divider.setBackgroundColor(BORDER);
        divider.setAlpha(0.65f);
        return divider;
    }

    public static GradientDrawable rounded(
            int fill,
            int stroke,
            int radiusDp,
            Context context) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(dp(context, radiusDp));
        drawable.setStroke(dp(context, 1), stroke);
        return drawable;
    }

    public static LinearLayout.LayoutParams margins(
            int width,
            int height,
            Context context,
            int left,
            int top,
            int right,
            int bottom) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(width, height);
        params.setMargins(dp(context, left), dp(context, top), dp(context, right), dp(context, bottom));
        return params;
    }

    public static LinearLayout.LayoutParams weighted(Context context, float weight, int margin) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, weight);
        params.setMargins(dp(context, margin), 0, dp(context, margin), 0);
        return params;
    }

    private static StateListDrawable buttonBackground(Context context, boolean primary) {
        int normal = primary ? CYAN : SURFACE_ALT;
        int pressed = primary ? GREEN : BORDER;
        int stroke = primary ? CYAN : BORDER;
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[]{android.R.attr.state_pressed}, rounded(pressed, pressed, 13, context));
        states.addState(new int[]{-android.R.attr.state_enabled}, rounded(SURFACE, BORDER, 13, context));
        states.addState(new int[]{}, rounded(normal, stroke, 13, context));
        return states;
    }
}

