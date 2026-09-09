"""B5 qbank 存储 + 挑题路由测试(mock 连接/TestClient 权限闸)。"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from official_agent.state import qbank
from official_agent.web.app import create_app

# ── store(mock 连接) ────────────────────────────────────


def _mock_conn(fetchone=None, fetchall=None, rowcount=1):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchone.side_effect = (lambda: fetchone) if fetchone is not None else (lambda: None)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return conn


def test_save_qbank_version_increments(monkeypatch) -> None:
    conn = _mock_conn(fetchone={"v": 1})
    monkeypatch.setattr(qbank, "_conn", lambda: conn)
    version = qbank.save_qbank(
        resume_id=9,
        cycle_id=2026,
        source="repo_deep_dive",
        envelope={"schema_name": "evaluation_qbank/v2", "questions": []},
        prompt_version="evaluation_investigate/v1",
    )
    assert version == 2
    insert = next(
        c for c in conn.execute.call_args_list if "INSERT INTO interview_qbank" in c.args[0]
    )
    assert insert.args[1][2] == 2


def test_latest_qbank_deserializes_envelope(monkeypatch) -> None:
    row = {
        "resume_id": 9,
        "cycle_id": 2026,
        "qbank_version": 1,
        "source": "guided",
        "envelope": '{"questions": []}',
        "prompt_version": "v1",
        "created_at": None,
    }
    conn = _mock_conn(fetchone=row)
    monkeypatch.setattr(qbank, "_conn", lambda: conn)
    result = qbank.latest_qbank(9, 2026)
    assert result is not None
    assert result["envelope"] == {"questions": []}


def test_record_pick_and_list(monkeypatch) -> None:
    question_ref = '{"anchor": "architecture", "question": "架构?"}'
    conn = _mock_conn(
        fetchone={"id": 3},
        fetchall=[{"id": 3, "question_ref": question_ref}],
    )
    monkeypatch.setattr(qbank, "_conn", lambda: conn)
    pid = qbank.record_pick(
        resume_id=9,
        cycle_id=2026,
        interviewer_user_id=5,
        question_ref={"anchor": "architecture", "question": "架构?"},
        schedule_id=None,
    )
    assert pid == 3
    picks = qbank.list_picks(9, 2026)
    assert picks[0]["question_ref"]["anchor"] == "architecture"


# ── 路由权限 ────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import contextlib
    from collections.abc import AsyncIterator

    @contextlib.asynccontextmanager
    async def _fake_checkpointer() -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


def _install_resolve(monkeypatch: pytest.MonkeyPatch, codes: list[str]) -> None:
    from official_agent.web import routes

    async def _resolve(*_a: object, **_k: object) -> dict:
        return {
            "user_id": 5,
            "role": "member",
            "role_names": ["面试官"],
            "permission_codes": codes,
            "source": "web",
        }

    monkeypatch.setattr(routes, "resolve", _resolve)


_AUTH = {"Authorization": "Bearer tok"}


def test_qbank_rejects_plain_candidate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolve(monkeypatch, ["candidate:read:own"])
    resp = client.get(
        "/api/agent/admin/evaluation/qbank?resume_id=1&cycle_id=2026", headers=_AUTH
    )
    assert resp.status_code == 403


def test_qbank_readable_by_interviewer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """面试官(interview:evaluate,无 resume:view)场景内可读题库(#128)。"""
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, ["interview:evaluate"])
    monkeypatch.setattr(
        ea.qbank_store,
        "latest_qbank",
        lambda r, c: {"resume_id": r, "cycle_id": c, "envelope": {"questions": []}},
    )
    resp = client.get(
        "/api/agent/admin/evaluation/qbank?resume_id=1&cycle_id=2026", headers=_AUTH
    )
    assert resp.status_code == 200


def test_pick_records_with_interviewer_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, ["interview:evaluate"])
    captured: dict = {}

    def _fake_pick(**kw):
        captured.update(kw)
        return 1

    monkeypatch.setattr(ea.qbank_store, "record_pick", _fake_pick)
    resp = client.post(
        "/api/agent/admin/evaluation/qbank/pick",
        headers=_AUTH,
        json={
            "resume_id": 9,
            "cycle_id": 2026,
            "schedule_id": 77,
            "questions": [{"anchor": "architecture", "question": "架构?"}],
        },
    )
    assert resp.status_code == 201
    assert resp.json() == {"picked": 1}
    assert captured["interviewer_user_id"] == 5  # 勾选人=当前面试官
    assert captured["schedule_id"] == 77  # 场次进 pick log(#127)


# ── #153:v2 信封门禁 + pickable 扁平视图 ──


def test_save_qbank_rejects_non_v2_envelope(monkeypatch) -> None:
    """D13 直接替换:非 v2 信封拒绝入库,并提示清理旧数据。"""
    conn = _mock_conn(fetchone={"v": 1})
    monkeypatch.setattr(qbank, "_conn", lambda: conn)
    with pytest.raises(ValueError, match="evaluation_qbank/v2"):
        qbank.save_qbank(
            resume_id=9,
            cycle_id=2026,
            source="repo_deep_dive",
            envelope={"repo_summary": "旧 v1 形状", "questions": []},
            prompt_version="evaluation_investigate/v1",
        )


def test_flatten_v2_pickable_covers_all_roles() -> None:
    envelope = {
        "schema_name": "evaluation_qbank/v2",
        "groups": [
            {
                "group": "repo",
                "owner": "me",
                "mode": "repo_deep_dive",
                "group_inner_note": "不该出现在扁平视图",
                "entry": {
                    "category": "C1_背景与动机",
                    "question": "入口题?",
                    "evidence": {"path": "README.md", "note": ""},
                    "time_minutes": 3,
                },
                "chains": [
                    {
                        "category": "C4_实现细节拷打",
                        "theme": "核心模块",
                        "layers": [
                            {"question": "L1", "expected_signal": "s1"},
                            {"question": "L2", "expected_signal": "s2"},
                        ],
                    }
                ],
                "reserves": [
                    {
                        "category": "C6_难点与调试",
                        "question": "备选?",
                        "evidence": {"path": "", "note": ""},
                        "time_minutes": 3,
                    }
                ],
            },
            {
                "group": "awards",
                "mode": "award_brief",
                "questions": [{"question": "奖项过程题", "anchor": "guided"}],
            },
        ],
    }
    from official_agent.state.qbank import flatten_v2_pickable

    flat = flatten_v2_pickable(envelope)
    roles = [(f["group_kind"], f["role"], f["question"]) for f in flat]
    assert ("repo", "entry", "入口题?") in roles
    assert ("repo", "chain", "L1") in roles and ("repo", "chain", "L2") in roles
    assert ("repo", "reserve", "备选?") in roles
    assert ("awards", "question", "奖项过程题") in roles
    chain_ref = next(f for f in flat if f["role"] == "chain")
    assert chain_ref["chain_index"] == 0 and chain_ref["layer_index"] == 0
    assert chain_ref["expected_signal"] == "s1"
    entry_ref = next(f for f in flat if f["role"] == "entry")
    assert entry_ref["evidence_path"] == "README.md"
