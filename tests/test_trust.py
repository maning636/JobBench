# -*- coding: utf-8 -*-
"""
岗位可信度体检测试
====================

三条纪律在这里逐条钉死：

  1. **可信度不参与匹配度扣分** —— 匹配度高不能掩盖岗位可能是假的
  2. **只报可观察的事实** —— 不下"这家公司骗人"这类结论
  3. **拿不出证据就不报** —— 宁可不报，不可乱报
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.domain.models import Job, SalaryState, apply_salary
from jobforge.domain.trust import (
    COMPANY_PAY_TRUST, CompanyType, TrustReport, TrustSignal,
    _cn_num, _days_since, check_trust, classify_company,
)

P, F = [], []


def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))


def mkjob(**kw):
    base = dict(position="Java开发", company="北京云启科技有限公司",
                city="北京", jd="", salary_text="", channel="boss")
    base.update(kw)
    j = Job(**base)
    if base.get("salary_text"):
        apply_salary(j)
    return j


def codes(r):
    return {s.code for s in r.signals}


# =============================================================================
print("\n[A] 公司类型识别 —— 判断不出来就承认判断不出来")
# =============================================================================
check("名字含'人力资源' → 外包派遣", classify_company("北京众合人力资源有限公司")
      == CompanyType.OUTSOURCED)
check("名字含'劳务' → 外包派遣", classify_company("某某劳务服务有限公司")
      == CompanyType.OUTSOURCED)
check("名字含'集团' → 大厂国企", classify_company("某某科技集团") == CompanyType.BIG)
check("名字含'研究所' → 大厂国企", classify_company("某某研究所") == CompanyType.BIG)
check("名字含'个体工商户' → 个体小公司",
      classify_company("某某个体工商户") == CompanyType.SMALL)
check("完全陌生的名字 → 明确返回 unknown",
      classify_company("张三李四") == CompanyType.UNKNOWN)

# ⚠ 顺序陷阱：一个人力资源公司名字里也常有"科技"，不能被归成创业公司
check("「人力资源+科技」优先判外包，不能被科技带偏",
      classify_company("云启人力资源科技有限公司") == CompanyType.OUTSOURCED,
      classify_company("云启人力资源科技有限公司"))

# ⚠ 这里刻意**没有**照抄 career-ops 的「Unknown 一律取低」。
# 理由写在他们那份映射表的注释里了，总结一句：
# 我们没有上市公司/融资/职级那套信号，BOSS 上绝大多数就是「XX有限公司」，
# 一律判低 = 每条岗位都显示"低" = 徽章失去区分度。
# 而且断言"低"本身就是一种不诚实的置信度 —— 我们明明不知道。
check("判断不出来时说'未知'，不假装成'低'",
      COMPANY_PAY_TRUST[CompanyType.UNKNOWN][0] == "未知",
      COMPANY_PAY_TRUST[CompanyType.UNKNOWN][0])
check("'未知'和'低'是不同的一档，不是同义词",
      COMPANY_PAY_TRUST[CompanyType.UNKNOWN][0]
      != COMPANY_PAY_TRUST[CompanyType.OUTSOURCED][0])
check("每种公司类型都有可信度定义", len(COMPANY_PAY_TRUST) == 5, str(len(COMPANY_PAY_TRUST)))

# 普通「XX有限公司」不该被判成低可信度 —— 那会让徽章彻底没用
plain = check_trust(mkjob(company="北京某某有限公司", salary_text="10-20K"))
check("普通有限公司 → 未知而不是低", plain.pay_trust == "未知", plain.pay_trust)
check("未知档要说明为什么", "问" in plain.pay_trust_reason or "面试" in plain.pay_trust_reason,
      plain.pay_trust_reason)

check("派遣公司仍然是低（这一档不能被稀释）",
      COMPANY_PAY_TRUST[CompanyType.OUTSOURCED][0] == "低")

# =============================================================================
print("\n[B] 薪资区间宽度过宽（career-ops 13 号信号的算术）")
# =============================================================================
# 判据：(hi - lo) > 0.5 * lo
r = check_trust(mkjob(salary_text="6-15K"))
check("6-15K：跨度9K > 下限一半3K → 报区间过宽",
      "wide_salary_range" in codes(r), str(codes(r)))

r = check_trust(mkjob(salary_text="15-20K"))
check("15-20K：跨度5K < 下限一半7.5K → 不报",
      "wide_salary_range" not in codes(r), str(codes(r)))

r = check_trust(mkjob(salary_text="9-11K"))
check("9-11K：跨度2K 远小于下限 → 不报",
      "wide_salary_range" not in codes(r), str(codes(r)))

r = check_trust(mkjob(salary_text="面议"))
check("面议不参与区间计算", "wide_salary_range" not in codes(r))

# 下限为 0 或倒挂时不能算，否则会除出假阳性
j = mkjob(salary_text="面议")
j.salary_min, j.salary_max = 0, 10
r = check_trust(j)
check("下限为 0 时不算区间", "wide_salary_range" not in codes(r), str(codes(r)))
j.salary_min, j.salary_max = 20, 10
r = check_trust(j)
check("上下限倒挂时不报", "wide_salary_range" not in codes(r), str(codes(r)))

sig = next((s for s in r.signals if s.code == "vague_pay:面议"), None)
check("面议有自己的信号", sig is not None, str(codes(r)))

# =============================================================================
print("\n[C] 试用期 —— 中国本土法条边界")
# =============================================================================
r = check_trust(mkjob(jd="试用期 6 个月，薪资 10-15K"))
check("试用期 6 个月：刚好上限，不报", "probation_too_long" not in codes(r), str(codes(r)))

r = check_trust(mkjob(jd="试用期 8 个月"))
check("试用期 8 个月：超过上限 → 报", "probation_too_long" in codes(r), str(codes(r)))

r = check_trust(mkjob(jd="试用期六个月"))
check("中文数字'六个月'是法定上限 → 不报", "probation_too_long" not in codes(r), str(codes(r)))

r = check_trust(mkjob(jd="试用期八个月"))
check("中文数字'八个月'能识别且超出上限", "probation_too_long" in codes(r), str(codes(r)))

r = check_trust(mkjob(jd="试用期 3 个月"))
check("试用期 3 个月：正常，不报", "probation_too_long" not in codes(r))

r = check_trust(mkjob(jd="试用期工资为正式工资的 70%"))
check("试用期只发 70% → 报", "probation_pay_too_low" in codes(r), str(codes(r)))

r = check_trust(mkjob(jd="试用期工资 85%"))
check("试用期 85%：合法，不报", "probation_pay_too_low" not in codes(r), str(codes(r)))

check("中文数字解析：十二", _cn_num("十二") == 12, str(_cn_num("十二")))
check("中文数字解析：六", _cn_num("六") == 6)
check("中文数字解析：三", _cn_num("三") == 3)
check("中文数字解析：8", _cn_num("8") == 8)

# =============================================================================
print("\n[D] 本土信号：竞业 / 加班 / 五险一金")
# =============================================================================
r = check_trust(mkjob(jd="入职后需签署竞业限制协议，期限 2 年"))
check("提到竞业 → 报", "non_compete" in codes(r), str(codes(r)))

r = check_trust(mkjob(jd="要求 996，每周工作六天"))
check("提到 996 → 报", any(c.startswith("overtime:") for c in codes(r)), str(codes(r)))

r = check_trust(mkjob(jd="大小周"))
check("提到大小周 → 报", any(c.startswith("overtime:") for c in codes(r)), str(codes(r)))

r = check_trust(mkjob(jd="缴纳五险一金"))
check("提到了五险一金 → 不报缺社保", "no_benefit_mention" not in codes(r), str(codes(r)))

r = check_trust(mkjob(jd="岗位要求：熟悉 Java"))
check("完全没提社保 → 提醒一句", "no_benefit_mention" in codes(r), str(codes(r)))

# =============================================================================
print("\n[E] ★ 铁律一：可信度绝不能参与匹配度扣分")
# =============================================================================
bad = check_trust(mkjob(salary_text="3-30K", jd="试用期 12 个月，签竞业",
                        company="某某人力资源服务有限公司"))
check("极端可疑的岗位照样 score_penalty=0", bad.score_penalty == 0.0,
      str(bad.score_penalty))
check("报告里显式带上 score_penalty 字段",
      bad.to_dict()["score_penalty"] == 0.0)

good = check_trust(mkjob(salary_text="15-20K", jd="五险一金，试用期 3 个月"))
check("正常岗位的 score_penalty 同样是 0",
      good.score_penalty == 0.0)

# =============================================================================
print("\n[F] ★ 铁律三：拿不出证据就不报")
# =============================================================================
# 温和的岗位不该被扣一堆信号
clean = check_trust(mkjob(company="中国某某银行股份有限公司",
                          salary_text="20-30K",
                          jd="负责核心系统开发，缴纳五险一金，试用期 3 个月"))
check("正规大厂岗不该报试用期问题", "probation_too_long" not in codes(clean),
      str(codes(clean)))
check("正规大厂薪资可信度应为高", clean.pay_trust == "高", clean.pay_trust)

# 每条 signal 都得能说清依据
r = check_trust(mkjob(salary_text="6-15K", jd="试用期 9 个月"))
for s in r.signals:
    if s.level == "bad":
        check(f"bad 级信号 [{s.code}] 必须带证据原文",
              bool(s.evidence.strip()), f"{s.code} 证据为空")

# =============================================================================
print("\n[G] 幽灵岗信号要多条叠加才判定，单条不冤枉人")
# =============================================================================
r = check_trust(mkjob(salary_text="15-20K", jd="熟悉 Java"))
check("只有普通信号时 ghost_risk=none", r.ghost_risk == "none", r.ghost_risk)

# 挂了很久 + 区间过宽 → 两条 → suspect
j = mkjob(salary_text="6-15K", jd="急聘，立即到岗")
j.first_seen_at = "2020-01-01"
r = check_trust(j)
check("多条可疑信号叠加 → ghost_risk=suspect", r.ghost_risk == "suspect", r.ghost_risk)
check("并给出一条人话解释", len(r.ghost_reason) > 10, r.ghost_reason)

# 只有一条 → 不判定为 suspect
j2 = mkjob(salary_text="6-15K", jd="熟悉 Java")
r2 = check_trust(j2)
check("单条信号不判为 suspect（不冤枉人）",
      r2.ghost_risk == "none", r2.ghost_risk)

check("挂了很久也会被记成信号", "long_listed" in codes(check_trust(j)))
check("急聘会被记成信号", "urgent_template" in codes(check_trust(j)))

# =============================================================================
print("\n[H] 输出契约")
# =============================================================================
r = check_trust(mkjob(salary_text="6-15K", jd="试用期 9 个月，急聘"))
d = r.to_dict()
for k in ("company_type", "pay_trust", "ghost_risk", "signals",
          "checked_fields", "worst_level", "score_penalty"):
    check(f"to_dict 里有 {k}", k in d, str(list(d.keys())))

check("worst_level 能给出最严重的一档", r.worst_level() == "bad", r.worst_level())
check("每条 signal 都能序列化",
      all(isinstance(s["title"], str) and isinstance(s["level"], str)
          for s in d["signals"]))
check("空岗位也不会崩", check_trust(mkjob(company="")).company_type
      == CompanyType.UNKNOWN)

# =============================================================================
print("\n[I] 边界")
# =============================================================================
check("_days_since 对空值返回 None", _days_since("") is None)
check("_days_since 对脏数据返回 None 而不是抛异常",
      _days_since("不是日期") is None)
check("_cn_num 对垃圾输入返回 None", _cn_num("abc") is None)
check("空的 TrustReport 序列化不崩", isinstance(TrustReport().to_dict(), dict))
check("空报告的最严重档是 info", TrustReport().worst_level() == "info")

print("\n" + "=" * 62)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    print("  失败项：")
    for x in F:
        print("   -", x)
print("=" * 62)
sys.exit(1 if F else 0)
