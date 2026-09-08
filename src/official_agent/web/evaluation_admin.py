"""B 模块管理 API(B2):/admin/evaluation——初筛触发与 job 面。

权限:resume:audit(#135 用户故事 1:评审触发初筛;与 kb:manage/
agent:monitor 一样走 JWT permission_codes 自校)。
审计双录:触发写 agent_audit_log(生成完成审计在 runner)。
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from official_agent.evaluation import runner as eval_runner
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.state import evaluation
from official_agent.web.routes import _authenticate

router = APIRouter()


async def _require_resume_audit(
    request: Request, authorization: Annotated[str | None, Header()] = None
):
    """初筛管理认证:JWT → resolve → permission_codes 含 resume:audit。"""
    identity, _ = await _authenticate(request, authorization)
    codes = identity.get("permission_codes") or []
    if "resume:audit" not in codes:
        raise HTTPException(status_code=403, detail="需要 resume:audit 权限")
    return identity


class RunBody(BaseModel):
    """触发初筛:单份或批量。items 元素含 resume_id/user_id(取简历走后者)。"""

    cycle_id: int = Field(ge=1)
    items: list[eval_runner.TriggerItem] = Field(min_length=1, max_length=200)


@router.post("/admin/evaluation/run", status_code=202)
async def run_evaluation_jobs(
    body: RunBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
) -> dict[str, Any]:
    """触发初筛(异步执行):返回 job_ids;结果落 evaluation_scorecard。"""
    try:
        job_ids = await eval_runner.get_runner().submit(
            body.cycle_id,
            [eval_runner.TriggerItem(i.resume_id, i.user_id) for i in body.items],
            trigger_user_id=int(identity.get("user_id") or 0),
        )
    except Exception as exc:  # noqa: BLE001 — 统一 500 固定文案
        raise HTTPException(status_code=500, detail="提交初筛任务失败,请稍后重试") from exc
    return {"job_ids": job_ids, "submitted": len(job_ids)}


@router.get("/admin/evaluation/jobs")
async def list_evaluation_jobs(
    _: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    cycle_id: int,
    status: str | None = None,
) -> dict[str, Any]:
    """job 执行面列表(0 分队列在 B6 按 scorecard.hard_zero 过滤呈现)。"""
    try:
        jobs = await asyncio.to_thread(evaluation.list_jobs, cycle_id, status=status)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="查询 job 失败,请稍后重试") from exc
    return {"items": jobs, "total": len(jobs)}


@router.post("/admin/evaluation/jobs/retry")
async def retry_failed_jobs(
    identity: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    cycle_id: int,
    include_stale: bool = False,
) -> dict[str, Any]:
    """失败 job 批量重试;include_stale=true 时连进程重启残留一起恢复。"""
    try:
        if include_stale:
            job_ids = await eval_runner.get_runner().retry_stale(cycle_id)
        else:
            job_ids = await eval_runner.get_runner().retry_failed(cycle_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="重试失败,请稍后重试") from exc
    return {"retried": job_ids}
