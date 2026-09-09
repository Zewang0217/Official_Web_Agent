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
    route: str
    repo_owner: str
    repo_name: str
    default_branch: str
    repo_brief: str
    paths: list[str]
    paths_truncated: bool
    worthiness: str
    question_set: dict[str, Any]
    error: str | None


async def route_node(state: InvestigationState) -> dict:
    """提取仓位置并探测可读性 → 路由(#130)。"""
    text = state["project_text"]
    repo = extract_repo(text)
    if repo is None:
        return {"route": route_project(text, None)}
    client = GitHubClient(
        base_url=state.get("github_base") or "https://api.github.com",
        token=state.get("github_token") or "",
    )
    try:
        meta = await client.repo(*repo)
        branch = meta.get("default_branch") or "main"
    except GitHubUnavailable:
        return {
            "route": route_project(text, False),
            "repo_owner": repo[0],
            "repo_name": repo[1],
        }
    return {
        "route": route_project(text, True),
        "repo_owner": repo[0],
        "repo_name": repo[1],
        "default_branch": str(branch),
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
    """出题:deep_dive 四证据锚题(题数=值得度);guided 1-2 道通用引导题。

    题数是硬校验(B3 评审 P1):worthiness=none 不调模型直接空集,
    模型多给/少给都翻 error 态——值得度不被提示词文本架空。
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
        part_note = (
            "题分两部分:Part 1 两道项目概况题(anchor=overview 与 "
            "tech_rationale,part=1);"
            if deep
            else "Part 1 一道项目概况题(anchor=overview,part=1);"
        )
        prompt_text = (
            load_prompt(PROMPT_FILE)
            + "\n\n---\n\n候选人材料:\n"
            + state["project_text"]
            + "\n\n仓库材料:\n"
            + brief
            + f"\n\n{part_note}其余为 Part 2 模块分析题(anchor=module_design/"
            "tradeoff/edge_case,part=2)。"
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
        # 生成 + strict 后置校验,不合规时带原因纠正重试一次(B3 评审同款纪律)
        paths = set(state.get("paths", []))
        paths_truncated = bool(state.get("paths_truncated"))
        result: QuestionSet | None = None
        last_err: ValueError | None = None
        corrective = ""
        for _attempt in range(2):
            resp = await model.ainvoke(
                [HumanMessage(content=prompt_text + corrective)]
            )
            raw = resp.content
            if isinstance(raw, list):
                raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
            content = raw if isinstance(raw, str) else str(raw)
            try:
                result = QuestionSet.model_validate_json(_extract_json(content))
                # strict 后置校验(P1 同款):题数/锚/路径/两部分四重一致性
                if deep and len(result.questions) != count:
                    raise ValueError(
                        f"题数不符:要求 {count},模型给 {len(result.questions)}"
                    )
                if not deep and len(result.questions) > 2:
                    raise ValueError(f"引导题超量:{len(result.questions)}")
                if deep:
                    if result.repo_overview is None:
                        raise ValueError("缺 repo_overview(AI 初判)")
                    parts = {q.part for q in result.questions}
                    if parts != {1, 2}:
                        raise ValueError(f"两部分缺一:parts={sorted(parts)}")
                for q in result.questions:
                    if deep:
                        if not q.evidence.path:
                            raise ValueError(
                                f"deep_dive 题缺 evidence.path:{q.question[:30]!r}"
                            )
                        # 树被 GitHub 截断时白名单不完整,放宽成员校验(评审 P2)
                        if not paths_truncated and q.evidence.path not in paths:
                            raise ValueError(
                                f"evidence.path 不在仓内:{q.evidence.path!r}"
                            )
                        if q.anchor == "guided":
                            raise ValueError("deep_dive 题不得用 guided 锚")
                    elif q.evidence.path:
                        raise ValueError("guided 题不应带仓路径")
                last_err = None
                break
            except ValueError as ve:
                last_err = ve
                result = None
                corrective = (
                    f"\n\n【纠正】你上一次的输出不合规:{ve}"
                    "\n请修正后重新输出完整 JSON(evidence.path 必须是上面"
                    "文件结构里逐字真实的路径;deep_dive 题分 Part 1/2 两部分)。"
                )
        if result is None or last_err is not None:
            raise ValueError(f"两次输出均不合规:{last_err}")
        return {"question_set": _dump(result, deep), "error": None}
    except Exception as exc:  # noqa: BLE001 — 失败进 error 态,B2 可重试
        return {"question_set": None, "error": f"{type(exc).__name__}: {exc}"}


def _dump(result: QuestionSet, deep: bool) -> dict[str, Any]:
    result.mode = "repo_deep_dive" if deep else "guided"
    result.prompt_version = _prompt_version()
    data = result.model_dump()
    if result.repo_overview:
        data["repo_overview"] = result.repo_overview.model_dump()
    return data


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
    github_base: str = "https://api.github.com",
    github_token: str = "",
) -> dict:
    """便捷入口:返回题集 dict(questions 可为空=skip/降级);LLM 失败抛 RuntimeError。"""
    graph = build_investigation_subgraph()
    final: InvestigationState = await graph.ainvoke(
        {
            "project_text": project_text,
            "github_base": github_base,
            "github_token": github_token,
        }
    )
    if final.get("error") or final.get("question_set") is None:
        raise RuntimeError(f"调查子图失败:{final.get('error')}")
    return final["question_set"]
