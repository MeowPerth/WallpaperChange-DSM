#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallpaper-daemon.py — DSM Auto Change Wallpaper 群晖套件守护进程

上游：https://github.com/MeowPerth/DSM_AutoChangeWallpaper （MIT）
在保留原脚本全部能力的前提下重构为 DSM 7.2 套件后端：

  * 图源：必应每日一图（支持市场/4K）或本地文件夹（递归/随机）
  * 目标：登录壁纸、桌面壁纸（1x/2x）、指定用户的桌面壁纸
  * 定时：每天固定时刻 / 固定间隔 / 仅手动；失败自动重试
  * 备份：首次改动系统文件前完整备份，支持一键恢复默认
  * 零第三方依赖：仅 Python 标准库；缩放优先用 ImageMagick，
    未安装时自动退化为直接复制原图（DSM 会自适应显示）
  * 本地 HTTP API（unix socket，DSM 应用窗口经 api.cgi 调用）
  * 认证：官方 authenticate.cgi 会话校验 + 安装期 token 兜底；
    写操作额外要求 DSM 管理员身份

设计约束：DSM 7.2 自带 python3 可能为 3.8，故不使用 3.9+ 语法。
"""

import argparse
import grp
import hashlib
import hmac
import http.client
import json
import logging
import os
import platform
import pwd
import random
import re
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PKG_NAME = "autowallpaper"
PKG_VERSION = "1.0.0"

AUTHENTICATE_CGI = "/usr/syno/synoman/webman/modules/authenticate.cgi"
SYNOINFO_CONF = "/etc/synoinfo.conf"

# ---------------------------------------------------------------- 系统路径 ----
# 这些是 DSM 上的绝对路径；SYSTEM_ROOT 仅用于本地隔离测试。
SYSTEM_ROOT = ""

LOGIN_DIR = "/usr/syno/etc"
LOGIN_FILES = (
    "login_background.jpg",
    "login_background_hd.jpg",
    "SYNO.Foto.AppInstance_login_background.jpg",
    "SYNO.SDS.App.FileStation3.Instance_login_background.jpg",
    "SYNO.SDS.DownloadStation.Application_login_background.jpg",
    "SYNO.SDS.SheetStation.Application_login_background.jpg",
    "SYNO.SDS.VideoStation.AppInstance_login_background.jpg",
    "SYNO.SDS.Virtualization.Application_login_background.jpg",
)
DESKTOP_BASE = "/usr/syno/synoman/webman/resources/images"
DESKTOP_SIZES = ("1x", "2x")
DESKTOP_TYPES = ("default_login_background", "default_wallpaper")
USER_PREF_DIR = "/usr/syno/etc/preference"

SYNOINFO_KEYS = ("login_background_customize", "login_welcome_title", "login_welcome_msg")
LOGIN_KEYS = set(os.path.join(LOGIN_DIR, n) for n in LOGIN_FILES)

SKIP_DIRS = {
    "@eaDir", "#recycle", "@tmp", ".thumbnails", "@Recently-Snapshot",
    "#snapshot", "#recycle", "lost+found",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MASK = "********"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

log = logging.getLogger("wallpaper")


# ---------------------------------------------------------------- 工具 ----

def rooted(path):
    """把 DSM 绝对路径映射到测试根目录（SYSTEM_ROOT 为空时原样返回）。"""
    if not path:
        return path
    if not os.path.isabs(path):
        path = "/" + path
    return SYSTEM_ROOT + path if SYSTEM_ROOT else path


def now_str(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def parse_time(text):
    if not text:
        return None
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_bytes(path, data, mode=0o644, uid=None, gid=None):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wptmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if uid is not None and gid is not None:
            try:
                os.chown(tmp, uid, gid)
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_copy(src, dst, mode=0o644, uid=None, gid=None):
    with open(src, "rb") as f:
        data = f.read()
    atomic_write_bytes(dst, data, mode=mode, uid=uid, gid=gid)
    return data


def resolve_target(path):
    """解析系统文件真实路径（处理 /etc/*.conf 等符号链接），限制在测试根内。"""
    full = rooted(path)
    real = os.path.realpath(full)
    if SYSTEM_ROOT:
        if not (real == SYSTEM_ROOT or real.startswith(SYSTEM_ROOT + os.sep)):
            return full
    return real


def sniff_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def conf_quote(value):
    value = str(value).replace("\r", " ").replace("\n", " ")
    value = (value.replace("\\", "\\\\").replace('"', '\\"')
             .replace("$", "\\$").replace("`", "\\`"))
    return '"%s"' % value


def read_synoinfo(full):
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def parse_conf_value(line):
    """解析 synoinfo 的一行，仅接受本套件管理的键；返回 (key, value)。"""
    m = re.match(r"\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line or "")
    if not m:
        return None, None
    key, raw = m.group(1), m.group(2)
    if key not in SYNOINFO_KEYS:
        return key, None
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        value = raw[1:-1]
        value = (value.replace("\\\\", "\\").replace('\\"', '"')
                 .replace("\\$", "$").replace("\\`", "`"))
    else:
        value = raw
    return key, value


def rewrite_synoinfo(full, updates=None, removes=None, restore_values=None):
    """更新 /etc/synoinfo.conf：删除同名键后追加新行，保留其他内容与权限。

    restore_values 用于恢复备份：值会重新经过 conf_quote，绝不直接写入
    manifest 中的原始行，避免被篡改的 manifest 注入 shell 片段。
    """
    updates = updates or {}
    removes = set(removes or ())
    restore_values = restore_values or {}
    text = read_synoinfo(full)
    if text is None:
        raise RuntimeError("无法读取 %s" % full)

    try:
        st = os.stat(full)
    except OSError:
        st = None

    out = []
    seen = set()
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z0-9_]+)\s*=", line)
        key = m.group(1) if m else None
        if key and key in removes:
            continue
        if key and key in updates:
            if key not in seen:
                out.append('%s=%s' % (key, conf_quote(updates[key])))
                seen.add(key)
            continue
        out.append(line)

    for key, value in updates.items():
        if key not in seen:
            out.append('%s=%s' % (key, conf_quote(value)))
    for key, value in restore_values.items():
        if key in SYNOINFO_KEYS:
            out.append('%s=%s' % (key, conf_quote(value)))

    new_text = "\n".join(out).rstrip("\n") + "\n"
    atomic_write_bytes(full, new_text.encode("utf-8"),
                       mode=(st.st_mode & 0o7777) if st else 0o644,
                       uid=st.st_uid if st else None,
                       gid=st.st_gid if st else None)


# ---------------------------------------------------------------- 配置 ----

DEFAULT_CONFIG = {
    "source": "bing",                 # bing | local
    "bing_market": "zh-cn",           # auto 或 xx-xx
    "bing_uhd": True,
    "local_dir": "",
    "local_recursive": True,
    "save_bing": False,
    "save_dir": "",
    "user_dir": "",
    "target_login": True,
    "target_desktop": True,
    "target_user": False,
    "user_names": [],
    "login_size": "1920x1200",
    "thumb_size": "160x100",
    "user_size": "1920x1200",
    "user_thumb_size": "120x120",
    "resize": True,
    "schedule_mode": "daily",         # daily | interval | off
    "daily_time": "04:00",
    "interval_minutes": 720,
    "run_on_start": False,
    "skip_duplicate": True,
    "retry_minutes": 30,
    "set_login_text": True,
    "download_timeout": 30,
    "download_retries": 5,
    "log_max_bytes": 1024 * 1024,
    "log_backup_count": 3,
    "require_admin": True,
    # 远程/反代/会话失效时可选的写权限兜底；默认关闭，开启后实例 token
    # 也可保存配置（任何能读取 index.html 的 DSM 用户都能拿到该 token）。
    "token_write": False,
}

RE_SIZE = re.compile(r"^\d{2,5}x\d{2,5}$")
RE_MARKET = re.compile(r"^(auto|[a-z]{2}(-[a-z]{2})?)$")
RE_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
RE_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


class Config:
    def __init__(self, path):
        self.path = path
        self._mtime = 0.0
        self.data = dict(DEFAULT_CONFIG)
        self.reload(force=True)

    def reload(self, force=False):
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = 0.0
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        data = dict(DEFAULT_CONFIG)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for k in DEFAULT_CONFIG:
                    if k in loaded and loaded[k] is not None:
                        data[k] = loaded[k]
        except FileNotFoundError:
            log.warning("配置文件不存在，使用默认配置: %s", self.path)
        except (ValueError, OSError) as e:
            log.error("配置文件读取失败，使用默认配置: %s (%s)", self.path, e)

        data = self._normalize(data)
        if data != self.data:
            self.data = data
        return True

    @staticmethod
    def _normalize(data):
        data["source"] = "local" if str(data.get("source")) == "local" else "bing"
        data["bing_market"] = str(data.get("bing_market") or "zh-cn").lower()
        for k in ("bing_uhd", "local_recursive", "save_bing", "target_login",
                  "target_desktop", "target_user", "resize", "run_on_start",
                  "skip_duplicate", "set_login_text", "require_admin",
                  "token_write"):
            data[k] = bool(data.get(k))
        for k in ("local_dir", "save_dir", "user_dir"):
            data[k] = str(data.get(k) or "").strip()
        for k in ("login_size", "thumb_size", "user_size", "user_thumb_size"):
            v = str(data.get(k) or "").strip().lower()
            data[k] = v if RE_SIZE.match(v) else DEFAULT_CONFIG[k]
        mode = str(data.get("schedule_mode") or "daily").lower()
        data["schedule_mode"] = mode if mode in ("daily", "interval", "off") else "daily"
        dt = str(data.get("daily_time") or "04:00").strip()
        data["daily_time"] = dt if RE_TIME.match(dt) else "04:00"
        for k, lo, hi, default in (
                ("interval_minutes", 5, 10080, 720),
                ("retry_minutes", 0, 1440, 30),
                ("download_timeout", 5, 300, 30),
                ("download_retries", 1, 10, 5),
                ("log_max_bytes", 64 * 1024, 50 * 1024 * 1024, 1024 * 1024),
                ("log_backup_count", 1, 10, 3)):
            try:
                v = int(data.get(k))
            except (TypeError, ValueError):
                v = default
            data[k] = max(lo, min(hi, v))
        names = data.get("user_names")
        if isinstance(names, str):
            names = re.split(r"[,\s]+", names)
        elif not isinstance(names, list):
            names = []
        cleaned = []
        for n in names:
            n = str(n).strip()
            if n and n not in cleaned:
                cleaned.append(n)
        data["user_names"] = cleaned
        return data

    def masked(self):
        return dict(self.data)

    def validate_and_apply(self, payload):
        errors = []
        d = dict(self.data)

        source = str(payload.get("source", d["source"])).lower()
        if source not in ("bing", "local"):
            errors.append("source: 只能为 bing 或 local")
        d["source"] = source

        market = str(payload.get("bing_market", d["bing_market"])).strip().lower()
        if not RE_MARKET.match(market):
            errors.append("bing_market: 格式应为 zh-cn / en-us 或 auto")
        d["bing_market"] = market or "zh-cn"
        d["bing_uhd"] = bool(payload.get("bing_uhd", d["bing_uhd"]))

        for k in ("local_dir", "save_dir", "user_dir"):
            d[k] = str(payload.get(k, d[k]) or "").strip()
            if d[k] and not os.path.isabs(d[k]):
                errors.append("%s: 需要填写绝对路径（如 /volume1/photo）" % k)
        d["local_recursive"] = bool(payload.get("local_recursive", d["local_recursive"]))
        d["save_bing"] = bool(payload.get("save_bing", d["save_bing"]))

        d["target_login"] = bool(payload.get("target_login", d["target_login"]))
        d["target_desktop"] = bool(payload.get("target_desktop", d["target_desktop"]))
        d["target_user"] = bool(payload.get("target_user", d["target_user"]))
        if not (d["target_login"] or d["target_desktop"] or d["target_user"]):
            errors.append("至少需要启用一个应用目标")

        names = payload.get("user_names", d["user_names"])
        if isinstance(names, str):
            names = re.split(r"[,\s]+", names)
        elif not isinstance(names, list):
            names = []
        cleaned = []
        for n in names:
            n = str(n).strip()
            if not n:
                continue
            if not RE_USER.match(n):
                errors.append("user_names: 用户名不合法: %s" % n)
                continue
            if n not in cleaned:
                cleaned.append(n)
        d["user_names"] = cleaned
        if d["target_user"] and not cleaned:
            errors.append("启用用户壁纸时至少要填写一个用户名")

        for k in ("login_size", "thumb_size", "user_size", "user_thumb_size"):
            v = str(payload.get(k, d[k])).strip().lower()
            if not RE_SIZE.match(v):
                errors.append("%s: 尺寸格式应为 宽x高（如 1920x1200）" % k)
            else:
                d[k] = v
        d["resize"] = bool(payload.get("resize", d["resize"]))

        mode = str(payload.get("schedule_mode", d["schedule_mode"])).lower()
        if mode not in ("daily", "interval", "off"):
            errors.append("schedule_mode: 只能为 daily / interval / off")
        d["schedule_mode"] = mode
        dt = str(payload.get("daily_time", d["daily_time"])).strip()
        if not RE_TIME.match(dt):
            errors.append("daily_time: 时间格式应为 HH:MM")
        else:
            d["daily_time"] = dt
        try:
            iv = int(payload.get("interval_minutes", d["interval_minutes"]))
            if not 5 <= iv <= 10080:
                raise ValueError
            d["interval_minutes"] = iv
        except (TypeError, ValueError):
            errors.append("interval_minutes: 需为 5-10080 分钟")
        try:
            rm = int(payload.get("retry_minutes", d["retry_minutes"]))
            if not 0 <= rm <= 1440:
                raise ValueError
            d["retry_minutes"] = rm
        except (TypeError, ValueError):
            errors.append("retry_minutes: 需为 0-1440 分钟")

        for k in ("run_on_start", "skip_duplicate", "set_login_text"):
            d[k] = bool(payload.get(k, d[k]))

        try:
            to = int(payload.get("download_timeout", d["download_timeout"]))
            if not 5 <= to <= 300:
                raise ValueError
            d["download_timeout"] = to
        except (TypeError, ValueError):
            errors.append("download_timeout: 需为 5-300 秒")
        try:
            rt = int(payload.get("download_retries", d["download_retries"]))
            if not 1 <= rt <= 10:
                raise ValueError
            d["download_retries"] = rt
        except (TypeError, ValueError):
            errors.append("download_retries: 需为 1-10 次")
        try:
            mb = int(payload.get("log_max_bytes", d["log_max_bytes"]))
            if not 64 * 1024 <= mb <= 50 * 1024 * 1024:
                raise ValueError
            d["log_max_bytes"] = mb
        except (TypeError, ValueError):
            errors.append("log_max_bytes: 需为 64KiB-50MiB")
        try:
            bc = int(payload.get("log_backup_count", d["log_backup_count"]))
            if not 1 <= bc <= 10:
                raise ValueError
            d["log_backup_count"] = bc
        except (TypeError, ValueError):
            errors.append("log_backup_count: 需为 1-10")

        d["require_admin"] = bool(payload.get("require_admin", d["require_admin"]))
        d["token_write"] = bool(payload.get("token_write", d["token_write"]))

        if d["source"] == "local":
            full = rooted(d["local_dir"])
            if not d["local_dir"]:
                errors.append("local_dir: 选择本地图源时必须填写图片文件夹")
            elif not os.path.isdir(full):
                errors.append("local_dir: 目录不存在: %s" % d["local_dir"])
        if d["save_bing"] and not d["save_dir"]:
            errors.append("save_dir: 启用保存必应原图时必须填写保存目录")
        if d["save_bing"] and d["source"] != "bing":
            errors.append("save_bing: 仅必应图源支持保存原图")
        if d["target_user"] and d["user_dir"] and not os.path.isdir(rooted(d["user_dir"])):
            errors.append("user_dir: 目录不存在: %s" % d["user_dir"])

        if errors:
            return False, errors
        self.data = d
        self.save()
        self._mtime = os.stat(self.path).st_mtime
        return True, []

    def save(self):
        atomic_write_json(self.path, self.data)


# ---------------------------------------------------------------- 状态 ----

STATE_DEFAULT = {
    "last_attempt_time": None,
    "last_success_time": None,
    "last_ok": None,
    "last_error": None,
    "last_result": None,
    "last_source": None,
    "image_path": None,
    "saved_to": None,
    "bing_date": None,
    "bing_title": None,
    "bing_copyright": None,
    "preview_version": 0,
    "run_count": 0,
    "restored_at": None,
}


class State:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(STATE_DEFAULT)
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for k in STATE_DEFAULT:
                    if k in loaded:
                        self.data[k] = loaded[k]
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            atomic_write_json(self.path, self.data)


# ---------------------------------------------------------------- 图片源 ----

class SourceError(RuntimeError):
    pass


_SSL_FALLBACK_WARNED = False


def _open_url(url, timeout, insecure=False):
    ctx = ssl._create_unverified_context() if insecure else None
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def http_get_bytes(url, timeout, retries, max_bytes=MAX_IMAGE_BYTES, label="资源"):
    """带重试的 HTTP GET；证书链异常时降级为不校验（仅记录一次告警）。"""
    global _SSL_FALLBACK_WARNED
    last_err = None
    for attempt in range(1, max(1, retries) + 1):
        for insecure in (False, True):
            try:
                with _open_url(url, timeout, insecure=insecure) as resp:
                    code = getattr(resp, "status", None)
                    if code is not None and code != 200:
                        if code >= 500:
                            last_err = SourceError("HTTP %s" % code)
                            break
                        raise SourceError("HTTP %s" % code)
                    data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise SourceError("%s超过大小上限" % label)
                if insecure and not _SSL_FALLBACK_WARNED:
                    _SSL_FALLBACK_WARNED = True
                    log.warning("[下载] 系统 CA 校验失败，已降级为不校验证书（请更新 DSM 根证书）")
                return data
            except SourceError:
                raise
            except (ssl.SSLError, urllib.error.URLError) as e:
                reason = getattr(e, "reason", e)
                ssl_issue = isinstance(reason, ssl.SSLError) or \
                    "CERTIFICATE_VERIFY_FAILED" in str(reason) or \
                    "certificate verify failed" in str(reason).lower()
                if ssl_issue and not insecure:
                    continue
                last_err = e
                break
            except (OSError, ValueError, http.client.HTTPException) as e:
                last_err = e
                break
        if attempt < max(1, retries):
            time.sleep(min(5, attempt * 1.5))
    raise SourceError("下载失败: %s" % (last_err or "未知错误"))


def parse_copyright(copyright_text):
    """上游 parse_copyright 的 Python 版：返回 (中文名, 英文名)。"""
    text = (copyright_text or "").strip()
    text = text.replace("，", ",").replace("（", "(").replace("）", ")")
    text = text.replace("/", "_")
    text = re.sub(r"\s+", " ", text)
    if not text:
        return "必应壁纸", "Bing Wallpaper"

    outside = re.sub(r"\([^)]*\)", "", text)
    outside = re.sub(r"\([^)]*$", "", outside).strip()
    m = re.search(r"\(([^)]*)\)", text)
    inside = (m.group(1).strip() if m else "")

    english = re.sub(r"©.*", "", inside).strip().strip(",").strip()
    if not english and inside:
        # 形如 "(© 摄影师/图库)"：没有描述文字时退回摄影师署名，避免英文名恒为空
        english = re.sub(r"^\s*©\s*", "", inside).strip().strip(",").strip()
    chinese = re.sub(r"©.*", "", outside).strip().strip(",").strip()
    if not chinese and not english:
        chinese = re.sub(r"©.*", "", text).strip().strip(",").strip()
        english = chinese
    if not chinese:
        chinese = "必应壁纸"
    if not english:
        english = chinese
    chinese = re.sub(r"^[,\s]+|[,\s]+$", "", chinese)
    english = re.sub(r"^[,\s]+|[,\s]+$", "", english)
    return chinese or "必应壁纸", english or chinese


def safe_filename(text, limit=80):
    """清理文件名并按 UTF-8 字节截断（避免中文长标题超过 255 字节上限）。"""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    out = ""
    for ch in text:
        if len((out + ch).encode("utf-8")) > limit:
            break
        out += ch
    return out.strip() or "wallpaper"


def fetch_bing(market, uhd, timeout, retries):
    mkt = "" if market == "auto" else "&mkt=" + market
    api = ("https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&uhd=1"
           + mkt)
    raw = http_get_bytes(api, timeout, retries, max_bytes=512 * 1024, label="必应接口")
    try:
        doc = json.loads(raw.decode("utf-8-sig", "replace"))
        img = doc["images"][0]
        url = img["url"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise SourceError("必应接口返回数据异常: %s" % e)

    urlbase = img.get("urlbase")
    candidates = []
    if uhd and urlbase:
        candidates.append("https://www.bing.com" + urlbase + "_UHD.jpg")
    if url:
        candidates.append("https://www.bing.com" + url)
    if not urlbase and not url:
        raise SourceError("必应接口未返回图片地址")

    data = None
    used = None
    errors = []
    for cand in candidates:
        try:
            data = http_get_bytes(cand, timeout, max(1, retries - 1),
                                  label="必应图片")
            sniff = sniff_image(data)
            if not sniff:
                raise SourceError("返回内容不是图片")
            used = cand
            break
        except SourceError as e:
            errors.append(str(e))
    if data is None:
        reason = "; ".join(errors[-2:]) if errors else "未知错误"
        raise SourceError("必应壁纸下载失败: %s" % reason)

    return {
        "source": "bing",
        "date": str(img.get("enddate") or ""),
        "title": str(img.get("title") or "").strip(),
        "copyright": str(img.get("copyright") or "").strip(),
        "url": used,
        "data": data,
    }


def pick_local_image(directory, recursive, rng=None):
    full = rooted(directory)
    if not os.path.isdir(full):
        raise SourceError("图片目录不存在: %s" % directory)
    rng = rng or random.SystemRandom()
    files = []
    for base, dirs, names in os.walk(full, followlinks=False):
        if recursive:
            dirs[:] = [d for d in dirs
                       if d not in SKIP_DIRS and not d.startswith(".")]
        else:
            dirs[:] = []
        for name in names:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                continue
            files.append(os.path.join(base, name))
            if len(files) >= 50000:
                break
        if len(files) >= 50000:
            break
    if not files:
        raise SourceError("目录中没有找到图片（支持 jpg/jpeg/png/webp/bmp）: %s" % directory)
    return rng.choice(files)


# ---------------------------------------------------------------- 图片缩放 ----

class Converter:
    """优先 ImageMagick（convert / magick）；不可用时退化为复制。"""

    CANDIDATES = (
        "/usr/bin/convert",
        "/usr/syno/bin/convert",
        "/usr/local/bin/convert",
        "/usr/bin/magick",
        "/usr/local/bin/magick",
        "/var/packages/ImageMagick/target/bin/convert",
        "/var/packages/ImageMagick/target/bin/magick",
        "/var/packages/ImageMagick7/target/bin/magick",
        "/var/packages/ImageMagick7/target/bin/convert",
    )

    def __init__(self):
        self.path = self._find()

    def _find(self):
        cands = list(self.CANDIDATES)
        for name in ("magick", "convert"):
            found = shutil.which(name)
            if found:
                cands.insert(0, found)
        seen = set()
        for c in cands:
            if not c or c in seen or not os.path.isfile(c) or not os.access(c, os.X_OK):
                continue
            seen.add(c)
            try:
                p = subprocess.run([c, "-version"], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=8)
                if p.returncode == 0:
                    return c
            except (OSError, subprocess.SubprocessError):
                continue
        return None

    @property
    def available(self):
        return bool(self.path)

    @property
    def label(self):
        return os.path.basename(self.path) if self.path else ""

    def render(self, src, dst, size):
        """把 src 转为 JPEG 写到 dst；size=None 表示不缩放。返回 warning 或 None。"""
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if not size or not self.path:
            atomic_copy(src, dst)
            if size and not self.path:
                return "未安装 ImageMagick，壁纸未缩放（直接复制原图；可在套件中心安装 ImageMagick）"
            return None

        tmp = dst + ".tmp-%d" % os.getpid()
        cmd = [self.path, src, "-auto-orient",
               "-resize", str(size) + ">",
               "-strip", "-quality", "90",
               "-background", "#000000", "-alpha", "remove", "-alpha", "off",
               "jpg:" + tmp]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=180)
            if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.chmod(tmp, 0o644)
                os.replace(tmp, dst)
                return None
            err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            log.warning("[缩放] ImageMagick 失败: %s", err[-1] if err else "unknown error")
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("[缩放] ImageMagick 调用异常: %s", e)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        atomic_copy(src, dst)
        return "ImageMagick 处理失败，已退化为直接复制原图"


# ---------------------------------------------------------------- 备份 ----

def safe_restore_target(key):
    """限制 backup manifest 可写回的路径，防止被篡改后 root 任意写文件。"""
    if key == SYNOINFO_CONF:
        return True
    if key in LOGIN_KEYS:
        return True
    prefix = DESKTOP_BASE + "/"
    if key.startswith(prefix):
        parts = key[len(prefix):].split("/")
        return (len(parts) == 3 and parts[0] in DESKTOP_SIZES
                and parts[1] in DESKTOP_TYPES
                and parts[2] in ("dsm7_01.jpg", "thumbnail_01.jpg"))
    prefix = USER_PREF_DIR + "/"
    if key.startswith(prefix):
        parts = key[len(prefix):].split("/")
        return (len(parts) == 2 and RE_USER.match(parts[0]) is not None
                and parts[1] in ("wallpaper", "1.jpg", "1thumb.jpg"))
    return False


class Backup:
    """首次改动系统文件前备份原文件；记录 /etc/synoinfo.conf 原始键值。"""

    def __init__(self, var_dir):
        self.dir = os.path.join(var_dir, "backup")
        self.files_dir = os.path.join(self.dir, "files")
        self.path = os.path.join(self.dir, "manifest.json")
        self.data = {"version": 1, "files": {}, "synoinfo": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data["files"] = loaded.get("files") or {}
                self.data["synoinfo"] = loaded.get("synoinfo") or {}
                self.data["restored"] = loaded.get("restored")
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(self.files_dir, exist_ok=True)
        atomic_write_json(self.path, self.data)

    def ensure(self, key):
        """key 为 DSM 系统路径；首次见到时备份原文件。"""
        files = self.data["files"]
        if key in files:
            return
        full = resolve_target(key)
        entry = {"existed": os.path.exists(full)}
        if entry["existed"]:
            try:
                st = os.stat(full)
                entry.update({"mode": st.st_mode & 0o7777, "uid": st.st_uid,
                              "gid": st.st_gid, "mtime": int(st.st_mtime)})
            except OSError:
                entry.update({"mode": 0o644, "uid": 0, "gid": 0})
            name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            ext = os.path.splitext(key)[1] or ".bin"
            rel = os.path.join("files", name + ext)
            dst = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
            entry["backup"] = rel
            log.info("[备份] %s -> %s", key, rel)
        files[key] = entry
        self.save()

    def snapshot_synoinfo(self, keys):
        info = self.data["synoinfo"]
        text = read_synoinfo(resolve_target(SYNOINFO_CONF))
        if text is None:
            return
        lines = text.splitlines()
        changed = False
        for key in keys:
            if key not in SYNOINFO_KEYS or key in info:
                continue
            raw = None
            for line in lines:
                m = re.match(r"\s*([A-Za-z0-9_]+)\s*=", line)
                if m and m.group(1) == key:
                    raw = line
            value = parse_conf_value(raw)[1] if raw else None
            info[key] = {"existed": raw is not None, "value": value}
            changed = True
        if changed:
            self.save()

    def restore(self):
        restored, missing, removed = 0, 0, 0
        for key, entry in list(self.data["files"].items()):
            if not safe_restore_target(key):
                missing += 1
                log.warning("[恢复] manifest 目标路径不在白名单内，跳过: %s", key)
                continue
            full = resolve_target(key)
            if entry.get("existed"):
                rel = entry.get("backup") or ""
                src = os.path.join(self.dir, rel)
                real_src = os.path.realpath(src)
                real_files = os.path.realpath(self.files_dir) + os.sep
                if (not rel.startswith("files/") or ".." in rel.split("/")
                        or not real_src.startswith(real_files)
                        or not os.path.isfile(real_src)):
                    missing += 1
                    log.warning("[恢复] 备份文件缺失或路径非法，跳过: %s", key)
                    continue
                atomic_copy(real_src, full, mode=entry.get("mode", 0o644),
                            uid=entry.get("uid"), gid=entry.get("gid"))
                restored += 1
            else:
                try:
                    if os.path.exists(full):
                        os.unlink(full)
                        removed += 1
                except OSError as e:
                    log.warning("[恢复] 删除失败 %s: %s", key, e)

        info = self.data.get("synoinfo") or {}
        if info:
            full = resolve_target(SYNOINFO_CONF)
            if os.path.exists(full):
                removes, restore_values = [], {}
                for key, item in info.items():
                    if key not in SYNOINFO_KEYS:
                        continue
                    if item.get("existed"):
                        value = item.get("value")
                        if value is None and item.get("line"):
                            value = parse_conf_value(item["line"])[1]
                        if value is not None:
                            removes.append(key)
                            restore_values[key] = value
                        else:
                            log.warning("[恢复] 无法解析原值，保留当前键: %s", key)
                    else:
                        removes.append(key)
                if removes or restore_values:
                    try:
                        rewrite_synoinfo(full, removes=removes,
                                         restore_values=restore_values)
                        restored += 1
                    except (RuntimeError, OSError) as e:
                        log.warning("[恢复] synoinfo.conf 恢复失败: %s", e)

        self.data["restored"] = now_str()
        self.save()
        return {"restored": restored, "removed": removed, "missing": missing}


# ---------------------------------------------------------------- 引擎 ----

class Engine:
    def __init__(self, cfg, state, var_dir, ui_dir, converter):
        self.cfg = cfg
        self.state = state
        self.var_dir = var_dir
        self.ui_dir = ui_dir
        self.converter = converter
        self.backup = Backup(var_dir)
        self.lock = threading.Lock()
        self.is_running = False
        self.started_at = time.time()

    # -- 单次执行 ---------------------------------------------------------
    def run(self, manual=False, force=False, reason="manual"):
        with self.lock:
            self.is_running = True
            started = time.time()
            self.cfg.reload(force=True)
            env = self.cfg.data
            st = self.state.data
            work = None
            try:
                os.makedirs(self.var_dir, exist_ok=True)
                work = tempfile.mkdtemp(prefix="run-", dir=self.var_dir)
                src = self._resolve_source(env, work)

                if (not force and env["skip_duplicate"] and src["source"] == "bing"
                        and src.get("date") and st.get("bing_date") == src["date"]
                        and st.get("last_ok") is True):
                    ts = now_str()
                    result = {
                        "ok": True, "skipped": True, "timestamp": ts,
                        "source": "bing", "title": src.get("title"),
                        "copyright": src.get("copyright"),
                        "bing_date": src.get("date"),
                        "message": "必应壁纸今日已应用，跳过重复执行",
                        "duration_ms": int((time.time() - started) * 1000),
                    }
                    st["last_attempt_time"] = ts
                    st["last_result"] = _trim_result(result)
                    self.state.save()
                    log.info("[跳过] 必应壁纸 %s 已应用过，跳过", src.get("date"))
                    return result

                warnings = []
                applied = self._apply_all(env, src, work, warnings)
                warnings = _dedupe(warnings)

                saved_to = None
                if env["save_bing"] and env["save_dir"] and src["source"] == "bing":
                    saved_to = self._save_bing(env, src, warnings)

                preview_warn = self._write_preview(src["path"], env)
                if preview_warn:
                    warnings.append(preview_warn)

                ts = now_str()
                result = {
                    "ok": True,
                    "skipped": False,
                    "timestamp": ts,
                    "source": src["source"],
                    "image": src.get("url") or src["path"],
                    "title": src.get("title"),
                    "copyright": src.get("copyright"),
                    "bing_date": src.get("date"),
                    "saved_to": saved_to,
                    "applied": applied,
                    "warnings": _dedupe(warnings),
                    "duration_ms": int((time.time() - started) * 1000),
                    "manual": bool(manual),
                    "reason": reason,
                }
                self._record_success(result, src)
                log.info("[完成] 图源=%s 登录=%s 桌面=%s 用户=%s 用时=%.1fs",
                         src["source"], applied.get("login"),
                         applied.get("desktop"), len(applied.get("users_ok") or []),
                         result["duration_ms"] / 1000.0)
                for w in result["warnings"]:
                    log.warning("[提示] %s", w)
                return result
            except Exception as e:  # noqa: BLE001
                err = str(e) or e.__class__.__name__
                log.exception("[失败] 更换壁纸失败: %s", err)
                ts = now_str()
                result = {
                    "ok": False, "error": err, "timestamp": ts,
                    "duration_ms": int((time.time() - started) * 1000),
                    "manual": bool(manual), "reason": reason,
                }
                self._record_failure(result)
                return result
            finally:
                if work:
                    shutil.rmtree(work, ignore_errors=True)
                self.is_running = False

    # -- 图源 -------------------------------------------------------------
    def _resolve_source(self, env, work):
        if env["source"] == "local":
            path = pick_local_image(env["local_dir"], env["local_recursive"])
            try:
                size = os.path.getsize(path)
            except OSError as e:
                raise SourceError("读取图片失败: %s" % e)
            if size < 512 or size > MAX_IMAGE_BYTES:
                raise SourceError("图片大小异常（%d 字节）: %s" % (size, path))
            with open(path, "rb") as f:
                head = f.read(32)
            if not sniff_image(head):
                raise SourceError("不是支持的图片格式: %s" % path)
            return {"source": "local", "path": path, "title": os.path.basename(path),
                    "copyright": os.path.dirname(path), "date": None}
        info = fetch_bing(env["bing_market"], env["bing_uhd"],
                          env["download_timeout"], env["download_retries"])
        if not sniff_image(info["data"]):
            raise SourceError("必应壁纸内容不是有效图片")
        path = os.path.join(work, "bing.jpg")
        with open(path, "wb") as f:
            f.write(info["data"])
        info["path"] = path
        return info

    # -- 应用 -------------------------------------------------------------
    def _apply_all(self, env, src, work, warnings):
        need_login = env["target_login"]
        need_desktop = env["target_desktop"]
        need_user = env["target_user"]

        login_bytes = thumb_bytes = None
        if need_login or need_desktop:
            login_img = os.path.join(work, "login.jpg")
            thumb_img = os.path.join(work, "thumb.jpg")
            w = self.converter.render(src["path"], login_img,
                                      env["login_size"] if env["resize"] else None)
            if w:
                warnings.append(w)
            w = self.converter.render(src["path"], thumb_img,
                                      env["thumb_size"] if env["resize"] else None)
            if w:
                warnings.append(w)
            with open(login_img, "rb") as f:
                login_bytes = f.read()
            with open(thumb_img, "rb") as f:
                thumb_bytes = f.read()

        applied = {"login": False, "desktop": False,
                   "users_ok": [], "users_failed": []}

        if need_login:
            applied["login"] = self._apply_login(login_bytes)
            self._update_login_text(env, src, warnings)

        if need_desktop:
            applied["desktop"] = self._apply_desktop(login_bytes, thumb_bytes)

        if need_user:
            self._apply_users(env, src, work, applied, warnings)

        if not (applied["login"] or applied["desktop"] or applied["users_ok"]):
            raise RuntimeError("没有成功写入任何壁纸，请检查权限与配置")
        return applied

    def _apply_login(self, data):
        count = 0
        for name in LOGIN_FILES:
            key = os.path.join(LOGIN_DIR, name)
            self.backup.ensure(key)
            atomic_write_bytes(resolve_target(key), data, mode=0o644)
            count += 1
        log.info("[应用] 登录壁纸 %d 个文件", count)
        return True

    def _apply_desktop(self, login_bytes, thumb_bytes):
        count = 0
        for scale in DESKTOP_SIZES:
            for kind in DESKTOP_TYPES:
                base = os.path.join(DESKTOP_BASE, scale, kind)
                for name, data in (("dsm7_01.jpg", login_bytes),
                                   ("thumbnail_01.jpg", thumb_bytes)):
                    key = os.path.join(base, name)
                    self.backup.ensure(key)
                    full = resolve_target(key)
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    atomic_write_bytes(full, data, mode=0o644)
                    count += 1
        log.info("[应用] 桌面壁纸 %d 个文件", count)
        return True

    def _apply_users(self, env, src, work, applied, warnings):
        user_src = src
        if env["user_dir"]:
            try:
                path = pick_local_image(env["user_dir"], env["local_recursive"])
                with open(path, "rb") as f:
                    head = f.read(32)
                if sniff_image(head):
                    user_src = {"source": "local", "path": path,
                                "title": os.path.basename(path)}
                else:
                    warnings.append("用户壁纸目录中的文件不是图片，已回退使用主图源")
            except SourceError as e:
                warnings.append("用户壁纸目录不可用（%s），已回退使用主图源" % e)

        wpath = os.path.join(work, "user.jpg")
        tpath = os.path.join(work, "user_thumb.jpg")
        w = self.converter.render(user_src["path"], wpath,
                                  env["user_size"] if env["resize"] else None)
        if w:
            warnings.append(w)
        w = self.converter.render(user_src["path"], tpath,
                                  env["user_thumb_size"] if env["resize"] else None)
        if w:
            warnings.append(w)
        with open(wpath, "rb") as f:
            user_bytes = f.read()
        with open(tpath, "rb") as f:
            thumb_bytes = f.read()

        try:
            users_gid = grp.getgrnam("users").gr_gid
        except KeyError:
            users_gid = None

        for name in env["user_names"]:
            try:
                pw = pwd.getpwnam(name)
            except KeyError:
                applied["users_failed"].append({"user": name, "error": "用户不存在"})
                warnings.append("用户不存在，已跳过: %s" % name)
                continue
            try:
                pref = os.path.join(USER_PREF_DIR, name)
                full_dir = resolve_target(pref)
                created = not os.path.isdir(full_dir)
                os.makedirs(full_dir, exist_ok=True)
                if created:
                    os.chmod(full_dir, 0o755)
                    try:
                        os.chown(full_dir, pw.pw_uid,
                                 users_gid if users_gid is not None else pw.pw_gid)
                    except OSError:
                        pass
                for fname, data in (("wallpaper", user_bytes),
                                    ("1.jpg", user_bytes),
                                    ("1thumb.jpg", thumb_bytes)):
                    key = os.path.join(pref, fname)
                    self.backup.ensure(key)
                    atomic_write_bytes(resolve_target(key), data, mode=0o644,
                                       uid=pw.pw_uid,
                                       gid=users_gid if users_gid is not None else pw.pw_gid)
                applied["users_ok"].append(name)
                log.info("[应用] 用户 %s 的桌面壁纸", name)
            except OSError as e:
                applied["users_failed"].append({"user": name, "error": str(e)})
                warnings.append("用户 %s 壁纸写入失败: %s" % (name, e))

    def _update_login_text(self, env, src, warnings):
        keys = ["login_background_customize"]
        updates = {"login_background_customize": "yes"}
        if src["source"] == "bing" and env["set_login_text"]:
            if src.get("title"):
                updates["login_welcome_title"] = src["title"]
                keys.append("login_welcome_title")
            chinese, english = parse_copyright(src.get("copyright") or "")
            message = "%s - %s" % (chinese, english)
            updates["login_welcome_msg"] = message
            keys.append("login_welcome_msg")
        full = resolve_target(SYNOINFO_CONF)
        if not os.path.isfile(full):
            warnings.append("未找到 /etc/synoinfo.conf，登录页标题未更新")
            return
        self.backup.snapshot_synoinfo(keys)
        try:
            rewrite_synoinfo(full, updates=updates)
        except (RuntimeError, OSError) as e:
            warnings.append("登录页信息写入失败: %s" % e)

    def _save_bing(self, env, src, warnings):
        save_dir = rooted(env["save_dir"])
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as e:
            warnings.append("无法创建必应壁纸保存目录 %s: %s" % (env["save_dir"], e))
            return None
        chinese, english = parse_copyright(src.get("copyright") or "")
        date = src.get("date") or time.strftime("%Y%m%d")
        name = "%s@%s-%s.jpg" % (date, safe_filename(chinese, 60),
                                 safe_filename(english, 60))
        dst = os.path.join(save_dir, name)
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            log.info("[保存] 原图已存在: %s", dst)
            return dst
        try:
            with open(src["path"], "rb") as f:
                atomic_write_bytes(dst, f.read(), mode=0o644)
            log.info("[保存] 必应原图 -> %s", dst)
            return dst
        except OSError as e:
            warnings.append("保存必应原图失败: %s" % e)
            return None

    def _write_preview(self, src_path, env):
        try:
            os.makedirs(self.ui_dir, exist_ok=True)
            dst = os.path.join(self.ui_dir, "preview.jpg")
            return self.converter.render(
                src_path, dst, "960x600" if env["resize"] else None)
        except OSError as e:
            log.warning("[预览] 生成失败: %s", e)
            return None

    # -- 状态 -------------------------------------------------------------
    def _record_success(self, result, src):
        st = self.state.data
        ts = result["timestamp"]
        st["last_attempt_time"] = ts
        st["last_success_time"] = ts
        st["last_ok"] = True
        st["last_error"] = None
        st["last_source"] = src["source"]
        st["image_path"] = src.get("url") or src.get("path")
        st["saved_to"] = result.get("saved_to")
        st["bing_date"] = src.get("date")
        st["bing_title"] = src.get("title")
        st["bing_copyright"] = src.get("copyright")
        st["last_result"] = _trim_result(result)
        st["preview_version"] = int(st.get("preview_version") or 0) + 1
        st["run_count"] = int(st.get("run_count") or 0) + 1
        self.state.save()

    def _record_failure(self, result):
        st = self.state.data
        st["last_attempt_time"] = result["timestamp"]
        st["last_ok"] = False
        st["last_error"] = result.get("error")
        st["last_result"] = _trim_result(result)
        st["run_count"] = int(st.get("run_count") or 0) + 1
        self.state.save()

    def restore(self):
        with self.lock:
            self.is_running = True
            try:
                info = self.backup.restore()
                preview = os.path.join(self.ui_dir, "preview.jpg")
                if os.path.exists(preview):
                    try:
                        os.unlink(preview)
                    except OSError:
                        pass
                st = self.state.data
                st["last_ok"] = None
                st["last_error"] = None
                st["last_source"] = "default"
                st["image_path"] = None
                st["saved_to"] = None
                st["bing_date"] = None
                st["bing_title"] = None
                st["bing_copyright"] = None
                st["restored_at"] = now_str()
                # 让"固定间隔"从恢复时刻重新计时，避免刚恢复就被调度器重新覆盖
                st["last_attempt_time"] = st["restored_at"]
                st["last_result"] = {
                    "ok": True, "restored": True, "timestamp": st["restored_at"],
                    "restored_files": info.get("restored", 0),
                    "removed_files": info.get("removed", 0),
                }
                st["preview_version"] = int(st.get("preview_version") or 0) + 1
                self.state.save()
                log.info("[恢复] 已恢复默认壁纸：恢复 %s，删除 %s，缺失 %s",
                         info.get("restored"), info.get("removed"), info.get("missing"))
                return {"ok": True, "restored": True, "detail": info,
                        "timestamp": st["restored_at"]}
            except Exception as e:  # noqa: BLE001
                log.exception("[恢复] 失败: %s", e)
                return {"ok": False, "error": str(e)}
            finally:
                self.is_running = False

    def check_dir(self, path, recursive=True):
        full = rooted(path or "")
        if not path:
            return {"exists": False, "error": "路径为空"}
        if not os.path.isdir(full):
            return {"exists": False, "is_dir": False,
                    "error": "目录不存在或不可访问"}
        sample = []
        count = 0
        try:
            for base, dirs, names in os.walk(full, followlinks=False):
                if recursive:
                    dirs[:] = [d for d in dirs
                               if d not in SKIP_DIRS and not d.startswith(".")]
                else:
                    dirs[:] = []
                for name in names:
                    if name.startswith("."):
                        continue
                    if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                        continue
                    count += 1
                    if len(sample) < 8:
                        sample.append(os.path.join(base, name))
                    if count >= 50000:
                        break
                if count >= 50000:
                    break
        except OSError as e:
            return {"exists": True, "is_dir": True, "error": str(e)}
        return {"exists": True, "is_dir": True, "recursive": bool(recursive),
                "count": count, "sample": sample, "truncated": count >= 50000}


def _trim_result(result):
    keep = {}
    for k in ("ok", "skipped", "error", "timestamp", "source", "title",
              "copyright", "bing_date", "saved_to", "warnings", "manual",
              "reason", "message", "restored"):
        if k in result:
            keep[k] = result[k]
    if "duration_ms" in result:
        keep["duration_ms"] = result["duration_ms"]
    applied = result.get("applied")
    if isinstance(applied, dict):
        keep["applied"] = {
            "login": applied.get("login"),
            "desktop": applied.get("desktop"),
            "users_ok": applied.get("users_ok") or [],
            "users_failed": applied.get("users_failed") or [],
        }
    return keep


def _dedupe(items):
    out = []
    for it in items:
        if it and it not in out:
            out.append(it)
    return out


# ---------------------------------------------------------------- 调度 ----

def compute_next(env, st, now):
    mode = env.get("schedule_mode") or "off"
    if mode == "off":
        return None
    if mode == "daily":
        hh, mm = [int(x) for x in env.get("daily_time", "04:00").split(":")]
        lt = time.localtime(now)
        try:
            candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                     hh, mm, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            return None
        if candidate <= now:
            candidate += 86400
    else:
        last = parse_time(st.get("last_attempt_time"))
        interval = int(env.get("interval_minutes") or 720) * 60
        candidate = (last + interval) if last else (now + 60)
        if candidate <= now:
            candidate = now + 60

    if st.get("last_ok") is False:
        retry = int(env.get("retry_minutes") or 0) * 60
        if retry > 0:
            candidate = min(candidate, now + retry)
    return candidate


class Scheduler(threading.Thread):
    def __init__(self, engine, cfg, state):
        super().__init__(name="wallpaper-scheduler", daemon=True)
        self.engine = engine
        self.cfg = cfg
        self.state = state
        self.stop_event = threading.Event()

    def run(self):
        if self.cfg.data.get("run_on_start"):
            if self.stop_event.wait(10):
                return
            log.info("[调度] 启动时执行一次更换")
            self.engine.run(manual=False, force=True, reason="startup")
        while not self.stop_event.is_set():
            try:
                self.cfg.reload()
                now = time.time()
                nxt = compute_next(self.cfg.data, self.state.data, now)
                if nxt is None:
                    wait = 30
                else:
                    wait = max(1, min(30, nxt - now))
                if self.stop_event.wait(wait):
                    break
                if nxt is not None and time.time() >= nxt - 0.5:
                    log.info("[调度] 到达计划时间，开始更换")
                    self.engine.run(manual=False, force=False, reason="schedule")
            except Exception as e:  # noqa: BLE001
                log.exception("[调度] 异常: %s", e)
                self.stop_event.wait(30)

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------- 认证 ----

def _admin_lookup(user):
    """返回 True/False/None（None=无法判定，例如域用户）。带 60s 缓存。"""
    if not user:
        return False
    if user == "admin":
        return True
    cache = _ADMIN_CACHE.get(user)
    now = time.time()
    if cache and now - cache[1] < 60:
        return cache[0]
    result = None
    try:
        p = subprocess.run(["id", "-Gn", user], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=5)
        text = p.stdout.decode("utf-8", "replace").strip()
        if p.returncode == 0 and text:
            result = "administrators" in text.split()
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None:
        try:
            p = subprocess.run(["synogroup", "--get", "administrators"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=5)
            text = p.stdout.decode("utf-8", "replace")
            m = re.search(r"Group Members:\s*\[([^\]]*)\]", text)
            if m is not None:
                members = [x.strip() for x in m.group(1).split(",") if x.strip()]
                result = user in members
        except (OSError, subprocess.SubprocessError):
            result = None
    _ADMIN_CACHE[user] = (result, now)
    return result


_ADMIN_CACHE = {}


class Auth:
    """unix：信任 api.cgi 用官方 authenticate.cgi 校验后的用户名头；token 只读兜底。
    auto：自行调用 authenticate.cgi（TCP 调试）；token/none 语义同上游。"""

    def __init__(self, mode, token_path):
        self.mode = mode
        self.token = None
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                self.token = f.read().strip() or None
        except OSError:
            pass

    def _token_of(self, handler):
        tok = handler.headers.get("X-Wallpaper-Token", "").strip()
        if tok:
            return tok
        cookies = handler.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)wallpaper_token=([0-9a-fA-F]{16,})", cookies)
        return m.group(1) if m else None

    def _session_user(self, handler):
        user, status = dsm_session_user(
            handler.client_address[0] if isinstance(handler.client_address, tuple) else "",
            handler.headers.get("Cookie", ""))
        return user if status == "ok" else None

    def check(self, handler):
        """返回 (allowed, user, via)。会话身份优先于 token（页面两者都会带）。"""
        if self.mode == "none":
            return True, handler.headers.get("X-Wallpaper-User", "").strip() or None, "none"
        tok = self._token_of(handler)
        token_ok = bool(tok and self.token and hmac.compare_digest(tok, self.token))
        user = handler.headers.get("X-Wallpaper-User", "").strip() or None
        if self.mode == "token":
            return (True, user, "token") if token_ok else (False, None, "unauthorized")
        if self.mode == "unix":
            if user:
                return True, user, "session"
            if token_ok:
                return True, None, "token"
            return False, None, "unauthorized"
        # auto：daemon 自己再校验一次 DSM 会话（TCP 调试场景），失败后回退 token
        session = self._session_user(handler)
        if session:
            return True, session, "session"
        if token_ok:
            return True, user, "token"
        return False, None, "unauthorized"


def dsm_session_user(remote_addr, cookie):
    env = dict(os.environ)
    env.update({
        "HTTP_COOKIE": cookie or "",
        "REMOTE_ADDR": remote_addr or "",
        "SERVER_ADDR": "127.0.0.1",
        "REQUEST_METHOD": "GET",
        "PATH": "/usr/syno/bin:/usr/bin:/bin:/usr/syno/sbin:/usr/sbin:/sbin",
    })
    try:
        p = subprocess.run([AUTHENTICATE_CGI], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, "unavailable"
    return (p.stdout.decode("utf-8", "replace").strip() or None), "ok"


# ---------------------------------------------------------------- HTTP ----

class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "autowallpaper/" + PKG_VERSION
    engine = None       # type: Engine
    auth = None         # type: Auth
    log_path = None
    started_at = 0.0

    def log_message(self, fmt, *args):
        try:
            code = int(args[1])
        except (IndexError, ValueError, TypeError):
            code = 0
        if code >= 400:
            log.info("[HTTP] %s %s -> %s", self.command, self.path, code)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorize(self):
        allowed, user, via = self.auth.check(self)
        if not allowed:
            self._json(401, {"error": "unauthorized",
                             "message": "需要登录 DSM 后访问"})
            return None
        write = self.command in ("POST", "PUT", "DELETE")
        if self.engine.cfg.data.get("require_admin", True) and via != "none":
            if via == "token" and write:
                if not self.engine.cfg.data.get("token_write", False):
                    self._json(403, {"error": "forbidden",
                                     "message": "当前为只读 token 认证，修改配置需要 DSM 管理员会话；"
                                                "如通过自定义域名/反代访问，可在高级设置启用 token 写入"})
                    return None
            if via == "session":
                adm = _admin_lookup(user)
                if adm is False:
                    self._json(403, {"error": "forbidden",
                                     "message": "只有 DSM 管理员可以修改系统壁纸"})
                    return None
                if adm is None:
                    log.warning("[认证] 无法确认用户 %s 的管理员身份，已放行（请检查域账号权限）", user)
        return user or ""

    def do_GET(self):
        user = self._authorize()
        if user is None:
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            return self._json(200, self._status(user))
        if path == "/api/config":
            return self._json(200, self.engine.cfg.masked())
        if path == "/api/defaults":
            data = dict(DEFAULT_CONFIG)
            data.pop("require_admin", None)
            return self._json(200, data)
        if path == "/api/logs":
            return self._logs()
        return self._json(404, {"error": "not_found"})

    def do_PUT(self):
        user = self._authorize()
        if user is None:
            return
        if self.path.split("?", 1)[0] != "/api/config":
            return self._json(404, {"error": "not_found"})
        payload, err = self._read_json(required=True)
        if payload is None:
            return self._json(400, {"error": "bad_request", "message": err})
        ok, errors = self.engine.cfg.validate_and_apply(payload)
        if not ok:
            return self._json(422, {"error": "; ".join(errors), "detail": errors})
        log.info("[配置] 配置已更新（用户 %s）", user or "-")
        return self._json(200, self.engine.cfg.masked())

    def do_POST(self):
        user = self._authorize()
        if user is None:
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/run":
            payload, _ = self._read_json(required=False)
            payload = payload if isinstance(payload, dict) else {}
            force = bool(payload.get("force", True))
            result = self.engine.run(manual=True, force=force, reason="manual")
            if result.get("ok"):
                return self._json(200, result)
            return self._json(502, {"error": result.get("error") or "更换失败",
                                    "result": result})
        if path == "/api/restore":
            self._read_json(required=False)   # 排空请求体，避免 keep-alive 串包
            result = self.engine.restore()
            if result.get("ok"):
                return self._json(200, result)
            return self._json(500, result)
        if path == "/api/check_dir":
            payload, _ = self._read_json(required=False)
            payload = payload if isinstance(payload, dict) else {}
            path_value = str(payload.get("path") or "").strip()
            recursive = bool(payload.get("recursive", True))
            return self._json(200, self.engine.check_dir(path_value, recursive))
        return self._json(404, {"error": "not_found"})

    def _read_json(self, required=True):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "bad Content-Length"
        if n <= 0:
            return (None, "empty body") if required else (None, None)
        if n > 512 * 1024:
            return None, "body too large"
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")), None
        except (ValueError, UnicodeDecodeError) as e:
            return None, "invalid JSON: %s" % e

    def _logs(self):
        try:
            n = int(urllib.parse.parse_qs(self.path.split("?", 1)[1]).get("n", ["300"])[0])
        except (IndexError, ValueError):
            n = 300
        n = max(1, min(n, 2000))
        lines = []
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-n:]
        except OSError:
            pass
        return self._json(200, {"lines": [l.rstrip("\n") for l in lines]})

    def _status(self, user):
        eng = self.engine
        st = dict(eng.state.data)
        env = eng.cfg.data
        now = time.time()
        nxt = compute_next(env, st, now)
        preview = os.path.join(eng.ui_dir, "preview.jpg")
        version = int(st.get("preview_version") or 0)
        st.update({
            "running": True,
            "busy": eng.is_running,
            "next_run_time": now_str(nxt) if nxt else None,
            "next_run_in_seconds": int(nxt - now) if nxt else None,
            "uptime_seconds": int(now - self.started_at),
            "package_version": PKG_VERSION,
            "python_version": platform.python_version(),
            "converter": {"available": eng.converter.available,
                          "path": eng.converter.path,
                          "label": eng.converter.label},
            "preview_url": ("preview.jpg?v=%d" % version) if os.path.isfile(preview) else None,
            "config": {
                "source": env["source"],
                "bing_market": env["bing_market"],
                "target_login": env["target_login"],
                "target_desktop": env["target_desktop"],
                "target_user": env["target_user"],
                "user_names": env["user_names"],
                "schedule_mode": env["schedule_mode"],
                "daily_time": env["daily_time"],
                "interval_minutes": env["interval_minutes"],
                "resize": env["resize"],
                "save_bing": env["save_bing"],
                "save_dir": env["save_dir"],
            },
            "user": user or None,
            "system_root": SYSTEM_ROOT or None,
            "log_path": self.log_path,
        })
        return st


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """连接被 CGI/浏览器提前关闭时不打印整段 traceback，保持日志干净。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        log.warning("[HTTP] 请求处理异常: %s", exc)


class UnixThreadingHTTPServer(QuietThreadingHTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        if os.path.exists(self.server_address):
            os.unlink(self.server_address)
        socketserver.TCPServer.server_bind(self)
        self.server_name = "autowallpaper"
        self.server_port = 0
        try:
            os.chmod(self.server_address, 0o660)
        except OSError:
            pass
        try:
            gid = grp.getgrnam("http").gr_gid
            os.chown(self.server_address, 0, gid)
        except (KeyError, OSError):
            try:
                os.chmod(self.server_address, 0o666)
            except OSError:
                pass


class TcpThreadingHTTPServer(QuietThreadingHTTPServer):
    allow_reuse_address = True


# ---------------------------------------------------------------- 日志 ----

class CappedFileHandler(logging.Handler):
    def __init__(self, path, cfg=None, default_max=1024 * 1024, default_backups=3):
        super().__init__()
        self.path = path
        self.cfg = cfg
        self.default_max = default_max
        self.default_backups = default_backups
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                            datefmt="%Y-%m-%d %H:%M:%S"))

    def _limits(self):
        if self.cfg is not None:
            try:
                return (max(64 * 1024, int(self.cfg.data.get("log_max_bytes")
                                           or self.default_max)),
                        max(1, min(10, int(self.cfg.data.get("log_backup_count")
                                           or self.default_backups))))
            except (TypeError, ValueError):
                pass
        return self.default_max, self.default_backups

    def emit(self, record):
        try:
            line = self.format(record) + "\n"
            max_bytes, backups = self._limits()
            self._rotate(len(line.encode("utf-8")), max_bytes, backups)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def _rotate(self, incoming, max_bytes, backups):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size + incoming <= max_bytes:
            return
        for i in range(backups - 1, 0, -1):
            src, dst = "%s.%d" % (self.path, i), "%s.%d" % (self.path, i + 1)
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        try:
            os.replace(self.path, "%s.1" % self.path)
        except OSError:
            pass

    def rotate_on_start(self):
        try:
            if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
                return
        except OSError:
            return
        _, backups = self._limits()
        for i in range(backups - 1, 0, -1):
            src, dst = "%s.%d" % (self.path, i), "%s.%d" % (self.path, i + 1)
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        try:
            os.replace(self.path, "%s.1" % self.path)
        except OSError:
            pass


def setup_logging(log_path, console=True, cfg=None):
    log.setLevel(logging.INFO)
    if log_path:
        max_bytes = 1024 * 1024
        backups = 3
        if cfg is not None:
            max_bytes = cfg.data.get("log_max_bytes", max_bytes)
            backups = cfg.data.get("log_backup_count", backups)
        fh = CappedFileHandler(log_path, cfg=cfg, default_max=max_bytes,
                               default_backups=backups)
        fh.setLevel(logging.INFO)
        fh.rotate_on_start()
        log.addHandler(fh)
    if console or not log_path:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)


# ---------------------------------------------------------------- 入口 ----

def derive_paths(config_path, state_path, log_path, listen):
    pkgvar = os.environ.get("SYNOPKG_PKGVAR")
    if not pkgvar:
        # 已安装环境：优先使用持久数据目录（DSM 下 /var/packages/<pkg>/var 是符号链接）
        installed = "/var/packages/%s/var" % PKG_NAME
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cand = os.path.join(here, "var")
        if os.path.isdir(installed):
            pkgvar = installed
        elif os.path.isdir(cand):
            pkgvar = cand
        else:
            pkgvar = os.path.join(os.getcwd(), "var")
    return (config_path or os.path.join(pkgvar, "config.json"),
            state_path or os.path.join(pkgvar, "state.json"),
            log_path or os.path.join(pkgvar, "wallpaper.log"),
            listen or os.environ.get("WALLPAPER_LISTEN",
                                     "unix:/var/run/autowallpaper.sock"))


def main():
    ap = argparse.ArgumentParser(description="DSM Auto Change Wallpaper daemon")
    ap.add_argument("--config")
    ap.add_argument("--state")
    ap.add_argument("--log")
    ap.add_argument("--listen", help="unix:PATH 或 tcp:HOST:PORT")
    ap.add_argument("--auth", choices=("unix", "auto", "token", "none"),
                    default=os.environ.get("WALLPAPER_AUTH", ""))
    ap.add_argument("--root", default=os.environ.get("WALLPAPER_ROOT", ""),
                    help="测试用系统根目录（生产留空）")
    ap.add_argument("--ui-dir", default="")
    ap.add_argument("--once", action="store_true", help="执行一次后退出")
    ap.add_argument("--restore", action="store_true", help="恢复默认壁纸后退出")
    ap.add_argument("--no-console-log", action="store_true")
    args = ap.parse_args()

    global SYSTEM_ROOT
    if args.root:
        SYSTEM_ROOT = os.path.abspath(args.root).rstrip("/")
        if not os.path.isdir(SYSTEM_ROOT):
            print("系统根目录不存在: %s" % SYSTEM_ROOT, file=sys.stderr)
            return 2

    cfg_path, state_path, log_path, listen = derive_paths(
        args.config, args.state, args.log, args.listen)
    if not args.auth:
        args.auth = "unix" if listen.startswith("unix:") else "auto"

    cfg = Config(cfg_path)
    setup_logging(None if (args.once or args.restore) else log_path,
                  console=not args.no_console_log or args.once or args.restore,
                  cfg=cfg)
    state = State(state_path)
    ui_dir = args.ui_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
    converter = Converter()
    engine = Engine(cfg, state, os.path.dirname(cfg_path), ui_dir, converter)

    if args.once or args.restore:
        if args.restore:
            result = engine.restore()
        else:
            result = engine.run(manual=True, force=True, reason="cli")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    token_path = os.path.join(os.path.dirname(cfg_path), "token")
    auth = Auth(args.auth, token_path)

    if listen.startswith("unix:"):
        sock_path = listen[5:]
        os.makedirs(os.path.dirname(sock_path), exist_ok=True)
        server = UnixThreadingHTTPServer(sock_path, ApiHandler)
    elif listen.startswith("tcp:"):
        _, host, port = listen.split(":", 2)
        server = TcpThreadingHTTPServer((host, int(port)), ApiHandler)
    else:
        log.error("非法 --listen: %s", listen)
        return 2

    ApiHandler.engine = engine
    ApiHandler.auth = auth
    ApiHandler.log_path = log_path
    ApiHandler.started_at = time.time()

    scheduler = Scheduler(engine, cfg, state)

    def shutdown(signum, frame):
        log.info("收到退出信号，保存状态并停止服务")
        scheduler.stop()
        state.save()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("=" * 56)
    log.info("壁纸自动更换 v%s 启动", PKG_VERSION)
    log.info("监听: %s  认证: %s", listen, args.auth)
    log.info("图源: %s  目标: 登录=%s 桌面=%s 用户=%s",
             cfg.data["source"], cfg.data["target_login"],
             cfg.data["target_desktop"], cfg.data["target_user"])
    log.info("调度: %s %s", cfg.data["schedule_mode"],
             cfg.data["daily_time"] if cfg.data["schedule_mode"] == "daily"
             else cfg.data["interval_minutes"])
    log.info("ImageMagick: %s", converter.path or "未安装（将直接复制原图）")
    log.info("=" * 56)

    scheduler.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        scheduler.stop()
        state.save()
        if listen.startswith("unix:") and os.path.exists(listen[5:]):
            try:
                os.unlink(listen[5:])
            except OSError:
                pass
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
