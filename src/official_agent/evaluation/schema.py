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
    evidence: str = Field(min_length=1, description="该维原文句,逐字引用")


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
