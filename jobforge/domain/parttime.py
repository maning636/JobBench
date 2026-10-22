# -*- coding: utf-8 -*-
"""
兼职域 —— 时薪、居家办公、工作内容分类、陷阱信号
====================================================

**为什么兼职不能复用全职那套判据。**

全职比的是「月薪 + 岗位要求」。兼职比的是**完全不同的三件事**：

  1. **时薪**，不是月薪。
     日薪 200 元/天、上班 8 小时 = 25 元/小时；
     月薪 5000、上班 22 天 × 8 小时 = 28.4 元/小时。
     数字差 3 倍，方向完全相反 —— 不换算就比，看到「200」以为比「5000」高。
     而兼职最大的坑恰恰是**工时不写**：写「200-400元/天」却不说每天几小时。

  2. **能不能居家**。
     对学生来说这常常比时薪更决定可行性 —— 通勤 1.5 小时做时薪 25 的家教，
     实际时薪算上通勤可能还不如时薪 18 的线上客服。
     但「居家」有真假之分：居家客服、居家电话销售听着自由，实际要全天在线。

  3. **工作内容是什么**。
     「运营助理」这四个字可能是真的做数据，也可能是拉人头。
     不分类就没法比 —— 一个「客服」和一个「家教」不该放在一张时薪榜里比。

────────────────────────────────────────────────────────────────────────
本模块的三条硬约束
────────────────────────────────────────────────────────────────────────

1. **工时是猜的就必须标成猜的。**
   `hourly_confidence` 永远存在。JD 明写「每天6小时」= stated；
   我们按默认 8 小时折算的 = assumed。
   **assumed 的时薪不允许拿去跟别的岗位做硬比较**，只能排序参考。
   宁可让用户知道「这个数不准」，也不要给一个看起来精确的错数。

2. **分类和居家判定都必须带证据原文。**
   拿不出证据就不判定（沿用岗位可信度那条纪律）。

3. **陷阱信号宁可多报，不可漏报。**
   兼职诈骗的损失（押金、无偿培训）是真金白银。
   这里允许「宁可误报」—— 漏报一条可能被骗钱，多报一条最多少看一个岗。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# 一、工时口径
# =============================================================================

# 每天默认 8 小时、每月默认出勤 21.75 天。
#
# ⚠ 21.75 的来历要说清楚，因为它经常被误解：
#   (365 - 104) / 12 = 21.75，这是《劳动法》规定的**月计薪天数**，
#   用来算加班费的，不是"实际上班 21.75 天"。
#   实际上班通常是 21~22 天，两天之差 ≈ 9% 时薪 —— 足以改变排名。
#   所以 JD 没说出勤天数时，必须把 21.75 当**默认假设**标出来，而不是当成事实。
DEFAULT_HOURS_PER_DAY = 8.0
DEFAULT_DAYS_PER_MONTH = 21.75

# 常见的其它工时口径（兼职里真的有人这么写）
HOURS_OPTIONS = (2, 3, 4, 5, 6, 7, 8, 10, 12)


@dataclass
class WorkSchedule:
    """从 JD 里抽出来的工时信息。每项都要标来源。"""
    hours_per_day: Optional[float] = None
    days_per_month: Optional[float] = None
    # stated = JD 明写；assumed = 我们按默认折算
    source: str = "assumed"
    evidence: str = ""

    @property
    def hours_per_month(self) -> float:
        h = self.hours_per_day or DEFAULT_HOURS_PER_DAY
        d = self.days_per_month or DEFAULT_DAYS_PER_MONTH
        return h * d


def extract_schedule(jd: str) -> WorkSchedule:
    """从 JD 文本抽工时。抽不到就交出 assumed，并说清楚用了什么默认。"""
    text = str(jd or "")
    sch = WorkSchedule()
    ev: List[str] = []

    # 每天几小时。中文有两种完全不同的语序，都得认：
    #   ① 「每天工作6小时」—— 数字在「每天」后面
    #   ② 「6小时/天」      —— 数字在「天」前面
    # 实测踩过：最初只写了 ②，结果「每天6小时」「每天12小时」
    # 一条都抽不出来 → 整条时薪直接算不出来。这不是边界情况，
    # 「每天X小时」是兼职 JD 里最主流的写法。
    hours = None
    m = re.search(r"(?:每天|每日)\s*(?:工作|上班|出勤)?\s*"
                  r"([0-9]+(?:\.[0-9]+)?)\s*(?:个)?\s*小时", text)
    if m:
        hours = float(m.group(1))
    else:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:个)?\s*小时\s*"
                      r"(?:／|/|每)\s*(?:天|日|一天)", text)
        if m:
            hours = float(m.group(1))
        else:
            m = re.search(r"(?:工作时长?|时长)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*小时", text)
            if m:
                hours = float(m.group(1))

    # 只接受真实存在过的排班时长。兼职里出现过 13 小时这种吗？没有。
    # 不在名单里的宁可当没写（走 assumed），也不要折出一个假数字。
    if hours is not None and hours in [float(h) for h in HOURS_OPTIONS]:
        sch.hours_per_day = hours
        ev.append(m.group(0).strip())

    # 「每周 N 天」
    m2 = re.search(r"每周\s*([0-9]+)\s*天", text)
    if m2:
        n = int(m2.group(1))
        sch.days_per_month = n * 4.33      # 52 周 / 12 月 ≈ 4.33
        ev.append(m2.group(0))

    # 「每月 N 天」
    m3 = re.search(r"每月\s*([0-9]+)\s*天", text)
    if m3:
        sch.days_per_month = float(m3.group(1))
        ev.append(m3.group(0))

    # 「上N班休N天」—— 兼职里极常见的排班写法
    m4 = re.search(r"上\s*([0-9]+)\s*班?休\s*([0-9]+)\s*天", text)
    if m4:
        work, rest = int(m4.group(1)), int(m4.group(2))
        if work + rest > 0:
            sch.days_per_month = round(30.0 * work / (work + rest), 1)
            ev.append(m4.group(0))

    if ev:
        sch.source = "stated"
        sch.evidence = "；".join(ev)
    else:
        sch.source = "assumed"
        sch.evidence = f"按默认每天 {DEFAULT_HOURS_PER_DAY:g} 小时、每月 {DEFAULT_DAYS_PER_MONTH} 天折算"
    return sch


# =============================================================================
# 二、时薪归一化
# =============================================================================

@dataclass
class HourlyRate:
    """时薪。**confidence 是这个对象里最重要的字段。**"""
    hourly_min: Optional[float] = None
    hourly_max: Optional[float] = None
    confidence: str = "unknown"      # stated / assumed / unknown
    schedule: Optional[WorkSchedule] = None
    # 折算过程写出来给用户看。看不见中间步骤的数字没人敢信。
    basis: str = ""
    comparable: bool = True          # False 时不允许参与硬比较/排名

    @property
    def is_known(self) -> bool:
        return self.hourly_min is not None and self.hourly_min > 0

    @property
    def mid(self) -> Optional[float]:
        if self.hourly_min is None:
            return None
        if self.hourly_max is None:
            return self.hourly_min
        return round((self.hourly_min + self.hourly_max) / 2, 1)

    def display(self) -> str:
        if not self.is_known:
            return "时薪算不出来"
        if self.hourly_max and self.hourly_max > self.hourly_min:
            s = f"{self.hourly_min:.0f} ~ {self.hourly_max:.0f} 元/小时"
        else:
            s = f"{self.hourly_min:.0f} 元/小时"
        if self.confidence == "assumed":
            s += "（工时是估的，仅供参考）"
        return s

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hourly_min": self.hourly_min,
            "hourly_max": self.hourly_max,
            "mid": self.mid,
            "confidence": self.confidence,
            "basis": self.basis,
            "comparable": self.comparable,
            "display": self.display(),
            "schedule": ({"hours_per_day": self.schedule.hours_per_day,
                         "days_per_month": self.schedule.days_per_month,
                         "source": self.schedule.source,
                         "evidence": self.schedule.evidence}
                        if self.schedule else None),
        }


def compute_hourly(job) -> HourlyRate:
    """把任何口径的薪资折成时薪。

    处理三种情况：
      · 日薪 → 需要每天小时数
      · 月薪 → 需要每月天数 × 每天小时数
      · 年薪 → 先折月薪再折时薪
      · 小时薪（"30元/小时"）→ 直接用
    任何一步缺必要输入，**宁可返回 unknown，不硬凑一个数**。
    """
    text = str(getattr(job, "salary_text", "") or "")
    period = str(getattr(job, "salary_period", "") or "")
    lo = getattr(job, "salary_min", None)
    hi = getattr(job, "salary_max", None)
    schedule = extract_schedule(getattr(job, "jd", "") or "")

    # ── 小时薪 ──
    # ⚠ 区间部分必须可选。最初写成「数字 - 数字 元/小时」，
    # 结果「50元/小时」这种单值写法匹配不上，整条掉进"认不出来"。
    # 兼职里单值时薪远比区间常见（"50元/小时" 远多于 "35-50元/小时"）。
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:[-~—至到]\s*([0-9]+(?:\.[0-9]+)?)\s*)?"
                  r"元?\s*(?:／|/|每)?\s*(?:小时|时薪|时)", text)
    if m:
        a = float(m.group(1))
        b = float(m.group(2)) if m.group(2) else a
        return HourlyRate(hourly_min=min(a, b), hourly_max=max(a, b),
                          confidence="stated", schedule=schedule,
                          basis=f"JD 直接写明小时薪：{m.group(0)}",
                          comparable=True)

    if not lo or lo <= 0:
        return HourlyRate(confidence="unknown", schedule=schedule,
                          basis="没解析出薪资数值，换不成时薪",
                          comparable=False)

    # ── 已经是小时薪（由 parse_salary 判定，最可靠的一条路）──
    # 不只靠上面的正则：正则可能被排版、换行、全角符号打断，
    # 而 salary_period 是解析器给出的明确结论，直接用它兜底。
    if period == "小时":
        return HourlyRate(hourly_min=float(lo),
                          hourly_max=float(hi) if hi else None,
                          confidence="stated", schedule=schedule,
                          basis=f"按小时计薪：{lo} 元/小时",
                          comparable=True)

    # ── 日薪 ──
    if period == "日":
        h = schedule.hours_per_day or DEFAULT_HOURS_PER_DAY
        stated = schedule.source == "stated"
        return HourlyRate(
            hourly_min=round(lo / h, 1),
            hourly_max=round(hi / h, 1) if hi else None,
            confidence="stated" if stated else "assumed",
            schedule=schedule,
            basis=f"日薪 {lo} 元 ÷ 每天 {h:g} 小时"
                  + ("" if stated else f"（每天小时数是按默认 8 估的）"),
            # 工时靠猜的时候，这个数不能拿去跟别人硬比
            comparable=stated)

    # ── 月薪 / 年薪 ──
    if period in ("月", "年"):
        monthly = lo * (12 if period == "年" else 1)
        monthly_hi = (hi * 12 if period == "年" else hi) if hi else None
        hpm = schedule.hours_per_month
        stated = schedule.source == "stated"
        hpd = schedule.hours_per_day or DEFAULT_HOURS_PER_DAY
        dpm = schedule.days_per_month or DEFAULT_DAYS_PER_MONTH
        return HourlyRate(
            hourly_min=round(monthly / hpm, 1),
            hourly_max=round(monthly_hi / hpm, 1) if monthly_hi else None,
            confidence="stated" if stated else "assumed",
            schedule=schedule,
            basis=(f"{'年薪' if period == '年' else '月薪'} {lo} 元 ÷ "
                   f"（每天 {hpd:g} 小时 × 每月 {dpm:g} 天 = {hpm:.1f} 小时）")
                  + ("" if stated else f"（工时是按默认值估的）"),
            comparable=stated)

    # 周期认不出来
    return HourlyRate(confidence="unknown", schedule=schedule,
                      basis=f"薪资周期「{period or '未知'}」认不出来，不敢折算时薪",
                      comparable=False)


# =============================================================================
# 三、居家办公
# =============================================================================

class Remote(str):
    FULL = "full"        # 完全居家，不用到岗
    HYBRID = "hybrid"    # 混合，部分天数居家
    ONSITE = "onsite"    # 明确要求到岗
    UNKNOWN = "unknown"  # 没提


REMOTE_LABEL = {
    Remote.FULL: "完全居家",
    Remote.HYBRID: "可混合办公",
    Remote.ONSITE: "需要到岗",
    Remote.UNKNOWN: "没说能不能居家",
}

_FULL_REMOTE = (
    "居家办公", "居家", "远程办公", "远程", "线上办公", "在家办公", "在家",
    "不用到岗", "无需到岗", "不坐班", "自由办公", "wfh", "work from home",
    "remote", "线上即可", "线上完成", "线上接单",
)
_HYBRID = ("混合办公", "弹性办公", "可居家", "部分居家", "每周到岗", "灵活坐班")
_ONSITE = (
    "坐班", "到岗", "坐班制", "门店", "出镜", "到店", "驻场", "现场",
    "面对面", "线下", "必须到岗",
)

# ★ 「居家」的反面陷阱：这些词带「居家/线上/远程」，但实际是全职在线的活，
#   学生常常以为很自由就接了，结果要随时接电话。必须单独标出来。
FAKE_FREE = (
    "居家客服", "居家电销", "电话销售", "在线客服", "话务", "呼叫中心",
    "外呼", "在线销售", "居家推广", "微商",
)


@dataclass
class RemoteInfo:
    level: str = Remote.UNKNOWN
    evidence: str = ""
    # 「居家但其实是坐席型」—— 看着自由，实际要随时在线
    fake_free: bool = False
    fake_free_reason: str = ""
    # 通勤成本提示（不参与计算，只做提醒）
    commute_hint: str = ""

    @property
    def label(self) -> str:
        return REMOTE_LABEL.get(self.level, "没说能不能居家")

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "label": self.label,
                "evidence": self.evidence,
                "fake_free": self.fake_free,
                "fake_free_reason": self.fake_free_reason,
                "commute_hint": self.commute_hint}


def detect_remote(job) -> RemoteInfo:
    """判定能不能居家。**必须带证据，拿不出证据就是 unknown。**"""
    jd = str(getattr(job, "jd", "") or "")
    position = str(getattr(job, "position", "") or "")
    city = str(getattr(job, "city", "") or "")
    blob = f"{position} {jd}".lower()

    info = RemoteInfo()

    # 1) 先抓「假自由」—— 顺序很重要，否则会被上面的"居家"盖掉
    for k in FAKE_FREE:
        if k in blob:
            info.fake_free = True
            info.fake_free_reason = (
                f"写着「{k}」，但这类岗位通常要长时间在线、随时接电话，"
                f"不算真正的自由。接之前问清楚：每天要在线几个小时、有没有考核")
            break

    # 2) 再判级别
    #
    # ⚠ 三件事曾经把这里判错过，都是往宽松了骗，比判错更糟：
    #
    #   a)「无需到岗」里含「到岗」—— 否定义被当成"必须到岗"。
    #      所以先把否定式从文本里剔除，再做关键词扫描。
    #   b)「支持居家办公，每周到岗3天」这种最典型的混合排班，
    #      原来因为「到岗」先命中被判成"完全居家"。
    #      两者并存恰恰说明是混合，必须判 hybrid。
    #   c)「培训」这类词在正常岗位也会出现，不能一见就判诈骗。
    _neg = re.sub(r"(无需|不用|不必|免|无须|无需到岗)(到岗|坐班|到店|到场)", " ", blob)
    for _k in ("无需到岗", "不用到岗", "不必到岗", "无需坐班", "不用坐班"):
        _neg = _neg.replace(_k, "居家")   # 否定"到岗"本身就是"居家"的证据

    _hit_onsite = next((k for k in _ONSITE if k in _neg), "")
    _hit_remote = next((k for k in _FULL_REMOTE if k in blob), "")
    _hit_hybrid = next((k for k in _HYBRID if k in blob), "")

    if _hit_hybrid:
        info.level = Remote.HYBRID
        info.evidence = _hit_hybrid
    elif _hit_onsite and _hit_remote:
        info.level = Remote.HYBRID
        info.evidence = f"{_hit_remote} + {_hit_onsite}"
    elif _hit_onsite:
        info.level = Remote.ONSITE
        info.evidence = _hit_onsite
    elif _hit_remote:
        info.level = Remote.FULL
        info.evidence = _hit_remote
    else:
        info.level = Remote.UNKNOWN
        info.evidence = ""

    # 3) 通勤提醒：这是学生选兼职最实际的约束
    if info.level == Remote.ONSITE and city:
        info.commute_hint = f"需要到岗（在{city}），通勤时间记得算进实际时薪"

    return info


# =============================================================================
# 四、工作内容分类
# =============================================================================

@dataclass(frozen=True)
class Category:
    key: str
    label: str
    # 这类活通常能不能居家（先验经验，只作提示不作结论）
    usually_remote: str = ""       # yes / often / rarely / ""
    # 需要特别小心的坑
    caution: str = ""


# 按「你实际在干什么」分类，不按招聘方怎么写岗位名。
# 招聘方会把同一件事写成「运营」「助理」「专员」，所以关键词要覆盖别名。
CATEGORIES: Tuple[Category, ...] = (
    Category("customer_service", "客服 / 坐席",
             usually_remote="often", caution="常见「先培训几天不给钱」，问清楚有无偿期"),
    Category("sales", "销售 / 电销 / 推广",
             usually_remote="often", caution="底薪可能为 0，全靠提成；问清提成怎么算、什么时候结"),
    Category("teaching", "家教 / 教育辅导",
             usually_remote="sometimes", caution="注意「先交培训费再上岗」是明确的红线"),
    Category("design", "设计 / 文案 / 翻译",
             usually_remote="often", caution="留意「免费试稿」，写完不采用还白干"),
    Category("dev", "编程 / 开发 / 测试",
             usually_remote="often", caution="要求先做付费作业的，基本可以排除"),
    Category("data", "数据录入 / 标注 / 审核",
             usually_remote="often", caution="单价常被压得很低，注意算时薪"),
    Category("delivery", "配送 / 跑腿 / 代驾",
             usually_remote="never", caution="看有没有社保和油补，纯粹吃身体"),
    Category("retail", "门店 / 导购 / 收银",
             usually_remote="never", caution="注意排班是否占用上课时间，"
                                            "以及有没有社保（兼职通常不交）"),
    Category("content", "摄影 / 摄像 / 剪辑",
             usually_remote="sometimes", caution="设备押金、素材归属要写清楚"),
    Category("promotion", "地推 / 拉新 / 扫码",
             usually_remote="never", caution="很多是传销式拉人头，看到「发展下线」直接跳过"),
    Category("survey", "问卷 / 众包 / 微任务",
             usually_remote="often", caution="单子量不稳定，不适合算稳定收入"),
    Category("admin", "行政 / 文档整理",
             usually_remote="often", caution="通常要求稳定排班，注意是否占用上课时间"),
    Category("event", "模特 / 礼仪 / 演出",
             usually_remote="never", caution="警惕需要交押金或培训费的"),
    Category("tutor_assist", "助教 / 兼职教师",
             usually_remote="sometimes", caution="寒暑假才有，注意是否占用上课时间"),
    Category("other", "其他 / 看不出来",
             caution="分类不出来通常意味着 JD 写得太含糊，先问清楚再决定"),
)

CAT_BY_KEY = {c.key: c for c in CATEGORIES}

# 分类关键词。**顺序有意义**：先匹配特征最强的，
# 否则「线上推广」会先被「推广」抢走，判成推广类而非销售类。
#
# ⚠ 关键词必须写成**元组的元组**。
# 实测踩过：一开始写成 ("sales", "销售电销话务外呼客服") 这种拼接字符串，
# 于是 `any(w in position for w in words)` 实际是在问
# 「整串「销售电销话务外呼客服」是不是岗位名的子集」—— 永远为假，
# **整个内容分类静默失效**。不报错、不崩溃，只是永远判成 other。
_CATEGORY_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # 特征最强、误判代价最大的排前面
    ("sales",            ("销售", "电销", "话务", "外呼", "呼叫")),
    ("customer_service", ("客服", "坐席", "客诉", "在线客", "接待")),
    ("teaching",         ("家教", "教师", "辅导", "授课", "补课")),
    ("tutor_assist",     ("助教",)),
    ("promotion",        ("地推", "拉新", "扫码", "推广专员")),
    ("delivery",         ("配送", "跑腿", "代驾", "外卖", "快递", "骑手")),
    ("retail",          ("店员", "导购", "收银", "营业员", "门店")),
    ("data",             ("标注", "录入", "质检", "审核")),
    ("survey",           ("问卷", "众包", "微任务")),
    ("dev",              ("开发", "编程", "测试", "运维", "算法", "程序")),
    ("design",           ("设计", "文案", "翻译", "美工", "剪辑")),
    ("content",          ("摄影", "摄像", "跟拍")),
    ("admin",            ("行政", "文档整理", "资料整理", "档案整理")),
    ("event",            ("模特", "礼仪", "演出", "主持")),
)


def classify_work(job) -> Dict[str, Any]:
    """给工作内容分类。

    **分不出来就说分不出来**，不要硬凑一个类别 ——
    归错类比不归类更糟，用户会拿错的标准去比。
    """
    position = str(getattr(job, "position", "") or "")
    jd = str(getattr(job, "jd", "") or "")
    etype = str(getattr(job, "employment_type", "") or "")
    blob = f"{position} {jd} {etype}"

    # 兼职岗的岗位名经常就是类别本身，优先只看岗位名，避免 JD 里
    # 顺带提一句"协助运营"就跑偏
    for key, words in _CATEGORY_RULES:
        if any(w in position for w in words):
            return _cat_dict(key, position)

    for key, words in _CATEGORY_RULES:
        if any(w in jd for w in words):
            return _cat_dict(key, position)

    return _cat_dict("other", position)


def _cat_dict(key: str, matched_on: str) -> Dict[str, Any]:
    c = CAT_BY_KEY.get(key, CAT_BY_KEY["other"])
    return {
        "key": c.key,
        "label": c.label,
        "usually_remote": c.usually_remote,
        "caution": c.caution,
        "matched_on": matched_on,
    }


# =============================================================================
# 五、兼职陷阱信号
# =============================================================================

# 分两档：
#   scam  —— 几乎可以直接排除（诈骗高发）
#   warn  —— 要问清楚才能决定
#
# 判断原则：**只根据 JD 里出现的事实，不做推断**。
# 出现"培训费"就是 scam 信号，不需要再证明它一定是骗局 ——
# 正规兼职收培训费这件事本身就不该发生。
SCAM_PATTERNS = (
    ("押金", "要求交押金 / 保证金"),
    ("保证金", "要求交押金 / 保证金"),
    ("培训费", "要求先交培训费 —— 正规兼职从不收费"),
    ("服装费", "要求交服装费 / 工服费"),
    ("器材费", "要求交器材 / 设备押金"),
    ("先充值", "要求先充值 / 预充值"),
    ("发展下线", "出现「发展下线」—— 典型传销话术"),
    ("拉人头", "出现「拉人头」"),
    ("交钱入职", "要求交钱才能入职"),
    ("材料费", "要求交材料费"),
)

WARN_PATTERNS = (
    ("日结", "日结通常是噱头，问清楚结的是哪一天、有没有条件"),
    ("高薪", "「高薪」+「无需经验」组合，报价通常虚高"),
    ("无需经验", "标「无需经验」的岗位要问清楚到底要做什么"),
    ("轻松", "「轻松」往往意味着单量不可控"),
    ("零门槛", "「零门槛」+ 高薪，实际门槛在后面"),
    ("月入过万", "「月入过万」多为头部幸存者案例，不是普遍水平"),
    ("底薪", "问清楚底薪是多少、有没有提成"),
    ("培训", "问清培训期有没有工资、几天"),
    ("无责底薪", "无责底薪之外通常还挂着提成任务"),
    ("综合薪资", "「综合薪资」常含补贴提成，不等于到手"),
)


@dataclass
class PartTimeSignal:
    level: str = "info"      # scam / warn / info
    code: str = ""
    title: str = ""
    evidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "code": self.code, "title": self.title,
                "evidence": self.evidence}


def detect_scams(job) -> List[PartTimeSignal]:
    """兼职陷阱检测。这里允许宁可多报。"""
    blob = f"{getattr(job, 'position', '')} {getattr(job, 'jd', '')}"
    out: List[PartTimeSignal] = []

    for kw, title in SCAM_PATTERNS:
        if kw in blob:
            # 把包含关键词的那一行摘出来当证据
            ev = next((l.strip() for l in str(getattr(job, "jd", "")).splitlines()
                       if kw in l), kw)
            out.append(PartTimeSignal("scam", f"scam:{kw}", title, ev[:120]))

    for kw, title in WARN_PATTERNS:
        if kw in blob:
            ev = next((l.strip() for l in str(getattr(job, "jd", "")).splitlines()
                       if kw in l), kw)
            out.append(PartTimeSignal("warn", f"warn:{kw}", title, ev[:120]))

    return out


# =============================================================================
# 六、综合报告
# =============================================================================

@dataclass
class PartTimeReport:
    """一个兼职岗位的完整评审。"""
    is_parttime: bool = False
    hourly: Optional[HourlyRate] = None
    remote: Optional[RemoteInfo] = None
    category: Dict[str, Any] = field(default_factory=dict)
    signals: List[PartTimeSignal] = field(default_factory=list)

    @property
    def scam_level(self) -> str:
        if any(s.level == "scam" for s in self.signals):
            return "scam"
        if any(s.level == "warn" for s in self.signals):
            return "warn"
        return "clean"

    @property
    def should_skip(self) -> bool:
        """诈骗信号直接建议跳过。"""
        return self.scam_level == "scam"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_parttime": self.is_parttime,
            "hourly": self.hourly.to_dict() if self.hourly else None,
            "remote": self.remote.to_dict() if self.remote else None,
            "category": self.category,
            "signals": [s.to_dict() for s in self.signals],
            "scam_level": self.scam_level,
            "should_skip": self.should_skip,
        }


def is_parttime_job(job) -> bool:
    """判断这是不是兼职岗。

    判定依据要宽：BOSS 上兼职岗位的 `employment_type` 字段经常是空的，
    得从岗位名和 JD 里一起看。
    """
    et = str(getattr(job, "employment_type", "") or "")
    if "兼职" in et:
        return True
    if et in ("全职", "实习"):
        return False

    pos = str(getattr(job, "position", "") or "")
    jd = str(getattr(job, "jd", "") or "")
    if any(k in pos for k in ("兼职", "寒暑假", "周末")):
        return True
    if any(k in jd for k in ("兼职", "每周", "按周结算", "时间灵活", "不影响上课")):
        return True
    # 日薪且不是实习 —— 大概率是兼职
    if str(getattr(job, "salary_period", "") or "") == "日":
        return True
    return False


def analyze_parttime(job) -> PartTimeReport:
    """兼职岗位的完整评审入口。"""
    r = PartTimeReport(is_parttime=is_parttime_job(job))
    r.hourly = compute_hourly(job)
    r.remote = detect_remote(job)
    r.category = classify_work(job)
    r.signals = detect_scams(job)
    return r
