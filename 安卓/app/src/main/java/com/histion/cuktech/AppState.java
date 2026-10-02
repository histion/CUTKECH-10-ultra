package com.histion.cuktech;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

/** 内存运行态 + 历史/日志环形缓冲（对齐 pyapp/state_store.py）。 */
final class AppState {
    static final int HISTORY_MAX = 3600;
    static final int LOG_MAX_LINES = 400;
    private static final long HIST_FILE_MAX = 8L * 1024 * 1024;

    final Object lock = new Object();
    volatile boolean collecting;
    volatile int restarts;
    volatile long lastSpawnAt;
    volatile JSONObject latest;
    volatile JSONObject link;
    final List<JSONObject> history = new ArrayList<JSONObject>();
    final List<String> logLines = new ArrayList<String>();
    volatile JSONArray writable = new JSONArray();
    volatile JSONArray switches = new JSONArray();
    volatile JSONObject sweep;
    EnergyLedger energy;
    private File dataDir;

    AppState(File dataDir) {
        this.dataDir = dataDir;
        link = new JSONObject();
        try {
            link.put("state", "idle");
            link.put("msg", "尚未启动采集");
            link.put("attempt", 0);
        } catch (Exception ignored) { }
    }

    void attachEnergy(EnergyLedger e) { this.energy = e; }

    void setLink(String state, String msg) { setLink(state, msg, 0, null); }

    void setLink(String state, String msg, int attempt, Integer rssi) {
        JSONObject l = new JSONObject();
        try {
            l.put("state", state);
            l.put("msg", msg == null ? "" : msg);
            l.put("attempt", attempt);
            if (rssi != null) l.put("rssi", rssi.intValue());
        } catch (Exception ignored) { }
        link = l;
    }

    /** 上一帧的 rssi（没有就不带这个键，与桌面版 JSON 一致）。 */
    Integer prevRssi() {
        JSONObject l = link;
        if (l != null && l.has("rssi")) return l.optInt("rssi");
        return null;
    }

    JSONObject snapshotLink() {
        JSONObject l = link;
        if (l == null) return new JSONObject();
        try { return new JSONObject(l.toString()); } catch (Exception e) { return l; }
    }

    /** 把一帧采样落成历史行 {at,total,w} 并追加到 history.jsonl，顺带触发电量积分。 */
    JSONObject pushHistory(JSONObject sample) {
        double total = 0;
        JSONObject w = new JSONObject();
        JSONArray ports = sample != null ? sample.optJSONArray("ports") : null;
        if (ports != null) {
            for (int i = 0; i < ports.length(); i++) {
                JSONObject p = ports.optJSONObject(i);
                if (p == null) continue;
                String id = p.optString("id");
                double pw = p.optDouble("w", 0);
                try { w.put(id, pw); } catch (Exception ignored) { }
                total += pw;
            }
        }
        JSONObject row = new JSONObject();
        try {
            row.put("at", sample == null ? System.currentTimeMillis() : sample.optLong("at", System.currentTimeMillis()));
            row.put("total", Util.jsRound(total, 2));
            row.put("w", w);
        } catch (Exception ignored) { }

        synchronized (lock) {
            history.add(row);
            int overflow = history.size() - HISTORY_MAX;
            if (overflow > 0) for (int i = 0; i < overflow; i++) history.remove(0);
        }

        try {
            File f = new File(dataDir, "history.jsonl");
            FileOutputStream fos = new FileOutputStream(f, true);
            OutputStreamWriter osw = new OutputStreamWriter(fos, "UTF-8");
            osw.write(row.toString());
            osw.write("\n");
            osw.close();
            if (f.length() > HIST_FILE_MAX) {
                // 只留末尾 2000 行
                List<String> lines = new ArrayList<String>();
                java.io.BufferedReader br = new java.io.BufferedReader(
                        new java.io.InputStreamReader(new java.io.FileInputStream(f), "UTF-8"));
                String line;
                while ((line = br.readLine()) != null) lines.add(line);
                br.close();
                int from = Math.max(0, lines.size() - 2000);
                StringBuilder sb = new StringBuilder();
                for (int i = from; i < lines.size(); i++) sb.append(lines.get(i)).append("\n");
                FileOutputStream f2 = new FileOutputStream(f, false);
                f2.write(sb.toString().getBytes("UTF-8"));
                f2.close();
            }
        } catch (Exception ignored) { }

        if (energy != null) {
            energy.integrate(EnergyLedger.sampleFrom(row));
            energy.touch();
        }
        return row;
    }

    void pushLog(String line) {
        String entry = "[" + Util.hms() + "] " + line;
        synchronized (lock) {
            logLines.add(entry);
            int overflow = logLines.size() - LOG_MAX_LINES;
            if (overflow > 0) for (int i = 0; i < overflow; i++) logLines.remove(0);
        }
    }

    /** 历史条数（HTTP 线程会读，采集线程会写，必须走同一把锁）。 */
    int historyCount() {
        synchronized (lock) {
            return history.size();
        }
    }

    List<JSONObject> historySince(long sinceMs) {
        List<JSONObject> out = new ArrayList<JSONObject>();
        synchronized (lock) {
            for (JSONObject r : history) {
                long at = r.optLong("at", 0);
                if (at >= sinceMs) out.add(r);
            }
        }
        return out;
    }

    List<String> tailLogs(int n) {
        synchronized (lock) {
            int from = Math.max(0, logLines.size() - n);
            return new ArrayList<String>(logLines.subList(from, logLines.size()));
        }
    }

    /** 文件日志（data/app.log）。 */
    void fileLog(String msg) {
        try {
            File f = new File(dataDir, "app.log");
            FileOutputStream fos = new FileOutputStream(f, true);
            OutputStreamWriter osw = new OutputStreamWriter(fos, "UTF-8");
            osw.write("[" + Util.isoNow() + "] " + msg + "\n");
            osw.close();
        } catch (Exception ignored) { }
    }

    static JSONObject zeroPorts() {
        JSONObject o = new JSONObject();
        try {
            for (String p : Miot.PORT_ORDER) o.put(p, 0);
        } catch (Exception ignored) { }
        return o;
    }

    static JSONObject mapPorts(Object src) {
        JSONObject out = zeroPorts();
        JSONObject s = src instanceof JSONObject ? (JSONObject) src : null;
        if (s == null) return out;
        try {
            for (String p : Miot.PORT_ORDER) out.put(p, Util.round3(s.optDouble(p, 0)));
        } catch (Exception ignored) { }
        return out;
    }

    static JSONArray toJsonArray(List<String> items) {
        JSONArray arr = new JSONArray();
        for (String s : items) arr.put(s);
        return arr;
    }

    static JSONArray rowsToArray(List<JSONObject> rows) {
        JSONArray arr = new JSONArray();
        for (JSONObject r : rows) arr.put(r);
        return arr;
    }

    /** 深拷贝（JSONObject 没有 clone，用字符串往返）。 */
    static JSONObject copy(JSONObject o) {
        if (o == null) return null;
        try { return new JSONObject(o.toString()); } catch (Exception e) { return null; }
    }

    static Iterator<String> keys(JSONObject o) {
        return o == null ? new ArrayList<String>().iterator() : o.keys();
    }
}
