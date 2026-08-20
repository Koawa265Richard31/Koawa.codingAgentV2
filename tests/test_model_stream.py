from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    ContentDelta,
    ContentKind,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelCallRef,
    ModelProtocolError,
    ModelRequest,
    ModelStreamFailure,
    ModelTurn,
    ModelUsage,
    OutputKind,
    PublicReasoningSummaryItem,
    StreamFailed,
    StreamFailureKind,
    StreamHeader,
    ToolArgumentsDelta,
    ToolCallItem,
    ToolDefinition,
    ToolResultMessage,
    TurnCompleted,
    TurnStarted,
    UnknownEvent,
    UsageReported,
    UserMessage,
)
from koawa_agent_v2.model.stream import (
    ModelStreamAssembler,
    StreamLimits,
    assemble_model_stream,
)


class ModelStreamAssemblerTest(unittest.TestCase):
    """验证 typed model stream 的聚合、顺序、身份、上限与安全输出。"""

    def setUp(self) -> None:
        self.model_turn_id = uuid4()
        self.provider = "test-provider"
        self.response_id = "response-1"
        self.model = "test-model"

    def header(
        self,
        sequence: int,
        *,
        model_turn_id: UUID | None = None,
        provider: str | None = None,
        response_id: str | None = None,
    ) -> StreamHeader:
        """创建默认属于同一 response 的连续事件头。"""
        return StreamHeader(
            model_turn_id=model_turn_id or self.model_turn_id,
            provider=provider or self.provider,
            provider_response_id=response_id or self.response_id,
            sequence=sequence,
            provider_sequence=sequence,
        )

    def started(self, sequence: int = 0) -> TurnStarted:
        """创建标准流首事件。"""
        return TurnStarted(self.header(sequence), self.model)

    def turn(
        self,
        output_items=(),
        *,
        finish_reason: FinishReason = FinishReason.STOP,
        usage: ModelUsage | None = None,
    ) -> ModelTurn:
        """创建与默认流身份一致的完成态快照。"""
        return ModelTurn(
            model_turn_id=self.model_turn_id,
            provider=self.provider,
            model=self.model,
            provider_response_id=self.response_id,
            output_items=tuple(output_items),
            finish_reason=finish_reason,
            usage=usage,
        )

    def assert_protocol_error(
        self,
        events,
        expected_code: str,
        *,
        limits: StreamLimits | None = None,
    ) -> None:
        """断言流被稳定协议码拒绝，而不是泄漏实现异常。"""
        with self.assertRaises(ModelProtocolError) as raised:
            assemble_model_stream(events, limits=limits)
        self.assertEqual(expected_code, raised.exception.code)

    def test_fragmented_text_is_assembled_exactly(self) -> None:
        """任意文本分片必须逐字保留，并与权威 done 快照一致。"""
        item = AssistantTextItem(0, "message-1", "修复完成：tests passed")
        events = (
            self.started(),
            ItemStarted(
                self.header(1),
                0,
                item.item_id,
                OutputKind.ASSISTANT_TEXT,
            ),
            ContentDelta(
                self.header(2),
                0,
                item.item_id,
                ContentKind.ASSISTANT_TEXT,
                "修",
            ),
            ContentDelta(
                self.header(3),
                0,
                item.item_id,
                ContentKind.ASSISTANT_TEXT,
                "复完成：",
            ),
            ContentDelta(
                self.header(4),
                0,
                item.item_id,
                ContentKind.ASSISTANT_TEXT,
                "tests passed",
            ),
            ItemCompleted(self.header(5), item),
            TurnCompleted(self.header(6), self.turn((item,))),
        )

        result = assemble_model_stream(events)

        self.assertEqual((item,), result.output_items)
        self.assertEqual("修复完成：tests passed", result.final_text)

    def test_two_interleaved_tool_calls_keep_identity_and_order(self) -> None:
        """交错参数分片按 item/call 隔离，完成先后不能改变 canonical 顺序。"""
        first = ToolCallItem(
            0,
            "tool-item-a",
            "call-a",
            "read_file",
            '{"path":"src/你好.py"}',
        )
        second = ToolCallItem(
            1,
            "tool-item-b",
            "call-b",
            "search",
            '{"query":"foo","count":2}',
        )
        completed_turn = self.turn(
            (first, second),
            finish_reason=FinishReason.TOOL_CALLS,
        )
        events = (
            self.started(),
            ItemStarted(
                self.header(1),
                0,
                first.item_id,
                OutputKind.TOOL_CALL,
                first.call_id,
                first.name,
            ),
            ToolArgumentsDelta(
                self.header(2), 0, first.item_id, first.call_id, '{"pa'
            ),
            ItemStarted(
                self.header(3),
                1,
                second.item_id,
                OutputKind.TOOL_CALL,
                second.call_id,
                second.name,
            ),
            ToolArgumentsDelta(
                self.header(4), 1, second.item_id, second.call_id, '{"query":'
            ),
            ToolArgumentsDelta(
                self.header(5), 0, first.item_id, first.call_id, 'th":"src/'
            ),
            ToolArgumentsDelta(
                self.header(6),
                1,
                second.item_id,
                second.call_id,
                '"foo","count":',
            ),
            ToolArgumentsDelta(
                self.header(7), 1, second.item_id, second.call_id, "2}"
            ),
            ItemCompleted(self.header(8), second),
            ToolArgumentsDelta(
                self.header(9), 0, first.item_id, first.call_id, '你好.py"}'
            ),
            ItemCompleted(self.header(10), first),
            TurnCompleted(self.header(11), completed_turn),
        )

        result = assemble_model_stream(events)

        self.assertEqual((first, second), result.output_items)
        self.assertEqual({"path": "src/你好.py"}, first.arguments)
        self.assertEqual({"query": "foo", "count": 2}, second.arguments)

    def test_equal_fragments_with_distinct_sequences_are_not_deduplicated(self) -> None:
        """内容相同不代表重复事件，合法的不同 sequence 必须都参与组装。"""
        item = AssistantTextItem(0, "message-1", "haha")
        events = (
            self.started(),
            ItemStarted(
                self.header(1), 0, item.item_id, OutputKind.ASSISTANT_TEXT
            ),
            ContentDelta(
                self.header(2),
                0,
                item.item_id,
                ContentKind.ASSISTANT_TEXT,
                "ha",
            ),
            ContentDelta(
                self.header(3),
                0,
                item.item_id,
                ContentKind.ASSISTANT_TEXT,
                "ha",
            ),
            ItemCompleted(self.header(4), item),
            TurnCompleted(self.header(5), self.turn((item,))),
        )

        self.assertEqual("haha", assemble_model_stream(events).final_text)

    def test_duplicate_missing_and_reordered_sequences_are_rejected(self) -> None:
        """重复、缺片、倒序以及非零起点都不能形成可信完成态。"""
        cases = {
            "duplicate": (
                self.started(),
                TurnCompleted(self.header(0), self.turn()),
            ),
            "missing": (
                self.started(),
                TurnCompleted(self.header(2), self.turn()),
            ),
            "reordered": (
                self.started(),
                UsageReported(self.header(2), ModelUsage(1, 1)),
                TurnCompleted(self.header(1), self.turn()),
            ),
            "non-zero start": (self.started(1),),
        }

        for name, events in cases.items():
            with self.subTest(name=name):
                self.assert_protocol_error(events, "stream_sequence_mismatch")

    def test_stream_identity_cannot_change_mid_response(self) -> None:
        """model turn、Provider 和 response ID 任一变化都不能跨流拼接。"""
        cases = {
            "model_turn_id": self.header(1, model_turn_id=uuid4()),
            "provider": self.header(1, provider="other-provider"),
            "response_id": self.header(1, response_id="response-2"),
        }

        for name, changed_header in cases.items():
            with self.subTest(name=name):
                events = (
                    self.started(),
                    UsageReported(changed_header, ModelUsage(1, 1)),
                )
                self.assert_protocol_error(events, "stream_identity_changed")

    def test_illegal_item_lifecycles_fail_closed(self) -> None:
        """Item 的 start、delta、done 目标和唯一身份必须满足封闭状态机。"""
        text = AssistantTextItem(0, "text-a", "ok")
        summary = PublicReasoningSummaryItem(0, "text-a", "ok")
        tool = ToolCallItem(0, "tool-a", "call-a", "read_file", '{}')
        wrong_tool = ToolCallItem(0, "tool-a", "call-x", "search", '{}')
        cases = (
            (
                "item before turn",
                (
                    ItemStarted(
                        self.header(0), 0, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                ),
                "stream_must_start_with_turn_started",
            ),
            (
                "duplicate turn start",
                (self.started(), TurnStarted(self.header(1), self.model)),
                "duplicate_turn_started",
            ),
            (
                "delta before item",
                (
                    self.started(),
                    ContentDelta(
                        self.header(1),
                        0,
                        "text-a",
                        ContentKind.ASSISTANT_TEXT,
                        "x",
                    ),
                ),
                "delta_before_item_started",
            ),
            (
                "wrong item target",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                    ContentDelta(
                        self.header(2),
                        0,
                        "text-b",
                        ContentKind.ASSISTANT_TEXT,
                        "x",
                    ),
                ),
                "delta_item_id_mismatch",
            ),
            (
                "text delta targets tool",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1),
                        0,
                        tool.item_id,
                        OutputKind.TOOL_CALL,
                        tool.call_id,
                        tool.name,
                    ),
                    ContentDelta(
                        self.header(2),
                        0,
                        tool.item_id,
                        ContentKind.ASSISTANT_TEXT,
                        "x",
                    ),
                ),
                "content_delta_target_mismatch",
            ),
            (
                "tool delta targets text",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                    ToolArgumentsDelta(
                        self.header(2), 0, "text-a", "call-a", "{}"
                    ),
                ),
                "tool_delta_target_mismatch",
            ),
            (
                "wrong tool call target",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1),
                        0,
                        tool.item_id,
                        OutputKind.TOOL_CALL,
                        tool.call_id,
                        tool.name,
                    ),
                    ToolArgumentsDelta(
                        self.header(2), 0, tool.item_id, "call-x", "{}"
                    ),
                ),
                "tool_delta_call_id_mismatch",
            ),
            (
                "duplicate item index",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                    ItemStarted(
                        self.header(2), 0, "text-b", OutputKind.ASSISTANT_TEXT
                    ),
                ),
                "duplicate_item_index",
            ),
            (
                "duplicate item id",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                    ItemStarted(
                        self.header(2), 1, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                ),
                "duplicate_item_id",
            ),
            (
                "duplicate call id",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1),
                        0,
                        "tool-a",
                        OutputKind.TOOL_CALL,
                        "call-a",
                        "read_file",
                    ),
                    ItemStarted(
                        self.header(2),
                        1,
                        "tool-b",
                        OutputKind.TOOL_CALL,
                        "call-a",
                        "search",
                    ),
                ),
                "duplicate_call_id",
            ),
            (
                "delta after done",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, text.item_id, OutputKind.ASSISTANT_TEXT
                    ),
                    ItemCompleted(self.header(2), text),
                    ContentDelta(
                        self.header(3),
                        0,
                        text.item_id,
                        ContentKind.ASSISTANT_TEXT,
                        "later",
                    ),
                ),
                "delta_after_item_completed",
            ),
            (
                "duplicate done",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, text.item_id, OutputKind.ASSISTANT_TEXT
                    ),
                    ItemCompleted(self.header(2), text),
                    ItemCompleted(self.header(3), text),
                ),
                "delta_after_item_completed",
            ),
            (
                "completed kind changed",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, text.item_id, OutputKind.ASSISTANT_TEXT
                    ),
                    ItemCompleted(self.header(2), summary),
                ),
                "completed_item_kind_mismatch",
            ),
            (
                "completed tool identity changed",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1),
                        0,
                        tool.item_id,
                        OutputKind.TOOL_CALL,
                        tool.call_id,
                        tool.name,
                    ),
                    ItemCompleted(self.header(2), wrong_tool),
                ),
                "completed_tool_identity_mismatch",
            ),
        )

        for name, events, expected_code in cases:
            with self.subTest(name=name):
                self.assert_protocol_error(events, expected_code)

    def test_done_snapshots_must_equal_the_accumulated_fragments(self) -> None:
        """done 是权威快照，但不能与此前完整 delta 拼接结果矛盾。"""
        text = AssistantTextItem(0, "text-a", "abd")
        tool = ToolCallItem(0, "tool-a", "call-a", "read_file", '{"x":2}')
        cases = (
            (
                "text",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, text.item_id, OutputKind.ASSISTANT_TEXT
                    ),
                    ContentDelta(
                        self.header(2),
                        0,
                        text.item_id,
                        ContentKind.ASSISTANT_TEXT,
                        "abc",
                    ),
                    ItemCompleted(self.header(3), text),
                ),
            ),
            (
                "tool arguments",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1),
                        0,
                        tool.item_id,
                        OutputKind.TOOL_CALL,
                        tool.call_id,
                        tool.name,
                    ),
                    ToolArgumentsDelta(
                        self.header(2), 0, tool.item_id, tool.call_id, '{"x":1}'
                    ),
                    ItemCompleted(self.header(3), tool),
                ),
            ),
        )

        for name, events in cases:
            with self.subTest(name=name):
                self.assert_protocol_error(
                    events, "completed_item_does_not_match_deltas"
                )

    def test_turn_terminal_rejects_open_items_and_snapshot_mismatch(self) -> None:
        """Turn terminal 只能引用全部关闭且完全相同的 canonical output。"""
        item = AssistantTextItem(0, "text-a", "done")
        open_item = (
            self.started(),
            ItemStarted(
                self.header(1), 0, item.item_id, OutputKind.ASSISTANT_TEXT
            ),
            TurnCompleted(self.header(2), self.turn()),
        )
        mismatched_snapshot = (
            self.started(),
            ItemStarted(
                self.header(1), 0, item.item_id, OutputKind.ASSISTANT_TEXT
            ),
            ItemCompleted(self.header(2), item),
            TurnCompleted(self.header(3), self.turn()),
        )

        self.assert_protocol_error(open_item, "turn_completed_with_open_item")
        self.assert_protocol_error(
            mismatched_snapshot, "completed_turn_snapshot_mismatch"
        )

    def test_any_event_after_a_terminal_is_rejected(self) -> None:
        """成功或失败 terminal 后的任意事件都不能被静默丢弃。"""
        completed = TurnCompleted(self.header(1), self.turn())
        failure = StreamFailed(
            self.header(1),
            StreamFailureKind.PROVIDER_ERROR,
            "provider_unavailable",
            retryable=True,
        )
        cases = (
            (
                "duplicate success terminal",
                (self.started(), completed, TurnCompleted(self.header(2), self.turn())),
            ),
            (
                "usage after success terminal",
                (self.started(), completed, UsageReported(self.header(2), ModelUsage(1, 1))),
            ),
            (
                "event after failure terminal",
                (self.started(), failure, UsageReported(self.header(2), ModelUsage(1, 1))),
            ),
        )

        for name, events in cases:
            with self.subTest(name=name):
                self.assert_protocol_error(events, "event_after_terminal")

    def test_empty_and_unterminated_streams_fail_at_eof(self) -> None:
        """EOF 不是成功 terminal，空流和所有半截状态都必须 fail closed。"""
        cases = (
            ("empty", (), "empty_model_stream"),
            ("started only", (self.started(),), "unexpected_stream_eof"),
            (
                "open item",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                ),
                "unexpected_stream_eof",
            ),
            (
                "partial text",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                    ContentDelta(
                        self.header(2),
                        0,
                        "text-a",
                        ContentKind.ASSISTANT_TEXT,
                        "partial",
                    ),
                ),
                "unexpected_stream_eof",
            ),
        )

        for name, events, expected_code in cases:
            with self.subTest(name=name):
                self.assert_protocol_error(events, expected_code)

    def test_typed_stream_failure_preserves_only_stable_failure_facts(self) -> None:
        """typed failure 在 EOF 转为安全异常，并保留 retryable 决策信息。"""
        events = (
            self.started(),
            StreamFailed(
                self.header(1),
                StreamFailureKind.PROVIDER_ERROR,
                "rate_limit",
                retryable=True,
            ),
        )

        with self.assertRaises(ModelStreamFailure) as raised:
            assemble_model_stream(events)

        self.assertEqual("rate_limit", raised.exception.code)
        self.assertIs(True, raised.exception.retryable)
        self.assertNotIn("provider payload", repr(raised.exception))

    def test_unknown_required_stream_event_is_rejected_without_payload(self) -> None:
        """未知语义只携带摘要墓碑，聚合时仍然立即阻断。"""
        event = UnknownEvent(
            self.header(1),
            "unknown-0123456789abcdef",
            4096,
            "a" * 64,
        )
        self.assert_protocol_error(
            (self.started(), event),
            "unknown_stream_event",
        )

    def test_tool_arguments_require_strict_json_objects(self) -> None:
        """工具参数拒绝截断、非 object、非 JSON 数字以及任意层 duplicate key。"""
        invalid_values = (
            '{"path":',
            '[]',
            '"text"',
            'null',
            '{"x":1} trailing',
            '{"x":NaN}',
            '{"x":1,"x":2}',
            '{"nested":{"x":1,"x":2}}',
        )

        for raw in invalid_values:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    ToolCallItem(0, "tool-a", "call-a", "read_file", raw)

        with self.assertRaises(ValueError):
            ToolDefinition("read_file", None, '{"type":"object","type":"array"}')

    def test_missing_usage_is_distinct_from_explicit_zero_usage(self) -> None:
        """Provider 未报告 usage 时保持 None，显式零计数不能被当成缺失。"""
        absent = assemble_model_stream(
            (
                self.started(),
                TurnCompleted(self.header(1), self.turn()),
            )
        )
        zero = ModelUsage(0, 0, 0)
        explicit_zero = assemble_model_stream(
            (
                self.started(),
                UsageReported(self.header(1), zero),
                TurnCompleted(self.header(2), self.turn(usage=zero)),
            )
        )

        self.assertIsNone(absent.usage)
        self.assertEqual(zero, explicit_zero.usage)
        self.assertEqual(0, explicit_zero.usage.total_tokens)

        mismatch = (
            self.started(),
            UsageReported(self.header(1), zero),
            TurnCompleted(self.header(2), self.turn()),
        )
        self.assert_protocol_error(mismatch, "completed_turn_usage_mismatch")

    def test_duplicate_usage_is_rejected(self) -> None:
        """同一 response 的 usage 只能明确报告一次。"""
        events = (
            self.started(),
            UsageReported(self.header(1), ModelUsage(1, 2, 3)),
            UsageReported(self.header(2), ModelUsage(1, 2, 3)),
        )
        self.assert_protocol_error(events, "duplicate_usage")

    def test_stream_limits_reject_unbounded_events_items_and_content(self) -> None:
        """事件、Item、单项文本、参数和总字符上限都在聚合期间生效。"""
        text = AssistantTextItem(0, "text-a", "abcd")
        tool = ToolCallItem(0, "tool-a", "call-a", "read_file", '{"x":1}')
        cases = (
            (
                "events",
                (self.started(), TurnCompleted(self.header(1), self.turn())),
                StreamLimits(max_events=1),
                "stream_event_limit_exceeded",
            ),
            (
                "items",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, "text-a", OutputKind.ASSISTANT_TEXT
                    ),
                    ItemStarted(
                        self.header(2), 1, "text-b", OutputKind.ASSISTANT_TEXT
                    ),
                ),
                StreamLimits(max_items=1),
                "stream_item_limit_exceeded",
            ),
            (
                "text delta",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, text.item_id, OutputKind.ASSISTANT_TEXT
                    ),
                    ContentDelta(
                        self.header(2),
                        0,
                        text.item_id,
                        ContentKind.ASSISTANT_TEXT,
                        "abcd",
                    ),
                ),
                StreamLimits(max_text_chars=3),
                "text_output_limit_exceeded",
            ),
            (
                "tool arguments delta",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1),
                        0,
                        tool.item_id,
                        OutputKind.TOOL_CALL,
                        tool.call_id,
                        tool.name,
                    ),
                    ToolArgumentsDelta(
                        self.header(2), 0, tool.item_id, tool.call_id, '{"x":1}'
                    ),
                ),
                StreamLimits(max_argument_chars=6),
                "tool_arguments_limit_exceeded",
            ),
            (
                "total content",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, text.item_id, OutputKind.ASSISTANT_TEXT
                    ),
                    ContentDelta(
                        self.header(2),
                        0,
                        text.item_id,
                        ContentKind.ASSISTANT_TEXT,
                        "abcd",
                    ),
                ),
                StreamLimits(max_total_chars=3),
                "stream_content_limit_exceeded",
            ),
            (
                "full snapshot without deltas",
                (
                    self.started(),
                    ItemStarted(
                        self.header(1), 0, text.item_id, OutputKind.ASSISTANT_TEXT
                    ),
                    ItemCompleted(self.header(2), text),
                ),
                StreamLimits(max_text_chars=3),
                "text_output_limit_exceeded",
            ),
        )

        for name, events, limits, expected_code in cases:
            with self.subTest(name=name):
                self.assert_protocol_error(events, expected_code, limits=limits)

    def test_stream_limits_require_positive_non_boolean_integers(self) -> None:
        """配置错误必须在接触 Provider 前同步失败。"""
        names = (
            "max_events",
            "max_items",
            "max_text_chars",
            "max_argument_chars",
            "max_total_chars",
        )
        for name in names:
            for invalid in (0, -1, True):
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaises(ValueError):
                        StreamLimits(**{name: invalid})

    def test_sensitive_content_is_absent_from_protocol_repr(self) -> None:
        """日志 repr 只暴露身份、类型和长度，不暴露正文、参数或结果。"""
        secret = "sk-test-DO-NOT-LEAK"
        text = AssistantTextItem(0, "text-a", secret)
        tool = ToolCallItem(
            0,
            "tool-a",
            "call-a",
            "read_file",
            '{"token":"' + secret + '"}',
        )
        definition = ToolDefinition(
            "read_file",
            secret,
            '{"type":"object","secret":"' + secret + '"}',
        )
        user = UserMessage("input-a", secret)
        call_ref = ModelCallRef(self.model_turn_id, tool.call_id)
        result = ToolResultMessage(call_ref, secret)
        request = ModelRequest(
            uuid4(),
            self.provider,
            self.model,
            (user,),
            (definition,),
        )
        turn = self.turn((text,))
        values = (
            text,
            tool,
            definition,
            user,
            result,
            request,
            turn,
            ContentDelta(
                self.header(1),
                0,
                text.item_id,
                ContentKind.ASSISTANT_TEXT,
                secret,
            ),
            ToolArgumentsDelta(
                self.header(1),
                0,
                tool.item_id,
                tool.call_id,
                '{"token":"' + secret + '"}',
            ),
            ItemCompleted(self.header(1), tool),
            TurnCompleted(self.header(1), turn),
        )

        for value in values:
            with self.subTest(value_type=type(value).__name__):
                self.assertNotIn(secret, repr(value))

    def test_incremental_assembler_does_not_return_before_typed_terminal(self) -> None:
        """即使早期 ToolCall 已 done，Assembler 也必须等待整个 response terminal。"""
        tool = ToolCallItem(0, "tool-a", "call-a", "read_file", '{}')
        assembler = ModelStreamAssembler()
        assembler.accept(self.started())
        assembler.accept(
            ItemStarted(
                self.header(1),
                0,
                tool.item_id,
                OutputKind.TOOL_CALL,
                tool.call_id,
                tool.name,
            )
        )
        assembler.accept(ItemCompleted(self.header(2), tool))

        with self.assertRaises(ModelProtocolError) as raised:
            assembler.finish()

        self.assertEqual("unexpected_stream_eof", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
