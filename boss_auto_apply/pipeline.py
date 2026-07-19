"""F8 状态机驱动（设计 §5.11 + §6）。

编排 F1→F1.6→F1.5→F2→...→F6；每 job 推进状态机；处理暂停点；dry-run 控制。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from . import db as db_mod
from .config import AppConfig
from .core import hrbp_check, imager, matcher, parser, profiler, qa, searcher, tailor
from .core.generator import compile_pdf
from .errors import BossAutoError, CircuitBreakerOpenError
from .llm import ClaudeClient
from .models import JobRow
from .ratelimiter import get_limiter

__all__ = ["Pipeline"]


class Pipeline:
    """状态机驱动主流程。

    pipeline.py 只做状态机编排与步骤调度，不直接调 LLM/解析器（调 core/ 各模块）。

    M3 扩展：``image_ready → greeted → text_sent → image_sent`` 三个分支
    （设计 §11.4）。通过 ``sender`` 注入 :class:`~boss_auto_apply.core.sender.Sender`；
    dry-run 模式 ``sender=None``，止于 image_ready 不进发送。
    """

    def __init__(
        self,
        cfg: AppConfig,
        conn: Any,
        llm: ClaudeClient,
        searcher_obj: searcher.BossSearcher,
        *,
        non_interactive: bool = False,
        logger_obj: Any = None,
        sender: Any = None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.llm = llm
        self.searcher = searcher_obj
        self.non_interactive = non_interactive
        self.log = logger_obj or logger
        # 限流器：传 cfg.limits + conn，确保读用户配置（active_hours 等）并持久化 daily_quota。
        # dry-run 模式 get_limiter 返回 DryRunLimiter（忽略 limits 参数，永远放行）。
        self.limiter = get_limiter(
            cfg.pipeline.mode,
            limits=cfg.limits,
            conn=conn,
            logger_obj=self.log,
        )
        # M3 发送编排器（dry-run 模式传 None，止于 image_ready）
        self.sender = sender

    # ============================================================
    # 一次性阶段：F1 解析 → F1.6 体检 → F1.55 候选人画像 → F1.5 画像 → F2 搜索入库
    # ============================================================
    # v3（ADR-0005）重构：删除 pipeline 层 _TITLE_WHITELIST / _TITLE_BLACKLIST。
    # 旧版按"产品经理"白名单硬编码过滤 title，无法适配其他候选人（Java/销售/...）。
    # 新版改为：所有 title 都入库，由 matcher.md LLM 基于 candidate_profile 判断
    # category_passed + level_match（一票否决），完全通用化。
    # 详见 matcher.md「岗位类别判断」+「资历级别判断」章节。
    def run_initial(self) -> None:
        """F1 → F1.6 → F1.55 → F1.5 → F2。"""
        master_path = self.cfg.paths.get("master_json", "data/master.json")
        resume_path = self.cfg.paths.get("resume_input", "input/resume.pdf")

        # ---- F1 解析 ----
        master = self._step_parse(resume_path, master_path)

        # ---- F1.6 体检（闸门）----
        master = self._step_hrbp_check(master, master_path)

        # ---- F1.55 候选人画像补全（ADR-0005，老 master 兼容）----
        master = self._step_candidate_profile(master, master_path)

        # ---- F1.5 画像 ----
        confirmed_targets = self._step_profile(master)

        # ---- F2 搜索入库 ----
        self._step_search(confirmed_targets, master)

    def _step_parse(self, resume_path: str, master_path: str) -> dict[str, Any]:
        """F1：简历文件 → master.json（支持 pdf/docx/md/txt + 交互问答）。"""
        self.log.info("=== F1 解析简历 ===")
        if os.path.exists(master_path):
            # 已有 master，跳过解析（断点续传）
            with open(master_path, encoding="utf-8") as f:
                master = json.load(f)
            self.log.info(f"master.json 已存在，跳过 F1（health={master.get('health_check_status')}）")
            return master

        ext = os.path.splitext(resume_path)[1]
        p = parser.get_parser(ext=ext, logger_obj=self.log)
        self.log.info(f"简历格式：{ext or '(无扩展名)'}，解析器：{getattr(p, 'name', '?')}")
        md = parser.parse_resume_to_markdown(resume_path, p)
        master = parser.structure_with_llm(md, self.llm, logger_obj=self.log)
        master.setdefault("health_check_status", "pending")

        # F1.7：简历存疑点审查 + 交互问答（require_resume_review 开关）
        if getattr(self.cfg.pipeline, "require_resume_review", True):
            self.log.info("=== F1.7 简历审查问答 ===")
            questions = qa.review_resume(master, self.llm, logger_obj=self.log)
            qa.present_questions(questions)
            answers = qa.await_qa_answers(
                questions, master_path,
                non_interactive_default_accept=self.non_interactive,
                logger_obj=self.log,
            )
            master = qa.apply_answers_to_master(master, answers)

        Path(master_path).parent.mkdir(parents=True, exist_ok=True)
        with open(master_path, "w", encoding="utf-8") as f:
            json.dump(master, f, ensure_ascii=False, indent=2)
        self.log.info(f"F1 完成：master.json → {master_path}")
        return master

    def _step_hrbp_check(self, master: dict[str, Any], master_path: str) -> dict[str, Any]:
        """F1.6：体检闸门。"""
        self.log.info("=== F1.6 HRBP 体检 ===")
        if master.get("health_check_status") == "passed":
            self.log.info("体检已通过，跳过 F1.6")
            return master
        report = hrbp_check.run_health_check(master, self.llm, logger_obj=self.log)
        # 落体检报告
        health_path = self.cfg.paths.get("master_health", "data/master.health.json")
        Path(health_path).parent.mkdir(parents=True, exist_ok=True)
        with open(health_path, "w", encoding="utf-8") as f:
            json.dump({"report": report.raw, "markdown": report.markdown}, f,
                      ensure_ascii=False, indent=2)
        hrbp_check.present_report(report)
        master = hrbp_check.await_user_confirmation(
            report, master_path, non_interactive_default_accept=self.non_interactive,
            logger_obj=self.log,
        )
        return master

    def _step_candidate_profile(
        self, master: dict[str, Any], master_path: str,
    ) -> dict[str, Any]:
        """F1.55：候选人画像补全（ADR-0005）。

        老的 master.json（v3 之前）没有 ``basics.candidate_profile`` 字段。
        这里调 LLM 补全，并写回 master.json。

        已有完整 candidate_profile 的 master 直接跳过（断点续传兼容）。

        Args:
            master: 已体检通过的 master dict。
            master_path: master.json 路径（写回用）。

        Returns:
            补全后的 master dict（candidate_profile 已就位）。
        """
        self.log.info("=== F1.55 候选人画像补全 ===")
        cp = (master.get("basics") or {}).get("candidate_profile")
        if cp and isinstance(cp, dict) and cp.get("target_role_keywords"):
            self.log.info("candidate_profile 已存在，跳过 F1.55")
            return master
        master = parser.derive_candidate_profile(master, self.llm, logger_obj=self.log)
        # 写回 master.json（持久化，下次断点续传不必重补）
        try:
            with open(master_path, "w", encoding="utf-8") as f:
                json.dump(master, f, ensure_ascii=False, indent=2)
            self.log.info(f"F1.55 完成：candidate_profile 已写回 {master_path}")
        except Exception as e:
            self.log.warning(f"F1.55 写回 master.json 失败（不影响流程）：{e}")
        return master

    def _step_profile(self, master: dict[str, Any]) -> list[profiler.RecommendedTarget]:
        """F1.5：画像 + 确认。"""
        self.log.info("=== F1.5 画像推荐 ===")
        constraints = self.cfg.target_constraints
        targets_path = self.cfg.paths.get("targets_confirmed", "data/targets.confirmed.yaml")
        if os.path.exists(targets_path):
            import yaml
            with open(targets_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            confirmed = [profiler.RecommendedTarget(**t) for t in data.get("targets", [])]
            if confirmed:
                self.log.info(f"targets.confirmed.yaml 已存在（{len(confirmed)} 条），跳过 F1.5")
                return confirmed

        pr = profiler.run_profiler(master, constraints, self.llm, logger_obj=self.log)
        profiler.present_targets(pr.targets)
        confirmed = profiler.await_target_confirmation(
            pr.targets, targets_path, non_interactive_default_accept=self.non_interactive,
            logger_obj=self.log,
        )
        return confirmed

    def _step_search(
        self, confirmed: list[profiler.RecommendedTarget], master: dict[str, Any]
    ) -> None:
        """F2：按 confirmed target 搜索入库。"""
        self.log.info(f"=== F2 搜索（{len(confirmed)} 个 target）===")
        exclude = self.cfg.pipeline.exclude_companies
        seen_ids = {r["job_id"] for r in self.conn.execute("SELECT job_id FROM jobs").fetchall()}

        for target in confirmed:
            params_list = profiler.generate_search_targets([target])
            for sp in params_list:
                # 每个 keyword 单独搜（覆盖 keywords 列表）
                for kw in sp["keywords"]:
                    params = searcher.SearchParams(
                        keyword=kw,
                        city=sp["city"],
                        salary=sp["salary"],
                        experience=self.cfg.target_constraints.get("experience", ""),
                        degree=self.cfg.target_constraints.get("degree", "大专"),
                        per_job_keywords=sp["per_job_keywords"],
                        limit=self.cfg.search.per_target_limit,
                        target_name=sp["name"],
                        sub_direction=sp["sub_direction"],
                        weight=sp["weight"],
                    )
                    try:
                        jobs = self.searcher.search(params)
                    except BossAutoError as e:
                        self.log.error(f"search 失败 target={sp['name']} kw={kw}: {e}")
                        continue
                    # 去重 + 黑名单
                    jobs = searcher.dedupe_jobs(jobs, by_company=self.cfg.pipeline.dedupe_by_company,
                                                exclude_companies=exclude, seen_job_ids=seen_ids)
                    for j in jobs:
                        seen_ids.add(j.job_id)
                        # v3（ADR-0005）：删除 title 白名单过滤。
                        # 所有 title 都入库，由 matcher 基于 candidate_profile 判断 category_passed。
                        # 这样能适配非产品经理候选人（Java/销售/...）。
                        # 拉 JD 详情（Mock 直接从 fixtures 读）
                        try:
                            jd_full = self.searcher.fetch_detail(j.job_id) or j.jd_full
                        except BossAutoError:
                            jd_full = j.jd_full
                        row = JobRow(
                            job_id=j.job_id,
                            target_name=sp["name"],
                            keyword=kw,
                            city=j.city,
                            title=j.title,
                            company=j.company,
                            salary=j.salary,
                            experience=j.experience,
                            degree=j.degree,
                            jd_full=jd_full or j.jd_full,
                            skills_required=j.skills_required,
                            status="found",
                        )
                        db_mod.upsert_job(self.conn, row)
        self.log.info(f"F2 完成，当前 job 总数：{sum(db_mod.count_by_status(self.conn).values())}")

    # ============================================================
    # 循环阶段：jd_analyzed → ... → image_ready
    # ============================================================
    def run_batch(self) -> dict[str, int]:
        """对每个 resumable job 推进到 image_ready。

        Returns:
            各最终状态的计数。
        """
        self.log.info("=== 批量处理 job ===")
        run_id = db_mod.start_run(self.conn, self.cfg.pipeline.mode, target_name=None)
        succeeded = failed = skipped = 0

        pending = db_mod.list_resumable(self.conn)
        self.log.info(f"待处理 job：{len(pending)} 个")
        for job in pending:
            try:
                ok = self._process_one(job)
                if ok:
                    succeeded += 1
                else:
                    skipped += 1
            except BossAutoError as e:
                final = db_mod.mark_failed(self.conn, job.job_id, e,
                                           max_retries=self.cfg.llm.max_retries)
                if final == "skipped":
                    skipped += 1
                else:
                    failed += 1
                self.log.error(f"job {job.job_id} 失败：{e} → {final}")
            except Exception as e:
                # 兜底：未知异常归 network
                final = db_mod.mark_failed(self.conn, job.job_id, e,
                                           max_retries=self.cfg.llm.max_retries)
                if final == "skipped":
                    skipped += 1
                else:
                    failed += 1
                self.log.exception(f"job {job.job_id} 未知异常 → {final}")

        db_mod.end_run(self.conn, run_id, succeeded, failed, skipped)
        counts = db_mod.count_by_status(self.conn)
        self.log.info(f"批量完成：succeeded={succeeded} failed={failed} skipped={skipped} | 状态分布={counts}")
        return counts

    # ============================================================
    # 流式主流程：采一个投一个（M3 真发送优化）
    # ============================================================
    def run_streaming(self) -> dict[str, int]:
        """流式主流程：一次性阶段 → 流式[搜索→抓JD→匹配→定制→PDF→PNG→发送]。

        与 ``run_initial + run_batch``（批量模式）的差异：
        - 搜索阶段**不批量抓 JD**，只拿列表页信息入库（status=found, jd_full 暂空）
        - 每搜到一个岗位，**立即**走 fetch_detail→匹配→定制→PDF→PNG→发送全链路
        - 搜完一个关键词的岗位就处理完，再搜下一个关键词（采一个投一个）

        适用于真发送（provider=drissionpage）。dry-run 仍走 ``run_initial + run_batch``。

        Returns:
            各最终状态的计数。
        """
        self.log.info("=== 流式主流程（采一个投一个）===")

        # ---- 一次性阶段（F1/F1.6/F1.55/F1.5，有产物跳过）----
        master_path = self.cfg.paths.get("master_json", "data/master.json")
        resume_path = self.cfg.paths.get("resume_input", "input/resume.pdf")
        master = self._step_parse(resume_path, master_path)
        master = self._step_hrbp_check(master, master_path)
        master = self._step_candidate_profile(master, master_path)
        confirmed_targets = self._step_profile(master)

        # 缓存 master（避免每个 job 重复读文件）
        master_cache = self._load_master()
        exclude = self.cfg.pipeline.exclude_companies
        seen_ids = {r["job_id"] for r in self.conn.execute("SELECT job_id FROM jobs").fetchall()}

        run_id = db_mod.start_run(self.conn, self.cfg.pipeline.mode, target_name=None)
        succeeded = failed = skipped = 0

        # ---- 流式：遍历 target/keyword，搜到岗位立即处理 ----
        for target in confirmed_targets:
            params_list = profiler.generate_search_targets([target])
            for sp in params_list:
                for kw in sp["keywords"]:
                    params = searcher.SearchParams(
                        keyword=kw,
                        city=sp["city"],
                        salary=sp["salary"],
                        experience=self.cfg.target_constraints.get("experience", ""),
                        degree=self.cfg.target_constraints.get("degree", "大专"),
                        per_job_keywords=sp["per_job_keywords"],
                        limit=self.cfg.search.per_target_limit,
                        target_name=sp["name"],
                        sub_direction=sp["sub_direction"],
                        weight=sp["weight"],
                    )
                    try:
                        jobs = self.searcher.search(params)
                    except BossAutoError as e:
                        self.log.error(f"流式 search 失败 target={sp['name']} kw={kw}: {e}")
                        continue
                    jobs = searcher.dedupe_jobs(
                        jobs, by_company=self.cfg.pipeline.dedupe_by_company,
                        exclude_companies=exclude, seen_job_ids=seen_ids,
                    )
                    self.log.info(f"流式搜索 keyword='{kw}' → {len(jobs)} 个新岗位，逐个处理")

                    for j in jobs:
                        seen_ids.add(j.job_id)
                        # v3（ADR-0005）：删除 title 白名单过滤。
                        # 所有 title 都入库，由 matcher 基于 candidate_profile 判断。
                        # 入库（jd_full 暂空，流式处理时再抓）
                        row = JobRow(
                            job_id=j.job_id,
                            target_name=sp["name"],
                            keyword=kw,
                            city=j.city,
                            title=j.title,
                            company=j.company,
                            salary=j.salary,
                            experience=j.experience,
                            degree=j.degree,
                            jd_full=j.jd_full,
                            skills_required=j.skills_required,
                            status="found",
                        )
                        db_mod.upsert_job(self.conn, row)
                        # ★ 立即流式处理这一个 job
                        try:
                            ok = self._process_one_streaming(row, master_cache)
                            if ok:
                                succeeded += 1
                            else:
                                skipped += 1
                        except BossAutoError as e:
                            final = db_mod.mark_failed(
                                self.conn, j.job_id, e,
                                max_retries=self.cfg.llm.max_retries)
                            if final == "skipped":
                                skipped += 1
                            else:
                                failed += 1
                            self.log.error(f"job {j.job_id} 流式处理失败：{e} → {final}")
                        except CircuitBreakerOpenError:
                            # 熔断开启：停止整个流式流程（保留已处理结果）
                            self.log.error("熔断开启，停止流式发送")
                            db_mod.end_run(self.conn, run_id, succeeded, failed, skipped)
                            raise
                        except Exception as e:
                            final = db_mod.mark_failed(
                                self.conn, j.job_id, e,
                                max_retries=self.cfg.llm.max_retries)
                            if final == "skipped":
                                skipped += 1
                            else:
                                failed += 1
                            self.log.exception(f"job {j.job_id} 流式处理未知异常 → {final}")

        db_mod.end_run(self.conn, run_id, succeeded, failed, skipped)
        counts = db_mod.count_by_status(self.conn)
        self.log.info(f"流式完成：succeeded={succeeded} failed={failed} skipped={skipped} | 状态分布={counts}")
        return counts

    def _process_one_streaming(self, job: JobRow, master_cache: dict[str, Any] | None = None) -> bool:
        """流式单 job 处理：抓JD → 复用 _process_one 全链路。

        与 ``_process_one`` 的唯一差异：在 found 状态时先 fetch_detail 抓 JD 全文
        （推迟抓取，只对实际处理的岗位抓，不批量抓）。

        Args:
            job: 待处理岗位（status 应为 found，或其他中间态走断点续传）。
            master_cache: 预加载的 master dict（避免重复读文件；None 则 _process_one 内部读）。

        Returns:
            True=成功推进，False=业务跳过。
        """
        # found 状态：先抓 JD 全文写回 DB（推迟的 fetch_detail）
        if job.status == "found":
            self.log.info(f"流式抓取 JD 详情：{job.job_id} ({job.title})")
            try:
                jd_full = self.searcher.fetch_detail(job.job_id) or ""
            except BossAutoError as e:
                self.log.warning(f"fetch_detail 失败（用列表页信息兜底）：{e}")
                jd_full = ""
            if jd_full:
                db_mod.update_jd_full(self.conn, job.job_id, jd_full)
                job = db_mod.get_job(self.conn, job.job_id)  # refresh 拿到 jd_full
        # 复用现有状态机（found→jd_analyzed→...→image_sent）
        return self._process_one(job)

    def _process_one(self, job: JobRow) -> bool:
        """单 job 状态机推进。返回 True=成功推进，False=业务跳过。

        限流策略（v2）：处理阶段（matcher/tailor/PDF/PNG）**不再预扣** limiter 名额。
        原因：旧版 `_process_one` 开头 acquire 会更新 last_send_at，
        导致 job 即使没真发送（greet 失败/mark_skipped）也被算作"投递过"，
        流式 N 个新岗位里第 2 个起全部被 interval 闸门拦截（"距上次投递 Xs < 间隔 Ys"）。
        现在 limiter 只在 `Sender.send_application` 内 acquire（image_ready → 发送时），
        处理阶段不受 interval 限制，5 分钟的 tailor 耗时天然满足间隔要求。
        """
        self.log.info(f"--- 处理 job {job.job_id} (status={job.status}) ---")

        master = self._load_master()

        # ---- found → jd_analyzed（F3）----
        if job.status == "found":
            target = self._target_for_job(job)
            mr = matcher.analyze_jd(job, master, target, self.llm, logger_obj=self.log)
            db_mod.transition(self.conn, job.job_id, "jd_analyzed", set_fields={
                "skills_required": mr.skills_required,
                "match_score": mr.match_score,
                "match_reason": mr.reason,
                "jd_signature": mr.jd_signature,
                # v3（ADR-0005）matcher 通用化字段落库
                "level_match": mr.level_match,
                "job_category": mr.job_category,
                "category_passed": mr.category_passed,
                "business_domain_match_score": mr.business_domain_match_score,
            })
            if not mr.worth_applying:
                db_mod.mark_skipped(self.conn, job.job_id,
                                    reason=f"不匹配：{mr.reason}（score={mr.match_score:.2f}）")
                # ⚠️ v2 修复：业务跳过（matcher 判定不匹配）**不**调用 report_result(success=False)。
                # 旧版这里误触 report_failure → consecutive_failures 累积 → 软熔断误开启，
                # 导致后续 job 进 _step_send 时 acquire 被熔断闸门拦截。
                # 业务跳过是正常决策，不是发送失败，不应计入熔断器。
                # daily_quota 也不应预扣（处理阶段从未 acquire 过；见 _process_one docstring）。
                return False
            job = db_mod.get_job(self.conn, job.job_id)  # refresh

        # ---- jd_analyzed → resume_tailored（F4）----
        if job.status == "jd_analyzed":
            target = self._target_for_job(job)
            resumes_out = self.cfg.paths.get("resumes_out", "data/resumes")
            tr = tailor.tailor_resume(job, master, target, self.llm,
                                      out_dir=resumes_out, logger_obj=self.log)
            # 改造3 v4：typ_path 指向入口 cv.typ（若存在）；否则用旧 resume.typ
            job_dir = os.path.join(resumes_out, job.job_id)
            cv_typ = os.path.join(job_dir, "cv.typ")
            if tr.has_profile:
                # 确保入口 cv.typ 已渲染（render_profile_entrypoint）
                from .core.generator import render_profile_entrypoint
                render_profile_entrypoint(job_dir)
                typ_path = cv_typ
            else:
                typ_path = os.path.join(job_dir, "resume.typ")
            # F7 同步生成个性化打招呼话术（haiku，快+省；失败走兜底默认话术）
            # 与简历定制并行：简历用 sonnet 重生成，招呼用 haiku 生成 2-3 句。
            # 落库字段 tailored_greet，发送时由 validate_greet_before_send 校验长度/禁忌词。
            set_fields = {
                "tailored_resume_path": typ_path,
                "health_checked": 1,
            }
            try:
                greet_text = tailor.generate_greet_text(
                    job, master, target, self.llm,
                    max_len=int(getattr(self.cfg.sender, "chat_text_max_len", 200) or 200),
                    logger_obj=self.log,
                )
                if greet_text:
                    set_fields["tailored_greet"] = greet_text
                    self.log.info(f"job {job.job_id} → 个性化招呼已生成：{greet_text[:50]}...")
                else:
                    self.log.info(f"job {job.job_id} 招呼生成返回 None，发送时走兜底默认话术")
            except Exception as e:
                # greet 失败不阻断简历定制流程（兜底默认话术仍可用）
                self.log.warning(f"job {job.job_id} generate_greet_text 异常（不影响简历）：{e}")
            db_mod.transition(self.conn, job.job_id, "resume_tailored", set_fields=set_fields)
            job = db_mod.get_job(self.conn, job.job_id)

        # ---- resume_tailored → pdf_generated（F5）----
        if job.status == "resume_tailored":
            pdf_cfg = self.cfg.pdf
            resumes_out = self.cfg.paths.get("resumes_out", "data/resumes")
            job_dir = os.path.join(resumes_out, job.job_id)
            typ_path = job.tailored_resume_path or os.path.join(job_dir, "resume.typ")
            pdf_path = compile_pdf(
                typ_path, job_dir,
                font=pdf_cfg.get("font", "Noto Sans CJK SC"),
                template_dir=pdf_cfg.get("template_dir", "templates/brilliant-cv"),
                typst_bin=pdf_cfg.get("typst_bin", "typst"),
                logger_obj=self.log,
            )
            db_mod.transition(self.conn, job.job_id, "pdf_generated", set_fields={
                "resume_pdf_path": pdf_path,
            })
            job = db_mod.get_job(self.conn, job.job_id)

        # ---- pdf_generated → image_ready（F6）----
        if job.status == "pdf_generated":
            pdf_cfg = self.cfg.pdf
            resumes_out = self.cfg.paths.get("resumes_out", "data/resumes")
            job_dir = os.path.join(resumes_out, job.job_id)
            typ_path = job.tailored_resume_path or os.path.join(job_dir, "resume.typ")
            png_path = imager.to_image(
                job.job_id, typ_path, job.resume_pdf_path, job_dir,
                dpi=pdf_cfg.get("png_dpi", 200),
                template_dir=pdf_cfg.get("template_dir", "templates/brilliant-cv"),
                typst_bin=pdf_cfg.get("typst_bin", "typst"),
                logger_obj=self.log,
            )
            db_mod.transition(self.conn, job.job_id, "image_ready", set_fields={
                "resume_image_path": png_path,
            })
            self.log.info(f"job {job.job_id} → image_ready ✅")
            # 不 return：刷新 job 后 fall-through 到 image_ready 分支继续推进
            # （流式「采一个投一个」要求 image_ready 后立即真发送，否则该 job 永远不会进 _step_send）
            job = db_mod.get_job(self.conn, job.job_id)

        # 已是 image_ready：dry-run 止于此，真发送进 _step_send
        if job.status == "image_ready":
            if self.cfg.pipeline.dry_run:
                # dry-run：止于 image_ready，不进真发送（副作用隔离）
                self.log.info(f"job {job.job_id} dry-run 止于 image_ready（不进 Sender）")
                return True
            # 真发送：先回滚处理阶段的预扣（_process_one 开头 acquire 的额度），
            # 让 sender.send_application 内部的 acquire 干净接管限流闭环，
            # 避免「处理 acquire + sender acquire」双扣 daily_quota.sent_count。
            # report_result(success=False, risk_signal=None) → _rollback_daily（见 ratelimiter）。
            # [known limitation] burst_window / last_send_at 不回滚（既有行为，与本次修复无关）。
            self.limiter.report_result(job.job_id, success=False)
            return self._step_send(job)

        # ---- M3 发送：greeted / text_sent → 断点续传发送 ----
        if job.status in ("greeted", "text_sent"):
            if self.cfg.pipeline.dry_run:
                self.log.info(f"job {job.job_id} dry-run 跳过 {job.status} 续传发送")
                return True
            return self._step_send(job)

        # image_sent 终态
        if job.status == "image_sent":
            return True

        return False

    def _step_send(self, job: JobRow) -> bool:
        """M3 发送步骤（image_ready/greeted/text_sent → ... → image_sent，设计 §11.4）。

        调用 :class:`~boss_auto_apply.core.sender.Sender.send_application`，
        内部三态推进（greeted/text_sent/image_sent）。限流前置 + 熔断反馈在 Sender 内。

        Args:
            job: 目标岗位（status 应为 image_ready/greeted/text_sent）。

        Returns:
            True=发送成功推进，False=业务跳过。

        Raises:
            BossAutoError: sender 未启用 / 发送失败。
        """
        if self.sender is None:
            raise BossAutoError("M3 发送未启用（sender=None）；dry-run 模式不应进 _step_send")
        try:
            result = self.sender.send_application(job, dry_run=self.cfg.pipeline.dry_run)
        except CircuitBreakerOpenError as e:
            # 熔断开启：停止整批（不让 mark_failed 把 job 标 skipped，留原态等熔断恢复）
            self.log.error(f"熔断开启，停止发送（job={job.job_id}）：{e}")
            raise
        if result.image_sent:
            self.log.info(f"job {job.job_id} 发送完成（image_sent 终态）")
            return True
        if result.error:
            self.log.warning(f"job {job.job_id} 发送部分失败：{result.error}")
        return False

    # ============================================================
    # resume（断点续传）
    # ============================================================
    def resume(self) -> dict[str, int]:
        """断点续传：读 list_resumable，从断点继续。

        若一次性阶段（F1/F1.6/F1.5）未完成，先跑 run_initial。
        """
        master_path = self.cfg.paths.get("master_json", "data/master.json")
        targets_path = self.cfg.paths.get("targets_confirmed", "data/targets.confirmed.yaml")
        if not os.path.exists(master_path) or not os.path.exists(targets_path):
            self.log.info("resume：一次性阶段未完成，先跑 run_initial")
            self.run_initial()
        return self.run_batch()

    # ============================================================
    # 辅助
    # ============================================================
    def _load_master(self) -> dict[str, Any]:
        master_path = self.cfg.paths.get("master_json", "data/master.json")
        with open(master_path, encoding="utf-8") as f:
            return json.load(f)

    def _target_for_job(self, job: JobRow) -> dict[str, Any]:
        """从 targets.confirmed.yaml 找 job 所属 target。"""
        import yaml
        targets_path = self.cfg.paths.get("targets_confirmed", "data/targets.confirmed.yaml")
        with open(targets_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for t in data.get("targets", []):
            if t.get("name") == job.target_name:
                return t
        # 兜底：用 config 的 sub_directions 第一个
        return {"name": job.target_name, "per_job_keywords": [],
                "sub_direction": "", "city": job.city}
