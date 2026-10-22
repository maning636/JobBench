# -*- coding: utf-8 -*-
"""
求职材料的编排层 —— Web 和 CLI 共用
==================================

和 `AnalysisService` 一个路数：缓存键 =（岗位指纹 + 简历哈希 + 材料类型）。
同一份简历同一个岗位，第二次点直接命中，不再烧额度。

**为什么这里也要缓存**：面试准备和简历定制每次都是一次完整 LLM 调用
（7~11 秒 + 一次额度）。用户很可能反复点、反复看。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from jobforge.adapters.llm.career_materials import (
    InterviewPrepGenerator, ResumeTailorGenerator,
)
from jobforge.adapters.llm.client import LLMClient
from jobforge.adapters.llm.config import LLMConfig
from jobforge.domain.candidate import Candidate
from jobforge.domain.career_materials import InterviewPrep, TailorReport
from jobforge.domain.models import Job
from jobforge.persistence.sqlite_store import Store


class CareerMaterialService:
    def __init__(self, store: Store, candidate: Optional[Candidate] = None,
                 llm: Optional[LLMConfig] = None):
        self.store = store
        self.candidate = candidate or Candidate()
        self.llm_cfg = llm if llm is not None else LLMConfig.load()
        self.client = LLMClient(self.llm_cfg, retries=1)
        self.prep_gen = InterviewPrepGenerator(self.client)
        self.tailor_gen = ResumeTailorGenerator(self.client)

    # ------------------------------------------------------------------

    @property
    def llm_enabled(self) -> bool:
        return self.client.available()

    def _cached(self, job: Job, kind: str) -> Optional[Dict[str, Any]]:
        ch = self.store.candidate_hash(
            self.candidate.resume_text, self.candidate.skill_names)
        return self.store.get_semantic(f"{job.fingerprint}::{kind}", ch)

    def _put(self, job: Job, kind: str, payload: Dict[str, Any]) -> None:
        ch = self.store.candidate_hash(
            self.candidate.resume_text, self.candidate.skill_names)
        self.store.put_semantic(f"{job.fingerprint}::{kind}", ch,
                                payload, self.llm_cfg.model)

    # ------------------------------------------------------------------

    def interview_prep(self, job: Job,
                       resume_text: Optional[str] = None) -> InterviewPrep:
        cached = self._cached(job, "interview_prep")
        if cached:
            out = _rebuild_prep(cached)
            out.from_cache = True
            out.model = self.llm_cfg.model
            return out

        out = self.prep_gen.build(job, self.candidate, resume_text)
        if out.available:
            self._put(job, "interview_prep", out.to_dict())
        return out

    def tailor(self, job: Job,
               resume_text: Optional[str] = None) -> TailorReport:
        cached = self._cached(job, "tailor")
        if cached:
            out = _rebuild_tailor(cached)
            out.from_cache = True
            out.model = self.llm_cfg.model
            return out

        out = self.tailor_gen.build(job, self.candidate, resume_text)
        if out.available:
            self._put(job, "tailor", out.to_dict())
        return out


def _rebuild_prep(d: Dict[str, Any]) -> InterviewPrep:
    from jobforge.domain.career_materials import PrepQuestion
    out = InterviewPrep(available=bool(d.get("available", True)),
                        error=str(d.get("error") or ""),
                        elapsed=float(d.get("elapsed") or 0.0),
                        model=str(d.get("model") or ""))
    for q in d.get("questions") or []:
        out.questions.append(PrepQuestion(
            question=str(q.get("question") or ""),
            points=list(q.get("points") or []),
            evidence_resume=str(q.get("evidence_resume") or ""),
            why=str(q.get("why") or ""),
            fallback=str(q.get("fallback") or ""),
        ))
    out.gaps = list(d.get("gaps") or [])
    out.ask_back = list(d.get("ask_back") or [])
    out.checklist = list(d.get("checklist") or [])
    return out


def _rebuild_tailor(d: Dict[str, Any]) -> TailorReport:
    out = TailorReport(available=bool(d.get("available", True)),
                       error=str(d.get("error") or ""),
                       elapsed=float(d.get("elapsed") or 0.0),
                       model=str(d.get("model") or ""))
    out.ats_hit = list(d.get("ats_hit") or [])
    out.ats_missing = list(d.get("ats_missing") or [])
    out.cannot_fake = list(d.get("cannot_fake") or [])
    for r in d.get("rewrite") or []:
        out.rewrite.append({
            "original": str(r.get("original") or ""),
            "suggested": str(r.get("suggested") or ""),
            "reason": str(r.get("reason") or ""),
        })
    return out
