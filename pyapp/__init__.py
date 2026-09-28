"""酷态科10号Ultra —— Python 服务端（替代 server.js 的运行时角色）。

本包是「去 Node 化」后的新服务端内核：路径层 / 状态与账本 / 设备链路与登录代理
（T01–T03），后续 HTTP 服务与打包链路（T04–T05）在此基础上叠加。

设计对齐原则：
  * 对外 JSON 字段结构、前端 ``web/index.html`` 一行不改 → 所有语义以 ``server.js``
    现状为准（见 ``docs/system_design.md`` §6）。
  * 旧 Node 链路（``server.js`` / ``launcher.js`` / ``autoA.js`` / ``src/sea-main.js``）
    保持随时可跑，本包只**新增**、**不修改**任何旧文件。

为什么把常量放在包根：``VERSION`` 与 ``APP_INFO`` 在服务端多处（``/api/ping``、
``/api/state``）出现，集中在包根定义可避免"两处各写一个版本号"的漂移。
"""

from __future__ import annotations

import os

# 服务 / 协议版本。**刻意与产品版本分离**：``server.js`` 里 ``VERSION='1.3.0'``
# 仅出现在 ``/api/ping.v`` 与 ``/api/state.version``，不上界面；产品版本
# ``app.version`` 仍为 ``1.0.0``，保证前端"一行不改"。（详见 system_design §12.1 / R12）
VERSION = "1.3.0"

# 产品信息默认值，与 exe 版本资源、界面显示保持一致（build 脚本亦读同一套字面量）。
PRODUCT = {
    "name": "cuktech 10 ultra",
    "version": "1.0.0",
    "vendor": "Histion",
    "id": "cuktech10ultra",
}

# 实际对外暴露的产品信息：单文件 exe 版由引导脚本注入环境变量，便携/开发期用默认值。
# 与 ``server.js`` 的 ``APP_INFO`` 语义逐字段一致（name/version/vendor 三键，顺序不变）。
APP_INFO = {
    "name": os.environ.get("CUKTECH_APP_NAME") or PRODUCT["name"],
    "version": os.environ.get("CUKTECH_APP_VERSION") or PRODUCT["version"],
    "vendor": os.environ.get("CUKTECH_APP_VENDOR") or PRODUCT["vendor"],
}

# MAC 地址正则（与 ``server.js`` 的 ``MAC_RE`` 等价）：登录自动发现、配置校验共用。
MAC_RE_TEXT = r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"

__all__ = ["VERSION", "PRODUCT", "APP_INFO", "MAC_RE_TEXT"]
