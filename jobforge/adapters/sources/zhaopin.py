# -*- coding: utf-8 -*-
"""
求职工作台 · 智联招聘抓取器
==========================

**这一层是可以测的**：`parse_search_page` 是纯函数，喂 HTML 进去出 Job 出来，
靠 `tests/fixtures/zhaopin/*.html` + `test_zhaopin_parser.py` 验证。

**这一层是不可以测的**：`collect()` 里的浏览器会话 —— 需要真实浏览器。
所以两者严格分开，且 collect() 拿不到数据时**必须报错**，不许静默返回空列表
（同 BOSS 采集器：v1.2.0 静默返回 []，用户以为是「今天没新岗位」）。

实测依据（2026-10-23，智联新版）
--------------------------------
智联已经改过版，老资料全部失效：

  · 老入口 `sou.zhaopin.com/jobs/searchresult.ashx` 早已废弃（现在 302 跳新版）
  · 老接口 `fe-api.zhaopin.com/c/i/sou` 只返回**占位数据**
    （`results: []` + `numFound: 999999`）—— 纯 HTTP 请求会被风控挡掉，
    加上 Referer / Cookie / x-zp-page-request-id 头也还是占位值
  · 新入口 `www.zhaopin.com/jobs/?jl=530&kw=Java&kt=3`
  · 新页面把岗位数据**内联在 `__INITIAL_STATE__` 这个 JSON 里**，
    路径 `positionList[]`，每个元素就是一条岗位

所以本采集器走「页面内联 JSON」路线，而不是 DOM 选择器：

  · 字段更全 —— JD 全文、真实薪资、岗位详情 URL 一次拿全
    （实测 20/20 条都有 jobDescription / positionURL / number）
  · 比 DOM 稳 —— DOM 上薪资被打码成 `**-**元`，且卡片里**没有**岗位链接；
    BEM class 一改就废，JSON 字段名改动频率低得多

**未登录也能采**：实测未登录时列表页照样返回 20 条完整岗位。
DOM 上的 `**-**元` 是前端打码，JSON 里的 `salary60` 是真值。
登录后能多拿「立即沟通」等能力，但不是采集的前提 —— 这点和 BOSS 不一样。

城市参数必须用**数字码**
----------------------
实测 `jl=北京`（中文名）**不报错，但返回的是成都/杭州的岗位** ——
静默返回错城市的数据比报错危险得多，所以这里查不到码就直接抛异常。
码表来自智联自己的 `allCity` 接口（省→市→区三级树），共 462 城，
存在 `zhaopin_cities.json` 里。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from jobforge.adapters.sources.browser import BrowserError, BrowserSession
from jobforge.domain.models import Channel, Job, apply_salary

logger = logging.getLogger(__name__)


class ParseError(Exception):
    """页面结构变了 —— 必须当成错误上报，不能当成「没抓到」"""


# =============================================================================
# 城市码
# =============================================================================

_CITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "zhaopin_cities.json")


def _load_city_codes() -> Dict[str, str]:
    try:
        with open(_CITY_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:  # noqa: BLE001
        raise ParseError(f"城市码表读不出来（{_CITY_FILE}）：{e}") from e
    return {str(k): str(v) for k, v in raw.items()}


CITY_CODES: Dict[str, str] = _load_city_codes()


def resolve_city_code(city: str) -> str:
    """城市名 → 智联数字城市码。

    **查不到就抛异常**，绝不猜。原因：智联对无法识别的 jl 参数**不报错**，
    会静默返回别的城市的数据（实测 `jl=北京` 返回的是成都/杭州的岗位）。
    这种错比直接失败危险得多 —— 用户会以为「北京没这个岗位」。

    也接受直接传数字码（那是智联的原生格式），但会校验它确实是已知城市。
    """
    name = (city or "").strip()
    if not name or name == "全国":
        return ""
    if name in CITY_CODES:
        return CITY_CODES[name]
    # 去掉「市」后缀再试一次（「北京市」→「北京」）
    alt = name[:-1] if name.endswith("市") else name + "市"
    if alt in CITY_CODES:
        return CITY_CODES[alt]
    # 已经是数字码：校验它是已知城市再放行
    if name.isdigit():
        if name in set(CITY_CODES.values()):
            return name
        raise ParseError(f"城市码「{name}」不是已知城市码。"
                         f"内置 {len(CITY_CODES) - 1} 个城市，"
                         f"换个说法或用「全国」。")
    raise ParseError(
        f"不认识的城市「{name}」。智联的城市必须用数字码，中文名无效"
        f"（传中文会静默返回别的城市的数据）。"
        f"目前内置 {len(CITY_CODES) - 1} 个城市，换个说法或用「全国」。"
    )


# =============================================================================
# 纯解析层 —— 有 fixture 测试
# =============================================================================

_STATE_KEY = "__INITIAL_STATE__="


def _extract_state(html: str) -> Dict[str, Any]:
    """从页面里抠出 __INITIAL_STATE__ 的 JSON。

    用括号配对而不是正则贪婪匹配 —— 那份 JSON 有 200KB+ 且嵌套很深，
    正则一旦遇到字符串里的 } 就会截断。
    """
    idx = (html or "").find(_STATE_KEY)
    if idx < 0:
        raise ParseError(
            "页面里找不到 __INITIAL_STATE__。智联可能又改版了，"
            "或者这一页返回的是登录墙/错误页。")
    start = html.find("{", idx)
    if start < 0:
        raise ParseError("__INITIAL_STATE__ 后面没有 JSON 对象")

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = html[start:i + 1]
                break
    else:
        raise ParseError("__INITIAL_STATE__ 的 JSON 没有闭合，页面可能被截断了")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ParseError(f"__INITIAL_STATE__ 不是合法 JSON：{e}") from e


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<\s*(br\s*/?|/p|/div|/li|/tr)\s*>")
_BLOCK_END_RE = re.compile(r"(?i)<\s*/\s*(p|div|li|tr)\s*>")
_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def strip_html(text: str) -> str:
    """JD 正文里有 HTML 标签（<div>/<br>），剥成纯文本。

    智联的 description 里常带 <div> 分段，直接塞进 JD 字段会让
    「简历定制」功能把标签当成技能词。
    """
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_END_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    # 常见实体
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(a, b)
    s = _WS_RE.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))
    s = _MULTI_NL_RE.sub("\n\n", s)
    return s.strip()


def _tag_names(items: Any) -> List[str]:
    """智联的标签有三种形态：
      · [{id, name, standard}]   —— jobSkillTags
      · [{state, value}]          —— skillLabel
      · ["纯字符串", ...]         —— desc.labels / welfareTags
    """
    out: List[str] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, str):
            if it.strip():
                out.append(it.strip())
            continue
        if not isinstance(it, dict):
            continue
        v = it.get("name") or it.get("value")
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _first_str(*vals: Any) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _parse_item(it: Dict[str, Any]) -> Optional[Job]:
    """positionList 里的一个元素 → Job。"""
    if not isinstance(it, dict):
        return None

    # 岗位名：顶层 name 最全；没有就退到嵌套的 base
    pos = (it.get("jobDetailData") or {}).get("position") or {}
    base = pos.get("base") or {}
    desc = pos.get("desc") or {}

    name = _first_str(it.get("name"), base.get("positionName"))
    if not name:
        return None

    job = Job(channel=Channel.ZHILIAN.value)

    job.position = name
    job.company = _first_str(it.get("companyName"))
    job.salary_text = _first_str(it.get("salary60"), it.get("salary"),
                                 base.get("salary"))
    job.city = _first_str(it.get("workCity"), it.get("cityName"))
    job.district = _first_str(it.get("cityDistrict"))
    job.experience = _first_str(it.get("workingExp"), base.get("positionWorkingExp"))
    job.education = _first_str(it.get("education"), base.get("education"))
    job.company_size = _first_str(it.get("companySize"))
    job.company_industry = _first_str(it.get("industryName"))
    job.employment_type = _first_str(it.get("workType"), base.get("workType"))

    # ⚠ JD 的坑：顶层的 jobDescription 在**接口返回里恒为空**（20/20），
    # 真正的 JD 埋在 jobDetailData.position.desc.description 里。
    # 只读顶层字段的话，采集回来的岗位一条 JD 都没有 ——
    # 而 JD 是匹配度分析的核心输入，缺了就等于白采。
    job.jd = strip_html(_first_str(it.get("jobDescription"),
                                  desc.get("description"),
                                  base.get("description")))

    # 标签：技能 + 行业 + 福利，去重保序
    tags: List[str] = []
    for t in (_tag_names(it.get("skillLabel"))
              + _tag_names(it.get("jobSkillTags"))
              + _tag_names(desc.get("labels"))
              + _tag_names(desc.get("welfareTags"))
              + _tag_names(it.get("welfareLabel"))
              + _tag_names(it.get("searchTagList"))):
        if t and t not in tags:
            tags.append(t)
    job.tags = tags

    job.external_id = _first_str(it.get("number"), base.get("positionNumber"))
    job.url = _first_str(it.get("positionURL"), it.get("positionUrl"),
                         it.get("redirectUrl"))
    if not job.url and job.external_id:
        job.url = (f"https://www.zhaopin.com/jobdetail/"
                   f"{job.external_id}.htm")

    if job.position:
        apply_salary(job)
        job.finalize()
    return job


def parse_search_page(html: str, source_url: str = "") -> List[Job]:
    """从智联搜索页 HTML 解析出岗位列表。

    解析不到就抛 ParseError —— **绝不返回空列表**。
    「今天没新岗位」和「智联改版了」是两回事，混为一谈用户就会白等。
    """
    if not html or len(html) < 500:
        raise ParseError(f"页面内容过短（{len(html or '')} 字符），"
                         "多半是登录墙、错误页或页面结构变了")

    state = _extract_state(html)

    # 平台自己给的失败原因比我们自己猜要准
    for key in ("listLoadError", "listEmptyReason"):
        v = state.get(key)
        if v:
            raise ParseError(f"智联返回了错误：{v}")

    items = state.get("positionList")
    if not isinstance(items, list):
        raise ParseError("__INITIAL_STATE__ 里没有 positionList —— 智联又改版了")

    if not items:
        raise ParseError(
            "智联返回了 0 条岗位。可能是关键词太窄、没有登录、"
            "或者触发了风控（多试一次或换个关键词）。")

    jobs: List[Job] = []
    for it in items:
        j = _parse_item(it)
        if j and j.position:
            jobs.append(j)

    if not jobs:
        raise ParseError(f"找到 {len(items)} 条记录但一条都没解析出来，"
                         "字段结构可能已变")
    return jobs


def parse_positions_response(payload: Dict[str, Any]) -> List[Job]:
    """解析 `/c/i/search/positions` 的返回。

    返回体形如：
        {"code": 200, "data": {"list": [ {...}, ... ], "count": 100}}

    `data.list` 里每一项的结构与 SSR 里 `positionList` 的元素**完全一致**，
    所以两条路共用 `_parse_item`。
    """
    if not isinstance(payload, dict):
        raise ParseError("接口返回的不是 JSON 对象")

    code = payload.get("code")
    if code != 200:
        msg = payload.get("message") or payload.get("msg") or "未知错误"
        raise ParseError(f"智联接口返回 code={code}：{msg}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ParseError("接口返回里没有 data 对象")

    items = data.get("list")
    if not isinstance(items, list):
        raise ParseError("接口返回里没有 data.list —— 智联又改版了")

    jobs: List[Job] = []
    for it in items:
        j = _parse_item(it)
        if j and j.position:
            jobs.append(j)

    if items and not jobs:
        raise ParseError(f"接口返回 {len(items)} 条但一条都没解析出来，"
                         "字段结构可能已变")
    return jobs


def positions_count(payload: Dict[str, Any]) -> int:
    """接口告诉我们的总条数（拿不到就 0）。"""
    data = (payload or {}).get("data") or {}
    try:
        return int(data.get("count") or 0)
    except (TypeError, ValueError):
        return 0


# =============================================================================
# URL 与请求体构造
# =============================================================================

# 岗位列表接口。**必须 POST + 用页面里的 fetch 发**，
# 直接用 urllib 打会被风控挡掉（返回 results:[] + numFound:999999 占位值）。
POSITIONS_API = "https://fe-api.zhaopin.com/c/i/search/positions"


def build_search_url(keyword: str, city: str = "", page: int = 1) -> str:
    """构造智联搜索页 URL。

    ⚠ 城市必须走数字码 —— 见 resolve_city_code 的说明。
    """
    kw = quote(keyword or "")
    code = resolve_city_code(city)
    u = "https://www.zhaopin.com/jobs/?kt=3"
    if code:
        u += f"&jl={code}"
    if kw:
        u += f"&kw={kw}"
    return u


def build_positions_body(keyword: str, city: str = "", page: int = 1,
                         page_size: int = 20) -> Dict[str, Any]:
    """构造 `/c/i/search/positions` 的请求体。

    参数名不是我编的，是从智联自己的 JS bundle 里挖出来的
    （`formatFilterParams` 把 `kw`/`jl` 映射成下面这两个名字）：
      S_SOU_FULL_INDEX —— 关键词
      S_SOU_WORK_CITY  —— 城市码
    传 `kw`/`jl` 会被服务端忽略，且**不报错**，只是给你全国数据。

    实测：pageIndex=1 与 pageIndex=2 返回的岗位号交集为 0 —— 真翻页。
    """
    code = resolve_city_code(city)
    body: Dict[str, Any] = {
        "pageSize": page_size,
        "pageIndex": page,
        "platform": 13,
        "version": "0.0.0",
        "eventScenario": "pcSearchedSouSearch",
        "anonymous": 1,
    }
    if keyword:
        body["S_SOU_FULL_INDEX"] = keyword
    if code:
        body["S_SOU_WORK_CITY"] = code
    return body


# =============================================================================
# 会话层 —— 需要真实浏览器，无法离线验证
# =============================================================================

class ZhaopinCollector:
    """智联招聘采集器。

    与 BOSS 采集器的三点关键差异：
      1. **不强制登录** —— 实测未登录也能拿到完整岗位数据，
         所以 check_ready 不会因为没登录就拒绝采集
      2. **城市是数字码** —— 中文城市名会被智联静默忽略
      3. **数据走它自己的接口** —— 页面里内联的 `__INITIAL_STATE__`
         只有第一页，且没有分页控件、没有列表 XHR；
         翻页必须 POST `/c/i/search/positions`
    """

    PLATFORM = "智联招聘"
    DOMAIN = "zhaopin.com"
    # 页面出现这些说明已登录（未登录时页面显示的是「微信扫码快捷登录」等）
    LOGGED_IN_MARKERS = ["我的简历", "退出登录", "消息中心"]

    def __init__(self, browser: Optional[BrowserSession] = None,
                 max_pages: int = 3, settle_ms: int = 3000,
                 rate_limit: bool = True):
        self.b = browser or BrowserSession()
        self.max_pages = max_pages
        self.settle_ms = settle_ms
        self.rate_limit = rate_limit

    def check_ready(self) -> Dict[str, Any]:
        """启动前自检：Chrome 在不在、有没有登录。给明确结论而不是让用户猜。"""
        if not self.b.available():
            return {"ok": False,
                    "reason": f"连不上 Chrome（{self.b.base}）",
                    "howto": f'chrome.exe --remote-debugging-port={self.b.port} '
                             f'--user-data-dir="%TEMP%\\chrome-jobforge"'}
        st = self.b.login_state(self.DOMAIN, self.LOGGED_IN_MARKERS)
        if st.get("logged_in") is True:
            return {"ok": True, "note": "Chrome 已连接，且已登录智联"}
        return {"ok": True,
                "note": "Chrome 已连接（未登录也能采集，登录后可点「立即沟通」）"}

    def collect(self, keyword: str, city: str = "",
                pages: Optional[int] = None,
                progress=None) -> Tuple[List[Job], List[str]]:
        """抓取岗位。**返回 (jobs, errors)**，errors 非空就说明有页没成功。"""
        ready = self.check_ready()
        if not ready["ok"]:
            raise BrowserError(f"{ready['reason']}\n{ready.get('howto', '')}")

        # 城市码先解析 —— 查不到立刻报错，不要等采完了才发现城市不对
        resolve_city_code(city)

        max_pages = pages or self.max_pages
        jobs: List[Job] = []
        errors: List[str] = []
        navigated = False

        for p in range(1, max_pages + 1):
            body = build_positions_body(keyword, city, p)
            page_url = build_search_url(keyword, city, p)
            try:
                payload = self.b.post_json_in_page(
                    page_url, self.DOMAIN, POSITIONS_API, body,
                    wait_selector=".job-card" if not navigated else "",
                    settle_ms=self.settle_ms,
                    prefer=["jobs", "zhaopin"],
                    navigate=not navigated,
                    keep_tab=True,
                )
                navigated = True
                got = parse_positions_response(payload)
            except ParseError as e:
                # 第一页就 0 条 = 关键词太窄 / 被风控；后面的页 0 条 = 到底了
                if p == 1:
                    errors.append(f"第 {p} 页解析失败：{e}")
                    break
                if "没有 data.list" in str(e) or "0 条" in str(e):
                    break
                errors.append(f"第 {p} 页解析失败：{e}")
                break
            except BrowserError as e:
                errors.append(f"第 {p} 页访问失败：{e}")
                if p == 1:
                    break
                break

            if not got:
                # 翻到底了 —— 这是正常的结束，不是错误
                break
            jobs.extend(got)
            if progress:
                progress(p, max_pages, len(got), len(jobs))

            if self.rate_limit and p < max_pages:
                self._delay()

        if not jobs and errors:
            raise ParseError("；".join(errors))
        if not jobs:
            raise ParseError("智联一条岗位都没返回。可能是关键词太窄，"
                             "或者触发了风控 —— 换个关键词再试一次。")
        return jobs, errors

    def _delay(self) -> None:
        """保守节流。速率做成方法参数，方便调。"""
        delay = random.uniform(45, 90)
        logger.info("等待 %.0fs 后翻下一页", delay)
        time.sleep(delay)
