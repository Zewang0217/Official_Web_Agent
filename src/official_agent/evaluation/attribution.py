"""入口瀑布与归属四级(B-AG2,#150;spec §3.1 + ADR-0008)。

瀑布(D2,只深挖简历点名的项目):
  1. 简历文本含 github.com/owner/repo → 直配(source="url")
  2. 绑定登录名 → 在其名下仓中按关键词匹配(source="bound")
  3. GitHub Search 按项目名搜(source="search")
  4. 全部落空 → None(由 route_project 决定 guided/skip)

归属四级(ADR-0008,只增不改):
  - trusted-own            owner == 绑定登录名 → 全量深挖
  - trusted-contribution   仓内查到 author=本人的 commits 或 PR(D3:fork 不要求
                           领先父仓)→ 深挖,题锚定具体贡献
  - claimed                简历 URL/贡献声明,本人自述未核对 → 深挖,信封标注
  - unverified             搜索命中且无归属证据 → **不深挖,仅 guided**

被否掉的核对手段(ADR-0008):author email(PII 纪律)、姓名署名(重名无信号)、
fork 领先父仓(与是否参与无关)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from official_agent.evaluation.github_client import GitHubClient, GitHubUnavailable
from official_agent.evaluation.investigate import extract_repos

AttributionLevel = Literal["trusted-own", "trusted-contribution", "claimed", "unverified"]

# 贡献声明:给 xx/yy(仓)贡献 / 向 xx/yy 提了 PR / contribute to owner/repo
_CONTRIB_RE = re.compile(
    r"(?:给|向|为)\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)|(?:contribute[ds]?\s+to\s+"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RepoAttribution:
    """一个仓的归属判定结果;evidence 写进信封给面试官看(ADR-0008)。"""

    owner: str
    name: str
    level: AttributionLevel
    evidence: str
    source: str  # url | bound | search | contribution

    @property
    def deep_dive_allowed(self) -> bool:
        """unverified 绝不产出仓锚定题(ADR-0008);贡献声明类 claimed 只出
        过程题不做仓内深挖(D4)——URL 类 claimed 仍深挖、信封标注。"""
        if self.level == "unverified":
            return False
        return not (self.level == "claimed" and self.source == "contribution")


def detect_contribution_target(text: str) -> tuple[str, str] | None:
    """从贡献声明里提取目标仓(owner/repo);「给 xx 仓做贡献」是一等调查对象(D4)。"""
    match = _CONTRIB_RE.search(text or "")
    if not match:
        return None
    ref = match.group(1) or match.group(2)
    owner, _, name = ref.partition("/")
    name = re.sub(r"[.,;:)!?}\],。;]+$", "", name)
    if not owner or not name:
        return None
    return owner, name


def keyword_candidates(text: str) -> list[str]:
    """从项目自述提取搜索/匹配关键词(确定性,不做 LLM 判断)。

    取:书名号/引号包住的词 + 拉丁词元(≥3 字符)。纯 CJK 自述提不出拉丁
    关键词时返回空——瀑布第 2/3 步静默跳过,走 guided,不硬猜。"""
    kws: list[str] = []
    for quoted in re.findall(r"[《「【\"']([^》」】\"']{2,40})[》」】\"']", text or ""):
        kws.append(quoted.strip())
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text or ""):
        if token.lower() not in {"github", "http", "https", "com", "www"}:
            kws.append(token)
    return kws


def _repo_matches(row: dict, keywords: list[str]) -> bool:
    """仓名/描述命中任一关键词(大小写不敏感)。"""
    hay = f"{row.get('name', '')} {row.get('description', '')}".lower()
    return any(kw.lower() in hay for kw in keywords)


async def _contribution_evidence(
    client: GitHubClient, owner: str, name: str, login: str
) -> str:
    """author=login 的 commits/PR 证据;查到返回描述串,没查到/不可读返回空。

    commits 与 PR 任一命中即算(D4/ADR-0008);查询失败按无证据处理——
    降级语义,不炸瀑布。"""
    try:
        commits = await client.read_commits(owner, name, author=login, per_page=5)
        if commits:
            return f"commits author={login} ×{len(commits)}"
    except GitHubUnavailable:
        pass
    try:
        prs = await client.search_issues(owner, name, f"author:{login}", is_pr=True, per_page=5)
        if prs:
            return f"PR author={login} ×{len(prs)}"
    except GitHubUnavailable:
        pass
    return ""


async def attribute(
    owner: str,
    name: str,
    *,
    login: str,
    source: str,
    client: GitHubClient,
) -> RepoAttribution:
    """对已定位的仓做四级归属判定(ADR-0008)。"""
    if login and owner.lower() == login.lower():
        return RepoAttribution(
            owner, name, "trusted-own", f"owner==绑定登录名 {login}", source
        )
    if login:
        evidence = await _contribution_evidence(client, owner, name, login)
        if evidence:
            return RepoAttribution(owner, name, "trusted-contribution", evidence, source)
    if source == "url":
        return RepoAttribution(owner, name, "claimed", "简历 URL 自述,未核对", source)
    if source == "contribution":
        reason = (
            "贡献声明,未绑定无法核对,仅过程题"
            if not login
            else "贡献声明,绑定但未查到 commits/PR"
        )
        return RepoAttribution(owner, name, "claimed", reason, source)
    return RepoAttribution(owner, name, "unverified", "搜索命中,无归属证据", source)


async def resolve_entry(
    project_text: str,
    *,
    login: str,
    client: GitHubClient,
) -> RepoAttribution | None:
    """入口瀑布(spec §3.1):URL 直配 → 绑定匹配 → 搜索兜底 → None。

    None = 无仓位置,调用方按既有 route_project 走 guided/skip;
    GitHubUnavailable 向上抛(不可读 → 调用方降级 guided,spec §3.4)。"""

    # 1. 简历 URL 直配
    urls = extract_repos(project_text)
    if urls:
        owner, name = urls[0]
        return await attribute(owner, name, login=login, source="url", client=client)

    keywords = keyword_candidates(project_text)
    if not keywords:
        return None

    # 2. 绑定登录名下按关键词匹配
    if login:
        rows = await client.list_user_repos(login)
        for row in rows:
            if _repo_matches(row, keywords):
                return await attribute(
                    row["owner_login"], row["name"], login=login, source="bound", client=client
                )

    # 3. GitHub Search 按项目名搜(撞名 → unverified,不深挖)
    for kw in keywords:
        hits = await client.search_repos(kw)
        for row in hits:
            if _repo_matches(row, keywords):
                return await attribute(
                    row["owner_login"], row["name"], login=login, source="search", client=client
                )
    return None
