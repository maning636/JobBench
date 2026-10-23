# -*- coding: utf-8 -*-
"""
求职工作台 · 每日采集任务
==========================

回答「岗位太少」这件事：**从今天开始每天自动积累**。
平台不提供"按发布时间回溯"的能力，所以历史只能靠时间换 ——
今天跑一次，明天就有第二天的增量，一个月后才有 30 天的厚度。

三条硬约束（都是从 v1.2.0 的坑来的）：
1. **失败必须可见**。抓不到 ≠ 今天没岗位。任务失败要记 error 并在界面显示，
   绝不能让用户以为「今天没新岗位」（v1.2.0 的静默失败）。
2. **可断点续采**。中途停了，下次接着跑，不从头再来。
3. **数据不达标不推进游标**。完整率不过关就不能标记「这批已处理」，
   否则坏数据会被永久跳过、且再也不会重抓。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from jobforge.application.filters import DataGate
from jobforge.persistence.sqlite_store import IngestReport, Store
from jobforge.domain.models import Job

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class CollectPlan:
    """一次每日任务要采什么。"""
    keyword: str = "Java"
    city: str = ""
    pages: int = 3
    sources: List[str] = field(default_factory=lambda: ["boss"])


@dataclass
class CollectOutcome:
    """一次任务的结果 —— 失败原因必须在最显眼的位置。"""
    ok: bool = False
    source: str = ""
    keyword: str = ""
    pages_done: int = 0
    pages_total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0            # 断点续采：上次已完成，本次跳过
    completeness: float = 0.0
    elapsed: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    finished_at: str = ""

    def headline(self) -> str:
        """一句话结论。成功要说清拿了多少，失败要说清为什么。"""
        if not self.ok:
            return f"❌ {self.source} 采集失败：{self.errors[0] if self.errors else '未知原因'}"
        if self.inserted == 0 and self.updated == 0:
            return (f"⚠️ {self.source} 采到 0 条（新增 0 / 更新 0），"
                    "可能是没有新岗位，也可能是选择器失效了 —— 请点开看错误")
        return (f"✅ {self.source}：新增 {self.inserted} 个岗位"
                f"（更新 {self.updated}，耗时 {self.elapsed:.0f}s）")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RunStore:
    """任务运行记录 + 断点游标。

    游标语义：**只记录「这一批源数据已完整处理」**。
    如果一轮采集完整率不达标，游标不推进 —— 下次会重抓同一批，
    而不是把坏数据标记成「已处理」后永远跳过。
    """

    def __init__(self, db_path: str = "data/jobforge.db"):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self):
        c = sqlite3.connect(str(self.path))
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        c = self._conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS collect_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT, finished_at TEXT,
                source TEXT, keyword TEXT, city TEXT, pages_total INTEGER,
                pages_done INTEGER, inserted INTEGER, updated INTEGER,
                completeness REAL, ok INTEGER, elapsed REAL,
                errors TEXT, warnings TEXT
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS collect_cursor (
                source TEXT, keyword TEXT, city TEXT,
                last_ok_at TEXT, last_page_done INTEGER DEFAULT 0,
                run_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0,
                PRIMARY KEY (source, keyword, city)
            )""")
        c.commit()
        c.close()

    def cursor(self, source: str, keyword: str, city: str) -> Dict[str, Any]:
        c = self._conn()
        row = c.execute(
            "SELECT * FROM collect_cursor WHERE source=? AND keyword=? AND city=?",
            (source, keyword, city)).fetchone()
        c.close()
        return dict(row) if row else {"last_page_done": 0, "run_count": 0,
                                       "fail_count": 0, "last_ok_at": ""}

    def advance(self, source: str, keyword: str, city: str,
                page_done: int, ok: bool) -> None:
        """推进游标。

        **失败时只累计失败次数，绝不推进 last_page_done。**
        这是关键：游标一旦误推进，那一批坏数据会被永久跳过、再也不重抓。

        实现上刻意用「先读后算再写」而不是一条 UPSERT + CASE：
        实测踩过 —— UPSERT 里 CASE 的多个占位符顺序容易错位，
        失败时把 last_page_done 也更新了，静默跳过了坏数据。
        """
        c = self._conn()
        cur = self.cursor(source, keyword, city)
        now = _now()
        run_count = (cur["run_count"] or 0) + 1
        fail_count = (cur["fail_count"] or 0) + (0 if ok else 1)
        new_page = page_done if ok else (cur["last_page_done"] or 0)
        last_ok = now if ok else (cur["last_ok_at"] or "")

        c.execute(
            """INSERT INTO collect_cursor
               (source, keyword, city, last_ok_at, last_page_done, run_count, fail_count)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(source, keyword, city) DO UPDATE SET
                 last_ok_at = excluded.last_ok_at,
                 last_page_done = excluded.last_page_done,
                 run_count = excluded.run_count,
                 fail_count = excluded.fail_count""",
            (source, keyword, city, last_ok, new_page, run_count, fail_count))
        c.commit()
        c.close()

    def start_run(self, o: CollectOutcome) -> int:
        c = self._conn()
        cur = c.execute(
            """INSERT INTO collect_runs
               (started_at, source, keyword, pages_total, ok)
               VALUES (?,?,?,?,?)""",
            (_now(), o.source, o.keyword, o.pages_total, 0))
        rid = cur.lastrowid
        c.commit()
        c.close()
        return rid

    def finish_run(self, rid: int, o: CollectOutcome) -> None:
        c = self._conn()
        c.execute(
            """UPDATE collect_runs SET finished_at=?, pages_done=?, inserted=?,
                   updated=?, completeness=?, ok=?, elapsed=?, errors=?, warnings=?
               WHERE id=?""",
            (_now(), o.pages_done, o.inserted, o.updated, o.completeness,
             1 if o.ok else 0, o.elapsed,
             json.dumps(o.errors, ensure_ascii=False),
             json.dumps(o.warnings, ensure_ascii=False), rid))
        c.commit()
        c.close()

    def recent(self, limit: int = 14) -> List[Dict[str, Any]]:
        c = self._conn()
        rows = c.execute(
            "SELECT * FROM collect_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        c.close()
        out = []
        for r in rows:
            d = dict(r)
            d["ok"] = bool(d["ok"])
            for k in ("errors", "warnings"):
                try:
                    d[k] = json.loads(d[k] or "[]")
                except ValueError:
                    d[k] = []
            out.append(d)
        return out

    def health(self) -> Dict[str, Any]:
        """最近一次任务的状态 —— 首页要显示它，否则用户无从判断数据是否在增长。"""
        recent = self.recent(7)
        if not recent:
            return {"status": "never", "text": "还没跑过每日采集"}
        last = recent[0]
        fails = [r for r in recent if not r["ok"]]
        if not last["ok"]:
            return {"status": "fail", "text": f"上次采集失败：{last['errors'][0] if last['errors'] else '未知'}",
                    "at": last["finished_at"]}
        total7 = sum(r["inserted"] for r in recent)
        return {"status": "ok",
                "text": f"累计新增 {total7} 个（近 7 次）",
                "at": last["finished_at"],
                "fail_count": len(fails)}


# =============================================================================
# 任务编排
# =============================================================================

def run_collect(plan: CollectPlan,
                store: Store,
                runs: RunStore,
                fetcher: Optional[Callable[[CollectPlan], Any]] = None,
                resume: bool = True,
                progress: Optional[Callable[[str], None]] = None) -> CollectOutcome:
    """执行一次采集。

    `fetcher` 是真正的抓取实现，返回 (jobs, errors, pages_done)。
    默认 None —— 此时只做「登记」不做「抓取」，
    因为没接源的时候必须说清楚，而不是假装跑了。

    注意：完整性门槛固定用 DataGate(min_new_jobs=0)，
    「新增 0 条」不算失败 —— 断点续采时本来就可能一条新的都没有。
    """
    t0 = time.time()
    o = CollectOutcome(source=plan.sources[0] if plan.sources else "unknown",
                       keyword=plan.keyword, pages_total=plan.pages)

    rid = runs.start_run(o)

    if fetcher is None:
        o.ok = False
        o.errors.append("未配置抓取源。可用：手动导入（web 界面「导入岗位」）")
        o.elapsed = time.time() - t0
        o.finished_at = _now()
        runs.finish_run(rid, o)
        return o

    cur = runs.cursor(o.source, plan.keyword, plan.city) if resume else \
        {"last_page_done": 0}
    start_page = (cur["last_page_done"] + 1) if resume and cur["last_page_done"] else 1

    if start_page > 1:
        o.skipped = start_page - 1
        if progress:
            progress(f"断点续采：从第 {start_page} 页开始（已跳过 {o.skipped} 页）")

    try:
        p2 = CollectPlan(keyword=plan.keyword, city=plan.city,
                         pages=plan.pages, sources=plan.sources)
        p2.pages = max(0, plan.pages - o.skipped)
        if p2.pages <= 0:
            o.warnings.append("已处理的页数超过计划页数，本次无事可做")
            o.ok = True
        else:
            fetched, errors, done = fetcher(p2)
            # 翻页过程中的零星失败是**警告**不是失败 —— 只要拿到了数据、
            # 数据又过了完整性门槛，这一批就算处理过，游标可以推进。
            # 实测踩过：把翻页错误当成任务失败，导致游标不推进，
            # 断点续采永远从头开始。
            o.warnings.extend(errors)
            o.pages_done = o.skipped + done

            if fetched:
                rep = store.upsert_many(fetched)
                o.inserted, o.updated = rep.inserted, rep.updated
                o.completeness = rep.completeness
                o.warnings.extend(rep.warnings)

                # 数据完整性看门狗（不看数量）
                #
                # 实测踩过的坑：曾经把「新增 0 条」也当成失败，
                # 结果断点续采时游标永远不推进 —— 续采本来就可能一条新的都没有
                # （岗位都已在库里），这不是错误。
                g = DataGate(min_new_jobs=0).check(rep, fetched)
                if not g["pass"]:
                    o.ok = False
                    o.errors.append("数据未过看门狗：" + "；".join(g["problems"]))
                else:
                    o.ok = True
                    if o.inserted == 0:
                        o.warnings.append(
                            "本轮没有新增岗位（可能都在库里了），游标正常推进")
            else:
                o.ok = False
                if not o.errors:
                    o.errors.append("采集到 0 个岗位 —— 请确认是否已登录，"
                                    "或平台选择器已失效")
    except Exception as e:  # noqa: BLE001
        o.ok = False
        o.errors.append(f"{type(e).__name__}: {e}")
        logger.exception("采集任务异常")

    o.elapsed = time.time() - t0
    o.finished_at = _now()

    # 只有成功且数据过门槛才推进游标
    runs.advance(o.source, plan.keyword, plan.city, o.pages_done, o.ok)
    runs.finish_run(rid, o)
    return o


def collector_fetcher_factory(collector) -> Callable[[CollectPlan], Any]:
    """把任意采集器包成 run_collect 需要的 fetcher 形状。

    BOSS 和智联都是 `collect(keyword, city, pages) -> (jobs, errors)`，
    所以同一个 factory 通吃 —— 平台差异全部封在各自的采集器里。
    """
    def _fetch(plan: CollectPlan):
        jobs, errors = collector.collect(plan.keyword, plan.city, plan.pages)
        return jobs, errors, plan.pages
    return _fetch


# 旧名保留：外面可能还有地方按这个名字调用
boss_fetcher_factory = collector_fetcher_factory


# =============================================================================
# Windows 每日计划任务
# =============================================================================

TASK_NAME = "求职工作台 每日采集"


def plan_install(db_path: str = "data/jobforge.db", keyword: str = "Java",
                 city: str = "", at: str = "09:30") -> int:
    """安装 Windows 每日计划任务。

    **一个绕不开的前提要说清楚**：
    抓取需要用户自己的 Chrome 登录态，而计划任务运行时通常没有浏览器在跑。
    所以这里安装的是两个任务：
      · 一个在开机时用调试端口拉起 Chrome（长期驻留，保留登录态）
      · 一个每天定时跑采集

    如果你的电脑会关机，就装不了这个 —— 那就改成手动跑 `collect`，
    每天一次，同样能积累出历史。这一点不对你隐瞒。
    """
    p = _resolve_plan(db_path, keyword, city, at)
    if not p.get("ok"):
        print(f"✗ {p['error']}")
        for line in p.get("manual_hint", "").splitlines():
            print(f"  {line}")
        return 1

    r = _install_pair(p)

    print("=" * 62)
    print("  每日自动采集已配置")
    print("=" * 62)
    print(f"  浏览器：{r['browser_rc']}")
    print(f"    {r['chrome_cmd']}")
    print(f"  采集：{r['collect_rc']}")
    print(f"    每天 {r['time']}　关键词「{r['keyword']}」　{r['city']}")
    print()
    print("  首次使用请注意：")
    print("   1. 手动启动一次上面那条 Chrome 命令")
    print("   2. 在那个窗口里登录 BOSS 直聘（只需一次，登录态会保留）")
    print("   3. 验证：python -m jobforge.cli collect --check")
    print("   4. 试跑一次：python -m jobforge.cli collect")
    print()
    print("  卸载：schtasks /Delete /TN \"%s\" /F　和　\"%s - Browser\" /F" % (TASK_NAME, TASK_NAME))
    print()
    print("  ⚠ 电脑关机时计划任务不会执行。这种情况请手动跑 collect，")
    print("    每天一次同样能积累历史 —— 别指望它自动补跑。")
    return 0 if r["ok"] else 1


def _resolve_plan(db_path: str, keyword: str, city: str, at: str) -> dict:
    """校验参数 + 定位浏览器和解释器。ok=False 时 error 是给人看的中文。

    CLI 和网页端共用这一份，参数校验只有一处（纪律 R1）。
    """
    import sys

    at = (at or "").strip() or "09:30"
    hh, _, mm = at.partition(":")
    if not (hh.isdigit() and mm.isdigit()
            and len(hh) == 2 and len(mm) == 2
            and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        return {"ok": False, "error": f"时间格式不对：{at}（应形如 09:30）"}

    # db 路径解析规则（唯一一条，不猜）：
    #   绝对路径 → 原样用
    #   相对路径 → 锚到项目根
    # 之前是「cwd 里存在就用 cwd 的，否则用项目根」的启发式 ——
    # 同一条命令在两个目录会解析出两个不同的库，用户会以为岗位丢了。
    if Path(db_path).is_absolute():
        db_abs = Path(db_path)
    else:
        db_abs = (Path(__file__).resolve().parents[2] / db_path).resolve()

    chrome = _find_chrome()
    if not chrome:
        return {
            "ok": False,
            "error": "没找到 Chrome / Edge 安装路径，无法自动拉起带调试端口的浏览器。",
            "db_abs": str(db_abs),
            "manual_hint": (
                '请手动启动一次（路径按你的实际安装位置调整）：\n'
                '"chrome.exe" --remote-debugging-port=9222 '
                '--user-data-dir="%TEMP%\\chrome-jobforge"\n'
                f'然后只安装采集任务：python -m jobforge.cli install-task --time {at}'
            ),
        }

    # 指向一个不存在的库去建每日任务 = 每天都会失败，而且用户要到第二天
    # 才发现。与其装个必坏的东西，不如当场说清（纪律 R2）。
    if not Path(db_abs).exists():
        return {"ok": False,
                "error": f"找不到数据库文件：{db_abs}",
                # 带上解析结果：用户/测试都要能确认「路径到底解析到哪了」。
                "db_abs": str(db_abs),
                "manual_hint": "请先在本工具里正常用过一次，让它把库建起来，再来开启每日自动采集。"}

    return {
        "ok": True, "at": at, "hh": int(hh), "mm": int(mm),
        "python": sys.executable, "db_abs": str(db_abs), "chrome": chrome,
        "keyword": keyword or "Java", "city": city or "",
        "user_data": str(Path.home() / "AppData" / "Local" / "Temp" / "chrome-jobforge"),
    }


def _install_pair(p: dict) -> dict:
    """真正调 schtasks 建那两个任务。"""
    chrome_cmd = (f'start "" "{p["chrome"]}" --remote-debugging-port=9222 '
                  f'--user-data-dir="{p["user_data"]}"')
    collect_cmd = (f'"{p["python"]}" -m jobforge.cli --db "{p["db_abs"]}" collect '
                   f'--keyword "{p["keyword"]}"'
                   + (f' --city "{p["city"]}"' if p["city"] else ""))

    r1 = _schtasks("/Create", "/F", "/SC", "ONLOGON",
                   "/TN", f"{TASK_NAME} - Browser", "/TR", chrome_cmd)
    r2 = _schtasks("/Create", "/F", "/SC", "DAILY", "/ST", f"{p['hh']:02d}:{p['mm']:02d}",
                   "/TN", TASK_NAME, "/TR", collect_cmd)
    return {
        "ok": r1 == 0 and r2 == 0,
        "browser_rc": r1, "collect_rc": r2,
        "chrome_cmd": chrome_cmd, "collect_cmd": collect_cmd,
        "time": f"{p['hh']:02d}:{p['mm']:02d}",
        "keyword": p["keyword"], "city": p["city"] or "全国",
    }


def plan_install_web(db_path: str = "data/jobforge.db", keyword: str = "Java",
                     city: str = "", at: str = "09:30") -> dict:
    """给网页端用：跟 CLI 装的是同一套东西，但返回结构化结果 + 人话，
    不往终端打印 —— 用户是大学生，不该让他看见 schtasks 这类东西。"""
    p = _resolve_plan(db_path, keyword, city, at)
    if not p.get("ok"):
        return p

    r = _install_pair(p)
    r["message"] = (
        f"已设置：每天 {r['time']} 自动采集（关键词「{r['keyword']}」·{r['city']}），"
        f"开机时自动打开专用浏览器并保持登录。"
        if r["ok"] else
        "没能装成功。多半是权限不够 —— 请右键「以管理员身份运行」本工具再点一次。"
    )
    # 关机这条限制必须跟着结果一起给出去，不能只写在文档里
    r["caveat"] = "电脑关机的那几天计划任务不会跑，那几天回来手动点一次「立即采集」就行。"
    return r


def _find_chrome() -> str:
    import os
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if Path(c).exists():
            return c
    return ""


def plan_install_status() -> dict:
    """查每日计划任务装没装 —— 网页端按钮要显示"开启/已开启"，不能瞎标。"""
    import subprocess

    def _query(name: str) -> bool:
        try:
            r = subprocess.run(["schtasks", "/Query", "/TN", name],
                               capture_output=True, text=True, timeout=15)
            return r.returncode == 0
        except Exception as e:  # noqa: BLE001
            logger.warning("查询计划任务失败：%s", e)
            return False

    installed = _query(TASK_NAME)
    return {
        "ok": True,
        "installed": installed,
        "browser_task": _query(f"{TASK_NAME} - Browser"),
        "task_name": TASK_NAME,
        # 半装状态必须说出来：只建了采集任务、没建浏览器任务 = 每天都会失败
        "message": ("每日自动采集已开启" if installed and _query(f"{TASK_NAME} - Browser")
                    else "每日采集任务在，但开机启动浏览器那项没了，需要重新开启"
                    if installed else "还没开启每日自动采集"),
    }


def _schtasks(*args) -> int:
    import subprocess
    try:
        r = subprocess.run(["schtasks", *args], capture_output=True, text=True,
                           timeout=30)
        if r.returncode != 0:
            logger.warning("schtasks 失败：%s", (r.stderr or r.stdout)[:200])
        return r.returncode
    except Exception as e:  # noqa: BLE001
        logger.warning("调用 schtasks 失败：%s", e)
        return 1
