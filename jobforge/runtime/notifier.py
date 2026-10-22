# -*- coding: utf-8 -*-
"""
求职工作台 · 飞书通知
======================

产品价值在「每天」。筛完不推等于白干 —— 用户不会主动打开一个网页。

用飞书自定义机器人 webhook：
  - 零成本，很多用户本来就在用飞书，开箱即用
  - 手机上直接收，不需要做 App（v2 明确不做手机端，靠飞书补齐这一环）
  - 消息是卡片式，可以把匹配度和判据一起带出去

**推送前置条件：数据必须过看门狗。**
v1.2.0 的随包演示数据 15 条岗位 salary 全空、company 存的是"5-10年"，
照样宣称"找到 15 个岗位"。每天自动推送的产品不能这样 ——
错一次，用户会慢慢不信。所以 gate 不通过时只发"异常告警"，不发岗位。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


DEFAULT_WEBHOOK = os.getenv("JOBFORGE_FEISHU_WEBHOOK", "")


@dataclass
class FeishuResult:
    ok: bool
    status: int = 0
    error: str = ""
    skipped: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "error": self.error,
                "skipped": self.skipped, "reason": self.reason}


class FeishuNotifier:
    def __init__(self, webhook: str = "", timeout: int = 10, dry_run: bool = False):
        self.webhook = webhook or DEFAULT_WEBHOOK
        self.timeout = timeout
        self.dry_run = dry_run

    @property
    def configured(self) -> bool:
        return bool(self.webhook)

    # ------------------------------------------------------------------

    def _post(self, payload: Dict[str, Any]) -> FeishuResult:
        if self.dry_run:
            print("[dry-run] 飞书消息体：")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return FeishuResult(ok=True, skipped=True, reason="dry-run 未实际发送")
        if not self.configured:
            return FeishuResult(ok=False, skipped=True,
                                reason="未配置 JOBFORGE_FEISHU_WEBHOOK")

        req = urllib.request.Request(
            self.webhook,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                code = body.get("code", resp.status)
                if code == 0:
                    return FeishuResult(ok=True, status=resp.status)
                return FeishuResult(ok=False, status=resp.status,
                                    error=str(body.get("msg", body)))
        except urllib.error.HTTPError as e:
            return FeishuResult(ok=False, status=e.code, error=f"HTTP {e.code}: {e.reason}")
        except Exception as e:  # noqa: BLE001 - 通知失败不应影响主流程
            return FeishuResult(ok=False, error=str(e))

    # ------------------------------------------------------------------
    # 消息构造
    # ------------------------------------------------------------------

    def build_daily_card(self, jobs: List[Any], health: Dict[str, Any],
                         stats: Dict[str, Any]) -> Dict[str, Any]:
        """每日岗位卡片。jobs 为 Job 列表，已按筛选排好序。"""
        lines = []
        for j in jobs[:10]:
            risk = " ⚠️疑似外包" if getattr(j, "is_outsourcing_risk", None) is True else ""
            sal = getattr(j, "salary_text", "") or "薪资未取到"
            city = getattr(j, "city", "") or "—"
            edu = getattr(j, "education", "") or ""
            lines.append(
                f"**{j.position}**　{sal}\n"
                f"<font color='grey'>{j.company} · {city} · {edu}</font>{risk}"
            )

        health_txt = "✅ 数据健康" if health.get("pass") else "⚠️ 数据异常"
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "blue" if health.get("pass") else "red",
                    "title": {
                        "tag": "plain_text",
                        "content": f"📮 今日新增 {stats.get('today_new', 0)} 个岗位"
                    },
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n---\n\n".join(lines)}},
                    {"tag": "hr"},
                    {"tag": "note", "elements": [
                        {"tag": "plain_text", "content":
                            f"{health_txt} · 累计 {stats.get('total', 0)} 个 · "
                            f"完整率 {round((health.get('completeness') or 0) * 100)}% · "
                            f"数据日期 {stats.get('today') or '—'}"}
                    ]},
                ],
            },
        }

    def build_alert_card(self, problems: List[str], stats: Dict[str, Any]) -> Dict[str, Any]:
        """数据异常告警 —— 数据不健康时推这个，而不是推岗位。"""
        items = "\n".join(f"· {p}" for p in problems)
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "red",
                    "title": {"tag": "plain_text", "content": "🚫 今日不推送：数据未过看门狗"},
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": items}},
                    {"tag": "note", "elements": [
                        {"tag": "plain_text", "content":
                            f"累计 {stats.get('total', 0)} 个岗位 · "
                            "这通常是采集或解析环节出问题了，请检查日志"}
                    ]},
                ],
            },
        }

    # ------------------------------------------------------------------

    def send_daily(self, jobs: List[Any], health: Dict[str, Any],
                   stats: Dict[str, Any]) -> FeishuResult:
        """每日推送入口。**不健康就只发告警，绝不推岗位。**"""
        if not health.get("pass"):
            return self._post(self.build_alert_card(health.get("problems", []), stats))
        if not jobs:
            return self._post(self.build_alert_card(
                ["今日没有符合硬性条件的新岗位"], stats))
        return self._post(self.build_daily_card(jobs, health, stats))

    def send_text(self, content: str) -> FeishuResult:
        return self._post({"msg_type": "text", "content": {"text": content}})
