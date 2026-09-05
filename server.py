"""
AirTransfer - Mac 本地文件接收服务器
基于 FastAPI + WebSocket 实现高速文件传输
"""

import os
import sys
import json
import ssl
import time
import uuid
import random
import string
import socket
import hashlib
import logging
import asyncio
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ============================================================
# 全局配置
# ============================================================

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_FILE = BASE_DIR / "config.json"

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "transfer.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("airtransfer")


def load_config() -> dict:
    """加载配置文件，不存在则使用默认值"""
    default = {
        "port": 8899,
        "save_dir": "~/Downloads/AirTransfer",
        "auto_subfolder": False,
        "conflict_strategy": "rename",
        "pair_code": None,
        "enable_ssl": False,
        "enable_notification": True,
        "max_concurrent_uploads": 5,
        "theme": "dark"
    }
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            default.update(user_cfg)
        except Exception as e:
            logger.warning(f"配置文件读取失败，使用默认配置: {e}")
    return default


config = load_config()

# 命令行参数覆盖配置
parser = argparse.ArgumentParser(description="AirTransfer 文件传输服务器")
parser.add_argument("--port", type=int, default=None, help="服务端口")
parser.add_argument("--dir", type=str, default=None, help="文件保存目录")
parser.add_argument("--no-auth", action="store_true", help="跳过配对验证")
parser.add_argument("--no-ssl", action="store_true", help="禁用 SSL")
parser.add_argument("--tunnel", type=str, default=None, help="内网穿透 (cloudflare)，无需配置")
args, _ = parser.parse_known_args()

if args.port:
    config["port"] = args.port
if args.dir:
    config["save_dir"] = args.dir
if args.no_auth:
    config["pair_code"] = "000000"
    config["no_auth"] = True
if args.no_ssl:
    config["enable_ssl"] = False
if args.tunnel:
    config["tunnel"] = args.tunnel

# 全局隧道 URL
TUNNEL_URL: Optional[str] = None

# 展开保存目录路径
SAVE_DIR = Path(os.path.expanduser(config["save_dir"])).resolve()
SAVE_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# 配对码 & 会话管理
# ============================================================

# 生成6位随机配对码
if config.get("pair_code"):
    PAIR_CODE = config["pair_code"]
else:
    PAIR_CODE = "".join(random.choices(string.digits, k=6))

# 会话存储: {token: {"created": timestamp, "device": str}}
active_sessions: dict[str, dict] = {}

# 暴力破解防护: {"ip": {"failures": int, "locked_until": float}}
auth_attempts: dict[str, dict] = {}

MAX_AUTH_FAILURES = 5
LOCKOUT_DURATION = 300  # 5分钟

# WebSocket 连接管理
ws_connections: dict[str, WebSocket] = {}


def generate_token() -> str:
    """生成安全的会话 token"""
    return uuid.uuid4().hex + uuid.uuid4().hex


def check_auth(ip: str) -> tuple[bool, str]:
    """检查 IP 是否被锁定"""
    record = auth_attempts.get(ip)
    if not record:
        return True, ""
    if record["failures"] >= MAX_AUTH_FAILURES:
        remaining = record["locked_until"] - time.time()
        if remaining > 0:
            return False, f"已锁定，请在 {int(remaining)} 秒后重试"
        else:
            record["failures"] = 0
    return True, ""


def record_auth_failure(ip: str):
    """记录认证失败"""
    if ip not in auth_attempts:
        auth_attempts[ip] = {"failures": 0, "locked_until": 0}
    auth_attempts[ip]["failures"] += 1
    auth_attempts[ip]["locked_until"] = time.time() + LOCKOUT_DURATION


def verify_token(token: str) -> bool:
    """验证会话 token 是否有效"""
    return token in active_sessions


def get_client_ip(request: Request) -> str:
    """获取客户端真实 IP"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ============================================================
# 文件冲突处理
# ============================================================

def resolve_conflict(filepath: Path) -> Path:
    """处理文件名冲突，返回不冲突的路径"""
    strategy = config.get("conflict_strategy", "rename")

    if not filepath.exists():
        return filepath

    if strategy == "overwrite":
        return filepath
    elif strategy == "skip":
        return None  # 跳过

    # 默认: rename - 自动重命名
    stem = filepath.stem
    suffix = filepath.suffix
    parent = filepath.parent
    counter = 1
    while True:
        new_name = f"{stem}({counter}){suffix}"
        new_path = parent / new_name
        if not new_path.exists():
            return new_path
        counter += 1


def get_save_path(filename: str) -> Path:
    """获取文件保存路径，可选按日期创建子目录"""
    target_dir = SAVE_DIR
    if config.get("auto_subfolder"):
        today = datetime.now().strftime("%Y-%m-%d")
        target_dir = SAVE_DIR / today
        target_dir.mkdir(parents=True, exist_ok=True)

    # 安全检查：防止路径遍历
    safe_name = Path(filename).name
    filepath = target_dir / safe_name
    return resolve_conflict(filepath)


# ============================================================
# 网络工具
# ============================================================

def get_local_ips() -> list[str]:
    """获取本机局域网 IP 地址列表"""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if "." in addr and addr != "127.0.0.1" and not addr.startswith("169.254"):
                if addr not in ips:
                    ips.append(addr)
    except Exception:
        pass

    # 备用方法：通过 UDP 连接获取
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip not in ips:
                ips.insert(0, ip)
        except Exception:
            pass

    return ips if ips else ["127.0.0.1"]


# ============================================================
# 内网穿透（Cloudflare Tunnel）
# ============================================================

import subprocess
import platform
import tarfile
import zipfile
import shutil

# cloudflared 子进程和 URL
_tunnel_process: Optional[subprocess.Popen] = None


def _get_cloudflared_path() -> Path:
    """获取 cloudflared 二进制文件路径"""
    return BASE_DIR / "bin" / "cloudflared"


def _download_cloudflared() -> Optional[Path]:
    """自动下载 cloudflared 二进制文件（支持国内镜像加速）"""
    import urllib.request

    bin_path = _get_cloudflared_path()
    if bin_path.exists():
        return bin_path

    bin_path.parent.mkdir(parents=True, exist_ok=True)

    system = platform.system().lower()
    machine = platform.machine().lower()

    # 确定文件名
    if system == "darwin":
        filename = "cloudflared-darwin-amd64.tgz"
    elif system == "linux":
        if machine in ("aarch64", "arm64"):
            filename = "cloudflared-linux-arm64"
        else:
            filename = "cloudflared-linux-amd64"
    else:
        logger.error(f"不支持的操作系统: {system}")
        return None

    github_path = f"cloudflare/cloudflared/releases/latest/download/{filename}"

    # 下载源列表：直连 + 国内镜像，依次尝试
    mirrors = [
        f"https://github.com/{github_path}",
        f"https://ghfast.top/https://github.com/{github_path}",
        f"https://gh-proxy.com/https://github.com/{github_path}",
        f"https://mirror.ghproxy.com/https://github.com/{github_path}",
    ]

    print(f"  ⏳ 正在下载 cloudflared...")
    logger.info(f"下载 cloudflared: {filename}")

    tmp_path = bin_path.parent / "cloudflared_tmp"

    for url in mirrors:
        try:
            logger.info(f"尝试: {url[:60]}...")
            urllib.request.urlretrieve(url, str(tmp_path))
            # 验证下载是否成功（至少 1MB）
            if tmp_path.stat().st_size < 1024 * 1024:
                tmp_path.unlink(missing_ok=True)
                continue
            break
        except Exception as e:
            logger.warning(f"下载失败: {e}")
            tmp_path.unlink(missing_ok=True)
            continue
    else:
        print(f"  ❌ 所有下载源均失败")
        print(f"     请手动下载: https://github.com/cloudflare/cloudflared/releases")
        print(f"     放到: {bin_path}")
        return None

    try:
        # 解压 tgz（macOS）
        if filename.endswith(".tgz"):
            with tarfile.open(str(tmp_path), "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name == "cloudflared" or member.name.endswith("/cloudflared"):
                        member.name = "cloudflared"
                        tar.extract(member, str(bin_path.parent))
                        break
            tmp_path.unlink(missing_ok=True)
        else:
            # 直接是二进制文件（Linux）
            tmp_path.rename(bin_path)

        bin_path.chmod(0o755)
        print(f"  ✓ cloudflared 已下载")
        logger.info("cloudflared 下载完成")
        return bin_path

    except Exception as e:
        logger.error(f"cloudflared 解压失败: {e}")
        print(f"  ❌ 解压失败: {e}")
        tmp_path.unlink(missing_ok=True)
        return None


def _find_cloudflared() -> Optional[Path]:
    """查找 cloudflared 二进制（优先本地，其次系统 PATH）"""
    # 1. 项目本地 bin 目录
    local = _get_cloudflared_path()
    if local.exists():
        return local

    # 2. 系统 PATH
    system_path = shutil.which("cloudflared")
    if system_path:
        return Path(system_path)

    return None


def start_tunnel(port: int) -> Optional[str]:
    """启动 Cloudflare Quick Tunnel，返回公网 URL"""
    global _tunnel_process

    if not config.get("tunnel"):
        return None

    # 查找或下载 cloudflared
    cf_path = _find_cloudflared()
    if not cf_path:
        cf_path = _download_cloudflared()
    if not cf_path:
        return None

    print("  ⏳ 正在建立公网隧道...")
    logger.info("启动 Cloudflare Quick Tunnel...")

    try:
        # 启动 cloudflared quick tunnel
        _tunnel_process = subprocess.Popen(
            [str(cf_path), "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # 从 stderr 读取公网 URL（cloudflared 输出到 stderr）
        import re as _re
        import time as _time
        public_url = None
        start = _time.time()

        while _time.time() - start < 30:  # 最多等 30 秒
            line = _tunnel_process.stderr.readline()
            if not line:
                if _tunnel_process.poll() is not None:
                    break
                _time.sleep(0.2)
                continue

            # 用正则提取 https://xxx-xxx-xxx.trycloudflare.com URL
            # 排除 api.trycloudflare.com 等非隧道地址
            match = _re.search(r'https?://[\w]+-[\w.-]+\.trycloudflare\.com', line)
            if match:
                public_url = match.group(0)
                break

        if public_url:
            logger.info(f"Cloudflare 隧道已建立: {public_url}")
            return public_url
        else:
            # 检查进程是否还活着
            if _tunnel_process.poll() is not None:
                stderr = _tunnel_process.stderr.read()
                logger.error(f"cloudflared 启动失败: {stderr}")
                print(f"  ❌ 隧道启动失败")
            else:
                logger.warning("cloudflared 启动超时，未获取到公网 URL")
                print("  ❌ 隧道启动超时")
            return None

    except Exception as e:
        logger.error(f"隧道启动异常: {e}")
        print(f"  ❌ 隧道启动失败: {e}")
        return None


def stop_tunnel():
    """关闭隧道"""
    global _tunnel_process, TUNNEL_URL
    if _tunnel_process:
        try:
            _tunnel_process.terminate()
            _tunnel_process.wait(timeout=5)
            logger.info("Cloudflare 隧道已关闭")
        except Exception:
            try:
                _tunnel_process.kill()
            except Exception:
                pass
        _tunnel_process = None
    TUNNEL_URL = None


# ============================================================
# FastAPI 应用
# ============================================================

import ipaddress

@asynccontextmanager
async def lifespan(app):
    """应用生命周期"""
    global TUNNEL_URL

    # 启动内网穿透
    if config.get("tunnel"):
        TUNNEL_URL = start_tunnel(config["port"])

    print_startup_info()
    logger.info(f"AirTransfer 启动 - 端口:{config['port']} 目录:{SAVE_DIR}")
    if TUNNEL_URL:
        logger.info(f"公网隧道: {TUNNEL_URL}")

    # 启动隧道保活检测
    health_task = None
    if config.get("tunnel"):
        health_task = asyncio.create_task(_tunnel_health_loop())

    yield

    # 关闭
    if health_task:
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass
    stop_tunnel()


async def _tunnel_health_loop():
    """后台轮询：每 30 秒检测隧道进程，挂了自动重启"""
    global TUNNEL_URL
    while True:
        await asyncio.sleep(30)
        try:
            dead = False
            if _tunnel_process is None:
                dead = True
            elif _tunnel_process.poll() is not None:
                dead = True
                logger.warning(f"cloudflared 进程已退出 (code={_tunnel_process.returncode})")

            if dead:
                logger.warning("隧道断开，正在重新连接...")
                print("  ⚠️ 隧道断开，正在重连...")
                stop_tunnel()
                TUNNEL_URL = start_tunnel(config["port"])
                if TUNNEL_URL:
                    logger.info(f"隧道已重连: {TUNNEL_URL}")
                    print(f"  ✓ 隧道已重连: {TUNNEL_URL}")
                    # 广播新 URL 给前端
                    await broadcast_file_event("tunnel_reconnected", {"url": TUNNEL_URL})
                else:
                    logger.error("隧道重连失败，30 秒后重试")
                    print("  ❌ 隧道重连失败，30 秒后重试")
                    await asyncio.sleep(15)  # 失败后多等一会
        except Exception as e:
            logger.error(f"隧道健康检测异常: {e}")
            await asyncio.sleep(15)

app = FastAPI(title="AirTransfer", docs_url=None, redoc_url=None, lifespan=lifespan)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# WebSocket 进度管理
# ============================================================

# 进度推送: {upload_id: {"filename": str, "size": int, "received": int, "speed": float, "ws_id": str}}
upload_progress: dict[str, dict] = {}


async def broadcast_progress(upload_id: str, data: dict):
    """向相关 WebSocket 连接推送进度更新"""
    msg = json.dumps({"type": "progress", "upload_id": upload_id, **data})
    disconnected = []
    for ws_id, ws in ws_connections.items():
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.append(ws_id)
    for ws_id in disconnected:
        ws_connections.pop(ws_id, None)


async def broadcast_file_event(event_type: str, data: dict):
    """广播文件事件（新增/删除等）"""
    msg = json.dumps({"type": event_type, **data})
    disconnected = []
    for ws_id, ws in ws_connections.items():
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.append(ws_id)
    for ws_id in disconnected:
        ws_connections.pop(ws_id, None)


# ============================================================
# 认证路由
# ============================================================

@app.get("/api/check-session")
async def check_session(token: str = Header(None, alias="X-Session-Token")):
    """检查会话是否有效"""
    if not token or not verify_token(token):
        return JSONResponse({"valid": False}, status_code=401)
    return {"valid": True}


@app.post("/api/pair")
async def pair_device(request: Request):
    """设备配对验证"""
    client_ip = get_client_ip(request)

    # 检查是否被锁定
    allowed, msg = check_auth(ip=client_ip)
    if not allowed:
        return JSONResponse({"success": False, "error": msg}, status_code=429)

    body = await request.json()
    code = body.get("code", "")
    device = body.get("device", "Unknown Device")

    if code != PAIR_CODE:
        record_auth_failure(client_ip)
        return JSONResponse({"success": False, "error": "配对码错误"}, status_code=401)

    # 配对成功，生成 token
    token = generate_token()
    active_sessions[token] = {
        "created": time.time(),
        "device": device,
        "ip": client_ip
    }

    logger.info(f"设备配对成功: {device} ({client_ip})")
    return {"success": True, "token": token}


# ============================================================
# 文件上传（流式写入，支持大文件）
# ============================================================

@app.post("/api/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    upload_id: str = Form(default=""),
    relative_path: str = Form(default=""),
    token: str = Header(None, alias="X-Session-Token"),
):
    """接收文件上传，流式写入磁盘"""
    # 验证会话
    if not token or not verify_token(token):
        raise HTTPException(status_code=401, detail="未授权")

    if not upload_id:
        upload_id = uuid.uuid4().hex[:12]

    # 确定保存路径
    if relative_path:
        # 文件夹上传：保留目录结构
        safe_rel = Path(relative_path)
        # 安全检查
        if ".." in safe_rel.parts:
            raise HTTPException(status_code=400, detail="非法路径")
        target_dir = SAVE_DIR
        if config.get("auto_subfolder"):
            today = datetime.now().strftime("%Y-%m-%d")
            target_dir = SAVE_DIR / today
        filepath = target_dir / safe_rel
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath = resolve_conflict(filepath)
    else:
        filepath = get_save_path(file.filename)

    if filepath is None:
        # skip 策略
        await broadcast_progress(upload_id, {
            "filename": file.filename,
            "status": "skipped",
            "progress": 100
        })
        return {"success": True, "filename": file.filename, "status": "skipped"}

    # 获取文件大小
    content_length = request.headers.get("content-length")
    total_size = int(content_length) if content_length else 0

    # 初始化进度
    upload_progress[upload_id] = {
        "filename": file.filename,
        "size": total_size,
        "received": 0,
        "speed": 0,
        "start_time": time.time()
    }

    # 流式写入
    received = 0
    last_broadcast = 0
    start_time = time.time()

    try:
        with open(filepath, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)

                # 每 200ms 推送一次进度
                now = time.time()
                if now - last_broadcast > 0.2:
                    elapsed = now - start_time
                    speed = received / elapsed if elapsed > 0 else 0
                    progress = (received / total_size * 100) if total_size else 0
                    remaining = ((total_size - received) / speed) if speed > 0 and total_size else 0

                    await broadcast_progress(upload_id, {
                        "filename": file.filename,
                        "status": "uploading",
                        "progress": round(progress, 1),
                        "received": received,
                        "total": total_size,
                        "speed": round(speed / 1024 / 1024, 2),  # MB/s
                        "remaining": round(remaining, 1)
                    })
                    last_broadcast = now

    except Exception as e:
        logger.error(f"文件写入失败: {file.filename} - {e}")
        if filepath.exists():
            filepath.unlink()
        await broadcast_progress(upload_id, {
            "filename": file.filename,
            "status": "error",
            "error": str(e)
        })
        raise HTTPException(status_code=500, detail=f"文件保存失败: {e}")

    # 上传完成
    elapsed = time.time() - start_time
    file_size = filepath.stat().st_size
    logger.info(f"文件接收成功: {filepath.name} ({format_size(file_size)}, {elapsed:.1f}s)")

    await broadcast_progress(upload_id, {
        "filename": file.filename,
        "saved_as": filepath.name,
        "status": "completed",
        "progress": 100,
        "size": file_size,
        "elapsed": round(elapsed, 1)
    })

    # 广播新文件事件
    await broadcast_file_event("file_added", {
        "filename": filepath.name,
        "size": file_size,
        "time": datetime.now().isoformat()
    })

    return {
        "success": True,
        "filename": filepath.name,
        "size": file_size,
        "elapsed": round(elapsed, 1)
    }


# ============================================================
# WebSocket 连接
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接，用于实时进度推送"""
    await websocket.accept()
    ws_id = uuid.uuid4().hex[:8]
    ws_connections[ws_id] = websocket
    logger.info(f"WebSocket 连接建立: {ws_id}")

    try:
        while True:
            data = await websocket.receive_text()
            # 心跳
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.info(f"WebSocket 断开: {ws_id}")
    except Exception as e:
        logger.error(f"WebSocket 错误: {ws_id} - {e}")
    finally:
        ws_connections.pop(ws_id, None)


# ============================================================
# 文件管理路由
# ============================================================

@app.get("/api/files")
async def list_files(token: str = Header(None, alias="X-Session-Token")):
    """列出已接收的文件"""
    if not token or not verify_token(token):
        raise HTTPException(status_code=401, detail="未授权")

    files = []
    if SAVE_DIR.exists():
        for item in sorted(SAVE_DIR.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if item.is_file() and not item.name.startswith("."):
                stat = item.stat()
                rel_path = item.relative_to(SAVE_DIR)
                files.append({
                    "name": item.name,
                    "path": str(rel_path),
                    "size": stat.st_size,
                    "size_formatted": format_size(stat.st_size),
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "type": get_file_type(item.name),
                    "extension": item.suffix.lower()
                })

    return {"files": files, "total": len(files), "save_dir": str(SAVE_DIR)}


@app.delete("/api/files/{file_path:path}")
async def delete_file(file_path: str, token: str = Header(None, alias="X-Session-Token")):
    """删除指定文件"""
    if not token or not verify_token(token):
        raise HTTPException(status_code=401, detail="未授权")

    filepath = (SAVE_DIR / file_path).resolve()
    if not str(filepath).startswith(str(SAVE_DIR)):
        raise HTTPException(status_code=403, detail="禁止访问")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    filename = filepath.name
    filepath.unlink()
    logger.info(f"文件已删除: {filename}")

    await broadcast_file_event("file_deleted", {"filename": filename, "path": file_path})
    return {"success": True, "message": f"已删除: {filename}"}


@app.get("/api/preview/{file_path:path}")
async def preview_file(file_path: str, token: str = Header(None, alias="X-Session-Token")):
    """预览文件（图片、视频、PDF、文本）"""
    if not token or not verify_token(token):
        raise HTTPException(status_code=401, detail="未授权")

    filepath = (SAVE_DIR / file_path).resolve()
    if not str(filepath).startswith(str(SAVE_DIR)):
        raise HTTPException(status_code=403, detail="禁止访问")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    # MIME 类型映射
    ext = filepath.suffix.lower()
    mime_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        ".pdf": "application/pdf",
        ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json",
        ".py": "text/x-python", ".js": "text/javascript", ".html": "text/html",
        ".css": "text/css", ".xml": "text/xml", ".csv": "text/csv",
    }
    mime = mime_types.get(ext, "application/octet-stream")

    return FileResponse(filepath, media_type=mime, filename=filepath.name)


@app.get("/api/thumbnail/{file_path:path}")
async def get_thumbnail(file_path: str, token: str = Header(None, alias="X-Session-Token")):
    """获取图片缩略图"""
    if not token or not verify_token(token):
        raise HTTPException(status_code=401, detail="未授权")

    filepath = (SAVE_DIR / file_path).resolve()
    if not str(filepath).startswith(str(SAVE_DIR)):
        raise HTTPException(status_code=403, detail="禁止访问")

    ext = filepath.suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
        raise HTTPException(status_code=400, detail="非图片文件")

    mime_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    }
    return FileResponse(filepath, media_type=mime_types.get(ext, "image/jpeg"))


# ============================================================
# 系统信息路由
# ============================================================

@app.get("/api/info")
async def server_info():
    """获取服务器信息（不需要认证）"""
    local_ips = get_local_ips()
    return {
        "name": "AirTransfer",
        "version": "1.0.0",
        "ips": local_ips,
        "port": config["port"],
        "ssl": config["enable_ssl"],
        "save_dir": str(SAVE_DIR),
        "no_auth": config.get("no_auth", False),
        "tunnel_url": TUNNEL_URL
    }


@app.get("/api/stats")
async def server_stats(token: str = Header(None, alias="X-Session-Token")):
    """获取服务器统计信息"""
    if not token or not verify_token(token):
        raise HTTPException(status_code=401, detail="未授权")

    total_files = 0
    total_size = 0
    if SAVE_DIR.exists():
        for item in SAVE_DIR.rglob("*"):
            if item.is_file() and not item.name.startswith("."):
                total_files += 1
                total_size += item.stat().st_size

    return {
        "total_files": total_files,
        "total_size": total_size,
        "total_size_formatted": format_size(total_size),
        "active_uploads": len(upload_progress),
        "active_connections": len(ws_connections),
        "save_dir": str(SAVE_DIR)
    }


# ============================================================
# 前端路由
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """提供前端页面"""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>AirTransfer</h1><p>前端文件缺失</p>", status_code=500)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/manifest.json")
async def serve_manifest():
    """PWA manifest"""
    manifest = {
        "name": "AirTransfer",
        "short_name": "AirTransfer",
        "description": "Mac 本地文件传输",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0a0e1a",
        "theme_color": "#667eea",
        "icons": [
            {
                "src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect fill='%23667eea' width='100' height='100' rx='20'/><text x='50' y='68' text-anchor='middle' font-size='50' fill='white'>A</text></svg>",
                "sizes": "any",
                "type": "image/svg+xml"
            }
        ]
    }
    return JSONResponse(manifest)


@app.get("/sw.js")
async def serve_sw():
    """PWA Service Worker"""
    sw_code = """
const CACHE_NAME = 'airtransfer-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
    if (e.request.url.includes('/api/') || e.request.url.includes('/ws')) return;
    e.respondWith(
        caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
            return resp;
        }))
    );
});
"""
    return Response(content=sw_code, media_type="application/javascript")


# ============================================================
# 辅助函数
# ============================================================

def format_size(size: int) -> str:
    """格式化文件大小"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} PB"


def get_file_type(filename: str) -> str:
    """根据文件名判断类型"""
    ext = Path(filename).suffix.lower()
    type_map = {
        ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
        ".webp": "image", ".svg": "image", ".bmp": "image", ".ico": "image",
        ".mp4": "video", ".webm": "video", ".mov": "video", ".avi": "video",
        ".mkv": "video", ".flv": "video", ".wmv": "video",
        ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".aac": "audio",
        ".ogg": "audio", ".wma": "audio", ".m4a": "audio",
        ".pdf": "pdf", ".doc": "document", ".docx": "document",
        ".xls": "spreadsheet", ".xlsx": "spreadsheet", ".ppt": "presentation",
        ".pptx": "presentation",
        ".txt": "text", ".md": "text", ".csv": "text", ".json": "text",
        ".xml": "text", ".html": "text", ".css": "text", ".js": "text",
        ".py": "text", ".java": "text", ".c": "text", ".cpp": "text",
        ".go": "text", ".rs": "text", ".ts": "text",
        ".zip": "archive", ".rar": "archive", ".7z": "archive",
        ".tar": "archive", ".gz": "archive", ".bz2": "archive",
        ".apk": "app", ".ipa": "app", ".exe": "app", ".dmg": "app",
    }
    return type_map.get(ext, "other")


# ============================================================
# SSL 证书生成
# ============================================================

def generate_self_signed_cert():
    """生成自签名 SSL 证书"""
    cert_dir = BASE_DIR / "certs"
    cert_dir.mkdir(exist_ok=True)
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"

    if cert_file.exists() and key_file.exists():
        return str(cert_file), str(key_file)

    # 使用 Python 生成自签名证书（无需 openssl 命令）
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime as dt

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "AirTransfer"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AirTransfer Local"),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime.utcnow())
            .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    *[x509.IPAddress(ipaddress.IPv4Address(ip)) for ip in get_local_ips()],
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        with open(key_file, "wb") as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        with open(cert_file, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        logger.info("自签名 SSL 证书已生成")
        return str(cert_file), str(key_file)

    except ImportError:
        logger.warning("未安装 cryptography 库，无法生成 SSL 证书，将使用 HTTP 模式")
        config["enable_ssl"] = False
        return None, None


# ============================================================
# 二维码显示
# ============================================================

def print_qr_code(url: str):
    """在终端打印二维码"""
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=1, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        # 使用紧凑的终端输出
        qr.print_ascii(invert=True)
    except ImportError:
        logger.info("安装 qrcode 库可显示终端二维码: pip install qrcode")


def print_startup_info():
    """打印启动信息"""
    local_ips = get_local_ips()
    protocol = "https" if config["enable_ssl"] else "http"
    port = config["port"]

    print("\n" + "=" * 50)
    print("  📡 AirTransfer - 文件传输服务器已启动")
    print("=" * 50)
    print(f"\n  📂 保存目录: {SAVE_DIR}")
    print(f"  🔐 配对码:   {PAIR_CODE}")
    print(f"  🔒 SSL:      {'启用' if config['enable_ssl'] else '禁用'}")
    print()

    # 公网隧道地址（优先显示）
    if TUNNEL_URL:
        print(f"  🌍 公网访问: {TUNNEL_URL}")
        print(f"     (手机用流量也能访问)")
        print()
        # 为隧道 URL 生成二维码
        print_qr_code(TUNNEL_URL)
        print()

    for ip in local_ips:
        url = f"{protocol}://{ip}:{port}"
        print(f"  📱 局域网访问: {url}")

    print(f"\n  🌐 本机访问: {protocol}://localhost:{port}")

    # 如果没有隧道，用局域网 IP 生成二维码
    if not TUNNEL_URL:
        print()
        primary_url = f"{protocol}://{local_ips[0]}:{port}"
        print_qr_code(primary_url)

    print("\n" + "=" * 50)
    print(f"  配对码: {PAIR_CODE}  |  Ctrl+C 停止服务")
    print("=" * 50 + "\n")


# ============================================================
# 启动入口
# ============================================================

if __name__ == "__main__":
    ssl_certfile, ssl_keyfile = None, None
    if config["enable_ssl"]:
        ssl_certfile, ssl_keyfile = generate_self_signed_cert()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config["port"],
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        log_level="info",
        access_log=False,
    )
