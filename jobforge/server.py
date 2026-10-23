# -*- coding: utf-8 -*-
"""起一个本地服务 —— 而且**不许**因为端口问题把整个程序干掉。

werkzeug 的坑（实测踩过）
--------------------------
``make_server()`` 在端口绑不上的时候，不是抛异常，而是：

    print(e.strerror, file=sys.stderr)
    ...
    sys.exit(1)

SystemExit 不是 OSError，``except OSError`` 拦不住它 ——
进程会**直接消失**，控制台上只留下一句
「以一种访问权限不允许的方式做了一个访问套接字的尝试。」

对完全不懂技术的人来说，这就是「双击了没反应」。

而且这不是偶发：Windows 会保留一批端口给 Hyper-V / WSL / Docker，
本机实测 3469-3668 连成 200 个端口一个都绑不上
（netsh interface ipv4 show excludedportrange protocol=tcp）。
挑到里面的端口就是这种下场。

所以这里**自己 bind、自己 listen**，再把 fd 交给 werkzeug ——
它就不再去 bind，也就不会 sys.exit。
绑定失败是我们自己的异常，可以干净地换一个端口重试。

只用标准库 + werkzeug，不引入新依赖。
"""
from __future__ import annotations

import socket
from typing import Optional, Tuple

# 和 werkzeug 的 LISTEN_QUEUE 对齐
LISTEN_QUEUE = 128

# 端口被占时往后找几个。Windows 一次能连续保留 200 多个端口，
# 扫得比它宽才跳得出去。
TRIES = 256


def bind_socket(host: str, port: int) -> socket.socket:
    """绑一个监听用的 socket。绑不上就抛 OSError（不会退出进程）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 和 werkzeug 起服务时的做法保持一致：允许复用 TIME_WAIT 的端口。
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(LISTEN_QUEUE)
        return s
    except BaseException:
        s.close()
        raise


def server_port(server, fallback: int = 0) -> int:
    """拿服务实际绑上的端口。

    注意：werkzeug 的 ``server_port`` 是在 ``server_bind()`` 里赋值的，
    而我们走的是 fd 分支（它不调 server_bind）—— 那个属性根本不存在。
    ``server_address`` 才是任何分支下都有的。
    """
    addr = getattr(server, "server_address", None)
    if addr:
        try:
            return int(addr[1])
        except (TypeError, IndexError, ValueError):
            pass
    return int(getattr(server, "server_port", None) or fallback)


def make_server_on(host: str, port: int, app, tries: int = TRIES
                   ) -> Tuple[object, int]:
    """起一个 werkzeug 服务；端口被占就自动往后找一个。

    返回 (server, 实际用上的端口)。
    整个窗口都绑不上就抛最后一个 OSError —— 那是真的没辙了。
    """
    from werkzeug.serving import make_server

    last: Optional[OSError] = None
    for i in range(max(1, tries)):
        try:
            sock = bind_socket(host, port + i)
        except OSError as e:
            last = e
            continue
        try:
            server = make_server(host, port + i, app, threaded=True,
                                 fd=sock.fileno())
        finally:
            # werkzeug 用 socket.fromfd 复制了一份，我们这份可以关掉了。
            # 关早了它还能用，关晚了就漏一个句柄。
            sock.close()
        return server, server_port(server, port + i)
    raise last if last else OSError("没有找到可用的端口")
