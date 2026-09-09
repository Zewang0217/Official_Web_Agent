"""B3 调查子图·仓深挖测试:路由/值得度/GitHub 客户端(respx)/子图三路径。"""


import httpx
import pytest
import respx
from httpx import Response

from official_agent.evaluation import investigate as inv
from official_agent.evaluation import investigate_graph as ig
from official_agent.evaluation.github_client import GitHubClient, GitHubUnavailable

# ── 路由与值得度(纯函数) ────────────────────────────────


def test_extract_repo_tolerates_git_suffix_and_noise() -> None:
    assert inv.extract_repo("项目 https://github.com/aB_C/demo.git 报名页") == ("aB_C", "demo")
    assert inv.extract_repo("github.com/owner/repo,做了 X") == ("owner", "repo")
    assert inv.extract_repo("只有文字没有链接") is None


def test_route_three_ways() -> None:
    long_text = "我做了社团官网重构,负责报名页与后端接口。" * 3
    assert inv.route_project(long_text, True) == "deep_dive"  # 有仓可读
    assert inv.route_project(long_text, None) == "guided"  # 无仓有实质
    assert inv.route_project("太短", None) == "skip"  # 无仓无实质
    assert inv.route_project("太短", False) == "guided"  # 有仓但不可读→引导


def test_worthiness_tiers() -> None:
    high = inv.repo_worthiness(
        readme_chars=800, commit_count=20, paths=[f"src/{i}.py" for i in range(40)]
    )
    none = inv.repo_worthiness(readme_chars=10, commit_count=1, paths=["a.py"])
    assert high == "high" and none == "none"
    assert inv.WORTHINESS_QUESTION_COUNT["high"] == 5  # 概况 2 + 模块分析 3
    assert inv.WORTHINESS_QUESTION_COUNT["none"] == 0


# ── GitHub 客户端(respx) ────────────────────────────────


@respx.mock
async def test_client_happy_paths() -> None:
    base = "https://api.github.test"
    respx.get(f"{base}/repos/o/r").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    respx.get(f"{base}/repos/o/r/readme").mock(
        return_value=Response(200, text="# Demo\n内容")
    )
    respx.get(f"{base}/repos/o/r/commits").mock(
        return_value=Response(
            200, json=[{"commit": {"message": "feat: x", "author": {"date": "2026-01-01"}}}]
        )
    )
    respx.get(f"{base}/repos/o/r/git/trees/main").mock(
        return_value=Response(
            200,
            json={
                "tree": [
                    {"type": "blob", "path": "src/app.py"},
                    {"type": "tree", "path": "src"},
                ]
            },
        )
    )
    client = GitHubClient(base_url=base)
    assert (await client.repo("o", "r"))["default_branch"] == "main"
    assert "Demo" in await client.readme("o", "r")
    commits = await client.commits("o", "r")
    assert commits[0]["message"] == "feat: x"
    paths, truncated = await client.tree_paths("o", "r", branch="main")
    assert paths == ["src/app.py"] and truncated is False


@respx.mock
async def test_client_unavailable_and_missing_readme() -> None:
    base = "https://api.github.test"
    respx.get(f"{base}/repos/o/private").mock(return_value=Response(404, json={}))
    respx.get(f"{base}/repos/o/noreadme/readme").mock(return_value=Response(404))
    respx.get(f"{base}/repos/o/noreadme").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    client = GitHubClient(base_url=base)
    with pytest.raises(GitHubUnavailable):
        await client.repo("o", "private")
    assert await client.readme("o", "noreadme") == ""  # 无 README 是扣分项不是错误


@respx.mock
async def test_client_network_error_is_unavailable() -> None:
    base = "https://api.github.test"
    respx.get(f"{base}/repos/o/r").mock(side_effect=httpx.ConnectError("refused"))
    client = GitHubClient(base_url=base)
    with pytest.raises(GitHubUnavailable, match="连接失败"):
        await client.repo("o", "r")


# ── 子图三路径 ──────────────────────────────────────────


class _FakeGH:
    """可读仓:README 足量+12 提交+结构目录 → high 值得度。"""

    def __init__(self, base_url: str = "", token: str = "") -> None:
        pass

    async def repo(self, owner, repo):
        return {"default_branch": "main"}  # route 探测;fetch 复用 branch

    async def readme(self, owner, repo):
        return "# Demo\n" + "x" * 600

    async def commits(self, owner, repo, per_page=30):
        return [{"message": f"feat: {i}", "date": "2026-01-01"} for i in range(12)]

    async def tree_paths(self, owner, repo, *, branch=None, limit=600):
        return ["README.md", "src/app.py", "tests/test_app.py"], False


def _q(part: int, anchor: str, path: str, text: str) -> str:
    return (
        f'{{"part": {part}, "anchor": "{anchor}", "question": "{text}", "sub_prompts": [],'
        f'"answer_reference": {{"strong": "s", "acceptable": "a", "weak": "w"}},'
        f'"evidence": {{"path": "{path}", "note": "n"}}, "time_minutes": 3}}'
    )


_GOOD_JSON = (
    '{"repo_overview": {"what": "社团官网与 Agent", "tech_stack": "TS",'
    ' "structure_note": "src 分层", "highlights": ["有测试"], "risks": [],'
    ' "ai_assessment": "值得深挖架构"}, "questions": ['
    + _q(1, "overview", "README.md", "这个项目是什么、解决什么问题?")
    + ","
    + _q(1, "tech_rationale", "src/app.py", "为什么选 TypeScript?")
    + ","
    + _q(2, "module_design", "tests/test_app.py", "核心模块怎么划分职责?")
    + ","
    + _q(2, "tradeoff", "src/app.py", "现在的架构取舍是什么?")
    + ","
    + _q(2, "edge_case", "tests/test_app.py", "如果输入非法会怎样?")
    + "]}"
)


def _install_fake_gh_and_model(monkeypatch, payload: str) -> None:
    monkeypatch.setattr(ig, "GitHubClient", _FakeGH)

    class _Msg:
        content = payload

    class _M:
        async def ainvoke(self, messages):
            return _Msg()

    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "get_effective_settings", _S)


@pytest.mark.asyncio
async def test_deep_dive_happy_path(monkeypatch) -> None:
    _install_fake_gh_and_model(monkeypatch, _GOOD_JSON)
    qs = await ig.run_investigation("我做了 https://github.com/me/demo 报名页重构")
    assert qs["mode"] == "repo_deep_dive"
    assert qs["questions"][0]["evidence"]["path"] == "README.md"  # 路径真实在仓
    assert qs["repo_overview"]["ai_assessment"]  # AI 初判随卡下发


@pytest.mark.asyncio
async def test_probe_failure_degrades_to_guided(monkeypatch) -> None:
    """仓探测失败 → guided 降级,题不带仓路径(#130:私有/不可达注明)。"""

    class _PrivateGH(_FakeGH):
        async def repo(self, owner, repo):
            raise GitHubUnavailable("GitHub 404")

    monkeypatch.setattr(ig, "GitHubClient", _PrivateGH)

    class _Msg:
        content = (
            '{"questions": [{"anchor": "guided", "question": "项目里你承担了什么?",'
            '"sub_prompts": [],'
            '"answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},'
            '"evidence": {"path": "", "note": "仓不可读,通用引导"},'
            '"time_minutes": 3}]}'
        )

    class _M:
        async def ainvoke(self, messages):
            return _Msg()

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())
    monkeypatch.setattr(ig, "get_effective_settings", _S)

    qs = await ig.run_investigation("我做了 github.com/me/private 电商后端,用了 Redis")
    assert qs["mode"] == "guided"
    assert qs["questions"][0]["evidence"]["path"] == ""


@pytest.mark.asyncio
async def test_skip_path_no_model_call(monkeypatch) -> None:
    def _boom(*a, **k):
        raise AssertionError("skip 路径不得调模型/GitHub")

    monkeypatch.setattr(ig, "build_model", _boom)
    monkeypatch.setattr(ig, "GitHubClient", _boom)
    qs = await ig.run_investigation("   ")
    assert qs["mode"] == "skipped" and qs["questions"] == []


@pytest.mark.asyncio
async def test_deep_dive_fabricated_path_rejected(monkeypatch) -> None:
    """证据路径不在仓内(编造)→ RuntimeError,B2 可重试。"""
    _install_fake_gh_and_model(
        monkeypatch,
        _GOOD_JSON.replace('"path": "src/app.py"', '"path": "src/编造的路径.py"'),
    )
    with pytest.raises(RuntimeError, match="不在仓内"):
        await ig.run_investigation("项目 https://github.com/me/demo")


def test_repo_regex_boundaries() -> None:
    """B3 评审 P2:句点收尾/伪站名不误配不误粘。"""
    assert inv.extract_repo("项目是 github.com/owner/repo.") == ("owner", "repo")
    assert inv.extract_repo("看 mygithub.com/owner/repo 这个") is None
    assert inv.extract_repo("github.com/owner/repo.git 已归档") == ("owner", "repo")


def test_worthiness_low_tier() -> None:
    """low 档:少量信号 → 2 题。"""
    assert (
        inv.repo_worthiness(
            readme_chars=800, commit_count=5, paths=["a.py", "b.py"]
        )
        == "low"
    )
    assert inv.WORTHINESS_QUESTION_COUNT["low"] == 2


@pytest.mark.asyncio
async def test_worthiness_none_skips_llm(monkeypatch) -> None:
    """零信号仓不出题也不调模型(B3 评审 P1:值得度不被提示词架空)。"""

    class _BareGH(_FakeGH):
        async def readme(self, owner, repo):
            return ""

        async def commits(self, owner, repo, per_page=30):
            return []

        async def tree_paths(self, owner, repo, *, branch=None, limit=600):
            return ["a.py"], False

    monkeypatch.setattr(ig, "GitHubClient", _BareGH)

    def _boom(*a, **k):
        raise AssertionError("零信号不得调模型")

    monkeypatch.setattr(ig, "build_model", _boom)

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "get_effective_settings", _S)
    qs = await ig.run_investigation("项目 https://github.com/me/bare 空仓")
    assert qs["mode"] == "repo_deep_dive" and qs["questions"] == []


@pytest.mark.asyncio
async def test_fetch_midway_failure_degrades_to_guided(monkeypatch) -> None:
    """B3 评审 P2:route 探测通过但 fetch 中途失败 → 降级 guided + 注明。"""

    class _MidwayGH(_FakeGH):
        async def readme(self, owner, repo):
            raise GitHubUnavailable("GitHub 403")

    monkeypatch.setattr(ig, "GitHubClient", _MidwayGH)

    class _Msg:
        content = (
            '{"questions": [{"anchor": "guided", "question": "自述的重构你承担了哪些?",'
            '"sub_prompts": [],'
            '"answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},'
            '"evidence": {"path": "", "note": "仓不可读,通用引导"},'
            '"time_minutes": 3}]}'
        )

    class _M:
        async def ainvoke(self, messages):
            return _Msg()

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())
    monkeypatch.setattr(ig, "get_effective_settings", _S)

    qs = await ig.run_investigation("我做了 github.com/me/demo 官网重构,React 技术栈")
    assert qs["mode"] == "guided"
    assert qs["questions"][0]["evidence"]["path"] == ""


@pytest.mark.asyncio
async def test_count_mismatch_rejected(monkeypatch) -> None:
    """B3 评审 P1:题数不符硬校验——high 档必须恰好 4 题。"""
    _install_fake_gh_and_model(monkeypatch, _GOOD_JSON.replace("time_minutes\": 3}]}",
                                                               "time_minutes\": 3}]}"))
    # _GOOD_JSON 恰好 4 题 → 通过;只给 1 题的旧 payload → 拒
    one_q = (
        '{"repo_overview": {"what": "x", "tech_stack": "t", "structure_note": "s",'
        ' "highlights": [], "risks": [], "ai_assessment": "a"}, "questions": ['
        + _q(1, "overview", "README.md", "架构?")
        + "]}"
    )
    _install_fake_gh_and_model(monkeypatch, one_q)
    with pytest.raises(RuntimeError, match="题数不符"):
        await ig.run_investigation("项目 https://github.com/me/demo 报名页")
