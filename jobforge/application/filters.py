# -*- coding: utf-8 -*-
"""
求职工作台 · 硬性条件筛选 + 数据健康看门狗
===========================================

产品核心不是"搜索"，是"筛选"。
v1.2.0 的五维评分有三套互不兼容的口径（1-5 分 / 百分制 / 🟢 四色），
且没有任何代码实现。v1.2.0 还把评分当成纯 Markdown 规范交给 LLM 自由发挥，
于是同一份 AGENTS.md 里写着"评分 1-5 分制"，FEATURES.md 写着"百分制 80-100"，
CLI 里又是 🟢≥75 —— 运行时会产出互相矛盾的结论。

这里做两件事：
1. `HardFilter` —— 纯规则、零风险、可解释。校招生最需要的是硬性条件过滤：
   城市、薪资下限、学历、应届身份、公司规模。先把不符合的筛掉。
2. `DataGate` —— 数据健康看门狗。不健康就不推送，而不是静默推一堆空岗位。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from jobforge.domain.models import Job, SalaryState


# =============================================================================
# 硬性条件筛选
# =============================================================================

_EDU_RANK = {"不限": 0, "": 0, "高中": 1, "中专": 1, "大专": 2, "专科": 2,
             "本科": 3, "硕士": 4, "研究生": 4, "博士": 5}


def edu_rank(text: str) -> int:
    return _EDU_RANK.get((text or "").strip(), 0)


@dataclass
class HardFilter:
    """硬性条件。命中即淘汰，且必须能说出淘汰原因。"""
    cities: List[str] = field(default_factory=list)          # 空 = 不限
    salary_min: Optional[int] = None                          # 元/月
    edu_at_least: str = ""                                   # 至少本科学历
    require_fresh_grad: bool = False                          # 只看应届/在校/经验不限
    exclude_outsourcing: bool = True                          # 排除外包（证据明确时）
    company_size_max: Optional[int] = None                    # 公司规模上限（人）
    keywords_include: List[str] = field(default_factory=list)
    keywords_exclude: List[str] = field(default_factory=list)
    hide_incomplete: bool = True                              # 完整率不达标的不给用户看

    def evaluate(self, job: Job) -> Dict[str, Any]:
        """返回 {pass, reasons, warnings}。reasons 是淘汰理由（给人看的）。"""
        reasons: List[str] = []
        warnings: List[str] = []

        if self.cities and job.city not in self.cities:
            reasons.append(f"城市不符（{job.city or '未知'}）")

        if self.salary_min is not None:
            if not job.is_salary_trustworthy():
                warnings.append("薪资未解析，无法判断是否达标")
            elif job.salary_period and job.salary_period != "月":
                # 实习日薪 / 年薪不能和月薪门槛直接比 —— 明确提示而不是误判
                warnings.append(f"薪资周期为「{job.salary_period}」，未与月薪门槛比较")
            elif job.salary_max is not None and job.salary_max < self.salary_min:
                reasons.append(f"薪资上限 {job.salary_max} 低于要求 {self.salary_min}")

        if self.edu_at_least:
            need = edu_rank(self.edu_at_least)
            if job.education and edu_rank(job.education) < need:
                reasons.append(f"学历 {job.education or '未知'} 低于 {self.edu_at_least}")

        if self.require_fresh_grad:
            text = f"{job.experience} {job.employment_type} {job.campus_batch}"
            fresh = any(k in text for k in ("应届", "在校", "经验不限", "不限经验", "校招"))
            if not fresh and job.experience not in ("", "不限"):
                reasons.append(f"要求应届/不限经验，当前为「{job.experience}」")

        if self.exclude_outsourcing and job.is_outsourcing_risk is True:
            ev = job.outsourcing_evidence[0] if job.outsourcing_evidence else ""
            reasons.append(f"疑似外包/派遣岗位{('　' + ev) if ev else ''}")

        if self.company_size_max is not None:
            m = re.search(r"(\d+)\s*-\s*(\d+)\s*人", job.company_size or "")
            if m and int(m.group(2)) > self.company_size_max:
                reasons.append(f"公司规模 {job.company_size} 超过上限")

        blob = f"{job.position} {job.company} {job.jd[:500]} {' '.join(job.tags)}"
        if self.keywords_include and not any(k in blob for k in self.keywords_include):
            reasons.append("不包含任何目标关键词")
        if self.keywords_exclude:
            for k in self.keywords_exclude:
                if k in blob:
                    reasons.append(f"命中排除词「{k}」")

        if self.hide_incomplete and job.completeness() < 0.6:
            missing = job.missing_required()
            if missing:
                reasons.append("数据不完整：" + "、".join(missing))

        return {"pass": not reasons, "reasons": reasons, "warnings": warnings}

    def apply(self, jobs: List[Job]) -> Dict[str, Any]:
        kept, dropped = [], []
        for j in jobs:
            r = self.evaluate(j)
            (kept if r["pass"] else dropped).append({"job": j, **r})
        return {
            "kept": kept,
            "dropped": dropped,
            "total": len(jobs),
            "kept_count": len(kept),
            "dropped_count": len(dropped),
        }


# =============================================================================
# 数据健康看门狗
# =============================================================================

@dataclass
class DataGate:
    """推送前的最后一道闸。

    v1.2.0 的随包演示数据：15 条岗位，salary 全部为空，company 存的是
    "5-10年"这种经验值，而文档里写着"找到 15 个岗位"。
    每天自动推送的产品不能这样 —— 错一次，用户就慢慢不信了。
    """
    min_completeness: float = 0.6      # 加权完整率下限
    max_incomplete_ratio: float = 0.2  # 必填字段缺失占比上限
    min_new_jobs: int = 1              # 当天至少新增几条才算"有货"
    min_salary_resolved_ratio: float = 0.5  # 薪资可解析占比下限

    def check(self, report, jobs: List[Job]) -> Dict[str, Any]:
        problems: List[str] = []

        comp = report.completeness
        if comp < self.min_completeness:
            problems.append(f"完整率 {comp:.0%} < {self.min_completeness:.0%}")

        if report.received:
            inc_ratio = report.incomplete / report.received
            if inc_ratio > self.max_incomplete_ratio:
                problems.append(
                    f"必填字段缺失率 {inc_ratio:.0%} > {self.max_incomplete_ratio:.0%}")

        if report.inserted < self.min_new_jobs:
            problems.append(f"新增岗位 {report.inserted} 条，低于下限 {self.min_new_jobs}")

        if jobs:
            resolved = sum(
                1 for j in jobs if j.salary_state in
                (SalaryState.VERIFIED.value, SalaryState.HEURISTIC.value)
            )
            ratio = resolved / len(jobs)
            if ratio < self.min_salary_resolved_ratio:
                problems.append(
                    f"薪资可解析率 {ratio:.0%} < {self.min_salary_resolved_ratio:.0%}")

        return {
            "pass": not problems,
            "problems": problems,
            "completeness": comp,
            "salary_resolved_ratio": (
                sum(1 for j in jobs if j.salary_state in
                    (SalaryState.VERIFIED.value, SalaryState.HEURISTIC.value))
                / len(jobs) if jobs else 0.0
            ),
            "sample_size": len(jobs),
        }
