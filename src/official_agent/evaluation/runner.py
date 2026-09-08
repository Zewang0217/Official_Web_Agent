"""B2 执行组织(#126):进程内 asyncio task runner + job 状态表。

- 触发:管理员手动(单/批量),POST /admin/evaluation/run → submit()
- 每 job 一个 asyncio task(信号量限并发,LLM 慢操作);状态全在
  evaluation_job 表,进程重启后 running/pending 残留由 requeue_failed
  手动恢复(B2 不做自动拾取——0 分队列/评审队列 UI 在 B6)
- AI 0 分 = 初筛不过特殊标记:卡内 hard_zero 落列,不自动拒(#135)
- 失败可重试:mark failed + requeue;审计走 agent_audit_log(#124 双录)
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import dataclass

from official_agent.evaluation.graph import _prompt_version, run_evaluation
from official_agent.evaluation.scoring import FieldText
from official_agent.state import audit, evaluation
from official_agent.tools.readonly import get_backend_client

_MAX_CONCURRENCY = 4


@dataclass(frozen=True)
class TriggerItem:
    resume_id: int
    user_id: int


async def fetch_scoring_fields(user_id: int, cycle_id: int) -> tuple[int, list[FieldText]]:
    """服务账号取简历详情,映射为打分维度(textarea 型字段)。

    对应 GET /api/resumes/admin/{userId}/{cycleId}(resume:view 权限走
    服务账号,与 B1 的 PII 纪律一致:脱敏由后端返回层+字段面决定,这里
    只取 textarea 型主观题)。返回 (resume_id, fields)。
    """
    client = await get_backend_client()
    data = await client.get(f"/api/resumes/admin/{user_id}/{cycle_id}")
    resume_id = int(data.get("resumeId") or 0)
    if resume_id <= 0:
        # 缺 resumeId 静默落 0 会产生查不到的幽灵卡(B2 评审 P2)
        raise RuntimeError("后端响应缺 resumeId,拒绝评分")
    fields = [
        FieldText(
            field_key=str(f.get("fieldKey") or ""),
            title=str(f.get("fieldLabel") or ""),
            value=str(f.get("fieldValue") or ""),
            placeholder=str(f.get("placeholder") or ""),
        )
        for f in data.get("simpleFields") or []
        if f.get("fieldType") == "textarea"
    ]
    return resume_id, fields


class EvaluationRunner:
    """进程内单例:提交/并发控制/重试。状态真源在 evaluation_job 表。"""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        # asyncio 只持任务弱引用:不保存会被 GC,job 静默卡死(B2 评审 P1)
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def submit(
        self, cycle_id: int, items: list[TriggerItem], *, trigger_user_id: int
    ) -> list[int]:
        if not items:
            return []
        job_ids = await asyncio.to_thread(
            evaluation.create_jobs,
            [(i.resume_id, i.user_id) for i in items],
            cycle_id,
        )
        # 先派发后审计:审计失败不得让已建的 job 永远 pending(B2 E2E 实测)
        for job_id in job_ids:
            self._spawn(self._run_job(job_id, cycle_id, trigger_user_id=trigger_user_id))
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                audit.write_audit,
                thread_id=f"eval:{cycle_id}:{secrets.token_hex(4)}",
                acting_user_id=trigger_user_id,
                channel="evaluation",
                agent="evaluation-runner",
                action={
                    "op": "run_initial_screening",
                    "cycle_id": cycle_id,
                    "resume_ids": [i.resume_id for i in items],
                },
                decision=f"u{trigger_user_id}:run",
                result=f"提交 {len(job_ids)} 个初筛 job",
            )
        return job_ids

    async def _run_job(self, job_id: int, cycle_id: int, *, trigger_user_id: int) -> None:
        job = await asyncio.to_thread(evaluation.get_job, job_id)
        if job is None:
            return
        async with self._sem:
            await asyncio.to_thread(evaluation.mark_job, job_id, "running")
            card: dict | None = None
            try:
                resume_id, fields = await fetch_scoring_fields(job["user_id"], cycle_id)
                card = await run_evaluation(
                    [
                        {
                            "field_key": f.field_key,
                            "title": f.title,
                            "value": f.value,
                            "placeholder": f.placeholder,
                        }
                        for f in fields
                    ],
                    resume_id=resume_id,
                    cycle_id=cycle_id,
                )
                version = await asyncio.to_thread(
                    evaluation.save_scorecard,
                    card,
                    resume_id=resume_id,
                    cycle_id=cycle_id,
                    prompt_version=_prompt_version(),
                )
                await asyncio.to_thread(
                    evaluation.mark_job,
                    job_id,
                    "succeeded",
                    card_version=version,
                )
            except Exception as exc:  # noqa: BLE001 — job 失败落表,可重试
                await asyncio.to_thread(
                    evaluation.mark_job,
                    job_id,
                    "failed",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
                return
            # 完成审计在保护段外:审计失败不得把已 succeeded 的 job 翻成 failed
            try:
                await asyncio.to_thread(
                    audit.write_audit,
                    thread_id=f"eval:{cycle_id}:{secrets.token_hex(4)}",
                    acting_user_id=trigger_user_id,  # 谁触发谁进审计(ADR-0006)
                    channel="evaluation",
                    agent="evaluation-runner",
                    action={
                        "op": "scorecard_generated",
                        "resume_id": resume_id,
                        "cycle_id": cycle_id,
                        "candidate_user_id": job["user_id"],
                        "hard_zero": bool(card.get("hard_zero")) if card else None,
                    },
                    decision="system:auto",
                    result=(
                        f"卡 v{version},总分 {card.get('total')}"
                        "(AI 参考分,未写 resume_score)"
                        if card
                        else "生成完成"
                    ),
                )
            except Exception:  # noqa: BLE001 — 审计失败只记日志,不影响 job 态
                import logging

                logging.getLogger(__name__).warning(
                    "完成审计写入失败(job=%s)", job_id, exc_info=True
                )

    async def retry_failed(self, cycle_id: int) -> list[int]:
        """失败 job 全部重回 pending 并重新派发;返回重派 job_ids。"""
        job_ids = await asyncio.to_thread(evaluation.requeue_failed, cycle_id)
        for job_id in job_ids:
            self._spawn(self._run_job(job_id, cycle_id, trigger_user_id=0))
        return job_ids


    async def retry_stale(self, cycle_id: int) -> list[int]:
        """残留恢复:failed + 超时限的 pending/running 全部重回 pending 并派发。"""
        job_ids = await asyncio.to_thread(evaluation.requeue_stale, cycle_id)
        for job_id in job_ids:
            self._spawn(self._run_job(job_id, cycle_id, trigger_user_id=0))
        return job_ids


_runner: EvaluationRunner | None = None


def get_runner() -> EvaluationRunner:
    """进程内单例(测试可 set_runner 注入替身)。"""
    global _runner
    if _runner is None:
        _runner = EvaluationRunner()
    return _runner


def set_runner(runner: EvaluationRunner | None) -> None:
    global _runner
    _runner = runner
