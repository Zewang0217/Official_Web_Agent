"""B4 测试:错因归类/最近最好/奖项降级/兜底题组/bundle 组装(fakes)。"""

import logging

import pytest

from official_agent.evaluation import autograding as ag
from official_agent.evaluation import awards
from official_agent.evaluation import bundle as bd
from official_agent.evaluation.autograding import TestFailure
from official_agent.evaluation.scoring import FieldText

# ── autograding 纯逻辑 ──────────────────────────────────


def test_pick_latest_best_prefers_score_then_recency() -> None:
    subs = [
        {"id": 1, "total_score": 70, "submitted_at": "2026-09-08"},
        {"id": 2, "total_score": 90, "submitted_at": "2026-09-01"},  # 最高分
        {"id": 3, "total_score": 90, "submitted_at": "2026-09-07"},  # 同分取最近
    ]
    assert ag.pick_latest_best(subs)["id"] == 3
    assert ag.pick_latest_best([]) is None


def test_extract_and_classify_failures() -> None:
    submission = {
        "tasks": [
            {
                "task_name": "task1",
                "test_results": [
                    {"name": "test_login_ok", "passed": True},
                    {"name": "test_query_timeout", "passed": False},
                    {"name": "test_edge_empty_input", "passed": False},
                    {"name": "test_calc_total", "passed": False},
                ],
            },
            {
                "task_name": "task5",
                "test_results": [{"name": "env_setup_error", "passed": False}],
            },
        ]
    }
    failures = ag.extract_failures(submission)
    assert {f.test_name for f in failures} == {
        "test_query_timeout",
        "test_edge_empty_input",
        "test_calc_total",
        "env_setup_error",
    }
    buckets = ag.classify(failures)
    # 桶值保留 (任务名, test 名)——#132 evidence 契约(B4 评审 P1)
    assert buckets["timeout"] == [("task1", "test_query_timeout")]
    assert buckets["boundary"] == [("task1", "test_edge_empty_input")]
    assert buckets["environment"] == [("task5", "env_setup_error")]
    assert buckets["logic"] == [("task1", "test_calc_total")]


def test_boundary_keyword_wins_over_error() -> None:
    """test_edge_error_* 这类命名应落边界桶而非环境桶(关键词序)。"""
    buckets = ag.classify([TestFailure(task="t1", test_name="test_edge_error_case")])
    assert buckets["boundary"] == [("t1", "test_edge_error_case")]


def test_full_score_skips_lane() -> None:
    assert ag.is_full_score({"total_score": 100, "max_total_score": 100})
    assert not ag.is_full_score({"total_score": 80, "max_total_score": 100})
    assert not ag.is_full_score({"total_score": None, "max_total_score": None})


# ── 奖项线(检查点⑤:NullProvider 不可考路径) ────────────


async def test_award_brief_unverifiable_without_search() -> None:
    brief = await awards.build_award_brief(awards.NullSearchProvider(), "国家奖学金")
    assert brief["status"] == "unverifiable"
    assert "不可考" in brief["background"]
    # 追问仍存在且纯过程(不考含金量)
    assert brief["questions"] and "作品/角色" in brief["questions"][0]["question"]


class _FakeSearch:
    async def search(self, query):
        return [{"snippet": "ACM 主办,国际赛事"}]


async def test_award_brief_verified_with_search() -> None:
    brief = await awards.build_award_brief(_FakeSearch(), "ACM 亚洲区银牌")
    assert brief["status"] == "verified"
    assert "ACM" in brief["background"]


def test_extract_awards_scans_award_fields() -> None:
    fields = [
        FieldText(field_key="award_history", title="获奖经历", value="- 蓝桥杯省一\n- 校奖学金"),
        FieldText(field_key="self_intro", title="自我介绍", value="我是张三。"),
    ]
    assert awards.extract_awards(fields) == ["蓝桥杯省一", "校奖学金"]


def test_base_three_and_plan() -> None:
    base = awards.base_three_questions()
    assert len(base) == 3  # 基础三维(#133)
    assert all(q["evidence"]["path"] == "" for q in base)
    plan = awards.suggest_plan(base + base, budget_minutes=15)
    assert sum(plan) <= 15  # 建议组合不超 15 分钟


# ── bundle 组装(repo skip + 兜底线) ─────────────────────


class _FakeModel:
    """返回部门技能题组 JSON(兜底线唯一 LLM 调用)。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1

        class _Msg:
            content = (
                '{"questions": ['
                '{"anchor": "guided", "question": "React 里 useState 和 useRef 区别?",'
                '"sub_prompts": [],'
                '"answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},'
                '"evidence": {"path": "", "note": "技术部技能题"},'
                '"time_minutes": 3},'
                '{"anchor": "guided", "question": "Git 协作分支怎么管理?",'
                '"sub_prompts": [],'
                '"answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},'
                '"evidence": {"path": "", "note": "技术部技能题"},'
                '"time_minutes": 3}]}'
            )

        return _Msg()


@pytest.mark.asyncio
async def test_bundle_fallback_for_no_evidence(monkeypatch) -> None:
    """无证据候选:仓线 skip + 无评测无奖项 → 基础三维+技能题组(#133)。"""
    fields = [
        FieldText(field_key="self_intro", title="自我介绍", value="我是李四。"),
        FieldText(field_key="dept", title="志愿部门", value="技术部"),
    ]

    async def _no_submission(github_key):
        return None  # 无评测记录 → 评测线不产出

    async def _skip_investigation(text, **kw):
        return {"mode": "skipped", "repo_summary": "", "questions": []}

    fake_model = _FakeModel()
    monkeypatch.setattr(bd.ig, "run_investigation", _skip_investigation)
    monkeypatch.setattr(
        "official_agent.evaluation.autograding.fetch_latest_submission",
        _no_submission,
    )
    monkeypatch.setattr(bd, "build_model", lambda *a, **k: fake_model)

    envelope = await bd.run_bundle(
        fields, resume_id=9, cycle_id=2026, github_key="usergithub"
    )
    groups = {g["group"]: g for g in envelope["groups"]}
    assert groups["repo"]["mode"] == "skipped"
    assert "autograding" not in groups  # 无评测记录 → 线不存在
    fallback = groups["base_and_skills"]
    assert len(fallback["questions"]) >= 3 + 2  # 基础三维 + 技能题组
    assert envelope["total_questions"] == len(fallback["questions"])
    assert sum(envelope["suggested_plan"]) <= 15
    assert fake_model.calls == 1  # 兜底线只调一次 LLM(技能题组)


def test_ddg_provider_search_and_degrade(monkeypatch) -> None:
    """DDG 提供方:正常出结构化结果;任何失败降级 [](→奖项不可考),不抛错。"""
    from official_agent.evaluation.awards import DuckDuckGoProvider

    provider = DuckDuckGoProvider()

    class _FakeDDGS:
        def __init__(self, timeout=10):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query, max_results=5):
            return [
                {"title": "蓝桥杯", "body": "教育部指导赛事", "href": "https://example.com"}
            ]

    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
    results = provider._search_sync("蓝桥杯", logging.getLogger("t"))
    assert results == [
        {"title": "蓝桥杯", "snippet": "教育部指导赛事", "url": "https://example.com"}
    ]

    class _BoomDDGS(_FakeDDGS):
        def text(self, query, max_results=5):
            raise RuntimeError("unreachable")

    monkeypatch.setattr("ddgs.DDGS", _BoomDDGS)
    assert provider._search_sync("x", logging.getLogger("t")) == []


def test_bundle_default_provider_is_ddg() -> None:
    """用户拍板:web_search 先用 DDG(bundle 缺省提供方)。"""
    import inspect

    src = inspect.getsource(bd.run_bundle)
    assert "DuckDuckGoProvider()" in src
