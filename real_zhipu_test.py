# -*- coding: utf-8 -*-
"""
用真实模型跑完整语义分析链路（走本项目自己的 LLMClient）

这是开发期自测脚本，不是产品的一部分。
密钥不要写进代码：把 key 和 base_url 放到环境变量里，或者写进
data/llm.json（已在 .gitignore 里）。

    set JOBFORGE_LLM_API_KEY=...
    set JOBFORGE_LLM_BASE_URL=https://<你的服务地址>/v1
    set JOBFORGE_LLM_MODEL=<模型名>
    python real_zhipu_test.py

任何兼容 OpenAI /v1/chat/completions 的服务都行 —— 云端各家、
本地 Ollama / vLLM / LM Studio 都可以。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

from jobforge.adapters.llm.config import LLMConfig
from jobforge.adapters.llm.client import LLMClient
from jobforge.adapters.llm.semantic import SemanticAnalyzer
from jobforge.adapters.sources.manual import parse_boss_text
from jobforge.application.analyzer import MatchAnalyzer, CompareAnalyzer
from jobforge.domain.candidate import Candidate

# 密钥来源：环境变量优先，其次 data/llm.json。绝不硬编码。
_cfg_from_file = LLMConfig.load("data/llm.json")
api_key = os.getenv("JOBFORGE_LLM_API_KEY", "") or _cfg_from_file.api_key
base_url = os.getenv("JOBFORGE_LLM_BASE_URL", "") or _cfg_from_file.base_url
model = os.getenv("JOBFORGE_LLM_MODEL", "") or _cfg_from_file.model
if not model:
    print("没有指定模型名。请设置 JOBFORGE_LLM_MODEL。")
    sys.exit(1)

if not (api_key and base_url):
    print("没有可用的模型配置。请设置 JOBFORGE_LLM_API_KEY 和 "
          "JOBFORGE_LLM_BASE_URL，或先在 data/llm.json 里配好。")
    sys.exit(1)

cfg = LLMConfig(
    base_url=base_url.rstrip("/"),
    api_key=api_key,
    model=model,
    temperature=0.2,
    max_tokens=1800,
    timeout=120,
)

print("=" * 74)
print("1. 连通性测试（走本项目自己的 LLMClient，不是我自己的 urllib）")
print("=" * 74)
client = LLMClient(cfg, retries=1)
r = client.test_connection()
print(f"  {'✓' if r.ok else '✗'} {r.elapsed:.1f}s  模型={r.model}")
print(f"    返回：{r.raw.strip()[:100]!r}" if r.ok else f"    错误：{r.error}")
if not r.ok:
    sys.exit(1)

# 存进项目配置（Key 不回显）
cfg.save("data/llm.json")
pub = cfg.to_public()
print(f"\n  已写入 data/llm.json")
print(f"    base_url = {pub['base_url']}")
print(f"    model    = {pub['model']}")
print(f"    api_key  = {pub['masked_key']}  ← 回显脱敏，不含明文")

RESUME = """张明
北京 | 2024届本科毕业生 | 计算机科学与技术

技能：
Java、Spring Boot、MyBatis、MySQL、Redis、RabbitMQ、Docker、Git
熟悉 MyBatis 常用增删改查，写过基于消息队列的异步解耦方案

项目经历：
1. 电商订单系统（个人项目）
   使用 Spring Boot + MySQL + Redis 实现订单创建与库存扣减，
   用 RabbitMQ 做订单超时取消的异步处理，日均请求约 2 万。

2. 校园二手交易平台（课程项目）
   前端用 Vue，后端用 Spring Boot，部署在 Docker 上。

求职意向：Java 后端开发，北京，期望 18K"""

JD = """Java开发工程师
15-25K·13薪
北京云启科技有限公司
北京 海淀区 3-5年 本科
职位描述：
1、负责交易系统的后端服务开发，必须精通 Java 和 Spring Boot，
   熟练使用 MySQL 进行表结构设计，熟练使用 Redis 做缓存；
2、需要熟悉 RabbitMQ 或 Kafka 等消息队列；
3、要求具备良好的抽象建模能力与系统设计思维，能独立设计子系统；
4、具备高并发、微服务架构与分布式事务的实际项目经验；
5、熟悉 Kubernetes 容器编排，有 Docker 部署经验优先；
6、有良好的沟通能力与责任心，能承受较强工作压力。"""

cand = Candidate.from_resume(RESUME, name="张明")
cand.salary_expect = "18K"
cand.finalize()
job = parse_boss_text(JD)

print()
print("=" * 74)
print("2. 规则引擎单独（对照组：LLM 要补的是哪些盲区）")
print("=" * 74)
rep = MatchAnalyzer(cand).analyze(job)
print(rep.to_display())
print()
print(f"  技术要求识别到：{[m.skill for m in rep.hits]}")
print(f"  规则引擎的盲区（模型要补的）：")
for b in ["抽象建模能力", "系统设计思维", "能独立设计子系统",
          "沟通能力", "责任心", "能承受较强工作压力"]:
    print(f"    · {b}")

print()
print("=" * 74)
print("3. 真实模型语义分析")
print("=" * 74)
sem = SemanticAnalyzer(LLMClient(cfg, retries=1)).analyze(job, cand)
print(f"  available={sem.available}  耗时 {sem.elapsed:.1f}s  模型 {sem.model}")
if not sem.available:
    print(f"  错误：{sem.error}")
    sys.exit(1)

print(f"\n  语义命中 {len(sem.semantic_matches)} 条（规则引擎读不懂的）：")
for m in sem.semantic_matches:
    print(f"    · {m.get('skill')}（置信度 {m.get('confidence')}）")
    print(f"        JD   ：{str(m.get('evidence_jd'))[:70]}")
    print(f"        简历 ：{str(m.get('evidence_resume'))[:70]}")

print(f"\n  模型判定的缺失项 {len(sem.missing)} 条：")
for m in sem.missing:
    print(f"    · {m.get('skill')}｜可自学={m.get('learnable')}｜成本={m.get('learn_cost')}")
    if m.get("why"):
        print(f"        理由：{str(m['why'])[:70]}")

print(f"\n  工作内容契合度：{sem.soft_fit_score}")
print(f"    {sem.soft_fit_comment}")
print("\n  简历建议：")
for a in sem.resume_advice:
    print(f"    · {a}")
print("\n  红旗：")
for f in sem.red_flags:
    print(f"    · {f}")

print()
print("=" * 74)
print("4. 幻觉检查 —— 模型有没有编造简历里没有的经历")
print("=" * 74)
resume_skills = {s["name"] for s in cand.skills}
bad = []
for m in sem.semantic_matches:
    name = str(m.get("skill", ""))
    ev = str(m.get("evidence_resume", ""))
    if name not in resume_skills and len(ev) < 8:
        bad.append((name, ev))
if bad:
    print("  ✗ 发现疑似幻觉（清洗层本应拦下，说明还有漏洞）：")
    for n, e in bad:
        print(f"    · {n!r} 简历证据={e!r}")
else:
    print("  ✓ 所有语义命中都带够长的简历原文证据（幻觉闸门生效）")

print()
print("=" * 74)
print("5. 合并 —— 规则结论是否被模型改动")
print("=" * 74)
rep2 = MatchAnalyzer(cand).analyze(job)
s0, v0, m0 = rep2.match_score, rep2.verdict, len(rep2.missing_hard)
merged = SemanticAnalyzer.merge(rep2, sem)
print(f"  匹配度 {s0} → {merged.match_score}  {'✓ 未被改动' if s0 == merged.match_score else '✗ 被改动'}")
print(f"  结论   {v0} → {merged.verdict}  {'✓ 未被改动' if v0 == merged.verdict else '✗ 被改动'}")
print(f"  硬性缺失 {m0} → {len(merged.missing_hard)}  {'✓ 未被改动' if m0 == len(merged.missing_hard) else '✗ 被改动'}")

print("\n  最终建议（用户看到的）：")
for s in merged.suggestions:
    print(f"    · {s}")
print("\n  说明区：")
for n in merged.notes:
    print(f"    · {n}")
