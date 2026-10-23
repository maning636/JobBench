# -*- coding: utf-8 -*-
"""桌面客户端外壳 —— 双击就是一个独立应用窗口，不是「浏览器标签页」。

用系统自带浏览器的 --app 模式开窗口
------------------------------------
--app 打开的窗口没有地址栏、没有标签栏、没有书签栏，
从用户角度看就是一个独立的程序窗口：

  · 标题栏  = 网页标题（求职工作台）
  · 任务栏图标 = 网页的 favicon（我们自己的图标）
  · Alt-Tab 里是独立的一个条目
  · 双击 EXE 就出来，��用自己开浏览器、复制网址

为什么不用 pywebview / WebView2
------------------------------
实测（2026-10-23，本机 125% 缩放）：pywebview 的 WinForms 后端算窗口尺寸
用的是 `GetDpiForWindow(hwnd)/96`，而窗口没上屏之前这个值是**系统 DPI**，
不是这台显示器的 125% —— 同一个代码跑两次，一次拿到 1.25（对）、
一次拿到 1.0（错，窗口比屏幕还宽，右半边跑到屏幕外）。
再加上 WinForms 会把 WebView2 控件按 DPI 放大 1.25 倍而窗体不跟着放大，
控件就比窗体宽，网页右边那一截落在窗口外面，看不见也点不到。

浏览器自己的 --app 窗口没这个问题 —— DPI 是浏览器自己算的，稳定。
顺带的好处：不用装任何额外运行时，EXE 体积能压到很小。

界面和采集是分开的
------------------
界面窗口只显示我们自己的界面；智联/BOSS 的页面始终是在**真实 Chrome**
里跑（走 CDP 9222）。所以界面用什么承载，完全不影响采集成功率。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional, Tuple

from jobforge import _PROJECT_ROOT
from jobforge.ports import DEFAULT_PORT as _PORTS_DEFAULT_PORT
from jobforge.ports import pick_port, port_free

APP_TITLE = "求职工作台"
DEFAULT_PORT = _PORTS_DEFAULT_PORT
# 窗口尺寸（逻辑像素）。1250 这个宽度是量出来的：
# 界面 .wrap 的 max-width 是 1120px，900px 以下会切成竖排布局，
# 1250 宽能保证横向布局不挤，同时在 1366×768 的老笔记本上也放得下。
WINDOW_SIZE = (1250, 780)
# 服务器起来要多久？本地 Flask 正常是 1 秒内，给 20 秒余量。
BOOT_TIMEOUT = 20.0


# --------------------------------------------------------------------------
# 端口
# --------------------------------------------------------------------------
#
# port_free / pick_port 在 jobforge.ports 里 —— 判断「空闲」必须真的 bind
# 一次，不能用 connect_ex（Windows 保留端口会被误判成空闲）。
# 这里保留这两个名字，是为了让模块内外的调用点都还能直接用。


# --------------------------------------------------------------------------
# 后端服务
# --------------------------------------------------------------------------

def serve_in_thread(app, port: int, tries: int = 256) -> Tuple[object, threading.Thread]:
    """在后台线程里跑 Flask，返回 (server, thread)。

    用 werkzeug 的 make_server 而不是 app.run()：
    app.run() 没有 shutdown()，窗口一关后台就变成孤儿线程，
    端口还占着，下次启动就得换端口。

    端口绑不上就自动往后找一个 —— 绑不上是常事，不是错误。
    （Windows 会保留上百个端口给 Hyper-V / WSL，那种端口
      connect 是连不上的，看起来像空闲，bind 却会报 errno 13；
      而 werkzeug 自己 bind 失败时会 sys.exit(1) 直接把进程带走，
      所以这里走 jobforge.server，自己 bind 好再把 fd 交给它。）

    实际用上的端口从 server.server_port 读，不要拿传进来的 port 当真。
    """
    from jobforge.server import make_server_on

    server, _port = make_server_on("127.0.0.1", port, app, tries=tries)
    thread = threading.Thread(target=server.serve_forever,
                              name="jobforge-http", daemon=True)
    thread.start()
    return server, thread


def wait_for_server(url: str, timeout: float = BOOT_TIMEOUT) -> bool:
    """等服务真的能响应了再开窗口。

    不等的话，窗口里会先闪一个「无法连接」的页面，
    用户以为程序坏了，其实只是起得慢。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "/api/summary", timeout=2).read()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


# --------------------------------------------------------------------------
# 应用窗口
# --------------------------------------------------------------------------

def ui_profile_dir() -> Path:
    """界面窗口专用的浏览器资料目录。

    ⚠️ 必须和采集用的浏览器**分开**：
    采集浏览器用的是 %TEMP%\\jobforge-browser（存着 BOSS 的登录态），
    两个 Chrome 进程不能共用同一个 user-data-dir，
    共用会导致第二个进程直接退出（Chrome 的自我保护）。
    """
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    d = base / "JobBench" / "ui-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_ui_browser() -> Optional[str]:
    """找一个能开 --app 窗口的浏览器。优先 Chrome，其次 Edge。"""
    from jobforge.application.browser_launcher import find_browser
    return find_browser()


def launch_app_window(url: str, width: int = WINDOW_SIZE[0],
                      height: int = WINDOW_SIZE[1]) -> Optional[subprocess.Popen]:
    """开一个应用窗口。返回进程句柄（失败返回 None）。"""
    path = find_ui_browser()
    if not path:
        return None
    args = [
        path,
        "--app=%s" % url,
        "--window-size=%d,%d" % (width, height),
        "--user-data-dir=%s" % ui_profile_dir(),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,TranslateUI",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--restore-last-session=false",
    ]
    try:
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            return subprocess.Popen(args, creationflags=flags,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return None


# --------------------------------------------------------------------------

def run_desktop(db_path: Optional[str] = None,
                port: int = DEFAULT_PORT) -> int:
    """跑桌面版。返回进程退出码。"""
    from jobforge.web.app import create_app

    port = pick_port(port)
    db = db_path or str(_PROJECT_ROOT / "data" / "jobforge.db")
    app = create_app(db)

    server, _thread = serve_in_thread(app, port)
    # 用服务实际绑上的端口，不要用传进来的那个 ——
    # 中间可能因为端口被占而自动往后挪过。
    from jobforge.server import server_port
    actual = server_port(server, port)
    url = "http://127.0.0.1:%d" % actual

    try:
        if not wait_for_server(url):
            print("本地服务没能在 %.0f 秒内启动，已退出。" % BOOT_TIMEOUT,
                  file=sys.stderr)
            return 1

        proc = launch_app_window(url)
        if proc is None:
            # 没有可用的浏览器 —— 退回普通网页，别让人卡住
            print("没找到可用的浏览器，改用默认浏览器打开：%s" % url)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                return 0

        print("求职工作台已启动。关掉那个窗口就等于退出这个程序。")
        # 用户关掉应用窗口 → 浏览器进程退出 → 我们收摊
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 —— 关闭失败不该影响退出
            pass
    return 0


# --------------------------------------------------------------------------

def missing_modules() -> list[str]:
    """运行时还缺哪些依赖。

    和 launch.py 的 REQUIRED_MODULES 保持一致 —— 少探一个，
    等用户点「立即采集」才崩，那时他已经不信任这个程序了。
    """
    import importlib.util
    mods = ("flask", "bs4", "websockets")
    return [m for m in mods if importlib.util.find_spec(m) is None]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="求职工作台 桌面版")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=str(_PROJECT_ROOT / "data" / "jobforge.db"))
    args = ap.parse_args(argv)

    # 直接 `python -m jobforge.desktop` 跑的时候，依赖可能还没装。
    # 交给 launch 走一遍自举 —— 它会建虚拟环境、pip install、
    # 并用人话告诉用户到底缺什么（而不是甩一段 ImportError 堆栈）。
    if missing_modules():
        from jobforge.launch import main as launch_main
        return launch_main(["--desktop", "--port", str(args.port)])

    try:
        return run_desktop(db_path=args.db, port=args.port)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
