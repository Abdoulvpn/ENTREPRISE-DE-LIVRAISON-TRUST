package com.trustdelivery.mobile;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ClipData;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.GeolocationPermissions;
import android.webkit.MimeTypeMap;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ProgressBar;
import android.widget.Toast;

import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

import com.google.firebase.FirebaseApp;
import com.google.firebase.FirebaseOptions;
import com.google.firebase.messaging.FirebaseMessaging;

import java.io.File;
import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER_REQUEST = 501;
    private static final int PERMISSION_REQUEST = 502;
    private static final String PREFS = "trustdelivery_mobile";
    private static final String PREF_URL = "server_url";
    private static final String PREF_TAB_ID = "web_tab_id";

    private WebView webView;
    private SwipeRefreshLayout swipeRefresh;
    private ProgressBar progressBar;
    private ValueCallback<Uri[]> fileCallback;
    private Uri cameraOutputUri;
    private String serverUrl;
    private String firebaseToken = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        getWindow().setStatusBarColor(Color.rgb(7, 27, 66));
        getWindow().setNavigationBarColor(Color.rgb(7, 27, 66));
        buildInterface();
        configureWebView();
        requestUsefulPermissions();
        configureFirebase();
        TrustFirebaseMessagingService.ensureChannel(this);

        String savedUrl = getSharedPreferences(PREFS, MODE_PRIVATE).getString(PREF_URL, "");
        serverUrl = normalizeUrl(!savedUrl.isEmpty() ? savedUrl : BuildConfig.WEB_APP_URL);
        if (serverUrl.isEmpty()) {
            askForServerUrl(false);
        } else {
            loadAppPage(getIntent());
        }
    }

    private void buildInterface() {
        FrameLayout root = new FrameLayout(this);
        swipeRefresh = new SwipeRefreshLayout(this);
        webView = new WebView(this);
        swipeRefresh.addView(webView, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        swipeRefresh.setColorSchemeColors(Color.rgb(249, 115, 22), Color.rgb(30, 79, 214));
        swipeRefresh.setOnRefreshListener(() -> webView.reload());
        swipeRefresh.setOnChildScrollUpCallback((parent, child) -> webView.getScrollY() > 0);
        root.addView(swipeRefresh, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        FrameLayout.LayoutParams progressParams = new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, dp(3));
        progressParams.gravity = android.view.Gravity.TOP;
        root.addView(progressBar, progressParams);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, windowInsets) -> {
            Insets bars = windowInsets.getInsets(WindowInsetsCompat.Type.systemBars());
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return windowInsets;
        });
        setContentView(root);
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setGeolocationEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        settings.setUserAgentString(settings.getUserAgentString() + " TrustDeliveryAndroid/1.0");
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        webView.setWebViewClient(new TrustWebViewClient());
        webView.setWebChromeClient(new TrustWebChromeClient());
        webView.setDownloadListener(createDownloadListener());
        webView.setOnLongClickListener(view -> {
            askForServerUrl(true);
            return true;
        });
    }

    private void askForServerUrl(boolean allowCancel) {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint(getString(R.string.server_hint));
        input.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(serverUrl);
        int padding = dp(22);
        FrameLayout holder = new FrameLayout(this);
        holder.setPadding(padding, dp(8), padding, 0);
        holder.addView(input, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT));

        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle(R.string.configure_server)
                .setMessage("Saisissez l’adresse HTTPS publique de votre application Flask.")
                .setView(holder)
                .setCancelable(allowCancel)
                .setNegativeButton(allowCancel ? "Annuler" : "Quitter", (d, which) -> { if (!allowCancel) finish(); })
                .setPositiveButton("Connecter", null)
                .create();
        dialog.setOnShowListener(ignored -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> {
            String candidate = normalizeUrl(input.getText().toString());
            if (candidate.isEmpty()) {
                input.setError("Utilisez une adresse commençant par https:// ou http://");
                return;
            }
            serverUrl = candidate;
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(PREF_URL, serverUrl).apply();
            dialog.dismiss();
            loadAppPage(getIntent());
        }));
        dialog.show();
    }

    private String normalizeUrl(String raw) {
        if (raw == null) return "";
        String value = raw.trim();
        if (!(value.startsWith("https://") || value.startsWith("http://"))) return "";
        while (value.endsWith("/")) value = value.substring(0, value.length() - 1);
        return value;
    }

    private void requestUsefulPermissions() {
        List<String> missing = new ArrayList<>();
        for (String permission : new String[]{Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION}) {
            if (ContextCompat.checkSelfPermission(this, permission) != PackageManager.PERMISSION_GRANTED) missing.add(permission);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            missing.add(Manifest.permission.POST_NOTIFICATIONS);
        }
        if (!missing.isEmpty()) ActivityCompat.requestPermissions(this, missing.toArray(new String[0]), PERMISSION_REQUEST);
    }

    private void configureFirebase() {
        if (BuildConfig.FIREBASE_APPLICATION_ID.isEmpty() || BuildConfig.FIREBASE_API_KEY.isEmpty() ||
                BuildConfig.FIREBASE_PROJECT_ID.isEmpty() || BuildConfig.FIREBASE_SENDER_ID.isEmpty()) return;
        try {
            if (FirebaseApp.getApps(this).isEmpty()) {
                FirebaseOptions options = new FirebaseOptions.Builder()
                        .setApplicationId(BuildConfig.FIREBASE_APPLICATION_ID)
                        .setApiKey(BuildConfig.FIREBASE_API_KEY)
                        .setProjectId(BuildConfig.FIREBASE_PROJECT_ID)
                        .setGcmSenderId(BuildConfig.FIREBASE_SENDER_ID)
                        .build();
                FirebaseApp.initializeApp(this, options);
            }
            firebaseToken = getSharedPreferences(PREFS, MODE_PRIVATE).getString(
                    TrustFirebaseMessagingService.PREF_FIREBASE_TOKEN, ""
            );
            FirebaseMessaging.getInstance().getToken().addOnSuccessListener(token -> {
                firebaseToken = token;
                getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                        .putString(TrustFirebaseMessagingService.PREF_FIREBASE_TOKEN, token).apply();
                registerPushToken();
            });
        } catch (Exception ignored) {
            firebaseToken = "";
        }
    }

    private void registerPushToken() {
        if (firebaseToken.isEmpty() || webView == null || webView.getUrl() == null ||
                !webView.getUrl().startsWith(serverUrl)) return;
        String quotedToken = org.json.JSONObject.quote(firebaseToken);
        String script = "(()=>{const u=new URL(location.href);const t=u.searchParams.get('_tab')||'';" +
                "fetch('/notifications/appareil?_tab='+encodeURIComponent(t),{" +
                "method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'}," +
                "body:JSON.stringify({token:" + quotedToken + "})}).catch(()=>{});})()";
        webView.evaluateJavascript(script, null);
    }

    private void loadAppPage(Intent intent) {
        String link = intent == null ? "" : intent.getStringExtra("link");
        if (link != null && link.startsWith("/") && !link.startsWith("//")) {
            String target = serverUrl + link;
            String tabId = getSharedPreferences(PREFS, MODE_PRIVATE).getString(PREF_TAB_ID, "");
            if (!tabId.isEmpty()) target = Uri.parse(target).buildUpon().appendQueryParameter("_tab", tabId).build().toString();
            webView.loadUrl(target);
        } else {
            webView.loadUrl(serverUrl);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (!serverUrl.isEmpty()) loadAppPage(intent);
    }

    private class TrustWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return openUri(request.getUrl());
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            return openUri(Uri.parse(url));
        }

        private boolean openUri(Uri uri) {
            if ("app".equals(uri.getScheme()) && "retry".equals(uri.getHost())) {
                webView.loadUrl(serverUrl);
                return true;
            }
            if ("http".equals(uri.getScheme()) || "https".equals(uri.getScheme())) {
                Uri home = Uri.parse(serverUrl);
                if (home.getHost() != null && home.getHost().equalsIgnoreCase(uri.getHost())) return false;
            }
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
            } catch (Exception ignored) {
                Toast.makeText(MainActivity.this, "Aucune application ne peut ouvrir ce lien.", Toast.LENGTH_SHORT).show();
            }
            return true;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            swipeRefresh.setRefreshing(false);
            progressBar.setVisibility(View.GONE);
            try {
                String tabId = Uri.parse(url).getQueryParameter("_tab");
                if (tabId != null && !tabId.isEmpty()) {
                    getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(PREF_TAB_ID, tabId).apply();
                }
            } catch (Exception ignored) {}
            registerPushToken();
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) showOfflinePage();
        }
    }

    private class TrustWebChromeClient extends WebChromeClient {
        @Override
        public void onProgressChanged(WebView view, int progress) {
            progressBar.setVisibility(progress < 100 ? View.VISIBLE : View.GONE);
            progressBar.setProgress(progress);
        }

        @Override
        public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
            boolean granted = ContextCompat.checkSelfPermission(MainActivity.this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
            callback.invoke(origin, granted, false);
        }

        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = callback;
            Intent content = params.createIntent();
            Intent camera = createCameraIntent();
            Intent chooser = Intent.createChooser(content, "Choisir une photo");
            if (camera != null) chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{camera});
            try {
                startActivityForResult(chooser, FILE_CHOOSER_REQUEST);
            } catch (Exception error) {
                fileCallback.onReceiveValue(null);
                fileCallback = null;
                return false;
            }
            return true;
        }
    }

    private Intent createCameraIntent() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) return null;
        Intent intent = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        if (intent.resolveActivity(getPackageManager()) == null) return null;
        try {
            File directory = new File(getCacheDir(), "camera");
            if (!directory.exists()) directory.mkdirs();
            File photo = File.createTempFile("TD_" + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date()), ".jpg", directory);
            cameraOutputUri = FileProvider.getUriForFile(this, getPackageName() + ".fileprovider", photo);
            intent.putExtra(MediaStore.EXTRA_OUTPUT, cameraOutputUri);
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
            return intent;
        } catch (IOException error) {
            return null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER_REQUEST || fileCallback == null) return;
        Uri[] results = null;
        if (resultCode == RESULT_OK) {
            if (data == null && cameraOutputUri != null) {
                results = new Uri[]{cameraOutputUri};
            } else if (data != null && data.getClipData() != null) {
                ClipData clip = data.getClipData();
                results = new Uri[clip.getItemCount()];
                for (int i = 0; i < clip.getItemCount(); i++) results[i] = clip.getItemAt(i).getUri();
            } else if (data != null && data.getData() != null) {
                results = new Uri[]{data.getData()};
            }
        }
        fileCallback.onReceiveValue(results);
        fileCallback = null;
        cameraOutputUri = null;
    }

    private DownloadListener createDownloadListener() {
        return (url, userAgent, contentDisposition, mimeType, contentLength) -> {
            try {
                DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
                request.setMimeType(mimeType);
                request.addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url));
                request.addRequestHeader("User-Agent", userAgent);
                request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                String extension = MimeTypeMap.getSingleton().getExtensionFromMimeType(mimeType);
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "TrustDelivery_" + System.currentTimeMillis() + (extension == null ? "" : "." + extension));
                ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(request);
                Toast.makeText(this, "Téléchargement démarré", Toast.LENGTH_SHORT).show();
            } catch (Exception error) {
                Toast.makeText(this, "Téléchargement impossible", Toast.LENGTH_SHORT).show();
            }
        };
    }

    private void showOfflinePage() {
        swipeRefresh.setRefreshing(false);
        String html = "<html><meta name='viewport' content='width=device-width'><body style='margin:0;background:#071b42;color:white;font-family:sans-serif;display:grid;place-items:center;min-height:100vh;text-align:center'><main style='padding:28px'><div style='font-size:52px'>📡</div><h2>Connexion indisponible</h2><p style='color:#b8c7e6'>Vérifiez votre connexion internet puis réessayez.</p><a href='app://retry' style='display:inline-block;margin-top:12px;background:#f97316;color:white;padding:12px 22px;border-radius:12px;text-decoration:none;font-weight:bold'>Réessayer</a></main></body></html>";
        webView.loadDataWithBaseURL(serverUrl, html, "text/html", "UTF-8", null);
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) webView.goBack(); else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
        }
        super.onDestroy();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
