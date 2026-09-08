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
