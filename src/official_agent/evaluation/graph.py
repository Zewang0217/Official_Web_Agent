"""评分子图(B1,#123/#126):precheck(绝对卡) → 条件分支 → llm_score → finalize。

- 一总图两子图中的「评分子图」;调查子图(B3)与它平行
- 确定性规则优先:任一打分维命中绝对卡 → 整份硬 0,不调模型(省钱+可测)
- LLM 轨:model_strong + 低温 0.1 + 结构化输出(schema.py 契约,
  function-calling 轨——openai-compatible 端点兼容,A 模块工具调用同轨)
- 输出是**卡 dict**(schema evaluation_scorecard/v1),落库由调用方
  (state/evaluation.py,B2 接线)负责;本图纯计算无 DB IO
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

from official_agent.config import get_effective_settings
from official_agent.evaluation.schema import ScorecardOutput
from official_agent.evaluation.scoring import (
    FieldText,
    detect_hard_zero,
    weighted_total,
)
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt, load_prompt_meta

PROMPT_FILE = "evaluation_scoring.md"
CARD_SCHEMA_VERSION = "evaluation_scorecard/v1"
SCORING_TEMPERATURE = 0.1


def _prompt_version() -> str:
    """prompt frontmatter 的 version(ADR-0004:文件是唯一权威)。"""
    return load_prompt_meta(PROMPT_FILE).get("version", "unknown")


class EvaluationState(TypedDict, total=False):
    """子图状态。fields 元素:{field_key,title,value}(plain dict,可序列化)。"""

    resume_id: int
    cycle_id: int
    fields: list[dict[str, str]]
    weights: dict[str, float]
    hard_zero: bool
    hard_zero_reasons: dict[str, str]
    card: dict[str, Any]
    error: str | None


def _as_field_texts(fields: list[dict[str, str]]) -> list[FieldText]:
    return [
        FieldText(
            field_key=str(f["field_key"]),
            title=str(f.get("title", "")),
            value=str(f.get("value", "")),
        )
        for f in fields
    ]


def _extract_json(text: str) -> str:
    """从模型回复中截取 JSON 主体(容忍代码围栏/前后杂文)。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"模型回复不含 JSON:{cleaned[:120]}")
    return cleaned[start : end + 1]


async def precheck(state: EvaluationState) -> dict:
    """绝对卡确定性短路:任一维命中 → 整份硬 0(#123)。"""
    reasons = detect_hard_zero(_as_field_texts(state["fields"]))
    return {"hard_zero": bool(reasons), "hard_zero_reasons": reasons}


def route_after_precheck(state: EvaluationState) -> str:
    return "finalize_hard" if state.get("hard_zero") else "llm_score"


async def finalize_hard(state: EvaluationState) -> dict:
    """硬 0 卡:全部打分维 0 分,依据=命中原因,不调模型。"""
    reasons = state.get("hard_zero_reasons", {})
    settings = get_effective_settings()
    dimensions = [
        {
            "field_key": f["field_key"],
            "score": 0,
            "rationale": f"态度不端硬 0:{reasons.get(f['field_key'], '命中绝对卡规则')}",
            "evidence": (f.get("value") or "")[:80],
        }
        for f in state["fields"]
    ]
    card = {
        "schema": CARD_SCHEMA_VERSION,
        "resume_id": state["resume_id"],
        "cycle_id": state["cycle_id"],
        "dimensions": dimensions,
        "attitude": {
            "verdict": "bad_faith",
            "reason": "确定性绝对卡短路:" + ";".join(sorted(reasons.values())),
        },
        "total": 0.0,
        "hard_zero": True,
        "hard_zero_reasons": reasons,
        "versions": {
            "prompt": _prompt_version(),
            "weights": "cycle-config",
            "model": settings.model_strong,
        },
    }
    return {"card": card, "error": None}


async def llm_score(state: EvaluationState) -> dict:
    """结构化打分:逐维给分+依据+原文证据,态度判定;异常进 error(B2 可重试)。"""
    settings = get_effective_settings()
    try:
        model = build_model(settings, temperature=SCORING_TEMPERATURE)
        # 结构化输出轨(检查点③实测拍板):当前代理的模型全是思考模式,
        # json_schema response_format 与强制 tool_choice 均被拒(400)——
        # 落到提示词 JSON 轨:模型输出 JSON 文本,strict Pydantic 校验
        # (schema.py extra=forbid)兜住形状;解析失败进 error 态由 B2 重试
        blocks = [
            "### "
            + (f.get("title") or f["field_key"])
            + f" (field_key={f['field_key']})\n{f.get('value', '')}"
            for f in state["fields"]
        ]
        prompt_text = (
            load_prompt(PROMPT_FILE)
            + "\n\n---\n\n简历各维原文:\n\n"
            + "\n\n".join(blocks)
            + "\n\n只输出符合上述 schema 的 JSON 对象,不要任何其他文字或代码围栏。"
            + "\nfield_key 取值必须是:"
            + ",".join(f["field_key"] for f in state["fields"])
        )
        resp = await model.ainvoke([HumanMessage(content=prompt_text)])
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        result = ScorecardOutput.model_validate_json(_extract_json(content))
        scores = {d.field_key: d.score for d in result.dimensions}
        card = {
            "schema": CARD_SCHEMA_VERSION,
            "resume_id": state["resume_id"],
            "cycle_id": state["cycle_id"],
            "dimensions": [d.model_dump() for d in result.dimensions],
            "attitude": result.attitude.model_dump(),
            "total": weighted_total(scores, state.get("weights", {})),
            "hard_zero": False,
            "hard_zero_reasons": {},
            "versions": {
                "prompt": _prompt_version(),
                "weights": "cycle-config",
                "model": settings.model_strong,
            },
        }
        return {"card": card, "error": None}
    except Exception as exc:  # noqa: BLE001 — 失败进 error 态,B2 任务可重试
        return {"card": None, "error": f"{type(exc).__name__}: {exc}"}


async def finalize(state: EvaluationState) -> dict:
    """透传到终态(卡已在 llm_score 组装;单节点占位便于 B2 挂钩/审计)。"""
    return {}


def build_evaluation_subgraph() -> Any:
    """评分子图:START → precheck →(绝对卡? finalize_hard : llm_score)→ finalize → END。"""
    g = StateGraph(EvaluationState)
    g.add_node("precheck", precheck)
    g.add_node("llm_score", llm_score)
    g.add_node("finalize_hard", finalize_hard)
    g.add_node("finalize", finalize)
    g.set_entry_point("precheck")
    g.add_conditional_edges("precheck", route_after_precheck)
    g.add_edge("llm_score", "finalize")
    g.add_edge("finalize_hard", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


async def run_evaluation(
    fields: list[dict[str, str]],
    *,
    resume_id: int,
    cycle_id: int,
    weights: dict[str, float] | None = None,
) -> dict:
    """便捷入口:跑完整子图,返回卡 dict;LLM 失败抛 RuntimeError(B2 落 job 失败)。"""
    graph = build_evaluation_subgraph()
    final: EvaluationState = await graph.ainvoke(
        {
            "resume_id": resume_id,
            "cycle_id": cycle_id,
            "fields": fields,
            "weights": weights or {},
        }
    )
    if final.get("error") or not final.get("card"):
        raise RuntimeError(f"评分子图失败:{final.get('error')}")
    return final["card"]
