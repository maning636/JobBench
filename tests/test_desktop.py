# -*- coding: utf-8 -*-
"""
桌面客户端外壳相关测试
======================

钉死四件「不报错但会静默坏事」的事：

1. **界面窗口的资料目录必须和采集浏览器分开** ——
   两个 Chrome 进程共用同一个 user-data-dir，第二个会**直接退出**（Chrome 的
   自我保护），表现是「双击了没反应」。
2. **应用窗口必须用 `--app` 模式** —— 普通 `--new-window` 会带地址栏和标签栏，
   用户看到的还是「一个浏览器」，不是客户端。
3. **窗口图标来自 favicon** —— `--app` 窗口拿网页的 favicon 当图标，
   favicon 没了，窗口就变成一个没有任何标识的空白框。
4. **桌面版不能依赖 pywebview** —— 实测在 125% 缩放的屏幕上，
   pywebview 的 WinForms 后端算窗口尺寸是「看运气」的（`GetDpiForWindow`
   在窗口没上屏前返回系统 DPI），WebView2 控件还会比窗体宽，
   右半边落在窗口外面。浏览器自己的 `--app` 窗口没这个问题。
5. **新机器第一次运行的转交路径不能断** —— 缺依赖时桌面版要把活
   转交给启动器去自举（建 .venv、装依赖）。这条路径只在全新机器上
   才会走到，本地开发期永远走不到，所以必须用测试钉死。
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobforge import desktop as D                        # noqa: E402
from jobforge import launch as L                         # noqa: E402
from jobforge.application import browser_launcher as BL   # noqa: E402

P, F = [], []


def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))


def _connect_refused(port: int) -> bool:
    """连不上这个端口吗？（用来证明「connect 判断」会误判）"""
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.settimeout(2)
    try:
        c.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        c.close()


# =============================================================================
print("\n--- 端口 ---")
# =============================================================================

free = D.pick_port(45000)
check("pick_port 返回一个可用端口", D.port_free(free), str(free))

# 占住一个端口，pick_port 必须跳过它
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(("127.0.0.1", 0))
sock.listen(1)
busy = sock.getsockname()[1]
try:
    got = D.pick_port(busy, tries=5)
    check("端口被占时会换一个", got != busy and D.port_free(got),
          f"asked={busy} got={got}")
finally:
    sock.close()

# =============================================================================
print("\n--- 资料目录不能和采集浏览器共用 ---")
# =============================================================================

ui_dir = D.ui_profile_dir()
check("界面资料目录建得出来", ui_dir.is_dir(), str(ui_dir))
check("界面资料目录在 LOCALAPPDATA 下",
      "JobBench" in str(ui_dir) and str(ui_dir).startswith(
          os.environ.get("LOCALAPPDATA", "") or "JobBench"),
      str(ui_dir))
check("界面资料目录和采集浏览器的不一样",
      ui_dir.resolve() != BL.user_data_dir().resolve(),
      f"ui={ui_dir} collect={BL.user_data_dir()}")
check("界面资料目录不在 TEMP 下（临时目录会被清掉）",
      "Temp" not in str(ui_dir) or "jobforge-browser" not in str(ui_dir),
      str(ui_dir))

# =============================================================================
print("\n--- 应用窗口的启动参数 ---")
# =============================================================================


class FakeProc:
    pid = 12345

    def wait(self):
        return 0


captured = {}


def fake_popen(args, **kw):
    captured["args"] = args
    captured["kw"] = kw
    return FakeProc()


old_popen = D.subprocess.Popen
old_find = D.find_ui_browser
D.subprocess.Popen = fake_popen
D.find_ui_browser = lambda: "fake-chrome.exe"
try:
    proc = D.launch_app_window("http://127.0.0.1:3399", 1250, 780)
finally:
    D.subprocess.Popen = old_popen
    D.find_ui_browser = old_find

args = captured.get("args", [])
check("窗口进程拉起来了", proc is not None)
check("用 --app 模式（没有地址栏、没有标签栏）",
      any(a.startswith("--app=http://127.0.0.1:3399") for a in args), str(args[:3]))
check("指定了窗口尺寸",
      "--window-size=1250,780" in args, str(args[:4]))
check("首次运行不弹欢迎页",
      "--no-first-run" in args and "--no-default-browser-check" in args)
check("界面用的是自己的资料目录",
      any(a.startswith("--user-data-dir=") and "JobBench" in a for a in args),
      str([a for a in args if a.startswith("--user-data-dir=")]))
check("没有把采集浏览器的资料目录用上",
      not any("jobforge-browser" in a for a in args))
check("没有把采集浏览器的调试端口占上",
      not any("remote-debugging-port" in a for a in args))

# =============================================================================
print("\n--- 后端服务能被窗口正常访问 ---")
# =============================================================================


class MiniApp:
    """够 Flask 用的最小 WSGI 应用。"""

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO") == "/api/summary":
            body = b'{"ok": true}'
            start_response("200 OK", [("Content-Type", "application/json"),
                                      ("Content-Length", str(len(body)))])
            return [body]
        body = b"ok"
        start_response("200 OK", [("Content-Type", "text/plain"),
                                  ("Content-Length", str(len(body)))])
        return [body]


port = D.pick_port(45100)
server, thread = D.serve_in_thread(MiniApp(), port)
try:
    check("服务在指定端口上起来了", D.wait_for_server(
        "http://127.0.0.1:%d" % port, timeout=10))
    body = urllib.request.urlopen(
        "http://127.0.0.1:%d/api/summary" % port, timeout=5).read()
    check("窗口要用的 /api/summary 能拿到数据", b"ok" in body, str(body[:40]))
finally:
    server.shutdown()

# =============================================================================
print("\n--- 窗口图标与标题 ---")
# =============================================================================

tpl = (ROOT / "jobforge/web/templates/index.html").read_text(encoding="utf-8")
static = ROOT / "jobforge/web/static"

check("页面引用了 favicon", 'rel="icon"' in tpl)
check("favicon 文件真的存在",
      (static / "favicon.png").is_file() and (static / "favicon.png").stat().st_size > 0)
check("favicon 不是空壳（>200 字节）",
      (static / "favicon.png").stat().st_size > 200,
      str((static / "favicon.png").stat().st_size))
check("窗口标题是短标题（标题栏放得下）",
      "<title>求职工作台</title>" in tpl)

# =============================================================================
print("\n--- 不能依赖 pywebview ---")
# =============================================================================

src = (ROOT / "jobforge/desktop.py").read_text(encoding="utf-8")
# 只看真正的 import 语句 —— 文档字符串里可以（也应该）解释为什么不用它
code = "\n".join(l for l in src.splitlines()
                 if not l.strip().startswith("#"))
check("桌面模块不 import pywebview",
      "import pywebview" not in code and "from pywebview" not in code)
req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
check("requirements 里没有 pywebview", "pywebview" not in req)
check("requirements 里没有 pythonnet（它会让安装变重）",
      "pythonnet" not in req.lower() and "clr" not in req.lower().split("\n"))

# =============================================================================
print("\n--- 端口判断必须是 bind，不能是 connect ---")
# =============================================================================
#
# 用「连一下看看通不通」判断空闲，会漏掉两类端口：
#
#   1. Windows 保留给 Hyper-V / WSL 的端口 —— connect 连不上，
#      看起来像空闲，bind 却报 errno 13。本机实测 3469-3668
#      连成 200 个端口一个都绑不上。
#   2. 已经被 bind 但没 listen 的端口 —— connect 会被拒绝
#      （Connection refused），也看起来像空闲。
#
# 第 2 种可以在任何机器上稳定复现，所以下面用它当判据。

_bound_only = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _bound_only.bind(("127.0.0.1", 0))       # 占住，但**不** listen
    _bound_only_port = _bound_only.getsockname()[1]
    check("只 bind 不 listen 的端口，connect 会失败（看起来像空闲）",
          _connect_refused(_bound_only_port))
    check("但它必须被判成「占用」（bind 才是权威）",
          D.port_free(_bound_only_port) is False,
          f"port={_bound_only_port}")
    _skipped = D.pick_port(_bound_only_port, tries=5)
    check("启动器会跳过它", _skipped != _bound_only_port,
          f"占着 {_bound_only_port}，选了 {_skipped}")
finally:
    _bound_only.close()

# 服务起不来时必须自动换一个端口，而不是崩掉
_held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _held.bind(("127.0.0.1", 0))
    _held.listen(1)
    _held_port = _held.getsockname()[1]
    _srv, _th = D.serve_in_thread(MiniApp(), _held_port, tries=5)
    try:
        from jobforge.server import server_port
        _real = server_port(_srv, None)
        check("端口被占时，服务自动换到别的端口上去了",
              _real is not None and _real != _held_port,
              f"占着 {_held_port}，实际用了 {_real}")
        check("换端口之后服务照样能用",
              D.wait_for_server("http://127.0.0.1:%d" % _real, timeout=10))
    finally:
        _srv.shutdown()
finally:
    _held.close()

# 子进程输出的解码必须容错（pip 在中文 Windows 上输出的是 GBK）
check("GBK 字节能被正确解码",
      L._decode("中文错误".encode("gbk")) == "中文错误")
check("UTF-8 字节能被正确解码",
      L._decode("中文错误".encode("utf-8")) == "中文错误")
check("解码不出来时也不会抛异常",
      L._decode(b"\xff\xfe\x00garbage") != "")

# =============================================================================
print("\n--- 新机器第一次运行的转交路径 ---")
# =============================================================================
#
# 这里曾经断过一次：`jobforge.desktop` 缺依赖时会把活转交给
# `jobforge.launch` 去自举（建 .venv、pip install），但 launch 的 main()
# 当时不接参数，于是直接 TypeError —— 崩在**装依赖之前**，
# 新机器上表现为「双击了没反应，连 venv 都没建出来」。
# 这条路径只有在新机器上才会走到，本地开发期永远走不到。

import inspect                                        # noqa: E402

sig = inspect.signature(L.main)
check("启动器 main() 能接 argv（自举时要靠它传参）",
      "argv" in sig.parameters, str(sig))

recorded = {}


def fake_launch_main(argv=None):
    recorded["argv"] = argv
    return 0


old_missing = D.missing_modules
old_launch_main = L.main
D.missing_modules = lambda: ["flask"]
L.main = fake_launch_main
try:
    rc = D.main(["--port", "4321"])
finally:
    D.missing_modules = old_missing
    L.main = old_launch_main

check("缺依赖时桌面版把活转交给启动器", recorded.get("argv") is not None, str(recorded))
check("转交时带上了 --desktop 和端口",
      recorded.get("argv") == ["--desktop", "--port", "4321"],
      str(recorded.get("argv")))
check("转交不影响退出码", rc == 0, str(rc))

# =============================================================================
print("\n" + "=" * 60)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    for n in F:
        print("   FAIL:", n)
    sys.exit(1)
print("  全部通过")
sys.exit(0)
