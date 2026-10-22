# -*- coding: utf-8 -*-
"""
求职工作台 · SQLite 存储
==========================

唯一持久化实现。v1.2.0 有两套存储（SQLite + data_manager 的 JSON）互不相通，
Agent 写的数据 Web 看不到，Web 抓的数据 Agent 读不到。这里合并成一套。

核心差异：
- 表结构由 domain/models.py 派生（build_ddl），不存在"改了字段忘同步 DDL"
- 去重用 fingerprint（平台ID或 公司+岗位+城市 稳定组合），不用 `title|company`
- 每日增量靠 `upsert_many` 返回 (新增数, 已有数)，首次发现时间不被覆盖
- 完整率看门狗：低于阈值时 `ingest_report()` 会明确标记为不健康，不静默通过
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jobforge.domain.models import (
    Job, JobStatus, SalaryState, SCHEMA_VERSION,
    build_ddl, build_json_schema,
)

_LIST_FIELDS = {"tags", "outsourcing_evidence"}
_DICT_FIELDS = {"score_details"}
_BOOL_FIELDS = {"is_outsourcing_risk"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ddl_columns(ddl: str) -> Dict[str, str]:
    """从 CREATE TABLE 语句解析出 {列名: 完整列定义}，供迁移做 diff。"""
    out: Dict[str, str] = {}
    for line in ddl.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith("CREATE"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] not in ("", "("):
            out[parts[0]] = parts[1]
    return out


def _encode(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _decode(name: str, value: Any) -> Any:
    if value is None:
        return [] if name in _LIST_FIELDS else ({} if name in _DICT_FIELDS else None)
    if name in _LIST_FIELDS:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return []
    if name in _DICT_FIELDS:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value


@dataclass
class IngestReport:
    """一次导入的结果 —— 数据健康度必须可见。"""
    received: int = 0
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    incomplete: int = 0
    completeness: float = 0.0
    healthy: bool = True
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "received": self.received, "inserted": self.inserted,
            "updated": self.updated, "duplicates": self.duplicates,
            "incomplete": self.incomplete, "completeness": self.completeness,
            "healthy": self.healthy, "warnings": self.warnings,
        }


def _coerce(name: str, value: Any) -> Any:
    """把 SQLite 取出的值还原成领域模型期望的类型。

    这不是洁癖：sqlite3 没有 bool 类型，存进去的 True 读出来是 1。
    而 `1 is True` 在 Python 里恒为 False —— 实测导致整个外包识别功能
    在展示层失效：卡片无风险标记、飞书无 ⚠️，而数据明明是对的。
    与 v1.2.0「数据在缝合处静默丢失」同一类缺陷。
    """
    if name in _LIST_FIELDS | _DICT_FIELDS:
        return _decode(name, value)
    if name in _BOOL_FIELDS:
        if value is None:
            return None
        return bool(value)
    return value


class Store:
    def __init__(self, db_path: str = "data/jobforge.db", min_completeness: float = 0.6):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.min_completeness = min_completeness
        self._init_db()

    # ------------------------------------------------------------------
    # 连接 / 建表
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _init_db(self) -> None:
        c = self._conn()
        # 物理主键由存储层注入，领域模型不感知数据库自增主键。
        # 业务去重靠 fingerprint 唯一索引，不靠自增 ID。
        ddl = build_ddl("jobs").replace(
            "CREATE TABLE IF NOT EXISTS jobs (\n",
            "CREATE TABLE IF NOT EXISTS jobs (\n    row_id INTEGER PRIMARY KEY AUTOINCREMENT,\n",
        )
        c.execute(ddl)
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.commit()
        c.close()
        self._migrate()

    def _migrate(self) -> List[str]:
        """轻量迁移：领域模型新增字段后，旧库自动补列。

        CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
        所以改了 models.py 之后老用户的库会直接报
        "table jobs has no column named xxx"。这个坑每个开源项目都会踩一次。
        这里做列级 diff + ALTER TABLE ADD COLUMN，不引入 alembic 级别的框架。
        """
        added: List[str] = []
        c = self._conn()
        cur = c.cursor()

        have = {r["name"] for r in cur.execute("PRAGMA table_info(jobs)")}
        want = _ddl_columns(build_ddl("jobs"))

        for name, decl in want.items():
            if name in have or name == "row_id":
                continue
            cur.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
            added.append(name)

        # 索引在迁移后补建（老库可能缺）
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_salary ON jobs(salary_min)")

        # 去重索引必须最后建：老库可能有重复指纹，先按 row_id 清理再收紧
        if added:
            cur.execute("""
                DELETE FROM jobs WHERE row_id NOT IN (
                    SELECT MIN(row_id) FROM jobs GROUP BY fingerprint
                )
            """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_fp ON jobs(fingerprint)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS semantic_cache (
                job_fp TEXT NOT NULL,
                candidate_hash TEXT NOT NULL,
                result TEXT NOT NULL,
                model TEXT DEFAULT '',
                created_at TEXT,
                PRIMARY KEY (job_fp, candidate_hash)
            )
        """)

        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,)
        )
        c.commit()
        c.close()
        return added

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def upsert_many(self, jobs: List[Job]) -> IngestReport:
        """批量增量入库。已存在的只更新可变字段，first_seen_at 永不被覆盖。"""
        rep = IngestReport(received=len(jobs))
        if not jobs:
            rep.warnings.append("本次没有任何岗位数据")
            rep.healthy = False
            return rep

        c = self._conn()
        cur = c.cursor()
        completeness_sum = 0.0

        for job in jobs:
            completeness_sum += job.completeness()
            if job.missing_required():
                rep.incomplete += 1

            existing = cur.execute(
                "SELECT row_id, first_seen_at FROM jobs WHERE fingerprint = ?",
                (job.fingerprint,)
            ).fetchone()

            data = job.to_dict()
            data.pop("completeness", None)
            data = {k: _encode(v) for k, v in data.items()}
            data["updated_at"] = _now()

            if existing:
                # 首次发现时间与状态不被覆盖
                data.pop("first_seen_at", None)
                data.pop("created_at", None)
                data.pop("status", None)
                sets = ", ".join(f"{k} = ?" for k in data)
                cur.execute(
                    f"UPDATE jobs SET {sets} WHERE row_id = ?",
                    (*data.values(), existing["row_id"])
                )
                rep.updated += 1
            else:
                cols = ", ".join(data.keys())
                marks = ", ".join(["?"] * len(data))
                cur.execute(
                    f"INSERT INTO jobs ({cols}) VALUES ({marks})",
                    tuple(data.values())
                )
                rep.inserted += 1

        c.commit()
        c.close()

        rep.completeness = round(completeness_sum / len(jobs), 3)
        rep.healthy = self._assess_health(rep)
        return rep

    def _assess_health(self, rep: IngestReport) -> bool:
        """数据健康度判定。**不健康就是不健康，不静默放行。**

        v1.2.0 的随包演示数据 15 条岗位 salary 全空、company 存的是"5-10年"，
        文档里却宣称"找到 15 个岗位"。这里让这种情况直接判为不健康。
        """
        if rep.inserted == 0 and rep.updated == 0:
            rep.warnings.append("没有任何岗位入库 —— 采集或解析环节可能已断")
            return False
        if rep.completeness < self.min_completeness:
            rep.warnings.append(
                f"完整率 {rep.completeness:.0%} 低于阈值 {self.min_completeness:.0%}"
            )
            return False
        if rep.incomplete > rep.received * 0.2:
            rep.warnings.append(
                f"{rep.incomplete}/{rep.received} 条缺少必填字段"
                "（岗位名/公司/薪资/城市），不应推送给用户"
            )
            return False
        return True

    def set_status(self, job_id: int, status: str, note: str = "") -> None:
        c = self._conn()
        c.execute(
            "UPDATE jobs SET status = ?, note = ?, updated_at = ? WHERE row_id = ?",
            (status, note, _now(), job_id)
        )
        c.commit()
        c.close()

    def set_evaluation(self, job_id: int, score: float, score_details: Dict[str, Any],
                       match_score: Optional[float] = None,
                       is_outsourcing: Optional[bool] = None,
                       evidence: Optional[List[str]] = None) -> None:
        c = self._conn()
        c.execute(
            """UPDATE jobs SET score = ?, score_details = ?, match_score = ?,
                              is_outsourcing_risk = ?, outsourcing_evidence = ?,
                              updated_at = ? WHERE row_id = ?""",
            (score, json.dumps(score_details, ensure_ascii=False), match_score,
             None if is_outsourcing is None else int(is_outsourcing),
             json.dumps(evidence or [], ensure_ascii=False), _now(), job_id)
        )
        c.commit()
        c.close()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        d = dict(row)
        row_id = d.pop("row_id", None)
        for name in list(d.keys()):
            d[name] = _coerce(name, d[name])
        job = Job.from_dict(d)
        # 数据库主键挂在实例上（不属于领域模型字段，故不入 DDL/CSV）
        job.db_id = row_id
        return job

    def list_jobs(self, status: Optional[str] = None, channel: Optional[str] = None,
                  since: Optional[str] = None, min_score: Optional[float] = None,
                  order: str = "score DESC, first_seen_at DESC",
                  limit: int = 100, offset: int = 0) -> List[Job]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        args: List[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if channel:
            sql += " AND channel = ?"
            args.append(channel)
        if since:
            sql += " AND first_seen_at >= ?"
            args.append(since)
        if min_score is not None:
            sql += " AND score IS NOT NULL AND score >= ?"
            args.append(min_score)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        args += [limit, offset]
        c = self._conn()
        rows = c.execute(sql, args).fetchall()
        c.close()
        return [self._row_to_job(r) for r in rows]

    def get(self, job_id: int) -> Optional[Job]:
        c = self._conn()
        row = c.execute("SELECT * FROM jobs WHERE row_id = ?", (job_id,)).fetchone()
        c.close()
        return self._row_to_job(row) if row else None

    def count(self, **filters) -> int:
        sql = "SELECT COUNT(*) FROM jobs WHERE 1=1"
        args: List[Any] = []
        for k, v in filters.items():
            if v is not None:
                sql += f" AND {k} = ?"
                args.append(v)
        c = self._conn()
        n = c.execute(sql, args).fetchone()[0]
        c.close()
        return n

    def status_breakdown(self) -> Dict[str, int]:
        c = self._conn()
        rows = c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        c.close()
        return {r["status"]: r["n"] for r in rows}

    def daily_new_counts(self, days: int = 14) -> List[Dict[str, Any]]:
        c = self._conn()
        rows = c.execute(
            """SELECT substr(first_seen_at, 1, 10) d, COUNT(*) n
               FROM jobs GROUP BY d ORDER BY d DESC LIMIT ?""", (days,)
        ).fetchall()
        c.close()
        return [{"date": r["d"], "count": r["n"]} for r in rows]

    def clear(self) -> int:
        c = self._conn()
        n = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        c.execute("DELETE FROM jobs")
        c.commit()
        c.close()
        return n

    # ------------------------------------------------------------------
    # 语义分析缓存
    # ------------------------------------------------------------------
    # 为什么必须缓存：批量分析 15 个岗位，每次 7~11 秒，
    # 不缓存就是每次都重新调一次模型 —— 慢且烧钱。
    # 键用「岗位指纹 + 简历哈希」：任一变了才失效，
    # 岗位内容变了或用户改了简历都会正确触发重新分析。

    @staticmethod
    def candidate_hash(resume_text: str, skill_names: List[str]) -> str:
        import hashlib as _h
        blob = (resume_text or "") + "|" + "|".join(sorted(skill_names or []))
        return _h.sha1(blob.encode("utf-8")).hexdigest()[:16]

    def get_semantic(self, job_fp: str, cand_hash: str) -> Optional[Dict[str, Any]]:
        c = self._conn()
        row = c.execute(
            "SELECT result FROM semantic_cache WHERE job_fp = ? AND candidate_hash = ?",
            (job_fp, cand_hash),
        ).fetchone()
        c.close()
        if not row:
            return None
        try:
            return json.loads(row["result"])
        except Exception:  # noqa: BLE001
            return None

    def put_semantic(self, job_fp: str, cand_hash: str,
                     result: Dict[str, Any], model: str = "") -> None:
        c = self._conn()
        c.execute(
            """INSERT OR REPLACE INTO semantic_cache
               (job_fp, candidate_hash, result, model, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (job_fp, cand_hash, json.dumps(result, ensure_ascii=False),
             model, _now()),
        )
        c.commit()
        c.close()

    def semantic_stats(self) -> Dict[str, int]:
        c = self._conn()
        n = c.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
        c.close()
        return {"cached": n}

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def export_csv(self, path: str, jobs: Optional[List[Job]] = None) -> str:
        import csv
        from jobforge.domain.models import build_csv_columns
        cols = build_csv_columns()
        jobs = jobs if jobs is not None else self.list_jobs(limit=100000)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for j in jobs:
                d = j.to_dict()
                for k in _LIST_FIELDS | _DICT_FIELDS:
                    if k in d and d[k] is not None:
                        d[k] = json.dumps(d[k], ensure_ascii=False)
                w.writerow(d)
        return path
