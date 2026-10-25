"""DC Panel v1.2.2 PWA 旧 UI 问题验证。

断言：
1. sw.js notificationclick：focus 已有窗口前先 postMessage dcp-reload；
   openWindow 带 cache-bust 参数；
2. index.html 版本自愈：dcpCheckVersion/dcpWatchVersion 存在，
   showMain 调用 dcpWatchVersion，监听 visibilitychange/pageshow/sw message；
3. main.py：/api/version/check 返回 current=APP_VERSION；
4. 版本三处一致 v1.2.2，无残留旧版本号；
5. v1.2.1 手机安全区修复无回归；
6. HTML 标签配对完整。
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "dc-panel"
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
SW = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))


# 1. sw.js
check("sw.js: focus 前 postMessage dcp-reload",
      'c.postMessage({ type: "dcp-reload"' in SW.replace("'", '"').replace('"', '"')
      or "dcp-reload" in SW and SW.find("postMessage") < SW.find("return c.focus()"),
      "notificationclick 未通知页面刷新")
check("sw.js: openWindow 带 cache-bust", "Date.now()" in SW and "clients.openWindow" in SW)

# 2. index.html 版本自愈
check("index.html: dcpCheckVersion 存在", "async function dcpCheckVersion" in HTML)
check("index.html: dcpWatchVersion 存在", "function dcpWatchVersion" in HTML)
check("index.html: showMain 调用 dcpWatchVersion",
      re.search(r"function showMain\([^)]*\)\{[^}]*dcpWatchVersion\(\)", HTML) is not None)
check("index.html: visibilitychange 监听", "visibilitychange" in HTML)
check("index.html: pageshow(bfcache) 监听", "e.persisted" in HTML)
_seg = HTML[HTML.find("function dcpWatchVersion"):]
check("index.html: sw message 监听 dcp-reload",
      "addEventListener('message'" in _seg and "dcp-reload" in _seg)
check("index.html: 版本变化触发 reload", "location.reload()" in HTML)
check("index.html: 版本写入 localStorage", "localStorage.getItem('dcp-ver')" in HTML)

# 3. main.py 版本接口
check("main.py: /api/version/check 路由", '@app.get("/api/version/check")' in MAIN)
check("main.py: 返回 current=APP_VERSION", '"current": APP_VERSION' in MAIN)

# 4. 版本一致
check("main.py APP_VERSION = v1.2.2", 'APP_VERSION = "v1.2.2"' in MAIN)
check("verInfo = v1.2.2", '<span id="verInfo">v1.2.2</span>' in HTML)
check("umVer = v1.2.2", 'id="umVer">v1.2.2</span>' in HTML)
check("无残留 v1.2.1/v1.2.0", "v1.2.1" not in HTML and "v1.2.0" not in HTML
      and 'APP_VERSION = "v1.2.1"' not in MAIN and 'APP_VERSION = "v1.2.0"' not in MAIN)

# 5. v1.2.1 安全区无回归
block = re.search(r"@media\(max-width:820px\)\{(.*?)\n\}", HTML, re.S)
check("820px 断点存在", bool(block))
if block:
    css = block.group(1)
    bad = []
    for body in re.findall(r"\.wrap\{([^}]*)\}", css):
        pt = re.search(r"padding(?:-top)?:\s*([^;}]+)", body)
        if pt and "safe-area-inset-top" not in pt.group(1):
            bad.append(pt.group(1).strip())
    check("安全区修复无回归（所有 .wrap padding-top 带 safe-area）", not bad, f"违规: {bad}")
check("viewport-fit=cover 保留", "viewport-fit=cover" in HTML)

# 6. HTML 标签配对
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
