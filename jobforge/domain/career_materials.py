# -*- coding: utf-8 -*-
"""
求职材料域 —— 面试准备 + 简历定制/ATS 对齐
==================================================

**这两个能力是从 career-ops 那个 75k star 的项目上学来的，但不是照搬。**
它有 44 个 mode，我们只取其中两个真正补上了我们闭环断点的。

为什么是这两个：

  · 我们的五步闭环是 发现→评估→投递→跟进→复盘。
    断在 **③投递**：我们会告诉你"这个岗位 60% 匹配"，然后呢？
    你拿同一份简历去投每一个岗位，ATS 关键词命中率低；
    进了面试也没有任何准备材料。

  · career-ops 有 `pdf/ats/cover` 和 `interview-prep`，
    正好补的就是这一段。

**为什么不抄它的 1-5 分制**：我们 0-100% 的体系已经渗透进
规则引擎、分母陷阱防护和一百多条测试，动它风险远大于收益。

**为什么不抄 auto-fill application**：它自己写的是 "You press Submit"。
这条底线我们同样守住 —— 投递必须由人按下去。

────────────────────────────────────────────────────────────────────────
本模块的铁律（比防幻觉还硬）
────────────────────────────────────────────────────────────────────────

面试准备和简历定制有一个共同的致命风险：**教用户撒谎**。

  · 面试准备如果说「你可以说自己熟悉 Kubernetes」，
    而用户简历里根本没有 —— 面试官追问两句就穿帮，
    比不准备更糟。

  · ATS 对齐本质是在教人往简历里塞关键词。
    如果不设防，它会变成「帮你编」的机器。

所以这里的每一条**来自简历**的内容都必须能一字不改地在原文里找到，
和 `adapters/llm/semantic.py` 用的是同一道闸门（那道无法绕过）。
凡是模型编不出来的，宁可少给，也不给它蒙混过关的机会。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# =============================================================================
# 面试准备
# =============================================================================

@dataclass
class PrepQuestion:
    """一个预测会问到的问题。

    `evidence_resume` 是这一条能说的**唯一依据** ——
    它必须是简历原文的逐字摘录。程序会去原文里比对，对不上就整条丢掉。
    """
    question: str = ""
    # 回答要点 —— 同样必须能在简历原文里找到
    points: List[str] = field(default_factory=list)
    # 支撑上面要点的简历原文摘录（逐字）
    evidence_resume: str = ""
    # 这个问题背后 JD 在考察什么
    why: str = ""
    # 答不上来时怎么办（诚实兜底，不是教编）
    fallback: str = ""


@dataclass
class InterviewPrep:
    """一场面试的准备材料。"""
    available: bool = False
    error: str = ""
    elapsed: float = 0.0
    model: str = ""
    from_cache: bool = False

    questions: List[PrepQuestion] = field(default_factory=list)
    # 岗位要、但你简历里没有的 —— 这些面试官很可能追问，
    # 提前想好怎么诚实回答，而不是怎么圆
    gaps: List[str] = field(default_factory=list)
    # 该反问面试官什么
    ask_back: List[str] = field(default_factory=list)
    # 面试前要准备好的东西（作品、代码、成绩单…）
    checklist: List[str] = field(default_factory=list)

    @property
    def answerable(self) -> int:
        return len(self.questions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "model": self.model,
            "from_cache": self.from_cache,
            "answerable": self.answerable,
            "questions": [
                {
                    "question": q.question,
                    "points": q.points,
                    "why": q.why,
                    "fallback": q.fallback,
                    "evidence_resume": q.evidence_resume,
                }
                for q in self.questions
            ],
            "gaps": self.gaps,
            "ask_back": self.ask_back,
            "checklist": self.checklist,
        }


# =============================================================================
# 简历定制 / ATS 对齐
# =============================================================================

@dataclass
class TailorReport:
    """针对某一个岗位的简历定制报告。

    **这个对象的存在意义之一是告诉用户「哪些东西不能补」。**
    `cannot_fake` 是这个产品最有价值的部分，比 `rewrite` 更值钱。
    """
    available: bool = False
    error: str = ""
    elapsed: float = 0.0
    model: str = ""
    from_cache: bool = False

    # 简历里已经有、且 JD 要的关键词 —— 这就是 ATS 会匹配上的部分
    ats_hit: List[str] = field(default_factory=list)
    # JD 要但简历里没有的关键词。
    # ⚠ 这里只做提示，**绝不能自动往简历里塞** —— 见 cannot_fake
    ats_missing: List[str] = field(default_factory=list)
    # 改写建议：只能是「把简历里已有的某句话换个说法/换个顺序」
    rewrite: List[Dict[str, str]] = field(default_factory=list)
    # 明确告诉用户：这个真的没有，别硬写
    cannot_fake: List[str] = field(default_factory=list)

    @property
    def ats_ratio(self) -> float:
        """ATS 关键词覆盖率。分母为 0 时返回 0，不返回 100 ——
        岗位没写技术要求时"100%"是骗人的。"""
        total = len(self.ats_hit) + len(self.ats_missing)
        if not total:
            return 0.0
        return round(len(self.ats_hit) / total, 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "model": self.model,
            "from_cache": self.from_cache,
            "ats_hit": self.ats_hit,
            "ats_missing": self.ats_missing,
            "ats_ratio": self.ats_ratio,
            "rewrite": self.rewrite,
            "cannot_fake": self.cannot_fake,
        }


# =============================================================================
# 这两个对象共同的边界校验
# =============================================================================

class MaterialError(Exception):
    """材料生成失败。**必须抛出来，不许静默返回空壳** ——
    v1.2.0 最贵的教训就是"拿不到数据被静默吞掉，
    用户以为是今天没新岗位"。"""


def require_evidence(quote: str, source: str, what: str) -> None:
    """模型给的摘录必须在原文里一字不差地出现，否则报错。

    正常情况下这道检查在 `_sanitize` 里逐条过滤就够了；
    这里给的是**更外层**的用法：调用方需要知道"这个模型完全不可信"时，
    可以直接拿它当异常抛出，而不是给用户一个空面板。
    """
    from jobforge.adapters.llm.semantic import SemanticAnalyzer
    if not SemanticAnalyzer._quote_is_real(quote, source):
        raise MaterialError(f"{what}的证据在原文里找不到，拒绝采用：{quote[:40]!r}")
