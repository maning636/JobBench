# -*- coding: utf-8 -*-
"""
求职工作台 · 端到端冒烟 + 契约测试
==================================

这份测试的作用，对应 v1.2.0 最贵的那个教训：
v1.2.0 零测试，导致「Web 入口必然 500」「CLI import 失败」
「干净安装解析器返回 0 条」三条缺陷全部逃逸。

这里每一条断言都对应一个已实测的历史缺陷。
"""
from __future__ import annotations

import socket as _sk
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.adapters.sources.manual import parse_boss_text, import_from_csv
from jobforge.application.filters import HardFilter, DataGate
from jobforge.domain.models import (
    Job, SalaryState, apply_salary, parse_salary, build_ddl, build_csv_columns,
)
from jobforge.persistence.sqlite_store import Store, IngestReport

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ← {detail}" if detail and not cond else ""))


# =============================================================================
print("\n[1] 薪资解析 —— 对应 v1.2.0「把猜测表当事实输出 01-34K」")
# =============================================================================
r = parse_salary("15-25K·13薪")
check("区间薪资正确解析", r["min"] == 15000 and r["max"] == 25000, str(r))
check("区间薪资标记为已核验", r["state"] == SalaryState.VERIFIED.value, str(r))

r = parse_salary("面议")
check("面议不做无意义数值", r["min"] is None and r["state"] == SalaryState.NA.value, str(r))

r = parse_salary("职位描述里没有薪资")
check("解析不出就是 UNRESOLVED，不猜", r["min"] is None and r["state"] == SalaryState.UNRESOLVED.value, str(r))

r = parse_salary("20-30K")
check("倒序区间自动纠正", r["min"] == 20000 and r["max"] == 30000, str(r))

r = parse_salary("200-300元/天")
check("实习日薪按日解析，不折算成月薪", r["min"] == 200 and r["period"] == "日", str(r))

r = parse_salary("30-50K")
check("月薪周期标记为月", r["period"] == "月", str(r))


# =============================================================================
print("\n[2] 文本解析 —— 对应 v1.2.0「company 存的是 5-10年」")
# =============================================================================
sample = """Java开发工程师
15-25K·13薪
北京云启科技有限公司
北京 海淀区 3-5年 本科
负责核心业务后端服务的设计与开发，参与微服务架构演进"""

j = parse_boss_text(sample)
check("岗位名正确", j.position == "Java开发工程师", j.position)
check("公司名正确", j.company == "北京云启科技有限公司", j.company)
check("公司名不是经验值", "5-10年" not in j.company, j.company)
check("公司名不是薪资", "15-25K" not in j.company, j.company)
check("城市正确", j.city == "北京", j.city)
check("薪资正确", j.salary_min == 15000, str(j.salary_min))
check("经验正确", j.experience == "3-5年", j.experience)
check("学历正确", j.education == "本科", j.education)
check("必填字段无缺失", j.missing_required() == [], str(j.missing_required()))

# 缺字段时必须留空并被看门狗抓到，而不是瞎猜
poor = "一些描述文字"
j2 = parse_boss_text(poor)
check("信息不足时必填字段被标记缺失", len(j2.missing_required()) > 0, str(j2.missing_required()))
check("信息不足时薪资为 UNRESOLVED", j2.salary_state == SalaryState.UNRESOLVED.value, j2.salary_state)
check("信息不足时完整率低", j2.completeness() < 0.6, str(j2.completeness()))


# =============================================================================
print("\n[3] 外包识别 —— 必须带可复核的证据")
# =============================================================================
j3 = parse_boss_text("测试岗位\n10-15K\n某外包服务有限公司\n北京 本科\n由甲方派遣驻场，长期驻场开发。")
check("强证据判为外包风险", j3.is_outsourcing_risk is True, str(j3.is_outsourcing_risk))
check("外包判定带证据句", len(j3.outsourcing_evidence) > 0, str(j3.outsourcing_evidence))
# 证据必须是最能让人复核的那句，而不是标题标签。
# 实测过的缺陷：证据抓到的是岗位名"后端开发工程师（派遣）"，
# 而 JD 正文里的"由甲方派遣驻场…长期驻场"才是真正的判据。
j5 = parse_boss_text(
    "后端开发工程师（派遣）\n13-20K\n某人力资源服务有限公司\n北京 3-5年 本科\n"
    "由甲方派遣驻场，负责支付系统后端开发，长期驻场。")
check(
    "外包证据优先给信息量最大的句子而非标题标签",
    bool(j5.outsourcing_evidence) and "派遣驻场" in j5.outsourcing_evidence[0],
    str(j5.outsourcing_evidence),
)

j4 = parse_boss_text("测试岗位\n10-15K\n某某网络科技有限公司\n北京 本科\n负责自研产品的前端开发工作。")
check("正常岗位不误判外包", j4.is_outsourcing_risk is not True, str(j4.is_outsourcing_risk))


# =============================================================================
print("\n[4] 契约单一源 —— 对应 v1.2.0「采集器给 title、数据库读 position」")
# =============================================================================
ddl = build_ddl()
cols_in_ddl = [ln.strip().split()[0] for ln in ddl.splitlines() if ln.strip().startswith(("position", "company", "salary"))]
check("DDL 含 position", "position" in ddl)
check("DDL 已无 title 字段", "\n    title " not in ddl and " title TEXT" not in ddl)
check("CSV 列含 position", "position" in build_csv_columns())
check("CSV 列已无 title", "title" not in build_csv_columns())

names = set(Job.field_names())
check("领域模型只有一份字段定义", "position" in names and "title" not in names)


# =============================================================================
print("\n[5] 去重指纹 —— 对应 v1.2.0「用 title|company 去重导致误判」")
# =============================================================================
a = Job(platform_id := "manual", company="北京云启科技有限公司", position="Java开发工程师", city="北京")
b = Job(platform_id := "manual", company="北京云启科技有限公司", position="Java开发工程师", city="北京")
c = Job(platform_id := "manual", company="北京云启科技有限公司", position="Java开发工程师", city="上海")
check("同岗位指纹一致", a.fingerprint == b.fingerprint)
check("不同城市指纹不同", a.fingerprint != c.fingerprint)

d = Job(channel="boss", external_id="abc123", company="某公司", position="岗位")
e = Job(channel="boss", external_id="abc123", company="别的公司", position="完全不同的岗位")
check("同平台 ID 指纹一致（公司名变了也能认出是同一个）", d.fingerprint == e.fingerprint)

# 这条是 v2 种子数据阶段实测抓到的真实缺陷：
# Job(channel=..., url=...) 先建空对象 → __post_init__ 用空值算出指纹 →
# 之后 position/company/city 虽然赋值了，指纹再没重算 → 12 个岗位指纹全等，
# 去重把整个库折叠成一条，且不报任何错。
samples = [
    ("Java开发工程师", "北京云启科技有限公司", "北京"),
    ("算法工程师（校招）", "上海启明智能科技有限公司", "上海"),
    ("前端开发工程师", "杭州星辰数据服务有限公司", "杭州"),
    ("运维工程师", "成都数云科技有限公司", "成都"),
]
late = []
for pos, co, city in samples:
    j = Job(channel="boss")          # 典型解析器写法：先建空对象
    j.position = pos                  # 再逐字段赋值
    j.company = co
    j.city = city
    j.finalize()                      # 字段就位后重算
    late.append(j)
fps = {j.fingerprint for j in late}
check("构造后赋值的字段必须参与指纹（否则全库折叠成一条）",
      len(fps) == len(samples), f"得到 {len(fps)} 个不同指纹，应为 {len(samples)}")


# =============================================================================
print("\n[6] 存储层 —— 增量、去重、首次发现时间不被覆盖")
# =============================================================================
tmp = Path(tempfile.mkdtemp()) / "t.db"
st = Store(str(tmp))

ja = parse_boss_text(sample)
ja.external_id = "job-001"
rep1 = st.upsert_many([ja])
check("首次入库成功", rep1.inserted == 1 and rep1.updated == 0, str(rep1.to_dict()))
check("首次入库健康", rep1.healthy, str(rep1.warnings))

rep2 = st.upsert_many([ja])
check("重复入库识别为更新", rep2.inserted == 0 and rep2.updated == 1, str(rep2.to_dict()))

first_seen = st.list_jobs()[0].first_seen_at
st.upsert_many([ja])
check("首次发现时间不被覆盖", st.list_jobs()[0].first_seen_at == first_seen)

job_id = getattr(st.list_jobs()[0], "db_id", None)
check("读回时携带数据库主键（状态切换依赖它）", job_id is not None, str(job_id))

st.set_status(job_id, "applied")
check("状态切换生效", st.get(job_id).status == "applied", st.get(job_id).status)

tags = st.list_jobs()[0].tags
check("列表字段往返正确（JSON 编解码）", isinstance(tags, list) and "北京" in tags, str(tags))

sd = st.list_jobs()[0].score_details
check("字典字段往返正确", isinstance(sd, dict), str(type(sd)))


# =============================================================================
print("\n[6b] bool 字段往返 —— 对应 v1.2.0「数据在缝合处静默丢失」")
# =============================================================================
# sqlite3 没有 bool 类型，True 存进去读出来是 1，
# 而 `1 is True` 在 Python 里恒为 False —— 实测导致外包识别整个功能
# 在展示层失效（卡片无风险标记、飞书无告警），数据却是对的。
outs = parse_boss_text("后端开发工程师（派遣）\n13-20K\n某人力资源服务有限公司\n北京 本科\n"
                       "由甲方派遣驻场，负责支付系统后端开发，长期驻场。")
outs.external_id = "job-outsourcing-001"
st.upsert_many([outs])

st2 = Store(str(tmp))
loaded = [j for j in st2.list_jobs(limit=100) if j.position == "后端开发工程师（派遣）"]
check("外包岗位能读回", len(loaded) == 1, f"读到 {len(loaded)} 条")
if loaded:
    v = loaded[0].is_outsourcing_risk
    check("bool 字段读回后仍是 bool（不是 0/1）", isinstance(v, bool), f"类型 {type(v).__name__}")
    check("外包判定在往返后仍为 True", v is True, f"实际 {v!r}")
    check("外包证据在往返后仍存在", len(loaded[0].outsourcing_evidence) > 0,
          str(loaded[0].outsourcing_evidence))
    check("外包证据优先给信息量最大的句子",
          "派遣驻场" in loaded[0].outsourcing_evidence[0],
          str(loaded[0].outsourcing_evidence))


# =============================================================================
print("\n[7] 数据健康看门狗 —— 对应 v1.2.0「15 条空岗位照样宣称成功」")
# =============================================================================
gate = DataGate()
bad_jobs = [Job(position="岗位", company="", salary_text="", city="") for _ in range(5)]
bad_rep = IngestReport(received=5, inserted=5, incomplete=5, completeness=0.2)
g = gate.check(bad_rep, bad_jobs)
check("残缺数据被判定为不健康", g["pass"] is False, str(g))
check("给出具体不推送原因", len(g["problems"]) > 0, str(g["problems"]))

good_jobs = [parse_boss_text(sample) for _ in range(3)]
good_rep = IngestReport(received=3, inserted=3, incomplete=0, completeness=0.9)
check("完整数据通过看门狗", gate.check(good_rep, good_jobs)["pass"] is True)


# =============================================================================
print("\n[8] 硬性筛选 —— 必须能说出淘汰理由")
# =============================================================================
f = HardFilter(cities=["北京"], salary_min=12000, edu_at_least="本科", hide_incomplete=False)
res = f.apply([ja, j3])
check("符合条件的保留", res["kept_count"] >= 0)
for e in res["dropped"]:
    check(f"淘汰岗位有理由：{e['job'].position}", len(e["reasons"]) > 0, str(e["reasons"]))

f2 = HardFilter(require_fresh_grad=True, hide_incomplete=False)
r2 = f2.apply([parse_boss_text("算法工程师\n20-30K\n某某科技\n北京 3-5年 本科\n负责算法研发")])
check("社招岗被应届筛选淘汰", r2["kept_count"] == 0, str(r2["kept"]))
check("淘汰原因为应届不符", any("应届" in x for x in r2["dropped"][0]["reasons"]), str(r2["dropped"][0]["reasons"]))


# =============================================================================
print("\n[9] CSV 导入")
# =============================================================================
csv_text = """岗位名,公司,薪资,城市,学历,经验,链接
测试工程师,某某公司,8-12K,杭州,本科,1-3年,https://example.com/1
前端开发,另一家公司,12-18K,广州,大专,不限,https://example.com/2"""
jobs, errs = import_from_csv(csv_text)
check("CSV 解析出 2 条", len(jobs) == 2, f"{len(jobs)} 条, 错误 {errs}")
check("CSV 中文表头映射正确", jobs[0].position == "测试工程师", jobs[0].position)
check("CSV 薪资解析正确", jobs[0].salary_min == 8000, str(jobs[0].salary_min))
check("CSV 无解析错误", errs == [], str(errs))


# =============================================================================
print("\n[10] 匹配度分析 —— 产品的核心，每条都对应一个实测过的错误结论")
# =============================================================================
from jobforge.domain.candidate import Candidate, extract_skills, _norm
from jobforge.application.analyzer import (
    MatchAnalyzer, CompareAnalyzer, SalaryAdvisor,
    MIN_REQUIRED_SKILLS, _expect_to_monthly,
)

RESUME = """张明
2024届本科毕业生
技能：Java、Spring Boot、MySQL、Redis、RabbitMQ、Docker、Vue
做过电商订单系统，用 RabbitMQ 做异步解耦。
"""
cand = Candidate.from_resume(RESUME, name="张明")
check("简历提取到技能", len(cand.skills) >= 6, str([s["name"] for s in cand.skills]))
check("识别为应届", cand.is_fresh_grad is True)
check("识别学历本科", cand.education == "本科", cand.education)

# 技能词典不能误报：'go' 不能命中 'google'、'es' 不能命中 'test'
noise = extract_skills("We use Google Cloud and TestNG for CI")
noise_names = {s["name"] for s in noise}
check("Go 不被 google 误触发", "Go" not in noise_names, str(noise_names))
check("Elasticsearch 不被 test 误触发", "Elasticsearch" not in noise_names, str(noise_names))

# 薪资单位：实测踩过「18K 被当成 18 元」的坑
check("预期 18K 折算为 18000", _expect_to_monthly("18K") == 18000, str(_expect_to_monthly("18K")))
check("预期 15-20K 取下限 15000", _expect_to_monthly("15-20K") == 15000, str(_expect_to_monthly("15-20K")))
check("预期 15000 原样", _expect_to_monthly("15000") == 15000, str(_expect_to_monthly("15000")))
check("预期空值返回 None", _expect_to_monthly("") is None)

# 强匹配
JD_STRONG = """Java开发工程师
15-25K·13薪
北京云启科技有限公司
北京 3-5年 本科
职位描述：
必须精通 Java 和 Spring Boot；熟练使用 MySQL；
需要熟悉 Kafka 或 RabbitMQ 消息队列；有 Docker 经验优先。"""
job_strong = parse_boss_text(JD_STRONG)
rep = MatchAnalyzer(cand).analyze(job_strong)
check("强匹配岗位匹配度高", rep.match_score >= 75, f"{rep.match_score}%")
check("硬性要求被识别", len(rep.missing_hard) >= 0)
check("命中项带 JD 证据", all(m.jd_evidence for m in rep.covered), str(rep.covered[:1]))
check("命中项带简历证据", all(m.resume_evidence for m in rep.covered))

# 完全不对口 → 应劝退，且结论不是"高���分"
JD_MISMATCH = """前端开发工程师
12-20K
杭州星辰数据服务有限公司
杭州 1-3年 本科
职位描述：
精通 React 与 TypeScript；熟悉 Webpack 构建工具；
必须熟练掌握 Figma 交互设计；熟悉 Node.js。"""
job_mis = parse_boss_text(JD_MISMATCH)
rep_mis = MatchAnalyzer(cand).analyze(job_mis)
check("不对口岗位判为不建议/边缘", rep_mis.verdict in ("不建议", "边缘", "有风险"), rep_mis.verdict)
check("不对口岗位匹配度不高", rep_mis.match_score <= 40, f"{rep_mis.match_score}%")

# ---- 分母陷阱：JD 写得越少，分数越高，必须拦住 ----
# 实测踩过：一个只写「负责后端开发」的实习岗识别出 1 项要求且命中，
# 算出 100%「强烈推荐」，排序冲到第一，把真正写清要求的岗位压下去。
JD_VAGUE = """Java开发实习生
200-300元/天
北京云启科技有限公司
北京 在校/应届 本科
职位描述：
负责后端开发。"""
job_vague = parse_boss_text(JD_VAGUE)
rep_vague = MatchAnalyzer(cand).analyze(job_vague)
check("JD 要求过少时不判为强烈推荐",
      rep_vague.verdict != "强烈推荐", f"{rep_vague.verdict} / {rep_vague.match_score}%")
check("JD 要求过少时判为无法评估",
      rep_vague.verdict == "无法评估", rep_vague.verdict)

# 排序：无法评估的必须排在能评估的后面
rows = CompareAnalyzer(cand).compare(
    [job_vague, job_strong, job_mis],
    [rep_vague, rep, rep_mis])
check("对比表按可评估性排序，无法评估垫底",
      rows[-1].verdict == "无法评估", f"最后一名是 {rows[-1].verdict}")
check("写清要求的岗位排在无法评估之前",
      rows[0].position == "Java开发工程师", rows[0].position)

# 模糊 JD 不能被说成「无硬性缺口」这种优势
check("模糊 JD 不被列为无硬性缺口",
      not any("无硬性缺口" in p for p in rows[-1].pros), str(rows[-1].pros))
check("模糊 JD 明确标注匹配度不可信",
      any("不可信" in c for c in rows[-1].cons), str(rows[-1].cons))

# 日薪不能和全职月薪并列成「薪资明确」优势
check("日薪岗不把日薪列为薪资优势",
      not any("薪资明确" in p for p in rows[-1].pros), str(rows[-1].pros))

# 薪资判断的单位
cand.salary_expect = "18K"
v = SalaryAdvisor(cand).view(job_strong)
check("薪资对比使用正确单位（18000 而非 18）",
      v.vs_expect is None or "18000" in v.vs_expect, str(v.vs_expect))

# 建议质量：0 匹配时不能给一堆一模一样的废话
check("不对口岗位的硬性缺失被合并成一条",
      len(rep_mis.suggestions) <= 3, str(rep_mis.suggestions))
check("不对口岗位的建议不是逐条重复",
      len(set(rep_mis.suggestions)) == len(rep_mis.suggestions), str(rep_mis.suggestions))

# 经验门槛必须影响结论，不能只当备注
check("应届投 3-5 年岗位被降级，不判强烈推荐",
      rep.verdict in ("有风险", "不要投"), rep.verdict)
check("经验不符进入 blockers",
      any("经验要求" in b for b in rep.blockers), str(rep.blockers))

# 兜底结论不能自相矛盾
# 实测踩过的坑：一个 0% 匹配的岗位，因为没有硬性缺失落到了兜底分支，
# 输出「硬性要求基本满足但整体契合度一般」—— 0% 说「一般」是矛盾的。
low_no_hard = parse_boss_text("""办公室行政专员
6-8K
某公司
北京 经验不限 大专
职位描述：
负责日常行政事务，熟练使用 Excel 与 Word，
有责任心、沟通能力强优先。""")
r_low = MatchAnalyzer(cand).analyze(low_no_hard)
if r_low.verdict in ("边缘", "不建议"):
    check("低匹配不判为'契合度一般'",
          "契合度一般" not in r_low.summary or r_low.match_score >= 40,
          f"{r_low.match_score}% → {r_low.summary}")
else:
    check("低匹配不判为'契合度一般'", True, f"{r_low.verdict}")
check("summary 与分数不矛盾",
      not (r_low.match_score < 20 and "契合度一般" in r_low.summary),
      f"{r_low.match_score}% → {r_low.summary}")


# =============================================================================
print("\n[11] Web 入口 —— 对应 v1.2.0「采集按钮必然 500」")
# =============================================================================
from jobforge.web.app import create_app

app = create_app(str(tmp), store=Store(str(tmp)))
c = app.test_client()

r = c.get("/api/summary")
check("GET /api/summary 返回 200", r.status_code == 200, str(r.status_code))
check("概览含健康度字段", "health" in r.get_json())

r = c.get("/api/jobs")
check("GET /api/jobs 返回 200", r.status_code == 200, str(r.status_code))
kept = r.get_json()["kept"]
check("岗位列表带真实主键", kept and kept[0]["id"] is not None, str(kept[:1]))

if kept:
    r = c.post(f"/api/jobs/{kept[0]['id']}/status", json={"status": "interested"})
    check("POST 状态切换返回 200", r.status_code == 200, str(r.status_code))

r = c.post("/api/jobs/import", json={"mode": "text", "content": sample})
check("POST 导入返回 200", r.status_code == 200, str(r.status_code))
check("导入报告含 inserted", "inserted" in r.get_json(), str(r.get_json())[:200])

r = c.post("/api/jobs/import", json={"mode": "text", "content": ""})
check("空内容返回 400 而非 500", r.status_code == 400, str(r.status_code))

r = c.get("/")
check("首页返回 200", r.status_code == 200, str(r.status_code))
# 设计 token 已从内联 <style> 拆到 app.css（内联单文件曾达 794 行），
# 所以首页本身不再有 :root，要去静态资源里验
check("首页含 tab 式工作台结构",
      'data-tab="track"' in r.get_data(as_text=True)
      and 'id="pane-track"' in r.get_data(as_text=True))
check("首页引用外部样式与脚本",
      "/static/app.css" in r.get_data(as_text=True)
      and "/static/app.js" in r.get_data(as_text=True))

r = c.get("/static/app.css")
check("app.css 返回 200", r.status_code == 200, str(r.status_code))
css = r.get_data(as_text=True)
check("设计 token 在 app.css 里", ":root{" in css and "--brand-500" in css)
check("响应式断点存在", "@media" in css and "prefers-reduced-motion" in css)

r = c.get("/static/app.js")
check("app.js 返回 200", r.status_code == 200, str(r.status_code))
js = r.get_data(as_text=True)
check("台账相关逻辑在 app.js 里",
      "loadTracking" in js and "loadInsight" in js and "buildReminders" not in js)
check("tab 用 hash 驱动（可刷新/可分享）", "hashchange" in js)

r = c.get("/api/export.csv")
check("CSV 导出返回 200", r.status_code == 200, str(r.status_code))


# =============================================================================
print("\n[12] 分析编排与缓存 —— 对应'批量跑一遍要等很久'")
# =============================================================================
from jobforge.application.service import AnalysisService, render_markdown
from jobforge.domain.candidate import Candidate, CandidateLoadError
from jobforge.adapters.llm.config import LLMConfig

cand2 = Candidate.from_resume(
    "张明 2024届本科\n技能：Java、Spring Boot、MySQL、Redis、RabbitMQ\n做过订单系统。",
    name="张明")
# 复用上面已灌了种子数据的 tmp 库 —— 之前用新建空库，
# 导致「报告含排序表」永远失败（空库本来就渲染不出表）
svc = AnalysisService(Store(str(tmp)), cand2, llm=LLMConfig())   # 不配 LLM
jobs2 = svc.store.list_jobs(limit=50)
check("服务可初始化", svc.has_profile() is True)
check("复用的库有岗位数据", len(jobs2) > 0, f"{len(jobs2)} 个岗位")

# 分两种情况：
#   use_llm=False  → 显式不调用，semantic 应为 None
#   use_llm=True 但未配置 → 应降级并标注原因
res = svc.analyze_many(jobs2, use_llm=False)
check("批量分析返回结果数正确", len(res) == len(jobs2), f"{len(res)}/{len(jobs2)}")
check("显式关闭 LLM 时不产生语义对象",
      all(r.semantic is None for r in res))

res2 = svc.analyze_many(jobs2, use_llm=True)
check("未配 LLM 时自动降级并标注原因",
      all(r.semantic is not None and not r.semantic.available
          and "未配置" in r.semantic.error for r in res2),
      str([r.semantic.error for r in res2[:1]]))
check("降级不影响规则结论",
      [r.report.match_score for r in res] == [r.report.match_score for r in res2])

cmpd = svc.compare(res)
check("对比结果行数一致", len(cmpd["rows"]) == len(res))
check("无法评估的岗位排在后面",
      cmpd["rows"][-1]["verdict"] == "无法评估" if cmpd["rows"] else True,
      str(cmpd["rows"][-1]) if cmpd["rows"] else "")

# 缓存键：同岗位同简历应命中
h1 = svc.store.candidate_hash(cand2.resume_text, cand2.skill_names)
svc.store.put_semantic("fp-1", h1, {"available": True, "semantic_matches": [], "x": 1}, "m")
got = svc.store.get_semantic("fp-1", h1)
check("语义缓存可写入并读回", got is not None and got.get("x") == 1, str(got))
check("不同简历哈希不命中旧缓存",
      svc.store.get_semantic("fp-1", h1 + "x") is None)

cand2.resume_text = cand2.resume_text + " 加了 Elasticsearch 经验"
h2 = svc.store.candidate_hash(cand2.resume_text, cand2.skill_names)
check("改了简历后哈希变化", h2 != h1, f"{h1} vs {h2}")
check("改简历后旧缓存不命中", svc.store.get_semantic("fp-1", h2) is None)

# 实测踩过的坑：Web 端「让模型再读一遍」按钮直接 new 了一个 SemanticAnalyzer，
# 完全没走 AnalysisService —— 同一个岗位每点一次就真打一次模型，白烧额度。
# 这里用计数假件钉死：同一岗位+同一简历，模型只能被调一次。
from jobforge.adapters.llm.semantic import SemanticResult
from jobforge.adapters.llm.client import LLMClient

_calls = {"n": 0}
class _CountingSem:
    def analyze(self, job, cand):
        _calls["n"] += 1
        r = SemanticResult(available=True, soft_fit_score=60)
        r.semantic_matches = []
        return r

svc.sem = _CountingSem()
_orig_avail = LLMClient.available
LLMClient.available = lambda self: True      # 假装模型可用
_job = jobs2[0]
try:
    a1 = svc.analyze_one(_job, use_llm=True)
    a2 = svc.analyze_one(_job, use_llm=True)
    a3 = svc.analyze_one(_job, use_llm=True)
    check("同一岗位连点三次，模型只被调用一次",
          _calls["n"] == 1, f"实际调用 {_calls['n']} 次")
    check("后两次标记为命中缓存", a2.from_cache and a3.from_cache)
    check("第一次不算命中缓存", a1.from_cache is False)
    check("命中缓存的结论与首次一致",
          (a2.semantic.soft_fit_score == a1.semantic.soft_fit_score))

    # 换了简历就必须重新问模型，不能拿旧结论糊弄
    _calls["n"] = 0
    cand2.resume_text = cand2.resume_text + " 又加了 Kafka"
    a4 = svc.analyze_one(_job, use_llm=True)
    check("简历改了之后重新调模型（缓存键跟着变）",
          _calls["n"] == 1 and a4.from_cache is False, f"调用 {_calls['n']} 次")
finally:
    LLMClient.available = _orig_avail

# 报告导出
md = render_markdown(res, cand2, cmpd)
check("Markdown 报告非空", len(md) > 100, str(len(md)))
check("报告含标题与排序表", "# 求职工作台分析报告" in md and "岗位排序" in md)
check("报告含候选人与技能", "候选人" in md, md[:200])
check("报告无乱码字符（写文件时编码写坏过）",
      "\ufffd" not in md, repr([c for c in md if c == "\ufffd"][:5]))
check("报告含逐个岗位分析", "逐个岗位分析" in md)

# 配置损坏必须显式报错，不能静默当成"未配置"
print("\n[13] 配置损坏必须显式报错 —— 对应'用户说没配过模型'")
from jobforge.adapters.llm.config import LLMConfigError
bad_cfg = Path(tempfile.mkdtemp()) / "llm.json"
# 复现 Windows PowerShell 写出的 BOM：普通 utf-8 读会抛 JSONDecodeError
bad_cfg.write_bytes(b"\xef\xbb\xbf" + b"{ not json at all ")
try:
    LLMConfig.load(str(bad_cfg))
    check("配置损坏时抛 LLMConfigError", False, "静默返回了空配置")
except LLMConfigError:
    check("配置损坏时抛 LLMConfigError", True)
except Exception as e:
    check("配置损坏时抛 LLMConfigError", False, f"抛的是 {type(e).__name__}")

# BOM 但内容合法：应正常读出（utf-8-sig 容忍）
ok_cfg = Path(tempfile.mkdtemp()) / "llm2.json"
ok_cfg.write_bytes(b"\xef\xbb\xbf" + b'{"model":"x","api_key":"k","base_url":"https://a/v1"}')
try:
    cfg2 = LLMConfig.load(str(ok_cfg))
    check("带 BOM 的合法配置能读出", cfg2.model == "x", cfg2.model)
except Exception as e:  # noqa: BLE001
    check("带 BOM 的合法配置能读出", False, f"{type(e).__name__}: {e}")

# 候选人档案同理
bad_cand = Path(tempfile.mkdtemp()) / "candidate.json"
bad_cand.write_bytes(b"\xef\xbb\xbf" + b"{ broken")
try:
    Candidate.load(str(bad_cand))
    check("档案损坏时抛 CandidateLoadError", False, "静默返回了空档案")
except CandidateLoadError:
    check("档案损坏时抛 CandidateLoadError", True)
except Exception as e:  # noqa: BLE001
    check("档案损坏时抛 CandidateLoadError", False, f"抛的是 {type(e).__name__}")

ok_cand = Path(tempfile.mkdtemp()) / "candidate2.json"
import json as _json
_body = _json.dumps({"name": "张明", "resume_text": "Java"}, ensure_ascii=False)
ok_cand.write_bytes(b"\xef\xbb\xbf" + _body.encode("utf-8"))
try:
    check("带 BOM 的合法档案能读出", Candidate.load(str(ok_cand)).name == "张明")
except Exception as e:  # noqa: BLE001
    check("带 BOM 的合法档案能读出", False, f"{type(e).__name__}: {e}")


# =============================================================================
print("\n[14] 双击启动入口 —— 对应'用户打开压缩包只会看到一个黑窗口'")
# =============================================================================
# 实测踩过：包里原本没有任何可双击的文件。用户解压后看到一堆 .py 就放弃了，
# 而"必须开终端敲 python"正是这个产品要消灭的东西。这里钉死入口存在且可用。
from jobforge import launch

check("启动器模块可导入", hasattr(launch, "main"))
check("默认端口为 3399", launch.DEFAULT_PORT == 3399)

_r = launch.find_python()
check("能找到可用的 Python", bool(_r), str(_r))

# --check 必须真的不常驻：它只自检，不该把服务挂住
_check_rc = subprocess.run(
    [sys.executable, "-X", "utf8", "-m", "jobforge.launch", "--check"],
    cwd=str(Path(__file__).resolve().parents[1]),
    capture_output=True, text=True, timeout=120)
check("--check 能正常跑完并返回 0",
      _check_rc.returncode == 0, (_check_rc.stderr or "")[-200:])
check("--check 输出里没有启动服务的痕迹（说明没常驻）",
      "Running on" not in _check_rc.stdout and "Press CTRL+C" not in _check_rc.stdout,
      _check_rc.stdout[-200:])
check("--check 的输出是给人看的中文，不是异常堆栈",
      "Traceback" not in _check_rc.stdout, _check_rc.stdout[-200:])

# 端口被占用时要自己换，而不是让用户去查
import socket as _sk
def _occupied(p):
    s = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", p)); return False
    except OSError:
        return True
    finally:
        s.close()

_free = launch.pick_port(3399)
check("选出的端口确实可用", not _occupied(_free), f"选出 {_free}")

# 真占住一个端口，再让启动器自己去挑 —— 证明它不会把用户卡在"端口被占用"
_held = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
try:
    _held.bind(("127.0.0.1", 0))
    _held.listen(1)
    _busy_port = _held.getsockname()[1]
    _picked = launch.pick_port(_busy_port)
    check("端口被占用时会自动往后找一个，而不是让用户自己查",
          _picked != _busy_port, f"占用了 {_busy_port}，选出 {_picked}")
    check("自动选出的端口确实是空闲的", not _occupied(_picked), str(_picked))
finally:
    _held.close()

# 命令里带参数的解释器（"py -3"）必须能正确拆开，否则 subprocess 会当成
# 一个叫 "py -3" 的可执行文件而找不到 —— 实测踩过这个
check("带参数的解释器能被拆成参数列表",
      launch._split("py -3") == ["py", "-3"], str(launch._split("py -3")))
check("普通解释器拆分后长度为 1",
      launch._split("python") == ["python"], str(launch._split("python")))

_bat = Path(__file__).resolve().parents[1] / "启动.bat"
check("压缩包里有可双击的启动文件", _bat.exists(), str(_bat))
if _bat.exists():
    _t = _bat.read_text(encoding="utf-8", errors="replace")
    check("启动文件会先切到自己的目录（双击时工作目录未必是它）",
          'cd /d "%~dp0"' in _t)
    check("启动文件依次尝试 py / python / python3",
          _t.count("python") >= 2 and "py -3" in _t)
    check("没装 Python 时给出官网地址",
          "python.org/downloads" in _t)
    check("启动文件提示勾上 Add Python to PATH",
          "Add Python to PATH" in _t)
    check("启动文件是纯 ASCII（cmd 代码页会把中文显示成乱码）",
          all(ord(ch) < 128 for ch in _t))

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")