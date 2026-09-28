"""创建桌面快捷方式（等价重写 ``server.js`` 的 ``createShortcut``）。

用 PowerShell ``-EncodedCommand``（Base64/UTF-16LE）彻底规避中文路径的编码问题，
只靠 ASCII 标记 ``RESULT_OK`` 判定成功。

⚠️ 图标指向**稳定根目录**的 ``app.ico`` 副本（``ensure_runtime_assets`` 落的），
``TargetPath`` 指向 ``sys.executable``（= exe 自身，路径稳定）——绝不能指向会被删除的
``_MEIPASS``（否则快捷方式图标失效，risk R6）。
"""

from __future__ import annotations

import base64
import os
import subprocess

from . import paths
from .collector_bridge import no_window_kwargs


def _ps_quote(s) -> str:
    return str(s).replace("'", "''")


def create_shortcut(on_log=None) -> dict:
    """在桌面创建 ``酷态科10号Ultra.lnk``。返回 ``{ok, raw}`` 或 ``{ok:false, error}``。"""
    say = on_log if callable(on_log) else (lambda _m: None)

    exe = os.environ.get("CUKTECH_EXE") or str(paths.exe_path())
    if not exe or not os.path.exists(exe):
        exe = ""     # exe 不存在（被删）→ 退回 bat 逻辑
    bat = str(paths.meipass_dir() / "启动酷态科10号Ultra.bat")
    target = exe or bat
    workdir = str(paths.exe_path().parent) if exe else str(paths.meipass_dir())
    ico = str(paths.root_dir() / "app.ico")

    ps = "; ".join([
        "$w = New-Object -ComObject WScript.Shell",
        "$d = [Environment]::GetFolderPath('Desktop')",
        "$lnk = Join-Path $d '酷态科10号Ultra.lnk'",
        "$s = $w.CreateShortcut($lnk)",
        "$s.TargetPath = '" + _ps_quote(target) + "'",
        "$s.WorkingDirectory = '" + _ps_quote(workdir) + "'",
        "$s.IconLocation = '" + _ps_quote(ico + ",0") + "'",
        "$s.Description = '酷态科10号Ultra（AD1204U）充电监视器'",
        "$s.WindowStyle = 7",
        "$s.Save()",
        "if (Test-Path $lnk) { 'RESULT_OK' } else { 'RESULT_FAIL' }",
    ])
    b64 = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")

    try:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", b64],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            **no_window_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    out, _err = proc.communicate()
    text = out.decode("utf-8", "replace")
    ok = "RESULT_OK" in text
    say("已创建桌面快捷方式" if ok else "创建桌面快捷方式失败（COM 未返回标记）")
    return {"ok": ok, "raw": text.strip()[:200]}


__all__ = ["create_shortcut"]
