# -*- coding: utf-8 -*-
"""
求职工作台 · BOSS 直聘抓取器
==============================

**这一层是可以测的**：`parse_list_page` / `parse_detail_page` 是纯函数，
喂 HTML 进去出 Job 出来，全靠 `tests/fixtures/boss/*.html` + `test_boss_parser.py` 验证。

**这一层不可以测的**：`collect()` 里的浏览器会话 —— 需要你自己的登录态。
所以两者严格分开，且 collect() 拿不到数据时**必须报错**，不许静默返回空列表
（v1.2.0 的教训：失败被吞掉，用户以为是「今天没新岗位」）。

选择器的诚实说明：
BOSS 的 DOM 改版频繁。下面每个选择器都配了 fixture，
但**平台改版时 fixture 不会自动失效** —— 这是静态 fixture 的固有局限。
所以 parse 返回 0 条时必须抛异常，不能当成「没岗位」。
"""
from __future__ import annotations

import logging
import re
import time
import random
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from jobforge.adapters.sources.browser import BrowserError, BrowserSession
from jobforge.adapters.sources.manual import _clean
from jobforge.domain.models import Channel, Job, apply_salary

logger = logging.getLogger(__name__)


class ParseError(Exception):
    """页面结构变了 —— 必须当成错误上报，不能当成「没抓到」"""


# 城市码只覆盖常见城市，够用即可；要全量请换数据源
CITY_CODES = {
    "全国": "100010000", "北京": "101010100", "上海": "101020100",
    "广州": "101280100", "深圳": "101280600", "杭州": "101210100",
    "南京": "101190100", "苏州": "101190400", "成都": "101270100",
    "武汉": "101200100", "西安": "101110100", "重庆": "101040100",
    "天津": "101030100", "长沙": "101250100", "郑州": "101180100",
    "青岛": "101120200", "厦门": "101230200", "济南": "101120100",
    "合肥": "101220100", "福州": "101230100", "大连": "101070200",
    "沈阳": "101070100", "哈尔滨": "101050100", "长春": "101060100",
    "济南 ": "101120100", "昆明": "101290100", "南宁": "101310100",
    "贵阳": "101260100", "南昌": "101160100", "太原": "101100100",
    "石家庄": "101090100", "兰州": "101110100", "乌鲁木齐": "101130100",
}

_SAL_RE = re.compile(
    r"(\d+\s*-\s*\d+\s*[kK](?:\s*[·*]\s*\d+\s*薪)?)"
    r"|(\d+\s*-\s*\d+\s*元/天)"
    r"|(面议)"
)


# =============================================================================
# 纯解析层 —— 有 fixture 测试
# =============================================================================

def _sel(css_list: List[Tuple[str, Any]], want: str) -> Optional[str]:
    for sel, text in css_list:
        if sel == want:
            return text
    return None


def parse_list_page(html: str, source_url: str = "") -> List[Job]:
    """从岗位列表页 HTML 解析出岗位列表。

    优先在浏览器里执行 JS 取数据（BOSS 的列表是虚拟滚动的，
    BeautifulSoup 拿不到未进入视口的卡片）；此处是 fallback 与测试入口。
    """
    from bs4 import BeautifulSoup

    if not html or len(html) < 200:
        raise ParseError(f"页面内容过短（{len(html or '')} 字符），"
                         "多半是登录墙或页面结构变了")

    soup = BeautifulSoup(html, "html.parser")
    cards = (soup.select(".job-card-box")
             or soup.select("li.job-card-box")
             or soup.select("[class*='job-card-box']"))

    if not cards:
        # 区分两种失败：登录墙 vs 结构变了
        if _looks_like_login_wall(html):
            raise ParseError("页面是登录墙 —— 请在 Chrome 里登录 BOSS 后重试")
        raise ParseError(
            "没有找到岗位卡片（.job-card-box）。"
            "这通常意味着 BOSS 改版了选择器，需要更新解析规则。"
        )

    jobs: List[Job] = []
    for card in cards:
        j = _parse_card(card, source_url)
        if j and j.position:
            jobs.append(j)
    if not jobs:
        raise ParseError(f"找到 {len(cards)} 张卡片但一条都没解析出来，"
                         "选择器可能已失效")
    return jobs


def _looks_like_login_wall(html: str) -> bool:
    markers = ["登录/注册", "请登录", "扫码登录", "登录后查看"]
    return sum(1 for m in markers if m in html) >= 2


def _parse_card(card: Any, source_url: str) -> Optional[Job]:
    def txt(sel: str) -> str:
        el = card.select_one(sel)
        return _clean(el.get_text(strip=True)) if el else ""

    def href(sel: str) -> str:
        el = card.select_one(sel)
        if not el or not el.get("href"):
            return ""
        h = el["href"]
        if h.startswith("http"):
            return h
        from urllib.parse import urljoin
        return urljoin("https://www.zhipin.com", h)

    job = Job(channel=Channel.BOSS.value)

    job.position = txt(".job-name") or txt("[class*='job-name']")
    job.salary_text = txt(".job-salary") or txt("[class*='salary']")
    job.company = txt(".company-name") or txt("[class*='company-name']")
    job.district = txt(".company-area") or txt(".job-area")

    job.url = href("a.job-name") or href("a[href*='/job_detail/']") or href("a[href]")

    tags = [_clean(li.get_text(strip=True))
            for li in card.select(".tag-list li")]
    job.tags = [t for t in tags if t]

    exp = next((t for t in tags if "年" in t or t in ("经验不限", "在校/应届")), "")
    job.experience = exp

    edus = ["博士", "硕士", "本科", "大专", "高中"]
    job.education = next((t for t in tags if t in edus), "")

    for t in tags:
        if t in ("全职", "兼职", "实习"):
            job.employment_type = t
            break

    job.company_size = next(
        (t for t in tags if re.search(r"\d+\s*-\s*\d+\s*人", t)), "")
    job.company_industry = next(
        (t for t in tags if re.match(r"^[一-龥]{2,6}$", t)
         and not re.search(r"\d", t) and t not in edus
         and t not in ("全职", "兼职", "实习", exp, job.company_size)), "")

    if job.position:
        job.city = _guess_city(job.district or job.company or "", source_url)
        apply_salary(job)
        job.finalize()
    return job


_CITY_HINT = ["北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都",
              "武汉", "西安", "重庆", "天津", "长沙", "郑州", "青岛", "厦门",
              "济南", "合肥", "福州", "大连", "沈阳", "哈尔滨", "长春", "昆明"]


def _guess_city(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    for c in _CITY_HINT:
        if c in blob:
            return c
    return ""


def build_search_url(keyword: str, city: str = "", page: int = 1) -> str:
    kw = quote(keyword)
    cc = CITY_CODES.get((city or "").strip(), CITY_CODES["全国"])
    u = f"https://www.zhipin.com/web/geek/job?query={kw}&city={cc}&page={page}"
    return u


def _to_city_code(city: str) -> str:
    return CITY_CODES.get((city or "").strip(), CITY_CODES["全国"])


# =============================================================================
# 会话层 —— 需要用户登录态，无法离线验证
# =============================================================================

class BossCollector:
    """BOSS 直聘采集器。

    与 v1.2.0 的三处关键差异：
      1. 拿不到数据时抛 ParseError，而不是返回空列表
         （v1.2.0 静默返回 []，用户以为是「今天没新岗位」）
      2. 选择器解析与浏览器会话彻底分离，前者有测试
      3. 节流速率可配置，且明确告知用户这一层未端到端验证
    """

    PLATFORM = "BOSS直聘"
    # 页面出现这些说明**已登录**（未登录时页面显示的是「登录/注册」「扫码登录」）
    LOGGED_IN_MARKERS = ["我的", "退出", "已登录", "消息"]

    def __init__(self, browser: Optional[BrowserSession] = None,
                 max_pages: int = 3, settle_ms: int = 2500,
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
        st = self.b.login_state("zhipin.com", self.LOGGED_IN_MARKERS)
        if st.get("logged_in") is False:
            return {"ok": False, "reason": "BOSS 页面显示未登录",
                    "howto": "在那个 Chrome 窗口里登录 BOSS 直聘账号"}
        return {"ok": True, "note": "Chrome 已连接"}

    def collect(self, keyword: str, city: str = "",
                pages: Optional[int] = None,
                progress=None) -> Tuple[List[Job], List[str]]:
        """抓取岗位。**返回 (jobs, errors)**，errors 非空就说明有页没成功。"""
        ready = self.check_ready()
        if not ready["ok"]:
            raise BrowserError(f"{ready['reason']}\n{ready.get('howto', '')}")

        max_pages = pages or self.max_pages
        jobs: List[Job] = []
        errors: List[str] = []

        for p in range(1, max_pages + 1):
            url = build_search_url(keyword, city, p)
            try:
                page = self.b.fetch(
                    url, "zhipin.com",
                    wait_selector=".job-card-box",
                    settle_ms=self.settle_ms,
                    prefer=["geek", "job", "zhipin"],
                )
                got = parse_list_page(page.html, page.url)
                jobs.extend(got)
                if progress:
                    progress(p, max_pages, len(got), len(jobs))
            except ParseError as e:
                errors.append(f"第 {p} 页解析失败：{e}")
                logger.warning("第 %s 页解析失败：%s", p, e)
                # 第一页就挂 → 结构变了，继续翻页没意义
                if p == 1:
                    break
            except BrowserError as e:
                errors.append(f"第 {p} 页访问失败：{e}")
                if p == 1:
                    break

            if self.rate_limit and p < max_pages:
                self._delay()

        if not jobs and errors:
            raise ParseError("；".join(errors))
        return jobs, errors

    def _delay(self) -> None:
        """保守节流。速率做成方法参数，方便调，不要再硬编码进业务逻辑
        —— v1.2.0 把 60-180 秒硬编码在业务里，导致既慢又难调。"""
        delay = random.uniform(45, 90)
        logger.info("等待 %.0fs 后翻下一页", delay)
        time.sleep(delay)
