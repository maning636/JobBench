# -*- coding: utf-8 -*-
"""
求职工作台 · 求职台账域模型
============================

用户求职的完整旅程有五步：发现 → 评估 → 投递 → 跟进 → 复盘。
v2 之前只做了前两步，第三步只有一个「已投递」标签就断了 ——
而第三步之后才是求职最焦虑的部分：投了没回音怎么办。

这里建模三件事：
  Application  一次投递（投了哪个岗、什么时候、用的哪版简历、现在什么状态）
  Followup      一次跟进动作（什么时候、通过什么方式、说了什么）
  Resume       一份简历版本（校招版/社招版/不同方向）

**关键设计：投递记录要冗余岗位信息。**
岗位可能被清理、数据可能更新，但投递记录是用户的历史，不能跟着岗位一起消失。
所以 position/company/salary 在投递时拍一份快照存下来。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from jobforge.domain.models import Job


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# =============================================================================
# 状态机
# =============================================================================

class ApplyStatus(str, Enum):
    """投递状态。吸收了 v1.2.0 的社招 5 态 + 校招 11 态，统一成一张表。"""
    SAVED = "saved"                    # 收藏，未投
    APPLIED = "applied"                # 已投递，等回音
    ASSESSMENT = "assessment"          # 笔试/测评
    INTERVIEW = "interview"            # 面试中
    OFFER = "offer"                    # 拿到 offer
    REJECTED = "rejected"              # 被拒
    WITHDRAWN = "withdrawn"            # 已撤回

    @property
    def label(self) -> str:
        return {
            "saved": "待投递", "applied": "已投递", "assessment": "笔试中",
            "interview": "面试中", "offer": "已获 Offer",
            "rejected": "已拒绝", "withdrawn": "已撤回",
        }[self.value]

    @property
    def is_open(self) -> bool:
        """还在进行中（还需要关注）。"""
        return self.value in ("saved", "applied", "assessment", "interview")

    @property
    def is_closed(self) -> bool:
        return self.value in ("offer", "rejected", "withdrawn")


STATUS_FLOW: Dict[str, List[str]] = {
    "saved": ["applied", "withdrawn"],
    "applied": ["assessment", "interview", "offer", "rejected", "withdrawn"],
    "assessment": ["interview", "rejected", "withdrawn"],
    "interview": ["interview", "offer", "rejected", "withdrawn"],
    "offer": ["rejected", "withdrawn"],
    "rejected": ["saved", "applied"],
    "withdrawn": ["saved", "applied"],
}


def can_transition(src: str, dst: str) -> bool:
    return dst in STATUS_FLOW.get(src, [])


# =============================================================================
# 简历版本
# =============================================================================

@dataclass
class Resume:
    """一份简历版本。

    为什么要有版本：投校招岗和社招岗要突出的东西不一样，
    同一份简历投所有岗位是效率最低的做法。
    """
    id: int = 0                    # 主键（由存储层填充）
    name: str = ""                 # 如「校招版」「Java方向」
    content: str = ""              # 简历全文（分析时用它）
    tag: str = ""                  # 面向方向，如「Java后端」
    is_default: bool = False
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        now = _now()
        self.created_at = self.created_at or now
        self.updated_at = now
        if not self.name:
            self.name = f"简历 {datetime.now().strftime('%m-%d %H:%M')}"

    @property
    def hash(self) -> str:
        return hashlib.sha1((self.content or "").encode("utf-8")).hexdigest()[:12]

    @property
    def row_id(self) -> int:
        return getattr(self, "id", 0) or 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["hash"] = self.hash
        d["length"] = len(self.content or "")
        return d

    @classmethod
    def field_names(cls) -> List[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Resume":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# =============================================================================
# 投递记录
# =============================================================================

@dataclass
class Application:
    """一次投递。岗位信息在投递时快照，不随岗位变化。"""

    # 主键（由存储层填充）
    id: int = 0

    # 关联
    job_fp: str = ""                # 岗位指纹（可为空：岗位被删了也要留住记录）
    job_id: Optional[int] = None    # 数据库 row_id，便于回跳

    # 快照（岗位可能变，这条记录不能变）
    position: str = ""
    company: str = ""
    city: str = ""
    salary_text: str = ""
    url: str = ""
    channel: str = ""

    # 投递
    applied_at: str = ""            # 投递日期 YYYY-MM-DD
    resume_name: str = ""           # 用的哪版简历
    resume_hash: str = ""
    match_score: Optional[float] = None   # 投递那一刻的匹配度快照
    match_verdict: str = ""         # 投递那一刻的结论快照

    # 过程
    status: str = ApplyStatus.APPLIED.value
    next_step_at: str = ""          # 下一步时间（笔试/面试/跟进）
    last_followup_at: str = ""      # 最近一次跟进
    followup_count: int = 0
    reply: str = ""                 # 对方回复的原文/摘要

    # 结果
    result_at: str = ""
    result_note: str = ""
    salary_offer: str = ""
    reject_reason: str = ""

    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        now = _now()
        self.created_at = self.created_at or now
        self.updated_at = now
        self.applied_at = self.applied_at or today()

    @property
    def row_id(self) -> int:
        return self.id or 0

    # ------------------------------------------------------------------

    @classmethod
    def from_job(cls, job: Job, resume: Optional[Resume] = None,
                 match_score: Optional[float] = None,
                 match_verdict: str = "") -> "Application":
        """从岗位创建投递记录，**当场快照岗位信息**。"""
        return cls(
            job_fp=job.fingerprint,
            job_id=getattr(job, "db_id", None),
            position=job.position,
            company=job.company,
            city=job.city,
            salary_text=job.salary_text,
            url=job.url,
            channel=job.channel,
            applied_at=today(),
            resume_name=resume.name if resume else "",
            resume_hash=resume.hash if resume else "",
            match_score=match_score,
            match_verdict=match_verdict,
            status=ApplyStatus.APPLIED.value,
        )

    def days_since_applied(self, ref: Optional[str] = None) -> int:
        d = self.applied_at or self.created_at[:10]
        try:
            dt = datetime.strptime(d[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            return 0
        ref_d = datetime.strptime(ref, "%Y-%m-%d").date() if ref else datetime.now().date()
        return (ref_d - dt).days

    def days_since_followup(self) -> Optional[int]:
        if not self.last_followup_at:
            return None
        try:
            dt = datetime.strptime(self.last_followup_at[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            return None
        return (datetime.now().date() - dt).days

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["days_since_applied"] = self.days_since_applied()
        d["status_label"] = ApplyStatus(self.status).label if self.status in [
            e.value for e in ApplyStatus] else self.status
        d["days_since_followup"] = self.days_since_followup()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Application":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# =============================================================================
# 跟进记录
# =============================================================================

class FollowupChannel(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    PLATFORM = "platform"      # BOSS 内聊
    OTHER = "other"


@dataclass
class Followup:
    id: int = 0                    # 主键（由存储层填充）
    application_id: int = 0
    at: str = ""
    channel: str = FollowupChannel.PLATFORM.value
    content: str = ""          # 我发了什么
    reply: str = ""            # 对方回了什么（没回就留空）
    got_reply: bool = False
    created_at: str = ""

    def __post_init__(self):
        now = _now()
        self.created_at = self.created_at or now
        self.at = self.at or now[:10]
        if self.reply:
            self.got_reply = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================================
# 跟进提醒规则
# =============================================================================

@dataclass
class Reminder:
    """一条待办提醒。"""
    level: str            # urgent / soon / info
    title: str
    detail: str
    application_id: int
    days: int = 0
    action: str = ""      # 建议动作

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# 规则参数（集中在这里，方便用户按自己的情况调）
RULE_FOLLOWUP_DAYS = 3      # 投递后几天没动静就建议跟进
RULE_FOLLOWUP_URGENT = 7    # 几天后就比较急
RULE_INTERVIEW_STALE = 3    # 面试中但没记录下一步，几天后提醒
RULE_OFFER_DECIDE = 3       # offer 后几天该做决定了


def build_reminders(apps: List[Application],
                    rule_followup: int = RULE_FOLLOWUP_DAYS,
                    rule_urgent: int = RULE_FOLLOWUP_URGENT) -> List[Reminder]:
    """从投递记录生成待办提醒。

    规则是纯代码，不靠 LLM —— 判断"该不该跟进"是可计算的，
    不该把确定性的事交给不确定的模型。
    """
    out: List[Reminder] = []
    for a in apps:
        st = a.status

        if st == ApplyStatus.APPLIED.value:
            d = a.days_since_applied()
            if d >= rule_urgent:
                out.append(Reminder(
                    "urgent", f"「{a.position}」投了 {d} 天没回音",
                    f"{a.company or '未知公司'}，已投递 {a.applied_at}，"
                    f"期间{'跟进过 ' + str(a.followup_count) + ' 次' if a.followup_count else '没跟进过'}",
                    a.row_id, d,
                    "主动联系 HR 问进度；很多公司不催不问就没下文",
                ))
            elif d >= rule_followup:
                out.append(Reminder(
                    "soon", f"「{a.position}」投了 {d} 天，可以跟一下",
                    f"{a.company or '未知公司'}，建议发条消息问投递结果",
                    a.row_id, d,
                    "礼貌问一句投递进展，不会被减分",
                ))

        elif st == ApplyStatus.ASSESSMENT.value:
            out.append(Reminder(
                "soon", f"「{a.position}」笔试进行中",
                f"{a.company or '未知公司'}，记得确认笔试截止时间",
                a.row_id, 0,
                "看清笔试时长与提交截止，别错过",
            ))

        elif st == ApplyStatus.INTERVIEW.value:
            since = a.days_since_followup()
            if since is None or since >= RULE_INTERVIEW_STALE:
                gap = f"已 {since} 天没记录" if since is not None else "还没记录下一步"
                out.append(Reminder(
                    "urgent", f"「{a.position}」面试中，{gap}",
                    f"{a.company or '未知公司'}，面试后一定要记录进展和下一轮时间",
                    a.row_id, since or 0,
                    "面试完当天就写下：问了什么、下轮什么时候",
                ))

        elif st == ApplyStatus.OFFER.value:
            d = a.days_since_applied()
            if d >= RULE_OFFER_DECIDE:
                out.append(Reminder(
                    "soon", f"「{a.position}」的 offer 还没答复",
                    f"{a.company or '未知公司'}，已过 {d} 天，别拖到过期",
                    a.row_id, d,
                    "核对用工形式、薪资结构、违约金后再答复",
                ))

    order = {"urgent": 0, "soon": 1, "info": 2}
    out.sort(key=lambda r: (order.get(r.level, 9), -r.days))
    return out


# =============================================================================
# 跟进话术生成（LLM，强制用户确认）
# =============================================================================

FOLLOWUP_SYSTEM = """你正在帮一位求职者写跟进消息。

硬性要求：
1. **绝对不能替用户编造事实**。不能说"我面试很顺利""我非常期待加入"
   这类他没说过的话。只根据给的信息写。
2. 语气专业、简短、不卑微。应届生也不该卑微。
3. 不超过 100 字。飞书/微信消息太长没人看。
4. 只输出 JSON，不要 markdown 代码块。

输出格式：
{
  "message": "跟进消息正文",
  "variants": {"若已过一周": "另一个版本", "若已面试过": "第三个版本"},
  "tips": ["发送前要注意什么"]
}"""


def build_followup_prompt(app: Application, days: int) -> str:
    kind = "投递后长期无回音" if days >= RULE_FOLLOWUP_URGENT else "常规跟进"
    return f"""【情况】
岗位：{app.position}
公司：{app.company or '未知'}
投递日期：{app.applied_at}（距今 {days} 天）
已跟进次数：{app.followup_count}
当前状态：{app.status}
用户备注：{app.note or '（无）'}

请写一条{kind}的消息。中文，不要客套套话，直接说事。"""


# =============================================================================
# 导出
# =============================================================================

def export_applications_csv(path: str, apps: List[Application]) -> str:
    import csv
    cols = list(Application.__dataclass_fields__.keys())
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError, OSError) as e:
        # 实测踩过：用户传的导出路径，其父级其实是个已存在的文件，
        # 直接 mkdir 会抛 FileExistsError 且看不出是路径写错了。
        raise ValueError(f"导出路径不可用（{p.parent} 不是目录）：{e}") from e
    with open(p, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(cols), extrasaction="ignore")
        w.writeheader()
        for a in apps:
            w.writerow(asdict(a))
    return str(p)
