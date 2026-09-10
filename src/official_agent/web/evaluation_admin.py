"""B 模块管理 API(B2):/admin/evaluation——初筛触发与 job 面。

权限:resume:audit(#135 用户故事 1:评审触发初筛;与 kb:manage/
agent:monitor 一样走 JWT permission_codes 自校)。
审计双录:触发写 agent_audit_log(生成完成审计在 runner)。
"""

import asyncio
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from official_agent.evaluation import runner as eval_runner
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.state import audit, evaluation
from official_agent.state import qbank as qbank_store
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


# ── B5 预置题库(/admin/evaluation/qbank):面试官挑题面(#127/#128) ──


def _require_any(*codes: str):
    """任一权限码通过即放行(qbank 面:面试官 interview:evaluate / 评审 resume:audit)。"""

    async def _dep(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ):
        identity, _ = await _authenticate(request, authorization)
        owned = identity.get("permission_codes") or []
        if not any(c in owned for c in codes):
            raise HTTPException(
                status_code=403, detail=f"需要 {' 或 '.join(codes)} 权限"
            )
        return identity

    return _dep


class PickBody(BaseModel):
    resume_id: int = Field(ge=1)
    cycle_id: int = Field(ge=1)
    schedule_id: int | None = None
    questions: list[dict[str, Any]] = Field(min_length=1, max_length=20)


@router.get("/admin/evaluation/qbank")
async def get_qbank(
    identity: Annotated[
        ResolvedIdentity, Depends(_require_any("interview:evaluate", "resume:audit"))
    ],
    resume_id: int,
    cycle_id: int,
) -> dict[str, Any]:
    """某候选最新预置题库(面试官面试前预查/打分工作台抽屉)。"""
    try:
        row = await asyncio.to_thread(qbank_store.latest_qbank, resume_id, cycle_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="查询题库失败,请稍后重试") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="该候选暂无预置题库")
    envelope = row.get("envelope") or {}
    row = dict(row)
    # #153:v2 题组的可挑题扁平视图(UI 挑题不感知组内嵌套)
    row["pickable"] = qbank_store.flatten_v2_pickable(envelope)
    return row


@router.post("/admin/evaluation/qbank/pick", status_code=201)
async def pick_questions(
    body: PickBody,
    identity: Annotated[
        ResolvedIdentity, Depends(_require_any("interview:evaluate", "resume:audit"))
    ],
) -> dict[str, Any]:
    """记录面试官实际勾选的题(pick log;候选人永不可见)。"""
    picked = 0
    try:
        for q in body.questions:
            await asyncio.to_thread(
                qbank_store.record_pick,
                resume_id=body.resume_id,
                cycle_id=body.cycle_id,
                interviewer_user_id=int(identity.get("user_id") or 0),
                question_ref=q,
                schedule_id=body.schedule_id,
            )
            picked += 1
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="记录勾选失败,请稍后重试") from exc
    return {"picked": picked}


@router.get("/admin/evaluation/qbank/picks")
async def list_question_picks(
    _: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    resume_id: int,
    cycle_id: int,
) -> dict[str, Any]:
    """勾选记录(resume:audit 管理面;反哺出题覆盖率分析)。"""
    try:
        picks = await asyncio.to_thread(
            qbank_store.list_picks, resume_id, cycle_id
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="查询勾选失败,请稍后重试") from exc
    return {"items": picks, "total": len(picks)}


# ── B6 评审队列(#124/#128):0 分队列/采纳/改分/驳回 ──────────────────────


class AdoptBody(BaseModel):
    """采纳(AI 参考分或人工改分,以评审本人一票 upsert 后端多人打分)。

    score 必填:采纳 AI 参考总分传回其 total,改分则传人工分——Agent 不
    帮任何人决定终分。
    """

    resume_id: int = Field(ge=1)
    cycle_id: int = Field(ge=1)
    score: int = Field(ge=0, le=100)
    version: int | None = None  # 缺省采纳最新版


class RejectBody(BaseModel):
    resume_id: int = Field(ge=1)
    cycle_id: int = Field(ge=1)
    version: int | None = None


@router.get("/admin/evaluation/queue")
async def evaluation_queue(
    _: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    cycle_id: int,
    queue: str = "all",
) -> dict[str, Any]:
    """评审队列:queue=zero → 初筛不过(hard_zero)子队列;all → 全部评分卡。"""
    if queue not in ("zero", "all"):
        raise HTTPException(status_code=400, detail="queue 只支持 zero/all")

    try:
        items = await asyncio.to_thread(evaluation.list_review_queue, cycle_id, queue)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="查询队列失败,请稍后重试") from exc
    return {"items": items, "total": len(items), "queue": queue}


@router.get("/admin/evaluation/scorecard")
async def get_scorecard_for_review(
    identity: Annotated[
        ResolvedIdentity,
        Depends(_require_any("interview:evaluate", "resume:audit")),
    ],
    resume_id: int,
    cycle_id: int,
) -> dict[str, Any]:
    """单候选评分卡:面试官(interview:evaluate)场景内只读维卡,#128。"""
    row = await asyncio.to_thread(evaluation.latest_scorecard, resume_id, cycle_id)
    if row is None:
        raise HTTPException(status_code=404, detail="该候选暂无评分卡")
    return row


@router.post("/admin/evaluation/adopt")
async def adopt_scorecard(
    body: AdoptBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """采纳 = 评审本人以自身身份向后端投一票(#124:AI 不占 scorer)。

    成功后才把 Agent 卡置 adopted;后端失败时卡保持 draft 可重试。
    """
    from official_agent.tools.client import BackendError
    from official_agent.tools.readonly import get_backend_client as _gbc

    token = (authorization or "").removeprefix("Bearer ").strip()
    client = await _gbc()
    try:
        await client.put_as_user(
            f"/api/resumes/{body.resume_id}/score",
            json={"score": body.score},
            user_token=token,
        )
    except BackendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # ADR-0006(评审 P1):采纳是写操作,审计必须权威——先落审计再产生任何
    # 副作用(投票/卡态迁移);审计失败 → 503,采纳不执行(可重试)。
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=f"eval:{body.cycle_id}:{secrets.token_hex(4)}",
            acting_user_id=int(identity.get("user_id") or 0),
            channel="evaluation",
            agent="evaluation-reviewer",
            action={
                "op": "adopt_vote_cast",
                "resume_id": body.resume_id,
                "cycle_id": body.cycle_id,
                "score": body.score,
            },
            decision=f"u{identity.get('user_id')}:adopt",
            result="评审采纳受理(先审计后投票,终分=多人平均)",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="审计服务不可用,采纳未执行,请稍后重试"
        ) from exc

    version = body.version
    if version is None:
        row = await asyncio.to_thread(
            evaluation.latest_scorecard, body.resume_id, body.cycle_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="该候选暂无评分卡,无法采纳")
        version = int(row["card_version"])
    # 卡态迁移同样先审计(ADR-0006):审计落了才置 adopted;失败 → 503,卡保持
    # draft 可重试(docstring 语义不变)。
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=f"eval:{body.cycle_id}:{secrets.token_hex(4)}",
            acting_user_id=int(identity.get("user_id") or 0),
            channel="evaluation",
            agent="evaluation-reviewer",
            action={
                "op": "adopt_scorecard",
                "resume_id": body.resume_id,
                "cycle_id": body.cycle_id,
                "version": version,
                "score": body.score,
            },
            decision=f"u{identity.get('user_id')}:adopt",
            result=f"评审采纳为一票(score={body.score});终分=多人平均",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="审计服务不可用,采纳未执行,请稍后重试"
        ) from exc
    changed = await asyncio.to_thread(
        evaluation.set_scorecard_status,
        body.resume_id,
        body.cycle_id,
        version,
        "adopted",
    )
    if not changed:
        raise HTTPException(status_code=404, detail="评分卡不存在")
    return {
        "resume_id": body.resume_id,
        "cycle_id": body.cycle_id,
        "version": version,
        "status": "adopted",
        "score": body.score,
    }


@router.post("/admin/evaluation/reject")
async def reject_scorecard(
    body: RejectBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
) -> dict[str, Any]:
    """驳回:卡置 rejected(AI 参考分不采纳);可复评(run 生成新版本)。"""
    version = body.version
    if version is None:
        row = await asyncio.to_thread(
            evaluation.latest_scorecard, body.resume_id, body.cycle_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="该候选暂无评分卡,无法驳回")
        version = int(row["card_version"])
    # ADR-0006:驳回是写操作,先落审计(失败 → 503,不迁移)。
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=f"eval:{body.cycle_id}:{secrets.token_hex(4)}",
            acting_user_id=int(identity.get("user_id") or 0),
            channel="evaluation",
            agent="evaluation-reviewer",
            action={
                "op": "reject_scorecard",
                "resume_id": body.resume_id,
                "cycle_id": body.cycle_id,
                "version": version,
            },
            decision=f"u{identity.get('user_id')}:reject",
            result="评审驳回 AI 参考分(可复评)",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="审计服务不可用,驳回未执行,请稍后重试"
        ) from exc
    changed = await asyncio.to_thread(
        evaluation.set_scorecard_status,
        body.resume_id,
        body.cycle_id,
        version,
        "rejected",
    )
    if not changed:
        raise HTTPException(status_code=404, detail="评分卡不存在")
    return {
        "resume_id": body.resume_id,
        "cycle_id": body.cycle_id,
        "version": version,
        "status": "rejected",
    }
