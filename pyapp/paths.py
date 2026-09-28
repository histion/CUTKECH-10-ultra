"""路径解析、环境变量契约、资源定位、数据迁移与旧残留清理。

这是"去 Node 化"里**最核心的差异点**（见 design §4.3）：PyInstaller onefile 的解压
目录（``sys._MEIPASS`` → ``%TEMP%\\_MEIxxxxxx``）**每次启动都变且退出即删**，因此
不能像旧 Node SEA 版那样"把代码释放到稳定目录再从那里读"。本模块把这层差异收敛成
两条纪律：

  1. **只读代码与资源**直接从 ``_MEIPASS`` 读（``resource_path``）；本进程存活期间
     ``_MEIPASS`` 是稳定的，读 ``web/index.html`` / 交给子进程 ``collector.py`` 都没问题。
  2. **必须跨进程/跨退出存活**的资源（``app.ico`` / ``tray.ps1``）落到稳定根目录
     （``ensure_runtime_assets``，sha256 幂等）。

对旧 Node 用户而言**零感知**：数据目录仍是 ``%LOCALAPPDATA%\\cuktech10ultra\\data``，
登录态无需重扫（``maybe_import_data``）。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import PRODUCT

# 必须"跨进程/跨退出存活"的只读资源：桌面快捷方式图标、托盘脚本。
# 落到稳定根目录，避免指向会被删除的 ``_MEIPASS``（否则快捷方式图标失效 —— 见 R6）。
_RUNTIME_ASSETS = ("app.ico", "tray.ps1")

# 数据迁移要搬运的小文件（同名不覆盖）。顺序无关，语义见 ``maybe_import_data``。
_DATA_FILES = ("cuktech.token", "config.json", "history.jsonl", "energy.json")

# ⚠️ 安全白名单：任何路径里出现"备份"二字，清理动作一律拒绝。
# 这是对用户名/交付基线（``0.0.01备份`` / ``1.0.0备份`` / 项目外离线备份）的硬保护——
# 宁可漏清，绝不可误删（见 design §8.2 / R8）。
_BACKUP_MARK = "备份"

# 旧 Node SEA 释放目录下"某版本被改名"的残留形如 ``1.0.0.old-<pid>``。
_OLD_SUFFIX_RE = re.compile(r"\.old-\d+$")


# ------------------------------------------------------------------ 定位

def frozen() -> bool:
    """是否运行在 PyInstaller 打包后的 exe 里。

    引导器会设 ``sys.frozen``；个别版本只设 ``sys._MEIPASS``，故两者都查。
    """
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def meipass_dir() -> Path:
    """解压目录（打包后）或项目根（开发期）。

    开发期取 ``pyapp`` 包的上一级 = 项目根，与"打包后把 ``web``/``vendor``/``collector.py``
    等平铺到包根"一一对应 —— 这样 ``resource_path('web/index.html')`` 在两种形态下都成立。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent


def exe_path() -> Path:
    """exe 自身路径（打包后 = exe；开发期 = python 解释器）。用于快捷方式指向。"""
    return Path(sys.executable)


def root_dir() -> Path:
    """稳定根目录：``CUKTECH_HOME`` 或 ``%LOCALAPPDATA%\\cuktech10ultra``。

    纯解析（不落盘、不校验可写）：解析非法盘符 / 非法字符时 ``Path.resolve()`` 可能抛
    ``OSError``，此处兜底退回未解析的原路径，交给 ``resolve_root()`` 去做可用性校验。
    """
    home = os.environ.get("CUKTECH_HOME")
    if home:
        try:
            return Path(home).resolve()
        except OSError:
            return Path(home)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / PRODUCT["id"]


def _temp_root() -> Path:
    """备用稳定根：``%TEMP%\\cuktech10ultra``。"""
    return Path(tempfile.gettempdir()) / PRODUCT["id"]


def resolve_root() -> Path:
    """校验稳定根**确实可建可写**。仅在启动入口调用一次（``__main__``）。

    行为（与旧 Node 版基线对齐，见 QA 回归）：
      * **正常**：合法可写的 ``CUKTECH_HOME`` / ``%LOCALAPPDATA%\\cuktech10ultra`` 原样使用
        —— 正常路径行为完全不变；
      * **显式 ``CUKTECH_HOME`` 非法/不可写**（不存在的盘 / 非法字符 / 指向一个文件）：
        **抛出 ``OSError``**，交由 ``__main__`` 的启动兜底写 ``%TEMP%\\cuktech-launch-error.log``
        并 ``os._exit(1)`` —— 旧 Node 版同样是「~0.2s 快速退出 code=1 + 落日志、不弹框」。
        绝不静默改用别的目录（那会让用户困惑且与基线不符）。
      * **未显式指定、但默认根不可写**：退化到 ``%TEMP%\\cuktech10ultra`` 并记日志继续；
        连它也不可用才上抛，由上层兜底退出。
    """
    home = os.environ.get("CUKTECH_HOME")
    root = root_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError as exc:
        if home:
            raise                                 # 显式指定的根不可用 → 交给启动兜底快速退出
        _log("默认根不可用（%s）：%s" % (root, exc))
    fb = _temp_root()
    fb.mkdir(parents=True, exist_ok=True)          # 这里再抛就继续上抛到兜底
    os.environ["CUKTECH_HOME"] = str(fb)
    os.environ["CUKTECH_DATA_DIR"] = str(fb / "data")
    _log("已回落到备用根：%s" % fb)
    return fb


def data_dir() -> Path:
    """用户数据目录：``CUKTECH_DATA_DIR`` 或 ``root_dir()/data``。

    ⚠️ 与 exe 位置解耦 —— 升级版本 / 移动 exe 都不丢登录态与电量账本。
    """
    override = os.environ.get("CUKTECH_DATA_DIR")
    if override:
        return Path(override).resolve()
    return root_dir() / "data"


def resource_path(rel: str) -> Path:
    """打包只读资源定位：``meipass_dir()/rel``。"""
    return meipass_dir() / rel


def data_file(name: str) -> Path:
    """数据目录下的某个文件（``cuktech.token`` / ``history.jsonl`` / ``app.log`` …）。"""
    return data_dir() / name


# ------------------------------------------------------------------ 日志兜底

def _log(msg: str) -> None:
    """惰性引用 ``state_store.log``，避免 ``paths`` ↔ ``state_store`` 循环导入。"""
    try:
        from .state_store import log as _slog

        _slog(msg)
    except Exception:
        pass


# ------------------------------------------------------------------ 环境契约

def apply_env_contract() -> None:
    """规范化环境变量契约（design §8.4），并注入给所有子进程的基准。

    * ``CUKTECH_HOME`` → 解析后的稳定根（测试隔离 / 便携覆盖）；
    * ``CUKTECH_DATA_DIR`` → 稳定数据目录（子进程必须与主进程读到同一份数据）；
    * ``CUKTECH_EXE`` → exe 自身（桌面快捷方式指向）；
    * ``CUKTECH_APP_*`` → 产品信息（与界面显示一致）。
    """
    os.environ.setdefault("CUKTECH_HOME", str(root_dir()))
    os.environ["CUKTECH_DATA_DIR"] = str(data_dir())
    os.environ["CUKTECH_EXE"] = str(exe_path())
    os.environ["CUKTECH_APP_DIR"] = str(root_dir())
    os.environ.setdefault("CUKTECH_APP_NAME", PRODUCT["name"])
    os.environ.setdefault("CUKTECH_APP_VERSION", PRODUCT["version"])
    os.environ.setdefault("CUKTECH_APP_VENDOR", PRODUCT["vendor"])


def child_argv(role: str, extra: list[str] | None = None) -> list[str]:
    """生成子进程命令行（exe 自我重入）。

    * 打包后：``[exe, "--role", role, *extra]`` —— 引导器发现 ``_MEIPASS2`` 已存在会
      **跳过解压**，子进程冷启动实测 ~0.21s；
    * 开发期：``[python, "-m", "pyapp", "--role", role, *extra]``。
    """
    args = list(extra or [])
    if frozen():
        return [str(exe_path()), "--role", role, *args]
    return [str(exe_path()), "-m", "pyapp", "--role", role, *args]


def child_env() -> dict:
    """子进程环境：数据目录对齐 + UTF-8 三件套 + 继承 ``_MEIPASS2``。

    ``_MEIPASS2`` 由 PyInstaller 引导器自动设置并随 ``os.environ`` 继承 ——
    这正是"子进程复用同一解压目录、免二次解压"的关键。
    """
    env = dict(os.environ)
    env["CUKTECH_DATA_DIR"] = str(data_dir())
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


# ------------------------------------------------------------------ 稳定资源

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_runtime_assets() -> dict:
    """把 ``app.ico`` / ``tray.ps1`` 安置到稳定根目录，sha256 幂等。

    幂等语义：目标不存在 → 复制；存在且 sha 一致 → 跳过（记 ``kept``）；
    存在但内容不同 → 覆盖（记 ``copied``）。二次调用不产生任何写盘 —— 这正是
    "稳定副本 + 幂等"要达成的效果（design §4.3 / R6）。

    返回 ``{"root", "copied": [...], "kept": [...], "missing": [...]}``，便于自测断言。
    """
    root = resolve_root()          # 非法/不可写根在此回落到备用根（否则上抛给 __main__ 兜底）
    out: dict = {"root": str(root), "copied": [], "kept": [], "missing": []}
    for name in _RUNTIME_ASSETS:
        src = resource_path(name)
        dst = root / name
        if not src.exists():
            out["missing"].append(name)
            continue
        if dst.exists():
            try:
                if _sha256(dst) == _sha256(src):
                    out["kept"].append(name)
                    continue
            except OSError:
                pass  # 读不了就当需要重写
        try:
            shutil.copyfile(src, dst)
            out["copied"].append(name)
        except OSError as exc:
            _log("稳定资源安置失败（忽略）：%s — %s" % (name, exc))
    return out


# ------------------------------------------------------------------ 数据迁移

def _import_sources() -> list[Path]:
    """迁移来源（按优先级，design §8.1）：

      1. ``root/app/<版本>/data``           —— 本次（旧 Node 版）释放目录
      2. ``root/app/<版本>.old-<pid>/data`` —— 历次重解压留下的残树（按名排序）
      3. ``exe 同目录/data``                 —— 老便携版就地升级（仅打包形态）
    """
    apps = root_dir() / "app"
    srcs: list[Path] = [apps / PRODUCT["version"] / "data"]
    try:
        names = sorted(p.name for p in apps.iterdir())
    except OSError:
        names = []
    for name in names:
        if _OLD_SUFFIX_RE.search(name):
            srcs.append(apps / name / "data")
    if frozen():
        # 打包形态下 exe 同目录才有意义；开发期不把项目 data 误当来源（会污染隔离测试）。
        srcs.append(exe_path().parent / "data")
    return srcs


def maybe_import_data() -> int:
    """旧数据迁移：**仅在目标目录尚无 ``cuktech.token`` 时**导入，同名不覆盖。

    ⚠️ 必须**早于** ``cleanup_legacy_app_tree`` 执行（先迁移、后删除），否则旧目录被
    改名/删除后 token 再也取不到 —— 这是"用户被迫重新扫码"的根因（design §8.1 / R7）。

    返回本次导入的文件数（0 表示未迁移）。
    """
    dest = data_dir()
    if (dest / "cuktech.token").exists():
        return 0
    for src in _import_sources():
        if not (src / "cuktech.token").exists():
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError:
            return 0
        count = 0
        for name in _DATA_FILES:
            s = src / name
            d = dest / name
            try:
                if s.exists() and not d.exists():
                    shutil.copyfile(s, d)
                    count += 1
            except OSError as exc:
                _log("导入 %s 失败（忽略）：%s" % (name, exc))
        if count:
            _log("已导入 %d 个数据文件：%s" % (count, src))
        return count
    return 0


# ------------------------------------------------------------------ 旧残留清理

def _is_protected(path: Path) -> bool:
    """清理动作的硬保护：路径含"备份"、或等于根/数据目录 → 拒绝。"""
    text = str(path)
    if _BACKUP_MARK in text:
        return True
    try:
        resolved = path.resolve()
        if resolved == root_dir().resolve() or resolved == data_dir().resolve():
            return True
    except OSError:
        pass
    return False


def cleanup_legacy_app_tree(on_log=None) -> None:
    """删除旧 Node SEA 释放树 ``root/app``（含 ``1.0.0`` / ``0.0.1`` / ``*.old-<pid>``）。

    纪律：**只按精确路径**（恰好 ``root/app`` 这一层）、失败只记日志、绝不阻塞启动；
    ``data`` 与任何"备份"目录永不触碰。调用方应以异步方式执行。
    """
    say = on_log if callable(on_log) else _log
    apps = root_dir() / "app"
    if not apps.exists():
        return
    if _is_protected(apps):
        say("清理跳过（受保护路径）：" + str(apps))
        return
    try:
        shutil.rmtree(apps)
        say("已清理旧 Node SEA 释放树：" + str(apps))
    except OSError as exc:
        say("清理旧释放树失败（忽略）：%s — %s" % (apps, exc))


def cleanup_legacy_profile(on_log=None) -> bool:
    """删除旧隔离档案目录 ``%LOCALAPPDATA%\\CuktechMonitor\\browser``。

    只删这一个精确路径；父目录恰为空才一并 ``rmdir``（非空失败即忽略）。
    返回是否确实删除成功。
    """
    say = on_log if callable(on_log) else _log
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    target = Path(base) / "CuktechMonitor" / "browser"
    if not target.exists():
        return False
    if _is_protected(target):
        say("清理跳过（受保护路径）：" + str(target))
        return False
    say("发现旧隔离档案目录，正在清理：" + str(target))
    try:
        shutil.rmtree(target)
        say("旧隔离档案目录已清理：" + str(target))
        try:
            target.parent.rmdir()
        except OSError:
            pass  # 父目录非空属正常
        return True
    except OSError as exc:
        say("旧隔离档案目录清理失败（忽略，不影响启动）：%s" % exc)
        return False


__all__ = [
    "frozen",
    "meipass_dir",
    "exe_path",
    "root_dir",
    "resolve_root",
    "data_dir",
    "resource_path",
    "data_file",
    "apply_env_contract",
    "child_argv",
    "child_env",
    "ensure_runtime_assets",
    "maybe_import_data",
    "cleanup_legacy_app_tree",
    "cleanup_legacy_profile",
]
