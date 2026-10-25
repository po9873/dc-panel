"""DC Panel v1.2.3 改动集成测试：
1. 面板自身版本自检：后台循环按 UI 设置的间隔检查（非硬编码）、结果缓存
2. 发现新版本只通知一次（同版本不重复推送）
3. /api/version/check 返回公开字段
4. 设置接口接受/钳制 self_check_interval、self_update_notify
5. 前端：定时轮询 + 设置控件 + 版本号
"""
import os
import re
import sys
import json
import shutil
import asyncio
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "_testdata"
shutil.rmtree(TESTDATA, ignore_errors=True)  # 清理上次残留，保证默认值断言准确
TESTDATA.mkdir(exist_ok=True)
os.environ["PANEL_DATA"] = str(TESTDATA)
os.environ["PANEL_PASSWORD"] = "testpass123"

APP_DIR = HERE.parent / "app"
sys.path.insert(0, str(APP_DIR))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))


print("=== 1. 配置默认值与钳制 ===")
cfg = main._automation_config()
check("默认 self_check_interval=5", cfg.get("self_check_interval") == 5, str(cfg.get("self_check_interval")))
check("默认 self_update_notify=True", cfg.get("self_update_notify") is True)

data = main._config()
data.setdefault("automation", {})["self_check_interval"] = 0
main._save_config(data)
cfg = main._automation_config()
check("间隔 0 被钳制为 1", cfg.get("self_check_interval") == 1)

data = main._config()
data["automation"]["self_check_interval"] = -7
main._save_config(data)
cfg = main._automation_config()
check("间隔负数被钳制为 1", cfg.get("self_check_interval") == 1)

data = main._config()
data["automation"].pop("self_check_interval", None)
data["automation"]["self_update_notify"] = False
main._save_config(data)
cfg = main._automation_config()
check("删掉间隔键回退默认 5", cfg.get("self_check_interval") == 5)
check("通知开关可关", cfg.get("self_update_notify") is False)

print("=== 2. _check_panel_update 缓存与通知（mock Docker Hub） ===")


class FakeResp:
    status_code = 200

    def json(self):
        return {"last_updated": "2999-01-01T00:00:00Z"}


class FakeClient:
    calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        FakeClient.calls += 1
        return FakeResp()


main.httpx.AsyncClient = FakeClient
notify_calls = []


async def fake_notify(record):
    notify_calls.append(record)


main.notify_update = fake_notify


async def test_check():
    main.PANEL_UPDATE_STATE.update({"update": False, "hub_time": None,
                                    "checked_at": 0.0, "notified_hub_time": None})
    FakeClient.calls = 0
    notify_calls.clear()

    st = await main._check_panel_update()
    check("首次检查真发请求", FakeClient.calls == 1)
    check("发现新版本 update=True", st["update"] is True)
    check("hub_time 已记录", st["hub_time"] == "2999-01-01T00:00:00Z")
    check("未传 notify 不推送", len(notify_calls) == 0)

    st2 = await main._check_panel_update()
    check("60 秒内命中缓存不再请求", FakeClient.calls == 1)

    main.PANEL_UPDATE_STATE["checked_at"] -= 120
    await main._check_panel_update()
    check("缓存过期后重新请求", FakeClient.calls == 2)

    notify_calls.clear()
    main.PANEL_UPDATE_STATE.update({"notified_hub_time": None})
    await main._check_panel_update(notify=True)
    check("notify=True 且是新版本 -> 推送一次", len(notify_calls) == 1)
    if notify_calls:
        r = notify_calls[0]
        check("通知记录字段完整",
              r.get("container") == "dc-panel" and r.get("status") == "可更新"
              and r.get("source") == "面板自检" and "2999" in r.get("message", ""))
    await main._check_panel_update(notify=True)
    check("同版本不重复推送", len(notify_calls) == 1)

    # 更新场景：Hub 时间早于启动时间 -> update=False
    class OldResp(FakeResp):
        def json(self):
            return {"last_updated": "2000-01-01T00:00:00Z"}
    OldClient = type("C", (FakeClient,), {"calls": 0})

    async def old_get(self, url):
        OldClient.calls += 1
        return OldResp()
    OldClient.get = old_get
    main.httpx.AsyncClient = OldClient
    main.PANEL_UPDATE_STATE.update({"notified_hub_time": None, "checked_at": 0.0})
    st3 = await main._check_panel_update(notify=True)
    check("Hub 无更新时 update=False", st3["update"] is False)
    check("无更新不推送", len(notify_calls) == 1)


asyncio.run(test_check())

print("=== 3. /api/version/check 公开字段 ===")
main.httpx.AsyncClient = FakeClient
main.PANEL_UPDATE_STATE["checked_at"] = 0.0
client = TestClient(main.app)
with client:
    r = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
    check("登录成功", r.status_code == 200, f"HTTP {r.status_code}")
    r = client.get("/api/version/check")
    d = r.json()
    check("接口 200", r.status_code == 200)
    check("含 current/repo/update/hub_time",
          {"current", "repo", "update", "hub_time"} <= set(d))
    check("不泄露内部字段", "checked_at" not in d and "notified_hub_time" not in d)
    check("current = APP_VERSION = v1.2.3", d.get("current") == "v1.2.3", str(d.get("current")))

    print("=== 4. 设置接口读写 self_check_interval / self_update_notify ===")
    r = client.put("/api/automation", json={
        "config": {
            "enabled": True,
            "self_check_interval": 3,
            "self_update_notify": False,
        },
        "devices": {},
    })
    check("PUT 200", r.status_code == 200, r.text[:120])
    r = client.get("/api/automation")
    auto = r.json().get("config", {})
    check("读回 self_check_interval=3", auto.get("self_check_interval") == 3)
    check("读回 self_update_notify=False", auto.get("self_update_notify") is False)

    r = client.put("/api/automation", json={"config": {"self_check_interval": 0, "self_update_notify": True}, "devices": {}})
    r2 = client.get("/api/automation")
    check("PUT 间隔 0 被钳制为 1", r2.json()["config"].get("self_check_interval") == 1)

print("=== 5. 前端断言 ===")
HTML = (HERE.parent / "static" / "index.html").read_text(encoding="utf-8")
checks = {
    "定时轮询 setInterval(checkPanelUpdate,60000)": "setInterval(checkPanelUpdate,60000)" in HTML,
    "update=false 时移除徽章并恢复版本号": "el.querySelector('.upd-badge')" in HTML and "d.current||''" in HTML,
    "设置项 auto-self-interval": 'id="auto-self-interval"' in HTML,
    "设置项 auto-self-notify": 'id="auto-self-notify"' in HTML,
    "collectAutomation 收 self_check_interval": "self_check_interval:Number($('auto-self-interval')" in HTML,
    "collectAutomation 收 self_update_notify": "self_update_notify:$('auto-self-notify')" in HTML,
    "版本 v1.2.3 (verInfo)": '<span id="verInfo">v1.2.3</span>' in HTML,
    "版本 v1.2.3 (umVer)": 'id="umVer">v1.2.3</span>' in HTML,
    "无残留 v1.2.2": "v1.2.2" not in HTML,
    "安全区修复无回归": "calc(10px + env(safe-area-inset-top))" in HTML,
    "清理按钮无回归": "clearHist('older30')" in HTML and "delHist(" in HTML,
    "版本自愈无回归": "function dcpWatchVersion" in HTML,
}
for k, v in checks.items():
    check(k, v)

from html.parser import HTMLParser  # noqa: E402


class Checker(HTMLParser):
    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "area", "base", "col", "embed", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

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
real = [e for e in c.errors if not any(x in e for x in ("li>", "td>", "tr>", "p>", "option"))]
leftover = [t for t, _ in c.stack if t not in ("html", "body")]
check("HTML 标签配对", not real and not leftover,
      "; ".join(real[:3] + [f"残留:{leftover[:3]}"]))

print()
print(f"===== 结果: {len(PASS)} 通过, {len(FAIL)} 失败 =====")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1)
