# 酷态科10号Ultra · 安卓版（v2.0.0.1-H）

桌面版（Windows 单文件 exe）在安卓上的等价实现：**功能一项不减**，界面沿用同一套
前端（深色卡片 + 自绘曲线），排版按手机重排（单列、触控目标放大、曲线可触摸读数）。

## 与桌面版的对应关系

| 桌面版 | 安卓版 | 说明 |
|---|---|---|
| `web/index.html` + 系统 Edge | `assets/index.html` + 内置 WebView | 同一份前端，加了手机媒体查询 |
| `pyapp/http_server.py` | `ApiServer.java` | 只监听 127.0.0.1 的本地 HTTP 服务，`/api/*` 字段逐字一致 |
| `collector.py` + bleak | `CollectorBridge.java` + Android BLE | 扫描→连接→米家认证→加密会话→定时轮询，状态机与文案一致 |
| `vendor/cuktech_ble/xiaomi/*` | `MiAuth.java` / `MiSession.java` / `Crypto.java` | ECDH-P256 + HKDF + HMAC-SHA256 + AES-CCM(自实现) |
| `login.py` + `xiaomi_cloud.py` | `LoginProxy.java` / `XiaomiCloud.java` | 小米云扫码登录、设备列表取 12 字节 BLE 密钥 |
| `pyapp/energy_ledger.py` | `EnergyLedger.java` | 功率时间积分、分日分桶、充电会话、重启回填 |
| `pyapp/auto_a.py` | `AutoA.java` | A 口自动化（迟滞/防抖/最小间隔/重试上限） |
| `pyapp/control.py` | `Control.java` | 端口开关、协议开关、单属性写入、扫描、恢复出厂 |
| 任务栏托盘图标 | 前台服务常驻通知 | 通知栏直接显示当前总功率与在充口数 |

密码学部分用 Python `cryptography` 生成过向量逐条比对：AES-CCM（4 字节 tag，有/无
AAD、空明文）、HMAC-SHA256、HKDF（注册/登录两种形状）、ECDH P-256 —— 全部 PASS
（`Crypto.java` 的实现是 RFC 3610 的 CBC-MAC + CTR，因为 Android 的 JCE 不保证有 CCM）。

## 界面风格：经典 / 鸿蒙 UI 一键切换

界面右上角「鸿蒙 UI / 经典 UI」按钮切换两套外观，状态存 `config.json` 的 `uiTheme`：

* **经典**（`classic`，默认）：原深色科技风；
* **鸿蒙**（`harmony`）：按 HarmonyOS 设计规范做的浅色界面 —— 系统蓝 `#007DFF`、
  胶囊按钮、8/12/16 圆角、轻阴影、官方给定时长/曲线；系统深色时自动跟随深色 token；
  按钮里用 **OpenHarmony 官方设计图标包**（`OpenHarmony_Icons.zip`，许可 **CC-BY-4.0**）。

实现要点（**只换皮，不动布局**）：

* 全部靠 `<html data-theme="harmony">` 覆盖既有 CSS 变量与视觉属性，
  **没有声明过任何布局属性**（已用 `layout_diff` 逐条比对：原 186 条规则的布局属性差异为 0）；
* 官方 SVG 是「全幅矩形 + mask 挖形状」，因此图标必须用 `DOMParser + importNode`
  注入（`innerHTML` 会把带 mask 的图标变成实心块），且每枚实例的内部 id 都加了前缀；
* canvas 曲线的取色改成读 CSS 变量（经典主题取到的仍是原值，观感不变）。

### 前端两端同步（重要）

桌面版与安卓版**共用同一份前端**，安卓端是它的派生副本。改完 `web/index.html` 后跑：

```bat
python 安卓\tools\sync_web_android.py
```

它会重新生成 `app/src/main/assets/index.html`（差异只有：手机媒体查询、触摸读数、
安卓文案 3 处），并注入 `安卓/tools/harmony_icons.js`（官方图标子集）到两端。
脚本是幂等的：内容没变时重跑产物字节一致。

## 目录结构

```
安卓/
├─ app/src/main/
│  ├─ AndroidManifest.xml             包名 com.histion.cuktech，versionCode=3 / versionName=2.0.0.1-H
│  ├─ java/com/histion/cuktech/       全部源码（无第三方依赖，只用 Android 框架 + org.json）
│  ├─ res/                            图标（自适应图标 + 通知栏剪影）、主题
│  └─ assets/index.html               手机排版版前端（由 web/index.html 同步生成）
├─ tools/
│  ├─ sync_web_android.py             前端同步脚本（改完 web 前端跑一次，两端不漂移）
│  └─ harmony_icons.js                OpenHarmony 官方图标子集（CC-BY-4.0）
├─ out/cuktech10ultra-2.0.0.1-H.apk   构建产物
├─ 构建APK.bat                        一键构建（javac → d8 → aapt2 → zipalign → apksigner）
└─ README.md
```

## 构建

1. 安装 **JDK 17**（或 11）；
2. 安装 **Android SDK**：`platforms;android-34` 与 `build-tools;34.0.0`
   （`sdkmanager "platform-tools" "platforms;android-34" "build-tools;34.0.0"`）；
3. 双击 `构建APK.bat`（或设置好 `JAVA_HOME` / `ANDROID_HOME` 后命令行运行）。

产物落在 `out/cuktech10ultra-2.0.0.apk`。首次构建会自动生成调试签名 `cuktech.jks`
（口令 `cuktech`）；要发布请换成自己的签名。

不依赖 Gradle / Android Studio，也不需要联网拉依赖。

## 运行要求

* Android 8.0（API 26）及以上，带 BLE；
* 首次启动授予**蓝牙**权限（Android 12+ 是「附近设备」，12 以下需要**位置**权限才能扫到设备，
  并且要打开定位服务）；
* 充电头要已在米家 App 里绑定过（本应用通过小米云扫码拿到 12 字节 BLE 密钥，不保存账号密码）；
* 数据（config.json / energy.json / history.jsonl / cuktech.token）写在应用私有目录，
  卸载即清除，不会外传。

## 与桌面版的差异（有意为之）

* 没有托盘/桌面快捷方式：改用前台服务常驻通知，界面上那两个开关按钮在手机上隐藏；
* 「扫描隐藏属性 / 温度」、A 口自动化、电量统计、协议开关、恢复出厂等**全部保留**；
* 手机上首次登录仍需在网页弹窗里点「生成二维码」，然后用**米家 App** 扫码授权
  （手机上没有米家的话，可以用另一台设备扫这个二维码）。
