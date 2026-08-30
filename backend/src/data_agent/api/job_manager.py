from __future__ import annotations

import asyncio
import copy
import logging
import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from data_agent.agent.memory import record_clarification_choice
from data_agent.agent.planner import (
    collect_conditional_clarification,
    has_blocking_questions,
)
from data_agent.api.artifact_service import (
    materialize_job_inputs,
    persist_job_inputs,
    publish_job_artifacts,
)
from data_agent.api.job_queue import (
    QueueMessage,
    durable_queue_enabled,
    enqueue_message,
)
from data_agent.api.job_store import (
    STATUS_AWAITING_CONFIRMATION,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    admit_job_status,
    answered_session_slots,
    api_work_dir,
    append_session_turn,
    create_task_session,
    list_pending_queue_messages,
    read_job_status,
    read_plan_snapshot,
    read_plan_state,
    read_task_session,
    record_clarification_question,
    record_session_plan,
    transition_job_status,
    update_job_observability,
    update_task_session,
    write_job_status,
    write_plan_snapshot,
    write_plan_state,
)
from data_agent.api.quotas import configured_tenant_quota
from data_agent.observability import capture_llm_usage
from data_agent.services import (
    build_plan_snapshot,
    build_planning_response,
    execute_planned_job,
    hydrate_plan_snapshot,
    plan_input_paths,
    process_planned_job_with_reflection,
)
from data_agent.tenancy import tenant_from_config, tenant_scope
from data_agent.tools import OutputContractError
from data_agent.utils.errors import to_user_message

logger = logging.getLogger("data_agent.api")


def _max_workers() -> int:
    return max(1, int(os.environ.get("DATA_AGENT_API_MAX_WORKERS", "2")))


def _clarify_enabled() -> bool:
    """Whether jobs may pause to ask a conditional clarification question.

    On by default (P1): the gate asks one unresolved ``TaskSpec.missing_slots``
    question at a time. Goals with no missing slots still run straight through
    ("一句话直达"). Opt out with ``DATA_AGENT_CLARIFY_ENABLED=0``.
    """
    raw = os.environ.get("DATA_AGENT_CLARIFY_ENABLED", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _confirm_enabled() -> bool:
    """Whether every answer-mode job pauses before deterministic execution.

    High-risk capabilities always require confirmation. This flag extends the gate
    to all answer-mode tasks; discover/plan modes never execute data operations.
    """
    raw = os.environ.get("DATA_AGENT_CONFIRM_ENABLED", "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _job_mode(config: dict) -> str:
    overrides = config.get("job_overrides") if isinstance(config, dict) else {}
    if not isinstance(overrides, dict):
        overrides = {}
    return str(overrides.get("mode") or config.get("mode") or "answer")


def _failure_stage(stage: str, error: Exception) -> str:
    return "delivery" if isinstance(error, OutputContractError) else stage


def _fold_answers_into_config(config: dict, plan_state: dict, answers: list[dict]) -> dict:
    """Return a config whose goal is enriched with the user's clarification answers.

    Only the *answers* are appended. The question text is the agent's own wording, and
    folding it into the goal made the planner read its own words back as the user's
    intent: the question "请确认是否以客户编号作为关联键，将客户档案.客户名称补充到
    订单明细中？" names 客户档案 before 订单明细, so the base-table rule picked the
    archive, the join vanished, and the job died on "处理计划未覆盖用户要求的关联字段".
    The goal string is the record of what the user asked for; nothing this agent
    generates belongs in it.
    """
    base_goal = str(plan_state.get("goal") or _config_goal(config) or "")
    original_mode = str(plan_state.get("mode") or _job_mode(config))
    asked = {
        str(item.get("id")): item
        for item in plan_state.get("clarification_questions") or []
        if isinstance(item, dict)
    }
    lines = []
    for answer in answers:
        text = str(answer.get("answer", "")).strip()
        if text:
            lines.append(text)
            _remember_choice(asked.get(str(answer.get("question_id"))), text)
    enriched = copy.deepcopy(config) if isinstance(config, dict) else {}
    if lines:
        enriched_goal = base_goal + "\n\n[用户澄清]\n" + "\n".join(lines)
    else:
        enriched_goal = base_goal
    enriched["goal"] = enriched_goal
    enriched["mode"] = original_mode
    enriched.setdefault("job_overrides", {})
    if not isinstance(enriched["job_overrides"], dict):
        enriched["job_overrides"] = {}
    # Goal and mode are processing metadata, not JobConfig overrides. Preserve the
    # original mode so a clarified plan-only task still stops before execution.
    enriched["job_overrides"].pop("goal", None)
    enriched["job_overrides"].pop("mode", None)
    return enriched


def _config_goal(config: dict) -> str:
    overrides = config.get("job_overrides") if isinstance(config, dict) else {}
    if not isinstance(overrides, dict):
        overrides = {}
    return str(overrides.get("goal") or config.get("goal") or "")


class JobManager:
    """Runs cleaning jobs on a background thread pool so requests never block.

    The HTTP handler creates the job (status ``pending``), hands the saved upload
    paths to :meth:`submit`, and returns immediately with the job_id. The worker
    thread flips the job to ``running``, executes the deterministic/agent pipeline,
    and writes the terminal ``succeeded`` / ``failed`` status document. The console
    polls ``/status`` and sees real progress instead of a 404 during execution.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=_max_workers(),
            thread_name_prefix="data-agent-job",
        )
        self._cancelled: set[str] = set()
        self._futures: dict[str, Future] = {}
        self._guard = threading.Lock()

    def new_job_id(self) -> str:
        return uuid.uuid4().hex

    async def submit(self, job_id: str, saved_paths: list[Path], config: dict) -> None:
        """Register the job as pending and schedule background execution."""
        admit_job_status(
            job_id,
            STATUS_PENDING,
            config=config,
            max_active_jobs=configured_tenant_quota().max_active_jobs,
        )
        create_task_session(job_id, _config_goal(config))
        input_artifacts = await asyncio.to_thread(
            persist_job_inputs,
            job_id,
            saved_paths,
            tenant_id=tenant_from_config(config),
        )
        if durable_queue_enabled():
            message = _queue_message(
                "run",
                job_id,
                {
                    "saved_paths": [str(path) for path in saved_paths],
                    "input_artifacts": input_artifacts,
                    "config": config,
                },
            )
            write_job_status(
                job_id,
                STATUS_PENDING,
                config=config,
                payload={
                    "queue_message": message,
                    "input_artifacts": input_artifacts,
                },
            )
            await _enqueue_durable(message)
            return
        write_job_status(
            job_id,
            STATUS_PENDING,
            config=config,
            payload={"input_artifacts": input_artifacts},
        )
        self._schedule(job_id, self._run, job_id, saved_paths, config)

    async def cancel(self, job_id: str) -> bool:
        with self._guard:
            self._cancelled.add(job_id)
            future = self._futures.get(job_id)
        if future is not None:
            future.cancel()
        changed, _payload = transition_job_status(
            job_id,
            {
                STATUS_PENDING,
                STATUS_RUNNING,
                STATUS_NEEDS_CLARIFICATION,
                STATUS_AWAITING_CONFIRMATION,
            },
            STATUS_CANCELLED,
            error="任务已取消",
        )
        if changed:
            self._set_session_status(job_id, STATUS_CANCELLED)
        return changed

    def _is_cancelled(self, job_id: str) -> bool:
        if durable_queue_enabled():
            try:
                return read_job_status(job_id).get("status") == STATUS_CANCELLED
            except Exception:  # noqa: BLE001 - a missing status behaves as cancelled
                return True
        with self._guard:
            return job_id in self._cancelled

    def is_active(self, job_id: str) -> bool:
        if durable_queue_enabled():
            try:
                return read_job_status(job_id).get("status") in {
                    STATUS_PENDING,
                    STATUS_RUNNING,
                }
            except Exception:  # noqa: BLE001 - missing metadata is not active
                return False
        with self._guard:
            future = self._futures.get(job_id)
            return future is not None and not future.done()

    def _schedule(self, job_id: str, fn, *args) -> None:
        def observed_worker() -> None:
            tenant_id = str(read_job_status(job_id).get("tenant_id") or "default")
            with tenant_scope(tenant_id), capture_llm_usage() as usage:
                try:
                    fn(*args)
                finally:
                    try:
                        update_job_observability(job_id, llm_usage=list(usage))
                    except Exception as exc:  # noqa: BLE001 - observability is non-blocking
                        logger.warning(
                            "job %s: unable to persist observability: %s",
                            job_id,
                            exc,
                        )

        future = self._executor.submit(observed_worker)
        with self._guard:
            self._futures[job_id] = future
        future.add_done_callback(lambda done: self._forget_future(job_id, done))

    def execute_queue_message(self, message: QueueMessage) -> None:
        """Execute one ARQ message after claiming its durable status document."""

        job_id = str(message.get("job_id") or "")
        payload = message.get("payload") or {}
        if not self._claim_queue_message(message):
            return

        tenant_id = tenant_from_config(dict(payload.get("config") or {}))
        with tenant_scope(tenant_id), capture_llm_usage() as usage:
            try:
                action = message.get("action")
                if action == "run":
                    stored_inputs = payload.get("input_artifacts")
                    input_artifacts = (
                        list(stored_inputs) if isinstance(stored_inputs, list) else []
                    )
                    saved_paths = materialize_job_inputs(job_id, input_artifacts)
                    if not saved_paths:
                        saved_paths = [
                            Path(path) for path in payload.get("saved_paths", [])
                        ]
                    self._run(
                        job_id,
                        saved_paths,
                        dict(payload.get("config") or {}),
                        already_claimed=True,
                    )
                elif action == "resume":
                    self._resume(
                        job_id,
                        dict(payload.get("config") or {}),
                        list(payload.get("answers") or []),
                    )
                elif action == "confirm":
                    self._confirm(
                        job_id,
                        dict(payload.get("config") or {}),
                        str(payload.get("plan_id") or ""),
                        str(payload.get("plan_hash") or ""),
                    )
                else:
                    raise ValueError(f"unknown queue action: {action}")
            finally:
                try:
                    update_job_observability(job_id, llm_usage=list(usage))
                except Exception as exc:  # noqa: BLE001 - observability is non-blocking
                    logger.warning(
                        "job %s: unable to persist observability: %s",
                        job_id,
                        exc,
                    )

    def _claim_queue_message(self, message: QueueMessage) -> bool:
        job_id = str(message.get("job_id") or "")
        current = read_job_status(job_id)
        stored = current.get("queue_message") or {}
        if str(stored.get("dispatch_id") or "") != str(
            message.get("dispatch_id") or ""
        ):
            return False
        if current.get("status") == STATUS_RUNNING:
            # ARQ retries the same id after a worker loss. The stable dispatch id is
            # the lease identity, so the retry may resume; a different message may not.
            return True
        if current.get("status") != STATUS_PENDING:
            return False
        claimed, _payload = transition_job_status(
            job_id,
            STATUS_PENDING,
            STATUS_RUNNING,
            payload=current,
            config=(message.get("payload") or {}).get("config") or {},
        )
        return claimed

    def fail_queue_message(self, message: QueueMessage, error: Exception) -> bool:
        """Close a dispatch after ARQ has exhausted infrastructure retries."""

        job_id = str(message.get("job_id") or "")
        try:
            current = read_job_status(job_id)
        except Exception:  # noqa: BLE001 - there is no status left to close
            return False
        stored = current.get("queue_message") or {}
        if str(stored.get("dispatch_id") or "") != str(
            message.get("dispatch_id") or ""
        ):
            return False
        changed, _payload = transition_job_status(
            job_id,
            {STATUS_PENDING, STATUS_RUNNING},
            STATUS_FAILED,
            payload={**current, "failure_stage": "execution", "retryable": True},
            config=(message.get("payload") or {}).get("config") or {},
            error=to_user_message(error),
        )
        if changed:
            self._set_session_status(job_id, STATUS_FAILED)
        return changed

    def _forget_future(self, job_id: str, future: Future) -> None:
        with self._guard:
            if self._futures.get(job_id) is future:
                self._futures.pop(job_id, None)
            self._cancelled.discard(job_id)

    def _run(
        self,
        job_id: str,
        saved_paths: list[Path],
        config: dict,
        *,
        already_claimed: bool = False,
    ) -> None:
        job_dir = api_work_dir() / job_id
        if self._is_cancelled(job_id):
            return
        if not already_claimed:
            claimed, _payload = transition_job_status(
                job_id,
                STATUS_PENDING,
                STATUS_RUNNING,
                config=config,
            )
            if not claimed:
                return
        logger.info("job %s: running (%d file(s))", job_id, len(saved_paths))
        stage = "planning"
        try:
            # Every async task plans exactly once. The frozen snapshot is the resume /
            # confirmation anchor and the same in-memory PlanResult feeds execution.
            clarify_on = _clarify_enabled()
            mode = _job_mode(config)
            plan = plan_input_paths(saved_paths, work_dir=job_dir, config=config)
            confirm_on = (
                mode == "answer"
                and (_confirm_enabled() or plan.execution_plan.requires_confirmation)
            )
            snapshot = build_plan_snapshot(plan, saved_paths)
            write_plan_snapshot(job_id, snapshot.model_dump(mode="json"))
            self._record_session_plan(job_id, plan.goal_plan, snapshot.plan_hash)

            # P1 conditional clarification gate: ask only the first
            # unresolved TaskSpec slot; complete specs run straight through.
            if clarify_on and mode != "discover":
                questions = collect_conditional_clarification(plan.goal_plan)
                if has_blocking_questions(questions):
                    self._pause_for_clarification(
                        job_id,
                        saved_paths,
                        config,
                        plan,
                        questions,
                        snapshot.plan_hash,
                    )
                    return

            if mode in {"discover", "plan"}:
                response = build_planning_response(plan, saved_paths)
                if self._complete_success(job_id, response, config):
                    logger.info("job %s: %s mode completed without execution", job_id, mode)
                return

            # P0 pre-execution confirmation gate for answer-mode execution.
            if confirm_on:
                self._pause_for_confirmation(
                    job_id,
                    config,
                    plan,
                )
                return

            stage = "execution"
            response = process_planned_job_with_reflection(
                plan,
                saved_paths,
                work_dir=job_dir,
            )
            if self._complete_success(job_id, response, config):
                logger.info("job %s: succeeded", job_id)
        except Exception as exc:  # noqa: BLE001 - top-level worker guard
            message = to_user_message(exc)
            logger.exception("job %s: failed: %s", job_id, exc)
            self._complete_failure(
                job_id,
                config,
                message,
                stage=_failure_stage(stage, exc),
            )

    def _complete_success(self, job_id: str, response: dict, config: dict) -> bool:
        if self._is_cancelled(job_id):
            transition_job_status(
                job_id,
                STATUS_RUNNING,
                STATUS_CANCELLED,
                config=config,
                error="任务已取消",
            )
            return False
        artifact_manifest = publish_job_artifacts(job_id)
        if artifact_manifest:
            response = {**response, "artifact_manifest": artifact_manifest}
        changed, _payload = transition_job_status(
            job_id,
            STATUS_RUNNING,
            STATUS_SUCCEEDED,
            payload=response,
            config=config,
        )
        if changed:
            self._set_session_status(job_id, STATUS_SUCCEEDED)
        return changed

    def _complete_failure(
        self,
        job_id: str,
        config: dict,
        message: str,
        *,
        stage: str,
    ) -> None:
        if self._is_cancelled(job_id):
            transition_job_status(
                job_id,
                STATUS_RUNNING,
                STATUS_CANCELLED,
                config=config,
                error="任务已取消",
            )
            return
        changed, _payload = transition_job_status(
            job_id,
            STATUS_RUNNING,
            STATUS_FAILED,
            payload={"failure_stage": stage},
            config=config,
            error=message,
        )
        if changed:
            self._set_session_status(job_id, STATUS_FAILED)

    def _record_session_plan(
        self,
        job_id: str,
        goal_plan: dict,
        plan_hash: str,
    ) -> None:
        try:
            record_session_plan(
                job_id,
                task_spec=dict(goal_plan.get("task_spec") or {}),
                plan_hash=plan_hash,
            )
        except Exception as exc:  # noqa: BLE001 - legacy jobs may have no session file
            logger.warning("job %s: unable to record task session plan: %s", job_id, exc)

    def _set_session_status(self, job_id: str, status: str) -> None:
        try:
            update_task_session(job_id, status=status)
        except Exception as exc:  # noqa: BLE001 - session audit must not break execution
            logger.warning("job %s: unable to update task session: %s", job_id, exc)

    def _append_session_turn(
        self,
        job_id: str,
        *,
        role: str,
        kind: str,
        content: str,
        structured_payload: Optional[dict] = None,
        status: Optional[str] = None,
    ) -> None:
        try:
            append_session_turn(
                job_id,
                role=role,
                kind=kind,
                content=content,
                structured_payload=structured_payload,
                status=status,
            )
        except Exception as exc:  # noqa: BLE001 - session audit must not break execution
            logger.warning("job %s: unable to append task session turn: %s", job_id, exc)

    def _pause_for_clarification(
        self,
        job_id: str,
        saved_paths: list[Path],
        config: dict,
        plan,
        questions: list[dict],
        plan_hash: str,
    ) -> None:
        """Persist the resume anchor and mark the job as awaiting user answers.

        ``questions`` contains the next unresolved TaskSpec slot. It is stored so
        resume can fold the answer back into the goal and create the next frozen
        plan version.
        """
        write_plan_state(
            job_id,
            {
                "input_paths": [str(path) for path in saved_paths],
                "goal": plan.options.goal,
                "mode": plan.options.mode,
                "clarification_questions": questions,
                "plan_hash": plan_hash,
            },
        )
        changed, _payload = transition_job_status(
            job_id,
            STATUS_RUNNING,
            STATUS_NEEDS_CLARIFICATION,
            config=config,
            payload={
                "goal_plan": plan.goal_plan,
                "clarification_questions": questions,
            },
        )
        if changed:
            first_question = questions[0] if questions else {}
            record_clarification_question(
                job_id,
                question=str(first_question.get("question") or "需要补充任务信息。"),
                structured_payload={
                    "questions": questions,
                    "plan_hash": plan_hash,
                },
            )
            logger.info(
                "job %s: awaiting clarification (%d question(s))",
                job_id,
                len(questions),
            )

    async def resume(self, job_id: str, config: dict, answers: list[dict]) -> bool:
        """Resume a paused job in the background, folding user answers into the goal."""
        durable = durable_queue_enabled()
        prior = read_job_status(job_id)
        message = _queue_message(
            "resume",
            job_id,
            {"config": config, "answers": answers},
        )
        claimed, _payload = transition_job_status(
            job_id,
            STATUS_NEEDS_CLARIFICATION,
            STATUS_PENDING if durable else STATUS_RUNNING,
            config=config,
            payload={**prior, "queue_message": message} if durable else prior,
        )
        if not claimed:
            return False
        self._append_session_turn(
            job_id,
            role="user",
            kind="clarification_answer",
            content="\n".join(
                f"{item.get('question_id', '')}: {item.get('answer', '')}"
                for item in answers
            ),
            structured_payload={"answers": answers},
        )
        if durable:
            await _enqueue_durable(message)
        else:
            self._schedule(job_id, self._resume, job_id, config, answers)
        return True

    def _resume(self, job_id: str, config: dict, answers: list[dict]) -> None:
        job_dir = api_work_dir() / job_id
        if self._is_cancelled(job_id):
            return
        logger.info("job %s: resuming after clarification", job_id)
        stage = "planning"
        try:
            plan_state = read_plan_state(job_id)
            input_paths = [Path(p) for p in plan_state.get("input_paths", [])]
            materialized = materialize_job_inputs(job_id)
            if materialized:
                input_paths = materialized
            if not input_paths:
                raise ValueError("无法恢复任务：缺少原始输入文件信息。")
            frozen = read_plan_snapshot(job_id)
            base_config = frozen.get("processing_options") or config
            enriched_config = _fold_answers_into_config(base_config, plan_state, answers)
            # Clarification intentionally creates one new plan version from the
            # enriched goal; that version is then frozen and executed without another
            # planner call.
            plan = plan_input_paths(input_paths, work_dir=job_dir, config=enriched_config)
            mode = _job_mode(enriched_config)
            snapshot = build_plan_snapshot(plan, input_paths)
            write_plan_snapshot(job_id, snapshot.model_dump(mode="json"))
            self._record_session_plan(job_id, plan.goal_plan, snapshot.plan_hash)
            questions = collect_conditional_clarification(
                plan.goal_plan,
                answered_slot_ids=answered_session_slots(job_id),
            )
            if _clarify_enabled() and has_blocking_questions(questions):
                session = read_task_session(job_id)
                if session["clarification_round"] < session["max_clarification_rounds"]:
                    self._pause_for_clarification(
                        job_id,
                        input_paths,
                        enriched_config,
                        plan,
                        questions,
                        snapshot.plan_hash,
                    )
                    return
                if mode == "answer":
                    self._pause_for_confirmation(job_id, enriched_config, plan)
                    return
            if mode in {"discover", "plan"}:
                response = build_planning_response(plan, input_paths)
                if self._complete_success(job_id, response, enriched_config):
                    logger.info(
                        "job %s: %s mode completed after clarification",
                        job_id,
                        mode,
                    )
                return
            if _confirm_enabled() or plan.execution_plan.requires_confirmation:
                self._pause_for_confirmation(job_id, enriched_config, plan)
                return
            stage = "execution"
            response = process_planned_job_with_reflection(
                plan,
                input_paths,
                work_dir=job_dir,
            )
            if self._complete_success(job_id, response, enriched_config):
                logger.info("job %s: succeeded after clarification", job_id)
        except Exception as exc:  # noqa: BLE001 - top-level worker guard
            message = to_user_message(exc)
            logger.exception("job %s: resume failed: %s", job_id, exc)
            self._complete_failure(
                job_id,
                config,
                message,
                stage=_failure_stage(stage, exc),
            )

    def _pause_for_confirmation(
        self,
        job_id: str,
        config: dict,
        plan,
    ) -> None:
        """Persist the resume anchor and mark the job as awaiting plan confirmation.

        Surfaces the already-computed goal understanding so the console can show the
        source / confidence / assumptions and warn on a generic-fallback plan before
        the user approves execution. No execution has run yet.
        """
        snapshot = read_plan_snapshot(job_id)
        changed, _payload = transition_job_status(
            job_id,
            STATUS_RUNNING,
            STATUS_AWAITING_CONFIRMATION,
            config=config,
            payload={
                "goal_plan": plan.goal_plan,
                "clarification_questions": plan.clarification_questions,
                # Store the identity beside the lifecycle state so PostgreSQL can
                # compare status + plan atomically under one row lock.
                "plan_id": str(snapshot.get("plan_id") or ""),
                "plan_hash": str(snapshot.get("plan_hash") or ""),
            },
        )
        if changed:
            self._append_session_turn(
                job_id,
                role="assistant",
                kind="plan_confirmation",
                content="处理计划已生成，等待确认后执行。",
                structured_payload={
                    "plan_hash": str(snapshot.get("plan_hash") or "")
                },
                status=STATUS_AWAITING_CONFIRMATION,
            )
            logger.info("job %s: awaiting plan confirmation", job_id)

    async def confirm(
        self,
        job_id: str,
        config: dict,
        *,
        plan_id: str,
        plan_hash: str,
    ) -> bool:
        """Approve exactly the paused snapshot the caller reviewed."""
        durable = durable_queue_enabled()
        prior = read_job_status(job_id)
        message = _queue_message(
            "confirm",
            job_id,
            {
                "config": config,
                "plan_id": plan_id,
                "plan_hash": plan_hash,
            },
        )
        claimed, _payload = transition_job_status(
            job_id,
            STATUS_AWAITING_CONFIRMATION,
            STATUS_PENDING if durable else STATUS_RUNNING,
            expected_plan_id=plan_id,
            expected_plan_hash=plan_hash,
            config=config,
            payload={**prior, "queue_message": message} if durable else prior,
        )
        if not claimed:
            return False
        self._append_session_turn(
            job_id,
            role="user",
            kind="plan_confirmation",
            content="确认当前处理计划并开始执行。",
            structured_payload={
                "plan_id": plan_id,
                "plan_hash": plan_hash,
            },
        )
        if durable:
            await _enqueue_durable(message)
        else:
            self._schedule(job_id, self._confirm, job_id, config, plan_id, plan_hash)
        return True

    def _confirm(
        self,
        job_id: str,
        config: dict,
        plan_id: str,
        plan_hash: str,
    ) -> None:
        job_dir = api_work_dir() / job_id
        if self._is_cancelled(job_id):
            return
        logger.info("job %s: resuming after plan confirmation", job_id)
        stage = "planning"
        try:
            frozen = read_plan_snapshot(job_id)
            if not frozen:
                raise ValueError("无法恢复任务：缺少已确认的计划快照。")
            frozen_hash = str(frozen.get("plan_hash") or "")
            frozen_id = str(frozen.get("plan_id") or frozen_hash[:32])
            if frozen_id != plan_id or frozen_hash != plan_hash:
                raise ValueError("无法执行任务：处理计划已在确认后发生变化。")
            materialized = materialize_job_inputs(job_id)
            input_paths = materialized or [
                Path(path) for path in frozen.get("input_paths", [])
            ]
            if materialized:
                frozen = {**frozen, "input_paths": [str(path) for path in materialized]}
            plan = hydrate_plan_snapshot(frozen, job_dir)
            stage = "execution"
            response = execute_planned_job(plan, input_paths)
            status_config = plan.options.model_dump(mode="json")
            if self._complete_success(job_id, response, status_config):
                logger.info("job %s: succeeded after plan confirmation", job_id)
        except Exception as exc:  # noqa: BLE001 - top-level worker guard
            message = to_user_message(exc)
            logger.exception("job %s: confirm failed: %s", job_id, exc)
            self._complete_failure(
                job_id,
                config,
                message,
                stage=_failure_stage(stage, exc),
            )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


_manager: Optional[JobManager] = None
_manager_guard = threading.Lock()


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        with _manager_guard:
            if _manager is None:
                _manager = JobManager()
    return _manager


def _queue_message(action: str, job_id: str, payload: dict) -> QueueMessage:
    if action not in {"run", "resume", "confirm"}:
        raise ValueError(f"unsupported queue action: {action}")
    return QueueMessage(
        dispatch_id=f"{job_id}-{uuid.uuid4().hex}",
        action=action,
        job_id=job_id,
        payload=copy.deepcopy(payload),
    )


async def _enqueue_durable(message: QueueMessage) -> bool:
    try:
        await enqueue_message(message)
    except Exception as exc:  # noqa: BLE001 - the durable outbox retries later
        logger.error(
            "job %s: unable to enqueue dispatch %s: %s",
            message.get("job_id"),
            message.get("dispatch_id"),
            exc,
        )
        return False
    return True


async def redispatch_pending_messages() -> int:
    """Replay the PostgreSQL outbox after API/Redis interruptions."""

    if not durable_queue_enabled():
        return 0
    dispatched = 0
    for raw in list_pending_queue_messages():
        message = QueueMessage(
            dispatch_id=str(raw.get("dispatch_id") or ""),
            action=str(raw.get("action") or ""),  # type: ignore[typeddict-item]
            job_id=str(raw.get("job_id") or ""),
            payload=dict(raw.get("payload") or {}),
        )
        if not message["dispatch_id"] or not message["job_id"]:
            continue
        dispatched += int(await _enqueue_durable(message))
    return dispatched


def _remember_choice(question: dict | None, answer: str) -> None:
    """Record a picked option so the next question can lead with it.

    Only options this run actually offered are remembered — a typed-in answer is free
    text, and memory here deliberately holds nothing but the names it proposed itself.
    Never raises: a preference that failed to save is not worth failing a job over.
    """

    if not isinstance(question, dict):
        return
    options = [str(item) for item in question.get("options") or []]
    if answer not in options:
        return
    kind = str(question.get("id") or "").split("_")[0] or str(question.get("kind") or "")
    record_clarification_choice(str(question.get("id") or kind), answer)
