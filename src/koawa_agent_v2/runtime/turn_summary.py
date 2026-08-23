"""D22 F6b: 收尾摘要——回合主体完成但最终回复失败时的可见输出。

两层：
1. 确定性摘要（零模型依赖）：从事件/会话记录拼装"已执行工具 + 改动文件 + 续做方式"；
2. 可选摘要模型回退（默认关闭，由用户显式配置 fallback_summary_model 启用）：
   仅对一次摘要请求使用该模型；request-scoped，绝不改变会话/回合模型配置
   （下一轮 turn 仍用主模型）；失败则回落确定性摘要，不二次回退。
"""

from __future__ import annotations

from uuid import uuid4

from typing import Any

from ..model.protocol import (
    AssistantTextItem,
    InstructionMessage,
    InstructionRole,
    ModelRequest,
    UserMessage,
)

_SUMMARY_SYSTEM_PROMPT = ("你是收尾摘要助手：用一句简洁中文总结 agent 刚完成的工作，"
                          "不要编造任何工具执行之外的事实。")


def build_turn_summary(
    ok_tools: list[str],
    changed_files: tuple[str, ...],
) -> str:
    """确定性收尾摘要；素材全部来自事件库/会话记录。"""
    lines = ["【回合主体已完成，最终回复生成失败】"]
    if ok_tools:
        lines.append("已执行：" + "、".join(sorted(set(ok_tools))))
    if changed_files:
        lines.append("改动文件：" + "、".join(changed_files))
    lines.append("动作已持久化；输入 /resume 可重试生成回复")
    return "\n".join(lines)


def summarize_with_model(
    client: Any,
    *,
    provider: str,
    model: str,
    text: str,
) -> tuple[str | None, bool]:
    """用指定模型生成一次收尾摘要（request-scoped，仅一次请求）。

    返回 (文本, ok)；任何失败返回 (None, False)，由调用方回落确定性摘要。
    本函数不修改任何会话状态——作用域保证见 D22 §2.6。
    """
    request = ModelRequest(
        model_turn_id=uuid4(),
        provider=provider,
        model=model,
        input_items=(
            InstructionMessage(
                InstructionRole.SYSTEM,
                _SUMMARY_SYSTEM_PROMPT,
            ),
            UserMessage("summary-source", text),
        ),
        tool_definitions=(),
        max_output_tokens=256,
    )
    parts: list[str] = []
    try:
        for event in client.stream(request):
            item = getattr(event, "item", None)
            if isinstance(item, AssistantTextItem):
                parts.append(item.text)
    except Exception:
        return None, False
    final_text = "".join(parts).strip()
    if not final_text:
        return None, False
    return final_text, True
