from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from koawa_agent_v2.execution.loop import ToolExecutionContext
from koawa_agent_v2.model.protocol import ModelCallRef, ToolCallItem
from koawa_agent_v2.tools.repository import (
    RepositoryToolLimits,
    _iter_text_lines,
    _literal_columns,
    build_repository_tool_registry,
    repository_tool_specs,
)
from koawa_agent_v2.tools.errors import ToolConfigurationError
from koawa_agent_v2.tools.registry import ToolRegistry


def _context(call_id: str = "call-1") -> ToolExecutionContext:
    model_turn_id = uuid4()
    return ToolExecutionContext(
        run_id=uuid4(),
        model_turn_id=model_turn_id,
        model_round=1,
        call_ref=ModelCallRef(model_turn_id, call_id),
    )


def _execute(
    registry: ToolRegistry,
    name: str,
    arguments: dict[str, object],
) -> tuple[object, dict[str, object]]:
    result = registry.execute(
        ToolCallItem(
            0,
            "tool-item",
            "call-1",
            name,
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        ),
        context=_context(),
    )
    return result, json.loads(result.content)


class RepositoryToolSpecTest(unittest.TestCase):
    def test_limits_are_the_single_source_for_all_provider_schema_bounds(self) -> None:
        limits = RepositoryToolLimits(
            max_path_chars=77,
            max_query_chars=33,
            max_glob_patterns=4,
            max_glob_pattern_chars=22,
            max_read_lines=9,
            max_directory_entries=8,
            max_search_files=7,
            max_depth=3,
            max_matches=6,
        )

        read_spec, list_spec, search_spec = repository_tool_specs(limits)

        self.assertEqual(77, read_spec.input_schema["properties"]["path"]["maxLength"])
        self.assertEqual(9, read_spec.input_schema["properties"]["max_lines"]["maximum"])
        self.assertEqual(8, list_spec.input_schema["properties"]["max_entries"]["maximum"])
        self.assertEqual(3, list_spec.input_schema["properties"]["max_depth"]["maximum"])
        self.assertEqual(33, search_spec.input_schema["properties"]["query"]["maxLength"])
        self.assertEqual(7, search_spec.input_schema["properties"]["max_files"]["maximum"])
        self.assertEqual(6, search_spec.input_schema["properties"]["max_matches"]["maximum"])
        self.assertEqual(4, search_spec.input_schema["properties"]["include"]["maxItems"])
        self.assertEqual(
            22,
            search_spec.input_schema["properties"]["include"]["items"]["maxLength"],
        )

    def test_invalid_limit_configuration_fails_before_a_registry_is_built(self) -> None:
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(ToolConfigurationError):
                RepositoryToolLimits(max_file_bytes=value)  # type: ignore[arg-type]
        with self.assertRaises(ToolConfigurationError):
            RepositoryToolLimits(max_output_chars=511)

        over_ceiling = {
            "max_path_chars": 4_097,
            "max_query_chars": 4_097,
            "max_glob_patterns": 65,
            "max_glob_pattern_chars": 1_025,
            "max_file_bytes": sys.maxsize + 1,
            "max_start_line": 2_147_483_648,
            "max_read_lines": 10_001,
            "max_directory_entries": 10_001,
            "max_directory_scan_entries": 100_001,
            "max_search_files": 10_001,
            "max_search_total_bytes": 64 * 1024 * 1024 + 1,
            "max_depth": 65,
            "max_matches": 10_001,
            "max_match_chars": 4_097,
            "max_output_chars": 262_145,
        }
        for name, value in over_ceiling.items():
            with self.subTest(name=name), self.assertRaises(ToolConfigurationError):
                RepositoryToolLimits(**{name: value})


class RepositoryToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_read_file_returns_a_bounded_utf8_slice_and_hash_metadata(self) -> None:
        (self.root / "notes.txt").write_bytes(
            b"\xef\xbb\xbf" + "one\r\ntwo\n三\nfour\n".encode()
        )
        with build_repository_tool_registry(self.root) as registry:
            result, document = _execute(
                registry,
                "read_file",
                {"path": "notes.txt", "start_line": 2, "max_lines": 2},
            )

        self.assertIs(False, result.is_error)
        self.assertEqual("two\n三", document["content"])
        self.assertEqual(2, document["start_line"])
        self.assertEqual(3, document["end_line"])
        self.assertEqual(4, document["total_lines"])
        self.assertIs(True, document["truncated"])
        self.assertEqual(64, len(document["sha256"]))

    def test_read_past_eof_is_explicit_and_invalid_arguments_never_reach_handler(self) -> None:
        (self.root / "one.txt").write_text("only\n", encoding="utf-8")
        limits = RepositoryToolLimits(max_read_lines=3)
        with build_repository_tool_registry(self.root, limits=limits) as registry:
            result, document = _execute(
                registry,
                "read_file",
                {"path": "one.txt", "start_line": 99, "max_lines": 1},
            )
            invalid, invalid_document = _execute(
                registry,
                "read_file",
                {"path": "one.txt", "start_line": 1, "max_lines": 4},
            )

        self.assertIs(False, result.is_error)
        self.assertEqual("", document["content"])
        self.assertIsNone(document["end_line"])
        self.assertIs(True, document["truncated"])
        self.assertIs(True, invalid.is_error)
        self.assertEqual("invalid_tool_arguments", invalid_document["error"]["code"])
        self.assertEqual("max_lines", invalid_document["error"]["field"])

    def test_read_rejects_binary_invalid_utf8_oversize_and_escape_without_leaks(self) -> None:
        (self.root / "binary.dat").write_bytes(b"abc\x00def")
        (self.root / "invalid.txt").write_bytes(b"\xff\xfe")
        (self.root / "large.txt").write_bytes(b"x" * 9)
        limits = RepositoryToolLimits(max_file_bytes=8)
        with build_repository_tool_registry(self.root, limits=limits) as registry:
            binary, binary_document = _execute(
                registry,
                "read_file",
                {"path": "binary.dat", "start_line": 1, "max_lines": 1},
            )
            invalid, invalid_document = _execute(
                registry,
                "read_file",
                {"path": "invalid.txt", "start_line": 1, "max_lines": 1},
            )
            large, large_document = _execute(
                registry,
                "read_file",
                {"path": "large.txt", "start_line": 1, "max_lines": 1},
            )
            escape, escape_document = _execute(
                registry,
                "read_file",
                {"path": "../secret.txt", "start_line": 1, "max_lines": 1},
            )

        self.assertEqual("binary_file", binary_document["error"]["code"])
        self.assertEqual("invalid_utf8", invalid_document["error"]["code"])
        self.assertEqual("workspace_file_too_large", large_document["error"]["code"])
        self.assertEqual("invalid_workspace_path", escape_document["error"]["code"])
        self.assertTrue(all(item.is_error for item in (binary, invalid, large, escape)))
        self.assertNotIn(str(self.root), escape.content)

    def test_read_result_never_exceeds_output_budget_even_for_one_long_line(self) -> None:
        (self.root / "long.txt").write_text("x" * 2_000, encoding="utf-8")
        limits = RepositoryToolLimits(
            max_file_bytes=3_000,
            max_output_chars=512,
        )
        with build_repository_tool_registry(self.root, limits=limits) as registry:
            result, document = _execute(
                registry,
                "read_file",
                {"path": "long.txt", "start_line": 1, "max_lines": 1},
            )

        self.assertLessEqual(len(result.content), 512)
        self.assertIs(True, document["output_truncated"])
        self.assertIs(True, document["truncated"])

    def test_list_files_is_recursive_sorted_depth_bounded_and_skips_control_trees(self) -> None:
        (self.root / "z.txt").write_text("z", encoding="utf-8")
        (self.root / "a").mkdir()
        (self.root / "a" / "b.txt").write_text("b", encoding="utf-8")
        (self.root / "a" / "deep").mkdir()
        (self.root / "a" / "deep" / "hidden.txt").write_text("h", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "object").write_text("secret", encoding="utf-8")
        with build_repository_tool_registry(self.root) as registry:
            result, document = _execute(
                registry,
                "list_files",
                {"path": ".", "max_depth": 1, "max_entries": 100},
            )
            direct, direct_document = _execute(
                registry,
                "list_files",
                {"path": ".", "max_depth": 0, "max_entries": 100},
            )

        paths = [entry["path"] for entry in document["entries"]]
        direct_paths = [entry["path"] for entry in direct_document["entries"]]
        self.assertIs(False, result.is_error)
        self.assertEqual(sorted(paths), paths)
        self.assertIn("a/b.txt", paths)
        self.assertIn("a/deep", paths)
        self.assertNotIn("a/deep/hidden.txt", paths)
        self.assertIn(".git", paths)
        self.assertNotIn(".git/object", paths)
        self.assertEqual([".git", "a", "z.txt"], direct_paths)
        self.assertIs(False, direct.is_error)

    def test_list_entry_and_scan_budgets_are_deterministic(self) -> None:
        for name in ("c.txt", "a.txt", "b.txt"):
            (self.root / name).write_text(name, encoding="utf-8")
        with build_repository_tool_registry(self.root) as registry:
            result, document = _execute(
                registry,
                "list_files",
                {"path": ".", "max_depth": 0, "max_entries": 2},
            )
        self.assertEqual(["a.txt", "b.txt"], [item["path"] for item in document["entries"]])
        self.assertIs(True, document["truncated"])
        self.assertEqual("entry_limit", document["truncation_reason"])
        self.assertLessEqual(len(result.content), RepositoryToolLimits().max_output_chars)

        strict = RepositoryToolLimits(
            max_directory_entries=2,
            max_directory_scan_entries=2,
        )
        with build_repository_tool_registry(self.root, limits=strict) as registry:
            failed, failed_document = _execute(
                registry,
                "list_files",
                {"path": ".", "max_depth": 0, "max_entries": 2},
            )
        self.assertIs(True, failed.is_error)
        self.assertEqual(
            "workspace_directory_scan_limit_exceeded",
            failed_document["error"]["code"],
        )

    def test_search_is_literal_deterministic_filtered_and_unicode_casefold_indexed(self) -> None:
        (self.root / "b.txt").write_text("nothing\na+b here\n", encoding="utf-8")
        (self.root / "a.txt").write_text("a+b first\n", encoding="utf-8")
        (self.root / "skip.log").write_text("a+b ignored\n", encoding="utf-8")
        (self.root / "unicode.txt").write_text("x Straße y\n", encoding="utf-8")
        with build_repository_tool_registry(self.root) as registry:
            result, document = _execute(
                registry,
                "search_text",
                {
                    "query": "a+b",
                    "path": ".",
                    "max_depth": 0,
                    "max_files": 10,
                    "max_matches": 10,
                    "include": ["*.txt"],
                    "exclude": ["unicode.*"],
                },
            )
            folded, folded_document = _execute(
                registry,
                "search_text",
                {
                    "query": "STRASSE",
                    "path": ".",
                    "max_depth": 0,
                    "max_files": 10,
                    "max_matches": 10,
                    "case_sensitive": False,
                    "include": ["unicode.txt"],
                },
            )

        self.assertIs(False, result.is_error)
        self.assertEqual(
            [("a.txt", 1, 1), ("b.txt", 2, 1)],
            [(item["path"], item["line"], item["column"]) for item in document["matches"]],
        )
        self.assertEqual(1, folded_document["returned_matches"])
        self.assertEqual(3, folded_document["matches"][0]["column"])
        self.assertIs(False, folded.is_error)

    def test_search_enforces_file_byte_match_and_output_limits(self) -> None:
        (self.root / "a.txt").write_text("hit hit hit\n", encoding="utf-8")
        (self.root / "b.txt").write_text("hit\n", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"hit\x00hit")
        limits = RepositoryToolLimits(
            max_file_bytes=100,
            max_search_files=2,
            max_search_total_bytes=100,
            max_matches=2,
            max_match_chars=8,
            max_output_chars=512,
        )
        with build_repository_tool_registry(self.root, limits=limits) as registry:
            result, document = _execute(
                registry,
                "search_text",
                {
                    "query": "hit",
                    "path": ".",
                    "max_depth": 0,
                    "max_files": 2,
                    "max_matches": 2,
                },
            )

        self.assertIs(False, result.is_error)
        self.assertLessEqual(len(result.content), 512)
        self.assertEqual(2, document["returned_matches"])
        self.assertIs(True, document["truncated"])
        self.assertIn(document["truncation_reason"], {"match_limit", "output_limit"})

    def test_literal_match_collection_stops_at_the_remaining_match_budget(self) -> None:
        self.assertEqual(
            [0],
            _literal_columns(
                "a" * 1_000_000,
                "a",
                case_sensitive=True,
                max_columns=1,
            ),
        )
        self.assertEqual(
            [2],
            _literal_columns(
                "x Straße y",
                "STRASSE",
                case_sensitive=False,
                max_columns=1,
            ),
        )

    def test_line_iteration_matches_splitlines_without_materializing_dense_file(self) -> None:
        sample = "a\r\n\r\nb\vc\u2028d\n"
        self.assertEqual(sample.splitlines(), list(_iter_text_lines(sample)))

        dense = "hit\n" * 500_000
        (self.root / "dense.txt").write_text(dense, encoding="utf-8")
        limits = RepositoryToolLimits(
            max_file_bytes=3_000_000,
            max_search_total_bytes=3_000_000,
            max_matches=1,
            max_output_chars=512,
        )
        with build_repository_tool_registry(self.root, limits=limits) as registry:
            read, read_document = _execute(
                registry,
                "read_file",
                {"path": "dense.txt", "start_line": 1, "max_lines": 1},
            )
            searched, search_document = _execute(
                registry,
                "search_text",
                {
                    "query": "hit",
                    "path": ".",
                    "max_depth": 0,
                    "max_files": 1,
                    "max_matches": 1,
                },
            )

        self.assertIs(False, read.is_error)
        self.assertEqual(500_000, read_document["total_lines"])
        self.assertEqual("hit", read_document["content"])
        self.assertIs(False, searched.is_error)
        self.assertEqual(1, search_document["returned_matches"])

    def test_repository_control_files_are_visible_but_never_read_or_searched(self) -> None:
        secret = "gitdir: D:/host/private/.git/worktrees/repository"
        (self.root / ".git").write_text(secret, encoding="utf-8")
        with build_repository_tool_registry(self.root) as registry:
            listed, listing = _execute(
                registry,
                "list_files",
                {"path": ".", "max_depth": 0, "max_entries": 10},
            )
            read, read_error = _execute(
                registry,
                "read_file",
                {"path": ".git", "start_line": 1, "max_lines": 10},
            )
            searched, search = _execute(
                registry,
                "search_text",
                {
                    "query": "gitdir",
                    "path": ".",
                    "max_depth": 0,
                    "max_files": 10,
                    "max_matches": 10,
                },
            )

        self.assertIs(False, listed.is_error)
        self.assertIn(".git", [item["path"] for item in listing["entries"]])
        self.assertIs(True, read.is_error)
        self.assertEqual(
            "repository_control_path_forbidden",
            read_error["error"]["code"],
        )
        self.assertIs(False, searched.is_error)
        self.assertEqual([], search["matches"])
        self.assertEqual(0, search["scanned_files"])
        self.assertNotIn(secret, searched.content)

    def test_control_directory_case_variants_are_never_recursed(self) -> None:
        control = self.root / ".GIT"
        control.mkdir()
        (control / "config").write_text("host-secret", encoding="utf-8")
        with build_repository_tool_registry(self.root) as registry:
            listed, listing = _execute(
                registry,
                "list_files",
                {"path": ".", "max_depth": 4, "max_entries": 10},
            )
            searched, search = _execute(
                registry,
                "search_text",
                {
                    "query": "host-secret",
                    "path": ".",
                    "max_depth": 4,
                    "max_files": 10,
                    "max_matches": 10,
                },
            )

        self.assertIs(False, listed.is_error)
        self.assertEqual([".GIT"], [item["path"] for item in listing["entries"]])
        self.assertIs(False, searched.is_error)
        self.assertEqual([], search["matches"])
        self.assertEqual(1, search["scanned_entries"])

    def test_closed_registry_returns_a_stable_resolver_error(self) -> None:
        (self.root / "one.txt").write_text("one", encoding="utf-8")
        registry = build_repository_tool_registry(self.root)
        registry.close()

        result, document = _execute(
            registry,
            "read_file",
            {"path": "one.txt", "start_line": 1, "max_lines": 1},
        )

        self.assertIs(True, result.is_error)
        self.assertEqual("workspace_resolver_closed", document["error"]["code"])


if __name__ == "__main__":
    unittest.main()
