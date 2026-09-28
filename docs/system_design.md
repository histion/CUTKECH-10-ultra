# 酷态科10号Ultra —— Python 单文件版系统设计（Route A：PyInstaller onefile）

> 版本：设计稿 v1（2026-09-28）
> 作者：Bob（Architect）
> 决策前提（用户已拍板）：**走 Route A（PyInstaller onefile）**，以 16.26 MB 体积为优先，接受每次启动约 3.6 s 的自解压开销。
> 关键约束（硬性）：
> 1. 新实现**并行新增**到新目录（`pyapp\`、`build_py\`、`dist_py\`），**不就地改写** `server.js` / `launcher.js` / `autoA.js` / `src\sea-main.js` / `build\build.js`；旧 Node 链路保持可跑，直到新链路通过全量回归。
> 2. `0.0.01备份\`、`1.0.0备份\`（及项目外 `E:\workbuddy\工作空间\酷态科10号Ultra-离线备份\`）是用户交付基线，**任何清理动作必须显式排除**（只按精确路径操作，禁止通配/按体积/按"不在白名单"批量删）。
> 3. 迁移后**用户无需重新扫码**；`web\index.html` 前端**一行不改**，接口 JSON 字段结构必须与 `server.js` 现状对齐。

---

# Part A：系统设计

## 1. 实现方案（Implementation Approach）

### 1.1 要解决的核心技术难点

| 难点 | 现状（Node SEA） | 新实现（PyInstaller onefile） |
|---|---|---|
| 单文件交付 | `runtime/node.exe`(92 MB) 复制 + SEA blob 注入，exe ≈ 109 MB | PyInstaller 直接把解释器+代码+依赖打进一个 exe，**无需外置 runtime** |
| 体积 | 109 MB（node.exe 底座无法瘦身） | **16.26 MB**（解释器+stdlib 子集+BLE 依赖，deflate 内嵌） |
| 后端语言 | `server.js`（Node，纯内置模块） | `pyapp\*.py`（Python 3.13，http.server 纯 stdlib） |
| BLE/登录 | 已是 Python（`collector.py` / `login.py`），被 Node 以子进程方式调用 | **原样复用** `collector.py` / `login.py`，改为 exe 自我重入（`--role`）子进程 |
| 自解压目录 | 自定义归档释放到 `%LOCALAPPDATA%\cuktech10ultra\app\<版本>`（**稳定、持久、带 marker/自愈**） | PyInstaller 释放到 `%TEMP%\_MEIxxxxxx`（**每次启动都变、退出即删**）→ 资源安置策略必须重构 |
| 子进程启动成本 | venv python 二次拉解释器，冷启动慢 | PyInstaller 引导器设 `_MEIPASS2` 环境变量，**自我重入的子进程复用同一解压目录**，实测 **0.21 s** |

### 1.2 框架与库选型

- **打包器：PyInstaller**（onefile + windowed）。
  - 理由：唯一成熟的"Python → 单文件 Windows exe"方案；支持 `--icon` / `--windowed`（无控制台）/ `--splash`（启动闪屏）；`_MEIPASS2` 机制天然支持"exe 自我重入做子进程"。
  - 备选（已否决）：Nuitka（编译型，构建慢、体积/兼容性不可控）；cx_Freeze（onefile 支持弱）。
- **HTTP 后端：标准库 `http.server`（`ThreadingHTTPServer` + `BaseHTTPRequestHandler`）**。
  - 理由：本地单用户、并发极低，前端只是每秒轮询若干个 GET + 少量 POST；零第三方依赖 = 不增加体积、不引入打包坑。用 `threading.RLock` 保护共享状态。
- **BLE / 登录：复用现有 `vendor\cuktech_ble` + `collector.py` + `login.py`**（依赖 `bleak`、`winrt-*`、`aiohttp`、`cryptography`、`cffi`，均已在本机 `runtime\python\Lib\site-packages`）。
- **托盘：复用 `tray.ps1`**（PowerShell + WinForms，零依赖）。
- **窗口/快捷方式：复用 `launcher.js` / `shortcut` 的逻辑**，等价重写为 `browser_launcher.py` / `shortcut.py`。

### 1.3 架构模式

- **分层 + 事件循环驱动**：HTTP 层（`http_server.py`）→ 服务/领域层（`state_store` / `energy_ledger` / `control` / `auto_a`）→ 设备层（`collector_bridge` 管理 BLE 子进程）。层间单向依赖，设备层通过 JSONL 管道与子进程通信（**沿用现有 `collector.py` 的 stdout 协议，零改动**）。
- **进程模型保持"1 主进程 + N 子进程"**：主进程 = HTTP 服务 + 托盘 + 窗口；子进程 = BLE 采集器 / 扫码登录 / 属性扫描，均由 exe 以 `--role` 自我重入。理由：与现状 1:1 对应，最大化复用现有经验证的代码与协议，降低回归风险。

### 1.4 体积与启动实测数据表（选型依据）

> 测量环境：本机 Windows x64、Python 3.13（`runtime\python`）、PyInstaller 6.x、UPX 关闭（`--noupx`）。体积为**最终单文件/目录的实际字节数**，启动为**冷启动**（无预缓存）平均耗时。

| 方案 | 形态 | 体积 | 启动 | 复现命令 | 产物路径 |
|---|---|---|---|---|---|
| **A：PyInstaller onefile（完整，含 BLE 依赖）** ✅选定 | 单文件 exe | **16.26 MB** | ~3.6 s / 次（每次解压） | `python -m PyInstaller --onefile --windowed --noupx --icon app.ico --name "cuktech 10 ultra" --collect-all bleak --collect-all winrt pyapp/__main__.py` | `dist_py\cuktech 10 ultra.exe` |
| B：PyInstaller onedir | 目录 | 37.62 MB | ~0.4 s（无需解压） | `python -m PyInstaller --onedir --windowed --noupx --icon app.ico --name "cuktech 10 ultra" pyapp/__main__.py` | `dist_py\cuktech 10 ultra\cuktech 10 ultra.exe` |
| 基线：纯 stdlib onefile | 单文件 exe | **7.72 MB** | ~1.x s | 用仅 `import http.server,json,...` 的探针脚本 `--onefile` 构建 | `build_py\_probe\probe.exe` |
| 子进程复用 `_MEIPASS2` | 无额外体积 | — | **0.21 s**（免二次解压） | 主进程 `subprocess([sys.executable,"--role","collector",...])`（引导器已设 `_MEIPASS2`） | — |

**结论与推理：**
- 基线 7.72 MB 是"解释器 + stdlib 子集"的下限；A 比基线多出的 **~8.5 MB** 全部来自 BLE 运行期依赖（`bleak` / `winrt-*` / `aiohttp` / `cryptography` / `cffi`），与实测依赖体量吻合。
- **B（onedir）37.62 MB** 明显更大（无整体压缩、含分立的 pyd/dll），且**不是单文件**，与"单文件 exe"交付形态冲突 → 否决。
- **A（onefile）16.26 MB** 满足"单文件 + 小体积"双要求，代价是每次启动自解压 ~3.6 s（用户已接受）；对比旧 Node SEA exe **≈109 MB**，体积下降约 **85%**。
- `_MEIPASS2` 复用把**子进程**（采集器/登录/扫描）启动压到 **0.21 s** —— 这保证了"exe 自我重入做子进程"在高频重连场景下无额外开销。

---

## 2. 文件清单（File List）

> 说明：`web\` / `vendor\` / `collector.py` / `login.py` / `tray.ps1` / `app.ico` 为**只读复用**（不修改、不移动）；下方"新增"为本次要写的文件。旧 Node 链路文件全部保留。

```
酷态科10号Ultra\
├─ pyapp\                              【新增】Python 服务端（替代 server.js 的运行时角色）
│  ├─ __init__.py                      包声明 + 语义化 VERSION 常量
│  ├─ __main__.py                      入口：角色分发 + 服务生命周期（单实例/空闲退出/关机）
│  ├─ paths.py                         根/数据/资源路径 + 环境变量契约 + 数据迁移 + 旧残留清理
│  ├─ config_store.py                  config.json 读写与校验
│  ├─ state_store.py                   内存状态容器 + history/logLines + 通用小工具
│  ├─ energy_ledger.py                 电量账本（积分/分日/会话）
│  ├─ auto_a.py                        A 口自动化（纯逻辑 + 运行态纪律）
│  ├─ collector_bridge.py              BLE 采集器子进程管理 + 命令收发（ack）
│  ├─ control.py                       /api/control 各动作（端口/协议/属性/恢复默认/复位）
│  ├─ login_proxy.py                   扫码登录（qr/poll 子进程代理）
│  ├─ shortcut.py                      创建桌面快捷方式
│  ├─ tray.py                          任务栏图标（启停 tray.ps1）
│  ├─ browser_launcher.py              Edge/Chrome --app 窗口启动 + 旧档案清理
│  └─ http_server.py                   HTTP 路由 + 各接口实现（对齐 server.js）
├─ build_py\                           【新增】新打包链路
│  ├─ build.py                         PyInstaller 编排（收集→构建→rcedit→子系统）
│  ├─ cuktech10ultra.spec              PyInstaller 规格（onefile/windowed/icon/hiddenimports）
│  ├─ requirements-build.txt           构建期依赖锁定
│  ├─ 重新打包exe(Python版).bat          一键构建（中文路径安全、幂等）
│  └─ qa_parity.py                     接口一致性回归（对齐 server.js 字段）
├─ dist_py\                            【新增】新产物输出目录（并行期，避免覆盖旧 exe）
│  └─ cuktech 10 ultra.exe
├─ collector.py        【复用·不修改】BLE 采集器（stdout JSONL）
├─ login.py            【复用·不修改】小米云扫码登录
├─ web\index.html      【复用·不修改】前端（打包进 exe）
├─ vendor\             【复用·不修改】cuktech_ble + ad1204u_read_props.py
├─ tray.ps1            【复用·不修改】托盘脚本（打包进 exe）
├─ app.ico             【复用·不修改】图标
├─ data\               【不打包】用户数据（token/配置/历史/账本/日志）
├─ server.js / launcher.js / autoA.js / src\sea-main.js / build\build.js
│                      【保留·不修改】旧 Node 链路（回归通过前一直可跑）
├─ 启动酷态科10号Ultra.bat / 调试启动.bat  【保留·不修改】旧便携版入口（详见 §6.3）
└─ docs\system_design.md               【本文件】
```

---

## 3. 数据结构与接口（Data Structures & Interfaces）

### 3.1 类图

见 `docs/class-diagram.mermaid`。核心类：

- `App`（`http_server.py`）：门面，持有 `AppState` / `EnergyLedger` / `CollectorBridge` / `LoginProxy` / `AutoARunner` / `ConfigStore` / `TrayManager`，实现所有接口。
- `AppState`（`state_store.py`）：线程安全的运行态容器。
- `EnergyLedger`（`energy_ledger.py`）：电量账本。
- `CollectorBridge`（`collector_bridge.py`）：采集器子进程生命周期与命令通道。
- `LoginProxy`（`login_proxy.py`）：登录 job 状态机。
- `AutoARunner`（`auto_a.py`）：自动化纪律状态机（纯逻辑函数 + 运行态）。
- `ConfigStore`（`config_store.py`）：配置持久化。

### 3.2 关键函数签名（节选）

```python
# pyapp/paths.py
def frozen() -> bool
def meipass_dir() -> Path                 # sys._MEIPASS（打包后）或项目根（开发期）
def exe_path() -> Path                    # sys.executable（打包后=exe 自身；稳定）
def root_dir() -> Path                    # CUKTECH_HOME 或 %LOCALAPPDATA%\cuktech10ultra
def data_dir() -> Path                    # CUKTECH_DATA_DIR 或 root_dir\data
def resource_path(rel: str) -> Path       # 打包资源定位（web/tray.ps1/app.ico/...）
def ensure_runtime_assets() -> dict       # 把必须"跨进程存活"的资源落到 root（app.ico/tray.ps1）
def apply_env_contract() -> None          # 规范化 CUKTECH_DATA_DIR/HOME/EXE 等
def maybe_import_data() -> int            # 旧数据迁移（不要求重新扫码）
def cleanup_legacy_app_tree(on_log=None) -> None   # 删 %LOCALAPPDATA%\cuktech10ultra\app\*
def child_argv(role: str, extra: list[str]) -> list[str]
def child_env() -> dict[str, str]

# pyapp/state_store.py
class AppState:
    def push_history(self, sample: dict) -> None
    def push_log(self, line: str) -> None
    def snapshot_link(self) -> dict
PORTS = ["c1", "c2", "c3", "a"]
def day_key(ts: int) -> str
def round3(v) -> float
def zero_ports() -> dict
def map_ports(p: dict | None) -> dict

# pyapp/energy_ledger.py
class EnergyLedger:
    def load(self) -> None
    def save(self, force: bool = False) -> None
    def touch(self) -> None
    def integrate(self, s: dict) -> None
    def backfill(self) -> int
    def snapshot(self, days: int) -> dict
    @staticmethod
    def sample_from(row: dict) -> dict

# pyapp/auto_a.py
LOAD_W = 0.5; RELEASE_W = 0.2; LOW_CURRENT_PIID = 0x0F; C_PORTS = ["c1","c2","c3"]
def compute_auto_a_action(latest: dict | None, current: dict | None = None) -> dict
def consume_write_budget(budget: dict, key: str, max_writes: int) -> dict
class AutoARunner:
    def evaluate(self) -> None
    def reset_counters(self) -> None

# pyapp/collector_bridge.py
class CollectorBridge:
    def start(self) -> dict
    def stop(self) -> None
    def restart_soon(self) -> None
    def send_command(self, obj: dict, timeout_ms: int = 15000) -> dict   # 返回 ack 原对象
    def on_line(self, obj: dict) -> None
    def run_sweep(self, lo: int | None, hi: int | None) -> dict

# pyapp/control.py
def set_port(bridge, port: str, action: str) -> dict
def set_protocol(bridge, port: str, sw: str, on: bool) -> dict
def set_prop(bridge, piid: int, value, advanced: bool = False) -> dict
def handle_control(bridge, body: dict | None) -> dict

# pyapp/http_server.py
class App:
    def build_state_payload(self) -> dict
    def build_energy_payload(self, days: int) -> dict
    def handle_api(self, method: str, path: str, query: dict, body: dict | None) -> tuple[int, bytes, str]
def make_handler(app: App) -> type[BaseHTTPRequestHandler]

# pyapp/__main__.py
def main() -> int          # 角色分发：server | collector | sweep | login-qr | login-poll
def run_server() -> int
def shutdown() -> None
def probe(port: int) -> bool
```

### 3.3 程序调用流

见 `docs/sequence-diagram.mermaid`（覆盖：冷启动、`/api/state` 轮询、`/api/control` 写设备、A 口自动化、扫码登录、退出）。

---

## 4. 运行时结构（Route A 选定路线的运行时布局）

### 4.1 谁解压谁

```
用户双击  dist_py\cuktech 10 ultra.exe
        │
        ▼
PyInstaller 引导器（bootloader，exe 头部）
  ├─ 首次进入：把内嵌归档自解压到  %TEMP%\_MEIxxxxxx\       ← 目录名每次启动都不同
  │    （约 16 MB，解压耗时 ~3.6 s；进程退出时引导器删除该目录）
  ├─ 设置环境变量 _MEIPASS2 = 该解压目录，供"自我重入"的子进程复用
  └─ 拉起 Python 解释器 → 执行 pyapp\__main__.py
        │
        ▼
pyapp\target = server（默认角色）
  ├─ sys._MEIPASS 指向  %TEMP%\_MEIxxxxxx\  → 读 web\index.html / tray.ps1 / collector.py ...
  ├─ 稳定数据目录：%LOCALAPPDATA%\cuktech10ultra\data\   （CUKTECH_DATA_DIR 注入）
  └─ 子进程：subprocess([sys.executable, "--role", "collector", ...])
        │
        ▼
PyInstaller 引导器（第二次进入）
  └─ 发现 _MEIPASS2 已指向有效解压目录 → **跳过解压**，直接跑 → 实测 0.21 s
```

### 4.2 三个"位置"的职责（关键变更）

| 位置 | 路径 | 生命周期 | 放什么 |
|---|---|---|---|
| **解压目录**（`_MEIPASS`） | `%TEMP%\_MEIxxxxxx\` | 本次进程存活期间；**每次启动变、退出即删** | 全部**只读代码与资源**：`pyapp`、`collector.py`、`login.py`、`web\`、`vendor\`、`tray.ps1`、`app.ico` |
| **稳定根目录** | `%LOCALAPPDATA%\cuktech10ultra\` | 持久 | `data\`（用户数据）、**`app.ico` 的稳定副本**、**`tray.ps1` 的稳定副本**、`running.lock`/`ready.json`、`launcher.log`（启动日志） |
| **用户数据目录** | `%LOCALAPPDATA%\cuktech10ultra\data\`（=`CUKTECH_DATA_DIR`） | 持久 | `cuktech.token` / `config.json` / `history.jsonl` / `energy.json` / `app.log` / `qr.png` / `qr-state.json` |

### 4.3 ⚠️ "释放目录里放东西"策略必须重构（本次最核心的差异）

Node SEA 版把代码释放到**稳定的** `app\<版本>\`，因此 `server.js` 可以放心从该目录读 `web\index.html`、`spawn tray.ps1`、用 `app.ico`，并靠 marker 缓存实现"二次启动 0.26 s"。**PyInstaller onefile 的解压目录每次启动都变且退出即删**，因此：

1. **代码与只读资源**：不再"释放到磁盘某处再读"，而是**直接从 `_MEIPASS` 读**（`resource_path()` 封装）。
   - `web\index.html`：由**本进程**按请求读取，`_MEIPASS` 在本进程存活期间稳定 → **直接读 `_MEIPASS\web\index.html` 即可，无需落盘到别处**。
   - `vendor\` / `collector.py` / `login.py`：由子进程通过 `runpy.run_path(_MEIPASS\collector.py)` 执行；`collector.py` 内部 `Path(__file__).parent/"vendor"` 恰好 = `_MEIPASS\vendor`（已把 `vendor\` 平铺进包根）→ **零改动可用**。
2. **必须"跨进程/跨退出存活"的资源**：**落到稳定根目录**（`ensure_runtime_assets()`，幂等：不存在或 sha256 不符才写）：
   - **`app.ico` → `%LOCALAPPDATA%\cuktech10ultra\app.ico`**：
     桌面快捷方式的 `IconLocation` 必须在本进程退出后仍有效，绝不能指向会被删除的 `_MEIPASS`。快捷方式 `TargetPath` 指向 `sys.executable`（= exe 自身，路径稳定）。
   - **`tray.ps1` → `%LOCALAPPDATA%\cuktech10ultra\tray.ps1`**：
     从稳定路径用 `-File` 启动 PowerShell 托盘（`tray.ps1` 虽在本进程存活期间读一次即可，但稳定副本让"服务重启后仍能拉起托盘""崩溃后手工排障"都成立，且便于日志定位）。
3. **marker / 自愈 / 旧版本（`app\1.0.0`）清理**：**自定义的释放目录机制整体退役**。
   - 不再有 `app\<版本>\` 释放树、不再有 `.cuktech-extracted.json` marker、不再有 `repairInPlace`（PyInstaller 自己保证解压完整性）。
   - **保留并改造**的只有清理：启动后**异步**删除 **旧 Node SEA 遗留树** `%LOCALAPPDATA%\cuktech10ultra\app\`（内含 `1.0.0\`、`0.0.1\`、`*.old-<pid>\`）与旧隔离档案 `%LOCALAPPDATA%\CuktechMonitor\browser`。删除**只针对这两个精确路径**，失败只记日志、绝不阻塞启动。

### 4.4 子进程重入（`_MEIPASS2` 复用）

- 子进程统一用 `paths.child_argv(role, extra)` 生成：
  - 打包后：`[sys.executable, "--role", role, *extra]`
  - 开发期：`[sys.executable, "-m", "pyapp", "--role", role, *extra]`
- 环境变量：`child_env()` 注入 `CUKTECH_DATA_DIR`（稳定数据目录）、`PYTHONIOENCODING=utf-8`、`PYTHONUNBUFFERED=1`、`PYTHONUTF8=1`（与现有 Node 版一致），并**继承 `_MEIPASS2`**（由 PyInstaller 引导器自动设置），使子进程复用同一解压目录（0.21 s）。
- ⚠️ **退出纪律**：onefile 引导器在**主进程退出时**删除 `_MEIPASS`；若有子进程仍占用该目录会删不掉并残留。因此 `shutdown()` **必须先 kill 全部子进程**（collector / login / sweep），再退出。

---

## 5. Python 服务端模块清单（与 `server.js` 的映射）

> 标记：【复用】= 现有 `.py` 直接用；【重写】= 等价实现 `server.js` 中对应功能；【新增】= 打包/集成层。

| 模块 | 来源 | 职责 | 对齐 `server.js` 的位置 |
|---|---|---|---|
| `pyapp/__init__.py` | 新增 | 包声明；`VERSION='1.3.0'`、`APP_INFO` 默认值 | `const VERSION` / `APP_INFO` |
| `pyapp/__main__.py` | 新增 | 角色分发；服务生命周期（单实例探测、空闲自杀、`shutdown`、信号） | `main()` / `probe()` / `shutdown()` |
| `pyapp/paths.py` | 新增 | 路径解析、环境变量契约、资源定位、数据迁移、旧残留清理 | `DATA`/`APP`/`WEB` + `sea-main.js` 的 `rootDir/dataDir/maybeImportData/queueCleanup` |
| `pyapp/config_store.py` | 重写 | `config.json` 读写 + 校验（interval 0.5–10、trayMode、autoA） | `loadConfig()` / `saveConfig()` |
| `pyapp/state_store.py` | 重写 | 运行态容器、`push_history`/`push_log`、`day_key`/`round3`/`map_ports` | `const state`、`pushHistory`、`pushLog`、`dayKey`、`round3`、`mapPorts` |
| `pyapp/energy_ledger.py` | 重写 | 电量积分/分日/会话/回填/快照 | 用电量统计整段 |
| `pyapp/auto_a.py` | 重写 | 纯逻辑（阈值/迟滞/预算）+ 运行态纪律（防抖/最小间隔/重入/重试上限） | `autoA.js` + `evaluateAutoA`/`applyAutoA`/`resetAutoCounters` |
| `pyapp/collector_bridge.py` | 重写 | 采集器子进程启停/重连、stdout JSONL 解析、`send_command`(ack)、`run_sweep` | `findPython`/`startCollector`/`stopCollector`/`restartCollectorSoon`/`handleCollectorLine`/`sendCommand`/`runSweep` |
| `pyapp/control.py` | 重写 | `/api/control` 各动作（端口位掩码、协议位掩码、单属性、恢复默认、复位、ext-read） | `setPort`/`setProtocol`/`setProp`/`handleControl`/`DEFAULTS` |
| `pyapp/login_proxy.py` | 重写 | 登录 job（`qr`/`poll` 子进程代理、地址自动发现、重启采集器） | `loginJob`/`runPy`/`loginStart`/`loginPoll` |
| `pyapp/shortcut.py` | 重写 | 用 PowerShell `-EncodedCommand` 建桌面 `.lnk` | `createShortcut()` |
| `pyapp/tray.py` | 重写 | 启停 `tray.ps1`（PS + WinForms），最多重试 4 次 | `startTray`/`stopTray` |
| `pyapp/browser_launcher.py` | 重写 | 注册表 App Paths→常见路径→默认浏览器；`--app=` 开窗；清旧隔离档案 | `launcher.js` 全部 |
| `pyapp/http_server.py` | 重写 | HTTP 路由 + 全部接口 + 静态文件 | `http.createServer` 整段 + `buildStatePayload`/`energySnapshot` |
| `collector.py` | **复用·零改动** | BLE 采集（`--role collector` 重入执行） | 现状 |
| `login.py` | **复用·零改动** | 扫码登录（`--role login-qr/poll` 重入执行） | 现状 |
| `tray.ps1` / `web/index.html` / `vendor/` / `app.ico` | **复用·零改动** | 见 §4.2 | 现状 |

---

## 6. 接口对齐清单（关键：前端一行不改）

> 以下 JSON 结构**逐字段对齐 `server.js` 现状**（含字段名大小写、`ok` 语义、默认值、裁剪范围）。实现时以本清单为准，`qa_parity.py` 逐条断言。

### 6.1 公共约定

- 所有响应 `Content-Type: application/json; charset=utf-8`、`Cache-Control: no-store`、`Content-Length` 显式。
- `VERSION = "1.3.0"`（仅出现在 `/api/ping.v` 与 `/api/state.version`，不上界面）。
- `APP_INFO = { "name": os.environ["CUKTECH_APP_NAME"] or "cuktech 10 ultra", "version": ... or "1.0.0", "vendor": ... or "Histion" }`。

### 6.2 逐接口字段

**GET `/api/ping`** →
```json
{ "ok": true, "v": "1.3.0" }
```

**GET `/api/state`** →（`ok` 恒 `true`）
```json
{
  "ok": true,
  "version": "1.3.0",
  "app": { "name": "cuktech 10 ultra", "version": "1.0.0", "vendor": "Histion" },
  "now": 1727500000000,
  "link": { "state": "idle|connected|reconnecting|need-login|error", "msg": "…", "attempt": 0, "rssi": -63 },
  "collecting": true,
  "config": { "address": "AA:BB:CC:DD:EE:FF", "interval": 1.5, "tray": true, "trayMode": "total|all|panel", "autoA": false },
  "env": { "python": "C:\\...\\cuktech 10 ultra.exe", "pythonFound": true, "tokenPresent": true, "appDir": "C:\\Users\\…\\cuktech10ultra", "platform": "win32 10.0.26100" },
  "latest": { "t": "state", "at": 1727500000000, "rssi": -63, "ports": [ {"id":"c1","v":20.1,"a":0.0,"w":0.0}, … ], "settings": { "…": "…" } },
  "historyCount": 240,
  "writable": [ { "piid": 5, "name": "场景模式", "min": 0, "max": 3, "advanced": false }, … ],
  "switches": [ { "port": "a", "sw": "ufcs", "bit": 24, "label": "…" }, … ],
  "sweep": null,
  "energy": {
    "todayWh": 12.345, "totalWh": 890.123, "tracking": true,
    "current": { "ms": 123456, "wh": 4.5, "peak": 88.2, "ports": {"c1":1.1,"c2":0.0,"c3":0.5,"a":0.2} }
  },
  "login": { "running": false, "kind": null, "result": null }
}
```
> 说明：
> - `env.python` 在 Python 版 = 冻结 exe 路径（`sys.executable`），`env.pythonFound` **恒 `true`**（解释器已内置），`env.appDir` = 稳定根目录。**键名必须保持不变**（前端可能据 `pythonFound` 显示环境提示）。
> - `link` 可带可选 `rssi`（沿用现状：connected 后随采样刷新）。
> - `latest` = 采集器最近一帧 `t:"state"` 原文（**透传，不改字段**）；无数据为 `null`。
> - `energy.tracking` = 是否存在进行中的充电会话；`energy.current` 无会话为 `null`。
> - `login` 的 `result` 取值：`null` / `{"ok":true,"region":…,"tokenLen":…}` / `{"ok":false,"error":"…"}`。

**GET `/api/history?seconds=300`** →（`seconds` 裁剪到 `[10, 3600]`，默认 300）
```json
{ "ok": true, "rows": [ { "at": 1727500000000, "total": 89.43, "w": {"c1":20.1,"c2":0.0,"c3":0.5,"a":68.8} }, … ] }
```

**GET `/api/log`** →（最近 **200** 行）
```json
{ "ok": true, "lines": [ "[20:31:02] 链路：connected", … ] }
```
> 行格式：`"[HH:MM:SS] " + 文本`（`zh-CN` 24 小时制）。

**GET `/api/energy?days=7`** →（`days` 裁剪到 `[1, 90]`，默认 7；`days` 数组**由旧到新**、长度 = `n`、含当天）
```json
{
  "ok": true,
  "today": { "day": "2026-09-28", "wh": 12.345, "ports": {"c1":1.1,"c2":0,"c3":0.5,"a":0.2} },
  "totalWh": 890.123,
  "ports": { "c1": 700.0, "c2": 100.0, "c3": 50.0, "a": 40.123 },
  "days": [ { "day": "2026-09-22", "wh": 0, "ports": {"c1":0,"c2":0,"c3":0,"a":0} }, … ],
  "current": { "start": 1727500000000, "lastActiveAt": 1727500123000, "ms": 123000, "wh": 4.5, "peak": 88.2, "ports": {"c1":1.1,"c2":0,"c3":0.5,"a":0.2} },
  "sessions": [ { "start": 1727400000000, "end": 1727403600000, "ms": 3600000, "wh": 60.0, "peak": 95.0, "ports": {"c1":60,"c2":0,"c3":0,"a":0} }, … ],
  "since": 1727300000000,
  "tracked": 1727500123000
}
```
> - `sessions`：最近 **20** 条的**倒序**（最新在前）。
> - `since` = `energy.firstAt || null`；`tracked` = `energy.lastAt || null`。
> - `round3` = 四舍五入到 3 位小数。

**GET `/api/login/qr.png`** → 原始 PNG 字节（读 `data\qr.png`）；`Content-Type: image/png`、`Cache-Control: no-store`；文件缺失 → `404` 文本 `no qr`。

**POST `/api/login/start`** →
```json
{ "ok": true, "timeout": 280, "ts": 1727500000000 }        // 成功
{ "ok": false, "error": "已有登录任务在进行" }               // 冲突/失败
```

**POST `/api/login/poll`** →（受理即返回，结果经 `/api/state.login.result` 获取）
```json
{ "ok": true }                                             // 已受理
{ "ok": false, "error": "已有登录任务在进行" }               // 冲突
```

**POST `/api/reconnect`** → `{ "ok": true }`（异步重启采集器）

**POST `/api/shortcut`** →
```json
{ "ok": true, "raw": "RESULT_OK" }        // 成功（raw = 截断到 200 的 stdout）
{ "ok": false, "error": "…" }             // 失败
```

**POST `/api/control`**（body `{ "action": "…", … }`）：
| action | 入参 | 响应 |
|---|---|---|
| `port` | `{port:"c1\|c2\|c3\|a\|all", on:bool}` | ack：`{ok:true,id,piid:16,value,readback,name}` 或 `{ok:true,piid:16,value,readback,noop:true}` 或 `{ok:false,error}` |
| `protocol` | `{port, sw, on:bool}` | `{ok:true,piid:21,value,readback}` / `{…,noop:true}` / `{ok:false,error}` |
| `ext` | `{}` | `{cmd:"ext-read"}` 的 ack（结构由 `collector.py` 定义） |
| `reset` | `{}` | `{cmd:"reset"}` 的 ack（**30 s** 超时） |
| `set` | `{piid:int, value:int, advanced?:bool}` | ack 或 `{ok:false,error}`（越界/非整数/不在可写清单） |
| `defaults` | `{}` | `{ok:true, done:[{piid,label,ok,error}]}` / `{ok:false,error,done}` |
| 其他 | — | `{ok:false, error:"未知动作：<a>"}` |

> `defaults` 步骤：`piid16=0x0f`（所有端口开启）→ 间隔 **260 ms** → `piid5=1`（场景=AI）；任一步失败即中止并返回 `done`。

**POST `/api/sweep?lo=0x16&hi=0x40`** →（`lo/hi` 可空，默认 `0x16`/`0x40`）
```json
{ "ok": true, "at": 1727500000000, "t": "sweep", "lo": 22, "hi": 64, "rows": [ … ], "found": [ … ] }
{ "ok": false, "error": "…" }
```
> 扫描前暂停采集器（BLE 单连接），完成/失败后 500 ms 重启采集器；原始输出写 `data\sweep.log`。

**POST `/api/config`**（body 可含 `interval`/`address`/`tray`/`trayMode`/`autoA`）→
```json
{ "ok": true, "config": { "address":"…", "interval":1.5, "tray":true, "trayMode":"total", "autoA":false } }
{ "ok": false, "error": "…" }     // 400
```
> 语义：`interval∈[0.5,10]`；`address` 需匹配 MAC 正则并大写；`tray` 切换即启停托盘；`trayMode∈{total,all,panel}`；`autoA` 变化时 `reset_counters()` + 记日志 + 立即评估一次；**仅当 `interval` 或有效地址真的变了**才重启采集器。

**POST `/api/show`** → `{ "ok": true }`（开窗口）
**POST `/api/quit`** → `{ "ok": true }`，200 ms 后 `shutdown()`

**GET `/` / `/index.html`** → `web\index.html`；**GET `/favicon.ico`** → `app.ico`（缺失 `204`）；其余路径 → 静态回退（越界 `403`）。

---

## 7. `autoA` 等价移植规格

> 必须与 `autoA.js` + `server.js` 的 `evaluateAutoA/applyAutoA` **行为等价**；阈值与纪律为硬性要求。

### 7.1 纯逻辑（`compute_auto_a_action`）

- 常量：`LOAD_W=0.5`、`RELEASE_W=0.2`、`LOW_CURRENT_PIID=0x0F`、`C_PORTS=["c1","c2","c3"]`。
- 区间判定：
  - **开条件** `band="load"`：任一 C 口 `w > LOAD_W` → `targetA=True, targetLowCurrent=True`。
  - **关条件** `band="idle"`：三 C 口**全部** `w <= RELEASE_W` → `targetA=False, targetLowCurrent=False`。
  - **迟滞区间** `band="hold"`：`RELEASE_W < max(C口) <= LOAD_W` **且**当前状态可读 → **保持当前状态**（`target=当前`）。
  - **降级** `band="idle-no-hysteresis"`：当前状态读不到（`ports_enabled.a` 或 `usb_a_always_on` 为 `null`）→ 退化为按负载判定（目标=关），由重试上限兜底。
- `currentStateFromLatest`：`a = latest.settings.ports_enabled.a`、`lowCurrent = latest.settings.usb_a_always_on`，缺失 → `null`。
- `needA = (curA is None) or (curA != targetA)`；`needLowCurrent` 同理；`changed = needA or needLowCurrent`。
- 返回字段与 JS 完全一致：`active, band, cLoaded, cAllReleased, aLoaded, cPorts, targetA, targetLowCurrent, curA, curLowCurrent, needA, needLowCurrent, changed, reason`。
- `consume_write_budget(budget, key, max_writes)`：key 变化 → 重置；同 key 第 `1..max` 次 `allow=true`；第 `max+1` 次 `allow=false, pause=true`；之后同 key `allow=false, pause=false`（已暂停）。

### 7.2 运行态纪律（`AutoARunner`）

| 参数 | 值 | 来源 |
|---|---|---|
| `AUTO_MIN_INTERVAL_MS` | **3000** | 两次实际动作最小间隔 |
| `AUTO_DEBOUNCE_SAMPLES` | **2** | 新目标需**连续 2 帧**一致才执行 |
| `AUTO_MAX_WRITES` | **3** | 同一目标连续写入 3 次仍未见效 → 暂停重试 |
| `busy` | bool | 写命令异步，防重入 |

- 评估触发：`config.autoA==True` 且 `link.state=="connected"` 且 `latest` 非空且 `not busy`，**每来一帧 `state` 调用一次**。
- 收敛：`changed==False` → `reset_counters()`（清防抖计数与重试预算）。
- 写序：**开** → 先 `setPort("a","on")` 再 `setProp(0x0F,1)`；**关** → 先 `setProp(0x0F,0)` 再 `setPort("a","off")`。
- 异常一律吞掉并记日志，**绝不影响采集主流程**。

### 7.3 日志文案（逐字对齐 `server.js`）

- 判定异常：`A口自动化：判定出错 — <msg>`
- 暂停重试：`A口自动化：连续 3 次写入后状态仍未变化，暂停重试`
- 动作头：
  - 开：`A口自动化：C 口出现负载 → 开启 USB-A + 小电流`
  - 关（A 口有载）：`A口自动化：C 口空闲，按自动化关闭 USB-A（此时 A 口有负载）`
  - 关（A 口空载）：`A口自动化：C 口空闲 → 关闭 USB-A + 小电流`
  - 失败后缀：` 失败：<项>（<原因>）；…`
- 开关切换：`A口自动化：已开启` / `A口自动化：已关闭`

---

## 8. 迁移与兼容

### 8.1 现有 `data\` 无缝沿用（**不得要求重新扫码**）

- 数据落点沿用 `CUKTECH_DATA_DIR`（默认 `%LOCALAPPDATA%\cuktech10ultra\data`）。新 exe 启动时执行 `maybe_import_data()`（等价重写 `sea-main.js` 的同名函数）：**仅当目标目录尚无 `cuktech.token`** 时，按优先级从以下来源导入 `cuktech.token` / `config.json` / `history.jsonl` / `energy.json`（同名不覆盖）：
  1. `%LOCALAPPDATA%\cuktech10ultra\app\<任意版本>\data`
  2. `%LOCALAPPDATA%\cuktech10ultra\app\<版本>.old-<pid>\data`
  3. exe 同目录 `data\`（老便携版就地升级）
- **必须早于**旧残留清理执行（先迁移、后删除），避免"迁移不可达"。
- 便携版（bat）用户数据在项目 `data\`：新 exe 首次运行时若 `CUKTECH_DATA_DIR` 未指向它，不会被自动带走；**处理方式**：安装包/说明中提示"如需沿用便携版数据，首次启动前设置 `CUKTECH_DATA_DIR` 指向它"，或提供一次性迁移脚本（列为可选增强，见 §11 UNCLEAR）。

### 8.2 旧 Node 残留清理

- 启动后**异步**（不阻塞启动）用 `cleanup_legacy_app_tree()` 删除 **旧 Node SEA 释放树**：`%LOCALAPPDATA%\cuktech10ultra\app\`（含 `1.0.0\`、`0.0.1\`、`*.old-<pid>\`）。
- 同时调用 `cleanup_legacy_profile()` 删除 `%LOCALAPPDATA%\CuktechMonitor\browser`（父目录仅恰为空才 `rmdir`）。
- **幂等、失败只记日志、只按精确路径**；`data\` 与所有"备份"目录**永不触碰**。

### 8.3 便携版 bat 处理

- `启动酷态科10号Ultra.bat` / `调试启动.bat` **保留、不修改**——它们驱动 **旧 Node 便携链路**（`server.js` + `runtime\node.exe`），该链路在回归通过前必须保持可跑（硬约束）。
- Python 版**暂无 bat 便携形态**：Route A 是"单文件 exe"，双击即用（数据在 `%LOCALAPPDATA%`）。建议在 `使用说明.txt` 增补一节"Python 版（新）"说明，并**标注旧 bat 属 Node 版**；不删除、不弃用文件本身。

### 8.4 环境变量契约（保持一致）

| 变量 | 用途 | 新实现 |
|---|---|---|
| `CUKTECH_HOME` | 覆盖根目录（默认 `%LOCALAPPDATA%\cuktech10ultra`） | 保留（测试隔离） |
| `CUKTECH_DATA_DIR` | 数据目录覆盖 | 保留，启动时注入给所有子进程 |
| `CUKTECH_EXE` | exe 自身路径（快捷方式指向） | 保留（= `sys.executable`） |
| `CUKTECH_NO_WINDOW=1` | 跳过开窗（自动化测试） | 保留 |
| `CUKTECH_NO_TRAY=1` | 跳过托盘 | 保留 |
| `CUKTECH_VERBOSE=1` | 日志同时打屏 | 保留 |
| `CUKTECH_APP_NAME/VERSION/VENDOR` | 产品信息注入 | 保留 |

---

## 9. 打包链路

### 9.1 新构建脚本（`build_py\`，与旧 `build\` 并存、互不干扰）

- **`build_py\build.py`**（编排，幂等）：
  1. 校验必需文件（`pyapp\__main__.py`、`collector.py`、`login.py`、`web\index.html`、`vendor\`、`tray.ps1`、`app.ico`）。
  2. 调用 PyInstaller（读 `cuktech10ultra.spec`）。
  3. 用 `rcedit` 写版本资源 + 图标（沿用本机隔离工具链 `C:/Users/…/.workbuddy/binaries/node/workspace/node_modules/rcedit`）。
  4. 可选：把 PE 子系统改为 `GUI(2)`（`--windowed` 已达成，无需二次改写；仅作为兜底核验）。
  5. 输出到 **`dist_py\cuktech 10 ultra.exe`**；打印体积 + sha256。
- **`build_py\cuktech10ultra.spec`** 要点：
  - `onefile`、`windowed`、`icon=app.ico`、`name="cuktech 10 ultra"`。
  - `datas`：`vendor\`、`web\`、`collector.py`、`login.py`、`tray.ps1`、`app.ico`（**平铺到包根**，保证 `collector.py` 的 `Path(__file__).parent/"vendor"` 命中）。
  - `hiddenimports` / `collect_all`：`bleak`、`winrt.*`（命名空间包，**必须显式收集**）、`aiohttp`、`cryptography`、`cffi`（`_cffi_backend`）。
  - `excludes`：`tkinter`、`test`、`idlelib`、`lib2to3`、`pip`、`ensurepip`（瘦身）。
  - `runtime_tmpdir`：建议设为 `%LOCALAPPDATA%\cuktech10ultra\rt`（稳定目录，减少杀软重复扫描；**注意退出纪律**，见 §11 风险）。
- **`build_py\requirements-build.txt`**：锁定 `pyinstaller==<ver>` 及运行期依赖版本（`bleak`、`aiohttp`、`winrt-*`、`cryptography`、`cffi`）。
- **`build_py\重新打包exe(Python版).bat`**：`chcp 65001`；用 `runtime\python\python.exe`（或 `py`）跑 `build.py`；**中文路径安全**（`%~dp0` 取脚本目录，全程引号）；失败 `pause` 显示报错；幂等（可重复执行）。
- **`build_py\qa_parity.py`**：启动打好的 exe（`CUKTECH_HOME` 指向临时目录隔离），逐条比对 §6 所有接口的**字段集合与类型**与 `server.js`（可用旧 Node 版同跑一侧做对照）。

### 9.2 与旧 Node 版构建互不干扰

- **旧链路原地保留**：`build\build.js`、`src\sea-main.js`、`server.js`、`launcher.js`、`autoA.js` 一行不改；`build\重新打包exe.bat` 照旧产出 `dist\cuktech 10 ultra.exe`。
- **新链路独立**：`build_py\*` 产出 `dist_py\*`。两条链路的中间产物、产物目录、入口脚本完全不同 → **无覆盖、无竞争**。
- **发布切换**：并行开发期新 exe 一直在 `dist_py\`；**仅当全量回归通过**后，再决定把 `dist_py\cuktech 10 ultra.exe` 提升为正式 `dist\cuktech 10 ultra.exe`（此切换为人工/一次性动作，见 §11 UNCLEAR 决策点）。

---

## 10. 任务分解（Part B）

### 10.1 依赖包（Required Packages）

**构建期（仅打包机需要，不进产物）：**
```
- pyinstaller@^6.x           : 单文件打包
- rcedit（本机隔离工具链）    : 写版本资源/图标
- pefile 或自写 PE 改子系统   : 可选，兜底 GUI 子系统
```
**运行期（打进 exe，均来自本机 `runtime\python\Lib\site-packages` 现有版本）：**
```
- bleak@3.0.2                          : BLE 通信
- winrt-runtime@3.2.1 + winrt-windows-* : WinRT 投影（BLE 后端）
- aiohttp@3.14.3 / aiosignal / multidict / yarl / frozenlist / propcache / aiohappyeyeballs : 异步 HTTP（小米云）
- cryptography@50.0.1 / cffi@2.1.1 / pycparser : 加密
- attrs / typing_extensions / idna      : 传递依赖
```
> 说明：以上运行期依赖**无需 pip 安装**——直接使用项目 `runtime\python` 中已就位的版本，PyInstaller 从该环境打包。新版本仅锁定，不升级。

### 10.2 任务清单（≤5 个，按依赖排序）

#### **T01 — 基础设施与路径层**（P0）
- **源文件**：`pyapp\__init__.py`、`pyapp\paths.py`、`pyapp\config_store.py`
- **依赖**：无
- **内容**：包声明与常量；`frozen/meipass_dir/exe_path/root_dir/data_dir/resource_path`；`apply_env_contract`；`ensure_runtime_assets`（app.ico/tray.ps1 落稳定根、sha 幂等）；`maybe_import_data`；`cleanup_legacy_app_tree`/`cleanup_legacy_profile`；`child_argv`/`child_env`；`config.json` 读写与校验。
- **验收点**：设 `CUKTECH_HOME=<tmp>` 后，根/数据目录落点正确；`ensure_runtime_assets` 二次调用不改写（sha 命中）；构造 `app\1.0.0\data\cuktech.token` 后 `maybe_import_data` 能导入且不覆盖已存在文件；旧 `app\` 树被清理而 `data\`/备份目录不受影响。

#### **T02 — 状态与账本内核**（P0）
- **源文件**：`pyapp\state_store.py`、`pyapp\energy_ledger.py`、`pyapp\auto_a.py`
- **依赖**：T01
- **内容**：`AppState`（线程安全）+ `push_history/push_log` + `day_key/round3/map_ports`；`EnergyLedger`（integrate/backfill/snapshot/track_session，`HISTORY_MAX=3600`、`DAYS_KEEP=400`、会话间隙 60 s、保存节流 15 s）；`auto_a.py` 纯逻辑 + `AutoARunner`（阈值/迟滞/降级/防抖/间隔/重入/预算）。
- **验收点**：`push_history` 超 3600 裁剪；梯形积分对间隔 > 60 s 的样本整段丢弃；`snapshot(days)` 结构完全匹配 §6；`compute_auto_a_action` 对"全 C 口空/有载/迟滞/状态不可读"四类输入与 JS 输出逐字段一致；`consume_write_budget` 第 4 次返回 `pause=true`。

#### **T03 — 设备链路与登录代理**（P0）
- **源文件**：`pyapp\collector_bridge.py`、`pyapp\control.py`、`pyapp\login_proxy.py`
- **依赖**：T02
- **内容**：① `CollectorBridge`：`start/stop/restart_soon`（等旧进程 `close` 再起、4 s 兜底）、stdout JSONL 解析（`hello/ack/link/error/state`）、`send_command`(ack + 超时 + 未连接/未运行拒绝)、`run_sweep`（暂停采集器→扫描→重启）；② `control.py`：端口位掩码（piid16，bit0..3）、协议位掩码（piid21，按 `switches` 位表翻转）、单属性（按 `writable` 校验范围）、`defaults`（260 ms 间隔）、`reset`（30 s 超时）；③ `login_proxy.py`：`start/poll`（`--wait 280 --verbose`）、成功后地址自动发现 + 重启采集器。
- **验收点**：无设备时 `send_command` 返回 `{ok:false,error:"采集器未运行…"}`；`setProp` 越界/非整数/不在清单被拒；`defaults` 步骤失败即中止并返回 `done`；`login/poll` 受理后 `state.login.result` 正确落值。

#### **T04 — 服务与集成层**（P0）
- **源文件**：`pyapp\http_server.py`、`pyapp\tray.py`、`pyapp\shortcut.py`、`pyapp\browser_launcher.py`、`pyapp\__main__.py`
- **依赖**：T03
- **内容**：`App` 门面 + `BaseHTTPRequestHandler` 路由（实现 §6 全部接口 + 静态文件）；`TrayManager`；`create_shortcut`（PS `-EncodedCommand`，指向 `sys.executable` + 稳定 `app.ico`，`RESULT_OK` 判定）；`find_browser/launch_window/cleanup_legacy_profile`；`__main__` 角色分发 + 单实例探测 + 空闲自杀（120 s）+ `shutdown`（先杀子进程）。
- **验收点**：`qa_parity.py` 全绿（字段/类型/默认值对齐）；`/api/state` 与旧版逐字段 diff 为空；无窗/无托盘模式（`CUKTECH_NO_WINDOW/NO_TRAY=1`）下可完整跑通；`/api/quit` 后进程干净退出、无 `_MEI` 残留。

#### **T05 — 打包链路与全量回归**（P0）
- **源文件**：`build_py\build.py`、`build_py\cuktech10ultra.spec`、`build_py\requirements-build.txt`、`build_py\重新打包exe(Python版).bat`、`build_py\qa_parity.py`
- **依赖**：T04
- **内容**：PyInstaller 编排 + spec（含 `bleak/winrt/aiohttp` 收集与 excludes）+ rcedit 版本/图标 + 一键 bat（中文路径、幂等）；对照 `server.js` 的接口一致性回归脚本。
- **验收点**：一键 bat 连续执行两次结果一致（幂等）；产物 `dist_py\cuktech 10 ultra.exe` ≈ 16 MB、双击无控制台；`runtime_tmpdir` 稳定目录策略下退出无残留；`qa_parity.py` 对新 exe 全绿。

### 10.3 并行性

- T01 完成后：`auto_a.py`（T02）与 `browser_launcher.py`/`tray.py`/`shortcut.py`（T04 内）**互不依赖**，可由不同人并行推进；`control.py`/`login_proxy.py`（T03）依赖 `collector_bridge` 的 `send_command` 接口，接口先冻结即可并行。

### 10.4 必须真机 BLE 验证的项（本机无充电头，静态/纯逻辑测试无法覆盖）

1. 采集器连通与稳定性（连接/重连/`MIOT RX counter` 类断开）。
2. `/api/control`：端口开关（piid16 位）、协议开关（piid21 位，含 A 口 UFCS bit24）、单属性写入、`defaults`、`reset`、`ext-read`。
3. `/api/sweep` 隐藏属性/GATT 扫描。
4. 扫码登录 `qr`→`poll`→`token` 落盘（真机二维码可扫、地址自动发现）。
5. **A 口自动化实机动作**：开/关 A 口与小电流、迟滞在涓流下的表现、失败重试与暂停日志。
6. 托盘图标/悬浮面板在真实任务栏的渲染与轮询。

---

## 11. 风险与缓解

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | onefile **每次启动都自解压**（~3.6 s，杀软实时扫描可能更慢） | 首屏延迟 | 用户已接受；`--splash` 闪屏（可选）；`runtime_tmpdir` 用稳定目录提升杀软信任；说明中提示加白名单 |
| R2 | **退出时子进程仍持有 `_MEIPASS`** → 引导器删不掉解压目录、残留 `_MEI` | 磁盘残留 | `shutdown()` **先 kill 全部子进程**（collector/login/sweep）再退出；`atexit` + 信号双保险 |
| R3 | `winrt-*` 是命名空间包，PyInstaller 易漏 | 运行时报缺模块（`ModuleNotFoundError: winrt...`） | spec 用 `collect_all('winrt')` + 显式 `hiddenimports`；T05 打包后**必跑打真机**（R 类）冒烟 |
| R4 | `--windowed` 下 `sys.stdout/stderr` 为 `None` | `print`/日志写 stdout 抛错或丢失 | 主进程日志一律走**文件**（`data\app.log`）；子进程用 `subprocess` **PIPE** 捕获（windowed 不影响我们自建的 PIPE） |
| R5 | 中文路径（项目路径含中文）+ PyInstaller | 构建/运行路径解析失败 | spec/bat 全程引号 + `chcp 65001`；路径统一 `pathlib.Path`；构建脚本不依赖 CWD |
| R6 | `app.ico` 被放进会被删除的 `_MEIPASS` | 桌面快捷方式图标失效 | `ensure_runtime_assets` 把 ico 复制到**稳定根目录**，快捷方式 `IconLocation` 指稳定路径 |
| R7 | 数据迁移遗漏 → **用户被迫重新扫码** | 严重体验事故 | `maybe_import_data` 沿用三段来源 + "无 token 才导入、不覆盖"；T01 专项用例覆盖三来源；迁移**早于**残留清理 |
| R8 | 清理误伤"备份/交付基线" | 不可逆数据损失 | 清理**只按精确路径**（`app\`、`CuktechMonitor\browser`）；代码内硬编码白名单；回归断言 `0.0.01备份\`/`1.0.0备份\` 未被触碰 |
| R9 | `runtime_tmpdir` 稳定目录下杀软更易锁定 | 解压失败 | 失败回退默认 `%TEMP%`；`build.py` 不做删除删除动作（避免 EBUSY） |
| R10 | 新旧产物同名覆盖 | 并行期丢失可用旧版 | 新产物固定 `dist_py\`；切换为**人工一次性**动作 |
| R11 | 前端依赖隐式字段（未被清单覆盖） | 界面局部异常 | 用真实 `web\index.html` 对旧/新 exe 各跑一遍，抓 `/api/state`/`/api/energy` 全字段 diff 为空；`qa_parity.py` 断言字段**集合**而非仅取值 |
| R12 | `server.js` 的 `VERSION='1.3.0'` 与产品 `1.0.0` 语义被改 | 前端版本显示错乱 | Python 版沿用 `VERSION='1.3.0'`、`app.version='1.0.0'`；如需区分新构建，单独决策（见下） |

---

## 12. Anything UNCLEAR（需确认/假设）

1. **新 exe 的产品版本号**：是否仍保持产品 `1.0.0`（与界面 `v1.0.0` 一致）？**假设保持**，以达成"前端一行不改"。若希望区分 Python 版，需同步改前端版本常量（违反"一行不改"，故默认不动）。
2. **正式产物命名与切换**：新 exe 是否最终仍叫 `dist\cuktech 10 ultra.exe`（即替换 Node 版），还是长期并列为独立名？**假设**：并行期用 `dist_py\cuktech 10 ultra.exe`，回归通过后由主理人决定是否提升为 `dist\`。
3. **便携版数据沿用**：是否需要"把项目 `data\` 一键迁移到 `%LOCALAPPDATA%`"的辅助（供旧便携版用户升级）？**假设**：本期不做自动迁移，仅保证 exe 版数据落点与旧 exe 版（同为 `%LOCALAPPDATA%\cuktech10ultra\data`）完全一致 → 旧 exe 用户**零感知**。
4. **启动闪屏（`--splash`）**：3.6 s 空白期是否要加 splash？**假设**：可选增强，本期先不加（保持与旧版一致的"双击后窗口直接出现"观感），如需再加。
5. **杀软白名单**：是否需要构建产物附带"加白名单"说明？**假设**：写入 `使用说明.txt` 的建议项，不做自动注册表/计划任务操作（安全优先）。
6. **`runtime_tmpdir` 稳定性 vs 残留**：稳定 tmpdir 提升性能但增加 R2 风险。**假设**：采用稳定 tmpdir，靠"先杀子进程"纪律兜底。
