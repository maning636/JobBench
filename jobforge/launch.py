# -*- coding: utf-8 -*-
"""
双击启动入口 —— 给完全不懂软件的人用
=====================================

设计前提：使用者可能连"打开终端"都不会。所以：

  · 只需要**双击一个文件**
  · 没装 Python → 告诉他去哪装，链接直接给
  · 没装依赖   → 自动建虚拟环境并装好，不让他敲 pip
  · 端口被占   → 换端口重试，不让他自己查
  · 服务起来后自动打开浏览器，不用他自己找地址

所有提示都是人话，不出现 python / pip / venv 这类词，
除非确实需要他去做某件事。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
DEFAULT_PORT = 3399
START_URL = "http://127.0.0.1:{port}"


# --------------------------------------------------------------------------
# 输出：Windows 控制台默认 GBK，中文容易炸。统一强制 UTF-8 并允许异常字符。
# --------------------------------------------------------------------------

def _fix_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def say(msg: str = "", pause: bool = False):
    print(msg, flush=True)
    if pause:
        try:
            input("\n按回车键关闭这个窗口…")
        except (EOFError, KeyboardInterrupt):
            pass


def die(msg: str):
    say("")
    say("=" * 58)
    say(f"  {msg}")
    say("=" * 58)
    say("")
    say("把上面这段话截图发给帮你的人就行，不用自己想办法。")
    say("", pause=True)
    sys.exit(1)


# --------------------------------------------------------------------------
# 找 Python
# --------------------------------------------------------------------------

def find_python() -> str | None:
    """找一个能用的 Python。venv 里的优先。"""
    if VENV.exists():
        for rel in ("Scripts/python.exe", "bin/python"):
            p = VENV / rel
            if p.exists():
                return str(p)

    # 已经是某个解释器在跑（venv 内二次进入），直接用
    if sys.prefix != sys.base_prefix or "venv" in sys.executable.lower():
        return sys.executable

    for exe in ("py", "python", "python3"):
        try:
            r = subprocess.run([exe, "-c", "import sys;print(sys.version_info[0])"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip() == "3":
                # py 启动器需要 -3 才是 python3
                if exe == "py":
                    r2 = subprocess.run(["py", "-3", "-c", "print(1)"],
                                        capture_output=True, text=True, timeout=15)
                    if r2.returncode == 0:
                        return "py -3"
                return exe
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            continue
    return None


# 运行时真正需要的模块。少一个都不能算「装好了」——
# 只探 flask 的话，缺 bs4 / websockets 也能通过自检，
# 等用户点「立即采集」才崩，那时他已经不信任这个程序了。
REQUIRED_MODULES = ("flask", "bs4", "websockets")


def _missing_modules(py: str) -> list[str]:
    """返回装不上的模块名列表（空 = 全齐）。"""
    probe = (
        "import importlib.util\n"
        f"mods={list(REQUIRED_MODULES)!r}\n"
        "missing=[m for m in mods "
        "if importlib.util.find_spec(m) is None]\n"
        "print(','.join(missing))\n"
    )
    try:
        r = subprocess.run(_split(py) + ["-c", probe],
                           capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return list(REQUIRED_MODULES)
    if r.returncode != 0:
        return list(REQUIRED_MODULES)
    out = (r.stdout or "").strip().splitlines()
    line = out[-1].strip() if out else ""
    return [m for m in line.split(",") if m]


def ensure_deps(py: str) -> bool:
    """依赖装好返回 True。装不上返回 False（并已打印人话）。"""
    missing = _missing_modules(py)
    if not missing:
        return True

    # 只有「本来就有 Python 环境但缺包」才当场装；
    # 全新机器上要先建 venv。
    has_flask = "flask" not in missing
    if has_flask:
        say("正在安装需要的组件…")
        try:
            r = subprocess.run(_split(py) + ["-m", "pip", "install", "-q",
                                             "-r", str(ROOT / "requirements.txt")],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                say(f"安装时的报错：{(r.stderr or r.stdout or '')[-400:]}")
                die("组件没能装好，通常是网络问题。换个网络再双击一次这个文件试试。")
        except subprocess.TimeoutExpired:
            die("下载太慢超时了。换个网络，或者过一会儿再双击一次这个文件。")
        except subprocess.SubprocessError as e:
            die(f"安装时出问题了：{e}")
        missing = _missing_modules(py)
        if not missing:
            return True
        die("组件装好了，但没能用起来。缺："
            + "、".join(missing)
            + "。把上面这段话截图发给帮你的人就行。")
        return False

    say("第一次运行，正在准备（要联网，大概一两分钟）…")
    try:
        subprocess.run(_split(py) + ["-m", "venv", str(VENV)],
                       check=True, capture_output=True, timeout=180)
    except (subprocess.CalledProcessError, subprocess.SubprocessError, OSError) as e:
        die(f"准备运行环境失败：{getattr(e, 'stderr', '') or e}")

    vp = find_python()
    if not vp:
        die("建好运行环境后还是找不到它，这一步我处理不了。")

    say("正在安装需要的组件…")
    try:
        r = subprocess.run(_split(vp) + ["-m", "pip", "install", "-q",
                                         "-r", str(ROOT / "requirements.txt")],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            say(f"安装时的报错：{(r.stderr or r.stdout or '')[-400:]}")
            die("组件没能装好，通常是网络问题。换个网络再双击一次这个文件试试。")
    except subprocess.TimeoutExpired:
        die("下载太慢超时了。换个网络，或者过一会儿再双击一次这个文件。")
    except subprocess.SubprocessError as e:
        die(f"安装时出问题了：{e}")

    # 装完必须再验一次。不能假设 pip 成功 = 依赖齐全。
    missing = _missing_modules(vp)
    if missing:
        die("组件没能装好，缺："
            + "、".join(missing)
            + "。把上面这段话截图发给帮你的人就行。")
        return False
    return True


def _split(py: str) -> list[str]:
    """`"py -3"` 这种带参数的命令拆成列表。"""
    return py.split()


# --------------------------------------------------------------------------
# 端口：被占了就换一个，别让用户自己查
# --------------------------------------------------------------------------

def port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) != 0


def pick_port(start: int) -> int:
    port = start
    for _ in range(20):
        if port_free(port):
            return port
        port += 1
    return port


# --------------------------------------------------------------------------

def main() -> int:
    _fix_console()
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true", help="只自检，不常驻")
    args, _ = ap.parse_known_args()

    say()
    say("  求职工作台")
    say("  " + "-" * 46)

    py = find_python()
    if not py:
        die("你的电脑上还没有装 Python，装了才能跑这个程序。\n"
            "  下载地址：https://www.python.org/downloads/\n"
            "  下载时**一定要勾上** Add Python to PATH（安装向导第一步那个方框）")
        return 1

    if not ensure_deps(py):
        return 1

    port = pick_port(args.port)
    if port != args.port:
        say(f"（{args.port} 端口被别的程序占着了，这次改用 {port}）")

    db = ROOT / "data" / "jobforge.db"
    cmd = _split(py) + ["-X", "utf8", "-m", "jobforge.web.app",
                        "--port", str(port), "--db", str(db)]

    if args.check:
        say("自检：依赖齐全，可以正常启动。")
        return 0

    say(f"正在启动，请在下面这个地址打开浏览器：{START_URL.format(port=port)}")
    say("  （程序会自动帮你打开；没自动打开的话，把上面那行地址复制到浏览器里）")
    say("  想关掉它：关掉这个黑色窗口就行。")
    say("  " + "-" * 46)
    say("")

    env = dict(os.environ, JOBFORGE_NO_RELOAD="1")
    try:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
    except OSError as e:
        die(f"启动失败：{e}")
        return 1

    # 等服务起来再开浏览器，否则会看到一个"连不上"的页面
    url = START_URL.format(port=port)
    for _ in range(40):
        if proc.poll() is not None:
            die("程序启动后立刻退出了，多半是少了什么东西。")
            return 1
        try:
            urllib.request.urlopen(url + "/api/summary", timeout=2).read()
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    else:
        say("服务还没响应，但我先帮你把浏览器打开，再等等看。")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 —— 打不开浏览器不影响主流程
            pass

    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
