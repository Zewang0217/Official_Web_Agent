"""evaluation_scorecard 数据面(B1):AI 参考分卡,版本递增旧版保留(#124)。

- AI 只作参考:卡存 Agent PG,**不写** resume_score_entry / resume_score
  (后端多人打分是真人票,#216 对齐)
- 重跑版本递增:UNIQUE (resume_id, cycle_id, card_version),旧版可回看
- 卡态:draft(默认)→ adopted/rejected(B6 评审队列迁移)

表自举 L-1 先例:DDL 进仓库,幂等;调用方管理事务(threads.py 风格)。
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings

_STATUS_DRAFT = "draft"


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def ensure_evaluation_tables(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """幂等建 evaluation_scorecard(L-1:新环境自举; lifespan 调用,B2 接线)。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_scorecard (
            id             bigserial   NOT NULL PRIMARY KEY,
            resume_id      bigint      NOT NULL,
            cycle_id       int         NOT NULL,
            card_version   int         NOT NULL,
            status         text        NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft', 'adopted', 'rejected')),
            hard_zero      boolean     NOT NULL DEFAULT false,
            total          real,
            card           jsonb       NOT NULL,
            prompt_version text        NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (resume_id, cycle_id, card_version)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_scorecard_resume "
        "ON evaluation_scorecard (resume_id, cycle_id)"
    )


def save_scorecard(
    card: dict[str, Any],
    *,
    resume_id: int,
    cycle_id: int,
    prompt_version: str,
) -> int:
    """落卡:版本 = 该 (resume, cycle) 现存最大版本+1(重跑递增,旧版保留)。

    hard_zero/total 从卡内冗余提列,供队列过滤(0 分队列)与列表免解 JSONB。
    返回本次 card_version。
    """
    # MAX+1 读改写有并发窗口(同简历并发重触发,B2 评审 P2):撞唯一键重读重试
    for attempt in range(2):
        try:
            with _conn() as conn:
                ensure_evaluation_tables(conn)
                row = conn.execute(
                    "SELECT COALESCE(MAX(card_version), 0) AS v "
                    "FROM evaluation_scorecard WHERE resume_id = %s AND cycle_id = %s",
                    (resume_id, cycle_id),
                ).fetchone()
                version = (row["v"] if row else 0) + 1
                conn.execute(
                    """
                    INSERT INTO evaluation_scorecard
                        (resume_id, cycle_id, card_version, status, hard_zero, total,
                         card, prompt_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resume_id,
                        cycle_id,
                        version,
                        _STATUS_DRAFT,
                        bool(card.get("hard_zero", False)),
                        card.get("total"),
                        # 卡 JSONB(ensure_ascii=False,评审面板直读中文)
                        json.dumps(card, ensure_ascii=False),
                        prompt_version,
                    ),
                )
            return version
        except psycopg.errors.UniqueViolation:
            if attempt:
                raise
    raise RuntimeError("unreachable")


def latest_scorecard(resume_id: int, cycle_id: int) -> dict[str, Any] | None:
    """最新一版的完整卡;无则 None。"""
    with _conn() as conn:
        ensure_evaluation_tables(conn)
        row = conn.execute(
            "SELECT card, card_version, status, hard_zero, total, prompt_version, created_at "
            "FROM evaluation_scorecard "
            "WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY card_version DESC LIMIT 1",
            (resume_id, cycle_id),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if isinstance(result["card"], str):
        result["card"] = json.loads(result["card"])
    return result


def list_scorecards(resume_id: int, cycle_id: int) -> list[dict[str, Any]]:
    """全部版本投影(不含 card JSONB,详情走 latest/get)。"""
    with _conn() as conn:
        ensure_evaluation_tables(conn)
        rows = conn.execute(
            "SELECT card_version, status, hard_zero, total, prompt_version, created_at "
            "FROM evaluation_scorecard WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY card_version DESC",
            (resume_id, cycle_id),
        ).fetchall()
    return [dict(r) for r in rows]


def set_scorecard_status(resume_id: int, cycle_id: int, version: int, status: str) -> bool:
    """卡态迁移(draft→adopted/rejected;B6 评审采纳/驳回写这里)。"""
    if status not in ("draft", "adopted", "rejected"):
        raise ValueError(f"非法卡态:{status!r}")
    with _conn() as conn:
        ensure_evaluation_tables(conn)
        cur = conn.execute(
            "UPDATE evaluation_scorecard SET status = %s "
            "WHERE resume_id = %s AND cycle_id = %s AND card_version = %s",
            (status, resume_id, cycle_id, version),
        )
        return cur.rowcount > 0


# ── B2 执行组织(#126):job 状态表 + 进程内 runner 的持久态 ────────────────

def ensure_evaluation_job_table(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """幂等建 evaluation_job;与 scorecard 同库同自举纪律。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_job (
            job_id      bigserial   NOT NULL PRIMARY KEY,
            resume_id   bigint      NOT NULL,
            user_id     bigint      NOT NULL,
            cycle_id    int         NOT NULL,
            status      text        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
            attempts    int         NOT NULL DEFAULT 0,
            error       text,
            card_version int,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_job_cycle "
        "ON evaluation_job (cycle_id, status)"
    )


def create_jobs(items: list[tuple[int, int]], cycle_id: int) -> list[int]:
    """每份简历一行 pending job。items = [(resume_id, user_id), ...]。"""
    ids: list[int] = []
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        for resume_id, user_id in items:
            row = conn.execute(
                """
                INSERT INTO evaluation_job (resume_id, user_id, cycle_id)
                VALUES (%s, %s, %s) RETURNING job_id
                """,
                (resume_id, user_id, cycle_id),
            ).fetchone()
            if row:
                ids.append(int(row["job_id"]))
    return ids


def mark_job(
    job_id: int,
    status: str,
    *,
    error: str | None = None,
    card_version: int | None = None,
) -> bool:
    """状态迁移(pending→running→succeeded/failed;failed 可重试回 pending)。"""
    if status not in ("pending", "running", "succeeded", "failed"):
        raise ValueError(f"非法 job 状态:{status!r}")
    # attempts 语义=实际执行次数:只在进入 running 时累加(B2 评审 P2)
    bump = ", attempts = attempts + 1" if status == "running" else ""
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        cur = conn.execute(
            f"""
            UPDATE evaluation_job
            SET status = %s, error = %s, card_version = %s,
                updated_at = now(){bump}
            WHERE job_id = %s
            """,
            (status, error, card_version, job_id),
        )
        return cur.rowcount > 0


def get_job(job_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        row = conn.execute(
            "SELECT job_id, resume_id, user_id, cycle_id, status, attempts, error, "
            "card_version, created_at, updated_at "
            "FROM evaluation_job WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def list_jobs(cycle_id: int, *, status: str | None = None) -> list[dict[str, Any]]:
    """按周期查 job(0 分队列在 B6 按 scorecard.hard_zero 过滤,这里看执行面)。"""
    where = "cycle_id = %s"
    params: list[Any] = [cycle_id]
    if status:
        where += " AND status = %s"
        params.append(status)
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        rows = conn.execute(
            "SELECT job_id, resume_id, user_id, cycle_id, status, attempts, error, "
            "card_version, created_at, updated_at "
            f"FROM evaluation_job WHERE {where} ORDER BY job_id DESC LIMIT 200",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def requeue_failed(cycle_id: int) -> list[int]:
    """失败 job 重回 pending(手动重试入口)。"""
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        rows = conn.execute(
            "UPDATE evaluation_job SET status = 'pending', error = NULL, updated_at = now() "
            "WHERE cycle_id = %s AND status = 'failed' RETURNING job_id",
            (cycle_id,),
        ).fetchall()
    return [int(r["job_id"]) for r in rows]


def requeue_stale(cycle_id: int, *, older_than_minutes: int = 10) -> list[int]:
    """残留恢复(进程重启后 pending/running 僵 job):超过时限才回 pending。

    时限防误伤:刚提交的 pending/running 有活任务在跑,重入队会双跑。
    """
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        rows = conn.execute(
            """
            UPDATE evaluation_job SET status = 'pending', error = NULL, updated_at = now()
            WHERE cycle_id = %s AND status IN ('failed', 'pending', 'running')
              AND updated_at < now() - (%s || ' minutes')::interval
            RETURNING job_id
            """,
            (cycle_id, str(older_than_minutes)),
        ).fetchall()
    return [int(r["job_id"]) for r in rows]

def list_review_queue(cycle_id: int, queue: str = "all") -> list[dict[str, Any]]:
    """评审队列投影(#128):每简历最新卡 + 关联 user_id(勾选重评需要,#154)。

    queue=zero → 仅初筛不过(hard_zero)子队列;all → 全部。
    """
    with _conn() as conn:
        ensure_evaluation_tables(conn)
        outer = "WHERE hard_zero = TRUE" if queue == "zero" else ""
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT DISTINCT ON (resume_id)
                    s.resume_id, s.card_version, s.status, s.hard_zero, s.total,
                    s.prompt_version, s.created_at,
                    (SELECT j.user_id FROM evaluation_job j
                      WHERE j.resume_id = s.resume_id AND j.cycle_id = %s
                      ORDER BY j.job_id DESC LIMIT 1) AS user_id
                FROM evaluation_scorecard s WHERE s.cycle_id = %s
                ORDER BY resume_id, card_version DESC
            ) latest {outer}
            ORDER BY resume_id
            """,
            (cycle_id, cycle_id),
        ).fetchall()
        return [dict(r) for r in rows]
