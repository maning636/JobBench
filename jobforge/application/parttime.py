# -*- coding: utf-8 -*-
"""
兼职筛选与对比
==============

**为什么不能直接用全职那套 HardFilter。**

全职筛「月薪 ≥ X」是有意义的。兼职筛「月薪 ≥ X」是荒谬的 ——
兼职根本没有月薪，写出来的多半是日薪或者"综合收入"。
所以兼职必须**先折时薪，再按时薪筛**。

而且顺序不能反：一堆岗里可能大部分折不出时薪（JD 没写薪资、或者写"面议"）。
如果先把折不出来的全丢掉，用户会觉得"怎么一个都筛不出来"，
其实只是那批数据本来就不可比 —— 这一点必须明说，不能假装筛过。

**一条硬规则：工时靠猜的时薪不参与"≥ X"的硬筛选。**
理由写在 domain/parttime.py 里了：折算口径不明的数字拿去比大小，
会得出一个看着精确、实际随意的结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from jobforge.domain.parttime import (
    PartTimeReport, Remote, analyze_parttime,
)


@dataclass
class PartTimeFilter:
    """兼职筛选条件。空值 = 不限。"""
    # 时薪下限（元/小时）。⚠ 只对 confidence=stated 的时薪生效
    hourly_min: Optional[float] = None
    # 居家要求
    remote: str = ""                 # "" / full / hybrid / onsite / any_ok
    # 工作内容分类，空 = 不限
    categories: List[str] = field(default_factory=list)
    # 结算周期
    settlement: str = ""             # "" / 日结 / 周结 / 月结
    # 排除有诈骗信号的
    exclude_scams: bool = True
    # 排除「假自由」（居家客服/电销）
    exclude_fake_free: bool = False
    keyword: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hourly_min": self.hourly_min,
            "remote": self.remote,
            "categories": self.categories,
            "settlement": self.settlement,
            "exclude_scams": self.exclude_scams,
            "exclude_fake_free": self.exclude_fake_free,
            "keyword": self.keyword,
        }


def _match_remote(want: str, got: str) -> bool:
    if not want or want == "any":
        return True
    if got == Remote.UNKNOWN:
        # 没说不等于不能 —— 但也不能算命中。放进 kept 但标 unknown，
        # 由上层决定要不要显示。筛选器本身不当它是 yes。
        return False
    if want == "full":
        return got == Remote.FULL
    if want == "hybrid":
        return got in (Remote.HYBRID, Remote.FULL)
    if want == "onsite":
        return got == Remote.ONSITE
    return True


SETTLEMENT_PATTERNS = (
    ("日结", ("日结", "当天结", "做完结", "日清")),
    ("周结", ("周结", "每周结", "一周一结")),
    ("月结", ("月结", "月底结", "每月结", "次月")),
)


def detect_settlement(job) -> str:
    """结算周期。兼职最关心的实际问题之一 —— 日结和月结差着一个月现金流。"""
    blob = f"{getattr(job, 'position', '')} {getattr(job, 'jd', '')}"
    for name, kws in SETTLEMENT_PATTERNS:
        if any(k in blob for k in kws):
            return name
    return ""


def evaluate_parttime(job, f: PartTimeFilter) -> Dict[str, Any]:
    """对单个兼职岗位跑筛选。淘汰必须说得出理由。"""
    rpt: PartTimeReport = analyze_parttime(job)
    reasons: List[str] = []
    notes: List[str] = []

    # ── 诈骗信号：直接淘汰 ──
    if f.exclude_scams and rpt.should_skip:
        scam = [s for s in rpt.signals if s.level == "scam"]
        reasons.append("疑似诈骗信号：" + "；".join(s.title for s in scam[:2]))

    # ── 假自由 ──
    if f.exclude_fake_free and rpt.remote and rpt.remote.fake_free:
        reasons.append("居家但不自由：" + rpt.remote.fake_free_reason[:40])

    # ── 关键词 ──
    if f.keyword:
        blob = (f"{getattr(job, 'position', '')} {getattr(job, 'jd', '')} "
                f"{rpt.category.get('label', '')}").lower()
        if f.keyword.lower() not in blob:
            reasons.append(f"不含关键词「{f.keyword}」")

    # ── 工作内容分类 ──
    if f.categories and rpt.category.get("key") not in f.categories:
        reasons.append(f"工作内容是「{rpt.category.get('label')}」，不在你要的范围")

    # ── 居家 ──
    if f.remote and not _match_remote(f.remote, rpt.remote.level):
        reasons.append(f"居家要求「{f.remote}」，这个岗位是「{rpt.remote.label}」")

    # ── 结算 ──
    if f.settlement:
        got = detect_settlement(job)
        if got != f.settlement:
            reasons.append(f"结算周期不符（要{f.settlement}，实际{got or '没写'}）")

    # ── 时薪：这里是最容易算错的一步 ──
    hourly_kept = True
    if f.hourly_min:
        h = rpt.hourly
        if not h.is_known:
            hourly_kept = False
            reasons.append("时薪算不出来（薪资或工时没写全）")
        elif h.confidence == "assumed":
            # 工时靠猜 → 不参与硬比较。
            # 但**门槛很低时仍然可以留下**：工时假设就算差一倍，
            # 也远在门槛之上，留着比丢掉对用户更有用。
            # 只是要标出来，并标成不可比、不参与排名。
            if h.hourly_min and h.hourly_min >= f.hourly_min * 2:
                notes.append(f"时薪 {h.display()} —— 工时是估的，不参与排名")
            else:
                hourly_kept = False
                reasons.append(
                    f"工时是估的（{h.schedule.evidence}），时薪只能参考不能当筛选用")
        elif h.hourly_max and h.hourly_max < f.hourly_min:
            hourly_kept = False
            reasons.append(f"时薪 {h.display()} 低于你要的 {f.hourly_min:g}")
        elif h.hourly_min < f.hourly_min:
            hourly_kept = False
            reasons.append(f"时薪 {h.display()} 低于你要的 {f.hourly_min:g}")
        else:
            notes.append(f"时薪 {h.display()}")

    if not rpt.remote or rpt.remote.level == Remote.UNKNOWN:
        notes.append("JD 没写能不能居家")

    return {
        "pass": not reasons,
        "reasons": reasons,
        "notes": notes,
        "report": rpt,
        # 时薪不可比时，排序权重要降下来
        "sort_key": (rpt.hourly.mid if (rpt.hourly and rpt.hourly.comparable) else None),
    }


def filter_parttime(jobs, f: PartTimeFilter) -> Dict[str, Any]:
    """批量筛选。返回结果里必须说清楚"有多少条因为时薪算不出来被排除在外"。"""
    kept, dropped, pending = [], [], []

    for j in jobs:
        r = evaluate_parttime(j, f)
        if r["pass"]:
            kept.append({"job": j, **{k: v for k, v in r.items() if k != "report"},
                          "report": r["report"]})
        else:
            item = {"job": j, **{k: v for k, v in r.items() if k != "report"},
                    "report": r["report"]}
            # 「时薪算不出来」和「时薪不够」是两回事，必须分开，
            # 否则用户会以为这些岗真的不达标
            if any("算不出来" in x or "估的" in x for x in r["reasons"]):
                pending.append(item)
            dropped.append(item)

    # 排序：能算准时薪的按时薪降序；算不出的放最后但仍可见
    kept.sort(key=lambda x: (x["sort_key"] is not None,
                             -(x["sort_key"] or 0)))

    return {
        "kept": kept,
        "dropped": dropped,
        # 时薪不可比、只是没法参与筛选的那些
        "pending": pending,
        "pending_count": len(pending),
        "stats": {
            "total": len(jobs),
            "kept": len(kept),
            "dropped": len(dropped),
            "comparable": sum(1 for k in kept if k["sort_key"] is not None),
        },
    }


# =============================================================================
# 对比
# =============================================================================

def compare_parttime(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """兼职对比表。

    **只拿可比的数据做结论。** 时薪是"assumed"的行，
    单独归到"仅供参考"区，不进主表 —— 否则对比出来的名次没有意义。
    """
    comparable = [i for i in items if i.get("sort_key") is not None]
    assumed = [i for i in items if i.get("sort_key") is None]

    rows = []
    for i in sorted(comparable, key=lambda x: -(x["sort_key"] or 0)):
        rpt: PartTimeReport = i["report"]
        h = rpt.hourly
        rows.append({
            "job_id": getattr(i["job"], "db_id", None) or getattr(i["job"], "id", None),
            "position": getattr(i["job"], "position", ""),
            "company": getattr(i["job"], "company", ""),
            "city": getattr(i["job"], "city", ""),
            "hourly_min": h.hourly_min,
            "hourly_max": h.hourly_max,
            "hourly_display": h.display(),
            "hourly_confidence": h.confidence,
            "basis": h.basis,
            "remote": rpt.remote.label,
            "remote_level": rpt.remote.level,
            "fake_free": rpt.remote.fake_free,
            "category": rpt.category.get("label", ""),
            "category_caution": rpt.category.get("caution", ""),
            "settlement": detect_settlement(i["job"]),
            "scam_level": rpt.scam_level,
            "signals": [s.to_dict() for s in rpt.signals[:3]],
        })

    explain = _explain(rows, assumed)
    return {
        "rows": rows,
        "assumed_only": [
            {
                "job_id": getattr(i["job"], "db_id", None),
                "position": getattr(i["job"], "position", ""),
                "hourly_display": i["report"].hourly.display(),
                "why": i["report"].hourly.basis,
            }
            for i in assumed
        ],
        "explain": explain,
        "note": ("时薪按默认工时估算的行不参与排名 —— 口径不明的数字比大小，"
                 "得出的名次没有意义。"),
    }


def _explain(rows: List[Dict[str, Any]], assumed: List[Dict[str, Any]]) -> str:
    if not rows:
        return ("这些岗位都算不出可比的时薪（JD 里通常不写每天几小时），"
                "没法给你排名。你可以先把它们挑出来问问 HR。")

    best = rows[0]
    lines = [f"按时薪算，{best['position']} 最高：{best['hourly_display']}。"]

    full = [r for r in rows if r["remote_level"] == Remote.FULL]
    if full:
        lines.append(f"其中 {len(full)} 个能完全居家，通勤成本按 0 算 —— "
                     f"实际时薪比表上看到的还高。")

    fake = [r for r in rows if r["fake_free"]]
    if fake:
        lines.append(f"⚠ 有 {len(fake)} 个写着「居家」但其实是客服/电销，"
                     f"要长时间在线，不等于自由。")

    if assumed:
        lines.append(f"另有 {len(assumed)} 个因为没写工时、没法折算，"
                     f"没有参与排名 —— 补上工时就能比了。")
    return " ".join(lines)
