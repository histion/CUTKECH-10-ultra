"""T01 自测：路径落点 / 资源安置幂等 / 数据迁移 / 旧残留清理（含"备份目录不被触碰"断言）。

离线运行（不碰真机、不碰真实数据）：
    python pyapp/tests/test_paths.py        # 或 runtime\\python\\python.exe ...
退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("OK  " + name)
    else:
        FAIL += 1
        print("BAD " + name + (("  → " + detail) if detail else ""))


def main() -> int:
    from pyapp import paths

    tmp = Path(tempfile.mkdtemp(prefix="cuk_home_"))
    os.environ["CUKTECH_HOME"] = str(tmp)
    os.environ.pop("CUKTECH_DATA_DIR", None)
    try:
        root = tmp.resolve()
        check("root_dir 落点 = CUKTECH_HOME", paths.root_dir() == root,
              "got %s" % paths.root_dir())
        check("data_dir 落点 = <home>/data", paths.data_dir() == root / "data",
              "got %s" % paths.data_dir())
        check("resource_path 命中项目 app.ico",
              paths.resource_path("app.ico").exists(),
              str(paths.resource_path("app.ico")))

        # --- 资源安置：首写 copied，二调 kept（sha 幂等）---
        first = paths.ensure_runtime_assets()
        check("ensure_runtime_assets 首调复制 app.ico", "app.ico" in first["copied"], str(first))
        check("ensure_runtime_assets 首调复制 tray.ps1", "tray.ps1" in first["copied"], str(first))
        check("稳定副本已落根目录", (root / "app.ico").exists() and (root / "tray.ps1").exists())
        second = paths.ensure_runtime_assets()
        check("ensure_runtime_assets 二调不重写（copied 为空）", second["copied"] == [], str(second))
        check("ensure_runtime_assets 二调 sha 命中（kept 两项）",
              set(second["kept"]) == {"app.ico", "tray.ps1"}, str(second))

        # --- 数据迁移：构造 app/1.0.0/data，导入且不覆盖已存在文件 ---
        src_data = root / "app" / "1.0.0" / "data"
        src_data.mkdir(parents=True, exist_ok=True)
        (src_data / "cuktech.token").write_text("TOKEN_FROM_OLD", encoding="utf-8")
        (src_data / "config.json").write_text('{"interval": 2.5}', encoding="utf-8")
        (src_data / "history.jsonl").write_text('{"at": 1}\n', encoding="utf-8")
        dest = root / "data"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "history.jsonl").write_text("KEEP_ME", encoding="utf-8")  # 预置，不应被覆盖

        imported = paths.maybe_import_data()
        check("maybe_import_data 导入 2 个文件（token+config）", imported == 2, "got %s" % imported)
        check("已导入 cuktech.token", (dest / "cuktech.token").read_text(encoding="utf-8") == "TOKEN_FROM_OLD")
        check("已导入 config.json", (dest / "config.json").read_text(encoding="utf-8") == '{"interval": 2.5}')
        check("已存在文件不被覆盖（history.jsonl 保持 KEEP_ME）",
              (dest / "history.jsonl").read_text(encoding="utf-8") == "KEEP_ME")

        # 二次调用：目标已有 token → 不再导入
        check("已有 token 时 maybe_import_data 返回 0", paths.maybe_import_data() == 0)

        # --- 备份保护：制造"备份"目录诱饵 + 指向真实基线 ---
        decoy_a = root / "1.0.0备份"
        decoy_b = root / "0.0.01备份"
        decoy_a.mkdir()
        decoy_b.mkdir()
        (decoy_a / "keep.txt").write_text("A", encoding="utf-8")
        (decoy_b / "keep.txt").write_text("B", encoding="utf-8")
        real_a = ROOT / "1.0.0备份"
        real_b = ROOT / "0.0.01备份"

        check("_is_protected 拒绝含“备份”的路径", paths._is_protected(real_a) is True)

        # --- 旧 app 树清理：只删 app，data/备份/诱饵/真实基线全部保留 ---
        paths.cleanup_legacy_app_tree()
        check("旧 app 树被清理", not (root / "app").exists())
        check("data 目录不受影响", (dest / "cuktech.token").exists())
        check("诱饵备份 1.0.0备份 未被触碰",
              (decoy_a / "keep.txt").read_text(encoding="utf-8") == "A")
        check("诱饵备份 0.0.01备份 未被触碰",
              (decoy_b / "keep.txt").read_text(encoding="utf-8") == "B")
        check("真实基线 1.0.0备份 未被触碰", real_a.exists())
        check("真实基线 0.0.01备份 未被触碰", real_b.exists())

        # --- 旧隔离档案清理（独立 LOCALAPPDATA）---
        lap = Path(tempfile.mkdtemp(prefix="cuk_lap_"))
        os.environ["LOCALAPPDATA"] = str(lap)
        prof = lap / "CuktechMonitor" / "browser"
        prof.mkdir(parents=True)
        (prof / "x").write_text("x", encoding="utf-8")
        removed = paths.cleanup_legacy_profile()
        check("旧隔离档案目录被清理", removed is True and not prof.exists())

        # --- 环境契约 ---
        paths.apply_env_contract()
        check("apply_env_contract 注入 CUKTECH_DATA_DIR",
              os.environ.get("CUKTECH_DATA_DIR") == str(paths.data_dir()))
        check("apply_env_contract 注入 CUKTECH_EXE", bool(os.environ.get("CUKTECH_EXE")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 60)
    print("T01 汇总：通过 %d，失败 %d" % (PASS, FAIL))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
