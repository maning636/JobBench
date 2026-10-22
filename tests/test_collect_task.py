# -*- coding: utf-8 -*-
"""
每日采集任务测试 —— 断点续采、失败可见、游标不误推进
=====================================================

v1.2.0 最贵的教训：抓不到数据被静默吞掉，用户以为是「今天没新岗位」。
这里逐条钉死相反的行为。
"""
from __future__ import annotations

import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.application.collect_job import (
    CollectPlan, CollectOutcome, RunStore, run_collect,
    _resolve_plan, plan_install_web,
)
from jobforge.application.filters import DataGate
from jobforge.persistence.sqlite_store import Store
from jobforge.adapters.sources.manual import parse_boss_text
from jobforge.domain.models import SalaryState

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(f"  {'PASS' if c else 'FAIL'}  {n}" + (f"  ← {d}" if d and not c else ""))

DB = str(Path(tempfile.mkdtemp()) / "t.db")
st, runs = Store(DB), RunStore(DB)

SAMPLE = """Java开发工程师
15-25K
北京云启科技有限公司
北京 3-5年 本科
必须精通 Java 和 Spring Boot"""


def make_jobs(n=3):
    out = []
    for i in range(n):
        j = parse_boss_text(SAMPLE.replace("Java开发工程师", f"Java开发工程师{i}"))
        j.external_id = f"ext-{i}"
        out.append(j)
    return out


def ok_fetcher(pages_done=3):
    def f(plan):
        return make_jobs(), [], pages_done
    return f


print("\n[1] 正常采集")
# =============================================================================
plan = CollectPlan(keyword="Java", city="", pages=3)
o = run_collect(plan, st, runs, fetcher=ok_fetcher(3))
check("任务成功", o.ok is True, str(o.errors))
check("入库 3 个", o.inserted == 3, str(o.inserted))
check("页数记录正确", o.pages_done == 3, str(o.pages_done))
check("结论说清了拿了多少", "新增 3" in o.headline(), o.headline())


print("\n[2] 断点续采 —— 中断后接着跑，不从头来")
# =============================================================================
plan2 = CollectPlan(keyword="Python", city="", pages=6)

# 第一次只跑了 2 页就中断
def partial(plan):
    return make_jobs(2), ["第 3 页超时"], 2

o1 = run_collect(plan2, st, runs, fetcher=partial)
check("部分失败被记为警告而非致命错误",
      len(o1.errors) == 0 and any("超时" in w for w in o1.warnings),
      f"errors={o1.errors} warnings={o1.warnings}")
check("拿到数据且达标时任务成功", o1.ok is True, str(o1.errors))

# 第二次续采：应该从第 3 页开始，只翻剩下的 4 页
captured = {}
def resumed(plan):
    captured["pages"] = plan.pages
    return make_jobs(2), [], 2

o2 = run_collect(plan2, st, runs, fetcher=resumed)
check("续采跳过了已完成的页", o2.skipped == 2, f"skipped={o2.skipped}")
check("续采只请求剩余页数", captured.get("pages") == 4, str(captured.get("pages")))

o3 = run_collect(plan2, st, runs, fetcher=ok_fetcher(1), resume=False)
check("resume=False 时从头开始", o3.skipped == 0, str(o3.skipped))


print("\n[3] 失败必须可见 —— 抓不到 ≠ 今天没岗位")
# =============================================================================
def zero_fetch(plan):
    return [], [], plan.pages

o4 = run_collect(CollectPlan(keyword="Go", pages=1), st, runs, fetcher=zero_fetch)
check("采到 0 条判为失败", o4.ok is False, str(o4))
check("给出可能原因", bool(o4.errors), str(o4.errors))
check("结论明确说'可能选择器失效'", "选择器" in o4.headline() or "0 条" in o4.headline(),
      o4.headline())

o5 = run_collect(CollectPlan(keyword="Rust", pages=1), st, runs, fetcher=None)
check("未配置抓取源时明确失败", o5.ok is False, str(o5))
check("未配置时给可执行建议",
      any("导入" in e or "手动" in e for e in o5.errors), str(o5.errors))

def boom(plan):
    raise RuntimeError("浏览器崩了")

o6 = run_collect(CollectPlan(keyword="Rust", pages=1), st, runs, fetcher=boom)
check("抓取抛异常时任务判失败", o6.ok is False)
check("异常信息被记下来", any("浏览器崩了" in e for e in o6.errors), str(o6.errors))


print("\n[4] 数据不达标不能推进游标")
# =============================================================================
# 残缺数据（薪资城市全空）不应被标记成「已处理」，
# 否则坏数据会被永久跳过、再也不会重抓。
def bad_fetch(plan):
    jobs = []
    for i in range(3):
        j = parse_boss_text("岗位" + str(i))   # 只有岗位名，其它全空
        j.external_id = f"bad-{i}"
        jobs.append(j)
    return jobs, [], 1

plan_bad = CollectPlan(keyword="PHP", pages=2)
ob = run_collect(plan_bad, st, runs, fetcher=bad_fetch)
check("残缺数据判为失败", ob.ok is False, str(ob.errors))
check("给出未过看门狗的原因",
      any("看门狗" in e or "完整率" in e for e in ob.errors), str(ob.errors))

cur_bad = runs.cursor("boss", "PHP", "")
check("失败时不推进游标", cur_bad["last_page_done"] == 0, str(cur_bad))

# 同一批重跑，仍然会被当成新任务（没被跳过）
again = run_collect(plan_bad, st, runs, fetcher=bad_fetch)
check("失败批次不会被跳过，下次仍会重跑", again.skipped == 0, str(again.skipped))
check("失败次数被累计", runs.cursor("boss", "PHP", "")["fail_count"] >= 2,
      str(runs.cursor("boss", "PHP", "")))


print("\n[5] 运行历史与健康度")
# =============================================================================
hist = runs.recent(20)
check("运行记录已持久化", len(hist) >= 6, str(len(hist)))
check("记录含错误详情", any(len(h["errors"]) > 0 for h in hist))

h = runs.health()
check("健康度可判定", h["status"] in ("ok", "fail", "never"), str(h))
check("健康度给出可读文本", bool(h["text"]), str(h))

o7 = run_collect(CollectPlan(keyword="Java", pages=1), st, runs, fetcher=ok_fetcher(1))
h2 = runs.health()
check("成功后健康度转为 ok", h2["status"] == "ok", str(h2))
check("健康度带累计新增数", "新增" in h2["text"], str(h2))


print("\n[6] 结果对象的表达准确性")
# =============================================================================
ok_o = CollectOutcome(ok=True, source="boss", inserted=5, updated=2, elapsed=30.0)
check("成功时结论有数量", "5" in ok_o.headline() and "2" in ok_o.headline(), ok_o.headline())
zero_o = CollectOutcome(ok=True, source="boss", inserted=0, updated=0, elapsed=10.0)
check("0 条时结论带警告而非报成功",
      "⚠" in zero_o.headline() or "0 条" in zero_o.headline(), zero_o.headline())
fail_o = CollectOutcome(ok=False, source="boss", errors=["选择器失效"])
check("失败时结论说清原因", "选择器失效" in fail_o.headline(), fail_o.headline())

print("\n[7] 每日自动采集的安装入口（网页端按钮背后那段）")
# =============================================================================
# 这段以前只有 CLI 一条路，网页上写「去终端敲 install-task」。
# 现在网页端也能点，所以校验逻辑必须离线可测 —— 不能靠真调 schtasks 来测。
#
# 用自己造的临时库（绝对路径），不碰 data/jobforge.db：
# 「库文件是否存在」是 _resolve_plan 的前置条件，依赖外部状态会导致
# 在没有 data/ 的环境里整段失败 —— 那是环境问题，不是逻辑问题。
good = _resolve_plan(DB, "Java", "", "09:30")
check("合法时间被接受", good["ok"] is True, str(good)[:120])
check("合法时间被归一成两位小时", good.get("hh") == 9 and good.get("mm") == 30, str(good)[:120])

for bad in ["25:00", "9:30", "0930", "abc", "09:61", "09-30", "9:5"]:
    r = _resolve_plan(DB, "Java", "", bad)
    check(f"非法时间 {bad!r} 被挡下", r["ok"] is False, str(r)[:120])

# 空值不是「非法」，是「没填」—— 回落到默认 09:30，别让人白填一次就报错
for empty in ["", None, "   "]:
    r = _resolve_plan(DB, "Java", "", empty)
    check(f"时间留空 {empty!r} 回落到 09:30",
          r["ok"] is True and (r["hh"], r["mm"]) == (9, 30), str(r)[:120])

r = _resolve_plan(DB, "Java", "", "99:99")
check("非法时间的报错是人话不是异常",
      r["ok"] is False and "时间格式" in r.get("error", ""), str(r)[:120])

r = _resolve_plan(DB, "", "", "09:30")
check("关键词留空时兜底成 Java，不装一个空关键词的任务",
      r["ok"] is True and r["keyword"] == "Java", str(r)[:120])

# 实测踩过的坑：相对 db 路径按项目父目录拼，装出来的每日任务指向
# 一个不存在的路径，用户要到第二天才发现每天都在失败。
# 规则：相对路径一律锚到项目根，不看当前工作目录。
# （旧的启发式是「cwd 里有就用 cwd 的」，同一条命令在两个目录
#   会解析出两个不同的库 —— 用户会以为岗位丢了。已改成单一规则。）
_rel = "data/jobforge.db"
r = _resolve_plan(_rel, "Java", "", "09:30")
_proj_root = Path(__file__).resolve().parents[1]
_expect_abs = str((_proj_root / _rel).resolve())
if Path(_expect_abs).exists():
    check("相对 db 路径锚到项目根，不是当前工作目录",
          r["ok"] is True and Path(r["db_abs"]).resolve() == Path(_expect_abs),
          f"得到 {r.get('db_abs')}，期望 {_expect_abs}")
else:
    check("相对 db 路径锚到项目根（本机无该库，跳过存在性比对）",
          r["ok"] is False and "找不到数据库文件" in r.get("error", ""),
          str(r)[:160])
    check("解析结果指向项目根下的 data/，不是当前工作目录",
          r.get("db_abs") == _expect_abs,
          f"得到 {r.get('db_abs')}，期望 {_expect_abs}")

r = _resolve_plan("data/definitely-not-here-xyz.db", "Java", "", "09:30")
check("库文件不存在时拒绝安装，不建一个每天必然失败的任务",
      r["ok"] is False and "找不到数据库文件" in r.get("error", ""), str(r)[:160])

r = _resolve_plan(str(Path(DB)), "Java", "", "09:30")
check("绝对路径指向真实库时放行", r["ok"] is True, str(r)[:160])

# 没装 Chrome 时必须明确失败并给出人话 —— 不能静默建一个每天必然失败的任务
import jobforge.application.collect_job as _cj
_orig = _cj._find_chrome
_cj._find_chrome = lambda: ""
try:
    r = _resolve_plan(DB, "Java", "", "09:30")
    check("找不到浏览器时报错而不是假装成功", r["ok"] is False, str(r)[:120])
    check("找不到浏览器时附上手动补救说明",
          "manual_hint" in r and "remote-debugging-port" in r["manual_hint"], str(r)[:200])

    r2 = plan_install_web(DB, "Java", "", "09:30")
    check("plan_install_web 透传失败原因给网页端",
          r2["ok"] is False and "error" in r2, str(r2)[:120])
finally:
    _cj._find_chrome = _orig

print("\n" + "=" * 62)
print(f"  通过 {len(P)} / {len(P) + len(F)}")
if F:
    print("  失败项：")
    for x in F:
        print("   -", x)
print("=" * 62)
sys.exit(1 if F else 0)

