"""预置题库与 pick log 数据面(B5,#127):面试官挑题的持久层。

- interview_qbank:调查/兜底产出的题集(候选+周期+版本,JSONB 信封)
- qbank_pick_log:面试官实际勾选(候选/场次/面试官/题)——反哺出题的证据,
  候选人永不可见(#135 用户故事 19)
- 表自举 L-1 先例;调用方管理事务
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


_ensured = False


def ensure_qbank_tables(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """进程级一次性自举(并发 DDL 会死锁,同 evaluation.py)。"""
    global _ensured
    if _ensured:
        return
    _ensure_qbank_tables_locked(conn)
    _ensured = True


def _ensure_qbank_tables_locked(conn: psycopg.Connection[dict[str, Any]]) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_qbank (
            id            bigserial   NOT NULL PRIMARY KEY,
            resume_id     bigint      NOT NULL,
            cycle_id      int         NOT NULL,
            qbank_version int         NOT NULL,
            source        text        NOT NULL,
            envelope      jsonb       NOT NULL,
            prompt_version text       NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (resume_id, cycle_id, qbank_version)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qbank_resume "
        "ON interview_qbank (resume_id, cycle_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qbank_pick_log (
            id                   bigserial   NOT NULL PRIMARY KEY,
            resume_id            bigint      NOT NULL,
            cycle_id             int         NOT NULL,
            schedule_id          bigint,
            interviewer_user_id  int         NOT NULL,
            question_ref         jsonb       NOT NULL,
            picked_at            timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qbank_pick_resume "
        "ON qbank_pick_log (resume_id, cycle_id)"
    )


def save_qbank(
    *,
    resume_id: int,
    cycle_id: int,
    source: str,
    envelope: dict[str, Any],
    prompt_version: str,
) -> int:
    """落题集:版本递增旧版保留(与 scorecard 同语义);返回 qbank_version。"""
    # MAX+1 并发窗口:撞唯一键重读重试(同 evaluation.save_scorecard 先例)
    for attempt in range(2):
        try:
            with _conn() as conn:
                ensure_qbank_tables(conn)
                row = conn.execute(
                    "SELECT COALESCE(MAX(qbank_version), 0) AS v "
                    "FROM interview_qbank WHERE resume_id = %s AND cycle_id = %s",
                    (resume_id, cycle_id),
                ).fetchone()
                version = (row["v"] if row else 0) + 1
                conn.execute(
                    """
                    INSERT INTO interview_qbank
                        (resume_id, cycle_id, qbank_version, source, envelope, prompt_version)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resume_id,
                        cycle_id,
                        version,
                        source,
                        json.dumps(envelope, ensure_ascii=False),
                        prompt_version,
                    ),
                )
            return version
        except psycopg.errors.UniqueViolation:
            if attempt:
                raise
    raise RuntimeError("unreachable")


def latest_qbank(resume_id: int, cycle_id: int) -> dict[str, Any] | None:
    """最新题集(含全部版本号元数据);无则 None。"""
    with _conn() as conn:
        ensure_qbank_tables(conn)
        row = conn.execute(
            "SELECT resume_id, cycle_id, qbank_version, source, envelope, "
            "prompt_version, created_at "
            "FROM interview_qbank WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY qbank_version DESC LIMIT 1",
            (resume_id, cycle_id),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if isinstance(result["envelope"], str):
        result["envelope"] = json.loads(result["envelope"])
    return result


def record_pick(
    *,
    resume_id: int,
    cycle_id: int,
    interviewer_user_id: int,
    question_ref: dict[str, Any],
    schedule_id: int | None = None,
) -> int:
    """记一道勾选题(题引用:anchor/question/evidence_path)。返回 pick id。"""
    with _conn() as conn:
        ensure_qbank_tables(conn)
        row = conn.execute(
            """
            INSERT INTO qbank_pick_log
                (resume_id, cycle_id, schedule_id, interviewer_user_id, question_ref)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (
                resume_id,
                cycle_id,
                schedule_id,
                interviewer_user_id,
                json.dumps(question_ref, ensure_ascii=False),
            ),
        ).fetchone()
    return int(row["id"]) if row else 0


def list_picks(resume_id: int, cycle_id: int) -> list[dict[str, Any]]:
    """某候选的勾选记录(时间倒序)。"""
    with _conn() as conn:
        ensure_qbank_tables(conn)
        rows = conn.execute(
            "SELECT id, resume_id, cycle_id, schedule_id, interviewer_user_id, "
            "question_ref, picked_at "
            "FROM qbank_pick_log WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY picked_at DESC",
            (resume_id, cycle_id),
        ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        if isinstance(item["question_ref"], str):
            item["question_ref"] = json.loads(item["question_ref"])
        result.append(item)
    return result
