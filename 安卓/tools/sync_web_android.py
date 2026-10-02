"""把 web/index.html 同步成安卓 assets/index.html。

桌面版与安卓版**共用同一份前端**，差异只有 4 处（都是有意为之）：
  1) 追加手机媒体查询（单列/两列、触控目标放大、隐藏桌面专属按钮）
  2) 曲线支持触摸读数（手机没有 hover）
  3) 登录弹窗里 token 路径文案
  4) 「任务栏功率图标」→「后台常驻通知」
另外把官方图标子集（harmony_icons.js）注入两端的 HARMONY_ICONS 占位符。

用法（改完 web/index.html 后跑一次，两端就不会漂移）：

    python 安卓/tools/sync_web_android.py
"""
import json
import os
import re
import shutil
import sys

# 本脚本在 <仓库根>/安卓/tools/ 下，往上三级才是仓库根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "web", "index.html")
AND = os.path.join(ROOT, "安卓", "app", "src", "main", "assets", "index.html")
ICONS = os.path.join(ROOT, "安卓", "tools", "harmony_icons.js")

MOBILE_CSS = """/* ================= 手机排版（安卓 WebView） =================
   桌面版是按 1100px+ 的窗口设计的，手机上做三件事：
   ① 根字号改为随屏宽线性放大（rem 体系跟着一起变）；
   ② 所有多列网格一律压成单列，四口卡片两列；
   ③ 控件触控目标放大到 44px 级，图表/日志高度收窄，弹窗按屏宽自适应。 */
html{-webkit-text-size-adjust:100%}
body{overscroll-behavior:none;touch-action:manipulation}
@media (max-width:820px){
  html{font-size:clamp(14.5px, calc(1.6vw + 9px), 18px)}
  .app{padding:0.86rem 0.71rem calc(1.71rem + env(safe-area-inset-bottom))}
  header{gap:0.57rem;margin-bottom:0.86rem}
  .brand{gap:0.57rem}
  .brand .mark{width:2.29rem;height:2.29rem;flex:0 0 2.29rem;border-radius:0.64rem}
  .brand .mark svg{width:1.43rem;height:1.43rem}
  .brand h1{font-size:1.07rem}
  .brand .sub{font-size:0.79rem}
  .pill{padding:0.36rem 0.71rem;font-size:0.82rem}
  button{padding:0.5rem 0.86rem;border-radius:0.64rem}
  .grid{gap:0.79rem}
  .row1,.row2,.row3{grid-template-columns:1fr!important}
  .ports{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:0.71rem;margin:0.79rem 0}
  .card{padding:0.93rem 1rem;border-radius:1rem}
  .ctl-grid{grid-template-columns:1fr!important;gap:0.57rem}
  .ctl-grid .sw-row{min-height:2.86rem}
  .switch{width:3.14rem;height:1.71rem;flex:0 0 3.14rem}
  .switch::after{width:1.29rem;height:1.29rem}
  .switch.on::after{left:calc(100% - 1.29rem - 0.14rem)}
  .gauge-wrap svg{max-width:15.71rem}
  .gauge-val{font-size:2.86rem}
  .chart-box{height:13.57rem}
  .log{height:9.29rem}
  .stat-row{grid-template-columns:1fr;gap:0.57rem}
  .stat .v{font-size:1.43rem}
  .ebars{height:7.14rem}
  .ebars .lbl,.ebars .val{font-size:0.71rem}
  table{font-size:0.79rem}
  th,td{padding:0.36rem 0.43rem}
  .scroll{max-height:15.71rem!important}
  select{max-width:none;width:8.57rem}
  input[type=number],input[type=text]{width:6.43rem}
  .modal{width:min(40rem,94vw);padding:1.29rem}
  .qrbox{width:min(16.86rem,72vw);height:min(16.86rem,72vw);padding:0.64rem}
  .qrbox img{width:100%;height:100%}
  /* 安卓没有任务栏托盘/桌面快捷方式：通知栏常驻即对应物 */
  #btn-shortcut,#in-traymode{display:none!important}
}
@media (max-width:400px){
  .ports{grid-template-columns:1fr!important}
}
"""

TIP_OLD = """// 悬停提示
(function bindTip() {
  const cv = $('#chart'), tip = $('#tip');
  cv.addEventListener('mousemove', (e) => {"""
TIP_NEW = """// 悬停 / 触摸提示（手机上没有 hover，所以触摸同样弹出读数）
(function bindTip() {
  const cv = $('#chart'), tip = $('#tip');
  const showTip = (clientX, clientY, lift) => {"""
TIP_OLD_TAIL = """    tip.style.top = Math.max(4, e.clientY - r.top - 46 * K) + 'px';
  });
  cv.addEventListener('mouseleave', () => { tip.style.opacity = '0'; });
})();"""
TIP_NEW_TAIL = """    tip.style.top = Math.max(4, clientY - r.top - lift * K) + 'px';
  };
  cv.addEventListener('mousemove', (e) => showTip(e.clientX, e.clientY, 46));
  cv.addEventListener('mouseleave', () => { tip.style.opacity = '0'; });
  const onTouch = (e) => {
    const t = e.touches && e.touches[0];
    if (t) showTip(t.clientX, t.clientY, 66);      // 手指会挡住下方，提示往上抬
  };
  cv.addEventListener('touchstart', onTouch, { passive: true });
  cv.addEventListener('touchmove', onTouch, { passive: true });
  cv.addEventListener('touchend', () => { setTimeout(() => { tip.style.opacity = '0'; }, 1800); });
  cv.addEventListener('touchcancel', () => { tip.style.opacity = '0'; });
})();"""

TOKEN_OLD = """    <p>用手机上的<b>米家 App</b> 扫描下面的二维码并确认授权。只需一次，之后本应用会把 12 字节的 BLE 密钥保存在本地
      <code>data\\cuktech.token</code>，不会再联网。</p>"""
TOKEN_NEW = """    <p>用手机上的<b>米家 App</b> 扫描下面的二维码并确认授权。只需一次，之后本应用会把 12 字节的 BLE 密钥保存在应用私有目录
      <code>cuktech.token</code>，不会再联网。</p>"""

TRAY_OLD = """          <label class="sw-row">任务栏功率图标"""
TRAY_NEW = """          <label class="sw-row">后台常驻通知"""


def load_icons() -> str:
    s = open(ICONS, encoding="utf-8").read().strip()
    assert s.startswith("const HARMONY_ICONS="), "图标文件格式不对（应为 const HARMONY_ICONS=...）"
    return s[len("const HARMONY_ICONS="):].rstrip().rstrip(";")


def inject_icons(html: str, icons_json: str, path: str) -> str:
    if "/*__HARMONY_ICONS__*/null" not in html:
        print("  [info] %s 无需注入图标（已内联或占位符不存在）" % path)
        return html
    return html.replace("/*__HARMONY_ICONS__*/null", icons_json)


def main() -> int:
    if not os.path.exists(WEB):
        print("找不到 web/index.html：" + WEB)
        return 2
    web = open(WEB, encoding="utf-8").read()
    icons_json = load_icons()

    web2 = inject_icons(web, icons_json, "web/index.html")
    if web2 != web:
        open(WEB, "w", encoding="utf-8", newline="").write(web2)
        print("web/index.html 已注入图标数据")
    web = web2

    and_html = inject_icons(web, icons_json, "assets/index.html")
    assert "</style>" in and_html
    and_html = and_html.replace("</style>", MOBILE_CSS + "</style>", 1)

    for old, new, label in ((TIP_OLD, TIP_NEW, "bindTip 头"),
                            (TIP_OLD_TAIL, TIP_NEW_TAIL, "bindTip 尾"),
                            (TOKEN_OLD, TOKEN_NEW, "token 文案"),
                            (TRAY_OLD, TRAY_NEW, "通知文案")):
        if old not in and_html:
            print("  [warn] 安卓补丁未命中：%s" % label)
        and_html = and_html.replace(old, new, 1)

    open(AND, "w", encoding="utf-8", newline="").write(and_html)
    print("assets/index.html 已重新生成：%d 字节" % len(and_html.encode("utf-8")))
    # 顺手把图标数据也放一份在 tools/ 下，方便下次重新内联
    shutil.copy(ICONS, os.path.join(ROOT, "安卓", "tools", "harmony_icons.js")) \
        if os.path.abspath(ICONS) != os.path.abspath(os.path.join(ROOT, "安卓", "tools", "harmony_icons.js")) \
        else None
    return 0


if __name__ == "__main__":
    sys.exit(main())
