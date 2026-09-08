"""GitHub 只读客户端(B3,#130):contents API 深挖仓,不 clone、不写。

- 只读三件事:仓库元数据 / README 原文 / 提交历史(+ tree 文件清单)
- base_url 可注入(respx 测试);GITHUB_TOKEN 可选(检查点④:批量跑再配,
  匿名 60 次/时限流对单候选深挖足够)
- 私有/不可达统一抛 GitHubUnavailable(路由降级为通用引导题,#130)
"""

from __future__ import annotations

import httpx

_DEFAULT_BASE = "https://api.github.com"


class GitHubUnavailable(RuntimeError):
    """仓库不可读(私有/不存在/限流/网络)——路由降级信号,不是错误。"""


class GitHubClient:
    def __init__(self, base_url: str = _DEFAULT_BASE, token: str = "") -> None:
        self._base = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def repo(self, owner: str, repo: str) -> dict:
        """仓库元数据:size/stars/language/default_branch/pushed_at。"""
        data = await self._get_json(f"/repos/{owner}/{repo}")
        return data if isinstance(data, dict) else {}

    async def readme(self, owner: str, repo: str) -> str:
        """README 原文(raw);没有 README → 空串(值得度扣分项,不报错)。"""
        url = f"{self._base}/repos/{owner}/{repo}/readme"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                url,
                headers={**self._headers(), "Accept": "application/vnd.github.raw+json"},
            )
        if resp.status_code == 404:
            return ""
        if resp.status_code != 200:
            raise GitHubUnavailable(f"README {resp.status_code}")
        return resp.text

    async def commits(self, owner: str, repo: str, *, per_page: int = 30) -> list[dict]:
        """最近提交(message/date);空仓返回 []。"""
        data = await self._get_json(
            f"/repos/{owner}/{repo}/commits", params={"per_page": per_page}
        )
        return [
            {
                "message": item.get("commit", {}).get("message", ""),
                "date": item.get("commit", {}).get("author", {}).get("date", ""),
            }
            for item in data
        ]

    async def tree_paths(self, owner: str, repo: str, *, limit: int = 400) -> list[str]:
        """默认分支文件路径清单(truncated 时截断到 limit)。"""
        meta = await self.repo(owner, repo)
        branch = meta.get("default_branch") or "main"
        data = await self._get_json(
            f"/repos/{owner}/{repo}/git/trees/{branch}", params={"recursive": "1"}
        )
        if not isinstance(data, dict):
            return []
        paths = [
            item["path"]
            for item in data.get("tree", [])
            if item.get("type") == "blob"
        ]
        return paths[:limit]

    async def _get_json(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise GitHubUnavailable(f"GitHub 连接失败:{exc}") from exc
        if resp.status_code in (401, 403, 404):
            # 403 含限流;对路由而言都等价于"此仓不可读"
            raise GitHubUnavailable(f"GitHub {resp.status_code}")
        if resp.status_code != 200:
            raise GitHubUnavailable(f"GitHub {resp.status_code}")
        return resp.json()
