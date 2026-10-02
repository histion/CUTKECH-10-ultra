"""布局零差异校验（对应使用说明 §7.3）。

把「改前 / 改后」两份前端的内联 CSS 解析成 (媒体上下文, 选择器) → 声明表，
只比对布局类属性：

    差异条数必须为 0        改动不允许影响任何元素的位置与尺寸
    缺失选择器数量必须为 0   原有规则一条都不能丢

只有颜色 / 背景 / 边框 / 圆角 / 阴影 / 字体 / 动效 / transform 允许变化。
"""
import re
import sys
from collections import OrderedDict

LAYOUT = {
    "display", "position", "top", "right", "bottom", "left", "inset", "z-index",
    "width", "min-width", "max-width", "height", "min-height", "max-height",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "gap", "row-gap", "column-gap", "grid-template-columns", "grid-template-rows",
    "grid-column", "grid-row", "flex", "flex-direction", "flex-wrap", "flex-basis",
    "flex-grow", "flex-shrink", "align-items", "align-self", "justify-content",
    "justify-self", "overflow", "overflow-x", "overflow-y", "aspect-ratio",
    "box-sizing", "float", "clear", "order", "white-space", "text-overflow",
}


def parse_css(css: str):
    """→ OrderedDict[(media_ctx, selector)] = {prop: value}"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = OrderedDict()
    i = 0
    ctx = ""

    def read_block(s, start):
        depth, j = 1, start
        while j < len(s) and depth:
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
            j += 1
        return s[start:j - 1], j

    n = len(css)
    while i < n:
        m = re.compile(r"[^{}]*").match(css, i)
        head = m.group(0).strip()
        i = m.end()
        if i >= n:
            break
        if head.startswith("@media"):
            inner, i = read_block(css, i + 1)
            sub = parse_css(inner)
            for (c, sel), decls in sub.items():
                out[(head + " " + c, sel)] = decls
            continue
        if head.startswith("@"):           # @keyframes / @font-face 等
            _, i = read_block(css, i + 1)
            continue
        body, i = read_block(css, i + 1)
        decls = OrderedDict()
        for part in body.split(";"):
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            k = k.strip().lower()
            if k:
                decls[k] = v.strip()
        for sel in head.split(","):
            sel = " ".join(sel.split())
            if sel:
                out[(ctx, sel)] = decls
    return out


def style_of(path: str) -> str:
    s = open(path, encoding="utf-8").read()
    return "\n".join(re.findall(r"<style>(.*?)</style>", s, re.S))


def main(old_path: str, new_path: str) -> int:
    a = parse_css(style_of(old_path))
    b = parse_css(style_of(new_path))

    missing = [k for k in a if k not in b]
    diffs = []
    for key, decls in a.items():
        other = b.get(key)
        if other is None:
            continue
        for prop, val in decls.items():
            if prop in LAYOUT and other.get(prop, "") != val:
                diffs.append("%s :: %s = %r → %r" % (key[1], prop, val, other.get(prop)))

    added = [k for k in b if k not in a]
    added_layout = []
    for key in added:
        for prop in b[key]:
            if prop in LAYOUT:
                added_layout.append("%s :: %s" % (key[1], prop))
                break

    print("旧规则数 %d，新规则数 %d" % (len(a), len(b)))
    print("缺失选择器（必须为 0）：%d" % len(missing))
    for k in missing[:10]:
        print("   - %s :: %s" % (k[0], k[1]))
    print("布局属性差异（必须为 0）：%d" % len(diffs))
    for d in diffs[:20]:
        print("   ! " + d)
    print("新增规则：%d 条，其中带布局属性的：" % len(added))
    for x in added_layout[:20]:
        print("   + " + x)
    ok = not missing and not diffs
    print("结论：" + ("布局零差异 ✔" if ok else "存在布局差异 ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
