# -*- coding: utf-8 -*-
"""
面试准备 / 简历定制的 LLM 生成层
================================

**这里最大的风险不是模型答得不好，是模型在教用户撒谎。**

  · 面试准备编一个「你熟悉 X」→ 面试官追问两句就穿帮，比不准备更糟
  · ATS 对齐本质是教人往简历里塞关键词 → 不设防就变成「帮你编」的机器

所以这里的每一条**来自简历**的内容，都必须能一字不改地在简历原文里找到。
校验复用 `SemanticAnalyzer` 里那道最硬的闸门（`_quote_in`），
不另起炉灶 —— 约束只有一处实现，才不会哪天只改了一边。

**四道闸门（可靠性从低到高）：**
  1. 字段完整
  2. 要点长度达标
  3. 模型自认「无证据」的措辞
  4. 摘录真的能在简历原文里找到   ← 无法绕过
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from jobforge.adapters.llm.client import LLMClient
from jobforge.adapters.llm.semantic import SemanticAnalyzer
from jobforge.domain.candidate import Candidate
from jobforge.domain.career_materials import (
    InterviewPrep, MaterialError, PrepQuestion, TailorReport,
)
from jobforge.domain.models import Job


# =============================================================================
# 面试准备
# =============================================================================

INTERVIEW_SYSTEM = """你是资深技术面试官，正在帮一位求职者准备即将到来的面试。

铁律，违反任何一条这条输出就会被程序丢弃：
1. 只依据给定的简历和 JD 文本。**绝对不要编造简历里没有的经历。**
2. `evidence_resume` 必须是简历里**一字不改的连续摘录**。
   程序会拿它和原文逐字比对，对不上整条丢弃。不要改写、不要拼接。
3. `points` 里的每一条都必须在 `evidence_resume` 这段摘录里有依据。
   如果某个问题你无法从简历里找到支撑，就不要把它放进 questions，
   放进 gaps —— "我没做过这个"也是有用的准备。
4. `fallback` 写的是**答不上来时怎么诚实地应对**，
   不是"怎么圆过去"。绝对不要出现"可以适当夸大""可以说自己了解"这类措辞。
5. 问题要具体。"你的项目经历"这种废话不要写，要写清楚会被问到哪个项目、哪个技术点。

只输出 JSON，格式：
{
  "questions": [
    {
      "question": "会被问到的问题",
      "points": ["回答要点，每条都要有简历依据"],
      "evidence_resume": "简历原文逐字摘录",
      "why": "这个问题在考察什么",
      "fallback": "答不上来时怎么诚实应对"
    }
  ],
  "gaps": ["JD 要但简历没有的，可能被追问的点"],
  "ask_back": ["该反问面试官的问题"],
  "checklist": ["面试前要准备好的东西，如作品、代码、项目数据"]
}"""


class InterviewPrepGenerator:
    """生成一场面试的准备材料。"""

    def __init__(self, client: LLMClient):
        self.client = client

    @property
    def available(self) -> bool:
        return self.client.available()

    def build(self, job: Job, cand: Candidate,
              resume_text: Optional[str] = None) -> InterviewPrep:
        out = InterviewPrep(model=self.client.cfg.model)
        src = resume_text or cand.resume_text

        # 顺序要紧：**先查简历，再查模型**。
        # 实测踩过：没有简历的用户先看到「还没接模型服务」，
        # 去设置里填完 Key 回来，还是这句 —— 白折腾一趟。
        # 没有简历是更根本的阻塞，必须先说。
        if not src or not src.strip():
            out.error = "还没有简历，先到「简历」页粘贴一份"
            return out
        if not self.available:
            out.error = "还没接模型服务，先到「设置」里填一下"
            return out

        t0 = time.time()
        user = (f"【简历】\n{src}\n\n【JD】\n{job.jd or job.position or ''}\n\n"
                f"【岗位】{job.position} @ {job.company}")
        r = self.client.chat_json(INTERVIEW_SYSTEM, user)
        out.elapsed = time.time() - t0
        if not r.ok:
            out.error = r.error or "模型没返回结果"
            return out

        try:
            data = r.data if isinstance(r.data, dict) else json.loads(r.data)
        except (json.JSONDecodeError, TypeError):
            out.error = "模型返回的不是合法 JSON"
            return out

        out = self._sanitize(out, data, src)
        out.available = True
        return out

    # ------------------------------------------------------------------

    MIN_EVIDENCE_LEN = 8
    NO_EVIDENCE_MARKERS = SemanticAnalyzer.NO_EVIDENCE_MARKERS

    # 模型一旦写出这些，就是在教用户撒谎 —— 整条丢掉，不降级
    LIE_MARKERS = (
        "适当夸大", "可以夸大", "夸大一点", "可以说自己了解", "假装",
        "包装一下", "编一个", "虚构", "适当美化", "先斩后奏",
    )

    @classmethod
    def _sanitize(cls, out: InterviewPrep, data: Dict[str, Any],
                  resume_src: str) -> InterviewPrep:
        """过滤不合规输出 —— 面试材料是最后一道幻觉闸门。"""
        for q in data.get("questions") or []:
            if not isinstance(q, dict):
                continue
            question = str(q.get("question") or "").strip()
            if not question:
                continue

            ev = str(q.get("evidence_resume") or "").strip()
            # 闸门 2：证据太短等于没有
            if len(ev) < cls.MIN_EVIDENCE_LEN:
                continue
            # 闸门 3：模型自己都承认没有证据
            if any(w in ev for w in cls.NO_EVIDENCE_MARKERS):
                continue
            # 教用户撒谎的措辞 —— 不降级，直接丢。
            # ⚠ 检查范围必须覆盖**每一个**字段：实测踩过，
            # 最初只查了 question/evidence/points，漏了 fallback，
            # 结果「如果不会可以说适当夸大」这种话就整条漏了过去 ——
            # 而诱导性话术恰恰最爱写在 fallback 里（"答不上来怎么办"）。
            blob_all = " ".join([
                question, ev, str(q.get("why") or ""), str(q.get("fallback") or ""),
                *map(str, q.get("points") or []),
            ])
            if any(w in blob_all for w in cls.LIE_MARKERS):
                continue
            # 闸门 4：摘录必须真在简历原文里 —— 最强，无法绕过
            if not SemanticAnalyzer._quote_is_real(ev, resume_src):
                continue

            points = [str(p).strip() for p in (q.get("points") or []) if str(p).strip()]
            if not points:
                continue

            out.questions.append(PrepQuestion(
                question=question,
                points=points[:6],
                evidence_resume=ev,
                why=str(q.get("why") or "").strip()[:160],
                fallback=str(q.get("fallback") or "").strip()[:160],
            ))

        out.gaps = _clean_list(data.get("gaps"), 8)
        out.ask_back = _clean_list(data.get("ask_back"), 6)
        out.checklist = _clean_list(data.get("checklist"), 6)
        return out


# =============================================================================
# 简历定制 / ATS 对齐
# =============================================================================

TAILOR_SYSTEM = """你是资深招聘顾问，正在帮一位求职者把**已有的简历**对齐到某一个岗位。

铁律，违反任何一条这条输出就会被程序丢弃：
1. **绝对不要编造简历里没有的经历。** 这是最重要的约束。
2. `rewrite` 只能做两件事：
   a) 把简历里**已经写过**的某段经历，换一种说法或换个顺序来呈现；
   b) 把简历里已经有的技能名称，换成 JD 里用的同义说法。
   你**不能**新增任何简历里不存在的事实、项目、数字、职责。
   每条 rewrite 的 `original` 必须是简历原文的逐字摘录，程序会逐字比对。
3. JD 要但简历没有的关键词，放进 `ats_missing` 和 `cannot_fake`，
   **不要**写进 rewrite。你可以在 rewrite 里建议"补一段真实经历"，
   但要明确标注那是"去做"而不是"现在就能写上去"。
4. `ats_hit` 和 `ats_missing` 必须是单个技术名词，不要整句。

只输出 JSON，格式：
{
  "ats_hit": ["简历里已有且岗位要的关键词"],
  "ats_missing": ["岗位要但简历里没有的关键词"],
  "rewrite": [
    {
      "original": "简历原文逐字摘录",
      "suggested": "改写后的说法，不得包含原文没有的事实",
      "reason": "为什么要改， ATS 或 HR 在看什么"
    }
  ],
  "cannot_fake": ["你确实不具备、但岗位点名要求的东西 —— 要老实补，别硬写"]
}"""


class ResumeTailorGenerator:
    """针对单个岗位做简历定制 / ATS 对齐。

    **它不会替你写任何你没做过的事。** 它的价值是把「你缺什么」说清楚。
    """

    def __init__(self, client: LLMClient):
        self.client = client

    @property
    def available(self) -> bool:
        return self.client.available()

    def build(self, job: Job, cand: Candidate,
              resume_text: Optional[str] = None) -> TailorReport:
        out = TailorReport(model=self.client.cfg.model)
        src = resume_text or cand.resume_text

        # 顺序同上：先说更根本的那个阻塞
        if not src or not src.strip():
            out.error = "还没有简历，先到「简历」页粘贴一份"
            return out
        if not self.available:
            out.error = "还没接模型服务，先到「设置」里填一下"
            return out

        t0 = time.time()
        user = (f"【简历】\n{src}\n\n【JD】\n{job.jd or job.position or ''}\n\n"
                f"【岗位】{job.position} @ {job.company}")
        r = self.client.chat_json(TAILOR_SYSTEM, user)
        out.elapsed = time.time() - t0
        if not r.ok:
            out.error = r.error or "模型没返回结果"
            return out

        try:
            data = r.data if isinstance(r.data, dict) else json.loads(r.data)
        except (json.JSONDecodeError, TypeError):
            out.error = "模型返回的不是合法 JSON"
            return out

        out = self._sanitize(out, data, src)
        out.available = True
        return out

    @classmethod
    def _sanitize(cls, out: TailorReport, data: Dict[str, Any],
                  resume_src: str) -> TailorReport:
        # ── 实测踩过的坑 ──────────────────────────────────────
        # 模型会把「日常业务支持」「交付」这种 JD 套话塞进 ats_missing。
        # 它们不是技术关键词，进了分母就会让覆盖率失真 ——
        # 实测一次真实调用里，7 个"缺失"里有 2 个是这种垃圾，
        # 报出来的 78% ATS 覆盖率是错的。
        # ATS 系统只匹配技术名词，套话本来就不参与匹配，
        # 留着只会让人以为「我差的就是这个」。
        # ───────────────────────────────────────────────────
        out.ats_hit = _clean_list(data.get("ats_hit"), 15)
        out.ats_missing = _clean_list(data.get("ats_missing"), 15)
        out.ats_hit = [k for k in out.ats_hit if _is_tech_keyword(k)]
        out.ats_missing = [k for k in out.ats_missing if _is_tech_keyword(k)]

        # cannot_fake 是给人看的，可以保留套话 —— 它是提醒不是统计量
        out.cannot_fake = _clean_list(data.get("cannot_fake"), 10)

        for item in data.get("rewrite") or []:
            if not isinstance(item, dict):
                continue
            original = str(item.get("original") or "").strip()
            suggested = str(item.get("suggested") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if not original or not suggested:
                continue
            if len(original) < 6:
                continue
            # 闸门：原文摘录必须真的在简历里
            if not SemanticAnalyzer._quote_is_real(original, resume_src):
                continue

            # ── 本模块最关键的一道 ────────────────────────────────
            # suggested 里出现了原文没有的技术名词 = 模型在教你编。
            # 改写措辞可以，但凭空多出「Kubernetes」不行。
            # ─────────────────────────────────────────────────
            if not _is_reframing(suggested, resume_src):
                continue

            out.rewrite.append({
                "original": original,
                "suggested": suggested[:220],
                "reason": reason[:160],
            })
            if len(out.rewrite) >= 6:
                break
        return out


# =============================================================================
# 共用工具
# =============================================================================

# 技术名词词典：只收"写进去就等于声称自己会"的词。
# 普通中文虚词不算 —— 否则"熟悉""掌握"这种高频词会误杀所有改写建议。
_TECH_TERMS = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]{1,20}"          # 英文技术词
    r"|微服务|高并发|分布式|容器化|云原生|大数据|机器学习|深度学习"
    r"|算法|缓存|消息队列|索引|事务|集群|监控|链路追踪|性能优化"
)


def _tech_terms(text: str) -> set:
    """抽出文本里的"技术名词"集合，用于比对改写有没有凭空新增。"""
    stop = {
        "the", "and", "for", "with", "that", "this", "you", "your",
        "a", "an", "of", "to", "in", "on", "at", "it", "is", "as", "be",
        "will", "have", "has", "can", "not", "but", "our", "we",
        "使用", "进行", "负责", "参与", "完成", "开发", "项目", "团队", "公司",
    }
    out = set()
    for m in _TECH_TERMS.finditer(str(text or "")):
        t = m.group(0)
        if t.lower() in stop or len(t) < 2:
            continue
        out.add(t.lower())
    return out


def _is_tech_keyword(text: str) -> bool:
    """判断一个词算不算"ATS 会去匹配的技术关键词"。

    规则：必须含一个英文技术词，或本身就是中文技术词。
    「日常业务支持」「交付」「抗压能力强」这类 JD 套话一律不算 ——
    它们进了 ATS 分母只会让覆盖率失真（实测踩过）。
    """
    s = str(text or "").strip()
    if not s or len(s) > 24:
        return False
    # 英文技术词：MySQL / Spring Boot / k8s / C++ / Node.js
    if re.search(r"[A-Za-z][A-Za-z0-9+.\-]{1,19}", s):
        return True
    # 中文技术词：微服务 / 高并发 / 算法 / 容器化 …
    return any(w in s for w in (
        "微服务", "高并发", "分布式", "容器", "云原生", "大数据",
        "机器学习", "深度学习", "算法", "缓存", "消息队列", "索引",
        "事务", "集群", "监控", "链路追踪", "性能优化", "数据库",
        "前端", "后端", "测试", "运维", "架构", "接口", "并发",
    ))


def _is_reframing(suggested: str, resume_src: str) -> bool:
    """允许"重排"和"同义替换"这两类合法改写。

    判据：suggested 里每一个技术名词，要么在简历里出现过，
    要么是**描述性短语**（"负责过"、"参与"这类）而不是一个新技能名。
    真正危险的信号是：出现了一个全新的、简历里从来没有的框架/工具名。
    """
    new = _tech_terms(suggested) - _tech_terms(resume_src)
    for t in new:
        # 纯数字/单位/英文虚词不算技能
        if re.fullmatch(r"[\d.%]+", t):
            continue
        # 描述性连接词
        if t in {"负责", "参与", "完成", "开发", "设计", "搭建", "优化"}:
            continue
        return False   # 出现了一个简历里从没有过的新名词 → 判为编造
    return True


def _clean_list(value: Any, limit: int) -> List[str]:
    """把模型返回的列表收拾干净：去空、去重、限长。"""
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for v in value:
        s = str(v).strip()
        if not s or len(s) > 120:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out
