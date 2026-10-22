# -*- coding: utf-8 -*-
"""
求职工作台 · LLM 语义分析层（增强，不是替代）
=============================================

规则引擎（`MatchAnalyzer`）用的是技能词典。它读得懂
「必须精通 Java 和 Spring Boot」，但读不懂
「要求具备良好的抽象建模能力与系统设计思维」。

这一层用 LLM 补上**语义理解**的缺口。

**最重要的设计约束：LLM 只能增强，不能推翻硬结论。**

学历不符、经验不符、外包证据、薪资数值 —— 这些是程序算出来的事实，
LLM 说「其实可以」也不能改。理由和 v1.2.0 的一条教训一致：
把模型的判断当事实输出，会静默污染整个决策。

所以这里的分工是：

    规则引擎  负责：事实、硬门槛、可核对的数字     —— 永远优先
    LLM      负责：语义补充、措辞建议、可读的解释   —— 只做加法

没配 LLM 时，这一层整体不工作，规则引擎照常产出结果，功能不残废。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from jobforge.adapters.llm.client import LLMClient, LLMResult
from jobforge.application.analyzer import MatchReport
from jobforge.domain.candidate import Candidate, is_soft_skill
from jobforge.domain.models import Job


SYSTEM = """你是资深技术招聘顾问，正在帮一位求职者判断某个岗位是否值得投。

硬性要求：
1. 只依据给定的简历和 JD 文本判断，不要编造简历里没有的经历。
2. evidence_jd 和 evidence_resume 都必须是**从原文里一字不改地摘录的连续片段**，
   不要改写、不要拼接、不要概括。这两个字段会被程序拿去和原文做比对，
   摘录不出来的条目会被直接丢弃。
3. 如果简历里没有某项能力的直接证据，就放进 missing，不要放进 semantic_matches。
   宁可少说，也不要说"可能间接体现"——那等于替用户编造。
4. skill 字段必须是**单个能力名词，不超过 12 个字**。
   JD 里如果写「高并发、微服务架构与分布式事务的实际项目经验」，
   要拆成「高并发」「微服务」「分布式事务」三条各自评估，
   不要把三项合成一条长句 —— 合成后无法逐项核实。
5. confidence 要诚实：
   high = 简历有直接、明确的对应经历；
   medium = 相关但不完全对应（例如简历有并发经验但没说微服务）；
   low = 只是沾边。
   宁可标 low 也不要标 high。
6. soft_fit.score 是 0~100 的**契合度**，100 表示工作内容完全对得上，0 表示完全对不上。
7. 输出必须是 JSON，不要任何解释性文字或 markdown 代码块。

输出 JSON 结构：
{
  "semantic_matches": [
    {"skill": "能力名称", "evidence_jd": "JD 原文连续片段", "evidence_resume": "简历原文连续片段", "confidence": "high|medium|low"}
  ],
  "missing": [
    {"skill": "缺失的能力", "why": "为什么需要", "learnable": true, "learn_cost": "1周|1月|3月|不可短期补"}
  ],
  "soft_fit": {
    "score": 0,
    "comment": "不考虑具体技术栈，只看 JD 描述的工作内容与候选人经历是否对得上"
  },
  "resume_advice": ["针对这个 JD 的简历修改建议，每条都要具体到改哪句"],
  "red_flags": ["JD 里值得警惕的表述，比如职责模糊、要求堆砌、疑似外包话术"]
}"""


@dataclass
class SemanticResult:
    """LLM 语义分析的产出。字段全部可选，拿不到就是 None。"""
    available: bool = False
    error: str = ""
    elapsed: float = 0.0
    model: str = ""

    semantic_matches: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[Dict[str, Any]] = field(default_factory=list)
    soft_fit_score: Optional[int] = None
    soft_fit_comment: str = ""
    resume_advice: List[str] = field(default_factory=list)
    red_flags: List[str] = field(default_factory=list)

    # 通过全部过滤但置信度不够高的条目 —— **不丢弃，降级呈现**。
    #
    # 为什么不直接丢：程序能验"证据是不是原文摘录"，但验不了
    # 「日均 2 万请求的项目」能不能支撑「具备分布式事务经验」——
    # 这是语义推断的合理性，代码判断不了。
    #
    # 实测踩过的坑：模型拿一段真实摘录去支撑一个过度引申的结论，
    # 长度检查和摘录校验都拦不住。强行丢弃会连带丢掉模型真正的发现，
    # 所以这里保留但标注，让用户自己判断 —— 匹配度和结论始终由规则引擎决定，
    # 这些只是"额外提示"，误导不了决策。
    weak_matches: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "model": self.model,
            "semantic_matches": self.semantic_matches,
            "weak_matches": self.weak_matches,
            "missing": self.missing,
            "soft_fit_score": self.soft_fit_score,
            "soft_fit_comment": self.soft_fit_comment,
            "resume_advice": self.resume_advice,
            "red_flags": self.red_flags,
        }


class SemanticAnalyzer:
    """LLM 语义分析。没配 Key 就整体降级，不影响规则引擎。"""

    def __init__(self, client: Optional[LLMClient] = None):
        self.client = client or LLMClient()

    @property
    def available(self) -> bool:
        return self.client.available()

    # ------------------------------------------------------------------

    def analyze(self, job: Job, candidate: Candidate,
                report: Optional[MatchReport] = None) -> SemanticResult:
        out = SemanticResult(model=self.client.cfg.model)

        if not self.available:
            out.error = "未配置 LLM，已跳过语义分析（规则引擎结果不受影响）"
            return out

        # JD 太短就没必要调 LLM —— 省钱省时间
        jd = (job.jd or "").strip()
        if len(jd) < 40:
            out.error = "JD 内容过短，语义分析没有意义"
            return out
        if not (candidate.resume_text or "").strip():
            out.error = "没有候选人简历，无法做语义比对"
            return out

        r = self.client.chat_json(SYSTEM, _build_user_prompt(job, candidate))
        out.elapsed = r.elapsed
        if not r.ok:
            out.error = r.error
            return out

        out.available = True
        d = r.data or {}
        out.semantic_matches = _as_list(d.get("semantic_matches"))
        out.missing = _as_list(d.get("missing"))
        sf = d.get("soft_fit") or {}
        score = sf.get("score")
        out.soft_fit_score = int(score) if isinstance(score, (int, float)) else None
        out.soft_fit_comment = str(sf.get("comment") or "")
        out.resume_advice = [str(x) for x in _as_list(d.get("resume_advice"))][:6]
        out.red_flags = [str(x) for x in _as_list(d.get("red_flags"))][:6]

        # 把原文传进去做摘录校验 —— 这是唯一能挡住"过度引申"的办法
        out = self._sanitize(out, jd_source=job.jd or "",
                             resume_source=candidate.resume_text or "")

        # 软性素质在规则层已被排除，LLM 层必须一致，
        # 否则模型会把「沟通能力与责任心」捞回来当能力命中 ——
        # 实测踩过：它甚至自己写「简历证据：无直接证据…可能间接体现」。
        out.semantic_matches = [
            m for m in out.semantic_matches
            if not is_soft_skill(str(m.get("skill") or "").strip())
        ]
        for m in list(out.missing):
            if is_soft_skill(str(m.get("skill") or "").strip()):
                out.missing.remove(m)
        return out

    # ------------------------------------------------------------------

    # 证据的最短长度。低于此长度视为"没有证据"——
    # 模型为了填字段很容易写"有"、"会"、"具备"这类敷衍内容。
    MIN_EVIDENCE_LEN = 8

    # 模型自认没有证据时会用的措辞。这类必须丢弃，
    # 实测踩过：模型输出「简历证据：无直接证据，但简历中提到熟悉 MyBatis，
    # 可能间接体现沟通能力」——它自己都承认没有，却填满了字段混过了长度检查。
    NO_EVIDENCE_MARKERS = (
        "无直接证据", "没有直接证据", "未提及", "没有提及", "未提到", "没有提到",
        "无法确认", "不确定", "可能间接", "间接体现", "可能体现", "推测",
        "简历中无", "简历里无", "未见", "查无",
    )

    @staticmethod
    def _has_evidence(text: Any) -> bool:
        s = str(text or "").strip()
        return len(s) >= SemanticAnalyzer.MIN_EVIDENCE_LEN

    @staticmethod
    def _norm(text: Any) -> str:
        """归一化：去掉所有空白与标点，只留实义字符。

        比对时要忽略标点差异，因为模型常把全角改成半角、
        或把顿号改成逗号 —— 那是格式差异不是编造，不该因此丢真证据。
        """
        s = str(text or "")
        return "".join(ch for ch in s if not re.match(r"[\s,，。、；;：:（）()「」『』\"'’“”\-—…\.\!！?？\[\]{}]", ch))

    @classmethod
    def _quote_is_real(cls, quote: Any, source: str) -> bool:
        """验证模型摘的片段**真的存在于原文里**。

        这是最可靠的一道闸：模型要通过这一关，必须真的从原文里
        一字不改地摘录。改写、编造、把两句话拼起来，都会因为
        归一化后对不上而被丢弃。

        实测踩过的坑：模型拿一段普通的项目描述（日均 2 万请求）
        去支撑「具备高并发、微服务架构与分布式事务经验」，
        长度检查完全拦不住 —— 证据"存在"，但结论不由它支撑。
        摘录比对能挡住这类过度引申。
        """
        q = cls._norm(quote)
        if not q:
            return False
        s = cls._norm(source)
        if not s:
            return False
        if q in s:
            return True
        # 允许证据被截断：取前后两段各半分别找
        if len(q) >= 20:
            half = len(q) // 2
            return q[:half] in s and q[-half:] in s
        return False

    @classmethod
    def _sanitize(cls, out: SemanticResult, jd_source: str = "",
                  resume_source: str = "") -> SemanticResult:
        """过滤掉不合规的输出 —— **这里是模型幻觉的最后一道闸**。

        四道检查，按可靠性从低到高：
          1. 字段完整（skill 存在）
          2. 证据长度达标
          3. 模型没有自认"无证据"
          4. **摘录真的能在原文里找到** ← 最强，无法绕过

        教训和 v1.2.0 同源：**约束必须写在代码里，不能只写进提示词。**
        宁可少一条，也不能让用户误以为自己会某项技能。
        """
        kept = []
        for m in out.semantic_matches:
            skill = str(m.get("skill") or "").strip()
            if not skill:
                continue
            ev_jd = str(m.get("evidence_jd") or "")
            ev_rs = str(m.get("evidence_resume") or "")

            if not cls._has_evidence(ev_jd) or not cls._has_evidence(ev_rs):
                continue

            # 模型自认没有证据
            blob = f"{ev_jd}{ev_rs}"
            if any(w in blob for w in cls.NO_EVIDENCE_MARKERS):
                continue

            # 摘录真实性校验（有原文可查才查）
            if jd_source and not cls._quote_is_real(ev_jd, jd_source):
                continue
            if resume_source and not cls._quote_is_real(ev_rs, resume_source):
                continue

            kept.append(m)

        # 去重
        seen, dedup = set(), []
        for m in kept:
            k = str(m.get("skill")).strip().lower()
            if k in seen:
                continue
            seen.add(k)
            dedup.append(m)

        # 置信度分流：high 才算命中，medium/low 降级为"需自行核实"
        strong = [m for m in dedup if str(m.get("confidence", "")).lower() == "high"]
        weak = [m for m in dedup if str(m.get("confidence", "")).lower() != "high"]
        out.semantic_matches = strong[:20]
        out.weak_matches = weak[:10]
        return out

    # ------------------------------------------------------------------

    @staticmethod
    def merge(report: MatchReport, sem: SemanticResult) -> MatchReport:
        """把语义结果并进规则报告 —— **只做加法**。

        不改 match_score：分数来自可核对的硬门槛加权，
        让模型去调它会引入不可解释的波动。
        不改 verdict：结论受学历/经验等硬门槛约束。
        语义层只补充建议与说明。
        """
        if not sem.available:
            return report

        # LLM 指出的额外缺失项 → 只进建议，不进评分
        for m in sem.missing:
            skill = str(m.get("skill") or "").strip()
            if not skill:
                continue
            why = str(m.get("why") or "").strip()
            cost = str(m.get("learn_cost") or "").strip()
            learnable = bool(m.get("learnable"))
            tail = f"（{cost}）" if cost else ""
            report.suggestions.append(
                f"语义分析补充：还缺「{skill}」{tail}"
                + (f"—— {why}" if why else "")
                + ("。这类能自学补" if learnable else "。短期内补不上，别硬投")
            )

        for a in sem.resume_advice:
            if not a or a in report.suggestions:
                continue
            # 实测踩过的坑：模型会输出「把 Kubernetes 写进简历，这样更有优势」——
            # 而候选人根本没这经验。规则引擎自己生成的每条建议都带
            # 「确实用过才写」的约束，模型这条没有保护，
            # 原样透传等于在教用户造假。所以强制追加同样的约束。
            report.suggestions.append(
                f"简历建议：{a}　"
                "**（这条来自模型，只能帮你把已经会的说清楚；"
                "确实没做过的不要写进简历，投出去被追问更糟）**"
            )

        for f in sem.red_flags:
            if f and f not in report.notes:
                report.notes.append(f"⚠ {f}")
        if sem.soft_fit_comment:
            report.notes.append(
                f"工作内容契合度 {sem.soft_fit_score if sem.soft_fit_score is not None else '?'}/100"
                f"：{sem.soft_fit_comment}"
            )

        # 置信度不足的语义命中 → 明确标注"需自行核实"，不当结论
        for m in sem.weak_matches:
            skill = str(m.get("skill") or "").strip()
            if not skill:
                continue
            conf = str(m.get("confidence") or "medium")
            ev = str(m.get("evidence_resume") or "")[:60]
            report.notes.append(
                f"模型认为你可能具备「{skill}」（置信度 {conf}，简历依据：{ev}…）—— "
                "**这条是推测不是结论，简历里没有明确写，请自己核实后再决定要不要用**"
            )

        report.suggestions = report.suggestions[:10]
        report.notes = report.notes[:8]
        return report


# =============================================================================

def _build_user_prompt(job: Job, c: Candidate) -> str:
    skills = "、".join(c.skill_names[:30]) or "（未识别到技术关键词）"
    return f"""【岗位】
岗位名：{job.position}
公司：{job.company or '未取到'}
城市：{job.city}
经验要求：{job.experience or '未写'}
学历要求：{job.education or '未写'}

JD 原文：
{job.jd}

【候选人简历】
识别到的技能：{skills}
学历：{c.education or '未识别'}
工作年限：{c.experience_years}
是否应届：{'是' if c.is_fresh_grad else '否'}
期望薪资：{c.salary_expect or '未填'}

简历原文：
{c.resume_text[:2000]}

请判断这个岗位与这位候选人的匹配情况，注意：
- 规则层已经算过技能命中率，你只需要补「语义层面」的东西：
  1. JD 里那些读起来像能力要求、但没有具体技术名词的描述
     （如"抽象建模能力""系统设计思维""有责任心"）
  2. 工作内容与候选人实际经历是否对得上（soft_fit）
  3. 针对这个 JD，简历具体该改哪几句
  4. JD 本身有没有值得警惕的表述
不要重复罗列 Java/Spring 这类已经能精确匹配的技术栈。"""


def _as_list(v: Any) -> List[Any]:
    if isinstance(v, list):
        return v
    if v is None:
        return []
    return [v]
