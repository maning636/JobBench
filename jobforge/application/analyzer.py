# -*- coding: utf-8 -*-
"""
求职工作台 · 匹配度分析引擎
============================

**这才是产品的核心。** 岗位收集和外包识别是手段，用户真正的问题只有一句：

    「这个岗位我能匹配多少？我差什么？简历该怎么改？」

三个引擎：
  1. MatchAnalyzer   我 vs 岗位：硬性要求命中、缺失项、简历改写建议
  2. CompareAnalyzer 多个岗位横向对比：这个比那个好在哪、差在哪
  3. SalaryAdvisor   薪资水位判断（需用户提供基准数据，不瞎猜）

设计上的三条底线：
- **不做假**。简历没写的技能一律算缺失，绝不用"推测你会"来加分。
  虚假匹配比不匹配更糟 —— 用户会因此投出一个必败的简历。
- **可核对**。每条结论都带 JD 原文 / 简历原文，用户能自己复核。
- **规则优先，LLM 可选**。没有 LLM 也能跑出真结果，不绑架外部服务。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from jobforge.domain.candidate import (
    Candidate, extract_skills, split_sentences, _requirement_level,
    SKILL_INDEX, _norm, is_soft_skill,
)
from jobforge.domain.models import Job


# =============================================================================
# 1. 匹配度分析
# =============================================================================

# 权重：硬性要求 > 普通要求 > 加分项
W_HARD, W_NORMAL, W_NICE = 3.0, 1.0, 0.4

# 低于这个数目的技术要求，匹配度不可信（分母陷阱，见 _verdict）
MIN_REQUIRED_SKILLS = 3

LEVEL_LABEL = {"hard": "硬性要求", "normal": "一般要求", "nice": "加分项"}


@dataclass
class SkillMatch:
    skill: str
    level: str                       # hard / normal / nice
    hit: bool
    jd_evidence: str = ""            # JD 里的原句
    resume_evidence: str = ""        # 简历里的原句
    category: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MatchReport:
    """「我 vs 这个岗位」的完整答案。"""
    job_position: str = ""
    job_company: str = ""

    match_score: float = 0.0         # 0-100
    verdict: str = ""                # 结论：强烈推荐 / 可以投 / 有风险 / 不建议
    summary: str = ""                # 一句话结论

    hits: List[SkillMatch] = field(default_factory=list)
    missing_hard: List[SkillMatch] = field(default_factory=list)   # 硬性缺失
    missing_normal: List[SkillMatch] = field(default_factory=list)  # 一般缺失
    covered: List[SkillMatch] = field(default_factory=list)         # 已命中

    suggestions: List[str] = field(default_factory=list)   # 简历/能力改进建议
    blockers: List[str] = field(default_factory=list)      # 投不了的硬伤
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["hit_count"] = len(self.covered)
        d["missing_count"] = len(self.missing_hard) + len(self.missing_normal)
        return d

    def to_display(self) -> str:
        lines = [
            f"【{self.job_position}】@{self.job_company}",
            f"匹配度 {self.match_score:.0f}%  →  {self.verdict}",
            f"  {self.summary}",
            f"  命中 {len(self.covered)} 项，缺 {len(self.missing_hard) + len(self.missing_normal)} 项",
        ]
        if self.missing_hard:
            lines.append("  硬性缺失：" + "、".join(m.skill for m in self.missing_hard))
        if self.missing_normal:
            lines.append("  一般缺失：" + "、".join(m.skill for m in self.missing_normal))
        for s in self.suggestions[:5]:
            lines.append(f"  建议：{s}")
        for b in self.blockers:
            lines.append(f"  ⚠ {b}")
        return "\n".join(lines)


class MatchAnalyzer:
    """JD 技能要求 vs 候选人技能的逐条比对。"""

    def __init__(self, candidate: Candidate, max_suggestions: int = 6):
        self.c = candidate
        self.have = {_norm(s) for s in candidate.skill_names}
        self.resume_by_skill: Dict[str, str] = {
            s["name"]: s.get("evidence", "") for s in candidate.skills
        }
        self.soft_found: Dict[str, str] = {}   # 软性素质要求（不进匹配度）
        self.max_suggestions = max_suggestions

    # ------------------------------------------------------------------

    def parse_jd(self, jd: str) -> List[SkillMatch]:
        """从 JD 提取技能要求，并判定每条是硬性/一般/加分。

        **软性素质单独收集，不进匹配度分母。**
        实测踩过的坑：「沟通能力」被当成技术要求，
        简历里没写就掉分 —— 但简历写"沟通能力强"本来没人信，
        拿它扣技术匹配度是在稀释真实的信号。软性项改为单独提示。
        """
        jd = jd or ""
        results: Dict[str, SkillMatch] = {}
        self.soft_found: Dict[str, str] = {}

        for sent in split_sentences(jd):
            level = _requirement_level(sent)
            for sk in extract_skills(sent):
                name = sk["name"]
                if is_soft_skill(name):
                    self.soft_found.setdefault(name, sent[:120])
                    continue
                prev = results.get(name)
                # 同名技能取"更硬"的那个等级
                rank = {"hard": 3, "normal": 2, "nice": 1}
                if prev and rank[prev.level] >= rank[level]:
                    continue
                results[name] = SkillMatch(
                    skill=name,
                    level=level,
                    hit=_norm(name) in self.have,
                    jd_evidence=sent[:160],
                    resume_evidence=self.resume_by_skill.get(name, ""),
                    category=sk["category"],
                )

        return list(results.values())

    # ------------------------------------------------------------------

    def analyze(self, job: Job) -> MatchReport:
        self.soft_found = {}          # 每次 analyze 重置，避免上一岗位的残留
        matches = self.parse_jd(job.jd or "")

        rep = MatchReport(job_position=job.position, job_company=job.company)
        rep.hits = matches

        total_w = 0.0
        got_w = 0.0
        for m in matches:
            w = {"hard": W_HARD, "normal": W_NORMAL, "nice": W_NICE}[m.level]
            total_w += w
            if m.hit:
                got_w += w
                rep.covered.append(m)
            else:
                (rep.missing_hard if m.level == "hard" else rep.missing_normal).append(m)

        rep.match_score = round(got_w / total_w * 100) if total_w else 0.0
        rep.blockers = self._blockers(rep, job)
        rep.verdict, rep.summary = self._verdict(rep, job)
        rep.suggestions = self._suggestions(rep, job)
        rep.notes = self._notes(job)
        return rep

    # ------------------------------------------------------------------

    def _verdict(self, r: MatchReport, job: Job) -> Tuple[str, str]:
        n_hard_missing = len(r.missing_hard)

        # 经验/学历这类硬门槛必须影响结论，不能只当备注。
        # 实测踩过的坑：应届生投「3-5年」的岗，技能匹配 90% 就被标成
        # 「强烈推荐」——而这条岗大概率根本不会看应届简历。
        exp_blocked = any("经验要求" in b for b in self._blockers(r, job))
        edu_blocked = any("学历不符" in b for b in self._blockers(r, job))

        if not r.hits:
            return "无法评估", "这个岗位没写清技术要求，没法算匹配度（这本身就是个信号：JD 太模糊）"

        # 分母陷阱 —— 实测踩过的坑：一个只写了「负责后端开发」的实习岗，
        # 只识别出 1 项要求且恰好命中，算出 100%「强烈推荐」，排序直接冲到第一，
        # 而真正写清要求的岗位只有 60%。要求太少时匹配度不可信，不能给高分。
        if len(r.hits) < MIN_REQUIRED_SKILLS:
            return "无法评估", (
                f"JD 只识别出 {len(r.hits)} 项技术要求（少于 {MIN_REQUIRED_SKILLS} 项），"
                "匹配度不可信：写得越含糊分母越小，分数越高。"
                "投之前先问清团队和业务"
            )

        if n_hard_missing == 0 and r.match_score >= 80:
            if edu_blocked:
                return "不要投", "技术全对得上，但学历不符，先别浪费投递机会"
            if exp_blocked:
                return "有风险", "技术全对得上，但岗位要的经验与你不符，投之前先确认是否接受应届"
            return "强烈推荐", "硬性要求全覆盖，可以直接投，简历把命中的技能往前放"

        if n_hard_missing == 0 and r.match_score >= 65:
            if edu_blocked:
                return "不要投", "技术基本达标，但学历不符"
            if exp_blocked:
                return "有风险", "技术达标，但经验要求与你不符，回复率可能很低"
            return "可以投", "硬性要求都满足，一般要求有缺口，值得投"

        if n_hard_missing == 1 and r.match_score >= 50:
            return "有风险", f"缺 1 项硬性要求（{r.missing_hard[0].skill}），要么补要么换个方向"
        if n_hard_missing >= 2:
            return "不建议", f"缺 {n_hard_missing} 项硬性要求，投出去大概率石沉大海"

        # 实测踩过的坑：一个 0% 匹配的岗位，因为一条硬性缺失都没有
        # （要求全是「一般」级别），落到了最后那个兜底分支，
        # 输出了「硬性要求基本满足但整体契合度一般」——
        # 0% 匹配说「契合度一般」是自相矛盾的，用户会直接困惑。
        # 所以兜底分支必须按分数区分，不能一句话盖过去。
        if r.match_score >= 40:
            return "边缘", f"匹配度 {r.match_score:.0f}%，硬性要求没冲突但整体契合度一般"
        return "不建议", (
            f"匹配度只有 {r.match_score:.0f}%，JD 提的 {len(r.hits)} 项要求你基本都不沾边"
            + ("（多为通用要求，可考虑投投看）" if r.match_score > 0 else "")
        )

    def _blockers(self, r: MatchReport, job: Job) -> List[str]:
        """投不了的硬伤 —— 这类必须说清楚，否则用户在浪费投递机会。"""
        out = []
        for m in r.missing_hard:
            out.append(f"硬性要求未满足：{m.skill}（JD 原文：{m.jd_evidence[:50]}…）")

        # 学历
        if job.education and self.c.education:
            from jobforge.application.filters import edu_rank
            if edu_rank(job.education) > edu_rank(self.c.education):
                out.append(f"学历不符：岗位要 {job.education}，你是 {self.c.education}")

        # 应届身份
        if self.c.is_fresh_grad and job.experience:
            if re.search(r"\d+\s*-\s*\d+\s*年", job.experience) and "应届" not in job.jd:
                out.append(
                    f"经验要求：岗位要「{job.experience}」，你是应届。"
                    "部分公司明确不接受应届，可以投但要接受低回复率"
                )

        # 薪资预期与岗位上限
        if self.c.salary_expect and job.salary_max:
            m = re.search(r"(\d+)", self.c.salary_expect)
            if m and int(m.group(1)) > job.salary_max:
                out.append(
                    f"薪资预期 {self.c.salary_expect} 高于岗位上限 "
                    f"{job.salary_max}，要么谈要么换"
                )
        return out

    def _suggestions(self, r: MatchReport, job: Job) -> List[str]:
        """建议必须是可执行的，且严守不造假底线。

        纪律：**按问题严重度排序，并且合并同类项**。
        实测踩过的坑：0% 匹配的岗位给出四条一模一样的
        「硬性缺 X：确实不会就别投」——等于什么都没说。
        """
        out: List[str] = []
        if not r.hits:
            return out

        # ---- 第一优先：硬性缺失，按类型合并成 1~2 条 ----
        if r.missing_hard:
            names = [m.skill for m in r.missing_hard]
            tool = [n for n in names if n in _TOOLCHAIN]
            exp = [n for n in names if n in _EXPERIENCE_SKILLS]
            other = [n for n in names if n not in _TOOLCHAIN and n not in _EXPERIENCE_SKILLS]

            if len(names) >= 2:
                head = (f"这个岗位缺 {len(names)} 项硬性要求"
                        f"（{'、'.join(names)}），建议先别投")
            else:
                head = f"缺 1 项硬性要求（{names[0]}）"
            out.append(head)

            if tool:
                out.append(
                    f"其中 {'、'.join(tool)} 属于工具链，自学 1~2 周能上手，"
                    "补齐后这个岗就从「不建议」变成「可以投」"
                )
            if exp:
                out.append(
                    f"其中 {'、'.join(exp)} 缺的是项目经历而不是知识点 —— "
                    "实习或课程项目里补一个相关场景，比面试前临时背概念有用"
                )
            if other:
                out.append(
                    f"其中 {'、'.join(other)} 没在你的简历里出现，"
                    "确认是真不会还是简历没写出来 —— 简历没写的都会被当成不会"
                )

        # ---- 第二优先：措辞对齐（只在确实做过时才有用）----
        jd_skills = {m.skill for m in r.hits}
        said = set()
        for s in self.c.skills:
            name = s["name"]
            if name in jd_skills or name in said:
                continue
            for other in jd_skills:
                if _similar(name, other) and other not in said:
                    said.add(other)
                    out.append(
                        f"简历写的是「{name}」，JD 要的是「{other}」。"
                        f"**确实用过 {other} 就明确写出来**；没用过不要写，"
                        "这条只帮你把已经会的东西说清楚"
                    )
                    break

        # ---- 第三优先：只有高匹配才值得说的优化项 ----
        if r.covered and r.match_score >= 75:
            names = [m.skill for m in r.covered][:6]
            out.append(
                f"简历技能区把这几项前置，并直接用 JD 的原词：{'、'.join(names)}"
            )

        # ---- JD 模糊 = 反向信号 ----
        if len(r.hits) < 3:
            out.append(
                f"这个 JD 只识别出 {len(r.hits)} 项技术要求，写得很含糊。"
                "面试前先问清团队和业务，否则容易被当廉价劳动力"
            )

        return out[: self.max_suggestions]

    def _notes(self, job: Job) -> List[str]:
        n = []
        if job.is_outsourcing_risk is True:
            n.append("已识别为外包/派遣岗位，优先看证据句再决定要不要投")
        if job.salary_state == "unresolved":
            n.append("薪资没解析出来，匹配度里没有把薪资算进去")
        if not job.jd:
            n.append("这个岗位没有 JD 文本，匹配度只基于岗位名，建议补充后再看")
        if self.soft_found:
            names = "、".join(list(self.soft_found)[:5])
            n.append(
                f"这个 JD 还要求了软性素质：{names}。"
                "这类没法用简历证明面试时的表现，**没计入匹配度**，"
                "面试前自己准备相应案例即可"
            )
        return n


def _similar(a: str, b: str) -> bool:
    """粗粒度的语义相近判断（词典级，不做 NLP）。"""
    pairs = [
        {"消息队列", "Kafka", "RabbitMQ", "RocketMQ"},
        {"缓存", "Redis", "Memcached"},
        {"搜索", "Elasticsearch", "Solr"},
        {"大数据", "Hadoop", "Spark", "Flink", "Hive"},
        {"前端框架", "React", "Vue", "Angular"},
        {"云原生", "Docker", "Kubernetes", "K8s", "DevOps"},
        {"机器学习", "深度学习", "PyTorch", "TensorFlow", "NLP", "大模型", "LLM"},
        {"自动化测试", "Selenium", "JMeter", "Appium", "单元测试"},
    ]
    for g in pairs:
        if a in g and b in g:
            return True
    return False


def _expect_to_monthly(text: str) -> Optional[int]:
    """把用户写的薪资预期折算成元/月。

    **单位必须显式处理**。实测踩过的坑：直接 `re.search(r'(\\d+)', '18K')`
    得到 18，当成 18 元，于是"岗位下限 15000 已达到你的预期 18"这种荒谬结论
    永远成立。和「日薪被当成月薪」是同一类错误。
    """
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~—至到]?\s*([kKwW万千]?)", text)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit in ("k", "K", "w", "W", "万"):
        val *= 1000
    if val < 1000:  # 小于 1000 基本只能是"写着 15 忘了加 K"
        val *= 1000
    return int(val)


# 工具链类技能：自学性价比高；经验类技能：只能靠项目经历
_TOOLCHAIN = {"Docker", "Kubernetes", "Jenkins", "CI/CD", "自动化测试",
              "Git", "Nginx", "Linux", "Shell 脚本"}
_EXPERIENCE_SKILLS = {"高并发", "高可用", "分布式", "分布式事务", "微服务",
                      "服务治理", "架构设计", "性能调优", "领域驱动", "DDD"}


# =============================================================================
# 2. 横向对比
# =============================================================================

@dataclass
class CompareRow:
    position: str
    company: str
    salary_text: str
    city: str
    match_score: float
    verdict: str
    outsourcing_risk: Optional[bool]
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CompareAnalyzer:
    """把 N 个岗位放一起，回答「选哪个」。"""

    def __init__(self, candidate: Candidate):
        self.c = candidate

    def compare(self, jobs: List[Job], reports: List[MatchReport]) -> List[CompareRow]:
        rows: List[CompareRow] = []
        for job, rep in zip(jobs, reports):
            row = CompareRow(
                position=job.position,
                company=job.company,
                salary_text=job.salary_text or "—",
                city=job.city,
                match_score=rep.match_score,
                verdict=rep.verdict,
                outsourcing_risk=job.is_outsourcing_risk,
            )
            if rep.match_score >= 75 and rep.verdict in ("强烈推荐", "可以投"):
                row.pros.append(f"匹配度高（{rep.match_score:.0f}%）")
            # JD 要求太少时不能说「无硬性缺口」——那不是优点，是没写清楚。
            # 实测踩过的坑：一条技术要求都没有的模糊 JD，
            # 因为「缺 0 项」被列成优势，看起来像白捡的好岗。
            if not rep.missing_hard and len(rep.hits) >= MIN_REQUIRED_SKILLS:
                row.pros.append("无硬性缺口")
            # 薪资优势只对同周期的岗位成立：
            # 实测踩过的坑是实习日薪 300 被写成「薪资明确（上限 300）」，
            # 和全职工资 25000 并列成优势，完全不可比。
            if (job.salary_state == "verified" and job.salary_max
                    and (job.salary_period or "月") == "月"
                    and rep.match_score >= 50):
                row.pros.append(f"薪资明确（上限 {job.salary_max}）")
            if job.is_outsourcing_risk is False:
                row.pros.append("未见外包特征")

            if rep.missing_hard:
                row.cons.append(
                    "缺硬性：" + "、".join(m.skill for m in rep.missing_hard[:3]))
            if len(rep.hits) < MIN_REQUIRED_SKILLS:
                row.cons.append(
                    f"JD 只写了 {len(rep.hits)} 项技术要求，匹配度不可信")
            if job.is_outsourcing_risk is True:
                row.cons.append("疑似外包/派遣")
            if job.salary_state in ("unresolved", "na"):
                row.cons.append("薪资不明")
            elif job.salary_period and job.salary_period != "月":
                row.cons.append(f"{job.salary_period}薪，与全职岗不可直接比")
            if not job.jd:
                row.cons.append("JD 信息不足")
            rows.append(row)

        # 排序：先按「能不能评估」，再按匹配度。
        # 实测踩过的坑：无法评估的岗位因为分母小拿了 0% 或高分，
        # 和真正评估过的岗位混排，用户照着排序会投错。
        evaluable = {"强烈推荐": 0, "可以投": 1, "有风险": 2,
                     "边缘": 3, "不建议": 4, "不要投": 5, "无法评估": 9}
        rows.sort(key=lambda r: (evaluable.get(r.verdict, 8), -r.match_score))
        return rows

    def explain(self, rows: List[CompareRow]) -> List[str]:
        """生成对比结论：第一名凭什么赢。"""
        # 只在「都能评估」的岗位之间比胜负。
        # 实测踩过的坑：拿一个 JD 模糊、无法评估的岗位去和正常岗比，
        # 生成的结论毫无意义。
        usable = [r for r in rows if r.verdict != "无法评估"]
        if len(usable) < 2:
            vague = len(rows) - len(usable)
            if vague:
                return [
                    f"{vague} 个岗位的 JD 技术要求写得太少，没法算匹配度，"
                    "已排到最后。这类岗投之前先问清团队和业务"
                ]
            return []
        top, second = usable[0], usable[1]
        out = [
            f"按匹配度，**{top.position}**（{top.company}，{top.match_score:.0f}%）"
            f"优于 {second.position}（{second.match_score:.0f}%）"
        ]
        only_top = [p for p in top.pros if p not in second.pros]
        only_second = [p for p in second.pros if p not in top.pros]
        if only_top:
            out.append(f"它多出来的优势：{'；'.join(only_top)}")
        if only_second:
            out.append(f"但对方在这些点上更好：{'；'.join(only_second)}")
        if second.cons and not top.cons:
            out.append(f"对方的风险：{'；'.join(second.cons)}")

        vague = len(rows) - len(usable)
        if vague:
            out.append(f"另有 {vague} 个岗位因 JD 写得太少无法评估，已排在后面")
        return out


# =============================================================================
# 3. 薪资判断
# =============================================================================

@dataclass
class SalaryView:
    """薪资水位判断。

    **诚实边界**：没有真实市场数据集时，本引擎只做
    「可算的比对」（和用户自己的预期比、和同批岗位比），
    绝不用模型记忆里的"大概行情"编一个市场分位数出来。
    要真正的市场分位，需要用户导入基准数据。
    """
    salary_text: str
    salary_min: Optional[int]
    salary_max: Optional[int]
    period: str
    vs_expect: Optional[str] = None
    vs_batch: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SalaryAdvisor:
    def __init__(self, candidate: Candidate):
        self.c = candidate

    def view(self, job: Job, batch: List[Job] = None) -> SalaryView:
        v = SalaryView(
            salary_text=job.salary_text or "",
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            period=job.salary_period,
        )

        if job.salary_state == "unresolved":
            v.note = "薪资没解析出来，不做任何判断"
            return v
        if job.salary_state == "na":
            v.note = "岗位标注面议，没有可比较的数值"
            return v
        if job.salary_period and job.salary_period != "月":
            v.note = f"这是{job.salary_period}薪，和月薪预期不直接可比"
            return v

        # 和用户自己的预期比
        if self.c.salary_expect and job.salary_max:
            exp = _expect_to_monthly(self.c.salary_expect)
            if exp:
                if job.salary_max < exp:
                    v.vs_expect = (f"岗位上限 {job.salary_max} 低于你的预期 {exp}"
                                   f"，差距 {exp - job.salary_max}"
                                   f"（{(exp - job.salary_max) * 100 // exp}%）")
                elif job.salary_min and job.salary_min >= exp:
                    v.vs_expect = f"岗位下限 {job.salary_min} 已达到你的预期 {exp}"
                else:
                    v.vs_expect = (f"与你的预期 {exp} 有重叠"
                                   f"（{job.salary_min}~{job.salary_max}）")

        # 和同批岗位比（这是有真实数据的比对，不是猜市场）
        if batch and job.salary_max:
            peers = [
                j.salary_max for j in batch
                if j.salary_max and (j.salary_period or "月") == (job.salary_period or "月")
                and (j.city == job.city)
            ]
            if len(peers) >= 2:
                peers.sort()
                rank = peers.index(job.salary_max) + 1
                # 必须说清方向：实测踩过的坑是只写「排第 1/2」，
                # 用户会理解成「排第一」，而实际是最便宜的那个
                if rank == len(peers):
                    pos = "这批里最高"
                elif rank == 1:
                    pos = "这批里最低"
                else:
                    pos = f"从低到高第 {rank}"
                v.vs_batch = f"在同城市同类型的 {len(peers)} 个岗位里，薪资{pos}"

        v.note = "市场分位需要外部基准数据，当前不做推断"
        return v
