#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wallpaper Changer CGI → daemon 适配器
部署位置：/webman/3rdparty/autowallpaper/api.cgi

为什么走 CGI：
  /webman/3rdparty/<pkg>/ 下的 CGI 在 DSM 的所有入口（5000/5001、自定义域名、
  自定义端口、反向代理）都可达，且 nginx 会注入 HTTP_COOKIE / REMOTE_ADDR 等
  官方鉴权所要求的环境变量。

职责：
  1. 在 CGI 环境中执行官方 authenticate.cgi，拿到已登录用户名；
  2. 把请求转发到守护进程的 unix socket
     （/var/run/autowallpaper.sock）；
  3. 原样透传 daemon 的 JSON 响应。

鉴权裁决在 daemon：管理员会话 / 只读 token 由 daemon 判断。
"""
import http.client
import os
import json
import re
import socket
import subprocess
import sys
import time
import urllib.parse

AUTH_CGI = "/usr/syno/synoman/webman/modules/authenticate.cgi"
SOCK = "/var/run/autowallpaper.sock"
TIMEOUT = 240          # 覆盖下载 + ImageMagick 缩放的最坏耗时

ROUTES = {             # action -> (HTTP method, upstream path)
    "status":      ("GET",  "/api/status"),
    "config":      ("GET",  "/api/config"),
    "defaults":    ("GET",  "/api/defaults"),
    "logs":        ("GET",  "/api/logs"),
    "config_put":  ("PUT",  "/api/config"),
    "run":         ("POST", "/api/run"),
    "restore":     ("POST", "/api/restore"),
    "check_dir":   ("POST", "/api/check_dir"),
}


def emit(obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Type: application/json; charset=utf-8\r\n")
    sys.stdout.buffer.write(b"Cache-Control: no-store\r\n")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
    sys.stdout.buffer.write(body)


def fail(msg):
    emit({"error": msg})


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        s.connect(self._path)
        self.sock = s


def dsm_user():
    """在 CGI 环境中执行 authenticate.cgi；有输出 = 已登录用户名。

    反向代理入口下 REMOTE_ADDR 是代理地址，会话绑定的却是真实客户端 IP，
    因此优先用 X-Forwarded-For 首段还原真实地址，避免误判未登录。
    """
    env = dict(os.environ)
    xff = os.environ.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        real = xff.split(",")[0].strip()
        if real:
            env["REMOTE_ADDR"] = real
    try:
        p = subprocess.run([AUTH_CGI], capture_output=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return None, False
    return p.stdout.decode("utf-8", "replace").strip() or None, True


def auth_diag(user, cgi_ok):
    """会话校验未命中时记录脱敏诊断（只记录形态，不记录凭据）。"""
    if user:
        return
    try:
        path = "/var/packages/autowallpaper/var/api-auth.log"
        cookies = os.environ.get("HTTP_COOKIE", "")
        names = sorted(n.strip().split("=")[0]
                       for n in cookies.split(";") if n.strip()) if cookies else []
        line = ("%(t)s result=EMPTY cgi_ok=%(ok)s remote=%(ra)s xff0=%(xff)s "
                "host=%(host)s scheme=%(sch)s cookie_names=%(cn)s\n") % {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": int(bool(cgi_ok)),
            "ra": os.environ.get("REMOTE_ADDR", "-"),
            "xff": (os.environ.get("HTTP_X_FORWARDED_FOR", "-").split(",")[0].strip() or "-"),
            "host": os.environ.get("HTTP_HOST", "-"),
            "sch": "https" if os.environ.get("HTTPS") else "http",
            "cn": "|".join(names) if names else "none",
        }
        if os.path.exists(path) and os.path.getsize(path) > 128 * 1024:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-200:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def main():
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    qs = os.environ.get("QUERY_STRING", "")

    if method == "POST":
        try:
            n = int(os.environ.get("CONTENT_LENGTH", "0") or 0)
        except ValueError:
            n = 0
        raw = sys.stdin.buffer.read(n) if n else b""
        qs = raw.decode("utf-8", "replace") if raw else qs

    form = urllib.parse.parse_qs(qs, keep_blank_values=True)
    action = (form.get("action") or [""])[0]

    user, cgi_ok = dsm_user()
    if not user:
        auth_diag(user, cgi_ok)
    token = os.environ.get("HTTP_X_WALLPAPER_TOKEN", "").strip()
    if not token:
        m = re.search(r"wallpaper_token=([0-9a-fA-F]{16,})",
                      os.environ.get("HTTP_COOKIE", ""))
        if m:
            token = m.group(1)

    if action not in ROUTES:
        fail("unknown action: %s" % (action or "(empty)"))
        return

    up_method, up_path = ROUTES[action]
    headers = {
        "X-Wallpaper-User": user or "",
        "X-Wallpaper-Token": token,
        "X-Wallpaper-CGI-Auth-Available": "1" if cgi_ok else "0",
        "Connection": "close",
    }
    body = None

    if action == "logs":
        try:
            n = max(1, min(int((form.get("n") or ["300"])[0]), 2000))
        except ValueError:
            n = 300
        up_path += "?n=%d" % n

    if up_method in ("PUT", "POST"):
        payload = (form.get("payload") or ["{}"])[0]
        body = payload.encode("utf-8")
        headers["Content-Type"] = "application/json"

    try:
        conn = UnixConn(SOCK)
        conn.request(up_method, up_path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
    except (OSError, http.client.HTTPException) as e:
        fail("daemon unreachable: %s" % e)
        return

    sys.stdout.buffer.write(b"Content-Type: application/json; charset=utf-8\r\n")
    sys.stdout.buffer.write(b"Cache-Control: no-store\r\n")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n")
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    main()
