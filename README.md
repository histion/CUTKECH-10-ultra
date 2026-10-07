# 酷态科10号Ultra 监视器【大肥鱼蹬的AI代码】
<img width="1121" height="1191" alt="a784ab9b-6d86-4083-8e78-126ba0d0653f" src="https://github.com/user-attachments/assets/6f168f09-4df4-4c87-a050-d213a61b26bb" />

通过蓝牙（小米 MiOT 协议）连接酷态科 10 号 Ultra（型号 AD1204U）充电头，在电脑上监视四个口的  
电压 / 电流 / 功率、实时曲线、协商到的快充档位，并可直接控制充电头、统计用电量。

## 功能

- **监视**：四个口的 V/A/W、协商到的 PD 档位、功率仪表盘 + 实时曲线。
- **控制**：开关四个口、逐口开关快充协议（PD / PPS / UFCS / SCP）、切场景模式、改息屏时间等；  
  每次写入设备都会回读确认，界面上的开关状态就是设备真实状态。
- **用电量统计**：对瞬时功率做梯形时间积分（米家 App 同思路），按天分桶存盘，重启不丢。
- **界面风格一键切换**：右上角按钮在「经典深色」与「鸿蒙 UI（HarmonyOS Design）」两套外观间切换，
  只换皮不动布局（系统蓝 #007DFF、胶囊按钮、8/12/16 圆角）；图标取自 OpenHarmony 官方设计图标包
  （CC-BY-4.0）。桌面版与安卓版共用同一份 `web/index.html`，切换方式一致。

## 从源码编译（产出单文件 exe）

环境：**Windows + Python 3.13**（需加入 `PATH`）。

```bat
双击 build_py\重新打包exe(Python版).bat
```

首次运行会自动建虚拟环境（`.venv`）并安装依赖，产物落在  
`dist_py\cuktech 10 ultra.exe`（约 17 MB，GUI 子系统，双击无控制台）。

手动等效步骤：

```bat
cd build_py
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe build.py
```

依赖版本锁定在 `build_py\requirements-build.txt`（PyInstaller / bleak / aiohttp / cryptography /  
cffi / winrt-Windows.Devices.Bluetooth），与已发布版本一致。

## 运行

双击 `dist_py\cuktech 10 ultra.exe`，首次会打开系统自带 Edge 的无地址栏应用窗口  
（观感等同原生程序，沿用你自己的 Edge 档案）。  
点右上角「登录小米云」，用手机米家 App 扫描二维码并确认授权**一次**即可。

数据目录默认在 `%LOCALAPPDATA%\cuktech10ultra\data`（可用环境变量 `CUKTECH_HOME` 覆盖）。

## 隐私

登录只用于从小米云取本机这台充电头的蓝牙密钥，密钥只保存在本机 `data\cuktech.token`，  
**不上传任何别的地方**。`data\` 已在 `.gitignore` 中排除，不会随仓库上传。

## 目录结构

| 路径                          | 说明                                                       |
| --------------------------- | -------------------------------------------------------- |
| `pyapp/`                    | 后端服务（HTTP API + 管理采集器进程 + 用电量账本 + A 口自动化）                |
| `collector.py` / `login.py` | 蓝牙采集器 / 小米云扫码登录（由 `pyapp` 直接调用，未改动）                      |
| `vendor/`                   | 第三方 BLE 协议库（MIT，取自 zuyan9/ha-cuk-ble）                    |
| `web/index.html`            | 界面（单文件，无任何外部依赖、零 CDN）                                    |
| `app.ico` / `tray.ps1`      | exe 图标 / 任务栏功率图标（由 `pyapp` 自动启动）                         |
| `build_py/`                 | 打包链路（`build.py` + `cuktech10ultra.spec` + 依赖清单 + 一键 bat） |
| `docs/`                     | 架构设计文档（`system_design.md` 及类图/时序图）                       |

## 已知限制

- 蓝牙同时只允许一个中心端连接：若米家 App 正连着充电头，请先在米家断开它。
- 本机固件**没有"定时关闭"对外出口**；充电头**温度**也**没有任何对外出口**  
  （界面「高级 - 扫描隐藏属性 / 温度」可随时重跑实测，固件升级后若有新属性会冒出来）。
- 详细的产品协议位定义、电量积分方法、读不到什么，见 `使用说明.txt`。
