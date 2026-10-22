# -*- coding: utf-8 -*-
"""
求职工作台 · LLM 客户端
========================

OpenAI 兼容的 `/v1/chat/completions`。不绑定任何厂商 ——
用户填什么 base_url 就连什么，本地 Ollama 和各家云服务一样对待。

三条工程纪律：
1. **必须有超时**。没有超时的外部调用会挂住整个流程，
   而 v1.2.0 最贵的教训就是长任务没有中断点。
2. **结构化输出优先于解析自由文本**。分析结果要能被程序消费，
   所以用 `response_format: json_object` 并对结果做校验，
   拿不到就当没有 —— 不用半截结果糊弄。
3. **失败必须可观测**。返回结构里带 error 和耗时，
   不静默吞异常（v1.2.0 的 24 处 `except: pass` 就是反面教材）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from jobforge.adapters.llm.config import LLMConfig


@dataclass
class LLMResult:
    ok: bool
    data: Optional[Dict[str, Any]] = None
    raw: str = ""
    error: str = ""
    elapsed: float = 0.0
    model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": self.error,
                "elapsed": round(self.elapsed, 2), "model": self.model}


class LLMClient:
    """极简 OpenAI 兼容客户端。不引入 openai SDK，少一个依赖少一份风险。"""

    def __init__(self, config: Optional[LLMConfig] = None, retries: int = 1):
        self.cfg = config or LLMConfig.load()
        self.retries = retries

    def available(self) -> bool:
        return self.cfg.is_configured()

    # ------------------------------------------------------------------

    def _post(self, payload: Dict[str, Any]) -> LLMResult:
        if not self.available():
            return LLMResult(ok=False, error="未配置 LLM（base_url / api_key / model）")

        req = urllib.request.Request(
            self.cfg.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
            },
            method="POST",
        )

        t0 = time.time()
        last_err = ""
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                raw = self._extract(body)
                return LLMResult(ok=True, raw=raw, elapsed=time.time() - t0,
                                 model=self.cfg.model)
            except urllib.error.HTTPError as e:
                detail = ""
                code = None
                try:
                    raw = e.read().decode("utf-8", "ignore")
                    detail = raw[:300]
                    j = json.loads(raw)
                    err = j.get("error") or {}
                    detail = str(err.get("message") or j.get("message") or raw[:200])
                    code = err.get("code")
                except Exception:  # noqa: BLE001
                    pass
                last_err = f"HTTP {e.code} code={code} {detail or e.reason}"

                # 429 不是一种情况，实测智谱上至少两种：
                #   code=1305 访问量过大  → 该退避重试
                #   code=1113 余额不足    → 重试一万次也没用，立刻失败
                # 不区分就会白等超时，用户以为程序卡死了。
                if e.code == 429 and str(code) == "1113":
                    last_err += "（余额不足，重试无意义，请充值或换免费模型）"
                    break
                if 400 <= e.code < 500 and e.code != 429:
                    break
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"

            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))

        return LLMResult(ok=False, error=last_err or "未知错误",
                         elapsed=time.time() - t0, model=self.cfg.model)

    @staticmethod
    def _extract(body: Dict[str, Any]) -> str:
        try:
            choice = body["choices"][0]
            msg = choice.get("message") or {}
            content = msg.get("content")
            if content:
                return content
            # 部分服务只返回 reasoning_content 之外的 text
            return choice.get("text", "") or ""
        except Exception:  # noqa: BLE001
            return json.dumps(body, ensure_ascii=False)[:2000]

    # ------------------------------------------------------------------

    def chat_json(self, system: str, user: str,
                  retries: Optional[int] = None) -> LLMResult:
        """要求返回 JSON。拿不到合法 JSON 就算失败 —— 不返回半截结果。"""
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "response_format": {"type": "json_object"},
        }
        r = self._post(payload)
        if not r.ok:
            return r

        parsed = _loads(r.raw)
        if parsed is None:
            # 有的服务不支持 response_format，会回一段带 ```json 的文本
            parsed = _loads(_strip_fence(r.raw))
        if parsed is None:
            return LLMResult(ok=False, raw=r.raw, model=r.model,
                             error="模型没有返回合法 JSON",
                             elapsed=r.elapsed)
        r.data = parsed
        return r

    def chat_text(self, system: str, user: str) -> LLMResult:
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        return self._post(payload)

    # ------------------------------------------------------------------

    def test_connection(self) -> LLMResult:
        """连通性自检。让用户填完配置立刻知道对不对，而不是等分析时才炸。

        判定标准：能通 + 有内容回来。
        不校验具体内容 —— 各家模型对这句话的回应不一样，
        用内容做断言会误杀本来能用的服务。
        """
        r = self.chat_text(
            "你是连通性测试助手，只做简短的应答，不要输出 JSON 或代码块。",
            "回复两个字即可：可用",
        )
        if not r.ok:
            return r
        if not (r.raw or "").strip():
            return LLMResult(ok=False, error="连通成功但模型返回为空内容",
                             elapsed=r.elapsed, model=r.model)
        return r


def _strip_fence(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    return s.strip()


def _loads(s: str) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:  # noqa: BLE001
        return None
