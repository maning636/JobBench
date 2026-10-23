# -*- coding: utf-8 -*-
"""端口选择 —— 「空闲」必须真的 bind 一次才算数。

为什么不能用「连一下看看通不通」
--------------------------------
Windows 会保留一批端口给 Hyper-V / WSL / Docker / WinNAT，
命令是 ``netsh interface ipv4 show excludedportrange protocol=tcp``。
这种端口 connect 是**连不上**的，用 connect_ex 判断会以为它是空闲的，
但 bind 会被系统直接拒绝，errno 13 (WSAEACCES)。

实测（2026-10-23，本机）：
  3469-3568、3569-3668 两段**连起来 200 个端口**一个都绑不上。
表现是程序一启动就崩，弹一句
「以一种访问权限不允许的方式做了一个访问套接字的尝试」——
用户看不懂，只能重试；重试还很可能又挑到同一段保留区里。

所以这里只有一种判断方式：**真的 bind 一次**。
试完立刻 close，没 listen 过，不会进 TIME_WAIT。

为什么**不**设 SO_REUSEADDR
---------------------------
Windows 上，如果占着端口的那个 socket 设了 SO_REUSEADDR，
新建的 socket 也设了就能抢过去（劫持）。
werkzeug 起服务时是设了 SO_REUSEADDR 的 —— 如果我们这里也设，
挑端口就会挑到「另一个正在跑的服务」的端口上去。

不设的话，Windows 要求两边都设才允许重复绑定，
所以**任何**正在监听的端口都能被我们正确地判成「占用」。
（实测：占方设了 REUSE、我们不设 → bind 失败 10048，判断正确。）

只用标准库，不依赖任何第三方包 —— 这个模块在依赖装好之前
就要能用（首次运行的引导流程会先用到它）。
"""
from __future__ import annotations

import socket

# 默认端口。3399 挑在常见的保留区之外，但保留区会随系统配置变化，
# 真正的兜底是「绑不上就自动往后找」。
DEFAULT_PORT = 3399

# 往后找几个。
#
# 256 不是随便拍的：Windows 一次能连续保留 200 多个端口
# （本机实测 3469-3668 整整 200 个连成一段），
# 扫得比它宽，才跳得出去。每次 bind 也就几十微秒，不心疼。
TRIES = 256


def port_free(port: int) -> bool:
    """这个端口现在能不能拿来监听。

    判断方式是**真的 bind 一次** —— 这是唯一权威的方式，
    connect / connect_ex 都会把「Windows 保留端口」误判成空闲。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ask_os_for_port() -> int | None:
    """让操作系统自己挑一个空闲端口。

    系统不会把「保留端口」分配给你，所以这是最后的兜底：
    附近几百个端口都被保留时，至少还能起来。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])
        except OSError:
            return None


def pick_port(start: int = DEFAULT_PORT, tries: int = TRIES) -> int:
    """找一个能用的端口。

    端口被占不是错误 —— 换一个继续。
    整个窗口都绑不上（Windows 保留了一大片），
    就让操作系统自己挑一个。
    """
    port = start
    for _ in range(max(1, tries)):
        if port_free(port):
            return port
        port += 1
    return ask_os_for_port() or port
