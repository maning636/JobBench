# -*- coding: utf-8 -*-
"""造真实的台账数据：覆盖各种状态与时间跨度，让提醒/复盘有东西可展示"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.application.service import AnalysisService
from jobforge.adapters.llm.config import LLMConfig
from jobforge.domain.application import Application, ApplyStatus, Followup, Resume
from jobforge.domain.candidate import Candidate
from jobforge.persistence.sqlite_store import Store
from jobforge.persistence.tracking_store import TrackingStore

DB = "data/jobforge.db"
st = Store(DB)
ts = TrackingStore(DB)
cand = Candidate.from_resume(
    "张明\n2024届本科毕业生\n技能：Java、Spring Boot、MySQL、Redis、RabbitMQ、Docker\n"
    "做过电商订单系统，用 RabbitMQ 做异步解耦，日均请求约2万。", name="张明")
cand.salary_expect = "18K"
cand.finalize()

# 简历版本
if not ts.list_resumes():
    ts.add_resume(Resume(name="校招版", tag="Java后端", content=cand.resume_text, is_default=True))
    ts.add_resume(Resume(name="数据方向", tag="数据分析",
                         content="张明\n技能：SQL、Python、Pandas、MySQL\n做过经营数据看板。"))

svc = AnalysisService(st, cand, llm=LLMConfig())
resume = ts.get_default_resume()


def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


jobs = st.list_jobs(limit=100)
# 覆盖：长期无回音 / 刚投 / 面试完没记录 / 拿到offer / 被拒 / 笔试中
plan = [
    (0, 13, "applied", 1, None),      # 投了13天没回音 → urgent
    (1, 4, "applied", 0, None),       # 投了4天 → soon
    (2, 1, "applied", 0, None),       # 刚投
    (3, 8, "interview", 1, None),     # 面试中但8天没记录 → urgent
    (4, 20, "offer", 0, "薪资待确认"),  # offer没答复
    (5, 15, "rejected", 0, "方向不符"),
    (6, 6, "assessment", 0, None),    # 笔试中
    (7, 25, "rejected", 2, "简历不够匹配"),
]

for job, dd, stt, fu, note in plan:
    if job >= len(jobs):
        continue
    rep = svc.matcher.analyze(jobs[job])
    app = Application.from_job(jobs[job], resume=resume,
                               match_score=rep.match_score, match_verdict=rep.verdict)
    app.applied_at = days_ago(dd)
    app.status = stt
    app.note = note or ""
    if stt in ("offer", "rejected"):
        app.result_at = days_ago(max(1, dd - 3))
    if stt == "offer":
        app.salary_offer = "18-22K"
    if stt == "rejected":
        app.reject_reason = note or ""
    aid = ts.add_application(app)
    for i in range(fu):
        ts.add_followup(Followup(
            application_id=aid, at=days_ago(dd - 5 - i * 2),
            channel="platform",
            content="您好，想问一下简历目前是否还在评估中，感谢。",
            reply="感谢投递，简历已收到，正在评估中。" if i == 0 else ""))

s = ts.tracking_summary()
print(f"台账：共 {s['total']} 条，进行中 {s['open']}，跟进 {s['followups']} 次，"
      f"offer {s['offer']}，回复率 {s['reply_rate']}%")
print(f"简历版本：{len(ts.list_resumes())} 个")
print()
print("提醒：")
for r in ts.reminders():
    print(f"  [{r['level']:<6}] {r['title']}")
    print(f"           {r['detail']} → {r['action']}")
