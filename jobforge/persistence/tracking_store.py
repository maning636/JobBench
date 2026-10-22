# -*- coding: utf-8 -*-
"""
求职工作台 · 台账存储
======================

applications / followups / resumes 三张表。DDL 从 domain 派生（R1）。

一条重要设计：**投递记录里冗余了岗位快照**。
岗位会被清理、会被重新采集更新，但投递记录是用户的历史，
不能因为岗位变了就跟着变 —— 否则用户过两周回来看，
"我投的是那个 15-25K 的岗"就变成了"现在显示 20-30K"，记忆对不上。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from jobforge.domain.application import (
    Application, ApplyStatus, Followup, Resume,
    build_reminders, can_transition,
)
from jobforge.domain.models import sql_type_of


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_LIST_FIELDS = {"tags", "outsourcing_evidence", "variants"}
_DICT_FIELDS = {"score_details"}
_BOOL_FIELDS = {"is_outsourcing_risk", "got_reply", "is_default"}


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
    if name in _BOOL_FIELDS:
        return bool(value)
    return value


class TrackingStore:
    """投递台账 + 简历版本。与 jobs 库同一个 SQLite 文件。"""

    def __init__(self, db_path: str = "data/jobforge.db"):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self) -> None:
        c = self._conn()
        cur = c.cursor()

        # DDL 完全从 domain 派生（R1）。id 是 domain 字段但由数据库自增生成，
        # 所以列清单里要跳过，否则会生成 duplicate column（实测踩过）。
        # 类型映射复用 domain.models 的唯一实现 —— 这里曾自己写了一套字面量
        # 比较，被 `from __future__ import annotations` 坑成全部 TEXT，
        # 导致 bool('0') 也算 True（实测踩过）。
        def _cols(cls) -> str:
            out = []
            for f in fields(cls):
                if f.name == "id":
                    continue
                out.append(f"{f.name} {sql_type_of(f.type)}")
            return ", ".join(out)

        cur.execute(
            "CREATE TABLE IF NOT EXISTS applications (\n"
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            f"  {_cols(Application)}\n)")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS followups (\n"
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            f"  {_cols(Followup)}\n)")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS resumes (\n"
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            f"  {_cols(Resume)}\n)")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_app_applied ON applications(applied_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fu_app ON followups(application_id)")
        c.commit()
        c.close()

    # =====================================================================
    # 投递记录
    # =====================================================================

    def add_application(self, app: Application) -> int:
        c = self._conn()
        d = asdict(app)
        d.pop("id", None)
        cols = ", ".join(d.keys())
        marks = ", ".join(["?"] * len(d))
        cur = c.execute(f"INSERT INTO applications ({cols}) VALUES ({marks})",
                        tuple(d.values()))
        aid = cur.lastrowid
        c.commit()
        c.close()
        app.id = aid
        return aid

    def update_application(self, aid: int, **kw) -> Optional[Application]:
        if not kw:
            return self.get_application(aid)
        kw["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in kw)
        c = self._conn()
        c.execute(f"UPDATE applications SET {sets} WHERE id = ?",
                  (*kw.values(), aid))
        c.commit()
        c.close()
        return self.get_application(aid)

    def set_status(self, aid: int, status: str, force: bool = False,
                   **extra) -> Dict[str, Any]:
        """改状态。**非法流转要拦下来**并说明原因，不静默改。

        实测踩过的坑（v1.2.0 状态机）：状态可以随便改，
        用户把「已拒绝」改回「已投递」没有任何提示，
        统计口径就乱了，用户自己也不知道发生了什么。
        """
        cur_app = self.get_application(aid)
        if not cur_app:
            return {"ok": False, "error": "记录不存在"}

        if not force and not can_transition(cur_app.status, status):
            allowed = [s for s in
                       ApplyStatus.__members__.values()
                       if can_transition(cur_app.status, s.value)]
            return {
                "ok": False,
                "error": f"不能从「{cur_app.status}」直接跳到「{status}」",
                "allowed": [s.value for s in allowed],
            }

        update = {"status": status}
        if status == ApplyStatus.APPLIED.value and not cur_app.applied_at:
            update["applied_at"] = datetime.now().strftime("%Y-%m-%d")
        if status in (ApplyStatus.OFFER.value, ApplyStatus.REJECTED.value,
                      ApplyStatus.WITHDRAWN.value):
            update["result_at"] = _now()[:10]
        update.update(extra)
        self.update_application(aid, **update)
        return {"ok": True, "application": self.get_application(aid).to_dict()}

    def delete_application(self, aid: int) -> bool:
        c = self._conn()
        c.execute("DELETE FROM followups WHERE application_id = ?", (aid,))
        n = c.execute("DELETE FROM applications WHERE id = ?", (aid,)).rowcount
        c.commit()
        c.close()
        return n > 0

    def _row_to_app(self, row: sqlite3.Row) -> Application:
        d = dict(row)
        for k in list(d.keys()):
            d[k] = _decode(k, d[k])
        return Application.from_dict(d)

    def get_application(self, aid: int) -> Optional[Application]:
        c = self._conn()
        row = c.execute("SELECT * FROM applications WHERE id = ?", (aid,)).fetchone()
        c.close()
        return self._row_to_app(row) if row else None

    def list_applications(self, status: Optional[str] = None,
                           open_only: bool = False,
                           limit: int = 500) -> List[Application]:
        sql = "SELECT * FROM applications WHERE 1=1"
        args: List[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if open_only:
            sql += " AND status IN ('saved','applied','assessment','interview')"
        sql += " ORDER BY COALESCE(NULLIF(applied_at,''), created_at) DESC, id DESC LIMIT ?"
        args.append(limit)
        c = self._conn()
        rows = c.execute(sql, args).fetchall()
        c.close()
        return [self._row_to_app(r) for r in rows]

    # =====================================================================
    # 跟进
    # =====================================================================

    def add_followup(self, f: Followup) -> int:
        d = asdict(f)
        d.pop("id", None)
        cols = ", ".join(d.keys())
        marks = ", ".join(["?"] * len(d))
        c = self._conn()
        cur = c.execute(f"INSERT INTO followups ({cols}) VALUES ({marks})",
                        tuple(d.values()))
        fid = cur.lastrowid
        # 同步回应用的计数与最近跟进时间；有回复则一并记录
        c.execute(
            """UPDATE applications SET
                 followup_count = (SELECT COUNT(*) FROM followups
                                   WHERE application_id = ?),
                 last_followup_at = ?,
                 reply = CASE WHEN ? != '' THEN ? ELSE reply END,
                 updated_at = ?
               WHERE id = ?""",
            (f.application_id, f.at, f.reply or "", f.reply or "", _now(),
             f.application_id),
        )
        c.commit()
        c.close()
        return fid

    def list_followups(self, aid: int) -> List[Dict[str, Any]]:
        c = self._conn()
        rows = c.execute(
            "SELECT * FROM followups WHERE application_id = ? ORDER BY at DESC, id DESC",
            (aid,)).fetchall()
        c.close()
        out = []
        for r in rows:
            d = dict(r)
            d["got_reply"] = bool(d.get("got_reply"))
            out.append(d)
        return out

    # =====================================================================
    # 简历版本
    # =====================================================================

    def add_resume(self, r: Resume) -> int:
        c = self._conn()
        if r.is_default:
            c.execute("UPDATE resumes SET is_default = 0")
        d = asdict(r)
        d.pop("id", None)
        cols = ", ".join(d.keys())
        marks = ", ".join(["?"] * len(d))
        cur = c.execute(f"INSERT INTO resumes ({cols}) VALUES ({marks})",
                        tuple(d.values()))
        rid = cur.lastrowid
        c.commit()
        c.close()
        r.id = rid
        return rid

    def update_resume(self, rid: int, **kw) -> bool:
        c = self._conn()
        if kw.get("is_default"):
            c.execute("UPDATE resumes SET is_default = 0")
        kw["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in kw)
        c.execute(f"UPDATE resumes SET {sets} WHERE id = ?", (*kw.values(), rid))
        c.commit()
        c.close()
        return True

    def delete_resume(self, rid: int) -> bool:
        c = self._conn()
        n = c.execute("DELETE FROM resumes WHERE id = ?", (rid,)).rowcount
        c.commit()
        c.close()
        return n > 0

    def list_resumes(self) -> List[Dict[str, Any]]:
        c = self._conn()
        rows = c.execute("SELECT * FROM resumes ORDER BY is_default DESC, id DESC").fetchall()
        c.close()
        out = []
        for r in rows:
            d = dict(r)
            d["is_default"] = bool(d.get("is_default"))
            d["hash"] = Resume(content=d.get("content", "")).hash
            d["length"] = len(d.get("content") or "")
            # id 必须回传 —— 否则上层无法 set_default（实测踩过：
            # list_resumes 不给 id，改默认时拿着 None 去 UPDATE，界面点了没反应）
            out.append(d)
        return out

    def get_resume(self, rid: int) -> Optional[Resume]:
        c = self._conn()
        row = c.execute("SELECT * FROM resumes WHERE id = ?", (rid,)).fetchone()
        c.close()
        if not row:
            return None
        return Resume.from_dict({k: v for k, v in dict(row).items()})

    def get_default_resume(self) -> Optional[Resume]:
        c = self._conn()
        row = c.execute(
            "SELECT * FROM resumes ORDER BY is_default DESC, id DESC LIMIT 1").fetchone()
        c.close()
        if not row:
            return None
        return Resume.from_dict({k: v for k, v in dict(row).items()})

    # =====================================================================
    # 提醒与统计
    # =====================================================================

    def reminders(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in build_reminders(self.list_applications())]

    def tracking_summary(self) -> Dict[str, Any]:
        apps = self.list_applications()
        by_status: Dict[str, int] = {}
        for a in apps:
            by_status[a.status] = by_status.get(a.status, 0) + 1
        open_n = sum(1 for a in apps if ApplyStatus(a.status).is_open) if apps else 0
        closed = [a for a in apps if ApplyStatus(a.status).is_closed]
        replied = [a for a in closed if a.status == ApplyStatus.OFFER.value]
        # 回复率：已进入终态的里面拿到 offer 的比例
        decided = [a for a in closed if a.status in
                   (ApplyStatus.OFFER.value, ApplyStatus.REJECTED.value)]
        reply_rate = round(len(replied) / len(decided) * 100) if decided else None
        return {
            "total": len(apps),
            "open": open_n,
            "by_status": by_status,
            "decided": len(decided),
            "offer": len(replied),
            "reply_rate": reply_rate,
            "followups": sum(a.followup_count for a in apps),
        }
