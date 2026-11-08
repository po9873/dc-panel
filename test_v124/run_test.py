"""v1.4.0 自更新检测回归测试：多源回退 / 构建时间判定 / 失败原因不再被吞。

只跑纯逻辑，不真的联网：把三个检测源替换成桩，验证
  1) 首选源可用时只问首选源；
  2) 首选源失败会自动回退到下一个，并把失败原因记下来；
  3) 全部失败时 source=None 且 errors 三个源都有原因（不能再静默「无更新」）；
  4) 远端构建时间 > 本地镜像构建时间 才判可更新；
  5) 时区/ISO 解析对 Z、+08:00、无时区都成立；
  6) 成功源会粘住，下一轮优先问它。

用法：python test_v124/run_test.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PANEL_DATA", tempfile.mkdtemp(prefix="dcp-test-"))

from app import main as m  # noqa: E402

ORIG_IMAGE_BUILD_TIME = m._image_build_time
NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=1)
NEW = NOW + timedelta(hours=1)


def reset(state_source=None, build_time=NOW):
    m.PANEL_UPDATE_STATE.update({
        "update": False, "hub_time": None, "source": state_source, "errors": {},
        "checked_at": 0.0, "notified_hub_time": None,
    })
    m.PANEL_UPDATE_SOURCES = ["ghcr", "mirror", "hub"]
    calls = []

    def fake_image_build_time():
        return build_time

    m._image_build_time = fake_image_build_time
    return calls


def stub(calls, name, ret=None, exc=None):
    async def _f(*args, **kwargs):
        calls.append(name)
        if exc:
            raise exc
        return ret, name
    return _f


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def case_prefer_ghcr():
    calls = reset()
    m._src_registry = stub(calls, "ghcr", ret=OLD)
    m._src_hub = stub(calls, "hub", ret=NEW)
    st = run(m._check_panel_update())
    assert st["source"] == "ghcr", st
    assert st["update"] is False, "远端比本地旧时不应报可更新"
    assert calls == ["ghcr"], f"首选可用时不该再问其它源: {calls}"
    print("ok 1 首选源可用即停，且旧版本不误报")


def case_fallback_and_errors():
    calls = reset()
    m._src_registry = stub(calls, "ghcr", exc=RuntimeError("ConnectionResetError"))
    m._src_hub = stub(calls, "hub", ret=NEW)
    st = run(m._check_panel_update())
    assert st["source"] == "hub", st
    assert st["update"] is True, "远端更新时应报可更新"
    assert "ghcr" in st["errors"] and "ConnectionResetError" in st["errors"]["ghcr"], st["errors"]
    assert calls[0] == "ghcr", calls
    print("ok 2 首选失败自动回退，并记录失败原因")


def case_all_fail():
    calls = reset()
    async def boom(*args, **kwargs):
        raise RuntimeError("blocked")
    m._src_registry = boom
    m._src_hub = boom
    st = run(m._check_panel_update())
    assert st["source"] is None, st
    assert st["update"] is False
    assert set(st["errors"]) == {"ghcr", "mirror", "hub"}, st["errors"]
    print("ok 3 全部失败时不再伪装成「无更新」，三个源都给出原因")


def case_sticky_source():
    calls = reset(state_source="mirror")
    m._src_registry = stub(calls, "mirror", ret=OLD)
    m._src_hub = stub(calls, "hub", ret=OLD)
    st = run(m._check_panel_update())
    assert calls[0] == "mirror", f"上一轮成功的源应优先: {calls}"
    assert st["source"] == "mirror"
    print("ok 4 成功源粘住，减少每轮无谓探测")


def case_time_parse():
    a = m._parse_time("2026-11-08T14:59:23Z")
    b = m._parse_time("2026-11-08T22:59:23+08:00")
    c = m._parse_time("2026-11-08T14:59:23")
    assert a == b == c, (a, b, c)
    assert a.tzinfo is not None
    assert m._parse_time("not-a-time") is None
    assert m._parse_time(None) is None
    print("ok 5 时间解析（Z / +08:00 / 无时区）一致")


def case_build_time_priority():
    m._image_build_time = ORIG_IMAGE_BUILD_TIME
    tmp = tempfile.mkdtemp(prefix="dcp-build-")
    p = os.path.join(tmp, "BUILD_TIME")
    with open(p, "w", encoding="utf-8") as f:
        f.write("2026-11-08T14:59:23Z\n")
    m.BASE_DIR = type(m.BASE_DIR)(tmp)  # 指向临时目录
    assert m._image_build_time() == datetime(2026, 11, 8, 14, 59, 23, tzinfo=timezone.utc)
    # 文件缺失时退回启动时间，不能抛异常
    m.BASE_DIR = type(m.BASE_DIR)(os.path.join(tmp, "nope"))
    assert m._image_build_time() is not None
    print("ok 6 构建时间优先读 BUILD_TIME，缺失时安全回退")


if __name__ == "__main__":
    case_prefer_ghcr()
    case_fallback_and_errors()
    case_all_fail()
    case_sticky_source()
    case_time_parse()
    case_build_time_priority()
    print("ALL PASS")
