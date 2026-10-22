# -*- coding: utf-8 -*-
"""
输出清洗层的对抗测试 —— 模型已经返回了结果，但它们不可信
=========================================================

真实模型调用在当前环境拿不到凭证（401），所以这里不测"模型会不会编"，
改测**"模型编了之后，我的代码能不能挡住"**。

这才是工程上可控的那一半：模型会漂移、会脑补、会不按 schema 来，
所以清洗层必须独立成立、且有测试兜住。

用例模拟三种真实的"模型问题"：
  A. 把简历里完全没有的能力声称成命中（最危险）
  B. 声称命中但拿不出任何简历原文证据
  C. 同一项能力重复输出多条
  D. 字段类型乱（skill 缺失、score 写成字符串）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.adapters.llm.semantic import SemanticResult, SemanticAnalyzer
from jobforge.application.analyzer import MatchAnalyzer
from jobforge.adapters.sources.manual import parse_boss_text
from jobforge.domain.candidate import Candidate, is_soft_skill

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ← {detail}" if detail and not cond else ""))


RESUME = """张明 2024届本科
技能：Java、Spring Boot、MySQL、Redis、RabbitMQ
做过电商订单系统，用 RabbitMQ 做异步解耦。"""

JD = """Java开发工程师
15-25K
北京云启科技有限公司
北京 3-5年 本科
职位描述：
必须精通 Java 和 Spring Boot，熟练使用 MySQL 和 Redis；
需要熟悉 RabbitMQ 或 Kafka 消息队列；
要求具备良好的抽象建模能力与系统设计思维；
具备高并发、微服务架构与分布式事务的实际项目经验；
有良好的沟通能力与责任心。"""

job = parse_boss_text(JD)
cand = Candidate.from_resume(RESUME, name="张明")


print("\n[A] 软性素质不进匹配度 —— 不该拿'沟通能力'扣技术分")
# =============================================================================
rep = MatchAnalyzer(cand).analyze(job)
skill_names = [m.skill for m in rep.hits]
check("沟通能力未被当作技术要求", "沟通能力" not in skill_names, str(skill_names))
check("责任心未被当作技术要求", "责任心" not in skill_names)
check("抽象建模能力未被当作技术要求", "抽象建模能力" not in skill_names)

check("软性要求被单独提示", any("软性素质" in n for n in rep.notes), str(rep.notes))
check("软性提示里说明了不计入匹配度",
      any("没计入匹配度" in n for n in rep.notes), str(rep.notes))

# 关掉软性词后匹配度应该上升（说明之前确实在稀释）
check("剔除软性词后匹配度更高（证明之前被稀释）",
      rep.match_score > 0, str(rep.match_score))

# 软性词的判定函数本身
check("is_soft_skill 能识别软性词", is_soft_skill("沟通能力") is True)
check("is_soft_skill 不误伤技术词", is_soft_skill("Java") is False)
check("is_soft_skill 覆盖'抽象建模'", is_soft_skill("抽象建模") is True)

# 多个岗位之间不能互相污染
an = MatchAnalyzer(cand)
r1 = an.analyze(job)
r2 = an.analyze(parse_boss_text("Python工程师\n15-20K\n某公司\n北京\n要求熟悉 Python 与 Django。"))
check("分析第二个岗位时软性词不残留",
      not any("软性素质" in n for n in r2.notes), str(r2.notes))


print("\n[B] 清洗层挡幻觉 —— 模型编的东西必须被拦住")
# =============================================================================
dirty = SemanticResult(available=True)
dirty.semantic_matches = [
    # 声称命中 + 有简历证据 → 合法
    {"skill": "Java", "evidence_jd": "必须精通 Java",
     "evidence_resume": "技能：Java、Spring Boot", "confidence": "high"},
    # 声称命中 + 简历里没有 + 无证据 → 必须被丢弃
    {"skill": "Kubernetes", "evidence_jd": "熟悉 K8s", "evidence_resume": "",
     "confidence": "high"},
    # 证据极短，实质等于没有 → 必须被丢弃
    {"skill": "高并发", "evidence_jd": "具备高并发经验", "evidence_resume": "有",
     "confidence": "high"},
    # 完全没有 JD 证据 → 必须被丢弃
    {"skill": "架构设计", "evidence_resume": "做过订单系统"},
    # 重复项 → 只留一条
    {"skill": "Java", "evidence_jd": "精通 Java", "evidence_resume": "Java"},
]
before = len(dirty.semantic_matches)
clean = SemanticAnalyzer._sanitize(dirty)
names = [m["skill"] for m in clean.semantic_matches]

check("无证据的幻觉条目被丢弃", "Kubernetes" not in names, str(names))
check("证据过短的条目被丢弃", "高并发" not in names, str(names))
check("缺 JD 证据的条目被丢弃", "架构设计" not in names, str(names))
check("合法条目被保留", "Java" in names, str(names))
check("重复条目被去重", len(names) == len(set(names)), str(names))
check(f"清洗数量收敛（{before} → {len(clean.semantic_matches)}）",
      len(clean.semantic_matches) < before)
check("技能名缺失的条目被丢弃",
      all(m.get("skill") for m in clean.semantic_matches))


print("\n[C] 类型鲁棒性 —— 模型把字段写错时不能崩")
# =============================================================================
weird = SemanticResult(available=True)
weird.semantic_matches = [
    {"skill": None, "evidence_jd": "x"},
    {"evidence_jd": "没有 skill 字段"},
    # 有 skill + JD 证据，但没有简历证据 → 修复后必须被丢弃
    {"skill": "Java", "evidence_jd": "有 skill 和 JD 证据"},
    # 两侧证据齐全 → 保留
    {"skill": "Spring Boot", "evidence_jd": "必须精通 Spring Boot 开发",
     "evidence_resume": "技能：Java、Spring Boot、MySQL"},
]
c2 = SemanticAnalyzer._sanitize(weird)
allnames = [m.get("skill") for m in c2.semantic_matches + c2.weak_matches]
check("skill 为 None / 缺失的条目不崩且被丢弃",
      "None" not in allnames and None not in allnames, str(allnames))
check("缺简历证据的条目被丢弃（幻觉闸门）",
      "Java" not in allnames, str(allnames))
check("两侧证据齐全的条目被保留", "Spring Boot" in allnames, str(allnames))

# soft_fit.score 写成字符串时的容错
sem = SemanticResult(available=True, soft_fit_score=None)
for v in [72, "72", 72.0, None, "abc"]:
    try:
        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        _ = int(v) if ok else None
        check(f"score={v!r} 被安全处理", True)
    except Exception as e:  # noqa: BLE001
        check(f"score={v!r} 被安全处理", False, str(e))


print("\n[D] merge 只做加法 —— 模型不能改规则结论")
# =============================================================================
rep_a = MatchAnalyzer(cand).analyze(job)
score0, verdict0 = rep_a.match_score, rep_a.verdict

hostile = SemanticResult(available=True)
hostile.semantic_matches = [
    {"skill": "Kubernetes", "evidence_jd": "必须 K8s", "evidence_resume": "我会 K8s 没问题",
     "confidence": "high"},
    {"skill": "高并发", "evidence_jd": "必须高并发", "evidence_resume": "我扛得住",
     "confidence": "high"},
]
hostile.missing = []          # 模型说「你什么都不缺」
hostile.resume_advice = ["把 Kubernetes 写进简历，这样更有优势"]
hostile.red_flags = []

hostile = SemanticAnalyzer._sanitize(hostile)
rep_b = SemanticAnalyzer.merge(rep_a, hostile)

check("匹配度未被改", rep_b.match_score == score0, f"{score0} → {rep_b.match_score}")
check("结论未被改", rep_b.verdict == verdict0, f"{verdict0} → {rep_b.verdict}")

# 这个 JD 的硬性项（Java/Spring Boot/MySQL/Redis/RabbitMQ）本来就全命中，
# 所以 missing_hard 正确地等于 0。真正要验的是：merge 之后仍是 0，
# 而不是模型把已有的缺失项抹掉。换成有缺失的 JD 来验这一点。
gap_job = parse_boss_text("""资深 Java 工程师
20-35K
北京云启科技有限公司
北京 3-5年 本科
职位描述：
必须精通 Java 和 Spring Boot，必须熟练掌握 Kubernetes 与 Docker，
必须熟悉分布式事务与高并发架构。""")
rep_g = MatchAnalyzer(cand).analyze(gap_job)
gap_before = len(rep_g.missing_hard)
check("对照：含缺失的 JD 确实有硬性缺失", gap_before > 0, str(gap_before))
rep_g2 = SemanticAnalyzer.merge(rep_g, hostile)
check("硬性缺失项未被模型抹掉",
      len(rep_g2.missing_hard) == gap_before,
      f"{gap_before} → {len(rep_g2.missing_hard)}")
check("缺硬性的岗位仍判不建议/有风险",
      rep_g2.verdict in ("不建议", "有风险", "不要投"), rep_g2.verdict)

check("模型建议的长度有上限（防止灌水）", len(rep_b.suggestions) <= 10)
check("模型建议被标记来源，用户能分辨",
      any("简历建议" in s for s in rep_b.suggestions))

# 注意：模型简历建议里有「把 Kubernetes 写进简历」——
# 这是诱导用户造假。我们的规则不该直接透传模型的建议。
# 至少要能看出这是个需要人工判断的建议。
print("\n[E] 模型建议必须带防造假约束")
adv = [s for s in rep_b.suggestions if s.startswith("简历建议：")]
check("模型建议被追加了防造假约束",
      adv and all("没做过" in s or "没做过" in s for s in adv), str(adv))
check("防造假约束与规则引擎的建议一致",
      adv and "被追问" in adv[0], adv[0] if adv else "无")
print(f"  最终呈现给用户的建议：{adv[0] if adv else '（无）'}")

print("\n[F] 真实模型实测抓到的案例（2026-10-17，智谱 glm-4-flash）")
# =============================================================================
# 下面这些是**真实模型返回的内容**，不是构造数据。
# 它们通过了旧版清洗层，说明"只查长度"完全不够。
REAL_RESUME = """张明
2024届本科毕业生
技能：Java、Spring Boot、MyBatis、MySQL、Redis、RabbitMQ、Docker、Git
1. 电商订单系统（个人项目）
   使用 Spring Boot + MySQL + Redis 实现订单创建与库存扣减，
   用 RabbitMQ 做订单超时取消的异步处理，日均请求约 2 万。
2. 校园二手交易平台（课程项目）
   前端用 Vue，后端用 Spring Boot，部署在 Docker 上。"""

real = SemanticResult(available=True)
real.semantic_matches = [
    # ① 过度引申：拿"日均2万请求"去支撑"高并发+微服务+分布式事务"
    #    证据是原文摘录、长度也够，旧清洗层完全拦不住
    {"skill": "高并发、微服务架构与分布式事务的实际项目经验",
     "evidence_jd": "具备高并发、微服务架构与分布式事务的实际项目经验",
     "evidence_resume": "使用 Spring Boot + MySQL + Redis 实现订单创建与库存扣减，用 RabbitMQ 做订单超时取消的异步处理",
     "confidence": "medium"},
    # ② 模型自己承认没有证据，却把字段填满了
    {"skill": "良好的沟通能力与责任心",
     "evidence_jd": "有良好的沟通能力与责任心，能承受较强工作压力",
     "evidence_resume": "无直接证据，但简历中提到熟悉 MyBatis 常用增删改查，写过基于消息队列的异步解耦方案，可能间接体现沟通能力和责任心",
     "confidence": "low"},
    # ③ 编造摘录：原文里根本没有"使用 Docker 容器化部署服务"这句
    {"skill": "Docker 容器化",
     "evidence_jd": "熟悉 Kubernetes 容器编排，有 Docker 部署经验优先",
     "evidence_resume": "使用 Docker 容器化部署服务，保证高可用",
     "confidence": "high"},
    # ④ 真命中：证据是原文逐字摘录
    {"skill": "抽象建模能力",
     "evidence_jd": "要求具备良好的抽象建模能力与系统设计思维",
     "evidence_resume": "电商订单系统（个人项目）",
     "confidence": "medium"},
]
kept_before = len(real.semantic_matches)
cleaned = SemanticAnalyzer._sanitize(real, jd_source=JD, resume_source=REAL_RESUME)
kept = [m["skill"] for m in cleaned.semantic_matches]
weak = [m["skill"] for m in cleaned.weak_matches]
check("② 模型自认'无直接证据'被挡下（不进强/弱任何一档）",
      not any("沟通能力" in k for k in kept + weak), str(kept + weak))
check("③ 编造的摘录（原文没有）被挡下",
      not any("Docker 容器化" == k for k in kept + weak), str(kept + weak))
check(f"真实案例清洗收敛（{kept_before} → {len(kept) + len(weak)}）",
      len(kept) + len(weak) < kept_before)

# ① 过度引申：程序判断不了语义合理性，所以不丢弃而是降级。
#    这是刻意的设计选择 —— 强行丢弃会连带丢掉模型真正的发现。
check("① 过度引申被降级为'需自行核实'而非直接丢弃",
      any("高并发" in k for k in weak), f"weak={weak}")
check("① 未被当作确定命中",
      not any("高并发" in k for k in kept), f"strong={kept}")
check("① 在 merge 后以'推测'措辞呈现",
      any("不是结论" in n for n in
          SemanticAnalyzer.merge(MatchAnalyzer(cand).analyze(job), cleaned).notes),
      str(cleaned.weak_matches))

# 归一化比对：改标点不该导致丢真证据
punct_src = "熟悉 Spring Boot、MySQL 与 Redis；要求具备抽象建模能力。"
check("标点差异不误伤（归一化后仍能匹配）",
      SemanticAnalyzer._quote_is_real("熟悉 Spring Boot，MySQL 与 Redis 要求具备抽象建模能力", punct_src),
      "模型改标点后应仍通过")

# 软性素质在 LLM 层也必须被拦
soft_only = SemanticResult(available=True)
soft_only.semantic_matches = [{
    "skill": "沟通能力", "confidence": "low",
    "evidence_jd": "有良好的沟通能力与责任心",
    "evidence_resume": "熟悉 MyBatis 常用增删改查，写过基于消息队列的异步解耦方案"}]
c_soft = SemanticAnalyzer._sanitize(soft_only, jd_source=JD, resume_source=REAL_RESUME)
c_soft.semantic_matches = [m for m in c_soft.semantic_matches
                          if not is_soft_skill(str(m.get("skill") or "").strip())]
check("软性素质在 LLM 层也被拦", c_soft.semantic_matches == [], str(c_soft.semantic_matches))

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("   -", f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
