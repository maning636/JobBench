# -*- coding: utf-8 -*-
"""
岗位可信度体检
================================

**这是从 career-ops（75k star）简化来的中间态。**

他们那边对应的东西散在三个地方：
  · `modes/zh/_shared.md`  → 公司类型 → 薪资可信度映射表（他们列了 10 类）
  · `modes/oferta-signals/13-*` → 薪资区间宽度过宽（纯算术，无法律依据）
  · `modes/oferta-signals/16-*` → 固定期限合同披露
  · Block G（职位真实性）→ 独立于评分的幽灵岗位检测

我们没抄全，是因为他们的判据大量依赖欧美法域（竞业补偿金比例、
AI 筛选披露、agency licensing）和多来源交叉验证（平台地点 vs 雇主官网），
这些在 BOSS/智联 的场景里大部分用不上。

**但内核是可以砍下来用的，而且砍掉之后对我们的用户更有价值。**
他们的公司类型表有 10 类，其中"猎头/第三方""开源社区"对我们无意义；
我们砍到 5 类，全是国内大学生真正会遇到的。

────────────────────────────────────────────────────────────────────────
三条不能丢的原则
────────────────────────────────────────────────────────────────────────

1. **可信度独立于匹配度。** 这是他们 Block G 的精髓，必须照搬：
   一个岗位匹配度 90% 但可能是幽灵岗，必须同时把"这岗位可能是假的"
   摆在用户眼前。**绝不能因为匹配度高就藏起真实性风险。**

2. **只报可观察的事实，不下结论。** 他们的原话是
   "state only observable facts … not legal advice"。
   我们说「区间宽度是下限的一半以上」，不说「这家公司在骗人」。

3. **宁可少报，不可乱报。** 每条信号都必须能从岗位文本或已有数据里
   指出**证据原文**。指不出证据的信号不报 —— 这是 v1.2.0 的老教训，
   把猜测当事实输出会静默污染决策。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


# =============================================================================
# 公司类型
# =============================================================================

class CompanyType(str):
    """砍到 5 类 —— 只留国内大学生真正会撞上的。"""

    BIG = "big"              # 大厂 / 国企 / 事业单位
    STARTUP = "startup"      # 创业公司 / 成长期
    OUTSOURCED = "outsourced"  # 外包 / 派遣 / 中介
    SMALL = "small"          # 个体 / 小公司 / 民营小微企业
    UNKNOWN = "unknown"      # 判断不出来


COMPANY_LABEL = {
    CompanyType.BIG: "大厂 / 国企 / 事业单位",
    CompanyType.STARTUP: "创业公司 / 成长期公司",
    CompanyType.OUTSOURCED: "外包 / 派遣 / 中介",
    CompanyType.SMALL: "个体户 / 小公司",
    CompanyType.UNKNOWN: "暂时看不出来",
}

# 每类公司的薪资可信度基准。参照 career-ops 的映射表，
# 但把 10 类砍成 5 类。
#
# ⚠ 这里对 career-ops 做了一处**故意的偏离**，必须说清楚：
# 他们规定「Unknown 时薪资可信度默认取低」，理由是他们有上市公司、
# 融资、职级体系等一大堆信号可以补充判断。
# 我们没有这些信号 —— BOSS 上绝大多数就是「XX有限公司」，
# 一律判低的结果是每条岗位都显示"低"，徽章彻底失去区分度，
# 用户看三次就再也不看了。
#
# 断言"低"本身就是一种不诚实的置信度：我们明明不知道。
# 按本项目一贯的原则（拿不出证据就不报），这里如实说"判断不出来"，
# 同时把它单列成一档，让它和真正判低的派遣/个体户区分开。
COMPANY_PAY_TRUST = {
    CompanyType.BIG: ("高", "大厂/国企的薪资结构和职级通常写得比较实"),
    CompanyType.STARTUP: ("中", "创业公司薪资常混着期权、绩效、补贴，不一定全是到手"),
    CompanyType.OUTSOURCED: ("低", "外包派遣报的往往是用工方预算，不等于你实际拿到的"),
    CompanyType.SMALL: ("低", "小公司常写「综合薪资」，不一定含五险一金"),
    CompanyType.UNKNOWN: ("未知", "公司性质看不出来，没法判断 —— 面试时直接问薪资结构和社保基数"),
}

_BIG_HINTS = (
    "集团", "股份", "上市", "国有", "国企", "央企", "事业单位", "政府",
    "研究院", "研究所", "银行", "证券", "保险", "医院", "学校", "大学",
)
_STARTUP_HINTS = ("创业", "科技有限", "网络科技", "信息技术", "互联网", "软件", "智能", "数据")
_OUTSOURCING_HINTS = (
    "人力资源", "劳务", "派遣", "外包", "中介", "人才服务", "咨询",
    "服务外包", " workforce", "人力外包",
)
_SMALL_HINTS = ("个体", "个体工商户", "小微企业", "工作室", "夫妻店", "门店")


def classify_company(company: str, extra_text: str = "") -> str:
    """从公司名和 JD 文本猜公司类型。

    **这是启发式，不是事实。** 判断不出来就返回 UNKNOWN，
    绝不硬凑一个类型 —— 因为类型会直接影响"这个薪资能不能信"的结论。
    """
    name = str(company or "")
    blob = name + " " + str(extra_text or "")

    # 外包优先级最高：一个人力资源公司名字里带"科技"很常见，
    # 但它的性质决定一切 —— 先判它是不是中介/派遣
    if any(k in name for k in _OUTSOURCING_HINTS):
        return CompanyType.OUTSOURCED
    if any(k in blob for k in ("第三方招聘", "代招", "劳务派遣", "外包岗位")):
        return CompanyType.OUTSOURCED

    if any(k in name for k in _BIG_HINTS):
        return CompanyType.BIG
    if any(k in name for k in _SMALL_HINTS):
        return CompanyType.SMALL
    if any(k in name for k in _STARTUP_HINTS):
        return CompanyType.STARTUP
    return CompanyType.UNKNOWN


# =============================================================================
# 信号
# =============================================================================

# 级别：info 只是提醒，warn 要留心，bad 是明显风险
LEVELS = {"info", "warn", "bad"}


@dataclass
class TrustSignal:
    level: str = "info"
    code: str = ""
    title: str = ""
    detail: str = ""
    evidence: str = ""      # 证据原文 —— 没有证据就不该报这条

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "code": self.code, "title": self.title,
                "detail": self.detail, "evidence": self.evidence}


# =============================================================================
# 报告
# =============================================================================

@dataclass
class TrustReport:
    company_type: str = CompanyType.UNKNOWN
    company_type_label: str = ""
    # 四档：高 / 中 / 低 / 未知。"未知" 是一等公民，不是"低"的同义词
    pay_trust: str = "未知"
    pay_trust_reason: str = ""

    # 独立于匹配度 —— 见文件头的原则 1
    ghost_risk: str = "none"          # none / suspect
    ghost_reason: str = ""

    signals: List[TrustSignal] = field(default_factory=list)
    # 本次到底依据了什么字段。什么都没依据时不能装作分析过。
    checked_fields: List[str] = field(default_factory=list)

    @property
    def score_penalty(self) -> float:
        """明确返回 0：可信度**不参与**匹配度计算。

        刻意做成函数而不是常量 —— 这样"要不要扣分"这个决定
        永远是显式的，将来谁想改都必须先动这里，而不是悄悄在别处加权。
        """
        return 0.0

    def worst_level(self) -> str:
        if any(s.level == "bad" for s in self.signals):
            return "bad"
        if any(s.level == "warn" for s in self.signals):
            return "warn"
        return "info"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "company_type": self.company_type,
            "company_type_label": self.company_type_label,
            "pay_trust": self.pay_trust,
            "pay_trust_reason": self.pay_trust_reason,
            "ghost_risk": self.ghost_risk,
            "ghost_reason": self.ghost_reason,
            "signals": [s.to_dict() for s in self.signals],
            "checked_fields": self.checked_fields,
            "worst_level": self.worst_level(),
            "score_penalty": self.score_penalty,
        }


# =============================================================================
# 主检测
# =============================================================================

def _days_since(iso: str) -> Optional[int]:
    if not iso:
        return None
    s = str(iso)[:10]
    try:
        return (date.today() - datetime.strptime(s, "%Y-%m-%d").date()).days
    except (ValueError, TypeError):
        return None


def check_trust(job, candidate_salary_expect: Optional[int] = None) -> TrustReport:
    """对单个岗位做可信度体检。

    全部是纯规则，不调 LLM —— 这类判断要的是**可复现**，
    同一份 JD 跑两次必须给出一样的结论。
    """
    r = TrustReport()
    jd = str(getattr(job, "jd", "") or "")
    name = str(getattr(job, "company", "") or "")
    blob = jd + " " + name + " " + str(getattr(job, "position", "") or "")

    r.checked_fields = [f for f in ("company", "jd", "salary", "first_seen")
                        if _has_field(job, f)]

    # ---------- 公司类型 → 薪资可信度 ----------
    r.company_type = classify_company(name, jd)
    r.company_type_label = COMPANY_LABEL.get(r.company_type, "")
    trust, why = COMPANY_PAY_TRUST.get(r.company_type, ("低", ""))
    r.pay_trust, r.pay_trust_reason = trust, why

    if r.company_type == CompanyType.OUTSOURCED:
        r.signals.append(TrustSignal(
            level="warn", code="outsourced_company",
            title="这家公司名字像人力资源/劳务/派遣",
            detail=f"{name} 这类公司往往是中介或外包。报出来的薪资是用工方的预算，"
                   f"不等于你实际到手的，也不等于你入职后是什么身份。",
            evidence=name))

    # ---------- 13 号信号：薪资区间过宽 ----------
    lo = getattr(job, "salary_min", None)
    hi = getattr(job, "salary_max", None)
    if lo and hi and lo > 0 and hi > lo:
        width = hi - lo
        # 他们用的是 (hi - lo) > 0.5 * lo，直接照搬。
        # 纯算术，不涉及任何法域。
        if width > 0.5 * lo:
            r.signals.append(TrustSignal(
                level="info", code="wide_salary_range",
                title="薪资区间开得特别宽",
                detail=f"下限 {lo / 1000:.0f}K、上限 {hi / 1000:.0f}K，"
                       f"跨度是下限的一半以上。区间这么宽通常说明这个岗位的"
                       f"真实 band 还没定，或者这条是模板/聚合出来的。"
                       f"值得直接问 HR 这个级别实际是多少。",
                evidence=str(getattr(job, "salary_text", "") or "")))

    # ---------- 模糊薪资表述 ----------
    sal_text = str(getattr(job, "salary_text", "") or "")
    for kw, why in (
        ("最高可达", "「最高可达」往往意味着要靠绩效才能拿到，写的是上限不是你能拿到的"),
        ("综合薪资", "「综合薪资」通常含补贴、提成、五险一金，不等于到手"),
        ("面议", "写「面议」等于没写"),
        ("无责底薪", "「无责底薪」之外通常还有提成任务，注意问清结构"),
    ):
        if kw in (sal_text + blob):
            r.signals.append(TrustSignal(
                level="info", code=f"vague_pay:{kw}",
                title=f"薪资表述里有「{kw}」",
                detail=why, evidence=sal_text or kw))
            break

    # ---------- 中国本土：试用期 ----------
    # 《劳动合同法》第十九条：合同期限三个月以上不满一年的，试用期不得超过一个月；
    # 一年以上不满三年的，不得超过二个月；三年以上及固定期限无固定期限的，不得超过六个月。
    m = re.search(r"试用期\s*([0-9一二三四五六七八九十]+)\s*个月", blob)
    if m:
        months = _cn_num(m.group(1))
        if months and months > 6:
            r.signals.append(TrustSignal(
                level="bad", code="probation_too_long",
                title=f"试用期写了 {months} 个月，超过 6 个月上限",
                detail="按《劳动合同法》第十九条，试用期最长不得超过六个月。"
                       "超过的部分你可以要求写在合同里，或直接不投。",
                evidence=m.group(0)))

    # 试用期打折：不得低于约定工资的 80%
    m2 = re.search(r"试用期[^。；;]{0,20}?([0-9]{2,3})\s*%", blob)
    if m2:
        pct = int(m2.group(1))
        if pct < 80:
            r.signals.append(TrustSignal(
                level="bad", code="probation_pay_too_low",
                title=f"试用期只发 {pct}% 工资",
                detail="《劳动合同法》第二十条：试用期工资不得低于本单位"
                       "相同岗位最低档工资或者劳动合同约定工资的 80%。"
                       "低于这个数是违法的，可以要求补。",
                evidence=m2.group(0)))

    # ---------- 竞业限制（应届生最容易忽略） ----------
    if "竞业" in blob:
        r.signals.append(TrustSignal(
            level="warn", code="non_compete",
            title="提到了竞业限制",
            detail="竞业期内不能去同行，期限最长 2 年，公司需按月支付补偿金"
                   "（一般不低于离职前平均工资的 30%）。应届生第一次入职就要签，"
                   "看清楚补偿怎么算、有没有上限。",
            evidence=next((l.strip() for l in jd.splitlines() if "竞业" in l), "竞业")))

    # ---------- 996 / 大小周 ----------
    for kw in ("996", "大小周", "早九晚九", "007"):
        if kw in blob:
            r.signals.append(TrustSignal(
                level="info", code=f"overtime:{kw}",
                title=f"提到了「{kw}」",
                detail="面试时可以直接问：有没有加班费？调休怎么安排？能不能落实？",
                evidence=kw))
            break

    # ---------- 五险一金 ----------
    # 对实习生和应届生，五险一金有时能占到总包相当一部分，
    # 面试时问清楚比事后发现强。
    if not any(k in blob for k in ("五险一金", "社保", "公积金", "六险一金")):
        r.signals.append(TrustSignal(
            level="info", code="no_benefit_mention",
            title="没提五险一金",
            detail="不一定是没交，但值得问一句：社保公积金按多少基数交？"
                   "实习期一般不交，正职要看清楚。",
            evidence=""))

    # ---------- 岗位真实性（幽灵岗）----------
    # 只用**我们自己采到的数据**，不联网查公司背景 ——
    # 理由见文件头原则 3：拿不出证据就不报。
    days = _days_since(getattr(job, "first_seen_at", ""))
    if days is not None and days >= 30:
        r.signals.append(TrustSignal(
            level="info", code="long_listed",
            title=f"这个岗位已经挂了 {days} 天",
            detail="挂了一个月以上还没下架，可能是长期招人，也可能只是没人管。"
                   "投之前值得问一句这个岗位目前还在招吗。",
            evidence=str(getattr(job, "first_seen_at", ""))[:10]))

    # 模板/聚合话术：同时出现「急聘」和「立即到岗」这类，常见于批量发布的岗位
    if any(k in blob for k in ("急聘", "立即到岗", "立即面试", "随时到岗")):
        r.signals.append(TrustSignal(
            level="info", code="urgent_template",
            title="有「急聘/立即到岗」这类话",
            detail="这类表述在批量发布的模板岗上很常见，本身不代表有问题，"
                   "但配合薪资区间特别宽时要多留个心眼。",
            evidence=next((k for k in ("急聘", "立即到岗", "立即面试", "随时到岗")
                           if k in blob), "")))

    # 汇总真实性判断
    ghost_codes = {s.code for s in r.signals}
    n_ghost = sum(1 for c in ghost_codes
                  if c in ("wide_salary_range", "long_listed", "urgent_template",
                           "vague_pay:面议"))
    if n_ghost >= 2:
        r.ghost_risk = "suspect"
        r.ghost_reason = ("这个岗位同时出现了多个「岗位可能不太活跃」的信号"
                          "（区间过宽 / 挂了很久 / 话术模板化 / 薪资写面议）。"
                          "不一定是假的，但值得你在投之前先问一句。")
    elif n_ghost == 1:
        r.ghost_risk = "none"

    return r


def _has_field(job, name: str) -> bool:
    v = getattr(job, name, None)
    return v not in (None, "", [], {})


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(s: str) -> Optional[int]:
    s = str(s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    if s.startswith("十"):          # 十二
        return 10 + _CN_DIGITS.get(s[1:], 0)
    if len(s) == 3 and s[1] == "十":  # 二十三
        return _CN_DIGITS.get(s[0], 0) * 10 + _CN_DIGITS.get(s[2], 0)
    return None
