"""D22 F6b：收尾摘要（确定性 + 可选模型，request-scoped）+ F4 解析。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.model.protocol import (
    AssistantTextItem,
    FinishReason,
    ItemCompleted,
    ItemStarted,
    ModelTurn,
    OutputKind,
    StreamHeader,
    TurnCompleted,
    TurnStarted,
)
from koawa_agent_v2.model.openai_client import OpenAICompatibleClientError
from koawa_agent_v2.runtime.cli import _collect_changed_files
from koawa_agent_v2.runtime.config import RuntimeConfigError
from koawa_agent_v2.runtime.turn_summary import build_turn_summary, summarize_with_model


class TurnSummaryBuildTest(unittest.TestCase):
    def test_deterministic_summary_lists_tools_files_and_resume_hint(self) -> None:
        text = build_turn_summary(["apply_patch", "git_status"], ("index.html",))
        self.assertIn("【回合主体已完成", text)
        self.assertIn("apply_patch", text)
        self.assertIn("index.html", text)
        self.assertIn("/resume", text)

    def test_summary_without_tools_still_mentions_resume(self) -> None:
        text = build_turn_summary([], ())
        self.assertIn("/resume", text)


class _FakeSummaryClient:
    def __init__(self, text: str = "已生成摘要", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.request = None
        self.calls = 0

    def stream(self, request):
        self.calls += 1
        self.request = request
        if self.fail:
            raise OpenAICompatibleClientError("openai.transport_error")
        header = StreamHeader(request.model_turn_id, request.provider, "resp-1", 0, 0)
        item = AssistantTextItem(0, "item-s", self.text)
        turn = ModelTurn(
            request.model_turn_id,
            request.provider,
            request.model,
            "resp-1",
            (item,),
            FinishReason.STOP,
        )
        return iter((
            TurnStarted(header, request.model),
            ItemStarted(StreamHeader(request.model_turn_id, request.provider, "resp-1", 1, 1),
                        0, item.item_id, OutputKind.ASSISTANT_TEXT),
            ItemCompleted(StreamHeader(request.model_turn_id, request.provider, "resp-1", 2, 2), item),
            TurnCompleted(StreamHeader(request.model_turn_id, request.provider, "resp-1", 3, 3), turn),
        ))


class SummarizeWithModelTest(unittest.TestCase):
    def test_uses_configured_model_once_and_returns_text(self) -> None:
        client = _FakeSummaryClient("已生成摘要")
        text, ok = summarize_with_model(
            client,
            provider="siliconflow",
            model="Qwen/Qwen3.5-4B",
            text="已执行工具：apply_patch；改动文件：index.html",
        )
        self.assertTrue(ok)
        self.assertEqual("已生成摘要", text)
        self.assertEqual(1, client.calls)
        self.assertIsNotNone(client.request)
        self.assertEqual("Qwen/Qwen3.5-4B", client.request.model)
        self.assertEqual("siliconflow", client.request.provider)
        self.assertEqual((), client.request.tool_definitions)

    def test_failure_returns_none_false(self) -> None:
        client = _FakeSummaryClient(fail=True)
        text, ok = summarize_with_model(
            client,
            provider="siliconflow",
            model="Qwen/Qwen3.5-4B",
            text="trace",
        )
        self.assertIsNone(text)
        self.assertFalse(ok)

    def test_scope_guarantee_helper_never_mutates_caller_state(self) -> None:
        client = _FakeSummaryClient()
        config_probe = {"provider": {"model": "main-model"}}
        summarize_with_model(
            client,
            provider="siliconflow",
            model="fallback-model",
            text="trace",
        )
        self.assertEqual("main-model", config_probe["provider"]["model"])
        self.assertEqual("fallback-model", client.request.model)


class FallbackSummaryModelConfigTest(unittest.TestCase):
    def _write_config(self, *, fallback: str | None) -> tuple[Path, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        repo = base / "repo"
        repo.mkdir()
        document = {
            "repo": str(repo),
            "db": str(base / "state.sqlite3"),
            "provider": {
                "base_url": "https://api.example.test/v1",
                "api_key_env": "X",
                "model": "m",
                "provider": "openai_compatible",
            },
            "sandbox": {"runner": "host", "host_trust": "builtin_fixture"},
            "test_profiles": [
                {"profile_id": "unit", "argv": ["python", "-m", "unittest"], "timeout_seconds": 60},
            ],
            "policy": {"patch_decision": "allow"},
            "system_prompt": "你是助手。",
        }
        if fallback is not None:
            document["fallback_summary_model"] = fallback
        target = base / "config.json"
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return target, base

    def test_field_parses(self) -> None:
        from koawa_agent_v2.runtime.config import load_runtime_config

        target, _ = self._write_config(fallback="Qwen/Qwen3.5-4B")
        config = load_runtime_config(target)
        self.assertEqual("Qwen/Qwen3.5-4B", config.fallback_summary_model)

    def test_field_absent_is_none(self) -> None:
        from koawa_agent_v2.runtime.config import load_runtime_config

        target, _ = self._write_config(fallback=None)
        config = load_runtime_config(target)
        self.assertIsNone(config.fallback_summary_model)

    def test_control_chars_rejected(self) -> None:
        from koawa_agent_v2.runtime.config import load_runtime_config

        target, _ = self._write_config(fallback="bad\x00model")
        with self.assertRaises(RuntimeConfigError) as raised:
            load_runtime_config(target)
        self.assertEqual("invalid_fallback_summary_model", raised.exception.code)

    def test_long_value_rejected(self) -> None:
        from koawa_agent_v2.runtime.config import load_runtime_config

        target, _ = self._write_config(fallback="m" * 300)
        with self.assertRaises(RuntimeConfigError):
            load_runtime_config(target)


class ChangedFilesParserTest(unittest.TestCase):
    """D22 F4：apply_patch changes 权威 + git_diff changed_paths 补集。"""

    def test_apply_patch_changes_include_untracked_add(self) -> None:
        parsed = {
            "ok": True,
            "changes": [
                {"operation": "add", "path": "new.md"},
                {"operation": "update", "path": "old.md"},
            ],
        }
        self.assertEqual({"new.md", "old.md"}, _collect_changed_files(parsed))

    def test_git_diff_changed_paths_still_collected(self) -> None:
        parsed = {"changed_paths": ["a.md", "b.md"], "diff": "..."}
        self.assertEqual({"a.md", "b.md"}, _collect_changed_files(parsed))

    def test_union_of_both_sources(self) -> None:
        parsed = {"changed_paths": ["a.md"], "changes": [{"path": "new.md"}]}
        self.assertEqual({"a.md", "new.md"}, _collect_changed_files(parsed))

    def test_garbage_inputs_do_not_crash(self) -> None:
        self.assertEqual(set(), _collect_changed_files(None))
        self.assertEqual(set(), _collect_changed_files("not json"))
        self.assertEqual(set(), _collect_changed_files({"changed_paths": [1, None]}))
        self.assertEqual(set(), _collect_changed_files({"changes": [{"path": 3}, "x"]}))


if __name__ == "__main__":
    unittest.main()
