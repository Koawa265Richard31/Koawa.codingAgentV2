"""C3/C4/C5/C6 regressions for the session memory plane.

C3: failed chat turns enter same-process history (failure echo visible).
C4: conclusion projection surfaces bounded test-evidence references.
C5: compacted-block total stays bounded (oldest blocks fold into a digest).
C6: cross-turn summaries replay from persisted preload instead of re-calling
    the summary model after a restart.
"""
from __future__ import annotations

import unittest
from uuid import uuid4

from koawa_agent_v2.runtime.session import (
    SessionHistory,
    SessionHistoryLimits,
    SessionTurn,
)


def _limits(**overrides) -> SessionHistoryLimits:
    values = dict(max_turns=2, max_chars=200, compact_min_turns=2)
    values.update(overrides)
    return SessionHistoryLimits(**values)


class FailedTurnEntersHistoryTest(unittest.TestCase):
    def test_failed_turn_projection_is_visible(self) -> None:
        history = SessionHistory(provider="test", limits=_limits())
        history.append(
            SessionTurn(
                user_input="do the thing",
                final_text=None,
                turn_id=uuid4(),
                status="failed",
                error="d2:model_client_failed",
            )
        )
        items = history.context_items()
        rendered = "\n".join(
            item.content
            if getattr(item, "content", None) is not None
            else getattr(getattr(item, "item", None), "text", "")
            for item in items
        )
        self.assertIn("d2:model_client_failed", rendered)
        self.assertIn("reconstructed-turn-outcome", rendered)


class ConclusionEvidenceRefsTest(unittest.TestCase):
    def test_conclusion_text_lists_evidence_digest_prefix(self) -> None:
        from koawa_agent_v2.runtime.session import _conclusion_text

        class _Ref(dict):
            pass

        class _Conclusion:
            turn_id = "t"
            turn_status = "completed"
            run_status = "completed"
            error_codes = ()
            successful_tools = ("read_file",)
            changed_files = ()
            uncertainty_codes = ()
            open_obligations = ()
            untrusted_summary = None
            test_evidence_refs = (
                {
                    "stream": "run",
                    "version": 7,
                    "event_id": "e1",
                    "evidence_digest": "abcdef0123456789ffff",
                },
            )

        text = _conclusion_text(_Conclusion())
        self.assertIn("test_evidence_refs=1", text)
        self.assertIn("evidence_digest_prefix=abcdef0123456789", text)


class CompactedBlocksBoundedTest(unittest.TestCase):
    def test_old_blocks_fold_into_merged_digest(self) -> None:
        history = SessionHistory(provider="test", limits=_limits())
        for index in range(60):
            history.append(
                SessionTurn(user_input=f"task {index} " + "x" * 40,
                            final_text="done")
            )
            # Real cadence: projection is rebuilt every turn, so compaction
            # accumulates one block per threshold instead of one mega-block.
            items = history.context_items()
        contents = [getattr(item, "content", "") for item in items]
        merged = [c for c in contents if "compact-merged" in c]
        self.assertEqual(1, len(merged))
        self.assertIn("older_compaction_blocks=", merged[0])
        remaining_compact = [
            c for c in contents if c.startswith("[session:compact]")
        ]
        self.assertLessEqual(len(remaining_compact), 4)


class SummaryReplayTest(unittest.TestCase):
    def test_preloaded_summaries_replay_without_model_call(self) -> None:
        calls = []

        def summarize(text: str) -> str:
            calls.append(text)
            return "fresh summary"

        history = SessionHistory(
            provider="test", limits=_limits(), summarize=summarize
        )
        history.preload_summaries(["persisted summary one"])
        for index in range(4):
            history.append(
                SessionTurn(user_input=f"task {index}", final_text="ok")
            )
        items = history.context_items()
        self.assertEqual(0, len(calls))
        rendered = "\n".join(getattr(item, "content", "") for item in items)
        self.assertIn("persisted summary one", rendered)


if __name__ == "__main__":
    unittest.main()
