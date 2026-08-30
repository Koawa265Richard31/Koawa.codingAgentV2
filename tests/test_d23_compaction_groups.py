"""D23-C tests: turn-internal closed execution-group parsing and selection.

Covers docs/day-23-memory-layer-upgrade.md §5.1 / §5.2 at the pure-function
level: grouping by ModelTurn boundary, unique ``call_ref`` pairing, the
closed-vs-open distinction, recent-group anchors, and the corruption detection
that raises ``CompactionError``.

The two spec-deferred choices are pinned here (see the module docstring of
``koawa_agent_v2.execution.compaction``):

* a duplicate result for the same ``call_ref`` raises
  ``CompactionError("duplicate_tool_result")`` (contradictory data, fail closed);
* a cross-turn result raises ``CompactionError("cross_turn_tool_result")``
  (mirrors the protocol validator's "tool result crosses model-turn order").
"""

from __future__ import annotations

import unittest
from uuid import uuid4

from koawa_agent_v2.execution.compaction import (
    ClosedExecutionGroup,
    CompactionError,
    anchors_are_preserved,
    parse_closed_groups,
    select_compressible,
)
from koawa_agent_v2.model.protocol import (
    AssistantMessage,
    AssistantTextItem,
    InstructionMessage,
    InstructionRole,
    ModelCallRef,
    ToolCallEcho,
    ToolCallItem,
    ToolResultMessage,
)


def _assistant(turn_id, text, index=0):
    return AssistantMessage(
        source_provider="test",
        model_turn_id=turn_id,
        item=AssistantTextItem(
            canonical_index=index, item_id=f"text-{index}", text=text
        ),
    )


def _call(turn_id, call_id, name="tool", index=0):
    return ToolCallEcho(
        source_provider="test",
        call_ref=ModelCallRef(turn_id, call_id),
        item=ToolCallItem(
            canonical_index=index,
            item_id=f"call-item-{call_id}",
            call_id=call_id,
            name=name,
            arguments_json="{}",
        ),
    )


def _result(turn_id, call_id, content="ok", is_error=False):
    return ToolResultMessage(
        call_ref=ModelCallRef(turn_id, call_id), content=content, is_error=is_error
    )


def _closed_groups(count):
    """Parse ``count`` independent, fully-paired single-call turns."""
    items = []
    for i in range(count):
        turn_id = uuid4()
        items.append(_assistant(turn_id, f"m{i}"))
        items.append(_call(turn_id, f"c{i}", index=1))
        items.append(_result(turn_id, f"c{i}"))
    return parse_closed_groups(items)


class ParseGroupsTest(unittest.TestCase):
    def test_empty_input_returns_empty_tuple(self):
        self.assertEqual(parse_closed_groups(()), ())
        self.assertEqual(parse_closed_groups([]), ())

    def test_non_context_item_raises_typeerror(self):
        turn_id = uuid4()
        with self.assertRaises(TypeError):
            parse_closed_groups([_assistant(turn_id, "hi"), "not a context item"])

    def test_single_call_complete_pairing(self):
        turn_id = uuid4()
        assistant = _assistant(turn_id, "hello")
        call = _call(turn_id, "c1", index=1)
        result = _result(turn_id, "c1")
        groups = parse_closed_groups([assistant, call, result])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertIsInstance(group, ClosedExecutionGroup)
        self.assertTrue(group.closed)
        self.assertEqual(group.model_turn_id, turn_id)
        self.assertEqual(group.assistant_text, "hello")
        self.assertEqual(group.calls, (call,))
        self.assertEqual(group.results, (result,))
        self.assertEqual(group.first_context_index, 0)
        self.assertEqual(group.last_context_index, 2)

    def test_multi_call_all_paired_in_order(self):
        turn_id = uuid4()
        assistant = _assistant(turn_id, "do it")
        call_a = _call(turn_id, "a", index=1)
        call_b = _call(turn_id, "b", index=2)
        result_a = _result(turn_id, "a")
        result_b = _result(turn_id, "b")
        groups = parse_closed_groups([assistant, call_a, call_b, result_a, result_b])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertTrue(group.closed)
        self.assertEqual(group.calls, (call_a, call_b))
        self.assertEqual(group.results, (result_a, result_b))
        # results pair positionally with calls
        self.assertEqual(group.results[0].call_ref, group.calls[0].call_ref)
        self.assertEqual(group.results[1].call_ref, group.calls[1].call_ref)

    def test_missing_one_result_not_closed(self):
        turn_id = uuid4()
        assistant = _assistant(turn_id, "x")
        call_a = _call(turn_id, "a", index=1)
        call_b = _call(turn_id, "b", index=2)
        result_a = _result(turn_id, "a")
        groups = parse_closed_groups([assistant, call_a, call_b, result_a])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertFalse(group.closed)
        self.assertEqual(group.calls, (call_a, call_b))
        self.assertEqual(group.results, (result_a,))

    def test_text_only_turn_is_closed(self):
        turn_id = uuid4()
        groups = parse_closed_groups([_assistant(turn_id, "no tools")])
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0].closed)
        self.assertEqual(groups[0].calls, ())
        self.assertEqual(groups[0].results, ())

    def test_error_result_is_still_closed(self):
        # is_error=True is a durable result, not an UNKNOWN outcome, so it
        # completes the pairing.
        turn_id = uuid4()
        assistant = _assistant(turn_id, "x")
        call = _call(turn_id, "c1", index=1)
        error_result = _result(turn_id, "c1", content="boom", is_error=True)
        groups = parse_closed_groups([assistant, call, error_result])
        self.assertTrue(groups[0].closed)

    def test_result_call_id_mismatch_raises_unpaired(self):
        turn_id = uuid4()
        assistant = _assistant(turn_id, "x")
        call = _call(turn_id, "c1", index=1)
        bad_result = _result(turn_id, "does_not_exist")
        with self.assertRaises(CompactionError) as caught:
            parse_closed_groups([assistant, call, bad_result])
        self.assertEqual(caught.exception.code, "unpaired_tool_result")

    def test_duplicate_result_raises(self):
        turn_id = uuid4()
        assistant = _assistant(turn_id, "x")
        call = _call(turn_id, "c1", index=1)
        first = _result(turn_id, "c1")
        second = _result(turn_id, "c1")
        with self.assertRaises(CompactionError) as caught:
            parse_closed_groups([assistant, call, first, second])
        self.assertEqual(caught.exception.code, "duplicate_tool_result")

    def test_cross_turn_result_raises(self):
        t1, t2 = uuid4(), uuid4()
        a1 = _assistant(t1, "one")
        c1 = _call(t1, "c1", index=1)
        a2 = _assistant(t2, "two")
        late = _result(t1, "c1")  # T1's result appears after T2 has begun
        with self.assertRaises(CompactionError) as caught:
            parse_closed_groups([a1, c1, a2, late])
        self.assertEqual(caught.exception.code, "cross_turn_tool_result")

    def test_duplicate_call_raises(self):
        turn_id = uuid4()
        assistant = _assistant(turn_id, "x")
        call = _call(turn_id, "c1", index=1)
        call_again = _call(turn_id, "c1", index=2)
        with self.assertRaises(CompactionError) as caught:
            parse_closed_groups([assistant, call, call_again])
        self.assertEqual(caught.exception.code, "duplicate_tool_call")

    def test_multiple_turns_produce_multiple_groups(self):
        t1, t2 = uuid4(), uuid4()
        items = [
            _assistant(t1, "m1"), _call(t1, "a", index=1), _result(t1, "a"),
            _assistant(t2, "m2"), _call(t2, "b", index=1), _result(t2, "b"),
        ]
        groups = parse_closed_groups(items)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group.closed for group in groups))
        self.assertEqual([group.model_turn_id for group in groups], [t1, t2])

    def test_instruction_message_is_a_boundary(self):
        t1, t2 = uuid4(), uuid4()
        items = [
            _assistant(t1, "m1"), _call(t1, "a", index=1), _result(t1, "a"),
            InstructionMessage(InstructionRole.SYSTEM, "instructions"),
            _assistant(t2, "m2"), _call(t2, "b", index=1), _result(t2, "b"),
        ]
        groups = parse_closed_groups(items)
        self.assertEqual(len(groups), 2)
        # The instruction is not part of either group's span.
        self.assertEqual(groups[0].last_context_index, 2)
        self.assertEqual(groups[1].first_context_index, 4)


class SelectCompressibleTest(unittest.TestCase):
    def test_keeps_recent_and_returns_older(self):
        groups = _closed_groups(5)
        selected = select_compressible(groups, keep_recent=2)
        self.assertEqual(selected, groups[:3])
        self.assertEqual(len(selected), 3)

    def test_keep_recent_gte_count_returns_empty(self):
        groups = _closed_groups(3)
        self.assertEqual(select_compressible(groups, keep_recent=3), ())
        self.assertEqual(select_compressible(groups, keep_recent=4), ())

    def test_keep_recent_below_one_raises_valueerror(self):
        groups = _closed_groups(2)
        with self.assertRaises(ValueError):
            select_compressible(groups, keep_recent=0)
        with self.assertRaises(ValueError):
            select_compressible(groups, keep_recent=-1)

    def test_keep_recent_non_int_raises_typeerror(self):
        with self.assertRaises(TypeError):
            select_compressible((), keep_recent=True)
        with self.assertRaises(TypeError):
            select_compressible((), keep_recent="2")

    def test_only_closed_groups_are_selected(self):
        t0, t1, t2, t3 = uuid4(), uuid4(), uuid4(), uuid4()
        items = [
            _assistant(t0, "m0"), _call(t0, "c0", index=1), _result(t0, "c0"),
            _assistant(t1, "m1"), _call(t1, "c1", index=1),  # missing result
            _assistant(t2, "m2"), _call(t2, "c2", index=1), _result(t2, "c2"),
            _assistant(t3, "m3"), _call(t3, "c3", index=1), _result(t3, "c3"),
        ]
        groups = parse_closed_groups(items)
        self.assertEqual([group.closed for group in groups], [True, False, True, True])
        selected = select_compressible(groups, keep_recent=1)
        # closed groups in order: [g0, g2, g3]; keep 1 recent -> [g0, g2]
        self.assertEqual(selected, (groups[0], groups[2]))
        self.assertTrue(all(group.closed for group in selected))


class AnchorsPreservedTest(unittest.TestCase):
    def test_legal_prefix_selection_returns_true(self):
        groups = _closed_groups(3)
        self.assertTrue(anchors_are_preserved(groups, groups[:2]))
        self.assertTrue(anchors_are_preserved(groups, groups[:1]))

    def test_empty_selection_returns_true(self):
        groups = _closed_groups(3)
        self.assertTrue(anchors_are_preserved(groups, ()))

    def test_selection_with_open_group_returns_false(self):
        t0, t1, t2 = uuid4(), uuid4(), uuid4()
        closed0 = _closed_groups(1)[0]
        open1 = parse_closed_groups([_assistant(t1, "m1"), _call(t1, "c1", index=1)])[0]
        closed2 = _closed_groups(1)[0]
        groups = (closed0, open1, closed2)
        self.assertFalse(anchors_are_preserved(groups, (open1,)))
        self.assertFalse(anchors_are_preserved(groups, (closed0, open1)))

    def test_non_contiguous_selection_returns_false(self):
        groups = _closed_groups(3)
        self.assertFalse(anchors_are_preserved(groups, (groups[0], groups[2])))

    def test_recent_group_selected_returns_false(self):
        groups = _closed_groups(3)
        # The most-recent group is always an anchor (keep_recent >= 1).
        self.assertFalse(anchors_are_preserved(groups, (groups[2],)))
        self.assertFalse(anchors_are_preserved(groups, groups))


class CompactionErrorTest(unittest.TestCase):
    def test_code_pattern_is_enforced(self):
        with self.assertRaises(ValueError):
            CompactionError("Bad Code!")
        with self.assertRaises(ValueError):
            CompactionError("1starts_with_digit")
        with self.assertRaises(ValueError):
            CompactionError("")
        error = CompactionError("unpaired_tool_result")
        self.assertEqual(error.code, "unpaired_tool_result")
        self.assertIsInstance(error, RuntimeError)


if __name__ == "__main__":
    unittest.main()
