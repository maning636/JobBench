# -*- coding: utf-8 -*-
"""
求职工作台 · 领域模型（唯一字段源）
=====================================

**本文件是全项目唯一允许定义字段的地方。**
SQLite DDL、CSV 表头、JSON Schema、Web API 响应体全部由这里派生。
任何模块手写第二处字段名 → `tests/test_contracts.py` 立即失败。

设计原则（对应 v1.2.0 的四条病根）：
1. 一个字段只有一个名字 —— v1.2.0 的采集器给 `title`、数据库读 `position`，
   空串通过 NOT NULL 所以不报错，数据静默消失。
2. 缺失必须可见 —— `salary_state` 强制标注薪资是"已验证/启发式/未解析"，
   绝不允许把猜测值当事实输出（v1.2.0 实测把猜测表当事实输出 '01-34K'）。
3. 纯数据、零 IO、零第三方依赖 —— 保证契约测试能零成本运行。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "2.0.0"


# =============================================================================
# 枚举
# =============================================================================

class SalaryState(str, Enum):
    """薪资可信度三态 —— 这是 v1.2.0 最缺的东西。

    v1.2.0 的教训：字体解码失败时，它把硬编码猜测表当事实输出，
    静默污染了占 25% 权重的评分维度。宁可空着，不可错着。
    """
    VERIFIED = "verified"        # 像素级/结构级校验通过
    HEURISTIC = "heuristic"      # 有猜测成分，UI 必须标注"待确认"
    UNRESOLVED = "unresolved"    # 未解析，不参与薪资维度评分
    NA = "na"                    # 岗位本身不标薪资（如"面议"、薪资面议）


class JobStatus(str, Enum):
    NEW = "new"
    INTERESTED = "interested"    # 感兴趣
    APPLIED = "applied"          # 已投递
    REJECTED = "rejected"        # 已拒绝
    IGNORED = "ignored"          # 不感兴趣
    ARCHIVED = "archived"


class Channel(str, Enum):
    MANUAL = "manual"            # 手动粘贴 / CSV 导入
    BOSS = "boss"
    ZHILIAN = "zhilian"
    JOB51 = "job51"
    LIEPIN = "liepin"


# 投递状态机（吸收 v1.2.0 的社招 5 态 + 校招 11 态，统一成一张表）
APPLICATION_STATES = [
    "saved", "applied", "assessment", "written_test", "group_interview",
    "tech_interview", "hr_interview", "medical_check", "publicity",
    "three_agreement", "offered", "rejected", "withdrawn",
]


# =============================================================================
# Job —— 岗位
# =============================================================================

@dataclass
class Job:
    """岗位。字段名在本文件定，全项目不再出现第二个名字。"""

    # --- 身份 ---
    channel: str = Channel.MANUAL.value
    external_id: str = ""              # 平台原始岗位 ID（抓取时拿不到则留空）
    fingerprint: str = ""               # 去重指纹，见 compute_fingerprint()

    # --- 核心内容（这四个字段是完整率看门狗的必检项）---
    position: str = ""                  # 岗位名（旧项目里被叫过 title，已废弃）
    company: str = ""                   # 公司名
    salary_text: str = ""               # 原始薪资文本，如 "10-15K·13薪"
    city: str = ""

    # --- 薪资解析结果 ---
    salary_min: Optional[int] = None    # 数值单位见 salary_period
    salary_max: Optional[int] = None
    salary_period: str = ""             # 月 / 日 / 年 —— 实习日薪不得当月薪参与评分
    salary_state: str = SalaryState.UNRESOLVED.value

    # --- 详情 ---
    district: str = ""
    experience: str = ""                # "3-5年" / "经验不限" / "在校"
    education: str = ""                 # "本科" / "硕士" / "大专" / "不限"
    company_size: str = ""
    company_industry: str = ""
    employment_type: str = ""           # 全职 / 实习 / 兼职
    jd: str = ""
    tags: List[str] = field(default_factory=list)
    url: str = ""

    # --- 校园招聘专用 ---
    campus_batch: str = ""              # 提前批 / 秋招 / 春招 / 社招
    first_seen_at: str = ""             # 首次发现时间 —— "这岗位挂了几天了"
    deadline_hint: str = ""             # 网申截止提示（由 LLM 从 JD 抽取）

    # --- 评估 ---
    score: Optional[float] = None       # 综合分 0-100，由 scoring.from_breakdown 换算
    score_details: Dict[str, Any] = field(default_factory=dict)
    match_score: Optional[float] = None
    is_outsourcing_risk: Optional[bool] = None
    outsourcing_evidence: List[str] = field(default_factory=list)

    # --- 状态 ---
    status: str = JobStatus.NEW.value
    note: str = ""

    created_at: str = ""
    updated_at: str = ""

    # ------------------------------------------------------------------
    # 行为
    # ------------------------------------------------------------------

    def __post_init__(self):
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self.created_at = self.created_at or now
        self.updated_at = self.updated_at or now
        self.first_seen_at = self.first_seen_at or self.created_at
        self.salary_state = self._derive_salary_state()
        if not self.fingerprint:
            self.fingerprint = self.compute_fingerprint()

    def finalize(self) -> "Job":
        """所有字段赋值完成后调用，重算派生值。

        **为什么必须显式调用**：`__post_init__` 在构造那一刻就固化了 fingerprint，
        而解析器普遍是 `Job(channel=...)` 先建空对象、再逐字段赋值。
        那样算出的指纹来自空值 —— 后果是所有岗位指纹相同，
        去重把整个库折叠成一条，且不报任何错。

        这个 bug 是在 v2 种子数据阶段实测抓到的（12 个岗位指纹全等），
        对应 v1.2.0「不同岗位被误判重复」的同一类缺陷。
        """
        self.salary_state = self._derive_salary_state()
        self.fingerprint = self.compute_fingerprint()
        return self

    def compute_fingerprint(self) -> str:
        """去重指纹。

        v1.2.0 用 `title|company` 去重，而 company 是从岗位标题里正则猜出来的
        （实测存的是"5-10年"这种经验值），导致不同岗位被误判重复。
        这里改为：优先 平台+平台ID；退化为 公司+岗位名+城市 的稳定组合。
        """
        if self.external_id:
            raw = f"{self.channel}:{self.external_id}"
        else:
            norm = lambda s: re.sub(r"\s+", "", (s or "")).lower()
            raw = f"{self.channel}:{norm(self.company)}:{norm(self.position)}:{norm(self.city)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def _derive_salary_state(self) -> str:
        if self.salary_state not in [e.value for e in SalaryState]:
            return SalaryState.UNRESOLVED.value
        if not self.salary_text:
            return SalaryState.UNRESOLVED.value
        # 只有真的解出了数值才可能 VERIFIED
        if self.salary_state == SalaryState.VERIFIED.value and self.salary_min is None:
            return SalaryState.HEURISTIC.value
        return self.salary_state

    # ------------------------------------------------------------------
    # 完整率看门狗
    # ------------------------------------------------------------------

    REQUIRED_FIELDS = ("position", "company", "salary_text", "city")
    WEIGHTED_FIELDS = ("position", "company", "salary_text", "city", "jd")

    def missing_required(self) -> List[str]:
        """必填字段缺失清单。"""
        return [f for f in self.REQUIRED_FIELDS if not getattr(self, f, "").strip()]

    def completeness(self) -> float:
        """加权完整度 0.0~1.0。

        v1.2.0 的随包演示数据 15 条岗位 salary 全空、company 存的是经验值，
        却照样在文档里宣称"找到 15 个岗位"。这里让数据质量变成一个能看的数字。
        """
        if not self.WEIGHTED_FIELDS:
            return 0.0
        filled = sum(1 for f in self.WEIGHTED_FIELDS if getattr(self, f, "").strip())
        return round(filled / len(self.WEIGHTED_FIELDS), 3)

    def is_salary_trustworthy(self) -> bool:
        """薪资是否可用于评分。

        v1.2.0 把猜测值当事实喂给占 25% 权重的评分维度。
        这里：只有 VERIFIED 才能进薪资评分。
        """
        return self.salary_state == SalaryState.VERIFIED.value and self.salary_min is not None

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["completeness"] = self.completeness()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def field_names(cls) -> List[str]:
        return [f.name for f in fields(cls)]


# =============================================================================
# SQL DDL —— 由领域模型派生，不手写
# =============================================================================

_SQL_TYPES = {
    "str": "TEXT", "int": "INTEGER", "float": "REAL", "bool": "INTEGER",
    "Optional[int]": "INTEGER", "Optional[float]": "REAL",
    "List[str]": "TEXT", "Dict[str, Any]": "TEXT",
    "Optional[bool]": "INTEGER",
}

DEFAULT_VALUES = {
    "str": "''", "int": "0", "float": "0.0", "bool": "0",
    "Optional[int]": "NULL", "Optional[float]": "NULL",
    "List[str]": "'[]'", "Dict[str, Any]": "NULL", "Optional[bool]": "NULL",
}


def sql_type_of(annotation: Any) -> str:
    """把类型注解转成 SQLite 列类型。**全项目唯一实现**。

    踩过的坑：`from __future__ import annotations` 会让所有注解变成字符串，
    所以**不能用 `f.type in ("int",)` 这种字面量比较** —— 那样全部失配，
    所有列都建成 TEXT，布尔值存进去变字符串，读出来 `bool('0')` 也是 True。
    必须走这张查表。这也是 R1「一个定义只写一处」的延伸：
    类型映射曾经在 models.py 和 tracking_store.py 各写一份，后者是错的。
    """
    a = str(annotation).replace("typing.", "").strip()
    return _SQL_TYPES.get(a, "TEXT")


def default_value_of(annotation: Any) -> str:
    a = str(annotation).replace("typing.", "").strip()
    return DEFAULT_VALUES.get(a, "''")


def _sql_type(annotation: str) -> str:
    return sql_type_of(annotation)


def _default_clause(annotation: str) -> str:
    return default_value_of(annotation)


def build_ddl(table: str = "jobs") -> str:
    """从 Job 领域模型生成 CREATE TABLE 语句。

    这是 R1「单一字段源」的强制点：改了 models.py，DB 结构自动跟着变，
    不存在"忘了同步 DDL"这种可能。

    注意：可空列（Optional，默认值为 NULL）不写 NOT NULL，
    否则会生成 `NOT NULL DEFAULT NULL` 这种自相矛盾的列定义。
    """
    cols = []
    for f in fields(Job):
        ann = f.type
        default = _default_clause(ann)
        nullability = "" if default == "NULL" else "NOT NULL"
        cols.append(f"    {f.name} {_sql_type(ann)} {nullability} DEFAULT {default}".replace("  ", " "))
    body = ",\n".join(cols)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n{body}\n)"


def build_csv_columns() -> List[str]:
    """导出列顺序（领域模型顺序 + 派生列）。"""
    return Job.field_names() + ["completeness"]


# =============================================================================
# JSON Schema —— 对外契约
# =============================================================================

_JSON_TYPES = {
    "str": "string", "int": "integer", "float": "number", "bool": "boolean",
    "Optional[int]": ["integer", "null"], "Optional[float]": ["number", "null"],
    "Optional[bool]": ["boolean", "null"],
    "List[str]": {"type": "array", "items": {"type": "string"}},
    "Dict[str, Any]": {"type": ["object", "null"]},
}


def build_json_schema() -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    required: List[str] = []
    for f in fields(Job):
        ann = str(f.type).replace("typing.", "").strip()
        props[f.name] = {"type": _JSON_TYPES.get(ann, "string")}
        if ann in ("str",) and f.name in Job.REQUIRED_FIELDS:
            required.append(f.name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://jobforge.local/schema/job/{SCHEMA_VERSION}.json",
        "title": "求职工作台岗位",
        "x-schema-version": SCHEMA_VERSION,
        "type": "object",
        "properties": props,
        "required": required,
    }


# =============================================================================
# 薪资解析（中文招聘薪资文本 → 数值）
# =============================================================================

_SALARY_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s*[-~—至到]\s*(?P<hi>\d+(?:\.\d+)?)\s*([kK])"
)
_SINGLE_RE = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*[-~—]?\s*([kK])")
# 无单位数值区间：日薪去掉"元/天"后剩 "200-300"
_PLAIN_RANGE_RE = re.compile(r"(?P<lo>\d{2,6})\s*[-~—至到]\s*(?P<hi>\d{2,6})(?!\s*[kK])")
# 去单位后剩下的裸单值：「160元/天」去掉「元/天」后剩 "160"
#
# 实测踩过两次：
#   ① 最初没有它，「160元/天」「100元/小时」整条解析不出来 ——
#      而兼职岗位绝大多数就是这种写法。
#   ② 修的时候把它放在区间规则**前面**，结果 "5000-8000元/月"
#      被单值规则吃掉上限、解析成 8000/8000 —— 下限直接丢了，比不修更糟。
# 所以它必须排在最后：区间、K 值都认完了，剩下的裸数字才轮到它。
_BARE_SINGLE_RE = re.compile(r"(?<![\d.\-~—至到])\b(?P<v>\d{2,6})(?:\.\d+)?\b(?!\s*[-~—至到])")


def parse_salary(text: str) -> Dict[str, Any]:
    """解析中文薪资文本。

    返回 {min, max, period, state}。**解析不出来就是 UNRESOLVED，不猜。**
    这是对 v1.2.0「猜测表当事实」的直接否定。

    实习日薪（200-300元/天）与月薪语义完全不同，绝不折算成月薪参与评分。
    """
    text = (text or "").strip()
    if not text:
        return {"min": None, "max": None, "period": "",
                "state": SalaryState.UNRESOLVED.value}
    if any(k in text for k in ("面议", "不限", "议")):
        return {"min": None, "max": None, "period": "", "state": SalaryState.NA.value}

    # 周期：日薪优先识别，避免 200-300元/天 被当成月薪
    #
    # ⚠「时」必须排在「月」之前查：「35元/时」是小时薪，
    # 而「时」字面里没有「月」，但下面月份分支会先命中"每月/月"——
    # 实测踩过：「35元/时」被识别成月薪，整个时薪折算全错。
    period = "月"
    if "元/天" in text or "/天" in text or "每天" in text or "元每天" in text:
        period = "日"
    elif ("元/小时" in text or "/小时" in text or "时薪" in text
          or "元/时" in text or "/时" in text or "元/时薪" in text):
        period = "小时"
    elif "元/年" in text or "/年" in text or "年薪" in text:
        period = "年"

    # 去掉周期单位。**两条都必须作用在 plain 上，不能都作用在 text 上。**
    # 实测踩过：第二条写成 re.sub(..., text)，把第一条算好的 plain 又覆盖回原文，
    # 结果「160元/天」整个解析失败 —— 而「160-200元/天」碰巧还能用
    # （数字在「元」前面，正则照样搜得到），所以只在单值格式上暴露，很难查。
    plain = re.sub(r"元\s*/\s*(天|日|小时|月|年|时)", " ", text)
    plain = re.sub(r"元\s*每\s*(天|日|小时|月|年|时)", " ", plain)
    plain = plain.replace("每天", " ").replace("时薪", " ")

    # 单值带单位：「160元/天」「100元/小时」「35元/时」
    #
    # 走「去掉单位后的裸单值」这条路：160元/天 → 160。
    # **必须排在区间之后** —— 顺序反了会把 5000-8000 解析成 8000/8000。
    m = _SALARY_RE.search(plain)
    if m:
        k = 1000 if m.group(3).lower() == "k" else 1
        lo = int(float(m.group("lo")) * k)
        hi = int(float(m.group("hi")) * k)
        return {"min": min(lo, hi), "max": max(lo, hi), "period": period,
                "state": SalaryState.VERIFIED.value}

    m = _SINGLE_RE.search(plain)
    if m:
        k = 1000 if m.group(2).lower() == "k" else 1
        v = int(float(m.group(1)) * k)
        return {"min": v, "max": v, "period": period,
                "state": SalaryState.HEURISTIC.value}

    # 无单位数值区间（如日薪去掉周期后剩 "200-300"）
    m = _PLAIN_RANGE_RE.search(plain)
    if m:
        lo, hi = int(m.group("lo")), int(m.group("hi"))
        return {"min": min(lo, hi), "max": max(lo, hi), "period": period,
                "state": SalaryState.VERIFIED.value}

    # 兜底：去掉单位后剩下的裸单值（「160元/天」→ 160）
    # 兼职岗位绝大多数是这种写法，缺了它整个时薪功能就是空的。
    m = _BARE_SINGLE_RE.search(plain)
    if m:
        v = int(float(m.group("v")))
        return {"min": v, "max": v, "period": period,
                "state": SalaryState.HEURISTIC.value}

    return {"min": None, "max": None, "period": period,
            "state": SalaryState.UNRESOLVED.value}


def apply_salary(job: Job) -> Job:
    """把解析结果写回 Job，并同步 salary_state。"""
    r = parse_salary(job.salary_text)
    job.salary_min = r["min"]
    job.salary_max = r["max"]
    job.salary_period = r["period"]
    if job.salary_state != SalaryState.VERIFIED.value or r["state"] == SalaryState.VERIFIED.value:
        job.salary_state = r["state"]
    job.salary_state = job._derive_salary_state()
    return job


def dump_schema(path: Optional[str] = None) -> str:
    """导出 JSON Schema 到磁盘。"""
    s = json.dumps(build_json_schema(), ensure_ascii=False, indent=2)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(s + "\n")
    return s
