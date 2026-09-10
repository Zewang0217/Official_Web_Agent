"""evaluation_job 真库集成测试(闸门2/3):幂等、attempts 上限、恢复。

需要真 PostgreSQL(POSTGRES_URL 指向可写库);无 PG 时整文件 skip——
CI 由 .github/workflows/ci.yml 的 postgres service 提供,本地开发者
用 docker compose 起 PG 后自动生效(不设 PG 则跳过,不阻塞本地跑)。
"""

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from official_agent.state import evaluation as ev_store  # noqa: E402

PG_URL = os.environ.get("POSTGRES_URL", "")


def _pg_available() -> bool:
    if not PG_URL:
        return False
    try:
        with psycopg.connect(PG_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — 连不上即 skip
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)"
)


@pytest.fixture
def cycle_id() -> int:
    """每个测试用独立 cycle_id,避免与其他测试/残留数据互相干扰。"""
    return uuid.uuid4().int % 2_000_000_000 + 1


@pytest.fixture(autouse=True)
def _point_settings_at_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_url", PG_URL)


def _cleanup(cycle_id: int) -> None:
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DELETE FROM evaluation_job WHERE cycle_id = %s", (cycle_id,))


def test_create_jobs_is_idempotent_for_active(cycle_id: int) -> None:
    """闸门2:同 (resume, cycle) 重复提交只保留一个活跃 job,返回同一 job_id。"""
    _cleanup(cycle_id)
    try:
        first = ev_store.create_jobs([(9001, 501)], cycle_id)
        second = ev_store.create_jobs([(9001, 501)], cycle_id)
        assert first == second, "活跃 job 应复用同一 job_id"
        with psycopg.connect(PG_URL) as conn:
            rows = conn.execute(
                "SELECT count(*) FROM evaluation_job "
                "WHERE cycle_id = %s AND status IN ('pending','running')",
                (cycle_id,),
            ).fetchone()
        assert rows[0] == 1, "活跃 job 只能有一条"
    finally:
        _cleanup(cycle_id)


def test_terminal_job_allows_new_version(cycle_id: int) -> None:
    """闸门2:终态(succeeded)不拦复评——重建新 job。"""
    _cleanup(cycle_id)
    try:
        first = ev_store.create_jobs([(9002, 502)], cycle_id)
        ev_store.mark_job(first[0], "succeeded", card_version=1)
        second = ev_store.create_jobs([(9002, 502)], cycle_id)
        assert second != first, "终态复评应新建 job(新版本卡)"
    finally:
        _cleanup(cycle_id)


def test_requeue_skips_attempts_exhausted(cycle_id: int) -> None:
    """闸门3:attempts 达上限的失败 job 不再自动重排。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9003, 503)], cycle_id)[0]
        for _ in range(ev_store._MAX_ATTEMPTS):
            ev_store.mark_job(job_id, "running")
        ev_store.mark_job(job_id, "failed", error="boom")
        assert ev_store.requeue_failed(cycle_id) == [], "超上限不应重排"
    finally:
        _cleanup(cycle_id)


def test_requeue_stale_recovers_within_cap(cycle_id: int) -> None:
    """闸门3:未超上限的僵 job 被 requeue_stale 捡回(用 0 分钟守卫即刻生效)。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9004, 504)], cycle_id)[0]
        ev_store.mark_job(job_id, "running")
        recovered = ev_store.requeue_stale(cycle_id, older_than_minutes=0)
        assert job_id in recovered
        assert ev_store.get_job(job_id)["status"] == "pending"
    finally:
        _cleanup(cycle_id)


def test_integrity_dedupes_existing_active(cycle_id: int) -> None:
    """闸门2 加固:旧库已有重复活跃 job 时,integrity 去重后能建唯一索引。"""
    _cleanup(cycle_id)
    try:
        # 绕过 create_jobs 的幂等,直接插两条活跃 job 模拟旧库脏数据
        with psycopg.connect(PG_URL) as conn:
            ev_store.ensure_evaluation_job_table(conn)
            conn.execute(
                "DROP INDEX IF EXISTS uq_eval_job_active_resume"
            )
            for _ in range(2):
                conn.execute(
                    "INSERT INTO evaluation_job (resume_id, user_id, cycle_id) "
                    "VALUES (%s, %s, %s)",
                    (9005, 505, cycle_id),
                )
        with psycopg.connect(PG_URL) as conn:
            ev_store.ensure_evaluation_job_integrity(conn)
            active = conn.execute(
                "SELECT count(*) FROM evaluation_job "
                "WHERE cycle_id = %s AND status IN ('pending','running')",
                (cycle_id,),
            ).fetchone()
        assert active[0] == 1, "去重后只保留一条活跃"
    finally:
        _cleanup(cycle_id)


def test_qbank_status_persisted(cycle_id: int) -> None:
    """闸门6:qbank_status 落库,管理面可见"有评分无题库"。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9006, 506)], cycle_id)[0]
        ev_store.mark_job(job_id, "running")
        ev_store.mark_job(job_id, "succeeded", card_version=1, qbank_status="failed")
        row = ev_store.get_job(job_id)
        assert row["status"] == "succeeded"
        assert row["qbank_status"] == "failed"
        assert ev_store.list_jobs(cycle_id)[0]["qbank_status"] == "failed"
    finally:
        _cleanup(cycle_id)
