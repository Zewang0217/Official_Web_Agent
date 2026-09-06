"""PII 确定性脱敏(共享层,#115 review P0-3)。

规则表先于 SEC-08 契约的简单版;conversation_log 写入与工具返回层
(get_resume_detail 等 trace 暴露面)共用同一规则,SEC-08(#68)拍板后
在此统一演进。
"""

from __future__ import annotations

import re
from typing import Any

_MASK_RULES: list[tuple[re.Pattern[str], str]] = [
    # 手机号(11 位,1 开头):留前 3 后 4
    (re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    # 身份证(18 位):留前 4 后 4
    (re.compile(r"(?<!\d)(\d{4})\d{10}(\d{4})(?!\d)"), r"\1**********\2"),
    # QQ(5-11 位纯数字,词边界):全掩
    (re.compile(r"(?<!\d)\d{5,11}(?!\d)"), "*****"),
]


def mask_pii(text: str) -> str:
    """确定性 PII 脱敏:手机号留前 3 后 4、身份证留前 4 后 4、QQ 全掩。

    规则表先于 SEC-08 契约的简单版;无匹配原样返回。纯数字串(如「2024」
    年份、会话 id)不受影响——QQ 规则限 5-11 位且词边界。
    """
    masked = text
    for pattern, repl in _MASK_RULES:
        masked = pattern.sub(repl, masked)
    return masked


def mask_pii_deep(payload: Any) -> Any:
    """递归脱敏 dict/list 结构里的全部字符串叶子(工具返回层用)。

    get_resume_detail 等返回给模型的完整简历内容,进模型上下文即进
    Langfuse trace——返回层就地脱敏,trace 侧随之闭环(P0-3 短期方案;
    trace 采集点二次脱敏/留存策略仍归 #68)。
    """
    if isinstance(payload, str):
        return mask_pii(payload)
    if isinstance(payload, dict):
        return {k: mask_pii_deep(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [mask_pii_deep(v) for v in payload]
    return payload
