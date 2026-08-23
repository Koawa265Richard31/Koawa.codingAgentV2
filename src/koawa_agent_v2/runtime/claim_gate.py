"""D22 F1: 交互完成门（防无工具幻觉完成）。

启发式：回合最终文本若声称修改了工作区文件，则该回合必须真实执行过至少一个
成功的写类工具。这是给廉价模型的低成本真实性检查，不是语义校验（D21 诚实边界）；
模式白名单 + 写工具白名单，误杀面刻意收窄，测试覆盖三类场景。
"""

from __future__ import annotations

import re

_CLAIM_PATTERN = re.compile(
    r"(?:已创建|创建了|已修改|修改了|已删除|删除了|已更新|更新了|"
    r"已写入|写入了|已生成|成功创建|成功修改|成功删除|成功写入|文件已)"
)

_WRITE_TOOLS = frozenset({"apply_patch"})


def claims_workspace_change(text: str) -> bool:
    """最终文本是否包含写入声明（白名单词）。"""
    if not isinstance(text, str):
        return False
    return _CLAIM_PATTERN.search(text) is not None


def claim_gate_allows(
    final_text: str,
    successful_write_tools: frozenset[str],
    *,
    write_tools: frozenset[str] = _WRITE_TOOLS,
) -> bool:
    """False 当回合声称改文件却没有任何成功写工具。"""
    if not claims_workspace_change(final_text):
        return True
    return bool(successful_write_tools & write_tools)
