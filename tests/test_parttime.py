# -*- coding: utf-8 -*-
"""
兼职评审测试 —— 时薪 / 居家 / 分类 / 陷阱 / 筛选 / 对比
=======================================================

这个模块最容易出的错，是**看起来算对了，其实口径是错的**。

所以本文件的主体不是"能不能算出 25 元/小时"，而是三件事：

  1. **算不出来的时候必须说算不出来**，不能硬凑一个数
  2. **估出来的时薪不能参与硬比较**（不能拿它去卡「≥ 25」）
  3. **分类和居家判定不许乱认**（宁可不判，也不能判错）

另外单测了工时抽取 —— 「每天4小时」「上5班休2天」「每周3天」
这三种写法折出来完全不同，抽错一个整条时薪就废了。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.application.parttime import (
    PartTimeFilter, compare_parttime, detect_settlement, evaluate_parttime,
    filter_parttime,
)
from jobforge.domain.models import Job, apply_salary
from jobforge.domain.parttime import (
    CATEGORIES, DEFAULT_DAYS_PER_MONTH, Remote, analyze_parttime,
    classify_work, compute_hourly, detect_remote, detect_scams,
    extract_schedule, is_parttime_job,
)

P, F = [], []


def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))


def mk(salary="", jd="", pos="兼职", company="某公司", period=None, etype="兼职"):
    j = Job(position=pos, company=company, salary_text=salary,
            city="北京", jd=jd, employment_type=etype)
    if salary:
        apply_salary(j)
    if period:
        j.salary_period = period
    return j


# =============================================================================
print("\n[A] 工时抽取 —— 抽错一个，整条时薪就废了")
# =============================================================================
s = extract_schedule("每天工作6小时")
check("「每天6小时」能抽到", s.hours_per_day == 6.0, str(s.hours_per_day))
check("抽到了就是 stated", s.source == "stated", s.source)
check("保留证据原文", "6小时" in s.evidence, s.evidence)

s = extract_schedule("每日4小时，按周结算")
check("「每日4小时」能抽到", s.hours_per_day == 4.0, str(s.hours_per_day))

s = extract_schedule("上5班休2天")
check("「上5班休2天」折出每月天数", s.days_per_month and abs(s.days_per_month - 21.4) < 1.5,
      str(s.days_per_month))

s = extract_schedule("每周3天，每天5小时")
check("「每周3天」折算月天数", s.days_per_month and abs(s.days_per_month - 13) < 0.5,
      str(s.days_per_month))
check("「每天5小时」和「每周3天」能同时抽到",
      s.hours_per_day == 5.0 and s.days_per_month is not None, str(s))

s = extract_schedule("招聘销售精英")
check("没写工时时是 assumed", s.source == "assumed", s.source)
check("并说清用了什么默认", "21.75" in s.evidence and "8" in s.evidence, s.evidence)
check("assumed 时 hours_per_day 为 None（不假装是事实）",
      s.hours_per_day is None, str(s.hours_per_day))

# 12 小时一天是合法的排班，不能被当成噪声
s = extract_schedule("每天12小时，月休两天")
check("「每天12小时」是真实存在的排班，要能抽到",
      s.hours_per_day == 12.0, str(s.hours_per_day))

# =============================================================================
print("\n[B0] 薪资解析器的单值带单位 —— 兼职时薪的前置依赖")
# =============================================================================
# 实测踩过的坑，而且极难查：
#   「160元/天」解析失败，但「160-200元/天」正常。
# 原因：去掉周期单位的两条 re.sub，第二条误写成作用在 text 上，
# 把第一条的结果覆盖回原文。区间格式碰巧不受影响 ——
# 因为数字在「元」前面，正则照样搜得到；单值格式的数字紧贴后缀就漏了。
from jobforge.domain.models import parse_salary

SAL_CASES = [
    ("160元/天", (160, 160, "日")),
    ("160-200元/天", (160, 200, "日")),
    ("200元每天", (200, 200, "日")),
    ("200-300元/天", (200, 300, "日")),
    ("100元/小时", (100, 100, "小时")),
    ("35元/时", (35, 35, "小时")),        # 「时」不能被当成「月」
    ("35-50元/小时", (35, 50, "小时")),
    ("5000-8000元/月", (5000, 8000, "月")),
    ("2000-4000元/月", (2000, 4000, "月")),
    ("10-15K", (10000, 15000, "月")),
]
for _t, _exp in SAL_CASES:
    _r = parse_salary(_t)
    check(f"解析「{_t}」", (_r["min"], _r["max"], _r["period"]) == _exp,
          f"得到 {_r['min']}/{_r['max']}/{_r['period']}，期望 {_exp}")

check("面议仍然认得出来", parse_salary("面议")["state"] == "na",
      parse_salary("面议")["state"])
check("区间下限不会被单值规则吃掉",
      parse_salary("5000-8000元/月")["min"] == 5000,
      str(parse_salary("5000-8000元/月")))

print("\n[B] 时薪归一化 —— 三种口径必须换算到同一个尺度")
# =============================================================================
# 日薪 160 元 / 天，每天 4 小时 → 40 元/小时
j = mk(salary="160元/天", jd="每天工作4小时")
h = compute_hourly(j)
check("日薪 ÷ 每天小时 = 时薪", h.hourly_min == 40.0, str(h.hourly_min))
check("工时写在 JD 里 → stated", h.confidence == "stated", h.confidence)
check("stated 才允许参与比较", h.comparable is True)
check("折算过程写出来给用户看", "÷" in h.basis, h.basis)

# 同样 160 元/天，但不写工时 → 只能用默认 8 小时，且不可比
j2 = mk(salary="160元/天", jd="兼职客服，包吃")
h2 = compute_hourly(j2)
check("没写工时 → 按 8 小时估", h2.hourly_min == 20.0, str(h2.hourly_min))
check("估出来的 → assumed", h2.confidence == "assumed", h2.confidence)
check("★ 估出来的时薪不允许参与硬比较", h2.comparable is False)
check("★ 显示上必须标明是估的", "估" in h2.display(), h2.display())

# 月薪折时薪：5000 ÷ (8 × 21.75) ≈ 28.7
j3 = mk(salary="5000-8000元/月", jd="兼职运营")
h3 = compute_hourly(j3)
check("月薪能折成时薪", h3.hourly_min and abs(h3.hourly_min - 28.7) < 0.5,
      str(h3.hourly_min))
check("月薪没写工时 → assumed", h3.confidence == "assumed")

# 写了工时的月薪 → stated
j4 = mk(salary="5000-8000元/月", jd="每天工作8小时，每月22天")
h4 = compute_hourly(j4)
check("写了工时的月薪 → stated", h4.confidence == "stated", h4.confidence)
check("可参与比较", h4.comparable is True)
check("22 天与默认 21.75 的差异被算进去了",
      h4.hourly_min and abs(h4.hourly_min - 5000 / (8 * 22)) < 0.3,
      str(h4.hourly_min))

# 直接写小时薪
j5 = mk(salary="35-50元/小时", jd="线上剪辑")
h5 = compute_hourly(j5)
check("直接写小时薪的能识别", h5.hourly_min == 35.0 and h5.hourly_max == 50.0,
      str((h5.hourly_min, h5.hourly_max)))
check("小时薪是 stated（最可信）", h5.confidence == "stated")

# 折不出来的
for s, why in (("面议", "面议"), ("", "没写")):
    hh = compute_hourly(mk(salary=s, jd="兼职"))
    check(f"{why} 时必须承认算不出来", not hh.is_known, str(hh.hourly_min))
    check(f"{why} 时不允许参与比较", hh.comparable is False)
    check(f"{why} 时显示要说人话", "算不出来" in hh.display(), hh.display())

# ★ 最重要的一个：日薪数字大 ≠ 时薪高
big_day = mk(salary="300元/天", jd="每天8小时")       # 37.5/小时
small_day = mk(salary="150元/天", jd="每天3小时")      # 50/小时
hb, hs = compute_hourly(big_day), compute_hourly(small_day)
check("★ 日薪 300 但每天8小时 → 时薪低于日薪150但每天3小时的",
      hs.hourly_min > hb.hourly_min,
      f"300/8={hb.hourly_min} vs 150/3={hs.hourly_min}")

check("区间时薪 mid 可用", compute_hourly(mk(salary="35-50元/小时")).mid == 42.5)
check("单值时 mid 就是本身", compute_hourly(mk(salary="40元/小时")).mid == 40.0)
check("to_dict 带出全部字段",
      all(k in compute_hourly(j5).to_dict()
          for k in ("hourly_min", "confidence", "basis", "comparable", "display")))

# =============================================================================
print("\n[C] 居家办公 —— 拿不出证据就不能说能居家")
# =============================================================================
r = detect_remote(mk(pos="居家客服", jd=""))
check("岗位名含居家 → 判为完全居家", r.level == Remote.FULL, r.label)
check("带证据", bool(r.evidence), r.evidence)

r = detect_remote(mk(pos="客服专员", jd="支持居家办公，每周到岗3天"))
check("「居家+到岗」并存 → hybrid（最典型的混合排班）",
      r.level == Remote.HYBRID, r.label)

r = detect_remote(mk(pos="门店店员", jd="需坐班，早9晚6"))
check("明确坐班 → onsite", r.level == Remote.ONSITE, r.label)
check("onsite 给出通勤提醒", "通勤" in r.commute_hint, r.commute_hint)

r = detect_remote(mk(pos="数据录入", jd="认真负责即可"))
check("没提居家 → unknown（不能说能也不能说不能）",
      r.level == Remote.UNKNOWN, r.label)
check("unknown 时没有证据", r.evidence == "")

# ★ 「假自由」：这是居家兼职最大的坑
r = detect_remote(mk(pos="居家客服", jd="居家办公，无需到岗"))
check("★ 居家客服被识别为假自由", r.fake_free is True)
check("并说清为什么", "在线" in r.fake_free_reason or "接电话" in r.fake_free_reason,
      r.fake_free_reason)
check("假自由仍然算 full（不能因为是假自由就否认它居家）",
      r.level == Remote.FULL, r.label)

r = detect_remote(mk(pos="家教", jd="可上门，也可居家"))
check("「可上门，也可居家」→ hybrid（两头都能做）",
      r.level == Remote.HYBRID, r.label)
check("家教不算假自由", r.fake_free is False)

# 「居家+到岗」并存 = 混合，不是"到岗优先"
r = detect_remote(mk(pos="客服", jd="需到岗，偶尔居家处理"))
check("「需到岗+居家」并存 → hybrid（不是按到岗算）",
      r.level == Remote.HYBRID, r.label)

# 否定义不能被当成肯定
r = detect_remote(mk(pos="客服", jd="居家办公，无需到岗"))
check("★ 「无需到岗」不能被判成「需要到岗」",
      r.level == Remote.FULL, f"得到 {r.label}")
r = detect_remote(mk(pos="客服", jd="居家办公，无须坐班"))
check("★ 「无须坐班」同样不算到岗", r.level == Remote.FULL, r.label)
r = detect_remote(mk(pos="客服", jd="需坐班"))
check("单纯的「需坐班」仍是 onsite", r.level == Remote.ONSITE, r.label)

# =============================================================================
print("\n[D] 工作内容分类 —— 分不出来就承认分不出来")
# =============================================================================
cases = [
    ("在线客服专员", "客服 / 坐席", "customer_service"),
    ("兼职电话销售", "销售 / 电销 / 推广", "sales"),
    ("小学数学家教", "家教 / 教育辅导", "teaching"),
    ("外卖骑手兼职", "配送 / 跑腿 / 代驾", "delivery"),
    ("数据标注员", "数据录入 / 标注 / 审核", "data"),
    ("线上美工兼职", "设计 / 文案 / 翻译", "design"),
    ("地推扫码推广", "地推 / 拉新 / 扫码", "promotion"),
]
for pos, label, key in cases:
    c = classify_work(mk(pos=pos))
    check(f"「{pos}」→ {label}", c["key"] == key,
          f"实际判成 {c['label']}({c['key']})")

c = classify_work(mk(pos="兼职伙伴", jd="负责公司日常事务"))
check("看不出是什么 → other", c["key"] == "other", c["label"])
check("other 也要给 caution", bool(c["caution"]), str(c))

# 岗位名优先于 JD —— 招聘方爱在 JD 里顺嘴提"协助运营"
c = classify_work(mk(pos="数据标注员", jd="协助运营团队做日常管理"))
check("岗位名优先，不被 JD 里的'运营'带跑",
      c["key"] == "data", c["label"])

check("分类表里每个 key 都唯一",
      len({c.key for c in CATEGORIES}) == len(CATEGORIES))
check("每类都有 caution 提示",
      all(c.caution for c in CATEGORIES),
      str([c.key for c in CATEGORIES if not c.caution]))

# =============================================================================
print("\n[E] 兼职陷阱信号")
# =============================================================================
sigs = detect_scams(mk(pos="兼职", jd="入职需缴纳培训费200元，包教包会"))
check("培训费 → scam", any(s.level == "scam" and "培训费" in s.code for s in sigs),
      str([s.code for s in sigs]))
check("带证据原文", any(s.evidence for s in sigs if s.level == "scam"))

sigs = detect_scams(mk(pos="兼职推广员", jd="无需经验，日结300元，需发展下线"))
check("发展下线 → scam", any(s.level == "scam" and "下线" in s.code for s in sigs),
      str([s.code for s in sigs]))
check("日结 → warn", any(s.level == "warn" and "日结" in s.code for s in sigs))

sigs = detect_scams(mk(pos="家教", jd="上门授课，每次150元，无需经验"))
check("正常兼职不该被判 scam", not any(s.level == "scam" for s in sigs),
      str([s.code for s in sigs]))
check("但「无需经验」该给 warn", any(s.level == "warn" for s in sigs))

# 「培训」本身不该是 scam（正常岗位也会说培训），但要 warn
sigs = detect_scams(mk(pos="客服", jd="提供岗前培训3天"))
check("「培训」只算 warn 不算 scam",
      not any(s.level == "scam" for s in sigs)
      and any(s.code == "warn:培训" for s in sigs),
      str([(s.level, s.code) for s in sigs]))

# =============================================================================
print("\n[F] 是不是兼职")
# =============================================================================
check("employment_type 写兼职", is_parttime_job(mk(etype="兼职")) is True)
check("employment_type 写全职", is_parttime_job(mk(etype="全职")) is False)
check("employment_type 写实习", is_parttime_job(mk(etype="实习")) is False)
check("字段为空但岗位名带兼职", is_parttime_job(mk(pos="周末兼职", etype="")) is True)
check("字段为空但 JD 写了兼职", is_parttime_job(mk(pos="某岗位", jd="兼职，时间灵活", etype="")) is True)
check("日薪且非实习 → 兼职",
      is_parttime_job(mk(salary="200元/天", pos="某岗位", etype="")) is True)
check("普通全职岗 → False",
      is_parttime_job(mk(salary="10-15K", pos="Java开发工程师", jd="负责后端开发", etype="")) is False)

# =============================================================================
print("\n[G] ★ 筛选：估出来的时薪不许拿去卡门槛")
# =============================================================================
# 这两个岗位日薪都是 200，但一个写了工时一个没写
stated = mk(salary="200元/天", jd="每天工作4小时")        # 50/小时，stated
assumed = mk(salary="200元/天", jd="兼职岗位，包吃")      # 25/小时，assumed

f = PartTimeFilter(hourly_min=45)
r1 = evaluate_parttime(stated, f)
check("工时写明的岗位按时薪 50 通过", r1["pass"] is True, str(r1["reasons"]))
check("并给出时薪备注", any("时薪" in n for n in r1["notes"]), str(r1["notes"]))

r2 = evaluate_parttime(assumed, f)
check("★ 工时靠猜的岗位被排除在硬筛选外", r2["pass"] is False)
check("★ 排除理由说清了是'算不出来'而不是'不够'",
      any("算不出来" in x or "估的" in x for x in r2["reasons"]), str(r2["reasons"]))

res = filter_parttime([stated, assumed], PartTimeFilter(hourly_min=45))
check("批量筛选：1 个 kept", len(res["kept"]) == 1, str(len(res["kept"])))
check("★ 被排除的那条进了 pending 而不是直接消失",
      res["pending_count"] == 1, str(res["pending_count"]))
check("统计里能看到可比的有几个", res["stats"]["comparable"] == 1, str(res["stats"]))

# 门槛很低时两个都留下 —— 工时估算就算差一倍也远在门槛之上，留着更有用
res = filter_parttime([stated, assumed], PartTimeFilter(hourly_min=1))
check("门槛很低时估算岗也留下", len(res["kept"]) == 2, str(len(res["kept"])))
# kept 是按时薪降序排的，估算岗没有 sort_key 所以排在后面 —— 按岗位名找，别按下标
_est = next(k for k in res["kept"] if k["job"].jd == assumed.jd)
check("并标明它是估的、不参与排名",
      any("估的" in n for n in _est["notes"]), str(_est["notes"]))
check("但它仍然不参与排名（sort_key 为空）",
      _est["sort_key"] is None, str(_est["sort_key"]))

# 门槛 40：写明工时的 50/小时过；估算的 25/小时够不到 2 倍门槛 → 落 pending
res = filter_parttime([stated, assumed], PartTimeFilter(hourly_min=40))
check("门槛高到估算岗够不着时，落到 pending",
      res["pending_count"] == 1, str(res["pending_count"]))
check("kept 里只有工时写明的那条", len(res["kept"]) == 1, str(len(res["kept"])))

# 门槛 100：两条都不达标
res = filter_parttime([stated, assumed], PartTimeFilter(hourly_min=100))
check("门槛 100 时两条都过不了", len(res["kept"]) == 0, str(len(res["kept"])))

# =============================================================================
print("\n[H] 筛选：居家 / 分类 / 诈骗")
# =============================================================================
remote_job = mk(salary="35元/小时", pos="居家客服", jd="居家办公，无需到岗")
desk_job = mk(salary="35元/小时", pos="门店导购", jd="需坐班")
unknown_job = mk(salary="35元/小时", pos="某岗位", jd="认真负责")

res = filter_parttime([remote_job, desk_job, unknown_job],
                      PartTimeFilter(remote="full"))
check("筛 full 只留完全居家的",
      len(res["kept"]) == 1 and res["kept"][0]["job"].position == "居家客服",
      str([k["job"].position for k in res["kept"]]))
check("没提居家的被淘汰且说了原因",
      any("居家" in " ".join(d["reasons"]) for d in res["dropped"]))

res = filter_parttime([remote_job, desk_job, unknown_job],
                      PartTimeFilter(remote="any"))
check("any_ok：居不居家都留（unknown 也不冤枉）",
      len(res["kept"]) == 3, str(len(res["kept"])))

scam_job = mk(salary="200元/天", pos="兼职推广", jd="日结300元，需缴纳培训费")
res = filter_parttime([scam_job, stated], PartTimeFilter())
check("诈骗岗被排除", len(res["kept"]) == 1, str(len(res["kept"])))
check("并说明是诈骗信号",
      any("诈骗" in " ".join(d["reasons"]) for d in res["dropped"]),
      str([d["reasons"] for d in res["dropped"]]))

res = filter_parttime([scam_job, stated], PartTimeFilter(exclude_scams=False))
check("关掉排除后诈骗岗会留下（用户自己选择）", len(res["kept"]) == 2)

teach = mk(salary="100元/小时", pos="初中数学家教", jd="上门或居家授课")
admin = mk(salary="30元/小时", pos="行政助理", jd="整理资料")
res = filter_parttime([teach, admin], PartTimeFilter(categories=["teaching"]))
check("按工作内容分类筛选", len(res["kept"]) == 1
      and res["kept"][0]["job"].position == "初中数学家教", str(len(res["kept"])))

d = mk(salary="100元/天", pos="某兼职", jd="做完全部当天结清")
w = mk(salary="100元/天", pos="某兼职2", jd="每周结一次")
check("日结能识别", detect_settlement(d) == "日结", detect_settlement(d))
check("周结能识别", detect_settlement(w) == "周结", detect_settlement(w))
res = filter_parttime([d, w], PartTimeFilter(settlement="日结"))
check("按结算周期筛选", len(res["kept"]) == 1
      and res["kept"][0]["job"].position == "某兼职", str(len(res["kept"])))

# =============================================================================
print("\n[I] ★ 对比：不可比的行不许混进排名")
# =============================================================================
a = mk(salary="50元/小时", pos="线上剪辑", jd="熟练使用剪映")
b = mk(salary="35元/小时", pos="数据标注", jd="居家办公")
c_ = mk(salary="200元/天", pos="客服", jd="包吃住")          # assumed
cmp = compare_parttime(filter_parttime([a, b, c_], PartTimeFilter())["kept"])
check("主表只有可比的两行", len(cmp["rows"]) == 2, str(len(cmp["rows"])))
check("按时薪降序", cmp["rows"][0]["hourly_min"] == 50.0,
      str(cmp["rows"][0]["hourly_min"]))
check("★ 不可比的单独归区", len(cmp["assumed_only"]) == 1, str(len(cmp["assumed_only"])))
check("并说明为什么没参与排名", "估" in cmp["assumed_only"][0]["why"],
      cmp["assumed_only"][0]["why"])
check("note 里点明不能比的原因", "口径不明" in cmp["note"], cmp["note"])

fake = mk(salary="30元/小时", pos="居家客服", jd="居家办公，无需到岗")
cmp2 = compare_parttime(filter_parttime([a, fake], PartTimeFilter())["kept"])
check("对比里标出假自由", cmp2["rows"][1]["fake_free"] is True)
check("结论里提醒假自由", "居家" in cmp2["explain"] and "在线" in cmp2["explain"],
      cmp2["explain"])
check("对比结论点名最高的是谁", "线上剪辑" in cmp2["explain"], cmp2["explain"])

cmp3 = compare_parttime(filter_parttime([c_], PartTimeFilter())["kept"])
check("全都不可比时不硬给排名", "没法给你排名" in cmp3["explain"], cmp3["explain"])

# =============================================================================
print("\n[J] 综合报告")
# =============================================================================
rpt = analyze_parttime(mk(salary="200元/天", pos="居家客服",
                          jd="居家办公，无需到岗，需发展下线"))
check("报告把四项都算齐了",
      rpt.hourly.is_known and rpt.remote and rpt.category and rpt.signals)
check("should_skip 跟着诈骗信号走", rpt.should_skip is True, rpt.scam_level)
d = rpt.to_dict()
check("to_dict 字段齐全",
      all(k in d for k in ("hourly", "remote", "category", "signals",
                           "scam_level", "should_skip")))
check("纯岗位不会崩", analyze_parttime(mk(salary="", jd="", pos="")).to_dict() is not None)

print("\n" + "=" * 62)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    print("  失败项：")
    for x in F:
        print("   -", x)
print("=" * 62)
sys.exit(1 if F else 0)
