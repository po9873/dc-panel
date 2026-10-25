"""DC Panel v1.2.1 手机安全区修复验证。

断言：
1. viewport 含 viewport-fit=cover（页面延伸到刘海下，必须靠 padding 避让）；
2. 820px 断点内所有 .wrap 规则的 padding-top 都带 env(safe-area-inset-top)，
   且不存在把 padding-top 重置为裸值的规则（v1.2.0 回归根因）；
3. #login 顶部margin 同样避让安全区；
4. 版本号三处一致为 v1.2.1（main.py / verInfo / umVer）；
5. HTML 标签配对完整。
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "dc-panel"
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))


# 1. viewport-fit=cover
m = re.search(r'<meta name="viewport" content="([^"]+)"', HTML)
check("viewport 含 viewport-fit=cover", bool(m and "viewport-fit=cover" in m.group(1)),
      m.group(1) if m else "未找到 viewport meta")

# 2. 提取 820px 媒体查询块，逐条检查 .wrap 规则
block = re.search(r"@media\(max-width:820px\)\{(.*?)\n\}", HTML, re.S)
check("找到 820px 断点块", bool(block))
if block:
    css = block.group(1)
    wrap_rules = re.findall(r"\.wrap\{([^}]*)\}", css)
    check("断点内存在 .wrap 规则", len(wrap_rules) >= 2, f"{len(wrap_rules)} 条")
    bad = []
    for i, body in enumerate(wrap_rules):
        # 每条规则要么不涉及 padding-top，要么必须带 safe-area-inset-top
        pt = re.search(r"padding(?:-top)?:\s*([^;}]+)", body)
        if pt and "safe-area-inset-top" not in pt.group(1):
            bad.append((i, pt.group(1).strip()))
    check("所有 .wrap 的 padding-top 均避让安全区", not bad, f"违规: {bad}")
    check("calc(10px + env(safe-area-inset-top)) 存在",
          "calc(10px + env(safe-area-inset-top))" in css)
    check("底部安全区保留", "env(safe-area-inset-bottom)" in css)

# 3. #login margin（检查 820px 断点内的移动端规则，基础规则桌面生效无需安全区）
lm = None
if block:
    lm = re.search(r"#login\{([^}]*)\}", block.group(1))
check("#login 移动端 margin 避让安全区",
      bool(lm and "safe-area-inset-top" in lm.group(1)),
      (lm.group(1)[:80] if lm else "820px 块内未找到 #login 规则"))

# 4. 版本一致
check("main.py APP_VERSION = v1.2.1", 'APP_VERSION = "v1.2.1"' in MAIN)
check("verInfo = v1.2.1", '<span id="verInfo">v1.2.1</span>' in HTML)
check("umVer = v1.2.1", 'id="umVer">v1.2.1</span>' in HTML)
check("无残留 v1.2.0", "v1.2.0" not in HTML and 'APP_VERSION = "v1.2.0"' not in MAIN)

# 5. HTML 标签配对
class Checker(HTMLParser):
    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "area", "base", "col", "embed", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack:
            self.errors.append(f"多余 </{tag}> @ {self.getpos()}")
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
        else:
            names = [t for t, _ in self.stack]
            if tag in names:
                while self.stack and self.stack[-1][0] != tag:
                    self.errors.append(f"未闭合 <{self.stack[-1][0]}> @ {self.stack[-1][1]}")
                    self.stack.pop()
                self.stack.pop()
            else:
                self.errors.append(f"找不到匹配 </{tag}> @ {self.getpos()}")


c = Checker()
c.feed(HTML)
real = [e for e in c.errors if not any(f"{t}>" in e for t in ("li", "td", "tr", "p", "option"))]
leftover = [t for t, _ in c.stack if t not in ("html", "body")]
check("HTML 标签配对完整", not real and not leftover, f"{real[:3]} {leftover}")

print("=" * 60)
fails = 0
for name, ok, detail in results:
    print(("  [OK] " if ok else "  [!!] ") + name + (f"  ({detail})" if detail and not ok else ""))
    fails += 0 if ok else 1
print("=" * 60)
print(f"{len(results) - fails}/{len(results)} 通过" + ("" if not fails else f"，{fails} 项失败"))
sys.exit(1 if fails else 0)
