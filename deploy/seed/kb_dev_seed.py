"""B/RAG 本地知识库种子(幂等):把 5 条社团/招新知识灌入 Agent PG 的 KB。

⚠️ 仅限本地 dev:直接写本地 .env 指向的 official_agent 库;生产 KB 由
管理面板人工录入,绝不能用本脚本灌。

前置:.env 配置 POSTGRES_URL(5433)与 EMBED_*(真实 embedding,入库即调用)。
用法:cd Official_Web_Agent && uv run python deploy/seed/kb_dev_seed.py
幂等:按 source_title 判重,已存在则跳过(重跑安全)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import psycopg  # noqa: E402

from official_agent.config import get_settings  # noqa: E402
from official_agent.kb.store import SourceInput, ingest_source  # noqa: E402

SEEDS = [
    {
        "title": "社团介绍",
        "type": "doc",
        "kind": "normal",
        "tags": ["社团"],
        "content_md": (
            "# 博远信息技术社\n"
            "博远信息技术社是华东师范大学的学生科技社团,面向全校本科生招新,"
            "社员以计算机相关专业为主,也欢迎其他专业的技术爱好者。\n\n"
            "## 社团宗旨\n以项目实践带动学习,每学年围绕真实产品迭代。\n\n"
            "## 活动形式\n常规活动包括:每周技术分享会、学期项目组开发、"
            "校内联合比赛、面向新手的编程入门工作坊。"
        ),
    },
    {
        "title": "部门介绍",
        "type": "doc",
        "kind": "normal",
        "tags": ["部门"],
        "content_md": (
            "# 部门设置\n博远信息技术社下设三个部门:\n\n"
            "## 技术部\n负责社团官网、小程序等产品的设计与开发,"
            "技术栈以 React/TypeScript 与 Python 为主。"
            "技术部成员需要参与每学期的项目组开发。\n\n"
            "## 宣传部\n负责社团公众号运营、活动海报设计与招新宣传物料制作。\n\n"
            "## 组织部\n负责活动策划与执行、场地预约、社员管理以及校企联络。"
        ),
    },
    {
        "title": "招新流程 FAQ",
        "type": "faq",
        "kind": "normal",
        "tags": ["招新"],
        "question": "怎么加入博远信息技术社?招新流程是什么?",
        "answer": (
            "每年九月开学季开放招新:1) 在官网填写报名表(基本信息+志愿部门);"
            "2) 技术部候选人完成编程评测小任务;3) 参加部门面试(约 15 分钟);"
            "4) 结果通过官网与邮件通知。其他部门以简历审核+面试为主。"
        ),
    },
    {
        "title": "常见问题 FAQ",
        "type": "faq",
        "kind": "normal",
        "tags": ["FAQ"],
        "question": "加入社团需要什么基础?零基础可以吗?",
        "answer": (
            "零基础可以加入。社团面向新手开设编程入门工作坊,"
            "宣传部和组织部门对编程没有要求;"
            "技术部希望候选人对编程有兴趣并愿意投入时间学习。"
        ),
    },
    {
        "title": "面试安排 FAQ",
        "type": "faq",
        "kind": "normal",
        "tags": ["面试"],
        "question": "面试是什么形式?会问什么?",
        "answer": (
            "面试约 15 分钟,线下进行。技术部面试会聊你填写的项目经历和"
            "编程评测中的作答;非技术部门主要聊你的经历和对部门工作的理解。"
            "面试官使用统一的预置题库,也会自由追问。"
        ),
    },
]


def main() -> int:
    seeded = 0
    for s in SEEDS:
        conn = psycopg.connect(get_settings().postgres_url)
        exists = False
        with conn:
            row = conn.execute(
                "SELECT source_id FROM kb_source WHERE source_title = %s",
                (s["title"],),
            ).fetchone()
            exists = row is not None
        conn.close()
        if exists:
            print(f"跳过(已存在):{s['title']}")
            continue
        payload = {k: v for k, v in s.items() if k not in ("question", "answer", "content_md")}
        if s["type"] == "faq":
            payload["question"] = s["question"]
            payload["answer"] = s["answer"]
        else:
            payload["content_md"] = s["content_md"]
        source_id = asyncio_run_ingest(payload)
        print(f"已入库:{s['title']} → {source_id}")
        seeded += 1
    print(f"完成:新入库 {seeded} 条,跳过 {len(SEEDS) - seeded} 条")
    return 0


def asyncio_run_ingest(payload: dict) -> str:
    import asyncio

    from official_agent.kb.store import SourceInput, ingest_source

    source = SourceInput(
        title=payload["title"],
        type=payload["type"],
        kind=payload.get("kind", "normal"),
        tags=tuple(payload.get("tags", ())),
        question=payload.get("question", ""),
        answer=payload.get("answer", ""),
        content_md=payload.get("content_md", ""),
        updated_by="local-seed",
    )
    return asyncio.run(ingest_source(source))


if __name__ == "__main__":
    sys.exit(main())
