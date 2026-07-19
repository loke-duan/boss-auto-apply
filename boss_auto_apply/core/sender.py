"""F7/M3 发送编排器（设计 §4.1 / §10 / §13.4）。

替换 M1+M2 的 ``NotImplementedError`` stub。``Sender.send_application`` 编排：
限流闸门 → BrowserManager → WebGreeter（点击沟通）→ 话术校验 → WebChatSender（发话术+图片）
→ 限流反馈，每步推进状态机（greeted/text_sent/image_sent）。

dry-run 安全：所有真发送操作（点击沟通/发话术/发图片）由 ``dry_run`` 开关控制，
dry_run=True 时只 log 不真执行，状态推进用 mock（设计 §13.4 A6）。

幂等：从 job 当前状态断点续传（greeted→只发话术；text_sent→只发图片）。
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from .. import db as db_mod
from ..browser.manager import BrowserManager
from ..browser.web_chat_sender import ChatSendResult, WebChatSender
from ..browser.web_greeter import GreetResult, WebGreeter
from ..errors import (
    BossAutoError,
    CaptchaDetectedError,
    CircuitBreakerOpenError,
    GreetValidationError,
    RateLimitedError,
)
from ..models import JobRow
from ..ratelimiter import AcquireContext, RealRateLimiter

__all__ = ["Sender", "validate_greet_before_send", "GreetResult", "ChatSendResult"]


# ============================================================
# 话术校验（设计 §10.3）
# ============================================================
# 禁含内容关键词（HRBP 强烈反对）
_SALARY_KEYWORDS = ("薪资", "月薪", "年薪", "k$", "期望薪资", "薪水", "待遇", "酬")
_GAP_KEYWORDS = ("空窗", "待业", "辞职", "失业", "裸辞", "gap")
_DEGREE_KEYWORDS = ("大专", "专升本", "自考", "成教", "学历")
# ADR-0002 禁忌词 — 通用过度承诺词（候选人专属词从 master.constraints 注入）
_FORBIDDEN_KEYWORDS = ("精通", "100%", "完美", "极致")


def validate_greet_before_send(
    greet: str,
    job: JobRow | None = None,
    *,
    max_len: int = 200,
    logger_obj: Any = None,
) -> str:
    """发送前最后一道话术校验（防 LLM 生成违规话术，设计 §10.3）。

    校验项：
    1. 非空（空话术 → 用兜底默认）。
    2. 长度：超 ``max_len`` → 截断到最后一个完整句号。
    3. 禁含内容扫描：薪资/空窗/学历（JD 要求该学历时放行）/禁忌词 → 命中抛错。

    Args:
        greet: 待校验话术。
        job: 目标岗位（用于判断 JD 是否要求该学历，放行学历词）。
        max_len: 最大字数（[假设] 200，待实测 T6）。
        logger_obj: loguru logger。

    Returns:
        校验通过的话术（可能截断 / 兜底）。

    Raises:
        GreetValidationError: 话术违规（含薪资/空窗/禁忌词）。
    """
    log = logger_obj or logger
    text = (greet or "").strip()
    # 1. 空话术 → 兜底默认
    title = (job.title if job else None) or "贵司"
    if not text:
        fallback = f"您好，看到贵司 {title} 岗位，希望进一步沟通。"
        log.warning(f"话术为空，用兜底默认：{fallback}")
        text = fallback
    # 3. 禁含内容扫描（先做，超长截断后内容也违规，所以先扫全文）
    _scan_forbidden(text, job=job)
    # 2. 超长截断到最后一个句号
    if len(text) > max_len:
        cut = text[:max_len]
        # 截到最后一个完整句号
        for sep in ("。", "！", "？", ".", "!", "?"):
            idx = cut.rfind(sep)
            if idx > max_len // 2:
                cut = cut[: idx + 1]
                break
        log.warning(f"话术超 {max_len} 字，截断到 {len(cut)} 字")
        text = cut
    return text


def _scan_forbidden(text: str, job: JobRow | None = None) -> None:
    """扫描禁含关键词，命中抛 :class:`GreetValidationError`。"""
    # 薪资词
    for kw in _SALARY_KEYWORDS:
        if kw in text.lower() or kw in text:
            raise GreetValidationError(f"话术含薪资关键词「{kw}」（HRBP 禁止）", greet=text)
    # 空窗词
    for kw in _GAP_KEYWORDS:
        if kw in text:
            raise GreetValidationError(f"话术含空窗期关键词「{kw}」", greet=text)
    # 学历词：若 JD 要求该学历则放行（如 JD 要求大专，话术提大专无妨）
    jd_degree = (job.degree if job else "") or ""
    for kw in _DEGREE_KEYWORDS:
        if kw in text and kw not in jd_degree:
            raise GreetValidationError(
                f"话术含学历关键词「{kw}」（非 JD 要求时 HRBP 禁止主动提及）", greet=text
            )
    # ADR-0002 禁忌词
    for kw in _FORBIDDEN_KEYWORDS:
        if kw in text:
            raise GreetValidationError(
                f"话术含 ADR-0002 禁忌词「{kw}」（数据造假红线）", greet=text
            )


def _sender_get(sender_cfg: Any, key: str, default: Any) -> Any:
    """从 sender 配置读字段，兼容 dict / pydantic 模型 / None。"""
    if sender_cfg is None:
        return default
    if isinstance(sender_cfg, dict):
        return sender_cfg.get(key, default)
    # pydantic 模型 / dataclass / 普通对象
    return getattr(sender_cfg, key, default)


# ============================================================
# Sender 编排器（设计 §4.1 / §16.2）
# ============================================================
class Sender:
    """M3 发送编排：BrowserManager + WebGreeter + WebChatSender + 限流熔断。

    编排（设计 §13.4）：
    1. ``acquire`` 限流闸门
    2. ``ensure_browser`` + ``ensure_logged_in``
    3. ``WebGreeter.click_and_greet``（→ greeted）
    4. ``validate_greet_before_send``
    5. ``WebChatSender.send_full``（→ text_sent → image_sent）
    6. ``report_result``（成功/失败/风控）

    每步推进状态机（``db.transition``）。

    幂等：按 job.status 断点续传（greeted→只发话术+图；text_sent→只发图）。
    """

    def __init__(
        self,
        cfg: Any,
        conn: Any,
        browser_manager: BrowserManager,
        limiter: RealRateLimiter,
        *,
        greeter: WebGreeter | None = None,
        chat_sender: WebChatSender | None = None,
        logger_obj: Any = None,
        max_retries: int = 3,
    ) -> None:
        """初始化发送编排器。

        Args:
            cfg: AppConfig（读 sender 配置）。
            conn: SQLite 连接。
            browser_manager: BrowserManager 实例。
            limiter: RealRateLimiter 实例（dry-run 模式传 DryRunLimiter 也行）。
            greeter: WebGreeter（None 则用 browser_manager 自建）。
            chat_sender: WebChatSender（None 则用 browser_manager 自建）。
            logger_obj: loguru logger。
            max_retries: 最大重试次数（状态机 mark_failed 用）。
        """
        self.cfg = cfg
        self.conn = conn
        self.browser = browser_manager
        self.limiter = limiter
        self.log = logger_obj or logger
        self.max_retries = max_retries
        # 读 sender 配置（max_text_len / image_max_kb / image_width_px）。
        # sender 可能是 dict（config.yaml 原始加载）或 SenderCfg（pydantic 模型），
        # 统一用 _sender_get 兼容两种。
        sender_cfg = getattr(cfg, "sender", None)
        self.max_text_len = int(_sender_get(sender_cfg, "chat_text_max_len", 200))
        self.image_max_kb = int(_sender_get(sender_cfg, "image_max_kb", 1024))
        self.image_width_px = int(_sender_get(sender_cfg, "image_width_px", 1080))
        self.wait_relation_sec = int(_sender_get(sender_cfg, "wait_relation_sec", 8))
        self.send_image_resume = bool(_sender_get(sender_cfg, "send_image_resume", True))
        # 引擎选择（ADR-0003）：nodriver 绕过 zpAegis，drissionpage 仅 dry-run
        self.driver = str(_sender_get(sender_cfg, "driver", "drissionpage"))
        self.user_data_dir = str(_sender_get(sender_cfg, "user_data_dir", "~/.boss-auto-apply/chrome-profile"))
        self.headless = bool(_sender_get(sender_cfg, "headless", False))
        # 子通道
        self.greeter = greeter or WebGreeter(browser_manager, logger_obj=self.log)
        self.chat_sender = chat_sender or WebChatSender(
            browser_manager, logger_obj=self.log,
            max_text_len=self.max_text_len,
            image_max_kb=self.image_max_kb,
            image_width_px=self.image_width_px,
            driver=self.driver,
            user_data_dir=self.user_data_dir,
            headless=self.headless,
        )

    # ============================================================
    # 主接口
    # ============================================================
    def send_application(self, job: JobRow, *, dry_run: bool = False) -> ChatSendResult:
        """编排单个 job 的完整发送（点击沟通 → 发话术 → 发图片）。

        幂等：按 job.status 断点续传。
        - ``image_ready`` → 全流程（greet + text + image）
        - ``greeted`` → 跳过 greet，发话术 + 图片
        - ``text_sent`` → 跳过 greet + 话术，只发图片

        Args:
            job: 目标岗位（status 应为 image_ready/greeted/text_sent）。
            dry_run: True 则所有真发送操作只 log 不执行，状态 mock 推进。

        Returns:
            :class:`ChatSendResult`（含每步成功标志）。

        Raises:
            CircuitBreakerOpenError: 限流闸门阻断（调用方应停止整批）。
            BossAutoError: 各步骤业务异常（状态机标 failed/skipped）。
        """
        result = ChatSendResult(job_id=job.job_id)
        # 1. 限流闸门（dry-run 模式 limiter 是 DryRunLimiter，永远放行）
        # 分级处理（v2）：
        #   - wait_sec ≤ 180s（频次类：interval/burst）→ sleep 等够后重试一次
        #     二次仍拒 → raise（视为风控或异常）
        #   - wait_sec > 180s（熔断/风控/日上限用尽/已过活跃时段）→ 直接 raise
        #     由调用方停止整批（保留现状语义）
        # 这样流式 N 个新岗位之间不会因 interval 名额预扣被全部拦死。
        decision = self.limiter.acquire(AcquireContext(job_id=job.job_id, dry_run=dry_run))
        if not decision.allow:
            if dry_run:
                self.log.warning(f"job {job.job_id} 被限流：{decision.reason}（wait {decision.wait_sec:.0f}s）")
                raise CircuitBreakerOpenError(decision.reason, decision.wait_sec)
            if decision.wait_sec > 180:
                self.log.warning(
                    f"job {job.job_id} 被限流（>180s，整批停）：{decision.reason}（wait {decision.wait_sec:.0f}s）"
                )
                raise CircuitBreakerOpenError(decision.reason, decision.wait_sec)
            # 频次类拒绝：sleep 等够后重试一次（不再返回 False 跳过，避免"沉默丢岗"）
            self.log.info(
                f"job {job.job_id} 频次限流（{decision.reason}），sleep {decision.wait_sec:.0f}s 后重试 acquire"
            )
            time.sleep(decision.wait_sec)
            decision = self.limiter.acquire(AcquireContext(job_id=job.job_id, dry_run=dry_run))
            if not decision.allow:
                self.log.warning(
                    f"job {job.job_id} 重试后仍被限流：{decision.reason}（wait {decision.wait_sec:.0f}s）"
                )
                raise CircuitBreakerOpenError(decision.reason, decision.wait_sec)

        # 2. 浏览器 + 登录态（dry-run 也启动浏览器，验证环境；但登录态宽松）
        # ⚠️ v2 优化（profile 互斥）：若 job 已经是 greeted/text_sent（断点续传场景），
        # 且 chat 用 nodriver 引擎，跳过 DrissionPage ensure_browser——
        # 否则 DrissionPage Chrome 启动后 nodriver 又用同 profile 启动会冲突。
        # 只在 status=image_ready（需要 greet）时启动 DrissionPage。
        status = job.status
        needs_drissionpage = (status == "image_ready") or dry_run
        if needs_drissionpage:
            self.browser.ensure_browser()
            if not dry_run:
                self.browser.ensure_logged_in()

        # 3. 点击沟通（image_ready → greeted）
        if status == "image_ready":
            greet_res = self._step_greet(job, dry_run=dry_run)
            if not greet_res.relation_established and not dry_run:
                # 失败：风控 → report；其他 → 标 failed
                self.limiter.report_result(job.job_id, success=False,
                                           risk_signal=greet_res.risk_signal)
                self._on_greet_failure(job, greet_res)
                result.error = greet_res.error or "greet_failed"
                result.risk_signal = greet_res.risk_signal
                return result
            if not dry_run:
                db_mod.transition(self.conn, job.job_id, "greeted",
                                  set_fields={"greet_sent_at": db_mod.iso_now()})
                self.log.info(f"job {job.job_id} → greeted ✅")
            else:
                # dry-run 也推进状态机（mock），否则后续 text_sent 迁移会因 image_ready 越级失败
                db_mod.transition(self.conn, job.job_id, "greeted",
                                  set_fields={"greet_sent_at": db_mod.iso_now()})
            status = "greeted"

            # ⚠️ v2 关键修复（profile 互斥）：greet 成功后，若 chat 用 nodriver 引擎，
            # 必须先 quit DrissionPage Chrome 释放 profile 锁，否则 nodriver 启动 Chrome
            # 会因 SingletonLock 冲突而 30s 超时（"端口未就绪/profile 损坏"）。
            # ADR-0003 约束：nodriver 和 DrissionPage 不能同时用同一 user_data_dir。
            if not dry_run and self.driver == "nodriver":
                self.log.info(f"job {job.job_id} greet 完成，quit DrissionPage Chrome 释放 profile（nodriver 即将接管）")
                self.browser.quit()

        # 4. 话术校验
        greet_text = validate_greet_before_send(
            job.tailored_greet, job=job, max_len=self.max_text_len, logger_obj=self.log,
        )

        # 5. 发话术 + 图片（greeted/text_sent → ... → image_sent）
        if status in ("greeted", "text_sent"):
            chat_res = self._step_chat(job, greet_text, status, dry_run=dry_run)
            result.text_sent = chat_res.text_sent or status == "text_sent"
            result.image_sent = chat_res.image_sent
            result.text_confirmed = chat_res.text_confirmed
            result.image_confirmed = chat_res.image_confirmed
            result.image_path = chat_res.image_path
            result.error = chat_res.error
            result.risk_signal = chat_res.risk_signal
            result.elapsed_sec = chat_res.elapsed_sec

        # 6. 限流反馈
        success = result.image_sent or (dry_run and result.text_sent)
        risk = result.risk_signal
        if not dry_run:
            self.limiter.report_result(job.job_id, success=success, risk_signal=risk)
        # 空闲自动退出（释放浏览器资源）
        self.browser.maybe_quit_idle()
        return result

    # ============================================================
    # 子步骤
    # ============================================================
    def _step_greet(self, job: JobRow, *, dry_run: bool) -> GreetResult:
        """点击沟通（设计 §6）。"""
        try:
            return self.greeter.click_and_greet(job, dry_run=dry_run)
        except (CaptchaDetectedError, RateLimitedError) as e:
            # 风控信号转 GreetResult
            signal = "captcha" if isinstance(e, CaptchaDetectedError) else "rate_limited"
            self.log.error(f"greet 风控（job={job.job_id}）：{e}")
            return GreetResult(job_id=job.job_id, risk_signal=signal, error=str(e))
        except BossAutoError as e:
            self.log.error(f"greet 业务异常（job={job.job_id}）：{e}")
            return GreetResult(job_id=job.job_id, error=f"{type(e).__name__}: {e}")

    def _on_greet_failure(self, job: JobRow, greet_res: GreetResult) -> None:
        """greet 失败的状态机处理。

        - risk_signal → 硬熔断已由 report_result 触发；这里 mark_failed（留原态）
        - button_not_found → business 类（岗位下线/改版）→ mark_failed（business 会 skipped）
        - click_timeout → network 类 → mark_failed（留原态重试）
        """
        err = RuntimeError(greet_res.error or "greet_failed")
        if greet_res.risk_signal:
            # 风控：留原态（CircuitBreaker 已熔断，整批会停）
            db_mod.mark_failed(self.conn, job.job_id, err, max_retries=self.max_retries)
        else:
            db_mod.mark_failed(self.conn, job.job_id, err, max_retries=self.max_retries)

    def _step_chat(
        self,
        job: JobRow,
        greet_text: str,
        status: str,
        *,
        dry_run: bool,
    ) -> ChatSendResult:
        """发话术 + 图片（设计 §7）。按 status 断点续传。

        Args:
            job: 目标岗位。
            greet_text: 已校验话术。
            status: 当前状态（greeted/text_sent）。
            dry_run: dry-run 标志。
        """
        image_path = job.resume_image_path or ""
        if status == "text_sent":
            # 只发图片（话术已发，幂等跳过）
            self.log.info(f"job {job.job_id} 已 text_sent，断点续传只发图片")
            return self._send_image_only(job, image_path, dry_run=dry_run)
        # 全量发（话术 + 图片）
        if not image_path and not dry_run:
            self.log.error(f"job {job.job_id} 缺 resume_image_path，无法发图片")
            db_mod.mark_failed(self.conn, job.job_id,
                               RuntimeError("missing resume_image_path"),
                               max_retries=self.max_retries)
            return ChatSendResult(job_id=job.job_id, error="missing_image_path")
        chat_res = self.chat_sender.send_full(
            job, greet_text, image_path,
            dry_run=dry_run, wait_relation_sec=self.wait_relation_sec,
        )
        # 状态机推进
        if dry_run:
            # dry-run mock 推进（设计 §13.4 A6）
            if chat_res.text_sent:
                db_mod.transition(self.conn, job.job_id, "text_sent",
                                  set_fields={"text_sent_at": db_mod.iso_now()})
            if chat_res.image_sent:
                db_mod.transition(self.conn, job.job_id, "image_sent",
                                  set_fields={"image_sent_at": db_mod.iso_now()})
            return chat_res
        # 真发送：按部分成功推进
        if chat_res.text_sent:
            db_mod.transition(self.conn, job.job_id, "text_sent",
                              set_fields={"text_sent_at": db_mod.iso_now()})
            self.log.info(f"job {job.job_id} → text_sent ✅")
            if chat_res.image_sent:
                db_mod.transition(self.conn, job.job_id, "image_sent",
                                  set_fields={"image_sent_at": db_mod.iso_now()})
                self.log.info(f"job {job.job_id} → image_sent ✅ (终态)")
            else:
                # 话术成功图片失败：留 text_sent，标 failed 待重试图片
                db_mod.mark_failed(self.conn, job.job_id,
                                   RuntimeError(chat_res.error or "image_send_failed"),
                                   max_retries=self.max_retries)
        else:
            # 话术失败：留 greeted，标 failed 待重试
            db_mod.mark_failed(self.conn, job.job_id,
                               RuntimeError(chat_res.error or "text_send_failed"),
                               max_retries=self.max_retries)
        return chat_res

    def _send_image_only(self, job: JobRow, image_path: str, *, dry_run: bool) -> ChatSendResult:
        """断点续传：text_sent → image_sent（只发图片）。"""
        result = ChatSendResult(job_id=job.job_id, text_sent=True, text_confirmed=True)
        if not image_path and not dry_run:
            self.log.error(f"job {job.job_id} 缺 resume_image_path，无法续传发图片")
            db_mod.mark_failed(self.conn, job.job_id,
                               RuntimeError("missing resume_image_path"),
                               max_retries=self.max_retries)
            result.error = "missing_image_path"
            return result
        img_ok = self.chat_sender.send_image(job, image_path, dry_run=dry_run)
        result.image_sent = img_ok
        if dry_run:
            if img_ok:
                db_mod.transition(self.conn, job.job_id, "image_sent",
                                  set_fields={"image_sent_at": db_mod.iso_now()})
            return result
        if img_ok:
            db_mod.transition(self.conn, job.job_id, "image_sent",
                              set_fields={"image_sent_at": db_mod.iso_now()})
            self.log.info(f"job {job.job_id} → image_sent ✅ (续传完成)")
        else:
            db_mod.mark_failed(self.conn, job.job_id,
                               RuntimeError("image_send_failed (resume)"),
                               max_retries=self.max_retries)
            result.error = "image_send_failed"
        return result
