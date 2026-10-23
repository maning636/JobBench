#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
求职工作台 · 命令行入口
========================

刻意不用 click —— v1.2.0 的 requirements.txt 里漏了 click，
导致 `python -m cli.main --help` 直接 ImportError，CLI 从未跑起来过。
标准库 argparse 零依赖，干净环境必然可用。

用法：
    python -m jobforge.cli web                  启动 Web 界面
    python -m jobforge.cli seed                 导入演示数据
    python -m jobforge.cli import <file>        从 txt/csv 导入
    python -m jobforge.cli list                 查看岗位
    python -m jobforge.cli daily                执行一次每日任务（推送测试）
    python -m jobforge.cli test                 跑测试
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobforge.adapters.sources.manual import import_from_csv, import_from_text
from jobforge.application.filters import DataGate, HardFilter
from jobforge.persistence.sqlite_store import Store
from jobforge.runtime.notifier import FeishuNotifier
from jobforge.runtime.scheduler import RunLog, run_daily

# 数据路径锚定到项目根，不跟当前工作目录跑。
# 相对路径按 cwd 解析时，从别处启动会把岗位库/简历写到别处，
# 表现是「明明导入了岗位，首页却是空的」。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = str(PROJECT_ROOT / "data" / "jobforge.db")


def resolve_db(path: str) -> str:
    """把相对路径锚到项目根，绝对路径原样返回。"""
    p = Path(path)
    return str(p if p.is_absolute() else (PROJECT_ROOT / p))


def sibling_path(db_path: str, name: str) -> str:
    """取数据库同目录下的某个文件。

    之前用 `a.db.rsplit("/", 1)[0]` 手搓 —— 在 Windows 上路径是反斜杠，
    切不出来，简历路径会算成整个 db 路径 + "/candidate.json"。
    """
    return str(Path(resolve_db(db_path)).parent / name)


def cmd_web(a):
    from jobforge.web.app import create_app
    print(f"求职工作台 → http://127.0.0.1:{a.port}")
    create_app(resolve_db(a.db)).run(host="127.0.0.1", port=a.port, threaded=True)


def cmd_seed(a):
    from jobforge.seed import seed
    seed(resolve_db(a.db))


def cmd_import(a):
    st = Store(resolve_db(a.db))
    raw = Path(a.file).read_text(encoding="utf-8")
    if a.file.lower().endswith(".csv"):
        jobs, errs = import_from_csv(raw)
    else:
        jobs, errs = import_from_text([b for b in raw.split("\n\n") if b.strip()])
    rep = st.upsert_many(jobs)
    print(f"解析 {len(jobs)} 条 · 新增 {rep.inserted} · 更新 {rep.updated} · "
          f"完整率 {rep.completeness:.0%}")
    if errs:
        print("告警：", errs)
    if not rep.healthy:
        print("数据未过看门狗：", rep.warnings)
    print("导出：", st.export_csv("data/export.csv"))


def cmd_list(a):
    st = Store(resolve_db(a.db))
    jobs = st.list_jobs(limit=a.top)
    if not jobs:
        print("库里还没有数据。先跑 `python -m jobforge.cli seed` 或 import")
        return
    for i, j in enumerate(jobs, 1):
        risk = "  ⚠疑似外包" if j.is_outsourcing_risk is True else ""
        missing = ("  [缺" + "、".join(j.missing_required()) + "]") if j.missing_required() else ""
        print(f"{i:>3}. {j.position[:26]:<28} {j.company[:20]:<22} "
              f"{(j.salary_text or '—'):<14} {j.city:<5} "
              f"{j.salary_state:<10}{risk}{missing}")
    print(f"\n共 {len(jobs)} 条")


def cmd_daily(a):
    st = Store(resolve_db(a.db))
    note = FeishuNotifier(dry_run=a.dry_run)
    r = run_daily(st, DataGate(), note, HardFilter(hide_incomplete=True), RunLog(resolve_db(a.db)))
    print(f"符合条件 {r['kept_count']} 条 · 数据健康 {r['health']['pass']} · "
          f"任务状态 {r['run']['state']}")
    if r.get("notify"):
        print(f"推送：{r['notify']}")


def cmd_collect(a):
    """每日采集。这是"从今天开始每天积累"的入口。"""
    from jobforge.application.collect_job import (
        CollectPlan, RunStore, run_collect, collector_fetcher_factory,
    )
    from jobforge.persistence.sqlite_store import Store
    from jobforge.adapters.sources.browser import BrowserError

    src = (getattr(a, "source", "boss") or "boss").strip().lower()
    if src == "zhaopin":
        from jobforge.adapters.sources.zhaopin import ZhaopinCollector, ParseError
        col_cls = ZhaopinCollector
        platform_name = "智联招聘"
    else:
        from jobforge.adapters.sources.boss import BossCollector, ParseError
        col_cls = BossCollector
        platform_name = "BOSS 直聘"
        src = "boss"

    runs = RunStore(resolve_db(a.db))

    # --check：只做连通性自检，不真抓。给用户一个明确的「能不能用」结论。
    if a.check:
        col = col_cls(rate_limit=False)
        r = col.check_ready()
        if r["ok"]:
            print(f"✓ {r.get('note', 'Chrome 已连接')}")
            print(f"  端点：{col.b.base}")
            print(f"  平台：{platform_name}")
            return 0
        print(f"✗ {r['reason']}")
        if r.get("howto"):
            print(f"\n请先执行：\n  {r['howto']}\n\n然后在那个 Chrome 窗口里登录{platform_name}。")
        return 1

    if a.history:
        print("=" * 66)
        print("  采集历史")
        print("=" * 66)
        h = runs.health()
        print(f"  状态：{h['text']}")
        for r in runs.recent(a.history):
            flag = "✓" if r["ok"] else "✗"
            print(f"  {flag} {r['started_at'][:16]}  {r['source']:<8} "
                  f"新增 {r['inserted']:<4} 更新 {r['updated']:<4} "
                  f"完整率 {r['completeness']:.0%}")
            for e in r["errors"][:2]:
                print(f"        ✗ {e}")
        return 0

    plan = CollectPlan(keyword=a.keyword, city=a.city, pages=a.pages,
                       sources=[src])
    col = col_cls(max_pages=a.pages, rate_limit=not a.fast)
    print(f"采集前自检（{platform_name}）…")
    ready = col.check_ready()
    if not ready["ok"]:
        print(f"✗ {ready['reason']}")
        if ready.get("howto"):
            print(f"\n请先执行：\n  {ready['howto']}\n\n"
                  f"然后在那个 Chrome 窗口里登录{platform_name}，再跑一次。\n"
                  f"（可以先用 `collect --check` 单独验证连通性）")
        return 1
    print(f"  {ready.get('note', '')}")

    print(f"\n开始采集：{platform_name} · 关键词「{a.keyword}」"
          + (f" · {a.city}" if a.city else "") + f" · 最多 {a.pages} 页")
    if not a.fast:
        print("  保守速率（每页间隔 45~90 秒），加 --fast 可跳过等待")

    def prog(msg):
        print(f"  {msg}")

    out = run_collect(
        plan, Store(resolve_db(a.db)), runs,
        fetcher=collector_fetcher_factory(col),
        resume=not a.no_resume,
        progress=prog,
    )

    print()
    print("=" * 66)
    print(f"  {out.headline()}")
    print("=" * 66)
    print(f"  页数 {out.pages_done}/{out.pages_total}"
          + (f"（跳过 {out.skipped} 页）" if out.skipped else ""))
    print(f"  完整率 {out.completeness:.0%}　耗时 {out.elapsed:.0f}s")
    for w in out.warnings:
        print(f"  ⚠ {w}")
    for e in out.errors:
        print(f"  ✗ {e}")
    print()
    print("  历史与健康状态：python -m jobforge.cli collect --history")
    return 0 if out.ok else 1


def cmd_install_task(a):
    """安装 Windows 每日计划任务。"""
    from jobforge.application.collect_job import plan_install
    return plan_install(resolve_db(a.db), a.keyword, a.city, a.time)


def cmd_analyze(a):
    """批量分析。这是 CLI 最常用的命令 —— 不用开界面就能刷一遍。"""
    from jobforge.application.service import AnalysisService, render_markdown
    from jobforge.domain.candidate import Candidate, CandidateLoadError

    st = Store(resolve_db(a.db))
    cpath = sibling_path(a.db, "candidate.json")
    try:
        cand = Candidate.load(cpath)
    except CandidateLoadError as e:
        # 实测踩过的坑：文件带 BOM 或被改坏，之前这里静默当成"没档案"，
        # 用户看到的提示和自己的问题完全对不上号。
        print(f"✗ {e}")
        return 1

    if not (cand.resume_text or cand.skill_names):
        print("还没有候选人档案。先建一个：")
        print("  python -m jobforge.cli profile --file 我的简历.txt")
        print("或者在 Web 界面的「我的能力」里粘贴简历。")
        return 1

    svc = AnalysisService(st, cand)

    if a.id:
        job = st.get(a.id)
        if not job:
            print(f"没有 id={a.id} 的岗位。用 `list` 看看有哪些。")
            return 1
        jobs = [job]
    elif a.match:
        # 真实可用性问题：row_id 是 SQLite AUTOINCREMENT（clear 后也不重用，
        # 实测跑到 51~66），让用户记这个不现实。必须能按岗位名找。
        kw = a.match.lower()
        jobs = [j for j in st.list_jobs(limit=1000)
                if kw in j.position.lower() or kw in j.company.lower()]
        if not jobs:
            print(f"没有岗位名或公司包含「{a.match}」的。用 `list` 看看有哪些。")
            return 1
        if len(jobs) > 1:
            print(f"匹配到 {len(jobs)} 个岗位：")
            for j in jobs[:10]:
                print(f"  {j.position} @ {j.company}（row_id={getattr(j, 'db_id', '?')}）")
            if not a.all:
                print("\n加 --all 分析全部匹配项，或用 --id 指定某一个。")
                return 1
            print()
    else:
        jobs = st.list_jobs(limit=1000)
        if a.top:
            # 粗排后取前 N 个。
            # **必须按和最终对比一样的顺序排**，不能只按 match_score ——
            # 实测踩过的坑：只按分数排，一个 JD 写得极少的实习岗
            # 靠"分母小"拿了 100% 冲进前 N，
            # 结果 top 6 里有 5 个是「无法评估」，真正能评估的优质岗反被砍掉。
            scored = [(svc.matcher.analyze(j), j) for j in jobs]
            rows = svc.comparer.compare([j for _, j in scored],
                                        [r for r, _ in scored])
            # 用业务键（岗位名+公司）做映射，不能用 id()。
            # 实测踩过的坑：compare() 返回的是**新建的 CompareRow 实例**，
            # id() 和 MatchReport 的 id 根本不是同一个对象，
            # 查不到就全退化成 99，"按可评估性优先"的排序静默失效。
            order = {(r.position, r.company): i for i, r in enumerate(rows)}
            scored.sort(key=lambda x: (
                order.get((x[0].job_position, x[0].job_company), 99),
                -x[0].match_score))
            jobs = [j for _, j in scored[: a.top]]
            evaluable = sum(1 for r, _ in scored
                            if r.verdict != "无法评估")
            print(f"全库 {len(scored)} 个岗位，其中 {evaluable} 个可评估；"
                  f"粗排后取前 {len(jobs)} 个")

    if not jobs:
        print("库里没有岗位。先跑 `python -m jobforge.cli seed` 或 import。")
        return 1

    use_llm = a.llm or (not a.no_llm and a.all_llm)
    if use_llm and not svc.llm_enabled:
        print("⚠ 未配置 LLM，本次只跑规则引擎。"
              "配置见 data/llm.json 或 Web 界面「模型接入」。\n")

    print(f"开始分析 {len(jobs)} 个岗位"
          f"（{'规则 + LLM 语义' if use_llm and svc.llm_enabled else '仅规则引擎'}）\n")

    def progress(i, total, r):
        tag = "缓存" if r.from_cache else (f"{r.elapsed:.0f}s" if use_llm else "")
        sem = ""
        if r.semantic:
            if not r.semantic.available:
                sem = "  [LLM跳过]"
            else:
                n = len(r.semantic.semantic_matches) + len(r.semantic.weak_matches)
                sem = f"  语义命中 {n}"
        print(f"  [{i}/{total}] {r.job.position[:24]:<26} "
              f"{r.report.match_score:>3.0f}%  {r.report.verdict:<6} {tag}{sem}")

    try:
        analyzed = svc.analyze_many(jobs, use_llm=use_llm, progress=progress)
    except KeyboardInterrupt:
        print("\n已中断。已完成的分析结果仍在缓存里，下次跑会跳过。")
        analyzed = []

    if not analyzed:
        return 1

    cmp_data = svc.compare(analyzed)

    print("\n" + "=" * 66)
    print("  结论")
    print("=" * 66)
    for e in cmp_data["explain"]:
        print(f"  · {e}")
    if not cmp_data["explain"]:
        print("  （可评估的岗位不足 2 个，无法横向对比）")

    print("\n" + "=" * 66)
    print(f"  {'岗位':<26}{'匹配':>6}  {'结论':<8}{'风险'}")
    print("=" * 66)
    for i, r in enumerate(cmp_data["rows"], 1):
        risk = "；".join(r.get("cons", [])[:2]) or "—"
        mark = ""
        if r["verdict"] == "无法评估":
            mark = " ⚠"
        print(f"  {i:<3}{r['position'][:24]:<26}{r['match_score']:>5.0f}%  "
              f"{r['verdict']:<8}{risk}{mark}")

    if a.verbose:
        print("\n" + "=" * 66)
        print("  逐个岗位详情")
        print("=" * 66)
        for x in sorted(analyzed, key=lambda y: -y.report.match_score):
            print()
            print(x.report.to_display())
            for n in x.report.notes:
                print(f"  · {n}")

    if a.export:
        md = render_markdown(analyzed, cand, cmp_data)
        Path(a.export).write_text(md, encoding="utf-8")
        print(f"\n报告已导出：{a.export}")

    if a.json:
        out = Path(a.json)
        import json as _j
        out.write_text(_j.dumps(
            {"results": [x.to_dict() for x in analyzed],
             "compare": cmp_data}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已导出：{out}")

    stats = st.semantic_stats()
    print(f"\n语义缓存：{stats['cached']} 条（下次同岗位同简历会直接命中）")
    return 0


def cmd_profile(a):
    """从文件建立候选人档案。"""
    from jobforge.domain.candidate import Candidate
    text = Path(a.file).read_text(encoding="utf-8-sig")
    c = Candidate.from_resume(text, name=a.name or "")
    if a.expect:
        c.salary_expect = a.expect
    if a.city:
        c.target_city = a.city
    c.finalize()
    p = c.save(sibling_path(a.db, "candidate.json"))
    print(f"档案已保存：{p}")
    print(f"  识别技能 {len(c.skills)} 项：{'、'.join(c.skill_names[:15])}")
    print(f"  学历 {c.education or '未识别'}　"
          f"{'应届/在校' if c.is_fresh_grad else f'经验 {c.experience_years} 年'}")
    return 0


def cmd_test(a):
    import subprocess
    tests = [
        "check_dom_contract.py",       # JS 引用的 id 必须都存在
        "test_contract_and_flow.py",   # 契约与流程
        "test_boss_parser.py",         # BOSS 解析层（离线 fixture）
        "test_zhaopin_parser.py",      # 智联解析层（离线 fixture）
        "test_browser_launcher.py",    # 浏览器启动 / 平台切换（共用窗口）
        "test_collect_task.py",        # 每日任务：断点续采 / 失败可见
        "test_tracking.py",            # 台账：状态机/提醒/简历版本
        "test_llm.py",                 # LLM 客户端与降级
        "test_output_guard.py",        # 模型输出清洗（对抗）
        "test_career_materials.py",    # 面试准备 / 简历定制（对抗：禁教用户撒谎）
        "test_trust.py",               # 岗位可信度体检（可信度不扣匹配度）
        "test_parttime.py",            # 兼职：时薪/居家/分类/陷阱（口径争议）
    ]
    root = Path(__file__).resolve().parents[1]
    failed = 0

    # 漏跑检测：tests/ 下有 test_*.py 却没注册进上面的列表 = 有人加了测试
    # 忘了挂上，那份测试**永远不会跑** —— 这是静默的质量倒退，比测试失败更难发现。
    registered = set(tests)
    on_disk = {p.name for p in (root / "tests").glob("test_*.py")}
    missing = sorted(on_disk - registered)
    if missing:
        print(f"\n⚠ 有测试文件没注册进测试列表：{missing}")
        print("  它们不会被执行。请把文件名加进 cmd_test 的 tests 列表。")
        failed += 1

    for t in tests:
        p = root / "tests" / t
        print(f"\n===== {t} =====")
        # cwd 必须显式指到项目根。
        # 不传的话子进程继承「用户当前工作目录」——
        # 在别处跑 `python -m jobforge.cli test` 会让测试里的相对路径
        # （data/ 下的库文件）解析到别处，出现「单跑一个文件全过、
        # 串起来跑就挂」这种假失败。实测踩过。
        failed += subprocess.call([sys.executable, "-X", "utf8", str(p)], cwd=str(root))
    print("\n" + ("全部通过" if not failed else f"有 {failed} 个测试文件失败"))
    return failed


def main():
    p = argparse.ArgumentParser(prog="jobforge", description="求职工作台 · 每日自动筛岗")
    p.add_argument("--db", default=DB, help="数据库路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("web", help="启动 Web 界面"); w.add_argument("--port", type=int, default=8080)
    w.set_defaults(fn=cmd_web)

    s = sub.add_parser("seed", help="导入演示数据"); s.set_defaults(fn=cmd_seed)

    i = sub.add_parser("import", help="从文件导入"); i.add_argument("file"); i.set_defaults(fn=cmd_import)

    l = sub.add_parser("list", help="查看岗位"); l.add_argument("--top", type=int, default=20); l.set_defaults(fn=cmd_list)

    d = sub.add_parser("daily", help="执行一次每日任务"); d.add_argument("--dry-run", action="store_true"); d.set_defaults(fn=cmd_daily)

    an = sub.add_parser("analyze", help="批量分析岗位匹配度（不用开界面）")
    an.add_argument("--id", type=int, help="按数据库主键分析单个岗位")
    an.add_argument("--match", help="按岗位名或公司模糊匹配，如 --match Java")
    an.add_argument("--all", action="store_true",
                    help="--match 匹配到多个时，全部分析而不是只列出来")
    an.add_argument("--top", type=int, default=0, help="粗排后只深入分析前 N 个")
    an.add_argument("--llm", action="store_true", help="启用 LLM 语义分析")
    an.add_argument("--all-llm", action="store_true", help="对全部岗位启用 LLM（默认只跑规则）")
    an.add_argument("--no-llm", action="store_true", help="强制只用规则引擎")
    an.add_argument("-v", "--verbose", action="store_true", help="打印逐个岗位详情")
    an.add_argument("--export", help="导出 Markdown 报告到指定文件")
    an.add_argument("--json", help="导出 JSON 到指定文件")
    an.set_defaults(fn=cmd_analyze)

    pr = sub.add_parser("profile", help="从简历文件建立候选人档案")
    pr.add_argument("file", help="简历文件路径（txt/md 均可）")
    pr.add_argument("--name", help="姓名")
    pr.add_argument("--expect", help="期望薪资，如 18K")
    pr.add_argument("--city", help="目标城市")
    pr.set_defaults(fn=cmd_profile)

    t = sub.add_parser("test", help="跑测试"); t.set_defaults(fn=cmd_test)

    co = sub.add_parser("collect", help="每日采集（从今天开始积累）")
    co.add_argument("--keyword", default="Java", help="搜索关键词")
    co.add_argument("--city", default="", help="城市，留空=全国")
    co.add_argument("--pages", type=int, default=3, help="最多翻几页")
    co.add_argument("--source", default="boss", choices=["boss", "zhaopin"],
                    help="采集平台：boss=BOSS直聘，zhaopin=智联招聘")
    co.add_argument("--fast", action="store_true", help="跳过页间等待（不推荐）")
    co.add_argument("--check", action="store_true", help="只做连通性自检，不抓取")
    co.add_argument("--history", type=int, nargs="?", const=10, default=0,
                    help="查看采集历史")
    co.add_argument("--no-resume", action="store_true", help="不续采，从第一页开始")
    co.set_defaults(fn=cmd_collect)

    it = sub.add_parser("install-task", help="安装 Windows 每日计划任务")
    it.add_argument("--keyword", default="Java")
    it.add_argument("--city", default="")
    it.add_argument("--time", default="09:30", help="每天几点跑，默认 09:30")
    it.set_defaults(fn=cmd_install_task)

    a = p.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
