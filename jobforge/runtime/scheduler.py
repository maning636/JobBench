# -*- coding: utf-8 -*-
"""
求职工作台 · 每日调度
======================

产品形态是「每天自动跑」，所以重活必须能在无人值守下完成。

对应 v1.2.0 的 P0-8：把 124 秒/次的节流采集直接塞进 Flask 请求处理函数，
导致浏览器 60 秒超时、按钮永远转圈、用户什么都拿不到。
v2 把这条纪律固化成 R3：>5 秒的操作一律 job 化，不进 HTTP 线程。

调度器负责：
  - 按 cron 触发每日任务
  - 任务状态机持久化（pending/running/done/failed），可断点续采
  - 每一步的产出与失败都写日志，不静默
"""
from __future__ import annotations

import json
import sqlite3
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Run:
    id: int = 0
    started_at: str = ""
    finished_at: str = ""
    state: str = RunState.PENDING.value
    inserted: int = 0
    updated: int = 0
    completeness: float = 0.0
    healthy: bool = True
    warnings: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RunLog:
    """任务运行记录。断点续采和"昨天到底跑没跑"都靠它。"""

    def __init__(self, db_path: str = "data/jobforge.db"):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path))
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        c = self._conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT, finished_at TEXT, state TEXT,
                inserted INTEGER DEFAULT 0, updated INTEGER DEFAULT 0,
                completeness REAL DEFAULT 0.0, healthy INTEGER DEFAULT 1,
                warnings TEXT, error TEXT
            )
        """)
        c.commit()
        c.close()

    def start(self) -> Run:
        run = Run(started_at=_now(), state=RunState.RUNNING.value)
        c = self._conn()
        cur = c.execute(
            "INSERT INTO runs (started_at, state) VALUES (?, ?)",
            (run.started_at, run.state),
        )
        run.id = cur.lastrowid
        c.commit()
        c.close()
        return run

    def finish(self, run: Run, state: str = RunState.DONE.value, error: str = "") -> Run:
        run.state = state
        run.finished_at = _now()
        run.error = error
        c = self._conn()
        c.execute(
            """UPDATE runs SET finished_at=?, state=?, inserted=?, updated=?,
                                completeness=?, healthy=?, warnings=?, error=?
               WHERE id=?""",
            (run.finished_at, run.state, run.inserted, run.updated, run.completeness,
             int(run.healthy), json.dumps(run.warnings, ensure_ascii=False),
             run.error, run.id),
        )
        c.commit()
        c.close()
        return run

    def recent(self, limit: int = 14) -> List[Dict[str, Any]]:
        c = self._conn()
        rows = c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        c.close()
        out = []
        for r in rows:
            d = dict(r)
            d["healthy"] = bool(d["healthy"])
            try:
                d["warnings"] = json.loads(d["warnings"] or "[]")
            except ValueError:
                d["warnings"] = []
            out.append(d)
        return out

    def last_success(self) -> Optional[Dict[str, Any]]:
        rows = self.recent(50)
        for r in rows:
            if r["state"] == RunState.DONE.value and r["healthy"]:
                return r
        return None


# =============================================================================
# 每日任务
# =============================================================================

def run_daily(
    store,
    gate,
    notifier,
    filters: Optional[Any] = None,
    run_log: Optional[RunLog] = None,
    fetch: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """每日任务主流程。

    顺序很重要：**先入库 → 再过看门狗 → 最后才推送**。
    数据没过关就不推，宁可让用户收不到，也不要收到一堆空岗位。

    fetch 参数留给后续接入 CDP 采集器（[决策-1] 选 B 时使用）。
    当前默认走手动导入路径，未接入 fetch 时只做「存量数据体检 + 推送」。
    """
    from jobforge.application.filters import HardFilter

    run = run_log.start() if run_log else Run(started_at=_now())
    result: Dict[str, Any] = {"run": run.to_dict()}

    try:
        jobs: List[Any] = []
        ingest_report = None

        if fetch is not None:
            fetched, errors = fetch()
            if errors:
                run.warnings.extend([str(e) for e in errors])
            if fetched:
                ingest_report = store.upsert_many(fetched)
                run.inserted = ingest_report.inserted
                run.updated = ingest_report.updated
                run.completeness = ingest_report.completeness
                run.healthy = ingest_report.healthy
                run.warnings.extend(ingest_report.warnings)

        # 筛选
        f = filters or HardFilter(hide_incomplete=True)
        all_jobs = store.list_jobs(limit=100000)
        kept = [e["job"] for e in f.apply(all_jobs)["kept"]]

        # 看门狗：拿当天入库的岗位做体检，没有则用全库
        subject = [j for j in (jobs or all_jobs)] if ingest_report else all_jobs
        health = gate.check(ingest_report, subject) if ingest_report else {
            "pass": len(all_jobs) > 0,
            "problems": [] if all_jobs else ["库中没有任何岗位数据"],
            "completeness": round(sum(j.completeness() for j in all_jobs) / len(all_jobs), 3)
            if all_jobs else 0.0,
            "salary_resolved_ratio": 0.0,
            "sample_size": len(all_jobs),
        }

        daily = store.daily_new_counts(days=1)
        stats = {
            "total": len(all_jobs),
            "today_new": daily[0]["count"] if daily else 0,
            "today": daily[0]["date"] if daily else None,
        }

        # 推送
        send = notifier.send_daily(kept, health, stats) if notifier else None
        result.update({
            "kept_count": len(kept),
            "health": health,
            "stats": stats,
            "notify": send.to_dict() if send else None,
        })

        run.healthy = bool(health.get("pass"))
        run.completeness = float(health.get("completeness") or 0.0)
        state = RunState.DONE.value if run.healthy else RunState.SKIPPED.value
        if run_log:
            run = run_log.finish(run, state)

    except Exception as e:  # noqa: BLE001 - 任务级兜底，必须记录而不是静默
        run.error = f"{type(e).__name__}: {e}"
        run.warnings.append("任务异常终止")
        if run_log:
            run = run_log.finish(run, RunState.FAILED.value, run.error)
        traceback.print_exc()
        result["error"] = run.error

    result["run"] = run.to_dict()
    return result
