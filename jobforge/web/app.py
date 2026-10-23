# -*- coding: utf-8 -*-
"""
求职工作台 · Web 薄壳
======================

只做三件事：提交导入、查列表、切状态。**不含任何业务逻辑。**

对应 v1.2.0 的 P0-8：把 124 秒的采集塞进 Flask handler，导致请求 3~159 分钟、
浏览器超时、按钮永远转圈。v2 里重活一律在 runtime 层 job 化，
Web 只负责读状态。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# 直启 `python jobforge/web/app.py` 或从别处 `python -m jobforge.web.app` 时，
# Python 按 cwd 找包会找不到 jobforge 自己。补上项目根。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask, jsonify, render_template, request

from jobforge.application.filters import DataGate, HardFilter
from jobforge.adapters.sources.manual import import_from_csv, import_from_text
from jobforge.adapters.llm.config import LLMConfig
from jobforge.adapters.llm.client import LLMClient
from jobforge.adapters.llm.semantic import SemanticAnalyzer
from jobforge.application.analyzer import MatchAnalyzer, CompareAnalyzer, SalaryAdvisor
from jobforge.domain.application import (
    Application, ApplyStatus, Followup, Resume, FollowupChannel,
    FOLLOWUP_SYSTEM, build_followup_prompt, export_applications_csv,
)
from jobforge.persistence.tracking_store import TrackingStore
from jobforge.domain.candidate import Candidate
from jobforge.domain.models import Job, JobStatus, SCHEMA_VERSION
from jobforge.persistence.sqlite_store import IngestReport, Store

# 数据目录锚定到「项目根」，不跟着当前工作目录跑。
# 相对路径按 cwd 解析时，用户从别的目录启动（或者计划任务在不同 cwd 下跑）
# 会把岗位库、简历、模型配置写到别的地方 —— 表现是「明明导入了岗位，
# 首页却是空的」。这跟 v1.2.0 的定时任务病根是同一个坑。
PROJECT_ROOT = _PROJECT_ROOT
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB = str(DATA_DIR / "jobforge.db")
CANDIDATE_PATH = str(DATA_DIR / "candidate.json")
GATE = DataGate()


def create_app(db_path: str = DEFAULT_DB, store: Optional[Store] = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    st = store or Store(db_path)
    app.config["STORE"] = st
    app.config["JSON_AS_ASCII"] = False
    # 实测踩过的坑：Flask 在非 debug 模式下缓存 Jinja 模板，
    # 改完 index.html 刷新页面看到的还是旧版，
    # 人会误以为是浏览器缓存。本地工具没有模板渲染性能压力，
    # 默认开启自动重载；部署时用 JOBFORGE_NO_RELOAD=1 关掉。
    app.config["TEMPLATES_AUTO_RELOAD"] = os.getenv("JOBFORGE_NO_RELOAD") != "1"
    app.config["EXPORT_DIR"] = str(Path(db_path).parent)
    app.config["CANDIDATE_PATH"] = str(
        Path(app.config["EXPORT_DIR"]) / "candidate.json")
    app.config["LLM_PATH"] = str(Path(app.config["EXPORT_DIR"]) / "llm.json")
    ts = TrackingStore(app.config["DB_PATH"] if "DB_PATH" in app.config
                       else str(Path(db_path)))
    app.config["TRACKING"] = ts

    from jobforge.application.collect_job import RunStore
    runs = RunStore(app.config["DB_PATH"] if "DB_PATH" in app.config
                    else str(Path(db_path)))

    def _all_cursors():
        import sqlite3
        c = runs._conn()
        rows = c.execute("SELECT * FROM collect_cursor").fetchall()
        c.close()
        return rows

    def client_llm() -> LLMClient:
        return LLMClient(LLMConfig.load(app.config["LLM_PATH"]), retries=1)

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("index.html", schema_version=SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # 概览 + 数据健康
    # ------------------------------------------------------------------

    @app.get("/api/summary")
    def summary():
        jobs = st.list_jobs(limit=100000)
        daily = st.daily_new_counts(days=14)
        today = daily[0]["date"] if daily else None
        today_new = daily[0]["count"] if daily else 0

        report = IngestReport(
            received=len(jobs),
            inserted=len(jobs),
            incomplete=sum(1 for j in jobs if j.missing_required()),
            completeness=round(sum(j.completeness() for j in jobs) / len(jobs), 3) if jobs else 0.0,
        )
        health = GATE.check(report, jobs)

        return jsonify({
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "total": len(jobs),
            "today_new": today_new,
            "today": today,
            "status_breakdown": st.status_breakdown(),
            "daily": daily,
            "health": health,
        })

    # ------------------------------------------------------------------
    # 岗位列表（带硬性筛选）
    # ------------------------------------------------------------------

    @app.get("/api/jobs")
    def jobs_api():
        f = _filter_from_request(request.args)
        rows = st.list_jobs(limit=500)
        result = f.apply(rows)

        def brief(entry) -> Dict[str, Any]:
            j: Job = entry["job"]
            return {
                "id": getattr(j, "db_id", None),
                "position": j.position,
                "company": j.company,
                "salary_text": j.salary_text,
                "salary_min": j.salary_min,
                "salary_max": j.salary_max,
                "salary_state": j.salary_state,
                "salary_period": j.salary_period,
                "city": j.city,
                "district": j.district,
                "experience": j.experience,
                "education": j.education,
                "employment_type": j.employment_type,
                "campus_batch": j.campus_batch,
                "company_size": j.company_size,
                "company_industry": j.company_industry,
                "status": j.status,
                "score": j.score,
                "completeness": j.completeness(),
                "missing": j.missing_required(),
                "first_seen_at": j.first_seen_at,
                "is_outsourcing_risk": j.is_outsourcing_risk,
                "outsourcing_evidence": j.outsourcing_evidence,
                "tags": j.tags,
                "url": j.url,
                "jd_head": _jd_head(j),
            }

        # 带真实数据库主键（由 Store 在读取时挂载）
        kept = [brief(e) for e in result["kept"]]

        return jsonify({
            "ok": True,
            "total": result["total"],
            "kept_count": result["kept_count"],
            "dropped_count": result["dropped_count"],
            "kept": kept,
            "dropped_sample": [
                {"position": e["job"].position, "company": e["job"].company,
                 "reasons": e["reasons"]}
                for e in result["dropped"][:8]
            ],
        })

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------

    @app.post("/api/jobs/import")
    def import_api():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "text")
        raw = (body.get("content") or "").strip()

        if not raw:
            return jsonify({"ok": False, "error": "内容为空"}), 400

        if mode == "csv":
            jobs, errors = import_from_csv(raw)
        else:
            items = [b for b in raw.split("\n\n") if b.strip()]
            jobs, errors = import_from_text(items)

        report = st.upsert_many(jobs)
        payload = report.to_dict()
        payload.update({"ok": True, "parsed": len(jobs), "parse_errors": errors})
        payload["health"] = GATE.check(report, jobs)
        return jsonify(payload)

    # ------------------------------------------------------------------
    # 状态切换
    # ------------------------------------------------------------------

    @app.post("/api/jobs/<int:job_id>/status")
    def status_api(job_id: int):
        body = request.get_json(silent=True) or {}
        status = body.get("status")
        if status not in [s.value for s in JobStatus]:
            return jsonify({"ok": False, "error": f"非法状态 {status}"}), 400
        st.set_status(job_id, status, body.get("note", ""))
        return jsonify({"ok": True, "id": job_id, "status": status})

    # ------------------------------------------------------------------
    # 候选人档案 —— 分析引擎的另一半输入
    # ------------------------------------------------------------------

    @app.post("/api/candidate")
    def candidate_api():
        body = request.get_json(silent=True) or {}
        resume = (body.get("resume") or "").strip()
        name = (body.get("name") or "").strip()
        expect = (body.get("salary_expect") or "").strip()
        city = (body.get("target_city") or "").strip()

        if not resume:
            return jsonify({"ok": False, "error": "简历内容为空"}), 400

        cand = Candidate.from_resume(resume, name=name)
        cand.salary_expect = expect
        cand.target_city = city
        cand.finalize()
        cand.save(app.config["CANDIDATE_PATH"])

        return jsonify({
            "ok": True,
            "profile": {
                "name": cand.name,
                "skill_count": len(cand.skills),
                "skills": cand.skills[:40],
                "education": cand.education,
                "is_fresh_grad": cand.is_fresh_grad,
                "experience_years": cand.experience_years,
                "salary_expect": cand.salary_expect,
            },
        })

    @app.get("/api/candidate")
    def candidate_get():
        cand = Candidate.load(app.config["CANDIDATE_PATH"])
        return jsonify({
            "ok": True,
            "configured": bool(cand.skill_names or cand.resume_text),
            "profile": {
                "name": cand.name,
                "skill_count": len(cand.skill_names),
                "skills": cand.skills[:40],
                "education": cand.education,
                "is_fresh_grad": cand.is_fresh_grad,
                "experience_years": cand.experience_years,
                "salary_expect": cand.salary_expect,
                "target_city": cand.target_city,
            },
        })

    # ------------------------------------------------------------------
    # 匹配度分析 —— 产品的核心
    # ------------------------------------------------------------------

    @app.get("/api/analyze")
    def analyze_api():
        cand = Candidate.load(app.config["CANDIDATE_PATH"])
        if not cand.resume_text:
            return jsonify({
                "ok": False,
                "error": "还没有候选人档案，先在「我的能力」里粘贴简历",
            }), 400

        f = _filter_from_request(request.args)
        rows = st.list_jobs(limit=500)
        kept = [e["job"] for e in f.apply(rows)["kept"]]

        analyzer = MatchAnalyzer(cand)
        reports = {getattr(j, "db_id", None): analyzer.analyze(j) for j in kept}

        # 横向对比：按匹配度排序，给出胜负手
        order = sorted(
            kept, key=lambda j: -reports[getattr(j, "db_id", 0)].match_score
        )
        rows_cmp = CompareAnalyzer(cand).compare(order, [reports[getattr(j, "db_id", 0)] for j in order])
        explain = CompareAnalyzer(cand).explain(rows_cmp)

        adv = SalaryAdvisor(cand)
        return jsonify({
            "ok": True,
            "candidate": {
                "skill_count": len(cand.skill_names),
                "education": cand.education,
                "is_fresh_grad": cand.is_fresh_grad,
            },
            "reports": {str(k): v.to_dict() for k, v in reports.items()},
            "compare": [r.to_dict() for r in rows_cmp],
            "explain": explain,
            "salary": {str(getattr(j, "db_id", 0)): adv.view(j, kept).to_dict() for j in kept},
        })

    @app.get("/api/trust/<int:job_id>")
    def trust_one(job_id: int):
        """岗位可信度体检。

        **刻意不参与匹配度计算** —— 详见 domain/trust.py 文件头。
        一个 90% 匹配的岗位如果同时挂着"这可能是条幽灵岗"，
        两条信息必须同时摆在用户眼前。
        """
        from jobforge.domain.trust import check_trust
        job = st.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "岗位不存在"}), 404
        cand = Candidate.load(app.config["CANDIDATE_PATH"])
        return jsonify({"ok": True, **check_trust(job, cand.salary_expect).to_dict()})

    @app.post("/api/trust/batch")
    def trust_batch():
        """给一批岗位体检 —— 首页一次性算完，前端不再逐个请求。"""
        from jobforge.domain.trust import check_trust
        cand = Candidate.load(app.config["CANDIDATE_PATH"])
        body = request.get_json(silent=True) or {}
        ids = body.get("ids") or []
        if not isinstance(ids, list):
            return jsonify({"ok": False, "error": "ids 必须是数组"}), 400
        ids = [int(i) for i in ids[:200] if str(i).lstrip("-").isdigit()]
        out = {}
        for jid in ids:
            j = st.get(jid)
            if j:
                out[str(jid)] = check_trust(j, cand.salary_expect).to_dict()
        return jsonify({"ok": True, "reports": out})

    # ------------------------------------------------------------------
    # 兼职 —— 判据跟全职完全不是一套
    #
    # 全职比「月薪 + 岗位要求」，兼职比的是「时薪 + 能不能居家 + 干什么活」。
    # 拿月薪去筛兼职是荒谬的：兼职根本没有月薪，写出来的多半是日薪。
    # ------------------------------------------------------------------

    def _parttime_payload():
        from jobforge.application.parttime import PartTimeFilter
        body = request.get_json(silent=True) or {}
        f = body.get("filter") or {}
        return PartTimeFilter(
            hourly_min=(float(f["hourly_min"])
                        if str(f.get("hourly_min") or "").strip() else None),
            remote=str(f.get("remote") or ""),
            categories=list(f.get("categories") or []),
            settlement=str(f.get("settlement") or ""),
            exclude_scams=bool(f.get("exclude_scams", True)),
            exclude_fake_free=bool(f.get("exclude_fake_free", False)),
            keyword=str(f.get("keyword") or "").strip(),
        )

    def _job_ids_from(body) -> list:
        ids = body.get("ids") or []
        return [int(i) for i in ids[:200] if str(i).lstrip("-").isdigit()]

    @app.get("/api/parttime/meta")
    def parttime_meta():
        """筛选面板需要的静态数据：分类表、居家档位、结算周期。"""
        from jobforge.domain.parttime import CATEGORIES, REMOTE_LABEL
        return jsonify({
            "ok": True,
            "categories": [{"key": c.key, "label": c.label,
                            "usually_remote": c.usually_remote,
                            "caution": c.caution} for c in CATEGORIES],
            "remote_levels": [{"key": k, "label": v}
                              for k, v in REMOTE_LABEL.items()],
            "settlements": ["日结", "周结", "月结"],
        })

    @app.post("/api/parttime/screen")
    def parttime_screen():
        """兼职筛选 + 对比，一次返回。"""
        from jobforge.application.parttime import (
            compare_parttime, filter_parttime, detect_settlement,
        )
        from jobforge.domain.parttime import analyze_parttime, is_parttime_job

        body = request.get_json(silent=True) or {}
        ids = _job_ids_from(body)
        jobs = [j for j in (st.get(i) for i in ids) if j]

        # 只考虑兼职岗；一条都没有时给用户一句话，而不是空列表
        pt_jobs = [j for j in jobs if is_parttime_job(j)]
        if not pt_jobs:
            return jsonify({
                "ok": True, "empty": True,
                "reason": ("这里还没有兼职岗位。兼职岗可以手动导入，"
                           "或者等每天采集时自动攒进来。"),
            })

        f = _parttime_payload()
        result = filter_parttime(pt_jobs, f)
        for item in result["kept"] + result["pending"]:
            item["settlement"] = detect_settlement(item["job"])
        return jsonify({
            "ok": True,
            "kept": [_pt_row(i) for i in result["kept"]],
            "pending": [_pt_row(i) for i in result["pending"]],
            "stats": result["stats"],
            "filter": f.to_dict(),
            "compare": compare_parttime(result["kept"]),
        })

    def _pt_row(item) -> dict:
        r = item["report"].to_dict()
        j = item["job"]
        return {
            "job_id": getattr(j, "db_id", None),
            "position": j.position, "company": j.company,
            "city": j.city, "salary_text": j.salary_text,
            "settlement": item.get("settlement", ""),
            "sort_key": item.get("sort_key"),
            "notes": item.get("notes", []),
            **r,
        }

    @app.get("/api/parttime/job/<int:job_id>")
    def parttime_job(job_id: int):
        job = st.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "岗位不存在"}), 404
        from jobforge.domain.parttime import analyze_parttime
        return jsonify({"ok": True, **analyze_parttime(job).to_dict()})

    @app.post("/api/parttime/report")
    def parttime_report():
        """导出这一批兼职的评审结论（纯文本，可直接复制走）。"""
        from jobforge.application.parttime import (
            PartTimeFilter, compare_parttime, filter_parttime,
        )
        from jobforge.domain.parttime import is_parttime_job

        body = request.get_json(silent=True) or {}
        jobs = [j for j in (st.get(i) for i in _job_ids_from(body)) if j]
        pt_jobs = [j for j in jobs if is_parttime_job(j)]
        if not pt_jobs:
            return jsonify({"ok": False, "error": "没有兼职岗位可评审"}), 400

        result = filter_parttime(pt_jobs, PartTimeFilter())
        cmp_ = compare_parttime(result["kept"])
        lines = [f"兼职岗位评审（{len(pt_jobs)} 个）", "=" * 46]
        for r in cmp_["rows"]:
            lines.append(f"\n{r['position']} @ {r['company']}")
            lines.append(f"  时薪    {r['hourly_display']}")
            lines.append(f"  居家    {r['remote']}"
                         + ("（假自由，要随时在线）" if r["fake_free"] else ""))
            lines.append(f"  内容    {r['category']}"
                         + (f"　注意：{r['category_caution']}" if r["category_caution"] else ""))
            if r["settlement"]:
                lines.append(f"  结算    {r['settlement']}")
            if r["signals"]:
                lines.append("  留意    " + "；".join(s["title"] for s in r["signals"]))
            if r["scam_level"] == "scam":
                lines.append("  ⛔ 有诈骗信号，别接")
        for r in cmp_["assumed_only"]:
            lines.append(f"\n{r['position']}")
            lines.append(f"  时薪    {r['hourly_display']}（{r['why']}，未参与排名）")
        lines.append("")
        lines.append(cmp_["note"])
        return jsonify({"ok": True, "text": "\n".join(lines),
                        "stats": result["stats"]})

    # ------------------------------------------------------------------
    # LLM 配置 —— 用户自己接，不预置任何厂商
    # ------------------------------------------------------------------

    @app.get("/api/llm")
    def llm_get():
        return jsonify({"ok": True, "config": LLMConfig.load(
            app.config["LLM_PATH"]).to_public()})

    @app.post("/api/llm")
    def llm_save():
        body = request.get_json(silent=True) or {}
        old = LLMConfig.load(app.config["LLM_PATH"])
        # 空字符串表示「保持不变」，避免前端回显脱敏串把 Key 覆盖掉
        api_key = (body.get("api_key") or "").strip() or old.api_key

        cfg = LLMConfig(
            base_url=(body.get("base_url") or old.base_url).strip(),
            api_key=api_key,
            model=(body.get("model") or old.model).strip(),
            temperature=float(body.get("temperature", old.temperature)),
            max_tokens=int(body.get("max_tokens", old.max_tokens)),
            timeout=int(body.get("timeout", old.timeout)),
        )
        errs = cfg.validate()
        if errs:
            return jsonify({"ok": False, "error": "；".join(errs),
                            "config": cfg.to_public()}), 400
        cfg.save(app.config["LLM_PATH"])
        return jsonify({"ok": True, "config": cfg.to_public()})

    @app.post("/api/llm/test")
    def llm_test():
        r = LLMClient(LLMConfig.load(app.config["LLM_PATH"])).test_connection()
        return jsonify(r.to_dict())

    @app.post("/api/llm/analyze/<int:job_id>")
    def llm_analyze(job_id: int):
        """对单个岗位跑语义分析。规则结论不受影响，只做加法。

        必须走 AnalysisService —— 它才带语义缓存。之前这里直连
        SemanticAnalyzer，同一岗位每点一次就真打一次模型，白烧额度。
        """
        from jobforge.application.service import AnalysisService

        job = st.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "岗位不存在"}), 404
        cand = Candidate.load(app.config["CANDIDATE_PATH"])
        if not cand.resume_text:
            return jsonify({"ok": False, "error": "还没有候选人简历"}), 400

        svc = AnalysisService(st, cand, llm=LLMConfig.load(app.config["LLM_PATH"]))
        if not svc.llm_enabled:
            return jsonify({"ok": False,
                            "error": "还没接模型服务，先到「设置」里填一下"}), 400

        aj = svc.analyze_one(job, use_llm=True)
        if not aj.semantic or not aj.semantic.available:
            return jsonify({"ok": False,
                            **(aj.semantic.to_dict() if aj.semantic
                               else {"error": "语义分析没跑起来"})}), 400
        return jsonify({"ok": True, **aj.semantic.to_dict(),
                        "from_cache": aj.from_cache,
                        # 让用户知道这次到底花没花额度
                        "message": ("这次直接用了之前的结果，没再调模型"
                                    if aj.from_cache else
                            f"已分析，用时 {aj.elapsed:.0f} 秒")})

    # ------------------------------------------------------------------
    # 求职材料 —— 面试准备 + 简历定制
    #
    # 这两个是从 career-ops（75k star）学来的，补的是我们闭环的断点：
    # 我们会说"这个岗位 60% 匹配"，然后呢？同一份简历投一百个岗位，
    # 进了面试也没有任何准备材料。
    #
    # 铁律不变：LLM 只做加法，且凡是"来自简历"的内容都必须能在简历原文里
    # 一字不差地找到。面试准备最怕的是教用户编，简历定制最怕的是教用户塞。
    # ------------------------------------------------------------------

    def _material_service():
        from jobforge.application.career_service import CareerMaterialService
        cand = Candidate.load(app.config["CANDIDATE_PATH"])
        return CareerMaterialService(
            st, cand, llm=LLMConfig.load(app.config["LLM_PATH"]))

    @app.post("/api/materials/interview/<int:job_id>")
    def materials_interview(job_id: int):
        """准备这一场面试。面试前用得上，不是面试后。"""
        job = st.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "岗位不存在"}), 404
        body = request.get_json(silent=True) or {}
        svc = _material_service()
        if not svc.llm_enabled:
            return jsonify({"ok": False,
                            "error": "还没接模型服务，先到「设置」里填一下"}), 400
        if not svc.candidate.resume_text and not (body.get("resume_text") or "").strip():
            return jsonify({"ok": False,
                            "error": "还没有简历，先到「简历」页粘贴一份"}), 400

        r = svc.interview_prep(job, (body.get("resume_text") or "").strip() or None)
        if not r.available:
            return jsonify({"ok": False, **r.to_dict()}), 400
        return jsonify({"ok": True, **r.to_dict(),
                        "message": ("这次直接用了之前的结果，没再调模型"
                                    if r.from_cache else
                                    f"已生成，用时 {r.elapsed:.0f} 秒")})

    @app.post("/api/materials/tailor/<int:job_id>")
    def materials_tailor(job_id: int):
        """把简历对齐到这个岗位。**只调整呈现，不新增任何事实。**"""
        job = st.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "岗位不存在"}), 404
        body = request.get_json(silent=True) or {}
        svc = _material_service()
        if not svc.llm_enabled:
            return jsonify({"ok": False,
                            "error": "还没接模型服务，先到「设置」里填一下"}), 400
        if not svc.candidate.resume_text and not (body.get("resume_text") or "").strip():
            return jsonify({"ok": False,
                            "error": "还没有简历，先到「简历」页粘贴一份"}), 400

        r = svc.tailor(job, (body.get("resume_text") or "").strip() or None)
        if not r.available:
            return jsonify({"ok": False, **r.to_dict()}), 400
        return jsonify({"ok": True, **r.to_dict(),
                        "message": ("这次直接用了之前的结果，没再调模型"
                                    if r.from_cache else
                                    f"已对齐，用时 {r.elapsed:.0f} 秒")})

    # ------------------------------------------------------------------
    # 每日采集 —— 从今天开始积累
    # ------------------------------------------------------------------

    @app.get("/api/collect/health")
    def collect_health():
        return jsonify({"ok": True, "health": runs.health(),
                        "recent": runs.recent(7),
                        "cursors": [dict(r) for r in _all_cursors()]})

    @app.post("/api/collect/run")
    def collect_run():
        """手动触发一次采集。

        注意：这是**同步**的，会阻塞到抓完（保守速率下可能几分钟）。
        所以前端必须先自检连通性，别让用户对着转圈等一个连不上的浏览器。
        """
        body = request.get_json(silent=True) or {}
        from jobforge.application.collect_job import (
            CollectPlan, run_collect, collector_fetcher_factory,
        )
        from jobforge.application import browser_launcher as bl

        keyword = (body.get("keyword") or "Java").strip()
        city = (body.get("city") or "").strip()
        pages = int(body.get("pages") or 3)
        src = (body.get("source") or "boss").strip().lower()
        if src == "zhaopin":
            from jobforge.adapters.sources.zhaopin import ZhaopinCollector
            col_cls = ZhaopinCollector
        else:
            from jobforge.adapters.sources.boss import BossCollector
            col_cls = BossCollector
            src = "boss"

        stt = bl.get_status(src)
        if not stt.ready:
            # 不再返回命令让用户自己敲 —— 告诉他点哪个按钮
            return jsonify({
                "ok": False, "error": stt.message, "hint": stt.hint,
                "next_step": _next_step(stt), "show_wizard": True,
            }), 400

        col = col_cls(max_pages=pages, rate_limit=not body.get("fast"))
        out = run_collect(
            CollectPlan(keyword=keyword, city=city, pages=pages,
                        sources=[src]),
            st, runs, fetcher=collector_fetcher_factory(col))
        return jsonify({"ok": out.ok, **out.to_dict(),
                        "headline": out.headline()})

    @app.post("/api/collect/status")
    def collect_status():
        """浏览器 + 登录状态。前端据此决定"你现在该点哪个按钮"。"""
        from jobforge.application import browser_launcher as bl
        body = request.get_json(silent=True) or {}
        src = (body.get("source") or "boss").strip().lower()
        st = bl.get_status(src)
        return jsonify({"ok": True, "source": src, **st.to_dict(),
                        "next_step": _next_step(st)})

    @app.post("/api/collect/launch-browser")
    def collect_launch():
        """一键启动专用浏览器 —— 不让用户去敲命令行。"""
        from jobforge.application import browser_launcher as bl
        body = request.get_json(silent=True) or {}
        src = (body.get("source") or "boss").strip().lower()
        r = bl.launch(source=src)
        return jsonify({"ok": bool(r.get("ok")), **r})

    @app.post("/api/collect/check")
    def collect_check():
        from jobforge.application import browser_launcher as bl
        body = request.get_json(silent=True) or {}
        src = (body.get("source") or "boss").strip().lower()
        r = bl.get_status(src)
        return jsonify({"ok": bool(r.get("ready")), **r.to_dict(),
                        "next_step": _next_step(r)})

    def _next_step(st) -> str:
        """把状态翻译成"下一步该做什么"，前端只管照着渲染。"""
        if not st.installed:
            return "install"
        if not st.running:
            return "launch"
        if st.logged_in is False:
            return "login"
        if st.logged_in is None:
            return "confirm"
        return "ready"

    # ------------------------------------------------------------------
    # 每日自动采集 —— 同样不做成「去终端敲 install-task」
    # ------------------------------------------------------------------

    @app.post("/api/collect/auto/status")
    def collect_auto_status():
        from jobforge.application.collect_job import plan_install_status
        return jsonify(plan_install_status())

    @app.post("/api/collect/auto/install")
    def collect_auto_install():
        from jobforge.application.collect_job import plan_install_web
        kw = (request.get_json(silent=True) or {})
        r = plan_install_web(db_path=db_path,
                             keyword=(kw.get("keyword") or "Java"),
                             city=(kw.get("city") or ""),
                             at=(kw.get("at") or "09:30"))
        if not r.get("ok") and "error" in r:
            return jsonify({"ok": False, **r}), 400
        return jsonify(r)

    # ------------------------------------------------------------------
    # 求职台账 —— 求职闭环的第三步到第五步
    # ------------------------------------------------------------------

    @app.get("/api/tracking/summary")
    def tracking_summary():
        return jsonify({"ok": True, "summary": ts.tracking_summary()})

    @app.get("/api/tracking/reminders")
    def tracking_reminders():
        return jsonify({"ok": True, "reminders": ts.reminders()})

    @app.get("/api/tracking/applications")
    def tracking_list():
        status = request.args.get("status")
        open_only = request.args.get("open", "").lower() in ("1", "true", "on")
        apps = ts.list_applications(status=status, open_only=open_only)
        return jsonify({
            "ok": True,
            "applications": [a.to_dict() for a in apps],
            "summary": ts.tracking_summary(),
        })

    @app.post("/api/tracking/applications")
    def tracking_add():
        """把某个岗位标记为已投递。**当场快照岗位信息**。"""
        body = request.get_json(silent=True) or {}
        job_id = body.get("job_id")
        job_fp = body.get("job_fp")

        job = None
        if job_id:
            job = st.get(job_id)
        elif job_fp:
            for j in st.list_jobs(limit=1000):
                if j.fingerprint == job_fp:
                    job = j
                    break
        if not job:
            return jsonify({"ok": False, "error": "找不到这个岗位"}), 404

        resume = None
        if body.get("resume_id"):
            resume = ts.get_resume(int(body["resume_id"]))
        else:
            resume = ts.get_default_resume()

        score, verdict = None, ""
        if body.get("match_score") is not None:
            score = body.get("match_score")
            verdict = body.get("match_verdict", "")

        app_obj = Application.from_job(job, resume=resume,
                                        match_score=score, match_verdict=verdict)
        app_obj.note = body.get("note", "")
        aid = ts.add_application(app_obj)
        return jsonify({"ok": True, "id": aid, "application": app_obj.to_dict()})

    @app.post("/api/tracking/applications/<int:aid>/status")
    def tracking_status(aid: int):
        body = request.get_json(silent=True) or {}
        status = body.get("status")
        extra = {k: v for k, v in body.items()
                 if k in ("note", "result_note", "salary_offer",
                          "reject_reason", "next_step_at", "reply")}
        r = ts.set_status(aid, status, force=bool(body.get("force")), **extra)
        if not r["ok"]:
            return jsonify(r), 400
        return jsonify(r)

    @app.get("/api/tracking/applications/<int:aid>")
    def tracking_detail(aid: int):
        a = ts.get_application(aid)
        if not a:
            return jsonify({"ok": False, "error": "不存在"}), 404
        return jsonify({
            "ok": True,
            "application": a.to_dict(),
            "followups": ts.list_followups(aid),
            "job": (st.get(a.job_id).to_dict()
                    if a.job_id and st.get(a.job_id) else None),
        })

    @app.post("/api/tracking/applications/<int:aid>/followups")
    def tracking_followup(aid: int):
        body = request.get_json(silent=True) or {}
        if not ts.get_application(aid):
            return jsonify({"ok": False, "error": "记录不存在"}), 404
        f = Followup(
            application_id=aid,
            channel=body.get("channel", "platform"),
            content=body.get("content", ""),
            reply=body.get("reply", ""),
            at=body.get("at", ""),
        )
        fid = ts.add_followup(f)
        return jsonify({"ok": True, "id": fid,
                        "application": ts.get_application(aid).to_dict()})

    @app.delete("/api/tracking/applications/<int:aid>")
    def tracking_delete(aid: int):
        ok = ts.delete_application(aid)
        return jsonify({"ok": ok})

    @app.post("/api/tracking/followup-draft/<int:aid>")
    def tracking_draft(aid: int):
        """生成跟进话术草稿。

        **只生成草稿，永不代发** —— 用户必须自己看过、改过、复制走。
        这和 v2 一贯的「不替用户做决定、不替用户说话」是同一条线。
        """
        a = ts.get_application(aid)
        if not a:
            return jsonify({"ok": False, "error": "记录不存在"}), 404

        days = a.days_since_applied()
        result = {"ok": True, "draft": None, "error": ""}

        if not client_llm().available():
            result["error"] = "未配置 LLM，暂不能生成话术草稿"
            # 降级给一个模板，至少让用户有个起点
            result["draft"] = {
                "message": f"您好，我是{a.resume_name and '应聘者' or '应聘者'}，"
                           f"想问一下之前投递的「{a.position}」，目前进展如何？"
                           f"感谢您的时间。",
                "variants": {},
                "tips": ["这是模板，需要你按实际情况修改后再发",
                         "语气保持礼貌即可，不必卑微"],
                "source": "template",
            }
            return jsonify(result)

        sem = SemanticAnalyzer(client_llm())
        user = build_followup_prompt(a, days)
        r = sem.client.chat_json(FOLLOWUP_SYSTEM, user)
        if not r.ok:
            result["error"] = r.error
            return jsonify(result)

        d = r.data or {}
        result["draft"] = {
            "message": str(d.get("message") or ""),
            "variants": d.get("variants") if isinstance(d.get("variants"), dict) else {},
            "tips": [str(x) for x in (d.get("tips") or [])][:5],
            "source": "llm",
        }
        return jsonify(result)

    # ------------------------------------------------------------------
    # 简历版本
    # ------------------------------------------------------------------

    @app.get("/api/resumes")
    def resumes_list():
        return jsonify({"ok": True, "resumes": ts.list_resumes()})

    @app.post("/api/resumes")
    def resumes_add():
        body = request.get_json(silent=True) or {}
        content = (body.get("content") or "").strip()
        if not content:
            return jsonify({"ok": False, "error": "简历内容为空"}), 400
        r = Resume(name=body.get("name", ""), content=content,
                   tag=body.get("tag", ""), is_default=bool(body.get("is_default")))
        rid = ts.add_resume(r)
        return jsonify({"ok": True, "id": rid, "resume": r.to_dict()})

    @app.post("/api/resumes/<int:rid>/default")
    def resumes_default(rid: int):
        ts.update_resume(rid, is_default=True)
        return jsonify({"ok": True, "resumes": ts.list_resumes()})

    @app.delete("/api/resumes/<int:rid>")
    def resumes_delete(rid: int):
        return jsonify({"ok": ts.delete_resume(rid)})

    @app.get("/api/export.tracking.csv")
    def export_tracking():
        out = Path(app.config["EXPORT_DIR"]) / "投递归账.csv"
        export_applications_csv(str(out), ts.list_applications())
        from flask import send_file
        return send_file(str(out), as_attachment=True, mimetype="text/csv")

    @app.get("/api/jobs/<int:job_id>")
    def job_api(job_id: int):
        j = st.get(job_id)
        if not j:
            return jsonify({"ok": False, "error": "不存在"}), 404
        return jsonify({"ok": True, "job": j.to_dict()})

    @app.get("/api/export.csv")
    def export_api():
        from flask import send_file
        out = Path(app.config["EXPORT_DIR"]) / "jobforge-export.csv"
        st.export_csv(str(out))
        return send_file(str(out), as_attachment=True, mimetype="text/csv")

    return app


def _jd_head(j: Job) -> str:
    """JD 摘要：跳过与岗位名/公司名重复的行，避免卡片上出现同一句话两遍。

    解析不出明确 JD 标记时 jd 会等于整段原文，第一行通常就是岗位名，
    直接显示会和标题重复。这里逐行跳过已出现在标题里的行。
    """
    if not j.jd:
        return ""
    seen = {j.position.strip(), j.company.strip()}
    for line in (j.jd or "").splitlines():
        t = line.strip()
        if not t or t in seen:
            continue
        return t[:120]
    return ""


def _filter_from_request(args) -> HardFilter:
    def _int(name):
        v = args.get(name, "")
        return int(v) if str(v).strip().isdigit() else None

    def _bool(name):
        return str(args.get(name, "")).lower() in ("1", "true", "on", "yes")

    def _list(name):
        return [x for x in (args.get(name, "") or "").split(",") if x.strip()]

    return HardFilter(
        cities=_list("cities"),
        salary_min=_int("salary_min"),
        edu_at_least=(args.get("edu", "") or "").strip(),
        require_fresh_grad=_bool("fresh"),
        exclude_outsourcing=not _bool("allow_outsourcing"),
        company_size_max=_int("size_max"),
        keywords_include=_list("include"),
        keywords_exclude=_list("exclude"),
        hide_incomplete=not _bool("show_incomplete"),
    )


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--db", default=DEFAULT_DB)
    a = p.parse_args()
    create_app(a.db).run(host="127.0.0.1", port=a.port, debug=False, threaded=True)
