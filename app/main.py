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
DC_IMAGE = "l429609201/dockercopilot"
USERNAME = os.environ.get("PANEL_USERNAME", "admin").strip() or "admin"

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="DC Panel", docs_url=None, redoc_url=None)
serializer: URLSafeTimedSerializer = None  # type: ignore


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
    return {"Authorization": f"Bearer {dc_jwt(secret_key)}",
            "Content-Type": "application/json"}


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


# ---------- 页面 ----------

@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Service Worker 脚本：必须可访问且带正确的 Content-Type / Scope"""
    return FileResponse(
        "static/sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.json", include_in_schema=False)
async def pwa_manifest():
    """PWA 清单（主屏图标、启动画面）"""
    return FileResponse("static/manifest.json",
                        media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/static/{fname}", include_in_schema=False)
async def static_asset(fname: str):
    """静态资源（图标等），只允许白名单内的图片，防目录穿越"""
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]+", fname) or ".." in fname:
        raise HTTPException(404, "not found")
    p = Path("static") / fname
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

HUB_REPO = os.environ.get("PANEL_HUB_REPO", "po9873/dc-panel")
APP_VERSION = "v1.1.0"

def _boot_time():
    try:
        with open("/proc/1/stat") as f:
            fields = f.read().split()
        btime = int(fields[21])
        return datetime.fromtimestamp(btime / 100, tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc) - timedelta(days=3650)

@app.get("/api/version/check")
async def version_check():
    """对照 Docker Hub latest 的构建时间与容器启动时间，判断面板是否有新版。"""
    result = {"current": APP_VERSION, "repo": HUB_REPO, "update": False, "hub_time": None}
    try:
        async with httpx.AsyncClient(timeout=8) as cli:
            r = await cli.get(f"https://hub.docker.com/v2/repositories/{HUB_REPO}/tags/latest")
            if r.status_code == 200:
                d = r.json()
                pushed = d.get("last_updated") or d.get("tag_last_pushed")
                result["hub_time"] = pushed
                if pushed:
                    hub_dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                    result["update"] = hub_dt > _boot_time()
    except Exception:
        pass
    return result


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


# ---------- 更新 ----------

async def _poll_progress(device: dict, task_id: str, record: dict):
    """后台轮询更新进度，写回历史记录"""
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(120):  # 最多 10 分钟
                await asyncio.sleep(5)
                try:
                    r = await dc_request(client, device, "GET", f"/api/progress/{task_id}")
                    p = r.json()
                except Exception:
                    continue
                status = (p.get("status") or "").lower()
                record["progress"] = p.get("progress", record.get("progress", ""))
                if status in ("completed", "success", "done"):
                    record["status"] = "成功"
                    await notify_update(record)
                    break
                if status in ("failed", "error"):
                    record["status"] = "失败"
                    record["message"] = p.get("message", "")
                    await notify_update(record)
                    break
                _refresh_history(record)
        if record["status"] == "进行中":
            record["status"] = "已完成（未确认结果）"
    finally:
        _refresh_history(record)


def _refresh_history(record: dict):
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
        containers = await dc_get(client, device, "/api/containers")
        containers = containers if isinstance(containers, list) else containers.get("containers") or []
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
            try:
                r = await dc_request(client, device, "POST", f"/api/container/{cid}/update")
                task_id = r.json().get("taskID") or r.json().get("taskId") or ""
                results.append({"id": cid, "name": record["container"], "ok": True, "taskID": task_id, "recordId": record["id"]})
                if task_id:
                    asyncio.create_task(_poll_progress(device, task_id, record))
            except Exception as e:
                record["status"] = "失败"
                record["message"] = str(e)
                _refresh_history(record)
                results.append({"id": cid, "name": record["container"], "ok": False, "error": str(e)})
    return results


@app.get("/api/history")
async def get_history(request: Request, days: int = 30):
    _check_login(request)
    tz = timezone(timedelta(hours=8))
    start = (datetime.now(tz).date() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    h = [r for r in _history() if r.get("time", "")[:10] >= start]
    return {"start": start, "records": h[:300]}


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
    devices = [{
        "name": d["name"],
        "host": d["url"].split("://", 1)[-1],  # 去掉 http(s):// 前缀，与 MP 插件一致
        "secret_key": d["secretKey"],
        "updatable": [],
        "auto_update": [],
    } for d in _devices()]
    payload = {
        "schema": "dc-helper",
        "schema_version": 1,
        "exported_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "dc-panel",
        "devices": devices,
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
    added, skipped = 0, 0
    for item in items:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or item.get("url") or "").strip()
        key = str(item.get("secret_key") or item.get("secretKey") or "").strip()
        if not host or not key:
            continue
        url = host if host.startswith("http") else "http://" + host
        url = url.rstrip("/")
        if any(d["url"].rstrip("/") == url for d in devices):
            skipped += 1
            continue
        devices.append({"id": secrets.token_hex(4), "name": str(item.get("name") or "").strip() or f"DC{added+1}",
                        "url": url, "secretKey": key})
        added += 1
    if not added and not skipped:
        raise HTTPException(400, "文件里没有有效设备（host / secret_key 为空）")
    _save_devices(devices)
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


async def notify_update(record: dict):
    """更新完成后按已启用的通道推送（文本内容按自定义模板渲染）"""
    notify = _config().get("notify") or {}
    text = _render_text(record)
    loop = asyncio.get_event_loop()
    for cid, item in CHANNEL_MAP.items():
        c = notify.get(cid) or {}
        if not c.get("enabled"):
            continue
        loop.create_task(_safe_notify(item[1], _dispatch(cid, text)))
    if _push_subs():
        loop.create_task(_safe_notify("Web Push", push_webpush_all(text)))


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
