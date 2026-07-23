#!/usr/bin/env python3
# Copyright (C) 2026 ENum
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY; see the GNU General
# Public License (LICENSE) for details.

"""自动升级：查 GitHub 最新 Release，和本地版本比，必要时就地更新。

设计上只有一条铁律：**查更新永远不能影响程序本身**。没网、DNS 挂了、GitHub
限流、返回的 JSON 是一坨乱码——统统安静地返回 None，调用方当作"没有更新"继续
跑。所以这里几乎每个外部调用都裹在 try 里，且只用标准库（不依赖 requests，更
不依赖 gh CLI——用户机器上不一定装了）。

更新方式按安装形态自动选：
- git 克隆装的（目录里有 .git）→ `git pull --ff-only`，最省事也最不会出错；
- 下载压缩包装的 → 拉 Release 的源码 tarball，解包后**覆盖式**铺进安装目录。

覆盖而不是"删掉重来"是刻意的：安装目录里往往还有用户自己的东西（Vokey 的语音
模型几百兆、FlipScan 的默认存图目录、venv），整个目录删掉重建会把它们一起抹掉。
覆盖只动仓库里本来就有的文件，其余原样保留。

用户配置在 ~/.config/<app>/，本来就在安装目录之外，两种方式都碰不到。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

GITHUB_LATEST_RELEASE = "https://api.github.com/repos/{repo}/releases/latest"

# 查更新的超时：宁可放弃也不要让启动卡住。GitHub 正常几百毫秒就回了，
# 6 秒还没动静基本就是网络不通，没必要再等。
DEFAULT_TIMEOUT = 6.0

# 覆盖更新时永远不动的路径（相对安装目录）。都是"仓库里没有、但用户机器上有"
# 的东西：虚拟环境、下载下来的模型、git 元数据、用户配置。
PRESERVE = (
    ".git", ".venv", "venv", "gemini_env", "__pycache__",
    "models", "model", ".config", "config.json",
)


# ── 版本号比较 ──────────────────────────────────────────────────────────────

_NUM = re.compile(r"\d+")


def parse_version(text):
    """把版本号字符串拆成可比较的 (数字段, 预发布段)。

    容忍现实里各种写法：``v1.2.3`` / ``1.2.3`` / ``V1.2`` / ``1.2.3-beta.1`` /
    ``1.2.3+build5``。拆不出数字的（None、空串、"latest"）返回全零，永远比任何
    真实版本号小——这样"解析失败"的后果是"认为没有新版本"，而不是误报升级。
    """
    if not isinstance(text, str):
        return ((0,), ())
    text = text.strip()
    if not text:
        return ((0,), ())
    if text[:1] in ("v", "V"):
        text = text[1:]
    # +build 元数据按 semver 不参与比较，直接丢掉
    text = text.split("+", 1)[0]
    # -beta / -rc.1 这类预发布后缀单独拆出来
    core, _, pre = text.partition("-")
    nums = tuple(int(n) for n in _NUM.findall(core)) or (0,)
    pre_parts = tuple(p for p in re.split(r"[.\-]", pre) if p) if pre else ()
    return (nums, pre_parts)


def _cmp_pre(a, b):
    """比较预发布段。没有预发布段的是正式版，**大于**任何预发布版。"""
    if not a and not b:
        return 0
    if not a:
        return 1          # 1.2.0 > 1.2.0-beta
    if not b:
        return -1
    for x, y in zip(a, b):
        # 纯数字段按数值比（rc.2 > rc.10 是错的），混合段按字符串比
        if x.isdigit() and y.isdigit():
            x, y = int(x), int(y)
        else:
            x, y = str(x), str(y)
        if x == y:
            continue
        try:
            return -1 if x < y else 1
        except TypeError:      # int 和 str 比不了：数字段视为更小
            return -1 if isinstance(x, int) else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def compare_versions(a, b):
    """a<b 返回 -1，相等 0，a>b 返回 1。

    数字段逐位按**数值**比较，所以 1.10 > 1.9（字符串比较会得出相反的错误结论，
    这也是版本比较最经典的坑）。位数不齐的短的一方补 0：1.2 == 1.2.0。
    """
    (na, pa), (nb, pb) = parse_version(a), parse_version(b)
    width = max(len(na), len(nb))
    na = na + (0,) * (width - len(na))
    nb = nb + (0,) * (width - len(nb))
    if na != nb:
        return -1 if na < nb else 1
    return _cmp_pre(pa, pb)


def is_newer(candidate, current):
    """candidate 是否比 current 新。"""
    return compare_versions(candidate, current) > 0


# ── 查 GitHub Release ───────────────────────────────────────────────────────

def fetch_latest_release(repo, timeout=DEFAULT_TIMEOUT):
    """取 <repo> 的最新 Release。任何失败都返回 None，绝不抛异常。

    "任何失败"包括但不限于：没网、DNS 解析不了、连接超时、GitHub 限流（403）、
    仓库还没发过 Release（404）、返回的不是 JSON、证书验证失败。这些在用户机器
    上都真实会发生，而它们没有一个值得打断用户正在做的事。
    """
    url = GITHUB_LATEST_RELEASE.format(repo=repo)
    request = urllib.request.Request(url, headers={
        # GitHub 要求带 User-Agent，不带会直接 403
        "User-Agent": "ENum-Updater",
        "Accept": "application/vnd.github+json",
    })
    try:
        # 显式建 SSL context：个别精简系统上默认 context 拿不到 CA 包
        context = ssl.create_default_context()
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=context) as response:
            if getattr(response, "status", 200) != 200:
                return None
            # 限制读取大小：Release JSON 正常几十 KB，真读到几百兆说明出事了
            raw = response.read(1 << 20)
        data = json.loads(raw.decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, ssl.SSLError, TimeoutError):
        return None
    except Exception:
        # 兜底：标准库在奇怪的网络栈上偶尔会抛意料之外的异常类型，
        # 查更新失败不值得让程序崩掉，所以这里也咽掉。
        return None
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name") or data.get("name")
    if not isinstance(tag, str) or not tag.strip():
        return None
    assets = []
    for asset in (data.get("assets") or []):
        if isinstance(asset, dict) and asset.get("browser_download_url"):
            assets.append({
                "name": str(asset.get("name") or ""),
                "url": str(asset["browser_download_url"]),
                "size": asset.get("size") or 0,
            })
    return {
        "tag": tag.strip(),
        "version": tag.strip().lstrip("vV"),
        "url": data.get("html_url") or "",
        "tarball": data.get("tarball_url") or "",
        "notes": data.get("body") or "",
        "assets": assets,
    }


def check_for_update(repo, current_version, timeout=DEFAULT_TIMEOUT):
    """有新版就返回 release dict，没有新版或查不到就返回 None。"""
    release = fetch_latest_release(repo, timeout=timeout)
    if not release:
        return None
    return release if is_newer(release["version"], current_version) else None


def check_in_background(repo, current_version, callback,
                        timeout=DEFAULT_TIMEOUT, delay=0.0):
    """后台线程查更新，有结果才回调。返回那个线程（daemon，不挡退出）。

    daemon=True 很关键：用户在查更新还没返回时就关掉程序，不该被一个还在等
    网络超时的线程拖着不退出。
    """
    def worker():
        try:
            if delay:
                time.sleep(delay)
            release = check_for_update(repo, current_version, timeout=timeout)
            if release:
                callback(release)
        except Exception:
            pass          # 后台线程里抛异常没人接得住，一律咽掉

    thread = threading.Thread(target=worker, name="enum-update-check",
                              daemon=True)
    thread.start()
    return thread


# ── 就地更新 ────────────────────────────────────────────────────────────────

def install_root(module_file):
    """程序安装目录 = 调用方源文件所在目录。"""
    return os.path.dirname(os.path.abspath(module_file))


def is_git_checkout(path):
    """这份程序是 git clone 来的吗？"""
    return os.path.isdir(os.path.join(path, ".git"))


def _run(args, cwd, timeout=180):
    """跑一条命令，返回 (成功, 输出)。不抛异常。"""
    try:
        proc = subprocess.run(args, cwd=cwd, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return proc.returncode == 0, proc.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return False, "command not found: %s" % args[0]
    except subprocess.TimeoutExpired:
        return False, "timed out: %s" % " ".join(args)
    except Exception as exc:
        return False, str(exc)


def update_via_git(path):
    """git 安装的：拉一下就行。

    用 --ff-only 而不是普通 pull：用户自己改过代码时宁可失败并说清楚，也不要
    留下一个冲突到一半的工作区——那比不更新糟糕得多。
    """
    ok, out = _run(["git", "-C", path, "pull", "--ff-only"], cwd=path)
    if ok:
        return True, out.strip() or "Already up to date."
    if "local changes" in out or "would be overwritten" in out or "diverge" in out:
        return False, (
            "本地有改动，自动更新已跳过（避免冲突）。请手动 git pull。\n"
            "Local changes present; automatic update skipped to avoid conflicts. "
            "Please run `git pull` manually.\n\n" + out.strip())
    return False, out.strip()


def _download(url, dest, timeout=DEFAULT_TIMEOUT, max_bytes=200 << 20):
    """下载到本地文件。失败返回 False，不抛。"""
    request = urllib.request.Request(url, headers={"User-Agent": "ENum-Updater"})
    try:
        context = ssl.create_default_context()
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=context) as response, \
                open(dest, "wb") as handle:
            total = 0
            while True:
                chunk = response.read(64 << 10)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:      # 防止磁盘被写爆
                    return False
                handle.write(chunk)
        return True
    except Exception:
        return False


def _safe_extract(tar, dest):
    """解包，挡掉 tarball 里指向目录外的路径（../../etc/passwd 这类）。

    压缩包是从网上下的，即使来源是自己的 Release 也当不可信输入处理。
    """
    dest = os.path.abspath(dest)
    members = []
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(dest, member.name))
        if not (target == dest or target.startswith(dest + os.sep)):
            continue                      # 越界，丢掉
        if member.issym() or member.islnk():
            continue                      # 链接可能指到目录外，一律不要
        members.append(member)
    try:
        # Python 3.12+ 的内置过滤器再兜一层（3.14 起是默认行为）。
        # 老版本没有这个参数，落到下面的普通 extractall——上面的手工筛查
        # 本来就已经挡住了越界路径和链接，这里只是双保险。
        tar.extractall(dest, members=members, filter="data")
    except TypeError:
        tar.extractall(dest, members=members)


def update_via_tarball(path, tarball_url, timeout=60.0, preserve=PRESERVE):
    """非 git 安装的：下 Release 源码包，覆盖式铺进安装目录。

    **只备份这次真的会被覆盖的那些文件**（不是整个安装目录）到
    <安装目录>.backup-<时间戳>，更新出问题时用户还有退路。

    刻意不整份 copytree：安装目录里可能躺着几百兆语音模型和整个 venv，每次更新
    都复制一遍既慢又能把磁盘塞满，而它们根本不会被这次更新动到——备份它们没有
    任何意义。preserve 里的路径（venv、模型、配置）一概不动。
    """
    if not tarball_url:
        return False, "No source tarball in the latest release."
    workdir = tempfile.mkdtemp(prefix="enum-update-")
    try:
        archive = os.path.join(workdir, "src.tar.gz")
        if not _download(tarball_url, archive, timeout=timeout):
            return False, "下载失败。/ Download failed."
        extracted = os.path.join(workdir, "x")
        os.makedirs(extracted, exist_ok=True)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                _safe_extract(tar, extracted)
        except (tarfile.TarError, OSError) as exc:
            return False, "压缩包损坏。/ Corrupt archive. (%s)" % exc

        # GitHub 的 tarball 外面永远套一层 <owner>-<repo>-<sha>/ 目录
        entries = [os.path.join(extracted, e) for e in os.listdir(extracted)]
        roots = [e for e in entries if os.path.isdir(e)]
        source = roots[0] if len(roots) == 1 else extracted

        backup = "%s.backup-%s" % (path.rstrip("/"),
                                   time.strftime("%Y%m%d-%H%M%S"))
        try:
            backed_up = _backup_files_to_be_replaced(source, path, backup,
                                                     preserve)
        except Exception as exc:
            return False, "备份失败，已中止更新。/ Backup failed, aborted. (%s)" % exc

        copied = _overlay(source, path, preserve)
        if backed_up:
            note = ("已更新 %d 个文件，被替换的 %d 个旧文件备份在 %s\n"
                    "Updated %d files; the %d replaced files were backed up to %s"
                    % (copied, backed_up, backup, copied, backed_up, backup))
        else:
            # 全是新增文件、一个旧文件都没被覆盖，那就没有备份目录可言
            note = ("已更新 %d 个文件（都是新增，没有文件被替换）。\n"
                    "Updated %d files (all new; nothing was replaced)."
                    % (copied, copied))
        return True, note
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _backup_files_to_be_replaced(source, dest, backup, preserve):
    """把「这次会被覆盖掉的旧文件」复制到 backup 下，返回备份了几个。

    只看 source 里有、dest 里也已经存在的那些路径——新增的文件没有旧版本可备份，
    dest 里独有的文件这次不会被动，两者都不用进备份。目录结构保持一致，出问题时
    直接把 backup 里的东西盖回去就能还原。
    """
    count = 0
    for root, dirs, files in os.walk(source):
        rel = os.path.relpath(root, source)
        rel = "" if rel == "." else rel
        dirs[:] = [d for d in dirs if d not in preserve]
        for name in files:
            if name in preserve:
                continue
            existing = os.path.join(dest, rel, name) if rel else os.path.join(dest, name)
            if not os.path.isfile(existing):
                continue                       # 新增文件，没有旧版本
            target_dir = os.path.join(backup, rel) if rel else backup
            os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(existing, os.path.join(target_dir, name))
            count += 1
    return count


def _overlay(source, dest, preserve):
    """把 source 里的文件铺到 dest 上，返回复制了几个文件。

    只覆盖、不删除：dest 里有而 source 里没有的文件原样留着。
    """
    count = 0
    for root, dirs, files in os.walk(source):
        rel = os.path.relpath(root, source)
        rel = "" if rel == "." else rel
        # 就地改 dirs 才能真正阻止 os.walk 下探进去
        dirs[:] = [d for d in dirs if d not in preserve]
        target_dir = os.path.join(dest, rel) if rel else dest
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError:
            continue
        for name in files:
            if name in preserve:
                continue
            try:
                shutil.copy2(os.path.join(root, name),
                             os.path.join(target_dir, name))
                count += 1
            except OSError:
                pass          # 单个文件复制不了（权限等）不该中断整次更新
    return count


def apply_update(path, release, timeout=60.0):
    """按安装形态自动选更新方式。返回 (成功, 给用户看的说明)。"""
    if is_git_checkout(path):
        return update_via_git(path)
    return update_via_tarball(path, release.get("tarball", ""), timeout=timeout)


def writable(path):
    """安装目录可写吗？装在 /opt 时不可写，得提示用户用管理员权限跑。"""
    return os.access(path, os.W_OK)

# ── 自动更新（查到就装，不问） ──────────────────────────────────────────────

def auto_update_in_background(repo, current_version, module_file,
                              on_done=None, delay=0.0, timeout=DEFAULT_TIMEOUT):
    """后台查更新，查到就**直接装好**，装完通过 on_done 回调告知（可选）。

    设计取舍写在这里，免得以后有人以为是随手写的：
    - **不弹通知、不弹确认框**。用户明确要求这些自用工具不要打扰他，更新这种事
      装好了下次启动生效就行，没必要为它中断正在做的事。
    - **只在安装目录可写时装**。装在 /opt 这类地方需要管理员权限，这里绝不偷偷
      提权——写不了就当没这回事，留给用户自己用 sudo 重跑安装器。
    - **失败一律安静跳过**。网络、磁盘、权限、tar 里有恶意路径……任何一步出问题
      都只是"这次没更新成"，不该让程序崩掉或吓到用户。被替换的文件在
      update_via_tarball 里已经先备份过了。
    - **不动用户数据**：配置、模型、虚拟环境都在 PRESERVE 里，不会被覆盖。
    - 装完**不重启程序**。正在用的时候把自己换掉是最糟的体验，下次启动自然生效。
    """
    def worker():
        try:
            if delay:
                time.sleep(delay)
            release = check_for_update(repo, current_version, timeout=timeout)
            if not release:
                return
            root = install_root(module_file)
            if not writable(root):
                return
            ok, detail = apply_update(root, release, timeout=max(timeout, 60.0))
            if on_done:
                try:
                    on_done(ok, release, detail)
                except Exception:
                    pass
        except Exception:
            pass

    thread = threading.Thread(target=worker, name="enum-auto-update", daemon=True)
    thread.start()
    return thread


def notify(summary, body="", icon="software-update-available"):
    """**故意不发通知**，直接返回 False。

    这些工具是自用/内部工具，用户明确要求整台机器上只保留聊天软件的消息通知，
    其他程序一律不许打扰——"有新版本"这种事完全可以等用户打开程序时再说。

    保留这个函数而不是删掉：调用点还在（各程序的 announce 回调曾经用它），
    留一个明确不做事的实现，比让 import 失败或让人以为"通知发了但没弹出来"
    要清楚。真要恢复通知，把下面这行换回 notify-send 即可。
    """
    del summary, body, icon
    return False
