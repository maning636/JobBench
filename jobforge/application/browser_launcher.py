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
    steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "installed": self.installed,
            "running": self.running,
            "logged_in": self.logged_in,
            "browser_path": self.browser_path,
            "browser_name": browser_name(self.browser_path) if self.browser_path else "",
            "message": self.message,
            "hint": self.hint,
        }

    @property
    def ready(self) -> bool:
        """能不能开始采集。"""
        return bool(self.installed and self.running and self.logged_in)


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


def probe_login(host: str = CDP_HOST, port: int = DEBUG_PORT,
                domain: str = "zhipin.com") -> Optional[bool]:
    """判断是否已登录。

    `None` = 判断不了（浏览器没开 / 没有该站标签页）。
    实现上宁可返回 None 让人点"我登录好了"再验一次，
    也不猜 —— 猜错会让用户以为登录成功了。
    """
    if not cdp_alive(host, port):
        return None
    from jobforge.adapters.sources.browser import BrowserSession, BrowserError
    b = BrowserSession(host=host, port=port)
    try:
        tab = b.find_tab(domain)
        if not tab:
            return None
        html = b.evaluate(tab["webSocketDebuggerUrl"],
                          "document.documentElement.outerHTML") or ""
        if any(m in html for m in _ANON_MARKERS):
            return False
        if any(m in html for m in _AUTH_MARKERS):
            return True
        return None
    except (BrowserError, Exception):  # noqa: BLE001
        return None


def get_status() -> BrowserStatus:
    st = BrowserStatus()
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

    st.logged_in = probe_login()
    if st.logged_in is True:
        st.message = "已经登录好了"
    elif st.logged_in is False:
        st.message = "浏览器已启动，但还没登录招聘网站"
        st.hint = "在弹出的浏览器窗口里扫码登录，然后回来点「我登录好了」"
    else:
        st.message = "浏览器已启动，暂时判断不出登录状态"
        st.hint = ("如果你已经登录过了，可以直接点「我登录好了」让程序确认一下；"
                   "没登录的话请在浏览器窗口里扫码登录")
    return st


# =============================================================================
# 启动
# =============================================================================

def launch(host: str = CDP_HOST, port: int = DEBUG_PORT,
           open_login_page: bool = True) -> Dict[str, Any]:
    """拉起带调试端口的浏览器。

    先检查是不是已经在跑 —— 重复启动会开出第二个窗口，
    用户会以为程序疯了（实测这是最容易被当成 bug 的行为）。
    """
    if cdp_alive(host, port):
        st = get_status()
        return {"ok": True, "already_running": True, **st.to_dict(),
                "message": "浏览器已经在运行了，不用重复启动"}

    path = find_browser()
    if not path:
        return {"ok": False, **get_status().to_dict()}

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
        args.append("https://www.zhipin.com/web/geek/job")

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
        return {"ok": False, **get_status().to_dict(),
                "message": f"启动失败：{e}"}

    # 等它把调试端口开起来，最多 12 秒
    import time
    for _ in range(24):
        time.sleep(0.5)
        if cdp_alive(host, port):
            st = get_status()
            return {"ok": True, "already_running": False, **st.to_dict(),
                    "message": "浏览器已启动，接下来在里面扫码登录"}

    return {"ok": False, **get_status().to_dict(),
            "message": ("浏览器启动了，但没连上。"
                        "可能已经有别的程序占用这个端口，"
                        "或者系统拦住了，请稍后再试一次")}
