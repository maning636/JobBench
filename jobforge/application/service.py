# -*- coding: utf-8 -*-
"""
求职工作台 · 分析编排
======================

CLI 和 Web 之前会各写一套分析流程,必然漂移。这里抽成唯一实现，
两边都调它 —— 和 v1.2.0「同一个字段四套定义」是同一个教训。

职责：
  1. 规则引擎分析（永远执行，免费且可核对）
  2. LLM 语义分析（可选，按「岗位指纹 + 简历哈希」缓存）
  3. 合并输出
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from jobforge.adapters.llm.client import LLMClient
from jobforge.adapters.llm.config import LLMConfig
from jobforge.adapters.llm.semantic import SemanticAnalyzer, SemanticResult
from jobforge.application.analyzer import (
    CompareAnalyzer, MatchAnalyzer, SalaryAdvisor,
)
from jobforge.domain.candidate import Candidate
from jobforge.domain.models import Job


@dataclass
class AnalyzedJob:
    job: Job
    report: Any            # MatchReport
    semantic: Optional[SemanticResult] = None
    from_cache: bool = False
    elapsed: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": getattr(self.job, "db_id", None),
            "position": self.job.position,
            "company": self.job.company,
            "salary_text": self.job.salary_text,
            "city": self.job.city,
            "match_score": self.report.match_score,
            "verdict": self.report.verdict,
            "summary": self.report.summary,
            "hit_count": len(self.report.covered),
            "missing_count": (len(self.report.missing_hard)
                              + len(self.report.missing_normal)),
            "report": self.report.to_dict(),
            "semantic": self.semantic.to_dict() if self.semantic else None,
            "from_cache": self.from_cache,
            "elapsed": round(self.elapsed, 1),
        }


class AnalysisService:
    """分析编排。Web 和 CLI 共用。"""

    def __init__(self, store, candidate: Optional[Candidate] = None,
                 llm: Optional[LLMConfig] = None):
        self.store = store
        self.candidate = candidate or Candidate()
        self.llm_cfg = llm if llm is not None else LLMConfig.load()
        self.client = LLMClient(self.llm_cfg, retries=1)
        self.sem = SemanticAnalyzer(self.client)
        self.matcher = MatchAnalyzer(self.candidate)
        self.comparer = CompareAnalyzer(self.candidate)
        self.salary = SalaryAdvisor(self.candidate)

    # ------------------------------------------------------------------

    @property
    def llm_enabled(self) -> bool:
        return self.client.available()

    def candidate_hash(self) -> str:
        return self.store.candidate_hash(
            self.candidate.resume_text, self.candidate.skill_names)

    def has_profile(self) -> bool:
        return bool(self.candidate.resume_text or self.candidate.skill_names)

    # ------------------------------------------------------------------

    def analyze_one(self, job: Job, use_llm: bool = True) -> AnalyzedJob:
        t0 = time.time()
        rep = self.matcher.analyze(job)

        sem = None
        if use_llm and self.llm_enabled and self.has_profile():
            ch = self.candidate_hash()
            cached = self.store.get_semantic(job.fingerprint, ch)
            if cached:
                sem = SemanticResult(**{k: v for k, v in cached.items()
                                        if k in SemanticResult.__dataclass_fields__})
                sem.available = True
            else:
                sem = self.sem.analyze(job, self.candidate)
                if sem.available:
                    self.store.put_semantic(
                        job.fingerprint, ch, sem.to_dict(), self.llm_cfg.model)
                return AnalyzedJob(job=job, report=rep, semantic=sem,
                                   from_cache=False,
                                   elapsed=time.time() - t0)
            return AnalyzedJob(job=job, report=rep, semantic=sem,
                               from_cache=True, elapsed=time.time() - t0)

        if use_llm and not self.llm_enabled:
            sem = SemanticResult(error="未配置 LLM，已跳过语义分析")

        return AnalyzedJob(job=job, report=rep, semantic=sem,
                           elapsed=time.time() - t0)

    # ------------------------------------------------------------------

    def analyze_many(self, jobs: List[Job], use_llm: bool = True,
                     progress: Optional[Callable[[int, int, AnalyzedJob], None]] = None,
                     stop_on_error: bool = True) -> List[AnalyzedJob]:
        out: List[AnalyzedJob] = []
        total = len(jobs)
        for i, j in enumerate(jobs, 1):
            try:
                a = self.analyze_one(j, use_llm=use_llm)
            except KeyboardInterrupt:
                if out:
                    return out
                raise
            except Exception as e:  # noqa: BLE001
                if stop_on_error:
                    raise
                a = AnalyzedJob(job=j, report=MatchAnalyzer(self.candidate).analyze(j),
                                semantic=SemanticResult(error=str(e)))
            out.append(a)
            if progress:
                progress(i, total, a)
        return out

    # ------------------------------------------------------------------

    def compare(self, analyzed: List[AnalyzedJob]) -> Dict[str, Any]:
        rows = self.comparer.compare(
            [a.job for a in analyzed], [a.report for a in analyzed])
        return {
            "rows": [r.to_dict() for r in rows],
            "explain": self.comparer.explain(rows),
        }

    def salary_views(self, analyzed: List[AnalyzedJob]) -> Dict[str, Any]:
        jobs = [a.job for a in analyzed]
        return {str(getattr(j, "db_id", i)): self.salary.view(j, jobs).to_dict()
                for i, (a, j) in enumerate(zip(analyzed, jobs))}


# =============================================================================
# 报告导出
# =============================================================================

def render_markdown(analyzed: List[AnalyzedJob], candidate: Candidate,
                    compare: Dict[str, Any], health: Optional[Dict] = None) -> str:
    """生成一份人能读的 Markdown 报告。"""
    L: List[str] = []
    L.append("# 求职工作台分析报告\n")
    L.append(f"- 候选人：{candidate.name or '未填'}")
    L.append(f"- 识别能力：{len(candidate.skill_names)} 项"
             f"（{'、'.join(candidate.skill_names[:12])}）")
    L.append(f"- 学历：{candidate.education or '未识别'}　"
             f"应届：{'是' if candidate.is_fresh_grad else '否'}")
    if candidate.salary_expect:
        L.append(f"- 期望薪资：{candidate.salary_expect}")
    if health:
        L.append(f"- 数据健康：{'通过' if health.get('pass') else '未通过'}"
                 f"（完整率 {round((health.get('completeness') or 0) * 100)}%）")
    L.append(f"- 分析岗位数：{len(analyzed)}\n")

    if compare.get("explain"):
        L.append("## 结论先行\n")
        for e in compare["explain"]:
            L.append(f"- {e}")
        L.append("")

    rows = compare.get("rows") or []
    if rows:
        L.append("## 岗位排序\n")
        L.append("| # | 岗位 | 公司 | 薪资 | 匹配 | 结论 | 主要风险 |")
        L.append("|---|------|------|------|------|------|----------|")
        for i, r in enumerate(rows, 1):
            risk = "；".join(r.get("cons", [])[:2]) or "—"
            L.append(f"| {i} | {r['position']} | {r['company']} | "
                     f"{r['salary_text'] or '—'} | {r['match_score']:.0f}% | "
                     f"{r['verdict']} | {risk} |")
        L.append("")

    L.append("## 逐个岗位分析\n")
    for a in sorted(analyzed, key=lambda x: -x.report.match_score):
        r = a.report
        L.append(f"### {a.job.position}　@{a.job.company}\n")
        L.append(f"**匹配度 {r.match_score:.0f}%　{r.verdict}**　{r.summary}\n")
        if a.job.salary_text:
            L.append(f"- 薪资：{a.job.salary_text}")
        if a.job.city:
            L.append(f"- 城市：{a.job.city}")
        if r.covered:
            L.append(f"- ✓ 已具备：{'、'.join(m.skill for m in r.covered)}")
        if r.missing_hard:
            L.append(f"- ✗ **硬性缺失**：{'、'.join(m.skill for m in r.missing_hard)}")
        if r.missing_normal:
            L.append(f"- △ 一般缺失：{'、'.join(m.skill for m in r.missing_normal)}")
        if r.blockers:
            L.append("\n**投之前必须知道**")
            for b in r.blockers:
                L.append(f"- ⛔ {b}")
        if r.suggestions:
            L.append("\n**你该做什么**")
            for s in r.suggestions:
                L.append(f"- {s}")
        if r.notes:
            L.append("\n**说明**")
            for n in r.notes:
                L.append(f"- {n}")
        if a.semantic and a.semantic.available and a.semantic.missing:
            L.append("\n**模型补充的缺失项**")
            for m in a.semantic.missing:
                L.append(f"- {m.get('skill')}（{m.get('learn_cost', '?')}）")
        L.append("")

    return "\n".join(L)
