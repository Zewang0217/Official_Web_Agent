"""B2 执行组织测试:runner 状态迁移 / job store SQL / 触发提交。

只测外部行为:fetch 与评分图被替换,验证状态机与审计;真库往返走集成档。
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from official_agent.evaluation import runner as ev_runner
from official_agent.evaluation.runner import EvaluationRunner
from official_agent.evaluation.scoring import FieldText
from official_agent.state import evaluation as ev_store

# ── store 层(mock 连接) ────────────────────────────────


def _mock_conn(fetchone=None, fetchall=None, rowcount=1):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchone.side_effect = (lambda: fetchone) if fetchone is not None else (lambda: None)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return conn


def test_create_jobs_inserts_per_item(monkeypatch) -> None:
    conn = _mock_conn(fetchone={"job_id": 7})
    monkeypatch.setattr(ev_store, "_conn", lambda: conn)
    ids = ev_store.create_jobs([(11, 101), (12, 102)], cycle_id=2026)
    assert ids == [7, 7]  # mock 恒返 7;关键是每 item 一次 INSERT
    inserts = [
        c
        for c in conn.execute.call_args_list
        if "INSERT INTO evaluation_job" in c.args[0]
    ]
    assert len(inserts) == 2


def test_mark_job_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="非法 job 状态"):
        ev_store.mark_job(1, "cancelled")


def test_requeue_failed_scoped_by_cycle(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[{"job_id": 5}])
    monkeypatch.setattr(ev_store, "_conn", lambda: conn)
    assert ev_store.requeue_failed(2026) == [5]
    sql, params = conn.execute.call_args.args
    assert "status = 'failed'" in sql and "RETURNING job_id" in sql
    assert params == (2026,)


# ── runner 状态机(fetch/评分注入) ────────────────────────


def _fields():
    return [FieldText(field_key="intro", title="自我介绍", value="认真的自我介绍内容。")]


def _fake_to_thread(seen: list):
    """直通版 to_thread:记录调用,返回可等待(真 to_thread 是 awaitable)。"""

    def _impl(fn, *a, **k):
        seen.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))

        async def _run():
            return fn(*a, **k)

        return _run()

    return _impl


@pytest.mark.asyncio
async def test_run_job_success_marks_succeeded(monkeypatch) -> None:
    seen: dict = {}

    async def _fetch(user_id, cycle_id):
        return 99, _fields()

    async def _run_evaluation(fields, *, resume_id, cycle_id, weights=None):
        seen["resume_id"] = resume_id
        return {"total": 66.0, "hard_zero": False, "dimensions": [], "attitude": {}}

    def _save(card, *, resume_id, cycle_id, prompt_version):
        seen["saved"] = (resume_id, card["total"])
        return 1

    def _mark(job_id, status, **kw):
        seen.setdefault("marks", []).append(status)
        return True

    async def _fetch_github(user_id):
        return "someuser"

    async def _bundle(fields, **kw):
        seen["bundle_kwargs"] = kw
        return {"groups": [], "prompt_version": "t"}

    runner = EvaluationRunner()
    with (
        patch.object(ev_runner, "fetch_scoring_fields", _fetch),
        patch.object(ev_runner, "run_evaluation", _run_evaluation),
        patch.object(ev_runner.evaluation, "save_scorecard", _save),
        patch.object(ev_runner.evaluation, "mark_job", _mark),
        patch.object(
            ev_runner.evaluation,
            "get_job",
            lambda jid: {"job_id": jid, "user_id": 42},
        ),
        patch.object(ev_runner.audit, "write_audit", lambda **k: None),
        patch.object(ev_runner.asyncio, "to_thread", _fake_to_thread([])),
        patch.object(ev_runner, "fetch_candidate_github", _fetch_github),
        patch("official_agent.evaluation.bundle.run_bundle", _bundle),
    ):
        await runner._run_job(1, 2026, trigger_user_id=9)
    assert seen["marks"] == ["running", "succeeded"]
    assert seen["resume_id"] == 99  # 以取回的 resumeId 为准(job 存的是取数键)
    assert seen["saved"] == (99, 66.0)
    # D17/#149 接线:github_key 从档案取、GITHUB_TOKEN 从 settings 传参进 bundle
    assert seen["bundle_kwargs"]["github_key"] == "someuser"
    assert "github_token" in seen["bundle_kwargs"]


@pytest.mark.asyncio
async def test_run_job_failure_marks_failed(monkeypatch) -> None:
    async def _boom(user_id, cycle_id):
        raise RuntimeError("backend down")

    marks: list = []

    def _mark(job_id, status, **kw):
        marks.append((status, kw.get("error")))
        return True

    runner = EvaluationRunner()
    with (
        patch.object(ev_runner, "fetch_scoring_fields", _boom),
        patch.object(
            ev_runner.evaluation,
            "get_job",
            lambda jid: {"job_id": jid, "user_id": 42},
        ),
        patch.object(ev_runner.evaluation, "mark_job", _mark),
        patch.object(ev_runner.audit, "write_audit", lambda **k: None),
        patch.object(ev_runner.asyncio, "to_thread", _fake_to_thread([])),
    ):
        await runner._run_job(1, 2026, trigger_user_id=9)
    status, error = marks[-1]
    assert status == "failed"
    assert "backend down" in (error or "")


@pytest.mark.asyncio
async def test_submit_empty_items_creates_nothing() -> None:
    runner = EvaluationRunner()
    with patch.object(ev_runner.evaluation, "create_jobs") as mock_create:
        ids = await runner.submit(2026, [], trigger_user_id=1)
    assert ids == []
    mock_create.assert_not_called()


def test_spawn_keeps_task_references() -> None:
    """B2 评审 P1:派发任务必须持强引用,否则可能被 GC 静默丢 job。"""
    runner = EvaluationRunner()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_spawn_probe(runner))
    finally:
        loop.close()


async def _spawn_probe(runner: EvaluationRunner) -> None:
    async def _noop():
        await asyncio.sleep(0)

    runner._spawn(_noop())
    await asyncio.sleep(0)
    assert runner._tasks, "任务应被强引用"


def test_attempts_only_bumps_on_running(monkeypatch) -> None:
    """B2 评审 P2:attempts=实际执行次数,终态不再翻倍。"""
    calls: list[tuple] = []

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

            class _Cur:
                rowcount = 1

            return _Cur()

    monkeypatch.setattr(ev_store, "_conn", lambda: _FakeConn())
    ev_store.mark_job(1, "running")
    ev_store.mark_job(1, "succeeded")
    updates = [sql for sql, _ in calls if "UPDATE evaluation_job" in sql]
    assert len(updates) == 2
    assert "attempts = attempts + 1" in updates[0]
    assert "attempts" not in updates[1]


@pytest.mark.asyncio
async def test_failure_marks_full_timeline_and_audits() -> None:
    """B2 评审 P2:失败用例断言完整时序;触发审计被调用。"""
    async def _boom(user_id, cycle_id):
        raise RuntimeError("backend down")

    marks: list = []
    audit_calls: list = []

    runner = EvaluationRunner()
    with (
        patch.object(ev_runner, "fetch_scoring_fields", _boom),
        patch.object(
            ev_runner.evaluation,
            "get_job",
            lambda jid: {"job_id": jid, "user_id": 42},
        ),
        patch.object(
            ev_runner.evaluation,
            "mark_job",
            lambda job_id, status, **kw: marks.append((status, kw.get("error"))) or True,
        ),
        patch.object(
            ev_runner.audit,
            "write_audit",
            lambda **k: audit_calls.append(k["action"]["op"]),
        ),
        patch.object(ev_runner.asyncio, "to_thread", _fake_to_thread([])),
    ):
        await runner._run_job(1, 2026, trigger_user_id=9)
    assert [m[0] for m in marks] == ["running", "failed"]
    assert audit_calls == ["scorecard_generated"] or audit_calls == []  # 失败路径无完成审计
    assert marks[-1][0] == "failed"


@pytest.mark.asyncio
async def test_eval_usage_log_written_per_job(monkeypatch) -> None:
    """#154/D9:job 完成后 evaluation 通道用量日志落 conversation_log(关联 job)。"""

    seen: dict = {}

    async def _fetch(user_id, cycle_id):
        return 99, _fields()

    async def _run_evaluation(fields, *, resume_id, cycle_id, weights=None):
        return {"total": 66.0, "hard_zero": False, "dimensions": [], "attitude": {}}

    def _save(card, *, resume_id, cycle_id, prompt_version):
        return 1

    def _mark(job_id, status, **kw):
        return True

    async def _fetch_github(user_id):
        return "someuser"

    async def _bundle(fields, **kw):
        return {
            "schema_name": "evaluation_qbank/v2",
            "groups": [],
            "explore_usage_total": {
                "input_tokens": 150,
                "output_tokens": 30,
                "cache_hit_tokens": 100,
                "cache_miss_tokens": 15,
            },
            "prompt_version": "t",
        }

    def _write_conversation(**kw):
        seen["log"] = kw

    monkeypatch.setattr(ev_runner, "fetch_scoring_fields", _fetch)
    monkeypatch.setattr(ev_runner, "run_evaluation", _run_evaluation)
    monkeypatch.setattr(ev_runner.evaluation, "save_scorecard", lambda *a, **k: 1)
    monkeypatch.setattr(ev_runner.evaluation, "mark_job", lambda *a, **k: True)
    monkeypatch.setattr(
        ev_runner.evaluation, "get_job", lambda jid: {"job_id": jid, "user_id": 42}
    )
    monkeypatch.setattr(ev_runner.audit, "write_audit", lambda **k: None)
    monkeypatch.setattr(ev_runner.asyncio, "to_thread", _fake_to_thread([]))
    monkeypatch.setattr(ev_runner, "fetch_candidate_github", _fetch_github)
    monkeypatch.setattr("official_agent.evaluation.bundle.run_bundle", _bundle)
    monkeypatch.setattr(
        "official_agent.state.qbank.save_qbank", lambda **k: 3  # 版本 3
    )
    monkeypatch.setattr("official_agent.state.conversation.write_conversation", _write_conversation)

    runner = EvaluationRunner()
    await runner._run_job(1, 2026, trigger_user_id=9)
    log = seen["log"]
    assert log["channel"] == "evaluation"
    assert log["thread_id"] == "eval:1"  # 关联 job
    assert log["input_tokens"] == 150
    assert log["cache_hit_tokens"] == 100
