# -*- coding: utf-8 -*-
"""
求职工作台 · CDP 浏览器会话
============================

只负责「让 Chrome 去访问一个页面、把渲染后的 HTML 拿回来」。
**不含任何平台逻辑** —— 选哪个标签页、导航、等多久都是可调的。

为什么不自己造 CDP 客户端：
v1.2.0 自己写了一套（cdp_client.py 561 行），里面埋着两个已实测的坑：
  · `import asyncio` 写在方法体里，出过一次 `name 'asyncio' is not defined`
  · 新建标签页会丢登录态，v1.2.0 为此专门打了补丁但没测过
所以这里只用标准库 websockets 做最小实现，并把「复用已登录标签页」做成硬性策略。

**未验证声明**：
本模块需要用户自己启动带调试端口的 Chrome 并完成登录。
开发环境无法端到端验证 —— 但下面的解析器逻辑有离线 fixture 测试（test_boss_parser.py）。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BrowserError(Exception):
    """浏览器会话类错误——必须可区分「没连上」和「页面没数据」"""


@dataclass
class Page:
    """一个已渲染的页面"""
    url: str
    html: str
    title: str = ""

    @property
    def size(self) -> int:
        return len(self.html or "")


class BrowserSession:
    """最小 CDP 会话。零第三方依赖，只用 urllib + websockets。

    连不上就抛 BrowserError 并给出**可执行的下一步**，
    不静默返回空字符串 —— v1.2.0 那种「拿不到就当没数据」会把问题埋掉。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9222, timeout: int = 30):
        self.host = host
        self.port = port
        self.timeout = timeout

    # ------------------------------------------------------------------

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/json/version", timeout=3):
                return True
        except Exception:  # noqa: BLE001
            return False

    def require(self) -> None:
        """连不上就抛出带操作指引的错误，而不是让上层拿到空页面。"""
        if self.available():
            return
        raise BrowserError(
            f"连不上 Chrome（{self.base}）。\n"
            f"请先执行：\n"
            f'  chrome.exe --remote-debugging-port={self.port} '
            f'--user-data-dir="%TEMP%\\chrome-jobforge"\n'
            f"然后在那个 Chrome 窗口里登录招聘平台账号。"
        )

    # ------------------------------------------------------------------

    def list_targets(self) -> List[Dict[str, Any]]:
        try:
            with urllib.request.urlopen(f"{self.base}/json/list", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
            return [t for t in data if t.get("type") == "page"]
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"读取标签页列表失败：{e}") from e

    def find_tab(self, domain: str, prefer: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """找已登录的标签页。

        `prefer` 是 URL 里应该出现的关键词（如 ['geek','zhipin']），
        命中优先 —— v1.2.0 用它来找「已登录」的 BOSS 标签页。
        """
        targets = self.list_targets()
        if prefer:
            for t in targets:
                u = t.get("url", "")
                if domain in u and any(k in u for k in prefer):
                    return t
        for t in targets:
            if domain in t.get("url", ""):
                return t
        return None

    def new_tab(self, url: str = "about:blank") -> Dict[str, Any]:
        """新建标签页，返回它的 target 信息。

        ⚠ Chrome 111+ 的 `/json/new` **只接受 PUT** ——
        GET 和 POST 一律 405 Method Not Allowed。
        之前这里用 `data=b""`（等价于 POST），在现代 Chrome 上必挂，
        而这条路径正是「切换平台后第一次采集」要走的
        （浏览器是 BOSS 和智联共用的，切到智联时智联标签页还不存在）。

        实测：POST → 405，PUT → 200。
        """
        try:
            req = urllib.request.Request(
                f"{self.base}/json/new?{url}", method="PUT")
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise BrowserError(f"新建标签页失败 HTTP {e.code}：{e.reason}") from e
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"新建标签页失败：{e}") from e

    # ------------------------------------------------------------------

    def _ws_call(self, ws_url: str, method: str, params: Optional[Dict] = None,
                 wait_result: bool = True, wait_ms: int = 2500) -> Any:
        """发一条 CDP 命令。同步等待返回。"""
        import asyncio

        try:
            import websockets
        except ImportError as e:
            raise BrowserError(
                "缺少 websockets 依赖，请执行：pip install websockets"
            ) from e

        msg_id = 1
        payload = {"id": msg_id, "method": method, "params": params or {}}

        async def _run():
            async with websockets.connect(ws_url, open_timeout=self.timeout) as ws:
                await ws.send(json.dumps(payload))
                deadline = time.time() + self.timeout
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    except asyncio.TimeoutError:
                        if not wait_result:
                            return None
                        continue
                    msg = json.loads(raw)
                    if msg.get("id") == msg_id:
                        if "error" in msg:
                            raise BrowserError(f"CDP {method} 报错：{msg['error']}")
                        return msg.get("result")
                raise BrowserError(f"CDP {method} 超时（{self.timeout}s）")

        try:
            return asyncio.run(_run())
        except BrowserError:
            raise
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"CDP 通信失败：{type(e).__name__}: {e}") from e

    def evaluate(self, ws_url: str, js: str, await_promise: bool = False) -> Any:
        """在页面里执行 JS 并取回值。

        `await_promise=True` 时会等页面里的 async 函数真正跑完再取结果。
        **不打开它，async 函数返回的是一个还没完成的 Promise 对象** ——
        序列化回来是空的，调用方拿到 None 却以为"接口返回了空"。
        """
        params: Dict[str, Any] = {"expression": js, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        r = self._ws_call(ws_url, "Runtime.evaluate", params)
        res = (r or {}).get("result", {})
        if res.get("subtype") == "error":
            raise BrowserError(f"页面脚本执行出错：{res.get('description')}")
        return res.get("value")

    # ------------------------------------------------------------------

    def fetch(self, url: str, domain: str, wait_selector: str = "",
              settle_ms: int = 2500, prefer: Optional[List[str]] = None,
              max_wait: int = 25) -> Page:
        """访问 url 并拿渲染后的 HTML —— 整个模块的核心。

        策略（按优先级）：
          1. 复用已登录的同域标签页（保住登录态）
          2. 没有才新建标签页
          3. 等 wait_selector 出现，最多 max_wait 秒
          4. 固定再等 settle_ms 让异步渲染落定

        返回的 html 是渲染后的 DOM，可直接喂给解析器。
        """
        self.require()

        tab = self.find_tab(domain, prefer)
        reused = tab is not None
        if not tab:
            tab = self.new_tab(url)
        ws = tab.get("webSocketDebuggerUrl")
        if not ws:
            raise BrowserError("标签页没有 webSocketDebuggerUrl，无法接管")

        try:
            self._ws_call(ws, "Page.navigate", {"url": url}, wait_result=False)

            if wait_selector:
                self._wait_for(ws, wait_selector, max_wait)
            time.sleep(settle_ms / 1000)

            html = self.evaluate(ws, "document.documentElement.outerHTML") or ""
            title = self.evaluate(ws, "document.title") or ""
            return Page(url=self.evaluate(ws, "location.href") or url,
                        html=html, title=title)
        finally:
            # 只关我们自己开的标签页，不动用户已有的
            if not reused:
                try:
                    self._ws_call(ws, "Target.closeTarget",
                                  {"targetId": tab.get("id")}, wait_result=False)
                except Exception:  # noqa: BLE001
                    pass

    def _wait_for(self, ws_url: str, selector: str, max_wait: int) -> bool:
        js = ("(function(){"
              "try{return !!document.querySelector(%s)}"
              "catch(e){return false}})()") % json.dumps(selector)
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                if self.evaluate(ws_url, js):
                    return True
            except BrowserError:
                pass
            time.sleep(0.6)
        logger.warning("等待选择器 %s 超时（%ss），仍返回当前页面", selector, max_wait)
        return False

    # ------------------------------------------------------------------

    def post_json_in_page(self, page_url: str, domain: str, api_url: str,
                          body: Dict[str, Any], wait_selector: str = "",
                          settle_ms: int = 2500, prefer: Optional[List[str]] = None,
                          max_wait: int = 25, navigate: bool = True,
                          keep_tab: bool = False) -> Any:
        """在页面上下文里发一个 POST，把 JSON 结果拿回来。

        **为什么需要它**：有些平台的数据不在 HTML 里，而在它自己的接口里
        （智联的 `/c/i/search/positions` 就是这样）。
        直接用 urllib 打这个接口会被风控挡掉 —— 实测智联对纯 HTTP 请求
        一律返回占位数据（`results: []` + `numFound: 999999`），
        加上 Referer / Cookie / x-zp-page-request-id 头也还是占位值。
        必须**借浏览器的手**：请求由页面里的 fetch 发出，
        自带完整 Cookie、TLS 指纹和 JS 环境。

        参数：
          page_url     —— 先导航到哪个页面（建立 cookie / 同源上下文）
          api_url      —— 要 POST 的接口
          body         —— JSON 请求体
          navigate     —— 是否先导航。翻页时传 False 复用已开的标签页，
                          智联的搜索页有 1.7MB，每翻一页都重新加载太慢
          keep_tab     —— 是否保留自己开的标签页（下次调用还能复用）
        """
        self.require()

        tab = self.find_tab(domain, prefer)
        reused = tab is not None
        if not tab:
            if not navigate:
                raise BrowserError(
                    f"没有找到 {domain} 的标签页，无法在已有页面上发请求")
            tab = self.new_tab(page_url)
        ws = tab.get("webSocketDebuggerUrl")
        if not ws:
            raise BrowserError("标签页没有 webSocketDebuggerUrl，无法接管")

        try:
            if navigate:
                self._ws_call(ws, "Page.navigate", {"url": page_url}, wait_result=False)
                if wait_selector:
                    self._wait_for(ws, wait_selector, max_wait)
                time.sleep(settle_ms / 1000)

            js = """
            (async () => {
                const r = await fetch(%(api)s, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json',
                              'Accept': 'application/json, text/plain, */*'},
                    body: JSON.stringify(%(body)s)
                });
                const t = await r.text();
                return {status: r.status, text: t};
            })()
            """ % {"api": json.dumps(api_url), "body": json.dumps(body)}

            raw = self.evaluate(ws, js, await_promise=True)
            if not isinstance(raw, dict):
                raise BrowserError(
                    "页面内 fetch 没有返回预期结果（"
                    "拿到 %r）—— 页面可能还没加载完" % (type(raw).__name__,))
            status = raw.get("status")
            text = raw.get("text") or ""
            if status != 200:
                raise BrowserError(f"接口 HTTP {status}：{text[:200]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise BrowserError(f"接口返回的不是 JSON：{text[:200]}") from e
        finally:
            # 只关我们自己开的标签页，不动用户已有的
            if not reused and not keep_tab:
                try:
                    self._ws_call(ws, "Target.closeTarget",
                                  {"targetId": tab.get("id")}, wait_result=False)
                except Exception:  # noqa: BLE001
                    pass

    def login_state(self, domain: str, logged_in_markers: List[str]) -> Dict[str, Any]:
        """粗判是否已登录。用于给用户明确提示，而不是靠猜。

        ⚠ 参数是「**已登录**时页面会出现的文案」，命中即视为已登录。
        传错方向会得到完全相反的结论 —— 这条链原先没有测试覆盖，
        现在参数名写死成 logged_in_markers 就是为了让传错的人看得见。
        """
        try:
            tab = self.find_tab(domain)
            if not tab:
                return {"logged_in": None, "reason": f"没有打开 {domain} 的标签页"}
            ws = tab["webSocketDebuggerUrl"]
            html = self.evaluate(ws, "document.documentElement.outerHTML") or ""
            hit = [m for m in logged_in_markers if m in html]
            return {"logged_in": bool(hit) if logged_in_markers else None,
                    "markers": hit}
        except BrowserError as e:
            return {"logged_in": None, "reason": str(e)}
