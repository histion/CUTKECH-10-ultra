package com.histion.cuktech;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONObject;

/**
 * 前台常驻服务：把采集放在一个不会被系统随手回收的地方，并在通知栏显示当前总功率
 * ——桌面版里"任务栏托盘功率"在安卓上的对应物。
 */
public class CoreService extends Service {
    static CoreService instance;

    private static final String CHANNEL = "cuktech-link";
    private static final int NOTIFY_ID = 20200;

    private Thread notifier;
    private volatile boolean running = true;
    private String lastText = "";

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        createChannel();
        Notification n = build("-- W");
        if (Build.VERSION.SDK_INT >= 26) startForeground(NOTIFY_ID, n);
        else ((NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE)).notify(NOTIFY_ID, n);

        AppCore.get(this).start();
        startNotifier();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        NotificationChannel ch = new NotificationChannel(CHANNEL, "充电头链路",
                NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("酷态科10号Ultra 的 BLE 采集状态与当前功率");
        nm.createNotificationChannel(ch);
    }

    private Notification build(String text) {
        Intent i = new Intent(this, MainActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, i,
                Build.VERSION.SDK_INT >= 23
                        ? (PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE)
                        : PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) b = new Notification.Builder(this, CHANNEL);
        else b = new Notification.Builder(this);
        b.setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("酷态科10号Ultra")
                .setContentText(text)
                .setContentIntent(pi)
                .setOngoing(true)
                .setOnlyAlertOnce(true);
        if (Build.VERSION.SDK_INT >= 21) b.setVisibility(Notification.VISIBILITY_PUBLIC);
        return b.build();
    }

    private void startNotifier() {
        notifier = new Thread(new Runnable() {
            @Override
            public void run() {
                while (running) {
                    update();
                    try { Thread.sleep(2000); } catch (InterruptedException e) { return; }
                }
            }
        }, "notifier");
        notifier.setDaemon(true);
        notifier.start();
    }

    private void update() {
        AppCore core = AppCore.peek();
        if (core == null) return;
        JSONObject latest = core.state.latest;
        String text;
        String link = core.state.snapshotLink().optString("state", "idle");
        if (!"connected".equals(link)) {
            text = linkText(link) + " · " + core.state.snapshotLink().optString("msg", "");
            if (text.length() > 60) text = text.substring(0, 60) + "…";
        } else {
            double total = latest == null ? 0 : latest.optDouble("total", 0);
            int active = 0;
            if (latest != null) {
                org.json.JSONArray ports = latest.optJSONArray("ports");
                if (ports != null) {
                    for (int i = 0; i < ports.length(); i++) {
                        JSONObject p = ports.optJSONObject(i);
                        if (p != null && p.optBoolean("in_use", false)) active++;
                    }
                }
            }
            text = String.format(java.util.Locale.CHINA, "%.1f W · 在充 %d 口", total, active);
        }
        if (!text.equals(lastText)) {
            lastText = text;
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            nm.notify(NOTIFY_ID, build(text));
        }
    }

    private static String linkText(String state) {
        if ("scanning".equals(state)) return "搜索中";
        if ("connecting".equals(state)) return "连接中";
        if ("auth".equals(state)) return "认证中";
        if ("connected".equals(state)) return "已连接";
        if ("reconnecting".equals(state)) return "重连中";
        if ("need-login".equals(state)) return "待登录";
        if ("error".equals(state)) return "出错";
        return state;
    }

    void applyTray(boolean on) {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (on) {
            lastText = "";
            update();
        } else {
            if (Build.VERSION.SDK_INT >= 26) stopForeground(true);
            else nm.cancel(NOTIFY_ID);
        }
    }

    void requestStop() {
        stopSelf();
    }

    @Override
    public void onDestroy() {
        running = false;
        AppCore core = AppCore.peek();
        if (core != null) core.shutdown();
        if (instance == this) instance = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
