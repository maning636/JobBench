# -*- coding: utf-8 -*-
"""
求职工作台 · 手动导入源
=========================

用户从 BOSS / 智联等页面复制一段 JD 文本粘贴进来，或导入一份 CSV。
这条路径的价值：

1. **对用户**：不是所有开源用户都愿意让程序接管他的浏览器登录态。
   这是必须保留的兜底路径。
2. **对开发**：不依赖任何登录态就能端到端验证整条链路。
   v1.2.0 的核心缺陷（数据是空的、字段对不上）恰恰是因为只有一条
   依赖登录态的路径，且从没在真实数据上验证过。

解析策略：**宁可标为待确认，也不猜。**
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from jobforge.domain.models import Job, Channel, apply_salary


# =============================================================================
# BOSS 复制文本解析
# =============================================================================

# BOSS 复制出来的典型形态：
#   Java开发工程师
#   15-25K·13薪
#   某科技有限公司
#   北京 海淀区 3-5年 本科
#   职位描述...
#
# 也可能是一整段连续文本（从详情页全选复制）。

_SALARY_LINE = re.compile(
    r"(\d+(?:\.\d+)?\s*[-~—至到]\s*\d+(?:\.\d+)?\s*[kK](?:[·*]\s*\d+\s*薪)?)"
    r"|(\d+(?:\.\d+)?\s*[-~—至到]?\s*[kK])"
    r"|(面议)"
)
_CITY_RE = re.compile(r"(北京|上海|深圳|广州|杭州|成都|武汉|南京|西安|重庆|苏州|天津|长沙|郑州|青岛|厦门|济南|合肥|福州|大连|沈阳|哈尔滨|昆明|贵阳|南宁|太原|石家庄|兰州|南昌|无锡|宁波|东莞|佛山)")
_DISTRICT_RE = re.compile(r"(海淀|朝阳|浦东|天河|南山|宝安|西湖|余杭|高新|园区|金融港|大学城)")
_EDU_RE = re.compile(r"(博士|硕士|本科|大专|专科|高中|中专)")
_EXP_RE = re.compile(r"(\d+\s*-\s*\d+\s*年|\d+\s*年\s*以上|经验不限|在校/应届|应届|不限经验)")
_TYPE_RE = re.compile(r"(全职|兼职|实习)")
_BATCH_RE = re.compile(r"(提前批|秋招|春招|社招|校招)")
_SIZE_RE = re.compile(r"(\d+\s*-\s*\d+\s*人|\d+\s*人以上|少于\d+\s*人|\d+\s*-\s*\d+\s*人融资|[A-H]轮|\d+人以上)")
_INDUSTRY_RE = re.compile(
    r"(互联网|计算机|软件|信息技术|人工智能|新能源|半导体|电子|金融|银行|证券|保险|"
    r"教育|医疗|生物医药|汽车|制造|通信|电商|游戏|文化传媒|建筑|地产|物流|零售|快消|能源)"
)
_OUTSOURCING_STRONG = re.compile(
    r"(劳务派遣|外包|派遣|第三方人力资源|人力资源服务|中介|顾问服务|"
    r"人力资源有限公司.{0,6}(派遣|外包))"
)
_OUTSOURCING_WEAK = re.compile(
    r"(驻场|甲方项目|大厂外包|第三方|乙方|驻场人员|长期驻场)"
)


def _clean(s: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", (s or "").strip())


def parse_boss_text(raw: str, url: str = "") -> Job:
    """把一段 BOSS 复制文本解析成 Job。

    解析不到的字段一律留空，由完整率看门狗决定这条要不要推给用户。
    绝不用正则"猜"公司名 —— v1.2.0 就是这么把"5-10年"存进了 company 字段。
    """
    raw = _clean(raw)
    if not raw:
        raise ValueError("输入为空")

    lines = [l for l in (x.strip() for x in raw.splitlines()) if l]
    job = Job(channel=Channel.BOSS.value, url=url)

    # ---- JD 正文：从"职位描述"或最长的一行开始 ----
    jd_start = None
    for i, l in enumerate(lines):
        if re.match(r"^(职位描述|岗位描述|工作内容|岗位职责|职位详情|工作职责)", l):
            jd_start = i + 1
            break
    head = lines[:jd_start] if jd_start is not None else lines[:6]
    job.jd = "\n".join(lines[jd_start:]).strip() if jd_start is not None else raw

    head_text = " | ".join(head)

    # ---- 薪资（第一个命中即取，不猜测）----
    m = _SALARY_LINE.search(head_text)
    if m:
        job.salary_text = _clean(next(g for g in m.groups() if g))
    apply_salary(job)

    # ---- 城市 / 区域 ----
    m = _CITY_RE.search(head_text)
    if m:
        job.city = m.group(1)
    m = _DISTRICT_RE.search(head_text)
    if m:
        job.district = m.group(1)

    # ---- 学历 / 经验 / 用工类型 / 批次 ----
    m = _EDU_RE.search(head_text)
    if m:
        job.education = m.group(1)
    m = _EXP_RE.search(head_text)
    if m:
        job.experience = _clean(m.group(1))
    m = _TYPE_RE.search(head_text)
    if m:
        job.employment_type = m.group(1)
    m = _BATCH_RE.search(head_text)
    if m:
        job.campus_batch = m.group(1)

    # ---- 公司规模 / 行业（从全文找，但优先头部区域）----
    m = _SIZE_RE.search(head_text)
    if m:
        job.company_size = _clean(m.group(1))
    m = _INDUSTRY_RE.search(head_text)
    if m:
        job.company_industry = m.group(1)

    # ---- 岗位名 / 公司名 ----
    # 规则很朴素：第一行是岗位名；头部区域里"不像标签"的短行是公司名。
    # 判定不了就留空 —— 留空会被看门狗抓出来，比存错好。
    if head:
        job.position = _clean(head[0])[:80]
    for l in head[1:]:
        cand = _clean(l)
        if not cand or len(cand) > 30:
            continue
        if _is_label(cand):
            continue
        job.company = cand
        break

    # ---- 标签 ----
    job.tags = _extract_tags(head_text, job)

    # ---- 外包风险初判（L1 强关键词 / L2 弱关键词），只标证据不标结论 ----
    job.is_outsourcing_risk, job.outsourcing_evidence = _detect_outsourcing(raw)

    # 字段全部就位后才重算指纹 —— 否则会用空值算出全库相同的指纹
    job.finalize()
    return job


_LABEL_PATTERNS = [
    _CITY_RE, _DISTRICT_RE, _EDU_RE, _EXP_RE, _TYPE_RE, _BATCH_RE,
    _SALARY_LINE, _SIZE_RE, _INDUSTRY_RE,
]


def _is_label(text: str) -> bool:
    for p in _LABEL_PATTERNS:
        if p.fullmatch(text):
            return True
    return False


def _extract_tags(text: str, job: Job) -> List[str]:
    tags = [t for t in (job.city, job.district, job.experience,
                        job.education, job.employment_type, job.campus_batch) if t]
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _detect_outsourcing(text: str) -> Tuple[Optional[bool], List[str]]:
    """外包/派遣识别（证据分级）。

    对应 ISSUES-AUDIT 的 L1/L2 判据：
      L1 强关键词（明确写了"劳务派遣/外包"）→ 高置信
      L2 弱关键词（驻场/第三方/大厂外包）      → 中置信，只标"疑似"

    返回 (是否有风险, 证据句)。**证据必须能让人自己复核**，这是这个功能的核心价值。

    证据排序：信息量最大的句子优先。
    岗位标题里的"（派遣）"虽然是信号，但它是标签不是判据；
    JD 正文里的"由甲方派遣驻场，负责…长期驻场"才是人能复核的证据。
    """
    evidence: List[str] = []
    risk: Optional[bool] = None

    sentences = [s.strip() for s in re.split(r"[。\n；;]", text) if s.strip()]

    strong_hits = [s for s in sentences if _OUTSOURCING_STRONG.search(s)]
    weak_hits = [s for s in sentences if _OUTSOURCING_WEAK.search(s)]

    # 长句信息量更大，优先作为首要证据
    strong_hits.sort(key=len, reverse=True)
    weak_hits.sort(key=len, reverse=True)

    if strong_hits:
        risk = True
        for s in strong_hits[:2]:
            evidence.append(f"[强] {s[:80]}")
        for s in weak_hits[:1]:
            if s not in strong_hits:
                evidence.append(f"[疑似] {s[:80]}")
    elif weak_hits:
        # 只有弱信号：判为"未见明确派遣表述，但有驻场/第三方特征"，需人工确认
        risk = False
        for s in weak_hits[:2]:
            evidence.append(f"[疑似] {s[:80]}")

    return risk, evidence


def import_from_text(items: Iterable[str], url_map: Optional[Dict[int, str]] = None) -> Tuple[List[Job], List[str]]:
    """批量解析。返回 (成功列表, 错误列表)。"""
    jobs, errors = [], []
    for i, raw in enumerate(items):
        raw = _clean(raw)
        if not raw:
            continue
        try:
            url = (url_map or {}).get(i, "")
            jobs.append(parse_boss_text(raw, url))
        except Exception as e:  # noqa: BLE001 - 单条失败不能中断整批
            errors.append(f"第 {i + 1} 条: {e}")
    return jobs, errors


# =============================================================================
# CSV 导入
# =============================================================================

_CSV_ALIASES = {
    "岗位名": "position", "职位": "position", "position": "position", "title": "position",
    "公司": "company", "公司名": "company", "company": "company",
    "薪资": "salary_text", "salary": "salary_text",
    "城市": "city", "city": "city",
    "区域": "district", "district": "district",
    "经验": "experience", "经验要求": "experience", "experience": "experience",
    "学历": "education", "education": "education",
    "公司规模": "company_size", "company_industry": "行业",
    "行业": "company_industry",
    "用工类型": "employment_type",
    "批次": "campus_batch",
    "链接": "url", "url": "url", "岗位链接": "url",
    "JD": "jd", "职位描述": "jd", "jd": "jd",
    "平台": "channel",
    "平台岗位ID": "external_id",
    "标签": "tags",
}


def import_from_csv(text: str) -> Tuple[List[Job], List[str]]:
    """导入 CSV。表头支持中文别名。"""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV 没有表头"]

    mapping = {}
    for raw_name in reader.fieldnames:
        key = (raw_name or "").strip()
        mapping[key] = _CSV_ALIASES.get(key, _CSV_ALIASES.get(key.lower(), ""))

    jobs, errors = [], []
    for i, row in enumerate(reader, 1):
        d: Dict[str, Any] = {}
        for k, v in row.items():
            f = mapping.get((k or "").strip())
            if f:
                d[f] = (v or "").strip()
        if not d.get("position"):
            errors.append(f"第 {i} 行: 缺少岗位名")
            continue
        tags = d.pop("tags", "")
        job = Job.from_dict({k: v for k, v in d.items() if v})
        if tags:
            job.tags = [t.strip() for t in re.split(r"[,，;；|]", tags) if t.strip()]
        apply_salary(job)
        job.finalize()
        jobs.append(job)
    return jobs, errors
