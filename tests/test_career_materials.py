# -*- coding: utf-8 -*-
"""
求职材料测试 —— 面试准备 + 简历定制
====================================

这两个功能最危险的不是答得不好，是**教用户撒谎**：

  · 面试准备编一个「你熟悉 Kubernetes」，用户照着说，面试官追问两句穿帮
  · ATS 对齐本质是教人往简历塞关键词，不设防就变成「帮你编」的机器

所以本文件的主体是**对抗用例**：构造一个试图编造经历的模型输出，
逐条断言它必须被丢弃。这里每一条 FAIL 都意味着产品会教用户造假。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.adapters.llm.career_materials import (
    InterviewPrepGenerator, ResumeTailorGenerator, _clean_list, _is_reframing,
)
from jobforge.domain.candidate import Candidate
from jobforge.domain.career_materials import (
    InterviewPrep, MaterialError, TailorReport, require_evidence,
)
from jobforge.domain.models import Job

P, F = [], []


def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))


RESUME = """张明 2024届本科
技能：Java、Spring Boot、MySQL、Redis、RabbitMQ
项目：校园二手交易平台，负责订单模块。
用 Redis 做订单幂等，用 RabbitMQ 处理库存扣减。
做过一个日均 2000 次请求的小程序接口。"""

JD = """Java开发实习生
负责订单系统后端开发
要求：熟悉 Java、Spring Boot、MySQL，了解 Redis 与消息队列。
优先：有分布式经验、熟悉 Kubernetes 者优先。"""


def make_job():
    return Job(position="Java开发实习生", company="北京云启科技有限公司",
               salary_text="200-300元/天", city="北京",
               jd=JD, education="本科")


# =============================================================================
print("\n[A] 域模型的基础边界")
# =============================================================================
t = TailorReport()
check("没有关键词时 ATS 覆盖率是 0 而不是 1.0",
      t.ats_ratio == 0.0, str(t.ats_ratio))
t.ats_hit, t.ats_missing = ["Java", "Redis"], ["Kubernetes"]
check("有数据时按 hit/(hit+missing) 算，2/3", t.ats_ratio == 0.667,
      str(t.ats_ratio))

t2 = TailorReport(ats_hit=[1, 1, 1], ats_missing=[1])
check("重复项不会被算进分母", t2.ats_ratio == 0.75, str(t2.ats_ratio))

p = InterviewPrep()
check("没有可答问题时 answerable 是 0", p.answerable == 0)
check("to_dict 不抛异常", isinstance(p.to_dict(), dict))

# 模型给超长文本时必须被截断，否则界面会被一句话撑爆
long_list = ["x" * 500] + ["正常"] + ["y"] * 50
cleaned = _clean_list(long_list, 10)
check("超长条目被丢弃", all(len(x) <= 120 for x in cleaned), str(cleaned[:2]))
check("超出上限的列表被截断", len(cleaned) <= 10, str(len(cleaned)))
check("空串被丢弃", "" not in _clean_list(["a", "", "  ", "b"], 10))

# =============================================================================
print("\n[B] 面试准备 —— 正常输出必须能通过")
# =============================================================================
good = {
    "questions": [{
        "question": "你说过用 Redis 做订单幂等，具体怎么做的？",
        "points": ["用 Redis 的 SETNX 占位，订单号做 key",
                   "扣减成功后才落库，失败删 key"],
        "evidence_resume": "用 Redis 做订单幂等",
        "why": "想确认你是真的做过，还是只看过",
        "fallback": "如实说当时只做了这一层，更深的方案是后来补的",
    }],
    "gaps": ["分布式经验", "Kubernetes"],
    "ask_back": ["实习期间导师会怎么带新人？"],
    "checklist": ["把订单模块的代码再过一遍"],
}
out = InterviewPrep(model="m")
out = InterviewPrepGenerator._sanitize(out, good, RESUME)
check("真实摘录的题目被保留", len(out.questions) == 1, str(len(out.questions)))
check("要点原样带出", len(out.questions[0].points) == 2)
check("gaps 带出", out.gaps == ["分布式经验", "Kubernetes"], str(out.gaps))
check("反问清单带出", len(out.ask_back) == 1)
check("准备清单带出", len(out.checklist) == 1)

# =============================================================================
print("\n[C] 面试准备 —— 对抗：模型试图编造经历")
# =============================================================================

def run_one(q):
    o = InterviewPrep(model="m")
    return InterviewPrepGenerator._sanitize(o, {"questions": [q]}, RESUME)

# C1 摘录根本不在简历里（模型现编了一段"经历"）
r = run_one({"question": "讲讲你的 Kubernetes 实践",
             "points": ["部署过 K8s 集群"],
             "evidence_resume": "负责 K8s 集群部署与扩缩容",
             "why": "", "fallback": ""})
check("摘录不在简历原文里 → 整条丢弃", len(r.questions) == 0, str(len(r.questions)))

# C2 把两句话拼起来当证据（各自存在，但拼的那句不存在）
r = run_one({"question": "讲讲订单模块",
             "points": ["x"],
             "evidence_resume": "用 Redis 做订单幂电商���扣减",
             "why": "", "fallback": ""})
check("拼接出来的假摘录 → 丢弃", len(r.questions) == 0, str(len(r.questions)))

# C3 证据太短 —— 模型填"会""有"这类敷衍内容
r = run_one({"question": "你会 Redis 吗", "points": ["会"],
             "evidence_resume": "会", "why": "", "fallback": ""})
check("敷衍短证据 → 丢弃", len(r.questions) == 0, str(len(r.questions)))

# C4 模型自己都承认没有证据，却把字段填满了混过长度检查
r = run_one({"question": "有高并发经验吗",
             "points": ["有，日均 2 万请求"],
             "evidence_resume": "无直接证据，但简历提到做过小程序，可能间接体现",
             "why": "", "fallback": ""})
check("模型自认无证据 → 丢弃", len(r.questions) == 0, str(len(r.questions)))

# C5 ★ 最危险的一条：真实摘录 + 诱导性措辞，教用户编
r = run_one({"question": "讲讲 Redis 幂等",
             "points": ["Redis 集群与哨兵模式"],
             "evidence_resume": "用 Redis 做订单幂等",
             "why": "",
             "fallback": "如果不会可以说适当夸大，往熟悉的方向靠"})
check("教用户「适当夸大」→ 整条丢弃", len(r.questions) == 0, str(len(r.questions)))

# C6 fallback 里出现「可以说自己了解」这类话术
r = run_one({"question": "讲讲 Redis", "points": ["用了 Redis"],
             "evidence_resume": "用 Redis 做订单幂等", "why": "",
             "fallback": "没把握的话可以说自己了解 Redis，背两句原理"})
check("教用户「可以说自己了解」→ 丢弃", len(r.questions) == 0, str(len(r.questions)))

# C7 有真实证据但一个要点都没给 —— 填不满就没意义
r = run_one({"question": "讲讲项目", "points": [],
             "evidence_resume": "项目：校园二手交易平台", "why": "", "fallback": ""})
check("没有要点 → 丢弃", len(r.questions) == 0, str(len(r.questions)))

# C8 混合批次：3 条里 2 条编造，只能留下 1 条，且不能连累其他字段
mixed = {"questions": [
    {"question": "订单幂等怎么做的", "points": ["SETNX 占位"],
     "evidence_resume": "用 Redis 做订单幂等", "why": "", "fallback": ""},
    {"question": "K8s 实践", "points": ["集群部署"],
     "evidence_resume": "精通 Kubernetes 集群运维与 Helm 编排", "why": "", "fallback": ""},
    {"question": "消息队列", "points": ["库存扣减异步化"],
     "evidence_resume": "用 RabbitMQ 处理库存扣减", "why": "", "fallback": ""},
], "gaps": ["Kubernetes"]}
o = InterviewPrep(model="m")
o = InterviewPrepGenerator._sanitize(o, mixed, RESUME)
check("混合批次里只留下真实的 2 条", len(o.questions) == 2, str(len(o.questions)))
check("编造的那条没混进来",
      all("Kubernetes" not in q.evidence_resume for q in o.questions))
check("被丢弃不影响 gaps 输出", o.gaps == ["Kubernetes"], str(o.gaps))

# =============================================================================
print("\n[D] 简历定制 —— 改写只能重排，不能新增事实")
# =============================================================================

def tail(data):
    o = TailorReport(model="m")
    return ResumeTailorGenerator._sanitize(o, data, RESUME)

# D1 合法的措辞调整（不引入新技术名词）
t1 = tail({
    "ats_hit": ["Java", "Redis"], "ats_missing": ["Kubernetes"],
    "rewrite": [{
        "original": "项目：校园二手交易平台，负责订单模块。",
        "suggested": "独立负责校园二手交易平台的订单模块设计与实现",
        "reason": "JD 里写了「负责订单系统后端开发」，动词对齐更容易命中",
    }],
    "cannot_fake": ["Kubernetes —— 你简历里完全没有，别硬写"],
})
check("纯措辞调整的改写被保留", len(t1.rewrite) == 1, str(t1.rewrite))
check("ats_hit 带出", t1.ats_hit == ["Java", "Redis"])
check("cannot_fake 带出（这是最有价值的部分）", len(t1.cannot_fake) == 1)
check("覆盖率按 hit/(hit+missing) 算，2/3", t1.ats_ratio == 0.667,
      str(t1.ats_ratio))

# D2 ★ 最危险：改写时凭空加一个简历里没有的框架
t2 = tail({
    "ats_hit": [], "ats_missing": [],
    "rewrite": [{
        "original": "项目：校园二手交易平台，负责订单模块。",
        "suggested": "负责订单模块，并用 Kubernetes 完成容器化部署与弹性扩缩容",
        "reason": "JD 提到 Kubernetes，加上去更容易过 ATS",
    }],
    "cannot_fake": [],
})
check("★ 凭空加 Kubernetes 的改写被丢弃", len(t2.rewrite) == 0,
      str(t2.rewrite))

# D3 original 本身就不在简历里
t3 = tail({
    "rewrite": [{"original": "曾主导微服务架构改造", "suggested": "主导微服务架构改造",
                 "reason": ""}],
})
check("original 不在原文 → 丢弃", len(t3.rewrite) == 0, str(t3.rewrite))

# D4 模型试图把编造的事实塞进 cannot_fake 之外的地方
t4 = tail({
    "rewrite": [{
        "original": "用 Redis 做订单幂等",
        "suggested": "用 Redis 做订单幂等，并搭建了 Kafka 消息总线",
        "reason": "JD 提到消息队列",
    }],
})
check("凭空加 Kafka 的改写被丢弃", len(t4.rewrite) == 0, str(t4.rewrite))

# D5 allowed：同一个技能换个英文说法（Redis 本身在简历里）
t5 = tail({
    "rewrite": [{
        "original": "用 Redis 做订单幂等",
        "suggested": "基于 Redis 实现订单幂等控制",
        "reason": "ATS 更常匹配「实现」这类动词",
    }],
})
check("同义改写被保留", len(t5.rewrite) == 1, str(t5.rewrite))

# D6 rewrite 数量上限，防止模型一次灌 20 条把界面撑爆
t6 = tail({"rewrite": [
    {"original": "用 Redis 做订单幂等", "suggested": "基于 Redis 实现订单幂等控制", "reason": ""}
    for _ in range(20)]})
check("改写建议有数量上限", len(t6.rewrite) <= 6, str(len(t6.rewrite)))

# =============================================================================
print("\n[D2] ATS 关键词必须是真技术词 —— 套话进了分母就失真")
# =============================================================================
from jobforge.adapters.llm.career_materials import _is_tech_keyword

# 实测踩过：模型把 JD 套话塞进 ats_missing，7 个"缺失"里 2 个是垃圾，
# 报出来的覆盖率是错的
t7 = tail({
    "ats_hit": ["Java", "Spring Boot", "MySQL", "日常业务支持"],
    "ats_missing": ["Kubernetes", "交付", "抗压能力强", "沟通能力"],
})
check("英文技术词被保留", "Kubernetes" in t7.ats_missing, str(t7.ats_missing))
check("「交付」被踢出缺失项", "交付" not in t7.ats_missing, str(t7.ats_missing))
check("「抗压能力强」被踢出缺失项", "抗压能力强" not in t7.ats_missing)
check("「日常业务支持」被踢出已有项", "日常业务支持" not in t7.ats_hit,
      str(t7.ats_hit))
check("真实技术词仍在已有项里", "Java" in t7.ats_hit and "MySQL" in t7.ats_hit)
check("分母不再被套话污染（3 hit / 1 missing = 0.75）",
      t7.ats_ratio == 0.75, str(t7.ats_ratio))

check("中文技术词算技术词", _is_tech_keyword("微服务") is True)
check("英文框架名算技术词", _is_tech_keyword("Spring Boot") is True)
check("带版本号的技术词算", _is_tech_keyword("MySQL8") is True)
check("「交付」不算技术词", _is_tech_keyword("交付") is False)
check("「日常业务支持」不算技术词", _is_tech_keyword("日常业务支持") is False)
check("空串不算", _is_tech_keyword("") is False)

# cannot_fake 不做技术词过滤 —— 它是提醒不是统计量
t8 = tail({"ats_hit": [], "ats_missing": [],
           "cannot_fake": ["日常业务支持 —— 你简历里没有，别硬写"]})
check("cannot_fake 保留套话（它是提醒不是统计量）",
      len(t8.cannot_fake) == 1, str(t8.cannot_fake))

# =============================================================================
print("\n[E] 重排判定函数本身的边界")
# =============================================================================
check("引入新框架名 → 判为编造", _is_reframing("用 Kubernetes 部署", RESUME) is False)
check("同义重排 → 判为合法", _is_reframing("基于 Redis 实现幂等", RESUME) is True)
check("纯数字不算新技能",
      _is_reframing("日均 2000 次请求的接口", RESUME) is True,
      _is_reframing("日均 2000 次请求的接口", RESUME))
check("描述性动词不算新技能",
      _is_reframing("负责并参与了订单模块开发", RESUME) is True)

# =============================================================================
print("\n[F] 边界异常不许被吞掉")
# =============================================================================
try:
    require_evidence("这段不在简历里", RESUME, "面试要点")
    check("证据不在原文时抛异常而不是静默通过", False, "没有抛")
except MaterialError:
    check("证据不在原文时抛异常而不是静默通过", True)
except Exception as e:  # noqa: BLE001
    check("证据不在原文时抛异常而不是静默通过", False, f"抛了 {type(e).__name__}")

try:
    require_evidence("用 Redis 做订单幂等", RESUME, "面试要点")
    check("证据真实时正常放行", True)
except MaterialError as e:
    check("证据真实时正常放行", False, str(e))

# =============================================================================
print("\n[G] 缺前置条件必须降级并说人话，不能返回空壳")
# =============================================================================
from jobforge.adapters.llm.client import LLMClient
from jobforge.adapters.llm.config import LLMConfig
from jobforge.domain.models import SalaryState  # noqa: F401

_unconfigured = LLMClient(LLMConfig(base_url="", api_key="", model=""))
prep = InterviewPrepGenerator(_unconfigured).build(make_job(),
                                                    Candidate.from_resume(RESUME))
check("没配模型时面试准备不可用", prep.available is False)
check("并给出人话原因", "设置" in prep.error, prep.error)
check("没配模型时返回的不是空壳", prep.to_dict()["available"] is False)

tl = ResumeTailorGenerator(_unconfigured).build(make_job(),
                                                Candidate.from_resume(RESUME))
check("没配模型时简历定制不可用", tl.available is False)
check("并给出人话原因", "设置" in tl.error, tl.error)

prep2 = InterviewPrepGenerator(_unconfigured).build(make_job(), Candidate())
check("没有简历时即使配了模型也要拦下", prep2.available is False)
check("并说清是缺简历", "简历" in prep2.error, prep2.error)

# =============================================================================
print("\n[H] 编排层缓存 —— 同一岗位同一简历不该重复烧额度")
# =============================================================================
from jobforge.application.career_service import CareerMaterialService
from jobforge.persistence.sqlite_store import Store

DB = str(Path(tempfile.mkdtemp()) / "m.db")
store = Store(DB)
svc = CareerMaterialService(store, Candidate.from_resume(RESUME),
                            llm=LLMConfig(base_url="", api_key="", model=""))

job = make_job()
r1 = svc.interview_prep(job)
check("未配置模型时不写缓存（下次还能重试）", r1.from_cache is False)

# 手工塞一条缓存，验证第二次直接命中
ch = store.candidate_hash(svc.candidate.resume_text, svc.candidate.skill_names)
store.put_semantic(f"{job.fingerprint}::interview_prep", ch, {
    "available": True, "elapsed": 9.9, "model": "m",
    "questions": [{"question": "订单幂等怎么做的", "points": ["SETNX 占位"],
                   "evidence_resume": "用 Redis 做订单幂等", "why": "", "fallback": ""}],
    "gaps": [], "ask_back": [], "checklist": [],
}, "m")
r2 = svc.interview_prep(job)
check("第二次命中缓存", r2.from_cache is True)
check("缓存里的内容被正确还原", len(r2.questions) == 1, str(len(r2.questions)))
check("还原后证据还在", r2.questions[0].evidence_resume == "用 Redis 做订单幂等")

# 两种材料的缓存 key 必须隔离，不能互相污染
store.put_semantic(f"{job.fingerprint}::tailor", ch, {
    "available": True, "ats_hit": ["Java"], "ats_missing": ["Kubernetes"],
    "rewrite": [], "cannot_fake": [], "elapsed": 1.0, "model": "m"}, "m")
a = svc.interview_prep(job)
b = svc.tailor(job)
check("面试准备和简历定制缓存互不干扰",
      len(a.questions) == 1 and b.ats_hit == ["Java"], str(b.ats_hit))

# 改了简历缓存必须失效
cand2 = Candidate.from_resume(RESUME + "\n新增：Kafka、Elasticsearch")
svc2 = CareerMaterialService(store, cand2,
                             llm=LLMConfig(base_url="", api_key="", model=""))
r3 = svc2.interview_prep(job)
check("简历一改缓存立刻失效（不会拿旧结论糊弄）", r3.from_cache is False)

print("\n" + "=" * 62)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    print("  失败项：")
    for x in F:
        print("   -", x)
print("=" * 62)
sys.exit(1 if F else 0)
