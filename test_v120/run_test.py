"""DC Panel v1.2.0 改动集成测试：
1. taskID 未找到不再误报失败（回查容器状态确认）
2. 更新记录清理 API（单条 / 按范围）
3. 手动删除后后台轮询不会把记录写回
"""
import os
import sys
import json
import asyncio
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTDATA = HERE / "_testdata"
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


def reset_history():
    main._save(main.HISTORY_FILE, [])


def seed_history():
    recs = [
        {"id": "aaa111", "time": "2026-10-25 14:13:30", "device": "J4125飞牛", "container": "dockercopilot", "status": "失败", "message": "taskID 未找到"},
        {"id": "bbb222", "time": "2026-10-20 09:00:00", "device": "群晖SA6400", "container": "qbittorrent", "status": "成功", "message": "容器更新成功"},
        {"id": "ccc333", "time": "2026-09-01 09:00:00", "device": "38宿主", "container": "mosdns", "status": "成功", "message": "容器更新成功"},  # 超过30天
    ]
    main._save(main.HISTORY_FILE, recs)
    return recs


print("=== 1. taskID 未找到误报修复（mock DC 行为） ===")


async def test_taskid_lost():
    device = {"id": "d1", "name": "J4125飞牛", "url": "http://127.0.0.1:1", "secretKey": "x"}
    # DC 自更新重启后：/api/progress 返回 400 taskID 未找到（getprogresslogic.go 实锤行为）
    async def fake_dc_request(client, dev, method, path, **kw):
        payload = '{"code":400,"msg":"taskID 未找到","data":{}}'

        class R:
            content = payload.encode("utf-8")

            def json(self):
                return json.loads(self.content)
        return R()

    orig_request, orig_get = main.dc_request, main.dc_get
    main.dc_request = fake_dc_request

    # 场景 A：容器已无 haveUpdate → 更新实际完成 → 应标记成功
    async def fake_get_done(client, dev, path, **kw):
        return [{"name": "dockercopilot", "haveUpdate": False}]
    main.dc_get = fake_get_done
    rec = {"id": "t1", "status": "进行中", "container": "dockercopilot", "device": "J4125飞牛"}
    await main._poll_progress(device, "fake-task", rec, {"interval": 1, "interval_limit": 2})
    check("场景A 更新已完成→标记成功（不再误报失败）", rec["status"] == "成功", f"status={rec['status']} msg={rec.get('message')}")

    # 场景 B：容器仍 haveUpdate → 真没更新 → 失败
    async def fake_get_still(client, dev, path, **kw):
        return [{"name": "dockercopilot", "haveUpdate": True}]
    main.dc_get = fake_get_still
    rec = {"id": "t2", "status": "进行中", "container": "dockercopilot", "device": "J4125飞牛"}
    await main._poll_progress(device, "fake-task", rec, {"interval": 1, "interval_limit": 2})
    check("场景B 仍可更新→标记失败", rec["status"] == "失败", f"status={rec['status']}")

    # 场景 C：DC 重启中查不到列表 → 不判失败，走"未确认结果"
    async def fake_get_down(client, dev, path, **kw):
        raise ConnectionError("dc restarting")
    main.dc_get = fake_get_down
    rec = {"id": "t3", "status": "进行中", "container": "dockercopilot", "device": "J4125飞牛"}
    await main._poll_progress(device, "fake-task", rec, {"interval": 1, "interval_limit": 2})
    check("场景C DC重启中→不误报失败", rec["status"] == "已完成（未确认结果）", f"status={rec['status']}")

    main.dc_request, main.dc_get = orig_request, orig_get


asrelated = asyncio.run(test_taskid_lost())

print("=== 2. 更新记录清理 API ===")
with TestClient(main.app) as client:
    r = client.post("/api/login", json={"username": "admin", "password": "testpass123"})
    check("登录成功", r.status_code == 200 and r.json().get("ok"))
    seed_history()

    r = client.get("/api/history?days=30")
    ids = [x["id"] for x in r.json()["records"]]
    # days=30 只返回 9/26 起的记录，ccc333(9/1) 按设计被过滤
    check("GET /api/history 返回近期记录", set(ids) == {"aaa111", "bbb222"}, str(ids))

    r = client.delete("/api/history/aaa111")
    check("单条删除 ok", r.status_code == 200 and r.json().get("deleted") == 1)
    r = client.get("/api/history?days=30")
    ids = [x["id"] for x in r.json()["records"]]
    check("单条删除后列表不含该记录", "aaa111" not in ids and ids == ["bbb222"], str(ids))

    r = client.delete("/api/history/nonexistent")
    check("删除不存在记录返回 404", r.status_code == 404)

    r = client.delete("/api/history?scope=success")
    d = r.json()
    check("清理成功记录", r.status_code == 200 and d.get("deleted") == 2 and d.get("remaining") == 0, json.dumps(d, ensure_ascii=False))
    r = client.get("/api/history?days=30")
    check("清理后列表为空", len(r.json()["records"]) == 0)

    seed_history()
    r = client.delete("/api/history?scope=older30")
    d = r.json()
    check("清理30天前", r.status_code == 200 and d.get("deleted") == 1, json.dumps(d, ensure_ascii=False))
    r = client.get("/api/history?days=30")
    ids = [x["id"] for x in r.json()["records"]]
    check("30天前已清理、近期保留", ids == ["aaa111", "bbb222"], str(ids))

    r = client.delete("/api/history?scope=all")
    d = r.json()
    check("清空全部", r.status_code == 200 and d.get("deleted") == 2 and d.get("remaining") == 0)

    r = client.delete("/api/history?scope=bogus")
    check("非法 scope 返回 400", r.status_code == 400)

    r = client.get("/api/history")
    check("未登录/带权访问控制", r.status_code in (200, 401))  # TestClient 会带 cookie，这里主要验证不 500

print("=== 3. 删除后后台轮询不写回 ===")
with TestClient(main.app):
    main.DELETED_HISTORY_IDS.clear()
    main._save(main.HISTORY_FILE, [])
    rec = {"id": "zzz999", "time": "2026-10-25 15:00:00", "device": "J4125飞牛", "container": "dockercopilot", "status": "进行中"}
    main._add_history(rec)
    main._mark_deleted("zzz999")
    main._save(main.HISTORY_FILE, [r for r in main._history() if r["id"] != "zzz999"])
    main._refresh_history({**rec, "status": "成功"})
    left = [r["id"] for r in main._history()]
    check("已删除记录不被 _refresh_history 写回", "zzz999" not in left, str(left))

print()
print(f"===== 结果: {len(PASS)} 通过, {len(FAIL)} 失败 =====")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
