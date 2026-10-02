package com.histion.cuktech;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;

/** config.json 读写与规范化（对齐 pyapp/config_store.py）。 */
final class ConfigStore {
    private final File file;
    final Object lock = new Object();
    JSONObject data;

    ConfigStore(File dataDir) {
        this.file = new File(dataDir, "config.json");
        this.data = validate(loadRaw());
    }

    private JSONObject loadRaw() {
        try {
            byte[] b = new byte[(int) file.length()];
            java.io.FileInputStream in = new java.io.FileInputStream(file);
            int n = in.read(b);
            in.close();
            if (n <= 0) return null;
            JSONObject o = new JSONObject(new String(b, 0, n, "UTF-8"));
            return o;
        } catch (Exception e) {
            return null;
        }
    }

    void save() {
        synchronized (lock) {
            try {
                FileOutputStream fos = new FileOutputStream(file, false);
                OutputStreamWriter osw = new OutputStreamWriter(fos, "UTF-8");
                osw.write(data.toString(2));
                osw.close();
            } catch (Exception ignored) { }
        }
    }

    JSONObject snapshot() {
        synchronized (lock) {
            return AppState.copy(data);
        }
    }

    Object get(String key, Object dflt) {
        synchronized (lock) {
            Object v = data.opt(key);
            return v == null ? dflt : v;
        }
    }

    void set(String key, Object value) {
        synchronized (lock) {
            try { data.put(key, value); } catch (Exception ignored) { }
        }
    }

    double interval() {
        Object v = get("interval", 1.5);
        return Util.asDouble(v, 1.5);
    }

    boolean autoA() {
        return Boolean.TRUE.equals(get("autoA", Boolean.FALSE));
    }

    static JSONObject validate(JSONObject raw) {
        JSONObject out = new JSONObject();
        try {
            out.put("address", "");
            out.put("interval", 1.5);
            out.put("tray", true);
            out.put("trayMode", "total");
            out.put("autoA", false);
            // 界面风格：classic = 原深色科技风（默认），harmony = 鸿蒙 UI。纯前端换肤。
            out.put("uiTheme", "classic");
        } catch (Exception ignored) { }

        if (raw != null) {
            java.util.Iterator<String> it = raw.keys();
            while (it.hasNext()) {
                String k = it.next();
                try { out.put(k, raw.get(k)); } catch (Exception ignored) { }
            }
        }
        double interval = Util.asDouble(out.opt("interval"), 1.5);
        if (!(interval >= 0.5 && interval <= 10)) interval = 1.5;
        String mode = out.optString("trayMode", "total");
        if (!"total".equals(mode) && !"all".equals(mode) && !"panel".equals(mode)) mode = "total";
        String theme = out.optString("uiTheme", "classic");
        if (!"harmony".equals(theme)) theme = "classic";
        String addr = out.optString("address", "");
        try {
            out.put("interval", interval);
            out.put("trayMode", mode);
            out.put("uiTheme", theme);
            out.put("autoA", Boolean.TRUE.equals(out.opt("autoA")));
            out.put("tray", !Boolean.FALSE.equals(out.opt("tray")));
            out.put("address", addr == null ? "" : addr.trim().toUpperCase());
        } catch (Exception ignored) { }
        return out;
    }
}
