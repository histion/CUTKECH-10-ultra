package com.histion.cuktech;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 电量账本：功率 → 时间积分、分日分桶、充电会话、重启回填（对齐 pyapp/energy_ledger.py）。
 *
 * 充电头没有"累计电量"属性可读，只能对瞬时功率积分；两次采样间隔 > 60s 的那一段整段丢弃。
 */
final class EnergyLedger {
    static final long SESSION_GAP_MS = 60000;
    static final double SESSION_MIN_W = AutoA.LOAD_W;
    private static final int DAYS_KEEP = 400;
    private static final long SAVE_MS = 15000;
    private static final int SESSIONS_KEEP = 50;
    private static final int SESSIONS_SNAPSHOT = 20;

    private final File file;
    private final Object lock = new Object();
    private PushLog pushLog;
    JSONObject data;
    private boolean dirty;
    private volatile long saveAt;
    private Thread timerThread;

    interface PushLog { void push(String msg); }

    EnergyLedger(File dataDir, PushLog pushLog) {
        this.file = new File(dataDir, "energy.json");
        this.pushLog = pushLog;
        this.data = empty();
        load();
    }

    static JSONObject empty() {
        JSONObject o = new JSONObject();
        try {
            o.put("version", 1);
            o.put("totalWh", 0);
            o.put("ports", AppState.zeroPorts());
            o.put("days", new JSONObject());
            o.put("lastAt", 0);
            o.put("firstAt", 0);
            o.put("lastSample", JSONObject.NULL);
            o.put("current", JSONObject.NULL);
            o.put("sessions", new JSONArray());
        } catch (Exception ignored) { }
        return o;
    }

    void load() {
        synchronized (lock) {
            try {
                byte[] b = new byte[(int) file.length()];
                java.io.FileInputStream in = new java.io.FileInputStream(file);
                int n = in.read(b);
                in.close();
                if (n <= 0) return;
                JSONObject raw = new JSONObject(new String(b, 0, n, "UTF-8"));
                JSONObject d = empty();
                java.util.Iterator<String> it = raw.keys();
                while (it.hasNext()) {
                    String k = it.next();
                    d.put(k, raw.get(k));
                }
                d.put("ports", AppState.mapPorts(raw.opt("ports")));
                d.put("days", raw.opt("days") instanceof JSONObject ? raw.optJSONObject("days") : new JSONObject());
                d.put("sessions", raw.opt("sessions") instanceof JSONArray ? raw.optJSONArray("sessions") : new JSONArray());
                data = d;
            } catch (Exception e) {
                data = empty();
            }
        }
    }

    void save(boolean force) {
        synchronized (lock) {
            if (!dirty && !force) return;
            dirty = false;
            try {
                JSONObject days = data.optJSONObject("days");
                if (days != null && days.length() > DAYS_KEEP) {
                    List<String> keys = new ArrayList<String>();
                    java.util.Iterator<String> it = days.keys();
                    while (it.hasNext()) keys.add(it.next());
                    java.util.Collections.sort(keys);
                    for (int i = 0; i < keys.size() - DAYS_KEEP; i++) days.remove(keys.get(i));
                }
                JSONArray sessions = data.optJSONArray("sessions");
                if (sessions != null && sessions.length() > SESSIONS_KEEP) {
                    JSONArray keep = new JSONArray();
                    for (int i = sessions.length() - SESSIONS_KEEP; i < sessions.length(); i++) {
                        keep.put(sessions.get(i));
                    }
                    data.put("sessions", keep);
                }
                FileOutputStream fos = new FileOutputStream(file, false);
                OutputStreamWriter osw = new OutputStreamWriter(fos, "UTF-8");
                osw.write(data.toString());
                osw.close();
            } catch (Exception ignored) { }
        }
    }

    void touch() {
        synchronized (lock) {
            dirty = true;
            if (timerThread != null && timerThread.isAlive()) return;
            timerThread = new Thread(new Runnable() {
                @Override public void run() {
                    try { Thread.sleep(SAVE_MS); } catch (InterruptedException e) { return; }
                    save(false);
                }
            }, "energy-save");
            timerThread.setDaemon(true);
            timerThread.start();
        }
    }

    /** 历史行 → 积分用采样 {at,total,w}。 */
    static JSONObject sampleFrom(JSONObject row) {
        JSONObject w = AppState.zeroPorts();
        JSONObject rw = row != null ? row.optJSONObject("w") : null;
        if (rw != null) {
            for (String p : Miot.PORT_ORDER) {
                try { w.put(p, Util.asDouble(rw.opt(p), 0)); } catch (Exception ignored) { }
            }
        }
        JSONObject s = new JSONObject();
        try {
            s.put("at", row == null ? System.currentTimeMillis() : row.optLong("at", System.currentTimeMillis()));
            s.put("total", Util.asDouble(row == null ? null : row.opt("total"), 0));
            s.put("w", w);
        } catch (Exception ignored) { }
        return s;
    }

    void integrate(JSONObject s) {
        synchronized (lock) {
            JSONObject prev = data.opt("lastSample") instanceof JSONObject ? data.optJSONObject("lastSample") : null;
            long curAt = s.optLong("at", 0);
            if (prev != null && curAt > prev.optLong("at", 0)) {
                long gap = curAt - prev.optLong("at", 0);
                if (gap <= SESSION_GAP_MS) step(prev, s, gap);
            }
            if (data.optLong("firstAt", 0) == 0) {
                try { data.put("firstAt", curAt); } catch (Exception ignored) { }
            }
            try { data.put("lastSample", s); data.put("lastAt", curAt); } catch (Exception ignored) { }
            trackSession(s);
        }
    }

    private void step(JSONObject prev, JSONObject s, long gap) {
        double dtH = gap / 3600000.0;
        double dTotal = avg(prev.opt("total"), s.opt("total")) * dtH;
        JSONObject ports = AppState.zeroPorts();
        JSONObject pw = prev.optJSONObject("w");
        JSONObject sw = s.optJSONObject("w");
        for (String p : Miot.PORT_ORDER) {
            double a = pw == null ? 0 : Util.asDouble(pw.opt(p), 0);
            double b = sw == null ? 0 : Util.asDouble(sw.opt(p), 0);
            try { ports.put(p, avg(a, b) * dtH); } catch (Exception ignored) { }
        }
        try {
            data.put("totalWh", data.optDouble("totalWh", 0) + dTotal);
            String key = Util.dayKey(s.optLong("at", System.currentTimeMillis()));
            JSONObject days = data.optJSONObject("days");
            if (days == null) { days = new JSONObject(); data.put("days", days); }
            JSONObject bucket = days.optJSONObject(key);
            if (bucket == null) {
                bucket = new JSONObject();
                bucket.put("total", 0);
                bucket.put("ports", AppState.zeroPorts());
                days.put(key, bucket);
            }
            bucket.put("total", bucket.optDouble("total", 0) + dTotal);
            JSONObject bp = AppState.mapPorts(bucket.opt("ports"));
            JSONObject dp = AppState.mapPorts(data.opt("ports"));
            for (String p : Miot.PORT_ORDER) {
                dp.put(p, dp.optDouble(p, 0) + ports.optDouble(p, 0));
                bp.put(p, bp.optDouble(p, 0) + ports.optDouble(p, 0));
            }
            data.put("ports", dp);
            bucket.put("ports", bp);

            JSONObject cur = data.opt("current") instanceof JSONObject ? data.optJSONObject("current") : null;
            if (cur != null) {
                cur.put("wh", cur.optDouble("wh", 0) + dTotal);
                JSONObject cp = AppState.mapPorts(cur.opt("ports"));
                for (String p : Miot.PORT_ORDER) cp.put(p, cp.optDouble(p, 0) + ports.optDouble(p, 0));
                cur.put("ports", cp);
            }
            dirty = true;
        } catch (Exception ignored) { }
    }

    private double avg(Object a, Object b) {
        return (Math.max(0, Util.asDouble(a, 0)) + Math.max(0, Util.asDouble(b, 0))) / 2.0;
    }

    private void trackSession(JSONObject s) {
        try {
            double total = Util.asDouble(s.opt("total"), 0);
            long curAt = s.optLong("at", 0);
            boolean active = total > SESSION_MIN_W;
            if (active) {
                JSONObject cur = data.opt("current") instanceof JSONObject ? data.optJSONObject("current") : null;
                if (cur == null) {
                    cur = new JSONObject();
                    cur.put("start", curAt);
                    cur.put("wh", 0);
                    cur.put("peak", 0);
                    cur.put("ports", AppState.zeroPorts());
                    cur.put("lastActiveAt", curAt);
                    data.put("current", cur);
                }
                cur.put("peak", Math.max(cur.optDouble("peak", 0), total));
                cur.put("lastActiveAt", curAt);
                dirty = true;
                return;
            }
            JSONObject cur = data.opt("current") instanceof JSONObject ? data.optJSONObject("current") : null;
            if (cur == null) return;
            long lastActive = cur.optLong("lastActiveAt", 0);
            if (lastActive > 0 && (curAt - lastActive) > SESSION_GAP_MS) {
                JSONObject rec = new JSONObject();
                long start = cur.optLong("start", lastActive);
                rec.put("start", start);
                rec.put("end", lastActive);
                rec.put("ms", lastActive - start);
                rec.put("wh", Util.round3(cur.optDouble("wh", 0)));
                rec.put("peak", Util.round3(cur.optDouble("peak", 0)));
                rec.put("ports", AppState.mapPorts(cur.opt("ports")));
                data.getJSONArray("sessions").put(rec);
                data.put("current", JSONObject.NULL);
                dirty = true;
                if (pushLog != null) {
                    pushLog.push("一次充电结束：" + Util.round3(cur.optDouble("wh", 0))
                            + " Wh，峰值 " + Util.round3(cur.optDouble("peak", 0)) + " W");
                }
            }
        } catch (Exception ignored) { }
    }

    /** 启动时把上次退出后 history.jsonl 里仍留存的采样补算进去。 */
    int backfill() {
        File hist = new File(file.getParentFile(), "history.jsonl");
        List<JSONObject> rows = new ArrayList<JSONObject>();
        try {
            java.io.BufferedReader br = new java.io.BufferedReader(
                    new java.io.InputStreamReader(new java.io.FileInputStream(hist), "UTF-8"));
            String line;
            while ((line = br.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty()) continue;
                try {
                    JSONObject r = new JSONObject(line);
                    if (r.has("at")) rows.add(r);
                } catch (Exception ignored) { }
            }
            br.close();
        } catch (Exception e) {
            return 0;
        }
        if (rows.isEmpty()) return 0;
        java.util.Collections.sort(rows, new java.util.Comparator<JSONObject>() {
            @Override public int compare(JSONObject a, JSONObject b) {
                long x = a.optLong("at", 0), y = b.optLong("at", 0);
                return x < y ? -1 : (x > y ? 1 : 0);
            }
        });
        long cut = data.optLong("lastAt", 0);
        int used = 0;
        for (JSONObject r : rows) {
            JSONObject s = sampleFrom(r);
            if (s.optLong("at", 0) <= cut) {
                try {
                    data.put("lastSample", s);
                    if (data.optLong("firstAt", 0) == 0) data.put("firstAt", s.optLong("at", 0));
                } catch (Exception ignored) { }
                continue;
            }
            integrate(s);
            used++;
        }
        return used;
    }

    JSONObject snapshot(int days) {
        int n = Math.max(1, Math.min(90, days));
        JSONObject out = new JSONObject();
        try {
            out.put("ok", true);
            Calendar cal = Calendar.getInstance(Locale.CHINA);
            cal.set(Calendar.HOUR_OF_DAY, 12);
            cal.set(Calendar.MINUTE, 0);
            cal.set(Calendar.SECOND, 0);
            cal.set(Calendar.MILLISECOND, 0);
            SimpleDateFormat fmt = new SimpleDateFormat("yyyy-MM-dd", Locale.CHINA);
            JSONArray daysArr = new JSONArray();
            JSONObject dayMap = data.optJSONObject("days");
            for (int i = n - 1; i >= 0; i--) {
                Calendar c = (Calendar) cal.clone();
                c.add(Calendar.DAY_OF_MONTH, -i);
                String key = fmt.format(c.getTime());
                JSONObject bucket = dayMap == null ? null : dayMap.optJSONObject(key);
                JSONObject d = new JSONObject();
                d.put("day", key);
                d.put("wh", bucket == null ? 0 : Util.round3(bucket.optDouble("total", 0)));
                d.put("ports", AppState.mapPorts(bucket == null ? null : bucket.opt("ports")));
                daysArr.put(d);
            }
            out.put("days", daysArr);
            out.put("today", daysArr.length() > 0 ? daysArr.get(daysArr.length() - 1) : new JSONObject());
            out.put("totalWh", Util.round3(data.optDouble("totalWh", 0)));
            out.put("ports", AppState.mapPorts(data.opt("ports")));

            JSONObject cur = data.opt("current") instanceof JSONObject ? data.optJSONObject("current") : null;
            if (cur != null) {
                JSONObject c = new JSONObject();
                long start = cur.optLong("start", 0);
                long last = cur.optLong("lastActiveAt", 0);
                c.put("start", start);
                c.put("lastActiveAt", last);
                c.put("ms", last - start);
                c.put("wh", Util.round3(cur.optDouble("wh", 0)));
                c.put("peak", Util.round3(cur.optDouble("peak", 0)));
                c.put("ports", AppState.mapPorts(cur.opt("ports")));
                out.put("current", c);
            } else {
                out.put("current", JSONObject.NULL);
            }

            JSONArray sessions = data.optJSONArray("sessions");
            JSONArray rev = new JSONArray();
            if (sessions != null) {
                int from = Math.max(0, sessions.length() - SESSIONS_SNAPSHOT);
                List<JSONObject> list = new ArrayList<JSONObject>();
                for (int i = from; i < sessions.length(); i++) list.add(sessions.getJSONObject(i));
                for (int i = list.size() - 1; i >= 0; i--) rev.put(list.get(i));
            }
            out.put("sessions", rev);
            out.put("since", data.optLong("firstAt", 0) == 0 ? JSONObject.NULL : data.optLong("firstAt", 0));
            out.put("tracked", data.optLong("lastAt", 0) == 0 ? JSONObject.NULL : data.optLong("lastAt", 0));
        } catch (Exception ignored) { }
        return out;
    }

    static String msToMin(long ms) {
        return String.valueOf(Math.max(1, Math.round(ms / 60000.0)));
    }

    static String dateLabel(long ts) {
        return new SimpleDateFormat("MM-dd HH:mm", Locale.CHINA).format(new Date(ts));
    }
}
