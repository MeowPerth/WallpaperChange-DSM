#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地无 NAS 测试：直接驱动 daemon 的核心逻辑（含 fake root 写入/恢复）。"""
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON = os.path.join(ROOT, "src", "bin", "wallpaper-daemon.py")
ICON = os.path.join(ROOT, "src", "ui", "images", "wallpaper_256.png")

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("wallpaper_daemon", DAEMON)
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)


def write_file(path, data=b"x"):
    if isinstance(data, str):
        data = data.encode("utf-8")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp-test-")
        self.root = os.path.join(self.tmp, "root")
        self.var = os.path.join(self.tmp, "var")
        self.ui = os.path.join(self.tmp, "ui")
        os.makedirs(self.var)
        os.makedirs(self.ui)
        wp.SYSTEM_ROOT = self.root

        self.photo_sys = "/photo"
        self.photo = os.path.join(self.root, "photo")
        os.makedirs(self.photo)
        shutil.copyfile(ICON, os.path.join(self.photo, "a.png"))
        shutil.copyfile(ICON, os.path.join(self.photo, "b.png"))

        # 原始系统壁纸（部分存在、部分缺失，用于验证恢复逻辑）
        self.login_dir = os.path.join(self.root, "usr/syno/etc")
        self.login_original = b"ORIGINAL-LOGIN-BYTES"
        write_file(os.path.join(self.login_dir, "login_background.jpg"),
                   self.login_original)
        write_file(os.path.join(self.login_dir, "login_background_hd.jpg"),
                   b"ORIGINAL-LOGIN-HD")
        self.desktop = os.path.join(self.root, "usr/syno/synoman/webman/resources/images")
        for scale in ("1x", "2x"):
            for kind in ("default_login_background", "default_wallpaper"):
                write_file(os.path.join(self.desktop, scale, kind, "dsm7_01.jpg"),
                           ("ORIG-" + scale + "-" + kind).encode())
                write_file(os.path.join(self.desktop, scale, kind, "thumbnail_01.jpg"),
                           b"ORIG-THUMB")

        self.synoinfo = os.path.join(self.root, "etc/synoinfo.conf")
        write_file(self.synoinfo,
                   'productversion="7.2"\nlogin_welcome_title="ORIGINAL TITLE"\n')

        self.cfg_path = os.path.join(self.var, "config.json")
        cfg = dict(wp.DEFAULT_CONFIG)
        cfg.update({
            "source": "local",
            "local_dir": self.photo_sys,
            "target_login": True,
            "target_desktop": True,
            "target_user": True,
            "user_names": ["root"],
            "resize": False,
            "schedule_mode": "off",
            "save_bing": False,
            "retry_minutes": 0,
        })
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        self.cfg = wp.Config(self.cfg_path)
        self.state = wp.State(os.path.join(self.var, "state.json"))
        self.conv = wp.Converter()
        self.engine = wp.Engine(self.cfg, self.state, self.var, self.ui, self.conv)

        logging.basicConfig(level=logging.ERROR)

    def tearDown(self):
        wp.SYSTEM_ROOT = ""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_parse_copyright(self):
        zh, en = wp.parse_copyright("上海外滩，中国 (© John Doe/Getty Images)")
        self.assertIn("上海外滩", zh)
        self.assertIn("John Doe", en)
        zh, en = wp.parse_copyright("")
        self.assertTrue(zh and en)
        self.assertEqual(wp.safe_filename('a/b:c*?"<>|'), "abc")

    def test_02_compute_next(self):
        env = dict(wp.DEFAULT_CONFIG)
        env.update({"schedule_mode": "daily", "daily_time": "04:00", "retry_minutes": 0})
        now = time.mktime((2026, 10, 31, 3, 0, 0, 0, 0, -1))
        nxt = wp.compute_next(env, {"last_ok": True}, now)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)),
                         "2026-10-31 04:00")
        now2 = time.mktime((2026, 10, 31, 5, 0, 0, 0, 0, -1))
        nxt = wp.compute_next(env, {"last_ok": True}, now2)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)),
                         "2026-11-01 04:00")
        env.update({"schedule_mode": "off"})
        self.assertIsNone(wp.compute_next(env, {}, now))
        env.update({"schedule_mode": "interval", "interval_minutes": 60,
                    "retry_minutes": 30})
        st = {"last_ok": False, "last_attempt_time": "2026-10-31 03:00:00"}
        nxt = wp.compute_next(env, st, now)
        self.assertEqual(nxt, now + 30 * 60)

    def test_03_validate_config(self):
        ok, errs = self.cfg.validate_and_apply({
            "source": "local", "local_dir": self.photo_sys,
            "target_login": True, "target_desktop": True, "target_user": True,
            "user_names": "",
        })
        self.assertFalse(ok)
        self.assertTrue(any("用户名" in e for e in errs))

        ok, errs = self.cfg.validate_and_apply({
            "source": "bing", "target_login": True, "target_desktop": False,
            "target_user": False, "bing_market": "zh-cn", "daily_time": "04:00",
            "schedule_mode": "daily", "save_bing": False,
            "login_size": "1920x1200", "thumb_size": "160x100",
            "user_size": "1920x1200", "user_thumb_size": "120x120",
        })
        self.assertTrue(ok, errs)

    def test_04_apply_and_restore(self):
        # 恢复为本地图源配置（test_03 已改成 bing）
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(dict({
                "source": "local", "local_dir": self.photo_sys,
                "target_login": True, "target_desktop": True, "target_user": True,
                "user_names": ["root"], "resize": False, "schedule_mode": "off",
                "retry_minutes": 0,
            }, **{k: v for k, v in wp.DEFAULT_CONFIG.items()
                  if k not in ("source", "local_dir", "target_login", "target_desktop",
                               "target_user", "user_names", "resize", "schedule_mode",
                               "retry_minutes")}), f, ensure_ascii=False)
        self.cfg.reload(force=True)

        r = self.engine.run(manual=True, force=True, reason="test")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["applied"]["login"])
        self.assertTrue(r["applied"]["desktop"])
        self.assertEqual(r["applied"]["users_ok"], ["root"])

        src = os.path.join(self.photo, "b.png")
        # 无 ImageMagick 时直接复制，内容应与某张源图一致
        applied = read_file(os.path.join(self.login_dir, "login_background.jpg"))
        self.assertIn(applied, (read_file(src), read_file(os.path.join(self.photo, "a.png"))))
        self.assertTrue(os.path.isfile(os.path.join(self.login_dir,
                                                    "SYNO.SDS.VideoStation.AppInstance_login_background.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(
            self.desktop, "2x", "default_wallpaper", "dsm7_01.jpg")))
        user_dir = os.path.join(self.root, "usr/syno/etc/preference/root")
        self.assertTrue(os.path.isfile(os.path.join(user_dir, "wallpaper")))
        self.assertTrue(os.path.isfile(os.path.join(user_dir, "1thumb.jpg")))

        with open(self.synoinfo, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('login_background_customize="yes"', text)
        self.assertIn('productversion="7.2"', text)

        self.assertTrue(os.path.isfile(os.path.join(self.ui, "preview.jpg")))
        self.assertGreaterEqual(self.state.data["preview_version"], 1)
        self.assertTrue(self.state.data["last_ok"])

        # 第二次运行（手动 force）应成功
        r2 = self.engine.run(manual=True, force=True, reason="test")
        self.assertTrue(r2["ok"], r2)

        # 恢复
        res = self.engine.restore()
        self.assertTrue(res["ok"], res)
        self.assertEqual(read_file(os.path.join(self.login_dir, "login_background.jpg")),
                         self.login_original)
        self.assertEqual(read_file(os.path.join(self.login_dir, "login_background_hd.jpg")),
                         b"ORIGINAL-LOGIN-HD")
        self.assertFalse(os.path.exists(os.path.join(
            self.login_dir, "SYNO.SDS.VideoStation.AppInstance_login_background.jpg")))
        self.assertEqual(read_file(os.path.join(
            self.desktop, "1x", "default_wallpaper", "dsm7_01.jpg")), b"ORIG-1x-default_wallpaper")
        with open(self.synoinfo, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("login_background_customize", text)
        self.assertIn('login_welcome_title="ORIGINAL TITLE"', text)
        self.assertFalse(os.path.exists(os.path.join(self.ui, "preview.jpg")))
        self.assertIsNone(self.state.data["last_ok"])

    def test_07_auth(self):
        token_path = os.path.join(self.var, "token")
        write_file(token_path, b"a" * 48)
        auth = wp.Auth("unix", token_path)

        class H:
            client_address = ("127.0.0.1", 1234)

            def __init__(self, headers):
                self.headers = headers

        ok, user, via = auth.check(H({"X-Wallpaper-User": "alice",
                                      "X-Wallpaper-Token": "a" * 48}))
        self.assertTrue(ok)
        self.assertEqual((user, via), ("alice", "session"))
        ok, user, via = auth.check(H({"X-Wallpaper-Token": "a" * 48}))
        self.assertEqual((ok, user, via), (True, None, "token"))
        ok, _, via = auth.check(H({"X-Wallpaper-Token": "b" * 48}))
        self.assertFalse(ok)
        self.assertEqual(via, "unauthorized")

    def test_06_fetch_bing_fallback(self):
        icon = read_file(ICON)
        calls = []

        def fake(url, timeout, retries, max_bytes=wp.MAX_IMAGE_BYTES, label="资源"):
            calls.append(url)
            if "HPImageArchive" in url:
                return json.dumps({"images": [{
                    "url": "/th?id=OHR.X.jpg&rf=1&pid=2",
                    "urlbase": "/th?id=OHR.X",
                    "enddate": "20261031",
                    "title": "Test",
                    "copyright": "某地 (© A/B)",
                }]}).encode("utf-8")
            if "_UHD" in url:
                raise wp.SourceError("HTTP 404")
            return icon

        old = wp.http_get_bytes
        wp.http_get_bytes = fake
        try:
            info = wp.fetch_bing("zh-cn", True, 5, 2)
        finally:
            wp.http_get_bytes = old
        self.assertEqual(info["date"], "20261031")
        self.assertIn("OHR.X.jpg", info["url"])
        self.assertTrue(any("_UHD" in c for c in calls))
        self.assertTrue(any("HPImageArchive" in c for c in calls))

    def test_08_restore_safety(self):
        self.assertTrue(wp.safe_restore_target("/etc/synoinfo.conf"))
        self.assertTrue(wp.safe_restore_target(
            "/usr/syno/synoman/webman/resources/images/1x/default_wallpaper/dsm7_01.jpg"))
        self.assertTrue(wp.safe_restore_target(
            "/usr/syno/etc/preference/root/wallpaper"))
        self.assertFalse(wp.safe_restore_target("/etc/passwd"))
        self.assertFalse(wp.safe_restore_target(
            "/usr/syno/synoman/webman/resources/images/1x/default_wallpaper/../../../../etc/passwd"))
        self.assertEqual(wp.conf_quote('$(reboot)'), '"\$(reboot)"')

    def test_05_check_dir(self):
        info = self.engine.check_dir(self.photo_sys, recursive=True)
        self.assertTrue(info["exists"])
        self.assertEqual(info["count"], 2)
        bogus = self.engine.check_dir("/nope")
        self.assertFalse(bogus["exists"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
