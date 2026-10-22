# -*- coding: utf-8 -*-
"""
LLM 层测试 —— 用本地假 OpenAI 服务验证，不依赖任何真实厂商
============================================================

覆盖三条路径：
  A. 未配置 → 整体降级，规则引擎不受影响
  B. 配置正确但服务不可用 → 报错可观测，不静默
  C. 正常返回 → 结构化结果可用，且**只做加法不改规则结论**
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.adapters.llm.config import LLMConfig
from jobforge.adapters.llm.client import LLMClient
from jobforge.adapters.llm.semantic import SemanticAnalyzer, SemanticResult
from jobforge.application.analyzer import MatchAnalyzer
from jobforge.adapters.sources.manual import parse_boss_text
from jobforge.domain.candidate import Candidate

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ← {detail}" if detail and not cond else ""))


# =============================================================================
# 假 OpenAI 兼容服务
# =============================================================================

CAPTURED = {"calls": [], "fail_mode": None}

# 注意：evidence_* 必须是 RESUME / JD 里的**原文摘录**。
# 因为清洗层会做摘录真实性校验，编造的证据会被丢弃 —— 这正是要验的东西。
FAKE_ANALYSIS = {
    "semantic_matches": [
        {"skill": "抽象建模能力", "confidence": "high",
         "evidence_jd": "要求具备良好的抽象建模能力与系统设计思维，能独立设计子系统",
         "evidence_resume": "做过电商订单系统，用 RabbitMQ 做异步解耦"},
        {"skill": "抽象建模能力", "confidence": "high",
         "evidence_jd": "要求具备良好的抽象建模能力",
         "evidence_resume": "做过电商订单系统"},
    ],
    "missing": [
        {"skill": "分布式事务", "why": "订单与库存要保证最终一致",
         "learnable": True, "learn_cost": "1月"},
        {"skill": "Elasticsearch", "why": "需要做检索能力",
         "learnable": False, "learn_cost": "不可短期补"},
    ],
    "soft_fit": {"score": 72, "comment": "工作内容与候选人的后端经历方向一致"},
    "resume_advice": ["把『消息队列』明确写成 RabbitMQ", "项目里补一句日均请求量"],
    "red_flags": ["职责描述过于笼统，未说明具体业务方向"],
}


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        CAPTURED["calls"].append(body)

        if CAPTURED["fail_mode"] == "401":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"invalid api key"}}')
            return
        if CAPTURED["fail_mode"] == "notjson":
            content = "抱歉，我无法完成这个请求。"
        elif CAPTURED["fail_mode"] == "empty":
            content = ""
        else:
            content = json.dumps(FAKE_ANALYSIS, ensure_ascii=False)

        payload = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": content}}]
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


server = HTTPServer(("127.0.0.1", 0), FakeHandler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}/v1"

TMP = Path(tempfile.mkdtemp())
CFG = str(TMP / "llm.json")

RESUME = """张明 2024届本科
技能：Java、Spring Boot、MySQL、Redis、RabbitMQ
做过电商订单系统，用 RabbitMQ 做异步解耦。"""

JD = """Java开发工程师
15-25K
北京云启科技有限公司
北京 3-5年 本科
职位描述：
1、必须精通 Java 和 Spring Boot，熟练使用 MySQL；
2、需要熟悉 Redis 缓存与 RabbitMQ 消息队列；
3、要求具备良好的抽象建模能力与系统设计思维，能独立设计子系统；
4、有 Docker 容器化部署经验优先；
5、熟悉分布式事务者优先。"""

job = parse_boss_text(JD)
cand = Candidate.from_resume(RESUME, name="张明")
base_report = MatchAnalyzer(cand).analyze(job)


print("\n[A] 配置层 —— 用户自填，不预置任何厂商")
# =============================================================================
check("默认未配置", LLMConfig().is_configured() is False)
check("默认不预置任何厂商 base_url", LLMConfig().base_url == "", repr(LLMConfig().base_url))
check("默认不预置任何厂商 model", LLMConfig().model == "", repr(LLMConfig().model))

cfg = LLMConfig(base_url=BASE, api_key="sk-test-key-1234", model="my-local-model")
check("自填配置后可用", cfg.is_configured() is True)
check("endpoint 自动拼接", cfg.endpoint == f"{BASE}/chat/completions", cfg.endpoint)
check("endpoint 幂等（已带路径不重复拼）",
      LLMConfig(base_url=BASE + "/chat/completions").endpoint
      == f"{BASE}/chat/completions")
check("Key 脱敏不回传明文", "1234" in cfg.masked_key() and "test-key" not in cfg.masked_key(),
      cfg.masked_key())
check("to_public 不含 api_key", "api_key" not in cfg.to_public(), str(list(cfg.to_public())))
check("校验能报缺失", len(LLMConfig().validate()) == 3)
check("校验拦截非 http 开头",
      "Base URL 必须以 http:// 或 https:// 开头" in LLMConfig(
          base_url="ftp://x", api_key="k", model="m").validate())

p = cfg.save(CFG)
check("配置落盘成功", Path(p).exists())
check("读回一致", LLMConfig.load(CFG).model == "my-local-model")

# 环境变量优先
import os
os.environ["JOBFORGE_LLM_MODEL"] = "env-model"
check("环境变量优先于文件", LLMConfig.load(CFG).model == "env-model")
del os.environ["JOBFORGE_LLM_MODEL"]


print("\n[B] 未配置时 —— 整体降级，规则结论不受影响")
# =============================================================================
sem_off = SemanticAnalyzer(LLMClient(LLMConfig(), retries=0)).analyze(job, cand)
check("未配置时 available=False", sem_off.available is False)
check("未配置时给出原因", "未配置" in sem_off.error, sem_off.error)
check("未配置时不产生任何建议", sem_off.resume_advice == [] and sem_off.missing == [])
check("未配置时匹配度完全不受影响",
      base_report.match_score == MatchAnalyzer(cand).analyze(job).match_score)
# 结论由规则引擎独立算出，未配置 LLM 时不受影响（此例缺 1 项硬性 → 有风险）
check("未配置时结论完全不受影响",
      base_report.verdict == "有风险",
      f"实际 {base_report.verdict}")


print("\n[C] 客户端 —— 超时/错误/非法 JSON 都可观测")
# =============================================================================
CAPTURED["calls"].clear()
cl = LLMClient(LLMConfig(base_url=BASE, api_key="k", model="m"), retries=0)

r = cl.test_connection()
check("连通性测试成功", r.ok is True, r.error)
check("有内容返回", bool((r.raw or "").strip()))
# 连通性测试应走 chat_text（不带 response_format），否则不支持该参数的服务会误报失败
check("连通性测试不强制 json_object",
      "response_format" not in CAPTURED["calls"][-1],
      str(list(CAPTURED["calls"][-1].keys())))
check("请求带 model 字段", CAPTURED["calls"][-1].get("model") == "m")

CAPTURED["fail_mode"] = "401"
r = cl.chat_json("sys", "user")
check("401 被识别为错误", r.ok is False)
check("错误信息含状态码", "401" in r.error, r.error)
CAPTURED["fail_mode"] = "notjson"
r = cl.chat_json("sys", "user")
check("模型返回非 JSON 时判失败", r.ok is False)
check("非 JSON 的失败原因明确", "JSON" in r.error, r.error)
CAPTURED["fail_mode"] = "empty"
r = cl.test_connection()
check("连通但返回空内容时报错", r.ok is False and "空" in r.error, r.error)
CAPTURED["fail_mode"] = None

bad = LLMClient(LLMConfig(base_url="http://127.0.0.1:1/v1", api_key="k", model="m"),
                retries=0)
r = bad.chat_json("sys", "user")
check("服务不可达时不静默", r.ok is False and bool(r.error), r.error)


print("\n[D] 语义分析 —— 结构化结果 + 只做加法")
# =============================================================================
sem = SemanticAnalyzer(LLMClient(LLMConfig(base_url=BASE, api_key="k", model="m"),
                                 retries=0)).analyze(job, cand)
check("语义分析可用", sem.available is True, sem.error)
check("semantic_matches 已解析", len(sem.semantic_matches) + len(sem.weak_matches) >= 1,
      f"strong={len(sem.semantic_matches)} weak={len(sem.weak_matches)}")
check("confidence=high 的进 strong 档", len(sem.semantic_matches) == 1,
      str([m.get("skill") for m in sem.semantic_matches]))
check("重复 skill 已去重",
      len({m["skill"] for m in sem.semantic_matches + sem.weak_matches})
      == len(sem.semantic_matches) + len(sem.weak_matches),
      str([m["skill"] for m in sem.semantic_matches + sem.weak_matches]))
check("missing 已解析", len(sem.missing) == 2, str(sem.missing))
check("soft_fit 分数已解析", sem.soft_fit_score == 72, str(sem.soft_fit_score))
check("resume_advice 已解析", len(sem.resume_advice) == 2)
check("red_flags 已解析", len(sem.red_flags) == 1)

# 隐私与边界：简历确实被发送（用户自己的选择），但 system prompt 里要求不许编造
last = CAPTURED["calls"][-1]
check("system prompt 明确禁止编造经历",
      "不要编造简历里没有的经历" in last["messages"][0]["content"])
check("system prompt 要求原文证据",
      "证据" in last["messages"][0]["content"])
check("请求使用了 max_tokens 上限", "max_tokens" in last)

# JD 太短时不调 LLM
CAPTURED["calls"].clear()
short_job = parse_boss_text("Java开发工程师\n15-25K\n某公司\n北京\n负责后端开发。")
s2 = SemanticAnalyzer(LLMClient(LLMConfig(base_url=BASE, api_key="k", model="m"),
                                retries=0)).analyze(short_job, cand)
check("JD 过短时不调用 LLM（省钱省时间）",
      s2.available is False and len(CAPTURED["calls"]) == 0, str(s2.error))


print("\n[E] merge —— 语义只做加法，不改规则结论")
# =============================================================================
rep = MatchAnalyzer(cand).analyze(job)
before_score = rep.match_score
before_verdict = rep.verdict
before_missing = len(rep.missing_hard)
before_notes = len(rep.notes)

merged = SemanticAnalyzer.merge(rep, sem)

check("匹配度没有被 LLM 改动", merged.match_score == before_score,
      f"{before_score} → {merged.match_score}")
check("结论没有被 LLM 改动", merged.verdict == before_verdict,
      f"{before_verdict} → {merged.verdict}")
check("硬性缺失项没有被 LLM 改动", len(merged.missing_hard) == before_missing)

check("LLM 的缺失项进入了建议", any("分布式事务" in s for s in merged.suggestions))
check("不可短期补的项标注了", any("别硬投" in s for s in merged.suggestions),
      str(merged.suggestions))
check("简历建议被并入", any(s.startswith("简历建议：") for s in merged.suggestions))
check("红旗被并入 notes", any("职责描述过于笼统" in n for n in merged.notes))
check("soft_fit 被并入 notes", any("工作内容契合度" in n for n in merged.notes))
check("建议数量有上限", len(merged.suggestions) <= 10, str(len(merged.suggestions)))
check("notes 数量有上限", len(merged.notes) <= 8, str(len(merged.notes)))

# 降级时 merge 不改变任何东西
rep2 = MatchAnalyzer(cand).analyze(job)
s_before = list(rep2.suggestions)
SemanticAnalyzer.merge(rep2, sem_off)
check("未配置时 merge 是空操作", rep2.suggestions == s_before)

server.shutdown()

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("   -", f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
