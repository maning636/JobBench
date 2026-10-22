# -*- coding: utf-8 -*-
"""
求职工作台 · LLM 配置
=======================

**只设一个自定义 Key，用户自己接。** 不预置任何厂商。

理由：
1. 不绑死厂商。国内 DeepSeek / 通义 / Kimi / 智谱 / 硅基流动、
   本地 Ollama / vLLM 都兼容 OpenAI 的 `/v1/chat/completions` 格式，
   填个 base_url 就能用，项目不用背各家适配的锅。
2. 用户可能用本地模型或私有网关，不该被"官方直连"绑住。
3. Key 不进仓库。用户自己的 Key 自己管。

安全边界：
- Key 存在 `data/llm.json`，**必须**在 .gitignore 里。
- 读配置接口永不回传 Key 明文，只回显前后 4 位。
- 简历会发给用户自己配置的模型 —— 这是用户自己的选择，不是我们的上传。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_PATH = "data/llm.json"


class LLMConfigError(Exception):
    """配置文件读不了。必须显式报，不能静默当成"未配置"。

    实测踩过的坑：配置损坏后静默降级，用户点了「测试连通」只看到
    「未配置 LLM」，完全不知道自己的文件坏了。
    """

# 环境变量优先级高于文件，方便 CI / 容器注入
ENV_KEYS = {
    "base_url": "JOBFORGE_LLM_BASE_URL",
    "api_key": "JOBFORGE_LLM_API_KEY",
    "model": "JOBFORGE_LLM_MODEL",
}


@dataclass
class LLMConfig:
    base_url: str = ""          # 例如 https://api.你的服务.com/v1
    api_key: str = ""
    model: str = ""              # 例如 deepseek-chat / qwen-plus / 本地模型名
    temperature: float = 0.2     # 分析类任务要稳定，不要发散
    # 实测 glm-4-flash 在 max_tokens=1800 时单次要 44 秒，
    # 降到 1200 后明显改善，且分析结果本来就不需要那么长。
    max_tokens: int = 1200
    timeout: int = 60

    # ---------------- 状态 ----------------

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def endpoint(self) -> str:
        b = (self.base_url or "").rstrip("/")
        if b.endswith("/chat/completions"):
            return b
        return f"{b}/chat/completions"

    def masked_key(self) -> str:
        k = self.api_key or ""
        if len(k) <= 8:
            return "•" * len(k)
        return f"{k[:4]}{'•' * 8}{k[-4:]}"

    def validate(self) -> list:
        """返回错误清单，空列表表示配置可用。"""
        errs = []
        if not self.base_url:
            errs.append("缺少 Base URL")
        elif not self.base_url.startswith(("http://", "https://")):
            errs.append("Base URL 必须以 http:// 或 https:// 开头")
        if not self.api_key:
            errs.append("缺少 API Key")
        if not self.model:
            errs.append("缺少模型名")
        return errs

    def to_public(self) -> Dict[str, Any]:
        """对外暴露的形态 —— 永远不含 Key 明文。"""
        return {
            "configured": self.is_configured(),
            "base_url": self.base_url,
            "model": self.model,
            "masked_key": self.masked_key() if self.api_key else "",
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "endpoint": self.endpoint if self.base_url else "",
        }

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # ---------------- 读写 ----------------

    @classmethod
    def load(cls, path: str = DEFAULT_PATH) -> "LLMConfig":
        # 环境变量优先
        env = {k: os.getenv(v) for k, v in ENV_KEYS.items()}
        env = {k: v for k, v in env.items() if v}

        p = Path(path)
        data: Dict[str, Any] = {}
        if p.exists():
            raw = p.read_text(encoding="utf-8-sig")   # 容忍 BOM，见下
            if raw.strip():
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise LLMConfigError(
                        f"模型配置解析失败（{path}）：{e}。"
                        "文件可能改坏了，删掉重填即可（不影响规则引擎使用）。"
                    ) from e
        data.update(env)

        known = {f for f in cls.__dataclass_fields__}  # noqa: F821
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str = DEFAULT_PATH) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        try:
            if os.name != "nt":
                os.chmod(p, 0o600)   # 非 Windows 下收紧权限
        except Exception:  # noqa: BLE001
            pass
        return str(p)
