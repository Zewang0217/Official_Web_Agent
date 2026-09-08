"""B1 评分子图测试:硬 0 短路不调模型 / 正常路径结构化输出 / 失败进 error。"""

from unittest.mock import patch

import pytest

from official_agent.evaluation import graph as ev

_FIELDS = [
    {"field_key": "intro", "title": "自我介绍", "value": "我是张三,做过两个 Web 项目。"},
    {"field_key": "reason", "title": "加入理由", "value": "认同社团氛围,想参与招新开发。"},
]

_WEIGHTS = {"intro": 3.0, "reason": 1.0}


def _fields_all_bad() -> list[dict[str, str]]:
    return [
        {"field_key": "intro", "title": "自我介绍", "value": "111"},
        {"field_key": "reason", "title": "加入理由", "value": "请输入加入理由"},
    ]


class _FakeMsg:
    def __init__(self, content):
        self.content = content


def _fake_model(payload: str):
    class _M:
        async def ainvoke(self, messages):
            return _FakeMsg(payload)

    return _M()


_GOOD_JSON = (
    '{"dimensions": ['
    '{"field_key": "intro", "score": 80, "rationale": "具体", "evidence": "做过两个 Web 项目"},'
    '{"field_key": "reason", "score": 40, "rationale": "偏短", "evidence": "认同社团氛围"}],'
    '"attitude": {"verdict": "sincere", "reason": "认真"}}'
)


def _settings():
    class _S:
        model_strong = "test-strong"

    return _S()


@pytest.mark.asyncio
async def test_hard_zero_short_circuits_without_model() -> None:
    """任一维绝对卡 → 整份硬 0,不调模型(#123 确定性规则优先)。"""

    def _boom(*a, **k):
        raise AssertionError("硬 0 路径不得调模型")

    with (
        patch.object(ev, "build_model", _boom),
        patch.object(ev, "get_effective_settings", _settings),
    ):
        card = await ev.run_evaluation(
            _fields_all_bad(), resume_id=1, cycle_id=2026, weights=_WEIGHTS
        )
    assert card["hard_zero"] is True
    assert card["total"] == 0.0
    assert card["attitude"]["verdict"] == "bad_faith"
    assert all(d["score"] == 0 for d in card["dimensions"])
    assert "单字符重复" in json_of_reasons(card)
    assert card["versions"]["prompt"] == "evaluation_scoring/v1"


def json_of_reasons(card: dict) -> str:
    return str(card["hard_zero_reasons"])


@pytest.mark.asyncio
async def test_normal_path_scores_and_weights_total() -> None:
    with (
        patch.object(ev, "build_model", lambda *a, **k: _fake_model(_GOOD_JSON)),
        patch.object(ev, "get_effective_settings", _settings),
    ):
        card = await ev.run_evaluation(
            _FIELDS, resume_id=2, cycle_id=2026, weights=_WEIGHTS
        )
    assert card["hard_zero"] is False
    assert card["total"] == 70.0  # (80×3 + 40×1) / 4
    assert card["attitude"]["verdict"] == "sincere"
    assert card["dimensions"][0]["evidence"] == "做过两个 Web 项目"
    assert card["schema"] == "evaluation_scorecard/v1"


@pytest.mark.asyncio
async def test_llm_failure_lands_in_error() -> None:
    class _Boom:
        async def ainvoke(self, messages):
            return _FakeMsg("模型打摆了,没有 JSON")

    with (
        patch.object(ev, "build_model", lambda *a, **k: _Boom()),
        patch.object(ev, "get_effective_settings", _settings),
        pytest.raises(RuntimeError, match="评分子图失败"),
    ):
        await ev.run_evaluation(_FIELDS, resume_id=3, cycle_id=2026)


@pytest.mark.asyncio
async def test_temperature_low_on_scorer() -> None:
    """评分走低温档(#123:model_strong+低温)。"""
    captured: dict = {}

    def _fake_build(settings, model=None, stream_usage=False, temperature=None):
        captured["temperature"] = temperature
        return _fake_model(
            '{"dimensions": [{"field_key": "intro", "score": 50, '
            '"rationale": "r", "evidence": "e"}],'
            '"attitude": {"verdict": "sincere", "reason": "r"}}'
        )

    with (
        patch.object(ev, "build_model", _fake_build),
        patch.object(ev, "get_effective_settings", _settings),
    ):
        await ev.run_evaluation(_FIELDS[:1], resume_id=4, cycle_id=2026)
    assert captured["temperature"] == ev.SCORING_TEMPERATURE
