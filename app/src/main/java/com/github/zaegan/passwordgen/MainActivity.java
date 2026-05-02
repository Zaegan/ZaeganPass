package com.github.zaegan.passwordgen;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Menu;
import android.view.MenuItem;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.appcompat.app.AppCompatDelegate;
import androidx.appcompat.widget.Toolbar;

import com.google.android.material.dialog.MaterialAlertDialogBuilder;

import java.util.Arrays;

public class MainActivity extends AppCompatActivity {

    private static final String PREFS_SETTINGS = "passwordgen_settings";
    private static final String KEY_THEME      = "theme_mode";

    // ── Preference keys (settings only — password is NEVER persisted) ─────────
    private static final String PREFS_NAME    = "passwordgen_prefs";
    private static final String KEY_LENGTH    = "length";
    private static final String KEY_LOWER     = "lower";
    private static final String KEY_UPPER     = "upper";
    private static final String KEY_DIGITS    = "digits";
    private static final String KEY_SYMBOLS   = "symbols";
    private static final String KEY_AMBIGUOUS = "exclude_ambiguous";

    private static final int LENGTH_MIN     = 8;
    private static final int LENGTH_MAX     = 128;
    private static final int LENGTH_DEFAULT = 16;

    /** Seconds after which the clipboard is automatically cleared. */
    private static final long CLIPBOARD_CLEAR_DELAY_MS = 60_000L;

    // ── Views ──────────────────────────────────────────────────────────────────
    private TextView  tvPassword;
    private TextView  tvStatus;
    private TextView  tvLength;
    private Button    btnCopy;
    private Button    btnMinus;
    private Button    btnPlus;
    private CheckBox  cbLower;
    private CheckBox  cbUpper;
    private CheckBox  cbDigits;
    private CheckBox  cbSymbols;
    private CheckBox  cbExcludeAmbiguous;

    // ── State ──────────────────────────────────────────────────────────────────
    private int currentLength = LENGTH_DEFAULT;

    /**
     * True while the clipboard holds a password we put there.
     * Used to avoid clearing a clipboard the user has since overwritten.
     */
    private boolean clipboardOwned = false;

    private final Handler  handler         = new Handler(Looper.getMainLooper());
    private final Runnable clearClipboard  = this::doClearClipboard;

    // ── Lifecycle ──────────────────────────────────────────────────────────────

    public static void applyTheme(String mode) {
        switch (mode) {
            case "dark":   AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_YES);  break;
            case "light":  AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO);   break;
            default:       AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM); break;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        applyTheme(getSharedPreferences(PREFS_SETTINGS, MODE_PRIVATE)
                .getString(KEY_THEME, "system"));
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        Toolbar toolbar = findViewById(R.id.toolbar);
        setSupportActionBar(toolbar);
        if (getSupportActionBar() != null) getSupportActionBar().setTitle(R.string.app_name);

        tvPassword          = findViewById(R.id.tvPassword);
        tvStatus            = findViewById(R.id.tvStatus);
        tvLength            = findViewById(R.id.tvLength);
        btnCopy             = findViewById(R.id.btnCopy);
        btnMinus            = findViewById(R.id.btnMinus);
        btnPlus             = findViewById(R.id.btnPlus);
        cbLower             = findViewById(R.id.cbLower);
        cbUpper             = findViewById(R.id.cbUpper);
        cbDigits            = findViewById(R.id.cbDigits);
        cbSymbols           = findViewById(R.id.cbSymbols);
        cbExcludeAmbiguous  = findViewById(R.id.cbExcludeAmbiguous);

        loadPrefs();
        updateLengthDisplay();

        btnMinus.setOnClickListener(v -> adjustLength(-1));
        btnPlus.setOnClickListener(v  -> adjustLength(+1));

        cbLower.setOnCheckedChangeListener((b, c)            -> savePrefs());
        cbUpper.setOnCheckedChangeListener((b, c)            -> savePrefs());
        cbDigits.setOnCheckedChangeListener((b, c)           -> savePrefs());
        cbSymbols.setOnCheckedChangeListener((b, c)          -> savePrefs());
        cbExcludeAmbiguous.setOnCheckedChangeListener((b, c) -> savePrefs());

        btnCopy.setOnClickListener(v -> copyToClipboard());
        findViewById(R.id.btnGenerate).setOnClickListener(v -> generatePassword());
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.menu_main, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.action_theme) {
            showThemeDialog();
            return true;
        } else if (id == R.id.action_privacy) {
            startActivity(new Intent(Intent.ACTION_VIEW,
                    Uri.parse("https://zaegan.github.io/PasswordGen/privacy")));
            return true;
        } else if (id == R.id.action_rate) {
            startActivity(new Intent(Intent.ACTION_VIEW,
                    Uri.parse("market://details?id=" + getPackageName())));
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    private void showThemeDialog() {
        String current = getSharedPreferences(PREFS_SETTINGS, MODE_PRIVATE)
                .getString(KEY_THEME, "system");
        String[] labels = {"Light", "Dark", "System default"};
        String[] values = {"light", "dark", "system"};
        int checked = 2;
        for (int i = 0; i < values.length; i++) {
            if (values[i].equals(current)) { checked = i; break; }
        }
        new MaterialAlertDialogBuilder(this)
                .setTitle(R.string.theme_dialog_title)
                .setSingleChoiceItems(labels, checked, (dialog, which) -> {
                    String mode = values[which];
                    getSharedPreferences(PREFS_SETTINGS, MODE_PRIVATE)
                            .edit().putString(KEY_THEME, mode).apply();
                    applyTheme(mode);
                    dialog.dismiss();
                    recreate();
                })
                .show();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        // Cancel any pending clipboard clear; the OS will reclaim the clipboard naturally.
        handler.removeCallbacks(clearClipboard);
    }

    // ── Password generation ────────────────────────────────────────────────────

    private void generatePassword() {
        PasswordGenerator.Options opts = buildOptions();
        char[] pwd = PasswordGenerator.generate(opts);

        if (pwd == null) {
            Toast.makeText(this, R.string.error_no_charset, Toast.LENGTH_SHORT).show();
            return;
        }

        // Display — unavoidably becomes a String for TextView; lives only in memory.
        String pwdStr = new String(pwd);
        Arrays.fill(pwd, '\0');   // zero the char[] immediately

        tvPassword.setText(pwdStr);
        btnCopy.setEnabled(true);

        // Auto-copy to clipboard
        copyToClipboard();
    }

    // ── Clipboard ──────────────────────────────────────────────────────────────

    private void copyToClipboard() {
        CharSequence text = tvPassword.getText();
        if (text == null || text.toString().equals(getString(R.string.password_placeholder))) return;

        ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        cm.setPrimaryClip(ClipData.newPlainText("password", text));
        clipboardOwned = true;

        // Show status and schedule auto-clear
        tvStatus.setText(R.string.copied);
        tvStatus.setVisibility(android.view.View.VISIBLE);

        handler.removeCallbacks(clearClipboard);
        handler.postDelayed(clearClipboard, CLIPBOARD_CLEAR_DELAY_MS);
    }

    private void doClearClipboard() {
        if (!clipboardOwned) return;
        ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);

        // On API 28+ clear natively; on older APIs overwrite with empty text.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            cm.clearPrimaryClip();
        } else {
            cm.setPrimaryClip(ClipData.newPlainText("", ""));
        }

        clipboardOwned = false;
        tvStatus.setText(R.string.clipboard_cleared);
    }

    // ── Length control ─────────────────────────────────────────────────────────

    private void adjustLength(int delta) {
        currentLength = Math.max(LENGTH_MIN, Math.min(LENGTH_MAX, currentLength + delta));
        updateLengthDisplay();
        savePrefs();
    }

    private void updateLengthDisplay() {
        tvLength.setText(String.valueOf(currentLength));
        btnMinus.setEnabled(currentLength > LENGTH_MIN);
        btnPlus.setEnabled(currentLength  < LENGTH_MAX);
    }

    // ── Preferences (settings only — password never stored) ───────────────────

    private void loadPrefs() {
        SharedPreferences p = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        currentLength = p.getInt(KEY_LENGTH,  LENGTH_DEFAULT);
        cbLower.setChecked(           p.getBoolean(KEY_LOWER,     true));
        cbUpper.setChecked(           p.getBoolean(KEY_UPPER,     true));
        cbDigits.setChecked(          p.getBoolean(KEY_DIGITS,    true));
        cbSymbols.setChecked(         p.getBoolean(KEY_SYMBOLS,   true));
        cbExcludeAmbiguous.setChecked(p.getBoolean(KEY_AMBIGUOUS, false));
    }

    private void savePrefs() {
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putInt(    KEY_LENGTH,    currentLength)
                .putBoolean(KEY_LOWER,     cbLower.isChecked())
                .putBoolean(KEY_UPPER,     cbUpper.isChecked())
                .putBoolean(KEY_DIGITS,    cbDigits.isChecked())
                .putBoolean(KEY_SYMBOLS,   cbSymbols.isChecked())
                .putBoolean(KEY_AMBIGUOUS, cbExcludeAmbiguous.isChecked())
                .apply();
    }

    // ── Options builder ────────────────────────────────────────────────────────

    private PasswordGenerator.Options buildOptions() {
        PasswordGenerator.Options opts = new PasswordGenerator.Options();
        opts.length           = currentLength;
        opts.includeLower     = cbLower.isChecked();
        opts.includeUpper     = cbUpper.isChecked();
        opts.includeDigits    = cbDigits.isChecked();
        opts.includeSymbols   = cbSymbols.isChecked();
        opts.excludeAmbiguous = cbExcludeAmbiguous.isChecked();
        return opts;
    }
}
