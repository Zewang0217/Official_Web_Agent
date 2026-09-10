"""#171 用户输入隐私闭环测试:trace 脱敏 / 删除闭环 / TTL / 管理审计。

同 test_web_routes 先例:TestClient + monkeypatch,不真连库;
真 PG 段(档案硬删往返)在文件尾部,无 PG 自动 skip。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from official_agent.web import routes
from official_agent.web.app import create_app


@contextlib.asynccontextmanager
async def _fake_checkpointer() -> AsyncIterator[None]:
    yield None


@pytest.fixture(autouse=True)
def _reset_registry():
    routes._sessions.clear()
    routes._sessions_last_access.clear()
    yield
    routes._sessions.clear()
    routes._sessions_last_access.clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


def _identity(user_id: int = 7, monitor: bool = False) -> dict:
    return {
        "user_id": user_id,
        "role": "admin" if monitor else "candidate",
        "role_names": ["管理员" if monitor else "申请人"],
        "permission_codes": ["agent:monitor"] if monitor else ["candidate:read:own"],
        "source": "web",
    }


def _sse_events(resp) -> list[dict]:
    body = "".join(resp.iter_text())
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data: ")]


# ── Langfuse 上报前输入脱敏 ──


def test_masked_handler_masks_copies_not_originals() -> None:
    """#171:handler 掩码的是**拷贝**,图状态里的原消息不被污染。"""
    from official_agent.observability import _PiiMaskedLangfuseHandler

    captured: dict = {}

    class _Inner:
        def on_chat_model_start(self, serialized, messages, **kwargs):
            captured["messages"] = messages

        def __getattr__(self, name):  # 委托其余方法
            raise AttributeError(name)

    original = HumanMessage(content="我手机 13812345678,简历如下……")
    handler = _PiiMaskedLangfuseHandler(_Inner())
    handler.on_chat_model_start({}, [[original]])
    masked_batch = captured["messages"][0]
    assert masked_batch[0].content == "我手机 138****5678,简历如下……"
    assert original.content == "我手机 13812345678,简历如下……", "原消息不得被就地修改"


def test_masked_handler_masks_llm_prompts() -> None:
    from official_agent.observability import _PiiMaskedLangfuseHandler

    captured: dict = {}

    class _Inner:
        def on_llm_start(self, serialized, prompts, **kwargs):
            captured["prompts"] = prompts

        def __getattr__(self, name):
            raise AttributeError(name)

    handler = _PiiMaskedLangfuseHandler(_Inner())
    handler.on_llm_start({}, ["电话 13812345678 其余内容"])
    assert captured["prompts"] == ["电话 138****5678 其余内容"]


# ── 删除闭环:档案硬删 + checkpoint/对话日志清理 ──


def test_delete_session_purges_all_store_faces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#171:用户删除 → 档案硬删 + checkpoint 清理 + 对话日志清理,三面联动。"""
    from official_agent.tools.client import BackendUnavailableError  # noqa: F401

    async def _resolve(*_a: object, **_k: object) -> dict:
        return _identity(7)

    monkeypatch.setattr(routes, "resolve", _resolve)

    import official_agent.state.threads as thread_store_mod

    rec = type(
        "Rec", (), {"thread_id": "web:u7:deadbeef", "owner_user_id": 7, "status": "active"}
    )()
    monkeypatch.setattr(
        thread_store_mod,
        "resolve_thread",
        lambda tid, uid: rec if tid == "web:u7:deadbeef" else None,
    )
    called: list[str] = []

    import official_agent.state.conversation as conv
    import official_agent.state.pg as pg_mod
    from official_agent.state import threads as thread_store

    monkeypatch.setattr(
        pg_mod, "purge_thread_checkpoints", lambda tid: called.append(f"ckpt:{tid}") or 3
    )
    monkeypatch.setattr(
        conv, "delete_thread_conversations", lambda tid: called.append(f"conv:{tid}") or 5
    )
    monkeypatch.setattr(
        thread_store,
        "hard_delete_thread",
        lambda tid, *, owner_user_id: called.append(f"row:{tid}") or True,
    )

    resp = client.delete(
        "/api/agent/sessions/web:u7:deadbeef",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 204
    assert sorted(called) == ["ckpt:web:u7:deadbeef", "conv:web:u7:deadbeef", "row:web:u7:deadbeef"]


def test_delete_session_rejects_non_owner(client: TestClient, monkeypatch) -> None:
    """非属主/不存在 → 404;patch 必须落在函数局部导入的源模块上。"""

    async def _resolve(*_a: object, **_k: object) -> dict:
        return _identity(7)

    monkeypatch.setattr(routes, "resolve", _resolve)
    import official_agent.state.threads as thread_store_mod

    monkeypatch.setattr(thread_store_mod, "resolve_thread", lambda tid, uid: None)
    resp = client.delete(
        "/api/agent/sessions/web:u7:ghost01",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 404


def test_admin_transcript_read_is_audited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#171:管理员原文读取落审计(actor/thread/时间可对账),fail-open。"""
    from official_agent.web import routes as r

    async def _resolve(*_a: object, **_k: object) -> dict:
        return _identity(9, monitor=True)

    monkeypatch.setattr(r, "resolve", _resolve)
    rec = type("Rec", (), {"thread_id": "web:u7:abc", "owner_user_id": 7, "status": "active"})()

    def _get_thread(tid):
        return rec if tid == "web:u7:abc" else None

    import official_agent.state.audit as audit_mod
    import official_agent.state.threads as thread_store_mod

    monkeypatch.setattr(thread_store_mod, "get_thread", _get_thread)
    monkeypatch.setattr(r, "_fetch_transcript", _fake_fetch)
    audits: list[dict] = []

    def _audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(audit_mod, "write_audit", _audit)
    resp = client.get(
        "/api/agent/admin/sessions/web:u7:abc/messages",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    assert len(audits) == 1
    assert audits[0]["action"]["op"] == "admin_read_transcript"
    assert audits[0]["acting_user_id"] == 9
    assert audits[0]["thread_id"] == "web:u7:abc"


async def _fake_fetch(request, thread_id):
    return []


# ── 真 PG:档案硬删往返 ──


def _pg_url() -> str:
    import os

    return os.environ.get("POSTGRES_URL", "")


def test_hard_delete_roundtrip_real_pg() -> None:
    import os  # noqa: F401
    import uuid

    import pytest

    from official_agent.config import get_settings
    from official_agent.state import threads as thread_store
    from official_agent.state.pg import purge_thread_checkpoints

    url = _pg_url()
    if not url:
        pytest.skip("需要真 PostgreSQL(POSTGRES_URL)")
    owner = uuid.uuid4().int % 1_000_000 + 10
    original = get_settings().postgres_url
    get_settings().postgres_url = url
    try:
        tid = thread_store.create_thread("web", owner, subject="web-chat").thread_id
        # checkpoint 三表清理对空/缺数据安全(真库冒烟)
        assert purge_thread_checkpoints(tid) >= 0
        assert thread_store.hard_delete_thread(tid, owner_user_id=owner) is True
        assert thread_store.get_thread(tid) is None, "硬删后档案不可见"
        assert thread_store.resolve_thread(tid, owner) is None, "硬删后恢复必须拒"
        assert thread_store.hard_delete_thread(tid, owner_user_id=owner) is False
    finally:
        get_settings().postgres_url = original
