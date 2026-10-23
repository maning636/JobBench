# -*- coding: utf-8 -*-
"""
一键启动专用浏览器
====================

**为什么有这个模块**：v2 之前，界面把这段甩给用户：

    chrome.exe --remote-debugging-port=9222 --user-data-dir="%TEMP%\\chrome-jobforge"

然后让人自己去开终端敲。对一个大学生来说，这一步就够了——
他不是不想用，是不会用。实测反馈："你不能直接让用户去登录，
你得设置一个按钮"。

所以这里负责把浏览器**拉起来**，并把状态翻译成人话：
没装 / 没开 / 开了没登录 / 登录好了。

一个关键约束：**不能占用用户日常用的 Chrome**。
用户资料目录必须独立，否则要么丢登录态、要么污染主浏览器配置。
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEBUG_PORT = 9222
CDP_HOST = "127.0.0.1"


# =============================================================================
# 找浏览器
# =============================================================================

def find_browser() -> Optional[str]:
    """找 Chrome 或 Edge。找不到返回 None（而不是编一个路径）。"""
    system = platform.system()

    def p(*parts: str) -> str:
        return os.path.join(*parts)

    win_cands: List[str] = []
    if system == "Windows":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            win_cands += [
                p(base, "Google", "Chrome", "Application", "chrome.exe"),
                p(base, "Microsoft", "Edge", "Application", "msedge.exe"),
            ]
    elif system == "Darwin":
        win_cands += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser",
                     "microsoft-edge", "brave-browser"):
            found = shutil.which(name)
            if found:
                return found

    for c in win_cands:
        if Path(c).exists():
            return c

    # Linux 也可能装在 snap/flatpak
    for name in ("google-chrome-stable", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def browser_name(path: str) -> str:
    base = Path(path).stem.lower()
    return {"chrome": "Chrome", "msedge": "Edge",
            "google chrome": "Chrome", "brave-browser": "Brave"}.get(base, "浏览器")


# =============================================================================
# 独立的数据目录
# =============================================================================

def user_data_dir() -> Path:
    """独立的用户资料目录 —— 不碰用户日常浏览器的配置与登录态。"""
    if platform.system() == "Windows":
        base = Path(os.environ.get("TEMP") or Path.home() / "AppData/Local/Temp")
    elif platform.system() == "Darwin":
        base = Path.home() / "Library/Caches"
    else:
        base = Path.home() / ".cache"
    d = base / "jobforge-browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


# =============================================================================
# 状态
# =============================================================================

@dataclass
class BrowserStatus:
    installed: bool = False
    running: bool = False
    logged_in: Optional[bool] = None
    browser_path: str = ""
    message: str = ""
    hint: str = ""
    # 这个平台未登录能不能采。智联能，BOSS 不能。
    login_required: bool = True
    steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "running": self.running,
            "logged_in": self.logged_in,
            "login_required": self.login_required,
            "browser_path": self.browser_path,
            "browser_name": browser_name(self.browser_path) if self.browser_path else "",
            "message": self.message,
            "hint": self.hint,
        }

    @property
    def ready(self) -> bool:
        """能不能开始采集。

        未登录是否算数，取决于平台 —— BOSS 未登录采不到东西，
        智联未登录也能采（实测），所以智联只要浏览器在跑就算就绪。
        """
        if not (self.installed and self.running):
            return False
        if not self.login_required:
            return True
        return self.logged_in is True


def cdp_alive(host: str = CDP_HOST, port: int = DEBUG_PORT) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


# 登录判据：BOSS 未登录时会出现这些文案
_ANON_MARKERS = ["登录/注册", "请登录", "扫码登录", "登录后查看"]
# 登录后会出现这些
_AUTH_MARKERS = ["我的", "退出", "已登录", "消息"]

# 智联的判据跟 BOSS 不一样（它的登录入口是「微信扫码快捷登录」）
_ZL_ANON_MARKERS = ["微信扫码快捷登录", "验证码登录/注册",
                    "国家网络身份认证登录", "其他方式登录"]
_ZL_AUTH_MARKERS = ["我的简历", "退出登录", "消息中心"]

# 各平台的登录页 + 判据
PLATFORMS = {
    "boss": {
        "domain": "zhipin.com",
        "login_url": "https://www.zhipin.com/web/geek/job",
        "anon": _ANON_MARKERS,
        "auth": _AUTH_MARKERS,
        # BOSS 未登录就采不到东西，必须登录
        "login_required": True,
        "name": "BOSS 直聘",
    },
    "zhaopin": {
        "domain": "zhaopin.com",
        "login_url": "https://www.zhaopin.com/jobs/",
        "anon": _ZL_ANON_MARKERS,
        "auth": _ZL_AUTH_MARKERS,
        # 智联实测未登录也能采到完整岗位 —— 登录只是锦上添花
        "login_required": False,
        "name": "智联招聘",
    },
}


def probe_login(host: str = CDP_HOST, port: int = DEBUG_PORT,
                domain: str = "zhipin.com",
                anon_markers: Optional[List[str]] = None,
                auth_markers: Optional[List[str]] = None) -> Optional[bool]:
    """判断是否已登录。

    `None` = 判断不了（浏览器没开 / 没有该站标签页）。
    实现上宁可返回 None 让人点"我登录好了"再验一次，
    也不猜 —— 猜错会让用户以为登录成功了。
    """
    if not cdp_alive(host, port):
        return None
    from jobforge.adapters.sources.browser import BrowserSession, BrowserError
    b = BrowserSession(host=host, port=port)
    anon = anon_markers if anon_markers is not None else _ANON_MARKERS
    auth = auth_markers if auth_markers is not None else _AUTH_MARKERS
    try:
        tab = b.find_tab(domain)
        if not tab:
            return None
        html = b.evaluate(tab["webSocketDebuggerUrl"],
                          "document.documentElement.outerHTML") or ""
        if any(m in html for m in anon):
            return False
        if any(m in html for m in auth):
            return True
        return None
    except (BrowserError, Exception):  # noqa: BLE001
        return None


def get_status(source: str = "boss") -> BrowserStatus:
    """浏览器 + 登录状态。

    `source` 决定看哪个平台、判据是什么，以及**未登录算不算就绪**：
      · BOSS —— 未登录采不到数据，必须登录才算 ready
      · 智联 —— 实测未登录也能采，登录只是多几个功能
    """
    st = BrowserStatus()
    p = PLATFORMS.get((source or "boss").strip().lower(), PLATFORMS["boss"])
    st.login_required = p["login_required"]
    path = find_browser()
    st.browser_path = path or ""
    st.installed = bool(path)

    if not st.installed:
        st.message = "没有找到 Chrome 或 Edge"
        st.hint = ("去官网下载安装：https://www.google.com/chrome/ "
                   "装好后回到这个页面点「启动浏览器」")
        return st

    st.running = cdp_alive()
    if not st.running:
        st.message = "浏览器还没启动"
        st.hint = "点下面的「启动浏览器」按钮，我们会帮你打开"
        return st

    st.logged_in = probe_login(domain=p["domain"],
                               anon_markers=p["anon"],
                               auth_markers=p["auth"])

    if st.logged_in is True:
        st.message = "已经登录好了"
    elif st.logged_in is False:
        if p["login_required"]:
            st.message = f"浏览器已启动，但还没登录{p['name']}"
            st.hint = f"在弹出的浏览器窗口里扫码登录，然后回来点「我登录好了」"
        else:
            st.message = f"浏览器已启动（{p['name']}未登录也能采集）"
            st.hint = f"想点「立即沟通」的话，扫码登录一下就行；不登录也能正常采集"
    else:
        if p["login_required"]:
            st.message = "浏览器已启动，暂时判断不出登录状态"
            st.hint = ("如果你已经登录过了，可以直接点「我登录好了」让程序确认一下；"
                       "没登录的话请在浏览器窗口里扫码登录")
        else:
            st.message = "浏览器已启动"
            st.hint = f"可以直接开始采集{p['name']}；登录后能多几个功能"
    return st


# =============================================================================
# 启动
# =============================================================================

# =============================================================================
# 打开平台登录页
# =============================================================================

def platform_config(source: str = "boss") -> Dict[str, Any]:
    """取某个平台的配置（登录页、判据、是否必须登录）。"""
    return PLATFORMS.get((source or "boss").strip().lower(), PLATFORMS["boss"])


def open_platform_page(source: str = "boss") -> Dict[str, Any]:
    """在专用浏览器里打开某个平台的登录页（新标签页）。

    **为什么必须有这个函数**：专用浏览器是**两个平台共用**的（一个 profile）。
    用户先用 BOSS，浏览器就一直在跑；之后把平台切到智联，
    智联的登录页**从来没被打开过** —— 没有这个函数，
    用户只能自己在浏览器里手敲网址。

    这也正是「切换平台后第一次采集」会走到的路径
    （智联标签页不存在 → 需要新建标签页）。

    浏览器没在跑的话，先把它拉起来。
    """
    p = platform_config(source)
    url = p["login_url"]

    if not cdp_alive():
        r = launch(source=source)
        if not r.get("ok"):
            return {"ok": False, "source": source, "name": p["name"],
                    "message": r.get("message") or "浏览器启动失败"}

    try:
        from jobforge.adapters.sources.browser import BrowserSession
        b = BrowserSession(host=CDP_HOST, port=DEBUG_PORT)
        tab = b.new_tab(url)
        return {"ok": True, "source": source, "name": p["name"],
                "opened": url, "tab_id": tab.get("id"),
                "message": f"已在专用浏览器里打开{p['name']}的页面"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "source": source, "name": p["name"],
                "message": f"打开{p['name']}页面失败：{e}"}


# =============================================================================
# 启动
# =============================================================================

def launch(host: str = CDP_HOST, port: int = DEBUG_PORT,
           open_login_page: bool = True, source: str = "boss") -> Dict[str, Any]:
    """拉起带调试端口的浏览器。

    先检查是不是已经在跑 —— 重复启动会开出第二个窗口，
    用户会以为程序疯了（实测这是最容易被当成 bug 的行为）。

    ⚠ 已经在跑的时候**不能只是说「已经在运行了」**就完事 ——
    浏览器是 BOSS 和智联共用的，用户很可能是在 BOSS 用完之后
    才切到智联，这时智联的登录页从没被打开过。
    所以这里照样把所选平台的登录页开出来（新标签页）。
    """
    if cdp_alive(host, port):
        opened = False
        if open_login_page:
            try:
                from jobforge.adapters.sources.browser import BrowserSession
                BrowserSession(host=host, port=port).new_tab(
                    platform_config(source)["login_url"])
                opened = True
            except Exception:  # noqa: BLE001
                opened = False
        st = get_status(source=source)
        return {"ok": True, "already_running": True,
                "opened_login_page": opened,
                **st.to_dict(),
                "message": ("浏览器已经在运行了，已帮你打开登录页"
                            if opened else "浏览器已经在运行了，不用重复启动")}

    path = find_browser()
    if not path:
        return {"ok": False, **get_status(source=source).to_dict()}

    ud = user_data_dir()
    args = [
        path,
        f"--remote-debugging-port={port}",
        f"--remote-allow-origins=*",
        f"--user-data-dir={ud}",
        # 首次启动不要一堆欢迎页干扰
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if open_login_page:
        args.append(platform_config(source)["login_url"])

    try:
        if platform.system() == "Windows":
            # DETACHED_PROCESS：窗口独立存在，不随父进程退出
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                      | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            subprocess.Popen(args, creationflags=flags,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(args, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, **get_status(source=source).to_dict(),
                "message": f"启动失败：{e}"}

    # 等它把调试端口开起来，最多 12 秒
    import time
    for _ in range(24):
        time.sleep(0.5)
        if cdp_alive(host, port):
            st = get_status(source=source)
            return {"ok": True, "already_running": False, **st.to_dict(),
                    "message": "浏览器已启动，接下来在里面扫码登录"}

    return {"ok": False, **get_status(source=source).to_dict(),
            "message": ("浏览器启动了，但没连上。"
                        "可能已经有别的程序占用这个端口，"
                        "或者系统拦住了，请稍后再试一次")}
