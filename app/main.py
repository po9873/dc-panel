"""DC Panel - DockerCopilot 多机管理面板（独立运行，不依赖 MoviePilot）"""
import json
import hmac
import hashlib
import base64
import time
import os
import secrets
import asyncio
import urllib.parse
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature

DATA_DIR = Path(os.environ.get("PANEL_DATA", "/data"))
CONFIG_FILE = DATA_DIR / "panel.json"
HISTORY_FILE = DATA_DIR / "history.json"
PORT = int(os.environ.get("PANEL_PORT", "8000"))
USERNAME = os.environ.get("PANEL_USERNAME", "admin").strip() or "admin"

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="DC Panel", docs_url=None, redoc_url=None)
serializer: URLSafeTimedSerializer = None  # type: ignore

# 仓库根目录（app/ 的上级）。静态资源一律用绝对路径，避免依赖启动时的 cwd，
# 否则从 app/ 目录直接起服务时 static/index.html 会解析到不存在的路径而 500。
BASE_DIR = Path(__file__).resolve().parent.parent


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _config() -> dict:
    return _load(CONFIG_FILE, {})


def _save_config(cfg: dict):
    _save(CONFIG_FILE, cfg)


def _history() -> list:
    return _load(HISTORY_FILE, [])


def _add_history(record: dict):
    h = _history()
    h.insert(0, record)
    _save(HISTORY_FILE, h[:2000])


DELETED_HISTORY_IDS: set = set()  # 用户手动删除过的记录 ID，防止后台轮询结束后又写回


def _mark_deleted(record_id: str):
    DELETED_HISTORY_IDS.add(record_id)
    if len(DELETED_HISTORY_IDS) > 5000:  # 防御性上限，避免无限增长
        DELETED_HISTORY_IDS.clear()


AUTOMATION_STATE_FILE = DATA_DIR / "automation_state.json"
AUTOMATION_DEFAULTS = {
    "enabled": True,
    "update_cron": "*/15 * * * *",
    "update_notify": True,
    "auto_update_cron": "",
    "auto_update_notify": True,
    "auto_update_success": True,
    "schedule_report": False,
    "delete_images": False,
    "interval": 10,
    # 轮询轮数：拉一个镜像常要几分钟，6 轮（约 1 分钟）根本看不到真进度，
    # 默认放宽到 20 轮（约 3 分钟），窗口结束后还有 confirm_minutes 的持续确认兜底。
    "interval_limit": 20,
    # 轮询窗口结束后仍会继续回查容器真实状态的最长分钟数。
    # 拉大镜像往往要好几分钟，窗口一结束就判定会让「其实拉完了」的更新永远
    # 停在「待确认」，用户收不到成功通知。这里给一个兜底确认期。
    "confirm_minutes": 45,
    "confirm_interval": 60,
    # 面板自身新版本自检：独立于上面的定时检查开关，间隔分钟数可在设置里调，
    # 改后下一次循环（≤20 秒）即按新间隔生效，无需重启。
    "self_check_interval": 5,
    "self_update_notify": True,
    # 是否允许面板更新自身容器：默认关闭，避免「停掉自己→进程死→误判失败→
    # 重启又扫到→再提交」的推送死循环。打开后由 DockerCopilot（独立进程）代为
    # 停旧建新，面板重启后 startup 会回查真实结果并补发通知。
    "allow_self_update": False,
}
AUTOMATION_TASK = None
AUTOMATION_LAST_RUN = {"update": "", "auto": ""}


def _automation_config() -> dict:
    cfg = _config().get("automation") or {}
    out = dict(AUTOMATION_DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in out})
    out["interval"] = max(1, int(out.get("interval") or 10))
    out["interval_limit"] = max(1, int(out.get("interval_limit") or 6))
    try:
        out["self_check_interval"] = max(1, int(out.get("self_check_interval")))
    except (TypeError, ValueError):
        out["self_check_interval"] = 5
    out["self_update_notify"] = bool(out.get("self_update_notify", True))
    try:
        out["confirm_minutes"] = max(1, min(240, int(out.get("confirm_minutes") or 45)))
    except (TypeError, ValueError):
        out["confirm_minutes"] = 45
    try:
        out["confirm_interval"] = max(20, int(out.get("confirm_interval") or 60))
    except (TypeError, ValueError):
        out["confirm_interval"] = 60
    return out


def _automation_devices() -> dict:
    raw = _config().get("automation_devices") or {}
    return raw if isinstance(raw, dict) else {}


def _cron_part(value: int, token: str, low: int, high: int) -> bool:
    for piece in str(token or "*").split(","):
        piece = piece.strip()
        if not piece:
            continue
        base, _, step_text = piece.partition("/")
        try:
            step = max(1, int(step_text)) if step_text else 1
        except ValueError:
            continue
        if base == "*":
            start, end = low, high
        elif "-" in base:
            try:
                start, end = (int(x) for x in base.split("-", 1))
            except ValueError:
                continue
        else:
            try:
                start = end = int(base)
            except ValueError:
                continue
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def _cron_matches(expr: str, now: datetime) -> bool:
    fields = str(expr or "").split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    cron_dow = (now.weekday() + 1) % 7
    if cron_dow == 0:
        dow_values = (0, 7)
    else:
        dow_values = (cron_dow,)
    minute_ok = _cron_part(now.minute, minute, 0, 59)
    hour_ok = _cron_part(now.hour, hour, 0, 23)
    month_ok = _cron_part(now.month, month, 1, 12)
    dom_any = str(dom).strip() == "*"
    dow_any = str(dow).strip() in ("*", "?", "")
    dom_ok = _cron_part(now.day, dom, 1, 31)
    dow_ok = any(_cron_part(v, dow, 0, 7) for v in dow_values)
    # 标准 cron：当日/月和星期字段都有限制时，两者采用 OR。
    day_ok = (dom_ok and dow_ok) if (dom_any or dow_any) else (dom_ok or dow_ok)
    return minute_ok and hour_ok and month_ok and day_ok


def _save_automation_state(state: dict):
    _save(AUTOMATION_STATE_FILE, state)


def _record_time() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _hash_password(pwd: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), 100_000).hex()


def _username(cfg: dict = None) -> str:
    """账号优先级：页面里改过的配置 > 环境变量 PANEL_USERNAME > admin"""
    cfg = cfg if cfg is not None else _config()
    return (cfg.get("username") or USERNAME or "admin").strip()


# ---------- DC API 客户端（本地签 HS256 JWT） ----------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def dc_jwt(secret_key: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "sub": "dc-panel",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }).encode())
    signing = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(secret_key.encode(), signing, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def dc_headers(secret_key: str) -> dict:
    # 不要在公共请求头中强制声明 JSON：DockerCopilot 的更新接口接收
    # application/x-www-form-urlencoded，而其他 json= 请求会由 httpx 自动设置类型。
    return {"Authorization": f"Bearer {dc_jwt(secret_key)}"}


async def dc_request(client: httpx.AsyncClient, device: dict, method: str, path: str, **kw):
    url = device["url"].rstrip("/") + path
    r = await client.request(method, url, headers=dc_headers(device["secretKey"]), timeout=15, **kw)
    if r.status_code == 401:
        raise HTTPException(502, f"DC [{device['name']}] 鉴权失败：secretKey 不对")
    r.raise_for_status()
    return r


async def dc_get(client, device, path):
    try:
        r = await dc_request(client, device, "GET", path)
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DC [{device['name']}] 请求失败：{e}")


def _container_array(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("data") or data.get("containers") or []
    return []


def _image_name(container: dict) -> str:
    return str(container.get("usingImage") or container.get("createImage")
               or container.get("image") or "").strip()


def _is_true(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "none", "null")
    return bool(value)


async def dc_update(client, device: dict, container: dict):
    """按 DockerCopilot 的实际接口提交更新请求。"""
    name = str(container.get("name") or container.get("id") or "").strip()
    cid = str(container.get("id") or name).strip()
    image = _image_name(container)
    if not name or not image:
        raise HTTPException(400, f"DC [{device['name']}] 缺少容器名或镜像名")
    # 自身容器默认不直接交给 DC 更新：DC 更新容器的第一步是停掉目标容器，
    # 面板一停就没有进程接手，进度必然读不到、结果必然被判失败，形成反复推送。
    # 但用户可以在设置里打开「允许面板更新自身」——打开后就正常提交，
    # 并把记录落盘，面板重启后由 startup 续查真实结果并补发通知（不屏蔽、不丢进度）。
    if (_is_self_container(container) or _is_self_name(name, image)) \
            and not _config().get("allow_self_update"):
        raise HTTPException(409, "面板自身容器默认不允许由面板更新（会中断自身并被误判失败）。"
                                 "请在宿主机执行：docker compose pull && docker compose up -d，"
                                 "或在设置里打开「允许面板更新自身」。")
    r = await dc_request(
        client, device, "POST", f"/api/container/{urllib.parse.quote(cid, safe='')}/update",
        # DockerCopilot 当前更新接口按表单字段解析；发送 JSON 会返回
        # `field "imageNameAndTag" is not set`，即使字段名看起来存在。
        data={"containerName": name, "imageNameAndTag": image},
    )
    payload = r.json() if r.content else {}
    if isinstance(payload, dict):
        code = payload.get("code")
        if not _ok_code(code):
            raise HTTPException(502, f"DC [{device['name']}] 更新失败：{payload.get('msg') or payload.get('message') or code}")
        if payload.get("success") is False:
            raise HTTPException(502, f"DC [{device['name']}] 更新失败：{payload.get('msg') or payload.get('message') or '接口返回失败'}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        task_id = payload.get("taskID") or payload.get("taskId") or data.get("taskID") or data.get("taskId") or ""
        return payload, str(task_id or "")
    raise HTTPException(502, f"DC [{device['name']}] 更新接口返回格式错误")


# ---------- 鉴权 ----------


import struct

def _totp_code(secret: str, t: int = None) -> str:
    t = int(time.time()) if t is None else t
    pad = (8 - len(secret) % 8) % 8
    key = base64.b32decode(secret.upper() + "=" * pad)
    dig = hmac.new(key, struct.pack(">Q", t // 30), hashlib.sha1).digest()
    o = dig[-1] & 0x0F
    return f"{(struct.unpack('>I', dig[o:o+4])[0] & 0x7FFFFFFF) % 1000000:06d}"

def _totp_verify(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    return hmac.compare_digest(_totp_code(secret, int(time.time())), code)

def _totp_gen() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _check_login(request: Request):
    global serializer
    if serializer is None:
        raise HTTPException(401, "未初始化")
    cookie = request.cookies.get("panel_session", "")
    if not cookie:
        raise HTTPException(401, "未登录")
    try:
        serializer.loads(cookie, max_age=7 * 86400)
    except BadSignature:
        raise HTTPException(401, "登录已过期")


@app.on_event("startup")
async def startup():
    print(f"[startup] DC Panel {APP_VERSION} | 自身容器识别提示: "
          f"{sorted(h for h in PANEL_SELF_HINTS if h)} | 自身容器ID: "
          f"{(_self_container_id() or '未读到')[:12]}", flush=True)
    # 面板自更新会把自己停掉：上一轮「进行中」的记录在新进程里续查真实结果。
    # 这样就算中间被中断，用户也能看到最终是成功还是失败，而不是永远「待确认」。
    try:
        for rec in _history():
            if str(rec.get("status") or "") not in ("进行中", "未确认", "等待中"):
                continue
            rid = str(rec.get("id") or "")
            if not rid or rid in _PENDING_CONFIRM:
                continue
            dev = next((d for d in _devices() if d.get("name") == rec.get("device")), None)
            if not dev:
                continue
            print(f"[startup] 续查未完成记录：{rec.get('device')}/{rec.get('container')}", flush=True)
            _PENDING_CONFIRM[rid] = asyncio.create_task(
                _confirm_watch(dev, rec, _automation_config()))
    except Exception as e:
        print(f"[startup] 续查失败: {e}", flush=True)
    global serializer
    cfg = _config()
    if "salt" not in cfg:
        cfg["salt"] = secrets.token_hex(16)
    env_pwd = os.environ.get("PANEL_PASSWORD", "").strip()
    if cfg.get("ui_pwd"):
        # 用户在 UI 里改过密码：以 UI 为准，环境变量永久失效
        pass
    elif env_pwd:
        # 变量只在 UI 未改过时生效（改变量即可改密码、忘密码也能救回）
        cfg["password"] = _hash_password(env_pwd, cfg["salt"])
        cfg["need_change"] = False
    elif "password" not in cfg:
        # 首次启动且未设置环境变量：随机生成密码并打印到容器日志，
        # 之后在页面里改的密码会持久保存到 /data/panel.json，重启不再变化
        rnd = secrets.token_urlsafe(9)
        cfg["password"] = _hash_password(rnd, cfg["salt"])
        cfg["need_change"] = False
        _save_config(cfg)
        print("=" * 60, flush=True)
        print("DC-Panel 首次启动：随机初始密码为", rnd, flush=True)
        print("请用 admin / 该密码 登录后，在页面底部「修改密码」改成自己的", flush=True)
        print("=" * 60, flush=True)
        return
    _save_config(cfg)
    serializer = URLSafeTimedSerializer(cfg["salt"] + "-session")
    global AUTOMATION_TASK
    if AUTOMATION_TASK is None or AUTOMATION_TASK.done():
        AUTOMATION_TASK = asyncio.create_task(_automation_loop())


@app.on_event("shutdown")
async def shutdown():
    global AUTOMATION_TASK
    if AUTOMATION_TASK and not AUTOMATION_TASK.done():
        AUTOMATION_TASK.cancel()
        try:
            await AUTOMATION_TASK
        except asyncio.CancelledError:
            pass
        AUTOMATION_TASK = None
    # 面板自身被更新时会先停机，聚合队列里可能还压着结果，退出前先冲出去。
    await flush_pending_notifications()


# ---------- 页面 ----------

@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Service Worker 脚本：必须可访问且带正确的 Content-Type / Scope"""
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.json", include_in_schema=False)
async def pwa_manifest():
    """PWA 清单（主屏图标、启动画面）"""
    return FileResponse(BASE_DIR / "static" / "manifest.json",
                        media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/static/{fname}", include_in_schema=False)
async def static_asset(fname: str):
    """静态资源（图标等），只允许白名单内的图片，防目录穿越"""
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]+", fname) or ".." in fname:
        raise HTTPException(404, "not found")
    p = BASE_DIR / "static" / fname
    if not p.is_file():
        raise HTTPException(404, "not found")
    ext = p.suffix.lstrip(".").lower()
    media = {"png": "image/png", "ico": "image/x-icon", "svg": "image/svg+xml",
             "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
             "json": "application/json"}.get(ext, "application/octet-stream")
    if media == "application/octet-stream":
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type=media,
                        headers={"Cache-Control": "public, max-age=86400"})


# ---------- 登录 / 密码 ----------

@app.get("/api/2fa-required")
async def twofa_required():
    cfg = _config()
    return {"enabled": bool(cfg.get("totp_enabled") and cfg.get("totp_secret"))}

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    cfg = _config()
    if (body.get("username") or "").strip() != _username(cfg):
        raise HTTPException(403, "账号或密码错误")
    if _hash_password(body.get("password", ""), cfg["salt"]) != cfg["password"]:
        raise HTTPException(403, "账号或密码错误")
    if cfg.get("totp_enabled") and cfg.get("totp_secret"):
        if not _totp_verify(cfg["totp_secret"], body.get("code") or ""):
            raise HTTPException(403, "TWOFA")
    resp = JSONResponse({"ok": True, "need_change": cfg.get("need_change", False)})
    resp.set_cookie("panel_session", serializer.dumps("ok"),
                    httponly=True, samesite="lax", max_age=7 * 86400)
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("panel_session")
    return resp


@app.post("/api/password")
async def change_password(request: Request):
    _check_login(request)
    body = await request.json()
    cfg = _config()
    if _hash_password(body.get("old", ""), cfg["salt"]) != cfg["password"]:
        raise HTTPException(403, "旧密码错误")
    new_user = (body.get("username") or "").strip()
    new = body.get("new", "")
    if new_user and new_user != _username(cfg):
        if len(new_user) < 2 or len(new_user) > 32:
            raise HTTPException(400, "账号长度需在 2~32 位之间")
        if not all(c.isalnum() or c in "_-." for c in new_user):
            raise HTTPException(400, "账号只能包含字母、数字、下划线、短横线、点")
        cfg["username"] = new_user
        cfg["ui_user"] = True  # UI 改过账号后，PANEL_USERNAME 变量失效
    if new:
        if len(new) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        cfg["password"] = _hash_password(new, cfg["salt"])
        cfg["need_change"] = False
        cfg["ui_pwd"] = True  # UI 改过密码后，PANEL_PASSWORD 变量永久失效
    if not new_user and not new:
        raise HTTPException(400, "账号和新密码都没填")
    _save_config(cfg)
    return {"ok": True, "username": _username(cfg)}



@app.get("/api/totp")
async def totp_status(request: Request):
    _check_login(request)
    cfg = _config()
    sec = cfg.get("totp_secret") if not cfg.get("totp_enabled") else None
    return {"enabled": bool(cfg.get("totp_enabled")), "secret": sec,
            "otpauth": (f"otpauth://totp/DC-Panel:{urllib.parse.quote(_username(cfg))}"
                        f"?secret={sec}&issuer=DC-Panel") if sec else None}

@app.post("/api/totp")
async def totp_manage(request: Request):
    _check_login(request)
    body = await request.json()
    cfg = _config()
    act = body.get("action")
    if act == "setup":
        cfg["totp_secret"] = _totp_gen()
        cfg["totp_enabled"] = False
        _save_config(cfg)
        return {"ok": True, "secret": cfg["totp_secret"],
                "otpauth": f"otpauth://totp/DC-Panel:{urllib.parse.quote(_username(cfg))}?secret={cfg['totp_secret']}&issuer=DC-Panel"}
    if act == "enable":
        if not cfg.get("totp_secret"):
            raise HTTPException(400, "请先生成密钥")
        if not _totp_verify(cfg["totp_secret"], body.get("code") or ""):
            raise HTTPException(403, "动态码错误，请确认手机时间准确后重试")
        cfg["totp_enabled"] = True
        _save_config(cfg)
        return {"ok": True}
    if act == "disable":
        if not cfg.get("totp_enabled"):
            return {"ok": True}
        if not _totp_verify(cfg.get("totp_secret", ""), body.get("code") or ""):
            raise HTTPException(403, "动态码错误")
        cfg["totp_enabled"] = False
        cfg.pop("totp_secret", None)
        _save_config(cfg)
        return {"ok": True}
    raise HTTPException(400, "未知操作")


@app.get("/api/account")
async def get_account(request: Request):
    _check_login(request)
    return {"username": _username()}


@app.get("/api/status")
async def status(request: Request):
    _check_login(request)
    cfg = _config()
    return {"need_change": cfg.get("need_change", False),
            "has_bg": bool(cfg.get("bg_dark") or cfg.get("bg_light")),
            "has_bg_dark": bool(cfg.get("bg_dark")),
            "has_bg_light": bool(cfg.get("bg_light"))}


# ---------- 面板自身更新检测（Docker Hub） ----------

HUB_REPO = os.environ.get("PANEL_HUB_REPO", "<dockerhub-user>/dc-panel")
APP_VERSION = "v1.3.0"

def _boot_time():
    try:
        with open("/proc/1/stat") as f:
            fields = f.read().split()
        btime = int(fields[21])
        return datetime.fromtimestamp(btime / 100, tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc) - timedelta(days=3650)

# 面板自身版本自检状态（进程内）。checked_at 用于节流：前端定时轮询命中
# /api/version/check 时直接读缓存，不会反复打 Docker Hub；真正按用户设置的
# 间隔查询由 _automation_loop 完成——间隔改后下一次循环（≤20 秒）即生效。
PANEL_UPDATE_STATE = {
    "current": APP_VERSION, "repo": HUB_REPO,
    "update": False, "hub_time": None,
    "checked_at": 0.0, "notified_hub_time": None,
}
PANEL_UPDATE_CACHE_TTL = 60.0


async def _notify_panel_update(hub_time):
    """发现新版本且该版本尚未通知过时，按已启用的通道推送一次。"""
    state = PANEL_UPDATE_STATE
    if state["notified_hub_time"] == hub_time:
        return
    state["notified_hub_time"] = hub_time
    record = {
        "id": secrets.token_hex(6), "time": _record_time(), "device": "本机",
        "container": "dc-panel", "image": f"{HUB_REPO}:latest",
        "status": "可更新", "source": "面板自检",
        "message": f"Docker Hub 已发布新镜像（构建于 {hub_time}），当前容器启动于更早版本",
    }
    await notify_update(record)


async def _check_panel_update(notify: bool = False) -> dict:
    """对照 Docker Hub latest 的构建时间与容器启动时间，判断面板是否有新版。

    notify=True 时（仅后台循环调用）在发现新版本、且该版本尚未通知过时，
    按已启用的通道推送一次；前端轮询只读结果，不重复通知。
    缓存命中（60 秒内已查过）时也处理通知，避免前端轮询刚好刷新缓存
    导致后台循环错过这一轮。
    """
    state = PANEL_UPDATE_STATE
    state["current"] = APP_VERSION
    state["repo"] = HUB_REPO
    now_ts = time.time()
    if state["checked_at"] and (now_ts - state["checked_at"]) < PANEL_UPDATE_CACHE_TTL:
        if notify and state["update"]:
            await _notify_panel_update(state["hub_time"])
        return state
    update, hub_time = False, None
    try:
        async with httpx.AsyncClient(timeout=8) as cli:
            r = await cli.get(f"https://hub.docker.com/v2/repositories/{HUB_REPO}/tags/latest")
            if r.status_code == 200:
                d = r.json()
                hub_time = d.get("last_updated") or d.get("tag_last_pushed")
                if hub_time:
                    update = datetime.fromisoformat(hub_time.replace("Z", "+00:00")) > _boot_time()
    except Exception:
        pass
    state.update({"update": update, "hub_time": hub_time, "checked_at": now_ts})
    if notify and update:
        await _notify_panel_update(hub_time)
    return state


@app.get("/api/version/check")
async def version_check():
    state = await _check_panel_update()
    return {"current": state["current"], "repo": state["repo"],
            "update": state["update"], "hub_time": state["hub_time"]}


# ---------- 背景图（深浅主题各一套） ----------

ALLOWED_BG = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}


def _bg_file(theme: str) -> Path:
    return DATA_DIR / ("bg-light.img" if theme == "light" else "bg-dark.img")


def _bg_migrate():
    """旧版单张 bg.img 迁移为深色壁纸"""
    old = DATA_DIR / "bg.img"
    if old.exists():
        tgt = DATA_DIR / "bg-dark.img"
        if not tgt.exists():
            try:
                old.rename(tgt)
            except OSError:
                pass
        else:
            try:
                old.unlink()
            except OSError:
                pass


_bg_migrate()


@app.get("/api/bg")
async def get_bg(request: Request, theme: str = "dark"):
    """主题壁纸（img 同源带 cookie，由前端探测决定是否使用）"""
    f = _bg_file(theme)
    if f.exists():
        ext = f.suffix.lstrip(".").lower()
        media = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        return FileResponse(f, media_type=media)
    raise HTTPException(404, "未设置背景")


@app.post("/api/bg")
async def upload_bg(request: Request, theme: str = "dark"):
    _check_login(request)
    ctype = request.headers.get("content-type", "")
    filename = ""
    data = b""
    if "multipart/form-data" in ctype:
        form = await request.form()
        f = form.get("file")
        if f is None or not getattr(f, "filename", ""):
            raise HTTPException(400, "未收到文件")
        filename = f.filename
        data = await f.read()
        if form.get("theme") in ("dark", "light"):
            theme = form["theme"]
    else:
        data = await request.body()
        filename = request.headers.get("x-filename", "")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_BG:
        raise HTTPException(400, f"仅支持图片格式：{'/'.join(sorted(ALLOWED_BG))}")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 15MB")
    tgt = _bg_file(theme)
    tgt.write_bytes(data)
    cfg = _config()
    cfg["bg_dark" if theme == "dark" else "bg_light"] = tgt.name
    _save_config(cfg)
    return {"ok": True, "theme": theme}


@app.delete("/api/bg")
async def delete_bg(request: Request, theme: str = "dark"):
    _check_login(request)
    f = _bg_file(theme)
    if f.exists():
        f.unlink()
    cfg = _config()
    cfg.pop("bg_dark" if theme == "dark" else "bg_light", None)
    _save_config(cfg)
    return {"ok": True}


# ---------- 设备管理 ----------

def _devices() -> list:
    return _config().get("devices", [])


def _save_devices(devices: list):
    cfg = _config()
    cfg["devices"] = devices
    _save_config(cfg)


@app.get("/api/devices")
async def list_devices(request: Request):
    _check_login(request)
    async with httpx.AsyncClient() as client:
        out = []
        for d in _devices():
            item = {"id": d["id"], "name": d["name"], "url": d["url"]}
            try:
                r = await client.get(d["url"].rstrip("/") + "/api/version",
                                     headers=dc_headers(d["secretKey"]), timeout=6)
                item["online"] = r.status_code == 200
                if r.status_code == 200:
                    v = r.json()
                    item["version"] = v.get("version") or v.get("Version") or ""
            except Exception:
                item["online"] = False
            out.append(item)
        return out


@app.post("/api/devices")
async def add_device(request: Request):
    _check_login(request)
    body = await request.json()
    name, url, key = (body.get("name") or "").strip(), (body.get("url") or "").strip(), (body.get("secretKey") or "").strip()
    if not name or not url or not key:
        raise HTTPException(400, "名称、地址、secretKey 都必填")
    if not url.startswith("http"):
        url = "http://" + url
    devices = _devices()
    if any(d["url"].rstrip("/") == url.rstrip("/") for d in devices):
        raise HTTPException(400, "该地址已存在")
    # 试连一次
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url.rstrip("/") + "/api/version", headers=dc_headers(key), timeout=6)
            if r.status_code == 401:
                raise HTTPException(400, "secretKey 校验失败（401）")
            r.raise_for_status()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"连不上该地址：{e}")
    devices.append({"id": secrets.token_hex(4), "name": name, "url": url.rstrip("/"), "secretKey": key})
    _save_devices(devices)
    return {"ok": True}


@app.delete("/api/devices/{device_id}")
async def del_device(device_id: str, request: Request):
    _check_login(request)
    devices = [d for d in _devices() if d["id"] != device_id]
    _save_devices(devices)
    return {"ok": True}


# ---------- 容器 ----------

@app.get("/api/devices/{device_id}/containers")
async def list_containers(device_id: str, request: Request):
    _check_login(request)
    device = next((d for d in _devices() if d["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "设备不存在")
    async with httpx.AsyncClient() as client:
        data = await dc_get(client, device, "/api/containers")
    containers = data if isinstance(data, list) else data.get("containers") or data.get("data") or []
    return containers


# ---------- 自动更新检查（按 V1 插件逻辑） ----------

async def _automation_loop():
    """独立于浏览器的周期扫描：发现 DC 返回 haveUpdate=true 就记录并通知。"""
    global AUTOMATION_LAST_RUN
    last_self_check = 0.0
    while True:
        try:
            cfg = _automation_config()
            now = datetime.now(timezone(timedelta(hours=8)))
            minute_key = now.strftime("%Y-%m-%d %H:%M")
            if cfg.get("enabled") and _cron_matches(cfg.get("update_cron"), now):
                if AUTOMATION_LAST_RUN.get("update") != minute_key:
                    AUTOMATION_LAST_RUN["update"] = minute_key
                    await _scan_updates(auto=False)
            if cfg.get("enabled") and cfg.get("auto_update_cron") and _cron_matches(cfg.get("auto_update_cron"), now):
                if AUTOMATION_LAST_RUN.get("auto") != minute_key:
                    AUTOMATION_LAST_RUN["auto"] = minute_key
                    await _scan_updates(auto=True)
            # 面板自身新版本检查：不受 enabled 限制（这是面板自己的事），
            # 间隔取设置里的 self_check_interval（分钟），每轮循环实时读取，
            # 用户在 UI 改完下一次循环即生效，无需重启。
            now_ts = time.time()
            interval = max(1, int(cfg.get("self_check_interval") or 5)) * 60
            if now_ts - last_self_check >= interval:
                last_self_check = now_ts
                await _check_panel_update(notify=bool(cfg.get("self_update_notify", True)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[automation] 扫描失败: {e}", flush=True)
        await asyncio.sleep(20)


async def _scan_updates(auto: bool = False):
    cfg = _automation_config()
    if auto and not cfg.get("auto_update_cron"):
        return
    async with httpx.AsyncClient() as client:
        for device in _devices():
            try:
                data = await dc_get(client, device, "/api/containers")
                await _scan_device_updates(device, _container_array(data), client, cfg, auto)
            except Exception as e:
                print(f"[automation] {device.get('name')} 检查失败: {e}", flush=True)


async def _scan_device_updates(device: dict, containers: list, client, cfg: dict, auto: bool):
    state = _load(AUTOMATION_STATE_FILE, {})
    pending = state.setdefault("pending", {})
    selections = _automation_devices().get(device.get("id"), {})
    selected_key = "auto_update" if auto else "update"
    selected = selections.get(selected_key) or []
    # 已勾进「自动更新」且配了自动更新定时的容器，由自动更新分支推「已触发/成功」，
    # 定时检查再推一条「可更新」属于重复通知，这里直接跳过。
    auto_sel = selections.get("auto_update") or []
    seen = set()
    for c in containers:
        name = str(c.get("name") or c.get("id") or "").strip()
        if not name:
            continue
        key = f"{device.get('id')}::{name}"
        seen.add(key)
        has_update = _is_true(c.get("haveUpdate"))
        if not has_update:
            pending.pop(key, None)
            continue
        # 面板自身容器：更新自己必然中断自身、进度丢失并被误判失败，
        # 跳过且不推送失败通知，避免「一直报错推送」的死循环。
        # 仅当用户在「自动化」设置里打开「允许面板更新自身」时才放行。
        if _is_self_container(c) and not _config().get("allow_self_update"):
            if not pending.get(key, {}).get("self_skipped"):
                pending.setdefault(key, {})["self_skipped"] = True
                record = {
                    "id": secrets.token_hex(6), "time": _record_time(), "device": device["name"],
                    "container": name, "image": _image_name(c), "status": "已跳过",
                    "source": "自动更新" if auto else "定时检查",
                    "message": "面板自身容器，不能由面板更新（会中断自身并被误判失败）。请在宿主机执行：docker compose pull && docker compose up -d",
                }
                _add_history(record)
            continue
        # 空选择表示该设备不启用这一类任务；有选择时只处理勾选项。
        if not selected or name not in selected:
            continue
        image = _image_name(c)
        fingerprint = image + "|" + str(c.get("latestImage") or c.get("updateImage") or c.get("tag") or "")
        if auto:
            entry = pending.get(key, {})
            # 只有更新任务已成功提交才去重。旧版本在请求前就写指纹，失败后会
            # 永久跳过同一镜像；没有 auto_status 的旧记录按失败状态重新尝试。
            if (entry.get("auto_fingerprint") == fingerprint
                    and entry.get("auto_status") == "submitted"):
                continue
            record = {
                "id": secrets.token_hex(6), "time": _record_time(), "device": device["name"],
                "container": name, "image": image, "status": "进行中", "source": "自动更新"
            }
            _add_history(record)
            try:
                payload, task_id = await dc_update(client, device, c)
                pending.setdefault(key, {}).update({
                    "auto_fingerprint": fingerprint,
                    "auto_status": "submitted",
                    "image": image,
                    "time": _record_time(),
                })
                record["message"] = str(payload.get("msg") or payload.get("message") or "自动更新任务已创建")
                record["taskID"] = task_id
                _refresh_history(record)
                if cfg.get("auto_update_notify"):
                    await notify_update({**record, "status": "已触发"})
                if task_id and (cfg.get("auto_update_success") or cfg.get("schedule_report")):
                    asyncio.create_task(_poll_progress(device, task_id, record, cfg))
            except Exception as e:
                message = str(e)
                failed_entry = pending.setdefault(key, {})
                failed_entry.pop("auto_fingerprint", None)
                failed_entry.update({
                    "auto_status": "busy" if "已有任务在执行中" in message else "failed",
                    "image": image,
                    "time": _record_time(),
                })
                # DockerCopilot 已在处理同一资源时不算更新失败。保留可重试状态，
                # 但不重复发送失败通知，避免 MP 插件与面板同时调度造成通知轰炸。
                if "已有任务在执行中" in message:
                    record["status"] = "等待中"
                    record["message"] = "DockerCopilot 已有同资源任务，已延后到下次周期重试"
                    _refresh_history(record); await _sync_out(record)
                elif "面板自身容器" in message:
                    record["status"] = "已跳过"
                    record["message"] = message
                    _refresh_history(record); await _sync_out(record)
                else:
                    record["status"] = "失败"; record["message"] = message; _refresh_history(record)
                    await _sync_out(record)
                    if cfg.get("auto_update_notify") or cfg.get("auto_update_success"):
                        await notify_update(record)
        else:
            if pending.get(key, {}).get("notify_fingerprint") == fingerprint:
                continue
            if cfg.get("auto_update_cron") and name in auto_sel:
                continue
            pending.setdefault(key, {})["notify_fingerprint"] = fingerprint
            pending[key].update({"image": image, "time": _record_time()})
            record = {
                "id": secrets.token_hex(6), "time": _record_time(), "device": device["name"],
                "container": name, "image": image, "status": "可更新", "source": "定时检查",
                "message": "DockerCopilot 检测到新镜像"
            }
            _add_history(record)
            if cfg.get("update_notify"):
                await notify_update(record)
    # 删除 DC 返回列表中已经消失的旧状态，避免容器重建后卡住去重状态。
    for key in list(pending):
        if key.startswith(f"{device.get('id')}::") and key not in seen:
            pending.pop(key, None)
    _save_automation_state(state)


# ---------- 更新 ----------

def _self_container_id() -> str:
    """容器内运行时读取自身容器 ID，用于识别「面板自己」。

    Docker 容器里 /proc/self/cgroup 形如 `0::/docker/<64位id>`（cgroup v2）、
    `12:pids:/docker/<id>`（v1），systemd 驱动下还有 `docker-<id>.scope`。
    读不到就返回空（非容器环境或权限受限），退化为按容器名/镜像名匹配。
    """
    for path in ("/proc/self/cgroup", "/proc/1/cgroup"):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except Exception:
            continue
        m = re.search(r"[0-9a-f]{64}", txt)
        if m:
            return m.group(0)
        m = re.search(r"docker[_-]([0-9a-f]{12,64})", txt)
        if m:
            return m.group(1)
    return ""


# 面板自身容器的识别提示：内置 "dc-panel" 一定保留（用户设了 PANEL_CONTAINER_NAME
# 也只是在此基础上追加，不会因为环境变量写错而失效），再兼容常见的自部署命名。
PANEL_SELF_HINTS = {
    "dc-panel",
    (os.environ.get("PANEL_CONTAINER_NAME") or "").strip().lower(),
    (os.environ.get("HOSTNAME") or "").strip().lower(),
}
PANEL_SELF_HINTS = {h for h in PANEL_SELF_HINTS if h}
PANEL_SELF_HINT = "dc-panel"


def _is_self_name(name: str = "", image: str = "") -> bool:
    """只按容器名/镜像名判断是不是面板自己（不依赖 cgroup，读不到 ID 时也有效）。"""
    nm = str(name or "").strip().lower()
    img = str(image or "").strip().lower()
    for hint in PANEL_SELF_HINTS:
        if not hint:
            continue
        if hint in nm or hint in img:
            return True
    return False


def _is_self_container(c: dict) -> bool:
    """判断这个容器是不是面板自己。

    面板更新自己必然失败：DockerCopilot 更新容器的第一步就是停掉目标容器，
    面板一停就没有进程接手，进度追踪立刻中断，重启后只能判失败；下一轮又扫到
    haveUpdate 再提交——形成「一直报错推送」的死循环。这里识别出来直接跳过。
    """
    try:
        cid = str(c.get("id") or "").strip().lower()
        name = str(c.get("name") or "").strip().lower()
        image = str(c.get("image") or c.get("usingImage")
                    or c.get("createImage") or "").lower()
        self_id = _self_container_id().lower()
        if self_id and cid and (
                self_id.startswith(cid[:12]) or cid.startswith(self_id[:12])):
            return True
        if _is_self_name(name, image):
            return True
    except Exception:
        pass
    return False


def _ok_code(code) -> bool:
    """判断 DockerCopilot 返回的 code 是否为成功码。

    DC 各版本约定不一致：有的用 200、有的用 0、有的干脆不返回 code，
    还有版本把数字序列化成字符串（"200"/"0"）。直接拿字面量比较会把字符串
    "200" 当成失败，进而把明明已创建的更新任务报错推给客户。这里统一归一化。
    """
    if code is None:
        # 不返回 code 的老版本按 HTTP 状态码表达结果，视为成功
        return True
    try:
        return int(code) in (0, 200, 201, 202)
    except (TypeError, ValueError):
        return False


# DockerCopilot 的 /api/progress 会返回 status 字段，但各版本取值混乱：
# 有的给 completed/success，有的在拉取过程中就给 pulling/downloading/running。
# 旧逻辑把「status 非空」一律当成终态，于是拉取中途只要 message 里出现过
# 「失败/错误/error」（某一层拉取重试很常见）就被判失败并推送给客户。
# 这里显式区分：运行态不算终态，只有明确的完成/失败/取消才算。
_STATUS_DONE = ("completed", "complete", "success", "succeeded", "done", "finished", "ok")
_STATUS_BAD = ("failed", "failure", "error", "canceled", "cancelled", "cancel", "timeout")
_STATUS_RUNNING = ("running", "pulling", "download", "downloading", "extract", "extracting",
                   "creating", "starting", "pending", "waiting", "in_progress", "inprogress",
                   "processing", "progress", "updating", "active", "")


def _is_terminal_status(status: str) -> bool:
    s = str(status or "").strip().lower()
    if not s or s in _STATUS_RUNNING:
        return False
    return s in _STATUS_DONE or s in _STATUS_BAD


async def _confirm_container_updated(client, device: dict, container_name: str):
    """DC 自更新会重启自身，内存里的进度记录随之丢失，/api/progress 报
    "taskID 未找到"。此时回查容器列表：haveUpdate 消失即更新已完成。
    返回 True=已完成 / False=仍标记可更新 / None=暂无法确认。"""
    if not container_name:
        return None
    try:
        data = await dc_get(client, device, "/api/containers")
    except Exception:
        return None
    target = str(container_name or "").strip()
    for c in _container_array(data):
        nm = str(c.get("name") or "").strip()
        cid = str(c.get("id") or "").strip()
        if target and (nm == target or (cid and (cid == target or cid.startswith(target[:12])
                                                 or target.startswith(cid[:12])))):
            return not _is_true(c.get("haveUpdate"))
    return None


# 轮询窗口结束后仍在等待确认的记录：record_id -> asyncio.Task。
# 防止面板重启前重复起确认任务，也让 UI 能看出「还在确认中」。
_PENDING_CONFIRM: dict = {}


async def _confirm_watch(device: dict, record: dict, cfg: dict):
    """轮询窗口结束后继续回查容器真实状态，直到确认成功或超过确认期。

    为什么需要它：拉一个大镜像往往要好几分钟，而轮询窗口默认只有 6 轮（约 1 分钟）。
    窗口一结束就下结论，「其实已经拉完、正在重建」的更新会被记成「待确认」/失败，
    用户永远收不到成功通知，只能一直以为都失败了。
    这里在后台按分钟继续回查：DC 那边的 haveUpdate 一旦消失，就补发成功通知。
    """
    rid = str(record.get("id") or "")
    deadline = time.time() + int(cfg.get("confirm_minutes") or 45) * 60
    step = max(20, int(cfg.get("confirm_interval") or 60))
    last_confirmed = None
    # 面板自更新会重启自身：本进程的启动时间晚于该记录的提交时间，就说明面板
    # 已经带着新镜像重启成功。DC 的镜像摘要缓存常有滞后，重启后仍把 dc-panel
    # 标成「可更新」，等满确认期只会误判失败——这里直接确认成功，不再空等。
    if _is_self_name(str(record.get("container") or ""), str(record.get("image") or "")):
        try:
            rt = datetime.strptime(str(record.get("time") or ""),
                                   "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=8)))
            if _boot_time() > rt:
                record["status"] = "成功"
                record["message"] = "面板已带新镜像重启完成（经启动时间确认）"
                record.pop("progress", None)
                _refresh_history(record); await _sync_out(record)
                if cfg.get("auto_update_success", True):
                    try:
                        await notify_update(record)
                    except Exception:
                        pass
                return
        except ValueError:
            pass
    try:
        async with httpx.AsyncClient() as client:
            while time.time() < deadline:
                await asyncio.sleep(step)
                if rid in DELETED_HISTORY_IDS:
                    return
                try:
                    confirmed = await _confirm_container_updated(
                        client, device, record.get("container", ""))
                except Exception:
                    confirmed = None
                if confirmed is not None:
                    last_confirmed = confirmed
                if confirmed is True:
                    record["status"] = "成功"
                    record["message"] = "更新已完成（轮询窗口后持续回查确认）"
                    record.pop("progress", None)
                    _refresh_history(record); await _sync_out(record)
                    if cfg.get("auto_update_success", True):
                        try:
                            await notify_update(record)
                        except Exception:
                            pass
                    return
                record["progress"] = ("容器仍标记为可更新，继续等待确认…"
                                      if confirmed is False else "暂无法读取容器状态，继续等待确认…")
                _refresh_history(record)
            if str(record.get("status") or "") in ("进行中", "未确认", ""):
                record.pop("progress", None)
                if last_confirmed is False:
                    record["status"] = "失败"
                    record["message"] = "超过确认期容器仍标记为可更新，DockerCopilot 未实际完成更新"
                    await _sync_out(record)
                else:
                    record["status"] = "未确认"
                    record["message"] = "超过确认期仍未拿到明确结果，请到容器页确认"
                    await _sync_out(record)
                _refresh_history(record)
                if cfg.get("auto_update_success", True):
                    try:
                        await notify_update(record)
                    except Exception:
                        pass
    finally:
        _PENDING_CONFIRM.pop(rid, None)


async def _poll_progress(device: dict, task_id: str, record: dict, automation_cfg: dict = None):
    """后台轮询更新进度，兼容 DC 的 code/msg 和 status/progress 两种返回格式。"""
    cfg = automation_cfg or {}
    final_notified = False
    still_pending_hits = 0
    prog_last_pct = -1
    prog_last_ts = 0.0
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(max(1, int(cfg.get("interval_limit") or 6))):
                await asyncio.sleep(max(1, int(cfg.get("interval") or 5)))
                try:
                    r = await dc_request(client, device, "GET", f"/api/progress/{urllib.parse.quote(str(task_id), safe='')}")
                    p = r.json() if r.content else {}
                except Exception:
                    continue
                if not isinstance(p, dict):
                    continue
                detail = p.get("data") if isinstance(p.get("data"), dict) else {}
                msg = str(detail.get("message") or detail.get("detailMsg")
                          or p.get("msg") or p.get("message") or p.get("detail") or "")
                status = str(detail.get("status") or p.get("status") or "").lower()
                code = p.get("code")
                percentage = detail.get("percentage", p.get("percentage"))
                detail_msg = str(detail.get("detailMsg") or detail.get("message") or msg)
                if percentage is not None:
                    record["progress"] = f"{percentage}% {detail_msg}".strip()
                else:
                    record["progress"] = str(p.get("progress") or detail_msg or record.get("progress") or "")
                is_done = _is_true(detail.get("isDone", p.get("isDone")))
                is_failed = _is_true(detail.get("failed", p.get("failed")))
                is_canceled = _is_true(detail.get("canceled", p.get("canceled")))
                # 终态只认明确信号：isDone/failed/canceled，或 status 是完成/失败类取值。
                # running/pulling/downloading 等运行态一律不算终态。
                status_terminal = _is_terminal_status(status)
                terminal = is_done or is_failed or is_canceled or status_terminal
                success = (is_done and not is_failed and not is_canceled) \
                    or status in _STATUS_DONE
                # 失败只在明确终态时判定：拉取镜像过程中 progress message 常出现
                # 「失败/错误/error」（某一层拉取重试的字样），据此判失败会不停给客户
                # 推送「拉取失败」的误报。字符串匹配仅在已到终态时才作为收尾依据。
                failed = is_failed or is_canceled \
                    or status in _STATUS_BAD \
                    or (terminal and any(x in msg.lower() for x in ("失败", "错误", "failed", "error", "取消", "canceled")))
                # 进度接口读不到（DC 重启 / 任务记录被清 / 返回异常码 / 404 等）：
                # 一律不做失败判定。DC 实际可能已经拉取并重建完成，只是进度查不到，
                # 直接判失败就会把「其实成功」的更新误报成失败推给客户。
                # 这里回查容器真实状态，确认不了就继续轮询，由窗口结束时做最终判定。
                explicit_fail = is_failed or is_canceled or status in _STATUS_BAD
                if not _ok_code(code) and not explicit_fail:
                    record["progress"] = f"进度不可读（code={code}），正在核对容器状态…"
                    confirmed = await _confirm_container_updated(client, device, record.get("container", ""))
                    if confirmed is True:
                        record["status"] = "成功"
                        record["message"] = "更新已完成（进度记录不可读，经容器状态确认）"
                        record.pop("progress", None)
                        _refresh_history(record); await _sync_out(record)
                        if cfg.get("auto_update_success", True):
                            await notify_update(record)
                            final_notified = True
                        break
                    if confirmed is False:
                        # 容器仍标记为可更新：DC 可能还没刷新，先记下来继续等，
                        # 连续确认到仍可更新才在窗口结束时判失败。
                        still_pending_hits += 1
                        _refresh_history(record)
                        continue
                    _refresh_history(record)
                    continue
                if cfg.get("schedule_report") and not (success or failed):
                    pct = _progress_pct(record.get("progress"))
                    now_ts = time.time()
                    # 进度只在「跨过 20% 台阶」或「久未推送」时发一条：默认 10 秒一轮
                    # 轮询、20 轮下来，每个容器逐条推要刷十几条，手机端直接刷屏。
                    if pct < 0:
                        should_push = now_ts - prog_last_ts >= 120
                    else:
                        should_push = pct >= prog_last_pct + 20 or now_ts - prog_last_ts >= 180
                    if should_push:
                        if pct >= 0:
                            prog_last_pct = pct
                        prog_last_ts = now_ts
                        await notify_update({**record, "status": "进度", "message": msg})
                if success:
                    record["status"] = "成功"
                    record["message"] = detail_msg or msg or "容器更新成功"
                    await _sync_out(record)
                    if cfg.get("auto_update_success", True):
                        await notify_update(record)
                        final_notified = True
                    break
                if failed:
                    record["status"] = "失败"
                    record["message"] = detail_msg or msg or "DockerCopilot 返回更新失败"
                    await _sync_out(record)
                    if cfg.get("auto_update_success", True):
                        await notify_update(record)
                        final_notified = True
                    break
                _refresh_history(record)
        if record["status"] == "进行中":
            # 轮询窗口结束仍无明确终态：不猜、不直接判失败。
            # 回查容器真实状态——DC 常常已经拉取并重建完成，只是进度接口读不到。
            # 只有「容器确实仍标记为可更新」才判失败；查不到就记未确认，绝不误报失败。
            try:
                async with httpx.AsyncClient() as client:
                    confirmed = await _confirm_container_updated(
                        client, device, record.get("container", ""))
            except Exception:
                confirmed = None
            record.pop("progress", None)
            if confirmed is True:
                record["status"] = "成功"
                record["message"] = "更新已完成（轮询未拿到终态，经容器状态确认）"
                await _sync_out(record)
            else:
                # 「仍标记为可更新」不等于失败：拉镜像常要几分钟，轮询窗口（约 1~3
                # 分钟）结束时多半还在拉取/重建，此时 DC 的 haveUpdate 自然还在。
                # 这里曾直接判「失败」并推送，造成大量误报失败；且记录一旦终态就
                # 不会再补成功通知。改为交给 _confirm_watch 持续确认，由它给出
                # 最终的成功/失败（成功必通知，失败要等满确认期才判，绝不抢跑）。
                record["status"] = "未确认"
                if confirmed is False and still_pending_hits:
                    record["message"] = "轮询窗口结束时镜像仍在拉取/重建中，已转入后台持续确认…"
                else:
                    record["message"] = "轮询结束，进度接口无明确终态且无法读取容器状态，正在继续确认…"
                await _sync_out(record)
            _refresh_history(record)
            # 关键：不要就此停下。拉大镜像往往超过轮询窗口，这里继续后台回查
            # 容器真实状态，一旦确认成功就补发成功通知——否则用户只会看到
            # 「待确认」，永远不知道其实已经更新成功了。
            rid = str(record.get("id") or "")
            if rid and rid not in _PENDING_CONFIRM:
                _PENDING_CONFIRM[rid] = asyncio.create_task(
                    _confirm_watch(device, record, cfg))
            if cfg.get("auto_update_success", True) and not final_notified \
                    and str(record.get("status") or "") != "未确认":
                await notify_update(record)
    finally:
        _refresh_history(record)


def _refresh_history(record: dict):
    # 用户手动删除过的记录不再被后台轮询写回
    if record.get("id") in DELETED_HISTORY_IDS:
        return
    h = _history()
    for i, r in enumerate(h):
        if r.get("id") == record["id"]:
            h[i] = record
            break
    else:
        h.insert(0, record)
    _save(HISTORY_FILE, h[:2000])


@app.post("/api/devices/{device_id}/update")
async def update_containers(device_id: str, request: Request):
    _check_login(request)
    device = next((d for d in _devices() if d["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "设备不存在")
    body = await request.json()
    ids = body.get("containerIds") or []
    if not ids:
        raise HTTPException(400, "未选择容器")
    async with httpx.AsyncClient() as client:
        containers = _container_array(await dc_get(client, device, "/api/containers"))
        name_by_id = {c.get("id") or c.get("name"): c for c in containers}
        results = []
        for cid in ids:
            c = name_by_id.get(cid) or {}
            image = (c.get("image") or "").split(":")[0]
            record = {
                "id": secrets.token_hex(6),
                "time": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                "device": device["name"],
                "container": c.get("name") or cid,
                "image": image,
                "status": "进行中",
            }
            _add_history(record)
            # 面板自身容器：更新自己会先停掉自己，必然中断并被误判失败。
            # 未开启「允许面板更新自身」时直接跳过，避免误判失败死循环。
            if _is_self_container(c) and not _config().get("allow_self_update"):
                record["status"] = "已跳过"
                record["message"] = ("面板自身容器，不能由面板更新（会中断自身并被误判失败）。"
                                     "请在宿主机执行：docker compose pull && docker compose up -d")
                _refresh_history(record)
                results.append({"id": cid, "name": record["container"],
                                "ok": False, "error": record["message"]})
                continue
            try:
                payload, task_id = await dc_update(client, device, c)
                record["message"] = str(payload.get("msg") or payload.get("message") or "更新任务已创建")
                record["taskID"] = task_id
                _refresh_history(record)
                results.append({"id": cid, "name": record["container"], "ok": True, "taskID": task_id, "recordId": record["id"]})
                if task_id:
                    asyncio.create_task(_poll_progress(device, task_id, record, _automation_config()))
            except Exception as e:
                record["status"] = "失败"
                record["message"] = str(e)
                _refresh_history(record)
                results.append({"id": cid, "name": record["container"], "ok": False, "error": str(e)})
    return results


@app.get("/api/automation")
async def get_automation(request: Request):
    _check_login(request)
    cfg = _automation_config()
    devices = []
    async with httpx.AsyncClient() as client:
        for d in _devices():
            item = {"id": d["id"], "name": d["name"], "containers": [], "selected": _automation_devices().get(d["id"], {})}
            try:
                data = await dc_get(client, d, "/api/containers")
                arr = _container_array(data)
                item["containers"] = [{"name": c.get("name") or c.get("id"), "haveUpdate": _is_true(c.get("haveUpdate"))} for c in arr]
            except Exception:
                pass
            devices.append(item)
    return {"config": cfg, "devices": devices}


@app.put("/api/automation")
async def set_automation(request: Request):
    _check_login(request)
    body = await request.json()
    incoming = body.get("config") or {}
    cfg = _automation_config()
    cron_errors = []
    for k in AUTOMATION_DEFAULTS:
        if k in incoming:
            cfg[k] = incoming[k]
    cfg["enabled"] = bool(cfg.get("enabled"))
    for key in ("update_notify", "auto_update_notify", "auto_update_success", "schedule_report", "delete_images", "self_update_notify", "allow_self_update"):
        cfg[key] = bool(cfg.get(key))
    cfg["interval"] = max(1, int(cfg.get("interval") or 10))
    cfg["interval_limit"] = max(1, int(cfg.get("interval_limit") or 6))
    try:
        cfg["self_check_interval"] = max(1, int(cfg.get("self_check_interval")))
    except (TypeError, ValueError):
        cfg["self_check_interval"] = 5
    for key in ("update_cron", "auto_update_cron"):
        expr = str(cfg.get(key) or "").strip()
        if expr:
            fields = expr.split()
            if len(fields) != 5 or not all(any(ch.isdigit() or ch == "*" for ch in part) for part in fields):
                cron_errors.append(f"{key} 不是有效的 5 段 cron")
    if cron_errors:
        raise HTTPException(400, "；".join(cron_errors))
    devices = {}
    valid_devices = {str(d.get("id")): d for d in _devices()}
    for did, selected in (body.get("devices") or {}).items():
        if not isinstance(selected, dict) or str(did) not in valid_devices:
            continue
        devices[str(did)] = {
            "update": [str(x) for x in (selected.get("update") or [])],
            "auto_update": [str(x) for x in (selected.get("auto_update") or [])],
        }
    data = _config()
    data["automation"] = cfg
    # allow_self_update 是面板级开关（拦截逻辑读顶层配置），与 automation 子配置
    # 分开存，避免只在 automation 子项里、而拦截处读不到导致开关失效。
    data["allow_self_update"] = bool(cfg.get("allow_self_update"))
    data["automation_devices"] = devices
    _save_config(data)
    return {"ok": True, "config": cfg, "devices": devices}


@app.post("/api/automation/run")
async def run_automation(request: Request):
    _check_login(request)
    await _scan_updates(auto=False)
    return {"ok": True, "message": "已立即检查所有设备"}


@app.get("/api/history")
async def get_history(request: Request, days: int = 30):
    _check_login(request)
    tz = timezone(timedelta(hours=8))
    start = (datetime.now(tz).date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    h = [r for r in _history() if r.get("time", "")[:10] >= start]
    return {"start": start, "records": h[:300]}


async def _sync_out(record: dict):
    """把终态结果回推给 MoviePilot 的 DC助手V3 插件，两边显示同一份状态。

    双向同步的另一半：面板触发的更新由面板推给插件，插件触发的由插件推给面板。
    只在设置里填了插件地址时才推；只装了其中一个的话不填即可，互不影响。
    """
    cfg = _config()
    # 优先用设置里填的，没填则退回环境变量（compose 里直接配也行）
    url = str(cfg.get("plugin_sync_url") or os.environ.get("PLUGIN_SYNC_URL") or "").strip().rstrip("/")
    if not url:
        return False
    status = str(record.get("status") or "")
    # 只同步终态，避免把「进行中」反复推过去
    if status not in ("成功", "失败", "已跳过", "未确认", "等待中", "可更新"):
        return False
    token = str(cfg.get("plugin_sync_token") or os.environ.get("PLUGIN_SYNC_TOKEN") or "").strip()
    payload = {"records": [{
        "id": record.get("id"),
        "time": record.get("time"),
        "device": record.get("device") or record.get("env") or "",
        "container": record.get("container") or "",
        "image": record.get("image") or "",
        "status": status,
        "source": record.get("source") or "DC Panel",
        "message": record.get("message") or "",
    }]}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(
                f"{url}/api/v1/plugin/DockerCopilotHelper/sync_records",
                json=payload,
                headers={"X-API-KEY": token} if token else None,
            )
            ok = 200 <= int(r.status_code) < 300
            if not ok:
                print(f"[sync] 回推插件失败 {r.status_code}", flush=True)
            return ok
    except Exception as e:
        print(f"[sync] 回推插件异常: {e}", flush=True)
        return False


def _sync_token() -> str:
    """插件 ↔ 面板同步用的令牌：首次访问时自动生成并落盘。"""
    cfg = _config()
    tok = str(cfg.get("sync_token") or "").strip()
    if not tok:
        tok = secrets.token_hex(16)
        cfg["sync_token"] = tok
        _save_config(cfg)
    return tok


@app.post("/api/sync/records")
async def sync_records_push(request: Request):
    """接收外部（MoviePilot 的 DC助手V3 插件）上报的更新记录。

    为什么要有这个口子：更新可能由插件触发、也可能由面板触发，两边各记一份就会
    「这边成功那边失败」。插件把结果推过来后，面板就是统一视图——谁触发的、
    结果是什么，两边显示一致。
    """
    body = await request.json()
    token = (request.headers.get("X-Sync-Token") or ""
             or (body.get("token") if isinstance(body, dict) else "") or "")
    if not hmac.compare_digest(str(token), _sync_token()):
        raise HTTPException(401, "同步令牌不匹配")
    incoming = (body or {}).get("records") if isinstance(body, dict) else None
    if not isinstance(incoming, list):
        raise HTTPException(400, "records 必须是数组")
    h = _history()
    by_id = {str(r.get("id")): r for r in h if r.get("id")}
    added = updated = 0
    for rec in incoming:
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or "")
        if not rid:
            rec["id"] = secrets.token_hex(6)
            h.insert(0, rec)
            added += 1
            continue
        rec.setdefault("source", "插件同步")
        if rid in by_id:
            # 以「更终态」为准：成功/失败/已跳过 覆盖 进行中/未确认
            old = by_id[rid]
            if str(old.get("status") or "") in ("进行中", "未确认", "等待中", ""):
                old.update(rec)
                updated += 1
        else:
            h.insert(0, rec)
            added += 1
    h.sort(key=lambda r: str(r.get("time") or ""), reverse=True)
    _save(HISTORY_FILE, h[:500])
    return {"ok": True, "added": added, "updated": updated, "total": len(h[:500])}


@app.get("/api/sync/records")
async def sync_records_pull(request: Request):
    """给插件拉取面板侧的更新记录，用于两边状态对齐。"""
    token = request.headers.get("X-Sync-Token") or request.query_params.get("token") or ""
    if not hmac.compare_digest(str(token), _sync_token()):
        raise HTTPException(401, "同步令牌不匹配")
    days = 7
    try:
        days = int(request.query_params.get("days") or 7)
    except ValueError:
        pass
    tz = timezone(timedelta(hours=8))
    start = (datetime.now(tz).date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    h = [r for r in _history() if r.get("time", "")[:10] >= start]
    return {"start": start, "records": h[:300]}


@app.put("/api/sync/plugin")
async def set_plugin_sync(request: Request):
    """配置回推给插件的地址（MoviePilot 根地址）与 API KEY，用于双向同步。"""
    _check_login(request)
    body = await request.json()
    cfg = _config()
    cfg["plugin_sync_url"] = str((body or {}).get("url") or "").strip()
    cfg["plugin_sync_token"] = str((body or {}).get("token") or "").strip()
    cfg["allow_self_update"] = bool((body or {}).get("allow_self_update",
                                                     cfg.get("allow_self_update")))
    _save_config(cfg)
    return {"ok": True, "url": cfg["plugin_sync_url"],
            "allow_self_update": cfg["allow_self_update"]}


@app.get("/api/sync/token")
async def get_sync_token(request: Request):
    """登录后可查看同步令牌（填到插件设置里）。"""
    _check_login(request)
    return {"token": _sync_token()}


@app.delete("/api/history/{record_id}")
async def delete_history(request: Request, record_id: str):
    """删除单条更新记录。"""
    _check_login(request)
    h = _history()
    left = [r for r in h if r.get("id") != record_id]
    if len(left) == len(h):
        raise HTTPException(404, "记录不存在或已删除")
    _mark_deleted(record_id)
    _save(HISTORY_FILE, left)
    return {"ok": True, "deleted": 1, "remaining": len(left)}


@app.delete("/api/history")
async def clear_history(request: Request, scope: str = "all"):
    """批量清理更新记录。scope: all=全部 / older30=30 天前 / success=成功 / failed=失败"""
    _check_login(request)
    h = _history()
    tz = timezone(timedelta(hours=8))
    if scope == "older30":
        cutoff = (datetime.now(tz).date() - timedelta(days=30)).strftime("%Y-%m-%d")
        left = [r for r in h if r.get("time", "")[:10] >= cutoff]
    elif scope == "success":
        left = [r for r in h if r.get("status") != "成功"]
    elif scope == "failed":
        left = [r for r in h if r.get("status") != "失败"]
    elif scope == "all":
        left = []
    else:
        raise HTTPException(400, "不支持的清理范围")
    left_ids = {str(r.get("id") or "") for r in left}
    for r in h:
        rid = str(r.get("id") or "")
        if rid not in left_ids:
            _mark_deleted(rid)
    _save(HISTORY_FILE, left)
    return {"ok": True, "deleted": len(h) - len(left), "remaining": len(left)}


@app.get("/api/stats")
async def stats(request: Request):
    _check_login(request)
    tz = timezone(timedelta(hours=8))
    start = (datetime.now(tz).date() - timedelta(days=29)).strftime("%Y-%m-%d")
    by_device, total = {}, 0
    for r in _history():
        day = r.get("time", "")[:10]
        if day < start:
            continue
        total += 1
        d = r.get("device", "未知")
        by_device[d] = by_device.get(d, 0) + 1
    items = sorted(by_device.items(), key=lambda x: -x[1])
    return {"start": start, "end": datetime.now(tz).strftime("%Y-%m-%d"), "total": total,
            "devices": [{"name": k, "count": v, "pct": round(v * 100 / total, 1) if total else 0} for k, v in items]}


# ---------- 数据迁移（与 MP 插件 dc-helper 格式互通） ----------

@app.get("/api/export")
async def export_config(request: Request):
    _check_login(request)
    selections = _automation_devices()
    devices = [{
        "name": d["name"],
        "host": d["url"].split("://", 1)[-1],  # 去掉 http(s):// 前缀，与 MP 插件一致
        "secret_key": d["secretKey"],
        "updatable": list((selections.get(d["id"], {}) or {}).get("update") or []),
        "auto_update": list((selections.get(d["id"], {}) or {}).get("auto_update") or []),
    } for d in _devices()]
    automation = _automation_config()
    payload = {
        "schema": "dc-helper",
        "schema_version": 1,
        "exported_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "dc-panel",
        "enabled": bool(automation.get("enabled")),
        "devices": devices,
        "notify": {
            "update_cron": automation.get("update_cron") or "",
            "update_notify": bool(automation.get("update_notify")),
            "auto_update_cron": automation.get("auto_update_cron") or "",
            "auto_update_notify": bool(automation.get("auto_update_notify")),
            "auto_update_success": bool(automation.get("auto_update_success")),
            "schedule_report": bool(automation.get("schedule_report")),
            "delete_images": bool(automation.get("delete_images")),
            "interval": automation.get("interval", 10),
            "interval_limit": automation.get("interval_limit", 6),
            "self_check_interval": automation.get("self_check_interval", 5),
            "self_update_notify": bool(automation.get("self_update_notify", True)),
        },
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(content=data, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="dc-helper-export.json"'})


@app.post("/api/import")
async def import_config(request: Request):
    _check_login(request)
    try:
        data = json.loads(await request.body())
    except Exception as e:
        raise HTTPException(400, f"JSON 解析失败：{e}")
    if not isinstance(data, dict):
        raise HTTPException(400, "内容不是 JSON 对象")
    items = data.get("devices") if isinstance(data.get("devices"), list) else []
    if not items and (data.get("host1") or data.get("hostN")):
        # 旧版扁平格式：host1/secretKey1/name1 ...
        n = 0
        items = []
        while True:
            n += 1
            h = str(data.get(f"host{n}") or "").strip()
            if not h:
                break
            items.append({"name": data.get(f"name{n}"), "host": h, "secret_key": data.get(f"secretKey{n}")})
    devices = _devices()
    existing_automation = _automation_devices()
    imported_automation = data.get("notify") if isinstance(data.get("notify"), dict) else {}
    automation = _automation_config()
    for source_key, target_key in (("update_cron", "update_cron"), ("update_notify", "update_notify"),
                                   ("auto_update_cron", "auto_update_cron"), ("auto_update_notify", "auto_update_notify"),
                                   ("auto_update_success", "auto_update_success"), ("schedule_report", "schedule_report"),
                                   ("delete_images", "delete_images"), ("interval", "interval"), ("interval_limit", "interval_limit")):
        if source_key in imported_automation:
            automation[target_key] = imported_automation[source_key]
    if "enabled" in data:
        automation["enabled"] = bool(data.get("enabled"))
    added, skipped = 0, 0
    imported_selections = []
    for item in items:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or item.get("url") or "").strip()
        key = str(item.get("secret_key") or item.get("secretKey") or "").strip()
        if not host or not key:
            continue
        url = host if host.startswith("http") else "http://" + host
        url = url.rstrip("/")
        found = next((d for d in devices if d["url"].rstrip("/") == url), None)
        if found:
            skipped += 1
            did = found["id"]
        else:
            did = secrets.token_hex(4)
            devices.append({"id": did, "name": str(item.get("name") or "").strip() or f"DC{added+1}",
                            "url": url, "secretKey": key})
            added += 1
        imported_selections.append((did, item))
    if not added and not skipped:
        raise HTTPException(400, "文件里没有有效设备（host / secret_key 为空）")
    for did, item in imported_selections:
        existing_automation[did] = {
            "update": [str(x) for x in (item.get("updatable") or item.get("update") or [])],
            "auto_update": [str(x) for x in (item.get("auto_update") or [])],
        }
    cfg = _config()
    cfg["automation"] = automation
    cfg["automation_devices"] = existing_automation
    _save_devices(devices)
    _save_config(cfg)
    return {"ok": True, "added": added, "skipped": skipped}


# ---------- 通知推送（多通道，独立于 MP） ----------

async def _with_proxy(coro_fn, proxy: str):
    kw = {"proxy": proxy.strip()} if (proxy or "").strip() else {}
    async with httpx.AsyncClient(timeout=15, **kw) as client:
        return await coro_fn(client)


async def _send_wecom(cfg, text):
    async with httpx.AsyncClient(timeout=10, **({"proxy": cfg["proxy"]} if cfg.get("proxy") else {})) as c:
        r = await c.post(cfg["webhook"].strip(), json={"msgtype": "text", "text": {"content": text[:2000]}})
    j = r.json() if r.content else {}
    if r.status_code != 200 or j.get("errcode") not in (0, None):
        raise Exception(f"HTTP {r.status_code} {r.text[:180]}")


async def _send_dingtalk(cfg, text):
    url = cfg["webhook"].strip()
    secret = (cfg.get("secret") or "").strip()
    if secret:
        # 钉钉加签：timestamp+换行+secret 作 key 的 HMAC-SHA256，base64 后附到 URL
        ts = str(round(time.time() * 1000))
        sign = base64.b64encode(hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest())
        url += f"&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json={"msgtype": "text", "text": {"content": text[:2000]}})
    j = r.json() if r.content else {}
    if r.status_code != 200 or j.get("errcode") not in (0, None):
        raise Exception(f"HTTP {r.status_code} {r.text[:180]}")


async def _send_feishu(cfg, text):
    url = cfg["webhook"].strip()
    payload = {"msg_type": "text", "content": {"text": text[:3000]}}
    secret = (cfg.get("secret") or "").strip()
    if secret:
        # 飞书加签：timestamp+换行+secret 作 key、空串作内容的 HMAC-SHA256，放 body
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest())
        payload["timestamp"] = ts
        payload["sign"] = sign.decode()
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(url, json=payload)
    j = r.json() if r.content else {}
    code = j.get("code", j.get("StatusCode", 0))
    if r.status_code != 200 or code not in (0, None):
        raise Exception(f"HTTP {r.status_code} {r.text[:180]}")


async def _send_serverchan(cfg, text):
    key = cfg["sendkey"].strip()
    url = f"https://sctapi.ftqq.com/{key}.send"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, data={"title": text.split("\n")[0][:60], "desp": text})
    j = r.json() if r.content else {}
    if r.status_code != 200 or j.get("code") not in (0, None):
        raise Exception(f"HTTP {r.status_code} {r.text[:180]}")


async def _send_telegram(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['bot_token'].strip()}/sendMessage"
    kw = {"proxy": cfg["proxy"].strip()} if (cfg.get("proxy") or "").strip() else {}
    async with httpx.AsyncClient(timeout=15, **kw) as c:
        r = await c.post(url, json={"chat_id": cfg["chat_id"].strip(), "text": text[:4000],
                                    "disable_web_page_preview": True})
    j = r.json() if r.content else {}
    if r.status_code != 200 or not j.get("ok"):
        raise Exception(f"HTTP {r.status_code} {r.text[:180]}")


async def _send_email(cfg, text):
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    def _do():
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header("DC Panel 通知", "utf-8")
        msg["From"] = cfg["smtp_user"]
        msg["To"] = cfg["mail_to"]
        host, port = cfg["smtp_host"].strip(), int(cfg.get("smtp_port") or 465)
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_pass"])
        s.sendmail(cfg["smtp_user"], [x.strip() for x in cfg["mail_to"].split(",") if x.strip()], msg.as_string())
        s.quit()
    await asyncio.get_event_loop().run_in_executor(None, _do)


async def _send_custom(cfg, text):
    import json as _j
    method = (cfg.get("http_method") or "POST").upper()
    headers = {"Content-Type": "application/json"}
    if (cfg.get("http_headers") or "").strip():
        try:
            headers.update(_j.loads(cfg["http_headers"]))
        except Exception:
            pass
    kw = {"proxy": cfg["proxy"].strip()} if (cfg.get("proxy") or "").strip() else {}
    async with httpx.AsyncClient(timeout=15, **kw) as c:
        if method == "GET":
            r = await c.get(cfg["webhook"].strip(), headers=headers,
                            params={"title": "DC Panel", "content": text})
        else:
            r = await c.post(cfg["webhook"].strip(), headers=headers, json={"title": "DC Panel", "content": text})
    if r.status_code >= 300:
        raise Exception(f"HTTP {r.status_code} {r.text[:180]}")


CHANNELS = [
    # (id, 名称, 需要的字段, 发送函数, 是否支持代理)
    ("wecom", "企业微信机器人", ["webhook"], _send_wecom, False),
    ("dingtalk", "钉钉机器人", ["webhook"], _send_dingtalk, False),
    ("feishu", "飞书机器人", ["webhook"], _send_feishu, False),
    ("tg", "Telegram", ["bot_token", "chat_id"], _send_telegram, True),
    ("serverchan", "Server酱", ["sendkey"], _send_serverchan, True),
    ("email", "邮件", ["smtp_host", "smtp_port", "smtp_user", "smtp_pass", "mail_to"], _send_email, False),
    ("custom", "自定义 Webhook", ["webhook"], _send_custom, True),
]
CHANNEL_MAP = {c[0]: c for c in CHANNELS}


async def _dispatch(channel_id: str, text: str):
    """按通道发送，配置取 panel.json 里的 notify[channel_id]"""
    item = CHANNEL_MAP.get(channel_id)
    if not item:
        raise Exception("未知通道")
    cfg = ( _config().get("notify") or {}).get(channel_id) or {}
    _, name, fields, fn, _p = item
    missing = [f for f in fields if not (cfg.get(f) or "").strip()]
    if missing:
        raise Exception(f"{name} 未配置完整，缺少：{', '.join(missing)}")
    await fn(cfg, text)


DEFAULT_TEMPLATE = ("{emoji} DC Panel 容器更新{status}\n"
                    "设备：{device}\n容器：{container}\n镜像：{image}\n时间：{time}"
                    "{extra}")


def _render_text(record: dict) -> str:
    tpl = ((_config().get("notify") or {}).get("template") or "").strip() or DEFAULT_TEMPLATE
    ok = record.get("status") == "成功"
    extra = ""
    if record.get("message"):
        extra += f"\n详情：{record['message']}"
    if record.get("progress"):
        extra += f"\n进度：{record['progress']}"
    fields = {
        "emoji": "✅" if ok else "❌",
        "status": "成功" if ok else "失败",
        "device": record.get("device", ""),
        "container": record.get("container", ""),
        "image": record.get("image", ""),
        "time": record.get("time", ""),
        "extra": extra,
        "message": record.get("message", ""),
        "progress": record.get("progress", ""),
    }
    class _Safe(dict):
        def __missing__(self, k):
            return "{" + k + "}"
    try:
        return tpl.format_map(_Safe(fields))
    except Exception:
        return DEFAULT_TEMPLATE.format_map(_Safe(fields))


# 通知去重：同一容器 + 同一状态在窗口内只推一次，避免自动更新每轮重复轰炸。
# 状态发生变化（进行中 → 成功/失败/已跳过）时立即放行，不吞掉真正的状态流转。
_NOTIFY_LAST: dict = {}
_NOTIFY_DEDUPE_WINDOW = 30 * 60


def _notify_dup(key: str, status: str) -> bool:
    now = time.time()
    prev = _NOTIFY_LAST.get(key)
    if prev and prev[1] == status and now - prev[0] < _NOTIFY_DEDUPE_WINDOW:
        return True
    _NOTIFY_LAST[key] = (now, status)
    return False


# ---------- 通知聚合（同类结果合并成一条） ----------
# 一轮扫描会为每个容器各产生一条结果（可更新 / 已触发 / 成功 / 失败）。容器多的
# 设备一轮就是几十条，逐条推送会在手机 WebPush 和企业微信机器人里堆成「一堆提示」。
# 这里把短窗口内的通知收集起来合并成一条汇总再发；窗口内只有一条时，仍按用户的
# 自定义模板渲染，行为与以前保持一致。
NOTIFY_BATCH_WINDOW = max(1.0, float(os.environ.get("PANEL_NOTIFY_BATCH_WINDOW") or 8))
NOTIFY_BATCH_MAX_LINES = 15
NOTIFY_BATCH_MAX_CHARS = 1600
_NOTIFY_QUEUE: list = []
_NOTIFY_FLUSH_TASK = None

_STATUS_ICON = {
    "成功": "✅", "失败": "❌", "可更新": "🔔", "已触发": "🚀",
    "进度": "⏳", "已跳过": "⏭️", "未确认": "❓", "等待中": "⌛",
}


def _progress_pct(text) -> int:
    """从进度文案里取百分比（取不到返回 -1）。"""
    m = re.search(r"(\d{1,3})\s*%", str(text or ""))
    if not m:
        return -1
    try:
        return max(0, min(100, int(m.group(1))))
    except ValueError:
        return -1


def _notify_enqueue(record: dict):
    """把一条通知放进聚合队列，由后台任务按窗口合并后统一推送。"""
    _NOTIFY_QUEUE.append(record)
    global _NOTIFY_FLUSH_TASK
    try:
        if _NOTIFY_FLUSH_TASK is None or _NOTIFY_FLUSH_TASK.done():
            _NOTIFY_FLUSH_TASK = asyncio.get_event_loop().create_task(_notify_flush_loop())
    except RuntimeError:
        # 没有运行中的事件循环（如单测直接调用）时直接同步发出去
        _NOTIFY_QUEUE.pop()


async def _notify_flush_loop():
    while True:
        await asyncio.sleep(NOTIFY_BATCH_WINDOW)
        try:
            await _notify_flush()
        except Exception as e:
            print(f"[notify] 汇总推送失败: {e}", flush=True)


async def _notify_flush():
    """把队列里累积的通知合并后推送一次。"""
    if not _NOTIFY_QUEUE:
        return
    records = _NOTIFY_QUEUE[:]
    _NOTIFY_QUEUE.clear()
    text = _render_text(records[0]) if len(records) == 1 else _render_batch(records)
    await _notify_send(text)


def _render_batch(records: list) -> str:
    """多条结果合并成一条汇总（按设备分组，超行数/字符数自动截断）。"""
    by_dev: dict = {}
    for r in records:
        by_dev.setdefault(str(r.get("device") or "未知设备"), []).append(r)
    lines = [f"【DC Panel】{len(records)} 条更新动态"]
    shown = 0
    for dev, recs in by_dev.items():
        lines.append(f"—— {dev}（{len(recs)} 条）")
        for r in recs:
            if shown >= NOTIFY_BATCH_MAX_LINES:
                break
            status = str(r.get("status") or "")
            line = f"{_STATUS_ICON.get(status, '•')} {r.get('container', '')} · {status}"
            extra = str(r.get("message") or "").strip().replace("\n", " ")
            if extra:
                line += f"｜{extra[:32]}"
            lines.append(line)
            shown += 1
        if shown >= NOTIFY_BATCH_MAX_LINES:
            break
    rest = len(records) - shown
    if rest > 0:
        lines.append(f"…另有 {rest} 条，详见面板「更新历史」")
    text = "\n".join(lines)
    if len(text) > NOTIFY_BATCH_MAX_CHARS:
        text = text[:NOTIFY_BATCH_MAX_CHARS - 18] + "\n…（内容过长已截断）"
    return text


async def _notify_send(text: str):
    """按已启用的通道推送一段文本（含手机 WebPush）。"""
    notify = _config().get("notify") or {}
    loop = asyncio.get_event_loop()
    for cid, item in CHANNEL_MAP.items():
        c = notify.get(cid) or {}
        if not c.get("enabled"):
            continue
        loop.create_task(_safe_notify(item[1], _dispatch(cid, text)))
    if _push_subs():
        loop.create_task(_safe_notify("Web Push", push_webpush_all(text)))


async def flush_pending_notifications():
    """退出前把队列里还没发出去的通知冲掉，避免丢结果。"""
    try:
        await _notify_flush()
    except Exception:
        pass


async def notify_update(record: dict):
    """更新结果通知入口：去重 + 自身容器拦截 → 进入聚合队列统一推送。"""
    # 同一容器同一结果 30 分钟内只推一次：自动更新按分钟级调度，一旦某台设备
    # 持续处于同一状态就会被反复推送，形成「连续发一堆」。
    dedupe_key = f"{record.get('device', '')}::{record.get('container', '')}"
    if _notify_dup(dedupe_key, str(record.get("status") or "")):
        print(f"[notify] 去重跳过：{dedupe_key} status={record.get('status')}", flush=True)
        return
    # 兜底硬闸：默认情况下（未开启「允许面板更新自身」），面板自身的容器
    # 一律不推「失败」——面板更新自己必然先把自己停掉，进度永远读不到，
    # 任何「失败」都是误报，而且会每轮反复推送。这里在推送出口再挡一次，
    # 避免上游漏判。一旦用户主动开启自更新，则放行真实失败以便排查。
    if (str(record.get("status") or "") == "失败" and _is_self_name(
            str(record.get("container") or ""), str(record.get("image") or ""))
            and not _config().get("allow_self_update")):
        record["status"] = "已跳过"
        record["message"] = ("面板自身容器不能由面板更新（会中断自身并被误判失败）。"
                             "请在宿主机执行：docker compose pull && docker compose up -d")
        record.pop("progress", None)
        print(f"[notify] 已拦截面板自身容器的失败推送：{record.get('container')}", flush=True)
    _notify_enqueue(record)


async def _safe_notify(name: str, coro):
    try:
        await coro
    except Exception as e:
        print(f"[notify] {name} 推送失败: {e}")


@app.get("/api/notify")
async def get_notify(request: Request):
    _check_login(request)
    n = _config().get("notify") or {}
    return {"template": n.get("template", ""), **{cid: n.get(cid) or {} for cid in CHANNEL_MAP}}


@app.put("/api/notify")
async def set_notify(request: Request):
    _check_login(request)
    body = await request.json()
    cfg = _config()
    notify = {"template": (body.get("template") or "").strip()}
    for cid in CHANNEL_MAP:
        incoming = body.get(cid) or {}
        notify[cid] = {k: (str(v).strip() if isinstance(v, str) else v) for k, v in incoming.items()}
        notify[cid]["enabled"] = bool(incoming.get("enabled"))
    cfg["notify"] = notify
    _save_config(cfg)
    return {"ok": True}


@app.post("/api/notify/test")
async def test_notify(request: Request):
    _check_login(request)
    body = await request.json()
    channel = body.get("channel") or ""
    try:
        if channel == "__push__":
            if not _push_subs():
                raise Exception("还没有浏览器订阅 Web Push")
            await push_webpush_all("🔔 DC Panel 测试消息\n通知配置成功，容器更新结果将推送至此。")
            return {"ok": True}
        await _dispatch(channel, "🔔 DC Panel 测试消息\n通知通道配置成功，更新结果将推送至此。")
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------- Web Push（浏览器推送，独立于任何平台） ----------

PUSH_SUBS_FILE = DATA_DIR / "push_subs.json"


# VAPID claims 里的 sub 必须是 mailto: 或 https: 开头的 URL（py_vapid 新版强校验）
VAPID_SUB = os.environ.get("PANEL_VAPID_SUB", "mailto:admin@example.com")


def _vapid_keys(cfg: dict) -> tuple:
    """获取或生成 VAPID 密钥对，返回 (public_b64, private_b64)

    public  = P-256 未压缩点原始字节（65B）base64url —— 前端 applicationServerKey 用
    private = PKCS8 DER base64url —— pywebpush Vapid.from_der 用
    """
    keys = cfg.get("vapid") or {}
    if keys.get("public") and keys.get("private"):
        return keys["public"], keys["private"]
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    priv_der = priv.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    pub = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(priv_der).rstrip(b"=").decode()
    cfg["vapid"] = {"public": pub, "private": p}
    _save_config(cfg)
    return pub, p


def _push_subs() -> list:
    return _load(PUSH_SUBS_FILE, [])


def _save_subs(subs: list):
    _save(PUSH_SUBS_FILE, subs[:50])


@app.get("/api/push/vapid")
async def push_vapid():
    """VAPID 公钥（前端订阅用，无需登录）"""
    pub, _ = _vapid_keys(_config())
    return {"public": pub}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    sub = await request.json()
    if not sub.get("endpoint"):
        raise HTTPException(400, "无效的订阅信息")
    subs = [s for s in _push_subs() if s.get("endpoint") != sub.get("endpoint")]
    subs.append(sub)
    _save_subs(subs)
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    sub = await request.json()
    _save_subs([s for s in _push_subs() if s.get("endpoint") != sub.get("endpoint")])
    return {"ok": True}


@app.get("/api/push/status")
async def push_status(request: Request):
    _check_login(request)
    return {"count": len(_push_subs())}


async def push_webpush_all(text: str):
    from pywebpush import webpush, WebPushException
    cfg = _config()
    pub, priv = _vapid_keys(cfg)
    for s in _push_subs():
        def _do():
            try:
                webpush(s, data=text, vapid_private_key=priv,
                        vapid_claims={"sub": VAPID_SUB})
            except WebPushException as e:
                if e.response and e.response.status_code in (404, 410):
                    _save_subs([x for x in _push_subs() if x.get("endpoint") != s.get("endpoint")])
                else:
                    raise
        await asyncio.get_event_loop().run_in_executor(None, _do)


def _ensure_self_signed(cert: Path, key: Path):
    """没有证书时自动生成自签名证书（HTTPS 供 Web Push / Service Worker 使用）"""
    if cert.exists() and key.exists():
        return
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime, ipaddress, socket
    key_obj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dc-panel")])
    sans = [x509.DNSName("dc-panel"), x509.DNSName("localhost")]
    try:
        host_ip = ipaddress.ip_address(socket.gethostbyname(socket.gethostname()))
        if not host_ip.is_loopback:
            sans.append(x509.IPAddress(host_ip))
    except Exception:
        pass
    try:
        for line in open("/etc/hosts", encoding="utf-8", errors="ignore"):
            parts = line.split()
            if len(parts) >= 3 and "docker" in line.lower():
                continue
    except Exception:
        pass
    cert_obj = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key_obj.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
                .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key_obj, hashes.SHA256()))
    cert.write_bytes(cert_obj.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(key_obj.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    try:
        key.chmod(0o600)
    except Exception:
        pass


def main():
    import uvicorn
    ssl_kwargs = {}
    cert = os.environ.get("PANEL_SSL_CERT", "").strip()
    key = os.environ.get("PANEL_SSL_KEY", "").strip()
    if os.environ.get("PANEL_SSL", "").strip().lower() == "auto" and not (cert and key):
        cert, key = str(DATA_DIR / "ssl.crt"), str(DATA_DIR / "ssl.key")
        _ensure_self_signed(Path(cert), Path(key))
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
    elif cert and key:
        ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", **ssl_kwargs)


if __name__ == "__main__":
    main()
