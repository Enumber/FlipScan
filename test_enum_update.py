#!/usr/bin/env python3
# Copyright (C) 2026 ENum
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY; see the GNU General
# Public License (LICENSE) for details.

"""自动升级模块（enum_update）的纯单元测试。

一行网络都不会真的发出去：所有涉及 HTTP 的用例都把 urlopen 换成假的。
测试重点是两件事——版本比较不能错，以及**查更新失败时必须安静地什么都不做**，
因为后者一旦失效，用户会在没网的时候打不开程序。
"""
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error
from unittest import mock

import enum_update


class ParseVersionTests(unittest.TestCase):
    def test_strips_v_prefix(self):
        self.assertEqual(enum_update.parse_version("v1.2.3")[0], (1, 2, 3))
        self.assertEqual(enum_update.parse_version("V1.2.3")[0], (1, 2, 3))
        self.assertEqual(enum_update.parse_version("1.2.3")[0], (1, 2, 3))

    def test_build_metadata_is_ignored(self):
        self.assertEqual(enum_update.parse_version("1.2.3+build9")[0], (1, 2, 3))

    def test_prerelease_is_split_off(self):
        nums, pre = enum_update.parse_version("1.2.3-rc.1")
        self.assertEqual(nums, (1, 2, 3))
        self.assertEqual(pre, ("rc", "1"))

    def test_garbage_parses_to_zero_rather_than_raising(self):
        # 解析失败要退化成"最小版本"，这样最坏后果是"以为没有新版"，
        # 而不是把垃圾当成新版本弹更新提示。
        for junk in (None, "", "   ", "latest", 42, [], {}):
            self.assertEqual(enum_update.parse_version(junk)[0], (0,))


class CompareVersionsTests(unittest.TestCase):
    def test_numeric_segments_compare_as_numbers_not_strings(self):
        # 版本比较最经典的坑：按字符串比 "1.10" < "1.9"，是错的。
        self.assertEqual(enum_update.compare_versions("1.10", "1.9"), 1)
        self.assertTrue(enum_update.is_newer("1.10", "1.9"))
        self.assertTrue(enum_update.is_newer("1.10.0", "1.9.9"))
        self.assertTrue(enum_update.is_newer("2.0", "1.99"))

    def test_v_prefix_does_not_affect_comparison(self):
        self.assertEqual(enum_update.compare_versions("v1.2.0", "1.2.0"), 0)
        self.assertTrue(enum_update.is_newer("v1.3.0", "1.2.9"))
        self.assertFalse(enum_update.is_newer("v1.2.0", "v1.2.0"))

    def test_missing_segments_are_zero_padded(self):
        self.assertEqual(enum_update.compare_versions("1.2", "1.2.0"), 0)
        self.assertEqual(enum_update.compare_versions("1.2.1", "1.2"), 1)
        self.assertEqual(enum_update.compare_versions("1", "1.0.0"), 0)

    def test_release_beats_prerelease(self):
        self.assertTrue(enum_update.is_newer("1.2.0", "1.2.0-beta"))
        self.assertFalse(enum_update.is_newer("1.2.0-beta", "1.2.0"))
        self.assertTrue(enum_update.is_newer("1.2.0-rc.2", "1.2.0-rc.1"))
        # rc.10 > rc.2：预发布里的数字段也要按数值比
        self.assertTrue(enum_update.is_newer("1.2.0-rc.10", "1.2.0-rc.2"))

    def test_older_is_not_newer(self):
        self.assertFalse(enum_update.is_newer("1.0.0", "1.1.0"))
        self.assertFalse(enum_update.is_newer("1.1.0", "1.1.0"))
        self.assertTrue(enum_update.is_newer("1.1.1", "1.1.0"))

    def test_real_world_tags_from_our_own_releases(self):
        self.assertFalse(enum_update.is_newer("v1.1.0", "1.1.0"))   # vokey/flipscan
        self.assertFalse(enum_update.is_newer("v2.0.0", "2.0.0"))   # deskctl
        self.assertFalse(enum_update.is_newer("v1.1", "1.1"))       # BeeBEEP


def _fake_response(payload, status=200):
    """做一个假的 urlopen 返回值（支持 with 语句）。"""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    response = mock.MagicMock()
    response.read.return_value = body
    response.status = status
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *a: False
    return response


class FetchFailureTests(unittest.TestCase):
    """网络/API 出任何问题，都必须安静地返回 None——这是本模块的核心契约。"""

    def _assert_silent(self, side_effect):
        with mock.patch("urllib.request.urlopen", side_effect=side_effect):
            self.assertIsNone(enum_update.fetch_latest_release("Enumber/vokey"))
            # check_for_update 也必须跟着安静，不能因为拿不到 release 就炸
            self.assertIsNone(
                enum_update.check_for_update("Enumber/vokey", "1.0.0"))

    def test_no_network(self):
        self._assert_silent(urllib.error.URLError("Network is unreachable"))

    def test_dns_failure(self):
        self._assert_silent(socket.gaierror("Name or service not known"))

    def test_timeout(self):
        self._assert_silent(socket.timeout("timed out"))

    def test_rate_limited_403(self):
        # GitHub 匿名调用一小时 60 次，超了就 403。很容易撞上，不能崩。
        self._assert_silent(urllib.error.HTTPError(
            "url", 403, "rate limit exceeded", {}, io.BytesIO(b"")))

    def test_repo_has_no_release_404(self):
        self._assert_silent(urllib.error.HTTPError(
            "url", 404, "Not Found", {}, io.BytesIO(b"")))

    def test_server_error_500(self):
        self._assert_silent(urllib.error.HTTPError(
            "url", 500, "Server Error", {}, io.BytesIO(b"")))

    def test_ssl_error(self):
        self._assert_silent(enum_update.ssl.SSLError("cert verify failed"))

    def test_unexpected_exception_type_is_still_swallowed(self):
        # 兜底 except Exception 的存在意义：奇怪网络栈上会冒出意想不到的异常
        self._assert_silent(RuntimeError("something exotic"))


class MalformedApiResponseTests(unittest.TestCase):
    """API 通了但内容不对劲，同样不能崩。"""

    def _fetch(self, payload):
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_response(payload)):
            return enum_update.fetch_latest_release("Enumber/vokey")

    def test_not_json_at_all(self):
        self.assertIsNone(self._fetch(b"<html>502 Bad Gateway</html>"))

    def test_truncated_json(self):
        self.assertIsNone(self._fetch(b'{"tag_name": "v1.2'))

    def test_json_but_not_an_object(self):
        self.assertIsNone(self._fetch([1, 2, 3]))
        self.assertIsNone(self._fetch("just a string"))

    def test_missing_tag_name(self):
        self.assertIsNone(self._fetch({"html_url": "https://example.invalid"}))

    def test_empty_or_wrongly_typed_tag(self):
        self.assertIsNone(self._fetch({"tag_name": ""}))
        self.assertIsNone(self._fetch({"tag_name": "   "}))
        self.assertIsNone(self._fetch({"tag_name": None}))
        self.assertIsNone(self._fetch({"tag_name": 1.1}))

    def test_non_200_status(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_response({"tag_name": "v9"}, status=204)):
            self.assertIsNone(enum_update.fetch_latest_release("Enumber/vokey"))

    def test_assets_that_are_junk_are_skipped_not_fatal(self):
        release = self._fetch({
            "tag_name": "v1.2.0",
            "assets": ["not a dict", {}, {"name": "x"},
                       {"name": "ok.tar.gz",
                        "browser_download_url": "https://example.invalid/ok.tar.gz",
                        "size": 10}],
        })
        self.assertIsNotNone(release)
        self.assertEqual([a["name"] for a in release["assets"]], ["ok.tar.gz"])

    def test_well_formed_release_is_parsed(self):
        release = self._fetch({
            "tag_name": "v1.2.0",
            "html_url": "https://github.com/Enumber/vokey/releases/tag/v1.2.0",
            "tarball_url": "https://api.github.com/repos/Enumber/vokey/tarball/v1.2.0",
            "body": "notes here",
        })
        self.assertEqual(release["tag"], "v1.2.0")
        self.assertEqual(release["version"], "1.2.0")
        self.assertEqual(release["notes"], "notes here")
        self.assertTrue(release["tarball"])


class CheckForUpdateTests(unittest.TestCase):
    def _check(self, tag, current):
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_response({"tag_name": tag})):
            return enum_update.check_for_update("Enumber/vokey", current)

    def test_newer_release_is_reported(self):
        self.assertIsNotNone(self._check("v1.2.0", "1.1.0"))

    def test_same_version_is_not_an_update(self):
        self.assertIsNone(self._check("v1.1.0", "1.1.0"))

    def test_older_release_is_not_an_update(self):
        # 手滑发了个旧 tag 也不该提示"升级"到旧版本
        self.assertIsNone(self._check("v1.0.0", "1.1.0"))


class BackgroundCheckTests(unittest.TestCase):
    def test_callback_fires_only_when_there_is_an_update(self):
        seen = []
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_response({"tag_name": "v9.9.9"})):
            enum_update.check_in_background(
                "Enumber/vokey", "1.0.0", seen.append).join(timeout=5)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["version"], "9.9.9")

    def test_no_update_means_no_callback(self):
        seen = []
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_response({"tag_name": "v1.0.0"})):
            enum_update.check_in_background(
                "Enumber/vokey", "1.0.0", seen.append).join(timeout=5)
        self.assertEqual(seen, [])

    def test_network_failure_never_reaches_the_callback(self):
        seen = []
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")):
            enum_update.check_in_background(
                "Enumber/vokey", "1.0.0", seen.append).join(timeout=5)
        self.assertEqual(seen, [])

    def test_a_throwing_callback_does_not_escape_the_thread(self):
        def boom(_release):
            raise ValueError("callback blew up")
        with mock.patch("urllib.request.urlopen",
                        return_value=_fake_response({"tag_name": "v9.9.9"})):
            thread = enum_update.check_in_background(
                "Enumber/vokey", "1.0.0", boom)
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_thread_is_daemon_so_it_cannot_block_exit(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")):
            thread = enum_update.check_in_background(
                "Enumber/vokey", "1.0.0", lambda r: None)
        self.assertTrue(thread.daemon)
        thread.join(timeout=5)


class OverlayTests(unittest.TestCase):
    """覆盖式更新：新文件铺进去，用户的东西留下来。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="enum-update-test-")
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        for d in (self.src, self.dst):
            os.makedirs(d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, base, rel, text):
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _read(self, base, rel):
        with open(os.path.join(base, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_new_files_overwrite_old_ones(self):
        self._write(self.src, "app.py", "new")
        self._write(self.dst, "app.py", "old")
        enum_update._overlay(self.src, self.dst, enum_update.PRESERVE)
        self.assertEqual(self._read(self.dst, "app.py"), "new")

    def test_files_only_in_the_release_are_added(self):
        self._write(self.src, "sub/added.py", "brand new")
        enum_update._overlay(self.src, self.dst, enum_update.PRESERVE)
        self.assertEqual(self._read(self.dst, "sub/added.py"), "brand new")

    def test_user_files_not_in_the_release_survive(self):
        # 安装目录里用户自己放的东西不能被更新抹掉
        self._write(self.dst, "my-notes.txt", "keep me")
        self._write(self.src, "app.py", "new")
        enum_update._overlay(self.src, self.dst, enum_update.PRESERVE)
        self.assertEqual(self._read(self.dst, "my-notes.txt"), "keep me")

    def test_preserved_dirs_are_never_touched(self):
        # 几百兆的模型、装好的 venv：Release 包里就算有同名目录也不许覆盖
        self._write(self.dst, "models/big.onnx", "user model")
        self._write(self.dst, ".venv/pyvenv.cfg", "user venv")
        self._write(self.src, "models/big.onnx", "REPLACED")
        self._write(self.src, ".venv/pyvenv.cfg", "REPLACED")
        enum_update._overlay(self.src, self.dst, enum_update.PRESERVE)
        self.assertEqual(self._read(self.dst, "models/big.onnx"), "user model")
        self.assertEqual(self._read(self.dst, ".venv/pyvenv.cfg"), "user venv")

    def test_returns_number_of_files_copied(self):
        self._write(self.src, "a.py", "1")
        self._write(self.src, "b/c.py", "2")
        self.assertEqual(
            enum_update._overlay(self.src, self.dst, enum_update.PRESERVE), 2)


class BackupTests(unittest.TestCase):
    """更新前只备份"真的会被覆盖"的文件，不是整个安装目录。

    整份 copytree 会把几百兆语音模型和整个 venv 也复制一遍——又慢又能把磁盘
    塞满，而它们根本不会被更新动到。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="enum-update-bak-")
        self.src = os.path.join(self.tmp, "src")
        self.dst = os.path.join(self.tmp, "dst")
        self.bak = os.path.join(self.tmp, "bak")
        for d in (self.src, self.dst):
            os.makedirs(d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, base, rel, text):
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _backup(self):
        return enum_update._backup_files_to_be_replaced(
            self.src, self.dst, self.bak, enum_update.PRESERVE)

    def test_replaced_file_is_backed_up_with_its_old_content(self):
        self._write(self.src, "app.py", "new")
        self._write(self.dst, "app.py", "old")
        self.assertEqual(self._backup(), 1)
        with open(os.path.join(self.bak, "app.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old")

    def test_brand_new_files_are_not_backed_up(self):
        # 新增的文件没有旧版本可备份
        self._write(self.src, "added.py", "new")
        self.assertEqual(self._backup(), 0)

    def test_user_only_files_are_not_backed_up(self):
        # 这次不会被动的文件，备份它没有意义
        self._write(self.dst, "my-notes.txt", "keep")
        self._write(self.src, "app.py", "new")
        self.assertEqual(self._backup(), 0)

    def test_heavy_preserved_dirs_are_never_copied(self):
        # 这是这个函数存在的全部理由：几百兆的模型不该被复制一遍
        self._write(self.dst, "models/big.onnx", "300MB pretend")
        self._write(self.src, "models/big.onnx", "new")
        self._write(self.dst, ".venv/pyvenv.cfg", "venv")
        self._write(self.src, ".venv/pyvenv.cfg", "new")
        self.assertEqual(self._backup(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.bak, "models")))
        self.assertFalse(os.path.exists(os.path.join(self.bak, ".venv")))

    def test_nested_structure_is_preserved_so_it_can_be_restored(self):
        self._write(self.src, "sub/deep/mod.py", "new")
        self._write(self.dst, "sub/deep/mod.py", "old")
        self.assertEqual(self._backup(), 1)
        with open(os.path.join(self.bak, "sub/deep/mod.py"),
                  encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old")

    def test_nothing_to_replace_leaves_no_backup_dir(self):
        self._write(self.src, "added.py", "new")
        self._backup()
        self.assertFalse(os.path.exists(self.bak))


class TarballSafetyTests(unittest.TestCase):
    """从网上下的压缩包按不可信输入处理。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="enum-update-tar-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_path_traversal_members_are_dropped(self):
        import tarfile
        archive = os.path.join(self.tmp, "evil.tar.gz")
        payload = os.path.join(self.tmp, "payload")
        with open(payload, "w", encoding="utf-8") as handle:
            handle.write("pwned")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(payload, arcname="../../escaped.txt")
            tar.add(payload, arcname="good.txt")
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(dest)
        with tarfile.open(archive, "r:gz") as tar:
            enum_update._safe_extract(tar, dest)
        self.assertTrue(os.path.exists(os.path.join(dest, "good.txt")))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "escaped.txt")))

    def test_no_tarball_url_fails_cleanly(self):
        ok, message = enum_update.update_via_tarball(self.tmp, "")
        self.assertFalse(ok)
        self.assertIn("tarball", message.lower())

    def test_download_failure_aborts_without_touching_the_install_dir(self):
        self._marker = os.path.join(self.tmp, "keep.txt")
        with open(self._marker, "w", encoding="utf-8") as handle:
            handle.write("original")
        with mock.patch.object(enum_update, "_download", return_value=False):
            ok, _ = enum_update.update_via_tarball(
                self.tmp, "https://example.invalid/x.tar.gz")
        self.assertFalse(ok)
        with open(self._marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original")


class GitUpdateTests(unittest.TestCase):
    def test_local_changes_produce_a_clear_message_not_a_broken_tree(self):
        # --ff-only 失败时要说人话，而不是把 git 的原始报错甩给用户
        with mock.patch.object(enum_update, "_run",
                               return_value=(False, "error: Your local changes "
                                                    "would be overwritten")):
            ok, message = enum_update.update_via_git("/nonexistent")
        self.assertFalse(ok)
        self.assertIn("git pull", message)

    def test_missing_git_binary_is_reported_not_raised(self):
        ok, message = enum_update._run(["definitely-not-a-real-binary"], cwd=".")
        self.assertFalse(ok)
        self.assertIn("not found", message)

    def test_is_git_checkout_detects_dot_git(self):
        tmp = tempfile.mkdtemp(prefix="enum-update-git-")
        try:
            self.assertFalse(enum_update.is_git_checkout(tmp))
            os.makedirs(os.path.join(tmp, ".git"))
            self.assertTrue(enum_update.is_git_checkout(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
