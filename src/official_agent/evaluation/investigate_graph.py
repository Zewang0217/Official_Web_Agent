"""调查子图·仓深挖(B3,#130):路由 → 取仓 → 值得度 → 四证据锚题。

- 与 B1 评分子图平行(一总图两子图的调查子图,#126)
- 生成轨与 B1 相同:提示词 JSON + strict Pydantic;后置校验(评审 P1 同款):
  deep_dive 题必带 evidence.path 且路径必须真实存在于仓;guided 题不得带仓路径
- GitHub 不可达不是失败:降级 guided(注明 unavailable),不阻塞任务
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

from official_agent.config import get_effective_settings
from official_agent.evaluation.attribution import (
    RepoAttribution,
    attribute,
    detect_contribution_target,
    resolve_entry,
)
from official_agent.evaluation.github_client import GitHubClient, GitHubUnavailable
from official_agent.evaluation.graph import _extract_json
from official_agent.evaluation.investigate import (
    WORTHINESS_QUESTION_COUNT,
    build_repo_brief,
    extract_repo,
    repo_worthiness,
    route_project,
)
from official_agent.evaluation.schema import QuestionSet
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt, load_prompt_meta

PROMPT_FILE = "evaluation_investigate.md"
SCORING_TEMPERATURE = 0.2


def _prompt_version() -> str:
    return load_prompt_meta(PROMPT_FILE).get("version", "unknown")


class InvestigationState(TypedDict, total=False):
    project_text: str
    github_base: str
    github_token: str
    candidate_login: str
    route: str
    repo_owner: str
    repo_name: str
    default_branch: str
    repo_brief: str
    attribution: dict
    paths: list[str]
    paths_truncated: bool
    worthiness: str
    question_set: dict[str, Any]
    error: str | None


async def route_node(state: InvestigationState) -> dict:
    """提取仓位置并探测可读性 → 路由(#130);入口瀑布+归属四级(#150)。

    支持 M-1 多仓:调用方可预置 repo_owner/repo_name 钉住某仓(逐仓深挖);
    未钉时走瀑布:简历 URL 直配 → 绑定登录名匹配 → GitHub 搜索兜底。
    unverified(搜索撞名)不深挖只 guided(ADR-0008)。
    """
    text = state["project_text"]
    login = state.get("candidate_login", "")
    pinned_owner = state.get("repo_owner")
    pinned_name = state.get("repo_name")
    repo = (
        (pinned_owner, pinned_name)
        if pinned_owner and pinned_name
        else extract_repo(text)
    )
    if repo is None and not login and detect_contribution_target(text) is None:
        # 无仓位置且无登录名/贡献声明:不建 client(零 GitHub 调用,skip 断言依赖此)
        return {"route": route_project(text, None)}
    client = GitHubClient(
        base_url=state.get("github_base") or "https://api.github.com",
        token=state.get("github_token") or "",
    )
    found: RepoAttribution | None = None
    if repo is None:
        # 贡献声明优先于绑定匹配/搜索(D4):「仓+贡献动词」是明确点名,
        # 绑定并查到 commits/PR → trusted-contribution 深挖;未绑定/无证据
        # → claimed,不深挖只出过程题
        target = detect_contribution_target(text)
        if target:
            found = await attribute(
                *target, login=login, source="contribution", client=client
            )
            if found.deep_dive_allowed:
                repo = target
            else:
                return {
                    "route": "guided",
                    "repo_owner": target[0],
                    "repo_name": target[1],
                    "attribution": {
                        "owner": found.owner,
                        "name": found.name,
                        "level": found.level,
                        "evidence": found.evidence,
                        "source": found.source,
                    },
                }
    if repo is None and login:
        # 瀑布第 2/3 步:绑定名下匹配 / 搜索兜底(第 1 步已被 extract_repo 覆盖)
        try:
            found = await resolve_entry(text, login=login, client=client)
        except GitHubUnavailable:
            found = None
        if found:
            repo = (found.owner, found.name)
    if repo is None:
        return {"route": route_project(text, None)}
    try:
        meta = await client.repo(*repo)
        branch = meta.get("default_branch") or "main"
    except GitHubUnavailable:
        degraded: dict = {
            "route": route_project(text, False),
            "repo_owner": repo[0],
            "repo_name": repo[1],
        }
        if found is not None:
            degraded["attribution"] = {
                "owner": found.owner,
                "name": found.name,
                "level": found.level,
                "evidence": found.evidence,
                "source": found.source,
            }
        return degraded
    if found is None:
        # 钉住/URL 直配的仓:简历自述来源 → source=url(归属内部自查 commits/PR)
        found = await attribute(
            repo[0], repo[1], login=login, source="url", client=client
        )
    readable = True
    route = route_project(text, readable)
    if not found.deep_dive_allowed:
        # unverified:仓存在也不深挖,仅 guided(ADR-0008)
        route = "guided"
    return {
        "route": route,
        "repo_owner": repo[0],
        "repo_name": repo[1],
        "default_branch": str(branch),
        "attribution": {
            "owner": found.owner,
            "name": found.name,
            "level": found.level,
            "evidence": found.evidence,
            "source": found.source,
        },
    }


def route_after_route(state: InvestigationState) -> str:
    return state["route"]  # deep_dive | guided | skip


async def fetch_node(state: InvestigationState) -> dict:
    """取 README+提交+文件树 → 简报与值得度;不可达则降级 guided(#130)。"""
    client = GitHubClient(
        base_url=state.get("github_base") or "https://api.github.com",
        token=state.get("github_token") or "",
    )
    try:
        owner, name = state["repo_owner"], state["repo_name"]
        readme = await client.readme(owner, name)
        commits = await client.commits(owner, name)
        paths, tree_truncated = await client.tree_paths(
            owner, name, branch=state.get("default_branch")
        )
    except GitHubUnavailable as exc:
        return {
            "route": "guided",
            "error": None,
            "repo_brief": f"仓库探测中途不可读({exc}),降级通用引导题",
        }
    worthiness = repo_worthiness(
        readme_chars=len(readme), commit_count=len(commits), paths=paths
    )
    return {
        "repo_brief": build_repo_brief(readme=readme, commits=commits, paths=paths),
        "paths": paths,
        "paths_truncated": tree_truncated,
        "worthiness": worthiness,
        "error": None,
    }


async def generate_node(state: InvestigationState) -> dict:
    """出题(deep_dive, M-3 v2):repo_summary=项目基本面 + questions=模块设计取向题。

    - 题数=值得度(none/低/高 → 0/2/4);
    - 设计取向问(架构分层/tradeoff/edge_case),允许纯技术栈取向题无单一文件
      锚点(path 留空 + note 说明)——不再强制「四证据锚各一/必带仓路径」。
    - guided 1-2 道通用引导题。题数是硬校验(B3 评审 P1):worthiness=none 不调
      模型直接空集,模型多给/少给都翻 error 态——值得度不被提示词文本架空。
    """
    try:
        deep = state["route"] == "deep_dive"
        if deep:
            count = WORTHINESS_QUESTION_COUNT.get(state.get("worthiness", "low"), 2)
            if count == 0:
                return {"question_set": _dump(QuestionSet(), deep), "error": None}
        else:
            count = 2
        settings = get_effective_settings()
        model = build_model(settings, temperature=SCORING_TEMPERATURE)
        brief = (
            state.get("repo_brief", "")
            if deep
            else (
                state.get("repo_brief", "")
                or "仓库不可读/未提供;仅依据候选人自述出通用项目引导题"
            )
        )
        prompt_text = (
            load_prompt(PROMPT_FILE)
            + "\n\n---\n\n候选人自述:\n"
            + state["project_text"]
            + "\n\n仓库材料:\n"
            + brief
            + f"\n\n必须恰好出 {count} 道题,多一题少一题都不合格。"
            + (
                "每题 evidence.path 必须是上面文件结构里真实存在的路径。"
                if deep
                else (
                    "仓库不可读:每题 evidence.path 留空字符串,"
                    "evidence.note 写「仓不可读,通用引导」。"
                )
            )
            + "\n只输出符合 schema 的 JSON 对象,不要任何其他文字或代码围栏。"
        )
        resp = await model.ainvoke([HumanMessage(content=prompt_text)])
        raw = resp.content
        if isinstance(raw, list):
            raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
        content = raw if isinstance(raw, str) else str(raw)
        result = QuestionSet.model_validate_json(_extract_json(content))
        # strict 后置校验(P1 同款):题数/锚/路径三重一致性
        if deep and len(result.questions) != count:
            raise ValueError(f"题数不符:要求 {count},模型给 {len(result.questions)}")
        if not deep and len(result.questions) > 2:
            raise ValueError(f"引导题超量:{len(result.questions)}")
        paths = set(state.get("paths", []))
        paths_truncated = bool(state.get("paths_truncated"))
        for q in result.questions:
            if deep:
                # M-3 设计取向重构:纯技术栈/设计哲学取向题可无单一仓内文件锚点
                # (该模块跨多文件);此时 evidence.note 须解释为何不落单路径。
                if not q.evidence.path:
                    if not q.evidence.note:
                        raise ValueError("deep_dive 题空路径须有 evidence.note")
                # 给了路径→仓内白名单校验(I/O 降级或树截断则放宽——评审 P2)
                elif paths and not paths_truncated and q.evidence.path not in paths:
                    raise ValueError(f"evidence.path 不在仓内:{q.evidence.path!r}")
                if q.anchor == "guided":
                    raise ValueError("deep_dive 题不得用 guided 锚")
            elif q.evidence.path:
                raise ValueError("guided 题不应带仓路径")
        return {"question_set": _dump(result, deep), "error": None}
    except Exception as exc:  # noqa: BLE001 — 失败进 error 态,B2 可重试
        return {"question_set": None, "error": f"{type(exc).__name__}: {exc}"}


def _dump(result: QuestionSet, deep: bool) -> dict[str, Any]:
    result.mode = "repo_deep_dive" if deep else "guided"
    result.prompt_version = _prompt_version()
    return result.model_dump()


async def skip_node(state: InvestigationState) -> dict:
    """skip:此维不出题(空集合法,#130)。"""
    envelope = QuestionSet(mode="skipped", prompt_version=_prompt_version())
    return {"question_set": envelope.model_dump(), "error": None}


async def finalize(state: InvestigationState) -> dict:
    return {}


_compiled: Any | None = None


def build_investigation_subgraph() -> Any:
    """调查子图:route →(deep_dive→fetch)/guided/skip → generate → finalize。"""
    global _compiled
    if _compiled is not None:
        return _compiled
    g = StateGraph(InvestigationState)
    g.add_node("route", route_node)
    g.add_node("fetch", fetch_node)
    g.add_node("generate", generate_node)
    g.add_node("skip", skip_node)
    g.add_node("finalize", finalize)
    g.set_entry_point("route")
    g.add_conditional_edges("route", route_after_route, {
        "deep_dive": "fetch",
        "guided": "generate",
        "skip": "skip",
    })
    g.add_edge("fetch", "generate")
    g.add_edge("generate", "finalize")
    g.add_edge("skip", "finalize")
    g.add_edge("finalize", END)
    _compiled = g.compile()
    return _compiled


async def run_investigation(
    project_text: str,
    *,
    repo: tuple[str, str] | None = None,
    github_base: str = "https://api.github.com",
    github_token: str = "",
    candidate_login: str = "",
) -> dict:
    """便捷入口:返回题集 dict(questions 可为空=skip/降级);LLM 失败抛 RuntimeError。

    M-1:可钉 repo(owner, repo) 逐仓调查(多仓候选一个仓一个 envelope);
    缺省按项目文本首个 GitHub URL。
    """
    graph = build_investigation_subgraph()
    init: InvestigationState = {
        "project_text": project_text,
        "github_base": github_base,
        "github_token": github_token,
        "candidate_login": candidate_login,
    }
    if repo:
        init["repo_owner"], init["repo_name"] = repo
    final = await graph.ainvoke(init)
    if final.get("error") or final.get("question_set") is None:
        raise RuntimeError(f"调查子图失败:{final.get('error')}")
    return final["question_set"]
