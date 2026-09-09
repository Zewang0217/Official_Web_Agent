"""评分输出契约(B1,#123):仓库首个 strict Pydantic 结构化输出。

strict 语义:extra="forbid" + 字段约束。输出轨为提示词 JSON + 本 schema
校验(检查点③实测:当前代理模型全为思考模式,json_schema response_format
与强制 tool_choice 都被 400 拒)——schema 即提示词的一部分,字段名/枚举值
改一个字模型行为就变,所以本文件是 prompt 级资产。

维度分 0-100(内部锚),总分是派生值(代码加权),不由模型给。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DimensionScore(BaseModel):
    """单维评分:分 + 依据 + 原文证据句。

    evidence 必须是该维 textarea 的原文片段(句级,#123)——评审复核的锚,
    模型编造证据时评委可直接对照简历打回。
    """

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(min_length=1, description="简历字段键(周期配置驱动)")
    score: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, description="为什么给这个分")
    evidence: str = Field(
        min_length=1, max_length=120, description="该维原文句,逐字引用"
    )


class AttitudeVerdict(BaseModel):
    """态度结论:端正(sincere)/敷衍(perfunctory,各维压低)/不端(bad_faith,整份 0)。"""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["sincere", "perfunctory", "bad_faith"]
    reason: str = Field(min_length=1)


class ScorecardOutput(BaseModel):
    """模型结构化输出整体;与确定性规则合并后落 evaluation_scorecard 卡。"""

    model_config = ConfigDict(extra="forbid")

    dimensions: list[DimensionScore] = Field(min_length=1)
    attitude: AttitudeVerdict


class QuestionEvidence(BaseModel):
    """证据锚:仓内可点路径(deep_dive 必填);无仓引导题留空+note 说明。"""

    model_config = ConfigDict(extra="forbid")

    path: str = ""
    note: str = ""


class AnswerReference(BaseModel):
    """参考答案三锚(#125/#127):面试官据此判断答得算好/达标/弱。"""

    model_config = ConfigDict(extra="forbid")

    strong: str = Field(min_length=1)
    acceptable: str = Field(min_length=1)
    weak: str = Field(min_length=1)


class InterviewQuestion(BaseModel):
    """单道预置面试题(envelope 核心;B5 qbank 落库的最小单元)。

    part:1=项目概况(是什么/技术选型),2=模块分析(设计哲学/权衡/边界)。
    锚点采用中性问法(不预设候选人自述):「为什么选取这个技术栈」
    「这个模块为什么这么设计」——而非「你自述了 X」式先入为主问法
    (用户 2026-09-09 反馈)。
    """

    model_config = ConfigDict(extra="forbid")

    part: Literal[1, 2] = 2
    anchor: Literal[
        "overview", "tech_rationale", "module_design", "tradeoff", "edge_case", "guided"
    ]
    question: str = Field(min_length=1)
    sub_prompts: list[str] = Field(default_factory=list, max_length=5)
    answer_reference: AnswerReference
    evidence: QuestionEvidence
    time_minutes: int = Field(default=3, ge=2, le=5)


class RepoOverview(BaseModel):
    """AI 对仓库的初步评判(给面试官看,不问候选人)。"""

    model_config = ConfigDict(extra="forbid")

    what: str = Field(min_length=1, description="这个项目是什么、解决什么问题")
    tech_stack: str = Field(min_length=1, description="实际技术栈(以仓库为准)")
    structure_note: str = Field(description="目录/文件结构概览")
    highlights: list[str] = Field(default_factory=list, max_length=5)
    risks: list[str] = Field(default_factory=list, max_length=5)
    ai_assessment: str = Field(min_length=1, description="AI 初判:值得深挖的点与原因")


class QuestionSet(BaseModel):
    """一次调查产出的题集;questions 空 = skip/零信号(合法,#130)。

    两部分结构(用户 2026-09-09 反馈):
    - Part 1 项目概况:是什么/为什么做/为什么选这个技术栈
    - Part 2 模块分析:锚定真实文件,追问「为什么这么设计/什么设计哲学」
    mode/prompt_version 是信封字段(非模型输出,生成后注入)。
    """

    model_config = ConfigDict(extra="forbid")

    repo_overview: RepoOverview | None = None
    questions: list[InterviewQuestion] = Field(default_factory=list, max_length=8)
    mode: Literal["repo_deep_dive", "guided", "skipped"] | None = None
    prompt_version: str = ""
