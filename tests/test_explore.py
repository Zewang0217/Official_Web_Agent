"""探索段受限 ReAct 循环单测(#151):预算四闸/槽位映射/缓存纪律/降级。

图级集成由 test_investigation.py 的 run_explore 替身覆盖;这里直接驱动
explore_repo,验证循环自身语义。
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from official_agent.evaluation import dossier as dossier_mod
from official_agent.evaluation import explore as explore_mod
from official_agent.evaluation.explore import explore_repo
from official_agent.evaluation.github_client import GitHubUnavailable


class _FakeModel:
    """bind_tools + 脚本化响应序列;记录全部 seen 消息。"""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.seen: list[list] = []
        self.system_texts: list[str] = []
        self.bind_failed = False

    def bind_tools(self, tools: Any, **kwargs: Any):
        if self.bind_failed:
            raise RuntimeError("no tools support")
        return self

    async def ainvoke(self, messages: list):
        self.seen.append(list(messages))
        for m in messages:
            if isinstance(m, SystemMessage):
                self.system_texts.append(m.content)
        return self.responses.pop(0)


class _FakeClient:
    """D6 鸭子类型替身:按脚本吐观察。"""

    def __init__(self, results: dict[str, Any] | None = None):
        self.results = results or {}
        self.calls: list[str] = []

    async def read_file(self, owner, repo, path, *, branch=None):
        self.calls.append(f"read_file:{path}")
        return self.results.get("read_file", {"type": "file", "content": "x = 1"})

    async def list_files(self, owner, repo, *, branch=None, limit=600):
        self.calls.append("list_files")
        return self.results.get("list_files", (["README.md", "src/app.py"], False))

    async def repo_meta(self, owner, repo):
        self.calls.append("repo_meta")
        return self.results.get("repo_meta", {"full_name": f"{owner}/{repo}", "fork": False})

    async def read_commits(self, owner, repo, *, author=None, per_page=30):
        self.calls.append("read_commits")
        return self.results.get("read_commits", [])

    async def search_in_repo(self, owner, repo, query, *, per_page=20):
        self.calls.append("search_in_repo")
        return self.results.get("search_in_repo", [])

    async def search_repos(self, query, *, per_page=10):
        self.calls.append("search_repos")
        return self.results.get("search_repos", [])

    async def list_user_repos(self, username, *, per_page=100):
        self.calls.append("list_user_repos")
        return self.results.get("list_user_repos", [])

    async def commit_detail(self, owner, repo, sha, *, max_patch_chars=8000):
        self.calls.append("commit_detail")
        return self.results.get("commit_detail", {})

    async def search_issues(self, owner, repo, query, *, is_pr=True, per_page=20):
        self.calls.append("search_issues")
        return self.results.get("search_issues", [])


def _ai_with_tools(calls: list[tuple[str, dict]]):
    return AIMessage(
        "",
        tool_calls=[
            {"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(calls)
        ],
    )


def _text_ai(text: str):
    return AIMessage(content=text)


@pytest.mark.asyncio
async def test_tool_roundtrip_writes_slots_and_paths() -> None:
    model = _FakeModel(
        [
            _ai_with_tools([("list_files", {}), ("read_file", {"path": "src/app.py"})]),
            _text_ai("材料够了"),
        ]
    )
    client = _FakeClient()
    dossier = await explore_repo(
        project_text="我做了 xx",
        owner="me",
        name="demo",
        attribution="trusted-own",
        login="me",
        client=client,  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
    )
    assert dossier.paths == ["README.md", "src/app.py"]  # list_files 结构化捕获
    assert "read_file:src/app.py" in client.calls
    assert dossier.slots["C3_架构与数据流"]  # 观察进槽
    assert dossier.slots["C4_实现细节拷打"]
    assert dossier.turns_used == 4  # 2 次 LLM + 2 次工具(D7 累计)
    assert not dossier.degraded
    assert dossier.attribution == "trusted-own"


@pytest.mark.asyncio
async def test_turn_budget_gate_marks_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explore_mod, "MAX_TURNS", 2)
    always_tools = [_ai_with_tools([("repo_meta", {})])] * 10
    model = _FakeModel(always_tools)
    dossier = await explore_repo(
        project_text="t", owner="o", name="r", attribution="", login="",
        client=_FakeClient(), model=model,  # type: ignore[arg-type]
    )
    assert dossier.degraded
    assert "轮数 2 触顶" in dossier.degrade_reason
    assert dossier.turns_used == 2


@pytest.mark.asyncio
async def test_wall_clock_gate_marks_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explore_mod, "MAX_WALL_SECONDS", 0)
    model = _FakeModel([_ai_with_tools([("repo_meta", {})])])
    dossier = await explore_repo(
        project_text="t", owner="o", name="r", attribution="", login="",
        client=_FakeClient(), model=model,  # type: ignore[arg-type]
    )
    assert dossier.degraded
    assert "墙钟" in dossier.degrade_reason


@pytest.mark.asyncio
async def test_dossier_cap_gate_discards_and_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dossier_mod, "_MAX_DOSSIER_CHARS", 50)
    model = _FakeModel(
        [
            _ai_with_tools([("read_file", {"path": "big.py"})]),
            _ai_with_tools([("read_file", {"path": "big2.py"})]),
            _text_ai("done"),
        ]
    )
    dossier = await explore_repo(
        project_text="t", owner="o", name="r", attribution="", login="",
        client=_FakeClient(
            {"read_file": {"type": "file", "content": "y" * 120}}
        ),  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
    )
    assert dossier.degraded
    assert "40K 上限触顶" in dossier.degrade_reason
    assert dossier.total_chars <= 120  # 第二条整条丢弃


@pytest.mark.asyncio
async def test_github_unavailable_becomes_observation_not_crash() -> None:
    class _DeadClient(_FakeClient):
        async def repo_meta(self, owner, repo):
            raise GitHubUnavailable("GitHub 403")

    model = _FakeModel(
        [_ai_with_tools([("repo_meta", {})]), _text_ai("查不了")]
    )
    dossier = await explore_repo(
        project_text="t", owner="o", name="r", attribution="", login="",
        client=_DeadClient(), model=model,  # type: ignore[arg-type]
    )
    assert not dossier.degraded  # 单工具失败是观察,不是任务失败
    assert "工具不可用" in dossier.slots["C1_背景与动机"] or dossier.slots["C1_背景与动机"] == ""


@pytest.mark.asyncio
async def test_model_exception_degrades_without_raise() -> None:
    class _BoomModel:
        def bind_tools(self, tools: Any, **kwargs: Any):
            return self

        async def ainvoke(self, messages: list):
            raise RuntimeError("LLM exploded")

    dossier = await explore_repo(
        project_text="t", owner="o", name="r", attribution="", login="",
        client=_FakeClient(), model=_BoomModel(),  # type: ignore[arg-type]
    )
    assert dossier.degraded
    assert "探索异常" in dossier.degrade_reason


@pytest.mark.asyncio
async def test_bind_failure_degrades() -> None:
    model = _FakeModel([])
    model.bind_failed = True
    dossier = await explore_repo(
        project_text="t", owner="o", name="r", attribution="", login="",
        client=_FakeClient(), model=model,  # type: ignore[arg-type]
    )
    assert dossier.degraded and "绑定失败" in dossier.degrade_reason


@pytest.mark.asyncio
async def test_cache_discipline_append_only_and_static_system() -> None:
    """D8:system 跨候选字节稳定;历史只增;步数计数在 user 消息尾部。"""
    model = _FakeModel(
        [
            _ai_with_tools([("repo_meta", {})]),
            _text_ai("ok"),
        ]
    )
    await explore_repo(
        project_text="自述甲", owner="o", name="r", attribution="", login="登录名甲",
        client=_FakeClient(), model=model,  # type: ignore[arg-type]
    )
    assert model.system_texts, "system 只发一次"
    assert all(t == model.system_texts[0] for t in model.system_texts)
    system = model.system_texts[0]
    assert "登录名甲" not in system and "甲" not in system  # 仓主/登录名不进 system
    assert "o/r" not in system

    first, second = model.seen
    # 历史只增:第二轮消息以第一轮为前缀(追加不改写)
    assert second[: len(first)] == first
    # 步数计数放 user 消息尾部(每轮一条,递增)
    user_texts = [m.content for m in second if isinstance(m, HumanMessage)]
    assert any("[探索步 1/" in t for t in user_texts)
    assert user_texts[-1].startswith("[探索步 3/")  # 尾部=最新步数(含工具调用累计)


@pytest.mark.asyncio
async def test_unknown_tool_reports_observation() -> None:
    model = _FakeModel(
        [_ai_with_tools([("delete_repo", {})]), _text_ai("done")]
    )
    dossier = await explore_repo(
        project_text="t", owner="o", name="r", attribution="", login="",
        client=_FakeClient(), model=model,  # type: ignore[arg-type]
    )
    assert not dossier.degraded  # 幻觉工具名不炸循环


@pytest.mark.asyncio
async def test_usage_tokens_accumulated_d9() -> None:
    """D9/#154:逐调用 usage_metadata 累计(含 cache hit/miss)进 dossier。"""

    class _UsageModel:
        def __init__(self):
            self.seen: list = []
            self.n = 0

        def bind_tools(self, tools: Any, **kwargs: Any):
            return self

        async def ainvoke(self, messages: list):
            self.seen.append(list(messages))
            self.n += 1
            tool_call = self.n == 1
            msg = (
                AIMessage(
                    "",
                    tool_calls=[{"name": "repo_meta", "args": {}, "id": "c1"}],
                    usage_metadata={
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                        "input_token_details": {"cache_read": 60, "cache_creation": 10},
                    },
                )
                if tool_call
                else AIMessage(
                    "done",
                    usage_metadata={
                        "input_tokens": 50,
                        "output_tokens": 10,
                        "total_tokens": 60,
                        "input_token_details": {"cache_read": 40, "cache_creation": 5},
                    },
                )
            )
            return msg

    model = _UsageModel()
    dossier = await explore_repo(
        project_text="t",
        owner="o",
        name="r",
        attribution="",
        login="",
        client=_FakeClient(),  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
    )
    assert dossier.input_tokens == 150
    assert dossier.output_tokens == 30
    assert dossier.cache_hit_tokens == 100
    assert dossier.cache_miss_tokens == 15
