package com.histion.cuktech;

import android.content.Context;
import android.os.Build;

import org.json.JSONObject;

import java.io.File;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * 应用内核：持有全部领域组件并实现 /api/* 的语义（对齐 pyapp/http_server.py 的 App）。
 *
 * 界面（assets/index.html）与桌面版共用同一套 JSON 契约，所以这里的字段名、
 * 默认值、裁剪范围都照抄 pyapp，一处都不能改。
 */
final class AppCore {
    static final String VERSION = "2.0.0";
    static final String APP_NAME = "cuktech 10 ultra";
    /** 带 "-H" 后缀 = 内置鸿蒙 UI 主题、可在界面上一键切换的版本。 */
    static final String APP_VERSION = "2.0.0.1-H";
    static final String APP_VENDOR = "Histion";

    private static final Pattern MAC_RE =
            Pattern.compile("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$");

    private static volatile AppCore instance;

    final Context ctx;
    final File dataDir;
    final AppState state;
    final ConfigStore config;
    final EnergyLedger energy;
    CollectorBridge bridge;
    LoginProxy login;
    AutoA.Runner autoA;
    ApiServer server;

    static AppCore get(Context ctx) {
        if (instance == null) {
            synchronized (AppCore.class) {
                if (instance == null) instance = new AppCore(ctx.getApplicationContext());
            }
        }
        return instance;
    }

    static AppCore peek() { return instance; }

    private AppCore(Context ctx) {
        this.ctx = ctx;
        this.dataDir = new File(ctx.getFilesDir(), "data");
        if (!dataDir.exists()) dataDir.mkdirs();
        this.state = new AppState(dataDir);
        this.config = new ConfigStore(dataDir);
        this.energy = new EnergyLedger(dataDir, new EnergyLedger.PushLog() {
            @Override public void push(String msg) { state.pushLog(msg); }
        });
        this.state.attachEnergy(energy);
    }

    void log(String msg) {
        state.pushLog(msg);
        state.fileLog(msg);
    }

    // ------------------------------------------------------------------ 生命周期

    synchronized void start() {
        if (server != null) return;
        energy.load();
        int used = energy.backfill();
        if (used > 0) log("电量回填 " + used + " 个采样点");
        state.writable = Miot.writableSchema();
        state.switches = Miot.switchesSchema();

        bridge = new CollectorBridge(ctx, this);
        login = new LoginProxy(this);
        autoA = new AutoA.Runner(state, config, bridge, new AutoA.Sink() {
            @Override public void push(String msg) { state.pushLog(msg); }
        });
        server = new ApiServer(this);
        server.start();
        log("本地服务已启动 127.0.0.1:" + server.port());
        bridge.start();
    }

    void shutdown() {
        if (bridge != null) bridge.stop();
        energy.save(true);
        if (server != null) server.stop();
        server = null;
    }

    int port() { return server == null ? 0 : server.port(); }

    // ------------------------------------------------------------------ 地址 / token

    String tokenAddress() {
        try {
            File f = new File(dataDir, "cuktech.token");
            byte[] b = new byte[(int) f.length()];
            java.io.FileInputStream in = new java.io.FileInputStream(f);
            int n = in.read(b);
            in.close();
            JSONObject o = new JSONObject(new String(b, 0, n, "UTF-8"));
            String addr = o.optString("address", "").toUpperCase();
            return MAC_RE.matcher(addr).matches() ? addr : null;
        } catch (Exception e) {
            return null;
        }
    }

    String effectiveAddress() {
        String cfg = config.data.optString("address", "").trim().toUpperCase();
        if (MAC_RE.matcher(cfg).matches()) return cfg;
        String t = tokenAddress();
        return t == null ? "" : t;
    }

    void setAddress(String mac) {
        config.set("address", mac);
        config.save();
    }

    boolean tokenPresent() {
        return new File(dataDir, "cuktech.token").exists();
    }

    // ------------------------------------------------------------------ /api/state

    JSONObject buildStatePayload() {
        long now = System.currentTimeMillis();
        JSONObject cfg = config.snapshot();
        JSONObject out = new JSONObject();
        try {
            out.put("ok", true);
            out.put("version", VERSION);
            JSONObject app = new JSONObject();
            app.put("name", APP_NAME);
            app.put("version", APP_VERSION);
            app.put("vendor", APP_VENDOR);
            out.put("app", app);
            out.put("now", now);
            out.put("link", state.snapshotLink());
            out.put("collecting", state.collecting);
            JSONObject c = new JSONObject();
            c.put("address", effectiveAddress());
            c.put("interval", cfg.optDouble("interval", 1.5));
            c.put("tray", cfg.optBoolean("tray", true));
            String mode = cfg.optString("trayMode", "total");
            c.put("trayMode", "all".equals(mode) || "panel".equals(mode) ? mode : "total");
            c.put("autoA", cfg.optBoolean("autoA", false));
            // 界面风格（classic / harmony）——前端首次拿到状态时同步一次
            c.put("uiTheme", "harmony".equals(cfg.optString("uiTheme", "classic")) ? "harmony" : "classic");
            out.put("config", c);
            JSONObject env = new JSONObject();
            env.put("python", "Android");
            env.put("pythonFound", true);
            env.put("tokenPresent", tokenPresent());
            env.put("appDir", dataDir.getAbsolutePath());
            env.put("platform", "android " + Build.VERSION.RELEASE);
            out.put("env", env);
            out.put("latest", state.latest == null ? JSONObject.NULL : state.latest);
            out.put("historyCount", state.historyCount());
            out.put("writable", state.writable == null ? new org.json.JSONArray() : state.writable);
            out.put("switches", state.switches == null ? new org.json.JSONArray() : state.switches);
            out.put("sweep", state.sweep == null ? JSONObject.NULL : state.sweep);

            JSONObject tb = energy.data.optJSONObject("days");
            JSONObject today = tb == null ? null : tb.optJSONObject(Util.dayKey(now));
            JSONObject cur = energy.data.opt("current") instanceof JSONObject
                    ? energy.data.optJSONObject("current") : null;
            JSONObject en = new JSONObject();
            en.put("todayWh", today == null ? 0 : Util.round3(today.optDouble("total", 0)));
            en.put("totalWh", Util.round3(energy.data.optDouble("totalWh", 0)));
            en.put("tracking", cur != null);
            if (cur != null) {
                JSONObject cc = new JSONObject();
                cc.put("ms", cur.optLong("lastActiveAt", 0) - cur.optLong("start", 0));
                cc.put("wh", Util.round3(cur.optDouble("wh", 0)));
                cc.put("peak", Util.round3(cur.optDouble("peak", 0)));
                cc.put("ports", AppState.mapPorts(cur.opt("ports")));
                en.put("current", cc);
            } else {
                en.put("current", JSONObject.NULL);
            }
            out.put("energy", en);
            out.put("login", login == null ? new JSONObject() : login.current());
        } catch (Exception ignored) { }
        return out;
    }

    // ------------------------------------------------------------------ /api/config

    JSONObject applyConfig(JSONObject body) {
        JSONObject cfg = config.data;
        double prevInterval = cfg.optDouble("interval", 1.5);
        String prevAddress = effectiveAddress();

        if (body != null && body.has("interval")) {
            double v = body.optDouble("interval", -1);
            if (v >= 0.5 && v <= 10) config.set("interval", v);
        }
        if (body != null && body.has("address")) {
            String a = body.optString("address", "");
            if (MAC_RE.matcher(a).matches()) config.set("address", a.toUpperCase());
        }
        if (body != null && body.has("tray")) {
            boolean t = body.optBoolean("tray", true);
            config.set("tray", t);
            if (CoreService.instance != null) CoreService.instance.applyTray(t);
        }
        if (body != null && body.has("trayMode")) {
            String m = body.optString("trayMode", "total");
            if ("total".equals(m) || "all".equals(m) || "panel".equals(m)) {
                boolean changed = !m.equals(cfg.optString("trayMode", "total"));
                config.set("trayMode", m);
                if (changed) {
                    String label = "panel".equals(m) ? "一行面板（四口+总）"
                            : ("all".equals(m) ? "图标轮流显示" : "仅总功率");
                    log("通知栏显示模式：" + label);
                }
            }
        }
        if (body != null && body.has("uiTheme")) {
            // 纯外观开关：只落盘，不重启采集器（重启会白白断一次蓝牙）
            String th = body.optString("uiTheme", "classic");
            if ("classic".equals(th) || "harmony".equals(th)) {
                if (!th.equals(cfg.optString("uiTheme", "classic"))) {
                    config.set("uiTheme", th);
                    log("界面风格：" + ("harmony".equals(th) ? "鸿蒙 UI" : "经典"));
                }
            }
        }
        boolean autoChanged = false;
        if (body != null && body.has("autoA")) {
            boolean want = body.optBoolean("autoA", false);
            if (want != cfg.optBoolean("autoA", false)) {
                config.set("autoA", want);
                autoChanged = true;
                if (autoA != null) autoA.resetCounters();
                log("A口自动化：" + (want ? "已开启" : "已关闭"));
            }
        }
        config.save();
        log("配置已保存：间隔 " + cfg.optDouble("interval", 1.5) + "s");
        if (autoChanged && cfg.optBoolean("autoA", false) && autoA != null) autoA.requestEvaluate();

        double newInterval = cfg.optDouble("interval", 1.5);
        if (newInterval != prevInterval || !effectiveAddress().equals(prevAddress)) {
            if (bridge != null) bridge.restartSoon();
        }
        JSONObject o = new JSONObject();
        try {
            o.put("ok", true);
            o.put("config", config.snapshot());
        } catch (Exception ignored) { }
        return o;
    }
}
