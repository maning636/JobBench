# -*- coding: utf-8 -*-
"""台账层验收：状态机/提醒/快照/简历版本"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.domain.application import (
    Application, ApplyStatus, Followup, Resume, build_reminders,
    can_transition, today, export_applications_csv,
)
from jobforge.persistence.tracking_store import TrackingStore
from jobforge.adapters.sources.manual import parse_boss_text

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))

tmp = Path(tempfile.mkdtemp()) / "t.db"
ts = TrackingStore(str(tmp))
EXPORT_DIR = Path(tempfile.mkdtemp())

job = parse_boss_text("""Java开发工程师
15-25K·13薪
北京云启科技有限公司
北京 海淀区 3-5年 本科
职位描述：必须精通 Java 和 Spring Boot，熟练使用 MySQL 与 Redis。""")

print("\n[1] 投递记录：岗位快照必须留存")
resume = Resume(name="校招版", tag="Java后端", content="张明\n技能：Java、Spring Boot",
                is_default=True)
rid = ts.add_resume(resume)
r = ts.get_resume(rid)
check("简历可保存可读回", r is not None and r.name == "校招版", str(r))
check("简历 hash 可算", len(r.hash) == 12)

app = Application.from_job(job, resume=r, match_score=87.0, match_verdict="可以投")
aid = ts.add_application(app)
check("投递记录已创建", aid > 0)
got = ts.get_application(aid)
check("岗位快照已冗余保存", got.position == "Java开发工程师" and got.company == "北京云启科技有限公司")
check("匹配度快照已保存", got.match_score == 87.0, str(got.match_score))
check("引用的简历版本已记录", got.resume_name == "校招版" and got.resume_hash == r.hash)
check("初始状态为已投递", got.status == ApplyStatus.APPLIED.value)

# 岗位信息变了，投递记录不能跟着变
job.salary_text = "30-40K"
job.position = "改名了"
again = ts.get_application(aid)
check("岗位变动不影响历史投递记录",
      again.position == "Java开发工程师" and again.salary_text == "15-25K·13薪",
      f"{again.position} / {again.salary_text}")

print("\n[2] 状态机：非法流转必须拦住并说明")
check("已投递→面试中 允许", can_transition("applied", "interview"))
check("已投递→已投递 不允许", not can_transition("applied", "applied"))
r1 = ts.set_status(aid, "applied")
check("非法流转被拒绝", r1["ok"] is False, str(r1))
check("拒绝时说明了原因", "不能从" in r1.get("error", ""), str(r1.get("error")))
check("拒绝时给出可选状态", len(r1.get("allowed", [])) > 0, str(r1.get("allowed")))

r2 = ts.set_status(aid, "interview")
check("合法流转成功", r2["ok"] is True, str(r2))
check("状态已更新", ts.get_application(aid).status == "interview")

r3 = ts.set_status(aid, "offer")
check("面试中→offer 允许", r3["ok"] is True, str(r3))
check("终态记录了结果日期", bool(ts.get_application(aid).result_at))

r4 = ts.set_status(aid, "applied")
check("offer→已投递 默认被拦", r4["ok"] is False, str(r4))
r5 = ts.set_status(aid, "applied", force=True)
check("force=True 可以强改", r5["ok"] is True, str(r5))

print("\n[3] 跟进记录与计数")
ts.set_status(aid, "applied", force=True)
f1 = ts.add_followup(Followup(application_id=aid, channel="platform",
                              content="您好，想问一下简历是否还在评估中"))
a2 = ts.get_application(aid)
check("跟进后计数+1", a2.followup_count == 1, str(a2.followup_count))
check("跟进后记录了最近时间", bool(a2.last_followup_at), str(a2.last_followup_at))
check("跟进可查回", len(ts.list_followups(aid)) == 1)

ts.add_followup(Followup(application_id=aid, channel="email", content="二次跟进"))
check("多次跟进计数正确", ts.get_application(aid).followup_count == 2)

print("\n[4] 跟进提醒：纯规则，不靠 LLM")
old = Application(position="很久没回的岗", company="某公司",
                  applied_at="2026-10-01", status="applied")
mid = Application(position="刚投的岗", company="某司",
                  applied_at=today(), status="applied")
interview = Application(position="面试完没记录的岗", company="某司",
                        applied_at="2026-09-20", status="interview",
                        last_followup_at="2026-09-20")
offer = Application(position="offer没答复", company="某司",
                   applied_at="2026-10-01", status="offer")
closed = Application(position="已拒绝的", company="某司",
                     applied_at="2026-09-01", status="rejected")

for i, a in enumerate([old, mid, interview, offer, closed], 1):
    a.id = i
rem = build_reminders([old, mid, interview, offer, closed])
titles = " | ".join(r.title for r in rem)
check("投递很久没回音 → urgent",
      any(r.level == "urgent" and "没回音" in r.title for r in rem), titles)
check("刚投的岗不提醒", not any("刚投的" in r.title for r in rem), titles)
check("面试没记录下一步 → 提醒", any("面试中" in r.title for r in rem), titles)
check("offer 未答复 → 提醒", any("offer" in r.title for r in rem), titles)
check("已关闭的岗位不提醒", not any("已拒绝" in r.title for r in rem), titles)
check("urgent 排在最前", rem[0].level == "urgent", rem[0].title)
check("每条提醒都有建议动作", all(bool(r.action) for r in rem))

print("\n[5] 统计与复盘")
# 类型推断陷阱：from __future__ import annotations 让注解变字符串，
# 用 `f.type in ("int",)` 这种字面量比较会全部失配 → 列建成 TEXT →
# 布尔值存进去变字符串 → bool('0') 也是 True → 默认简历永远是"全部默认"。
import sqlite3 as _sq
_conn = _sq.connect(ts.path)
_cols = {r[1]: r[2] for r in _conn.execute("PRAGMA table_info(resumes)")}
check("bool 列建成 INTEGER 而不是 TEXT",
      _cols.get("is_default") == "INTEGER", str(_cols.get("is_default")))
check("int 列建成 INTEGER", _cols.get("followup_count", "INTEGER") in ("INTEGER",), str(_cols))
_conn.close()

from jobforge.domain.models import sql_type_of
check("类型映射对字符串注解生效（bool）", sql_type_of("bool") == "INTEGER")
check("类型映射对 Optional 生效", sql_type_of("Optional[int]") == "INTEGER")
check("类型映射对 float 生效", sql_type_of("Optional[float]") == "REAL")

a_apply = Application(position="A", company="C1", applied_at="2026-09-01",
                      status="rejected")
a_off = Application(position="B", company="C2", applied_at="2026-09-05",
                    status="offer", match_score=90.0)
a_off2 = Application(position="B2", company="C3", applied_at="2026-09-06",
                     status="offer", match_score=30.0)
ts.add_application(a_apply)
ts.add_application(a_off)
ts.add_application(a_off2)
s = ts.tracking_summary()
check("统计总数正确", s["total"] >= 4, str(s["total"]))
check("统计区分状态", s["by_status"].get("offer") == 2, str(s["by_status"]))
check("回复率可算", s["reply_rate"] is not None, str(s["reply_rate"]))
check("回复率=2/3", s["reply_rate"] == 67, str(s["reply_rate"]))

print("\n[6] 简历多版本")
ts.add_resume(Resume(name="社招版", tag="Java后端", content="张明\n5年经验 Java 后端"))
rs = ts.list_resumes()
check("可存多份简历", len(rs) >= 2, str(len(rs)))
check("默认简历排在最前", rs[0]["name"] == "校招版", str(rs[0]["name"]))
ts.update_resume(rs[1]["id"], is_default=True)
rs2 = ts.list_resumes()
check("设默认后只有一份默认",
      sum(1 for r in rs2 if r["is_default"]) == 1, str([r["name"] for r in rs2]))
check("默认切换生效", rs2[0]["name"] == "社招版", rs2[0]["name"])

print("\n[7] 删除与导出")
fid = ts.add_followup(Followup(application_id=aid, content="待删的跟进"))
check("删除会连带删跟进", ts.delete_application(aid) is True)
check("跟进已级联删除", ts.list_followups(aid) == [])
p = export_applications_csv(str(EXPORT_DIR / "apps.csv"), ts.list_applications())
check("台账可导出 CSV", Path(p).exists() and "position" in Path(p).read_text(encoding="utf-8-sig"))

# 导出路径的父级是文件时，要给清晰错误而不是崩
try:
    export_applications_csv(str(tmp / "x.csv"), ts.list_applications())
    check("父路径非法时给清晰错误", False, "竟然成功了")
except ValueError as e:
    check("父路径非法时给清晰错误", "不是目录" in str(e) or "不可用" in str(e), str(e))
except Exception as e:  # noqa: BLE001
    check("父路径非法时给清晰错误", False, f"抛的是 {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    print("  失败项：")
    for x in F:
        print("   -", x)
print("=" * 60)
sys.exit(1 if F else 0)
