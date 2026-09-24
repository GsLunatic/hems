#!/usr/bin/env python3
"""Authenticated local web UI for the Home Assistant energy manager add-on."""
from __future__ import annotations

import base64
import copy
import hmac
import json
import logging
import os
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core import State
from telemetry import Telemetry
from backup import APP_VERSION, export_backup, validate_backup

HOST = "0.0.0.0"
PORT = 1569
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
WEB_DIR = Path(__file__).resolve().parent / "web"
HA_API = os.environ.get("HA_API", "http://supervisor/core/api")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
MAX_BODY_BYTES = 1024 * 1024
LOG = logging.getLogger("home_energy_manager.web")


def load_web_pages(web_dir: Path = WEB_DIR) -> dict[str, bytes]:
    """Read required assets before accepting requests, independently of cwd."""
    pages = {}
    for name in ("index.html", "config.html"):
        path = web_dir / name
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"无法读取网页文件 {path} ({type(exc).__name__})。"
                "请完整覆盖加载项源代码（包含 web 目录），然后更新或重新构建加载项；仅重启不会更新镜像。"
            ) from exc
        if b"<html" not in content.lower() or b"</html>" not in content.lower():
            raise RuntimeError(f"网页文件为空或不完整：{path}。请完整覆盖源代码并重新构建加载项。")
        pages[name] = content
    return pages


def read_password(data_dir: Path = DATA_DIR) -> str:
    """Supervisor options are the normal source; an environment override is useful for Docker."""
    if "WEB_PASSWORD" in os.environ:
        password = os.environ["WEB_PASSWORD"]
    else:
        try:
            options = json.loads((data_dir / "options.json").read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            options = {}
        except (OSError, ValueError) as exc:
            raise ValueError("无法读取 /data/options.json，请检查加载项配置。") from exc
        if not isinstance(options, dict):
            raise ValueError("加载项 options.json 必须是对象。")
        password = options.get("web_password", "")
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("请先到加载项的【配置】设置 web_password（至少 8 个字符），然后启动加载项。Web 用户名为 admin。")
    return password


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: State, password: str) -> None:
        self.web_pages = load_web_pages()
        self.state = state
        self.credentials = ("admin:" + password).encode("utf-8")
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeEnergyManager/" + APP_VERSION
    sys_version = ""

    def _send(self, status: int, data: bytes, content_type: str = "application/json; charset=utf-8", *, authenticate: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
        if authenticate:
            self.send_header("WWW-Authenticate", 'Basic realm="Home Energy Manager", charset="UTF-8"')
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _via_ingress(self) -> bool:
        # Home Assistant authenticates and proxies ingress requests.  The
        # internal add-on server must accept those requests without asking
        # the browser for a second Basic Auth login.
        marker = self.headers.get("X-Ingress-Path", "")
        return bool(marker and marker.startswith(("/api/hassio_ingress/", "/app/")))

    def _path(self) -> str:
        path = urllib.parse.urlsplit(self.path).path
        marker = self.headers.get("X-Ingress-Path", "").rstrip("/")
        if marker and path == marker:
            return "/"
        if marker and path.startswith(marker + "/"):
            return path[len(marker):] or "/"
        return path

    def _authenticated(self) -> bool:
        if self._via_ingress():
            return True
        supplied = b""
        authorization = self.headers.get("Authorization", "")
        if authorization.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(authorization.split(" ", 1)[1], validate=True)
            except (ValueError, TypeError):
                pass
        if not hmac.compare_digest(supplied, self.server.credentials):
            self._send(HTTPStatus.UNAUTHORIZED, "请输入加载项配置中的 Web 密码，用户名为 admin。".encode("utf-8"), "text/plain; charset=utf-8", authenticate=True)
            return False
        return True

    def _write_request(self) -> bool:
        """Reject simple browser cross-site requests and any supplied foreign origin."""
        if not self._authenticated():
            return False
        if self.headers.get_content_type() != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "写入请求必须使用 application/json。"})
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "拒绝跨站请求。"})
            return False
        origin = self.headers.get("Origin")
        if origin and not self._via_ingress():
            try:
                parsed = urllib.parse.urlsplit(origin)
                same_origin = parsed.scheme == "http" and parsed.netloc.lower() == self.headers.get("Host", "").lower() and not parsed.path and not parsed.query and not parsed.fragment and parsed.username is None
            except ValueError:
                same_origin = False
            if not same_origin:
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "拒绝跨站请求：Origin 与当前主机不一致。"})
                return False
        return True

    def _read_json(self) -> Any:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的 Content-Length。") from exc
        if not 0 < length <= MAX_BODY_BYTES:
            raise ValueError("请求体必须为 1 字节到 1 MB 的 JSON。")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("请求体不完整。")
        return json.loads(raw.decode("utf-8"))

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authenticated():
            return
        path = self._path()
        state = self.server.state
        if path == "/api/config":
            self.send_json(HTTPStatus.OK, state.get_config())
        elif path == "/api/backup":
            self.send_json(HTTPStatus.OK, export_backup(state.get_config()))
        elif path == "/api/backup/previous":
            try:
                data = json.loads((state.data_dir / "before_restore.json").read_text(encoding="utf-8"))
                validate_backup(data)
                self.send_json(HTTPStatus.OK, data)
            except FileNotFoundError:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "尚无恢复前备份；首次恢复时会自动创建。"})
            except (OSError, ValueError, TypeError):
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "无法读取恢复前备份，请查看加载项日志。"})
        elif path == "/api/status":
            self.send_json(HTTPStatus.OK, state.get_payload())
        elif path == "/api/bootstrap":
            if state.telemetry is not None:
                state.telemetry.ready.wait(5.5)
            payload = state.get_payload()
            payload.update(config=state.get_config(), entities=state.get_entities())
            self.send_json(HTTPStatus.OK, payload)
        elif path == "/api/entities":
            try:
                self.send_json(HTTPStatus.OK, state.get_entities())
            except Exception as exc:
                LOG.warning("Home Assistant entity request failed: %s", exc)
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": "无法读取 HA 实体，请检查加载项日志和 HA 连接。"})
        elif path in ("/", "/index.html", "/config.html"):
            page = "config.html" if path == "/config.html" else "index.html"
            self._send(HTTPStatus.OK, self.server.web_pages[page], "text/html; charset=utf-8")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if not self._write_request():
            return
        path = self._path()
        if path in ("/api/restore/preview", "/api/restore"):
            try:
                data = self._read_json()
                config = validate_backup(data)
                if path == "/api/restore":
                    self.server.state.save(config, backup_before=True)
                self.send_json(HTTPStatus.OK, {"config": config, "created_at": data["created_at"],
                                             "app_version": data["app_version"]})
            except (ValueError, TypeError, UnicodeDecodeError, RecursionError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "备份无效：" + str(exc)})
            except OSError:
                LOG.exception("restore configuration failed")
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "恢复未完成，无法写入配置或恢复前备份；请检查存储空间和加载项日志。"})
            return
        if path != "/api/check":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在。"})
            return
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象。")
            self.server.state.control_cycle()
            with self.server.state.lock:
                result = copy.deepcopy(self.server.state.last_status)
            self.send_json(HTTPStatus.OK, result)
        except (ValueError, UnicodeDecodeError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            LOG.exception("manual check failed")
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "检查失败，请查看加载项日志。"})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._write_request():
            return
        if urllib.parse.urlsplit(self.path).path != "/api/config":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在。"})
            return
        try:
            data = self._read_json()
            self.server.state.save(data)
            self.send_json(HTTPStatus.OK, self.server.state.get_config())
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except OSError:
            LOG.exception("saving configuration failed")
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "配置保存失败，请检查数据目录和加载项日志。"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self._authenticated():
            self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "不允许跨域访问。"})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        password = read_password()
    except ValueError as exc:
        LOG.error("%s", exc)
        raise SystemExit(1) from exc
    if not SUPERVISOR_TOKEN:
        LOG.error("缺少 SUPERVISOR_TOKEN，无法访问 Home Assistant。请从 HA 加载项启动。")
        raise SystemExit(1)
    state = State(DATA_DIR, HA_API, SUPERVISOR_TOKEN)
    try:
        server = WebServer((HOST, PORT), state, password)
    except RuntimeError as exc:
        LOG.error("Web 启动失败：%s", exc)
        raise SystemExit(1) from exc
    state.telemetry = Telemetry(state.ha_request, state.stop)
    state.telemetry.start()
    thread = threading.Thread(target=state.loop, daemon=True, name="energy-control")
    thread.start()
    LOG.info("版本 %s；网页已加载：%s（首页和配置页）。", APP_VERSION, WEB_DIR)
    LOG.info("Web 配置地址 http://<HA地址>:%s，用户名 admin。", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop.set()
        server.server_close()
        thread.join(timeout=10)
        state.telemetry.thread.join(timeout=6)


if __name__ == "__main__":
    main()


