# -*- coding: utf-8 -*-
"""
浏览器启动 / 平台切换相关测试
================================

钉死的三件事，都是「不报错但会静默坏事」那一类：

1. **Chrome 111+ 的 `/json/new` 只吃 PUT** —— GET/POST 一律 405。
   而这条路径正是「切换平台后第一次采集」要走的：
   专用浏览器是 BOSS 和智联**共用**的，切到智联时智联标签页还不存在。
   原实现发的是 POST → 必挂。
2. **`launch()` 必须把 source 传下去** —— 不传就默认按 BOSS 判状态，
   选智联时返回的却是 BOSS 的状态。
3. **浏览器已在跑时也要打开所选平台的页面** —— 两个平台共用一个窗口，
   用户先用完 BOSS 再切智联，智联的页面不会自己出现。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.adapters.sources import browser as B          # noqa: E402
from jobforge.application import browser_launcher as BL     # noqa: E402

P, F = [], []


def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))


class FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# =============================================================================
print("\n[1] new_tab 的 HTTP 方法 —— Chrome 111+ 只吃 PUT")
# =============================================================================
cap = {}


def fake_urlopen(req, *a, **kw):
    cap["method"] = req.get_method()
    cap["url"] = req.full_url
    return FakeResp({"id": "T1", "url": "https://x/",
                     "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/x"})


_orig_urlopen = B.urllib.request.urlopen
B.urllib.request.urlopen = fake_urlopen
try:
    tab = B.BrowserSession(host="127.0.0.1", port=9222).new_tab(
        "https://www.zhaopin.com/jobs/")
finally:
    B.urllib.request.urlopen = _orig_urlopen

check("new_tab 用 PUT 而不是 GET/POST", cap.get("method") == "PUT", str(cap.get("method")))
check("new_tab 带上目标 URL", "zhaopin.com" in cap.get("url", ""), cap.get("url", ""))
check("new_tab 返回 target 信息", tab.get("id") == "T1", str(tab)[:120])


# =============================================================================
print("\n[2] launch() 必须把 source 传下去")
# =============================================================================
opened = []
seen_source = []


class FakeSession:
    def __init__(self, host=None, port=None, **kw):
        pass

    def new_tab(self, url):
        opened.append(url)
        return {"id": "T2", "url": url}


def fake_status(source="boss"):
    seen_source.append(source)
    st = BL.BrowserStatus(installed=True, running=True, logged_in=True)
    st.login_required = BL.PLATFORMS.get(source, BL.PLATFORMS["boss"])["login_required"]
    return st


_orig_bs = B.BrowserSession
_orig_alive = BL.cdp_alive
_orig_status = BL.get_status
B.BrowserSession = FakeSession
BL.cdp_alive = lambda *a, **k: True
BL.get_status = fake_status
try:
    r = BL.launch(source="zhaopin")
finally:
    B.BrowserSession = _orig_bs
    BL.cdp_alive = _orig_alive
    BL.get_status = _orig_status

check("浏览器已在跑时也打开所选平台的页面",
      bool(opened) and "zhaopin.com" in opened[0], str(opened))
check("打开的是智联而不是 BOSS",
      bool(opened) and "zhipin.com" not in opened[0], str(opened))
check("launch 把 source 传给了 get_status",
      "zhaopin" in seen_source, str(seen_source))
check("已运行时不重复拉起浏览器", r.get("already_running") is True, str(r)[:160])
check("返回里带 opened_login_page", r.get("opened_login_page") is True, str(r)[:160])


# =============================================================================
print("\n[3] open_platform_page —— 两个平台都能单独打开页面")
# =============================================================================
opened.clear()
B.BrowserSession = FakeSession
BL.cdp_alive = lambda *a, **k: True
try:
    rb = BL.open_platform_page("boss")
    rz = BL.open_platform_page("zhaopin")
finally:
    B.BrowserSession = _orig_bs
    BL.cdp_alive = _orig_alive

check("open_platform_page(boss) 打开 BOSS 页面",
      rb.get("ok") and any("zhipin.com" in u for u in opened), str(opened))
check("open_platform_page(zhaopin) 打开智联页面",
      rz.get("ok") and any("zhaopin.com" in u for u in opened), str(opened))
check("两个平台开的是不同的页面", len(set(opened)) == 2, str(opened))
check("返回里带平台名", rz.get("name") == "智联招聘", str(rz)[:160])


# =============================================================================
print("\n[4] 未登录算不算就绪 —— 按平台区分")
# =============================================================================
_orig_find = BL.find_browser
_orig_alive = BL.cdp_alive
_orig_probe = BL.probe_login
BL.find_browser = lambda: "chrome.exe"
BL.cdp_alive = lambda *a, **k: True
BL.probe_login = lambda **kw: False
try:
    st_b = BL.get_status("boss")
    st_z = BL.get_status("zhaopin")
finally:
    BL.find_browser = _orig_find
    BL.cdp_alive = _orig_alive
    BL.probe_login = _orig_probe

check("BOSS 未登录 = 不就绪（采不到东西）",
      st_b.login_required is True and st_b.ready is False, str(st_b.to_dict())[:160])
check("智联未登录 = 就绪（实测能采）",
      st_z.login_required is False and st_z.ready is True, str(st_z.to_dict())[:160])
check("智联未登录时提示语说清不用登录",
      "不登录" in (st_z.hint or ""), st_z.hint)


# =============================================================================
print("\n[5] Web 接口 —— 按 source 路由")
# =============================================================================
from jobforge.persistence.sqlite_store import Store       # noqa: E402
from jobforge.web.app import create_app                   # noqa: E402

tmp = str(Path(tempfile.mkdtemp()) / "t.db")
app = create_app(tmp, store=Store(tmp))
c = app.test_client()

hit = []
_orig_open = BL.open_platform_page


def spy_open(source="boss"):
    hit.append(source)
    return {"ok": True, "source": source, "message": "ok"}


BL.open_platform_page = spy_open
try:
    r = c.post("/api/collect/open-login", json={"source": "zhaopin"})
    ok1 = r.status_code == 200 and r.get_json().get("ok") is True
    r2 = c.post("/api/collect/open-login", json={"source": "boss"})
    ok2 = r2.status_code == 200
finally:
    BL.open_platform_page = _orig_open

check("POST /api/collect/open-login 返回 200", ok1, str(r.status_code))
check("接口把 source 传给了后端", hit == ["zhaopin", "boss"], str(hit))
check("第二个平台的请求也返回 200", ok2, str(r2.status_code))


# =============================================================================
print("\n[6] 前端契约 —— 第 2 步要能选打开哪个平台")
# =============================================================================
html = (Path(__file__).resolve().parents[1] / "jobforge/web/templates/index.html").read_text(
    encoding="utf-8")
js = (Path(__file__).resolve().parents[1] / "jobforge/web/static/app.js").read_text(
    encoding="utf-8")

check("模板里有平台按钮容器 #wz2links", 'id="wz2links"' in html)
check("模板里有确认按钮 #wz2confirm", 'id="wz2confirm"' in html)
check("JS 里有平台列表 PLATFORM_LIST", "PLATFORM_LIST" in js)
check("JS 里有 openLoginPage", "openLoginPage" in js)
check("JS 调的是新接口", "/api/collect/open-login" in js)
check("两个平台都在列表里",
      "'boss'" in js and "'zhaopin'" in js)
check("确认按钮文案带平台名", "确认 " in js and "info.name" in js)
check("checkLogin 按 id 取确认按钮，不是「第 2 步第一个 .btn」",
      "$('#wz2confirm')" in js and "$('#wz2 .btn')" not in js)
check("模板里的默认按钮文案已带平台名", ">确认 BOSS 直聘 登录好了<" in html)


# =============================================================================
print("\n" + "=" * 60)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    for n in F:
        print("   FAIL:", n)
    sys.exit(1)
print("  全部通过")
sys.exit(0)
