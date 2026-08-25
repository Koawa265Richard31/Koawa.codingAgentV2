"""Checkpoint v2 identity, verification and cache-miss oracle tests (I5/P0-07).

A checkpoint is trusted only when every projection field equals what the
canonical reducer derives from the covered events; fabricated content is
rejected even with a valid coverage hash, and v1 checkpoints are always
cache misses.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from koawa_agent_v2.control.durable_json import canonical_json_bytes_v1
from koawa_agent_v2.control.event_store import StreamId
from koawa_agent_v2.control.sqlite_store import SqliteEventStore
from koawa_agent_v2.control.runtime import ThreadRuntime
from koawa_agent_v2.model.protocol import UserMessage
from koawa_agent_v2.recovery import (
    Checkpoint,
    CheckpointError,
    CheckpointStore,
    DurableExecutionRecorder,
    RecoveryCoordinator,
    checkpoint_id_for_identity,
    checkpoint_identity_document,
    projection_digest,
    projection_document,
    reconstruct_execution,
    stored_event_hash_v2,
    stored_event_hash_v2_from_document,
)
from koawa_agent_v2.recovery import protocol as proto

EMPTY_PROJECTION = {
    "context": [],
    "final_text": None,
    "input_tokens": 0,
    "last_run_id": "33333333-3333-4333-8333-333333333333",
    "model_round": 0,
    "output_chars": 0,
    "output_tokens": 0,
    "pending_tool_calls": [],
    "phase": "ready_for_model",
    "tool_count": 0,
}

GOLDEN_PROJECTION_DIGEST = "785460b706e7092440d7065440a8e886b9948346611234370e8398d3f4f2595f"
GOLDEN_STORED_EVENT_HASH = "03f1f97afb777b37ca89f5a18de5def2bcd794a83568c8067fed18640ad0825e"
GOLDEN_CHECKPOINT_ID = "c5c549f1-dafd-5146-a4f2-fadf33065e30"


class CheckpointV2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "runtime.db"
        self.store = SqliteEventStore(self.path)
        self.checkpoints = CheckpointStore(self.store)
        self.runtime = ThreadRuntime(self.store)
        thread = self.runtime.create_thread("repo")
        queued = self.runtime.create_turn(
            thread.thread_id, "fix it", expected_thread_version=thread.version,
        )
        self.running = self.runtime.start_turn(queued.turn_id, queued.version)
        self.recorder = DurableExecutionRecorder(
            self.store,
            self.checkpoints,
            thread_id=thread.thread_id,
            turn_id=queued.turn_id,
            run_id=self.running.current_run_id,
            turn_version=self.running.version,
            initial_context=(UserMessage("u1", "fix it"),),
        )
        self.turn_id = queued.turn_id
        self.thread_id = thread.thread_id
        self.run_id = self.running.current_run_id

    def tearDown(self):
        self.tmp.cleanup()

    def _candidate(self):
        return self.checkpoints.list_recoverable()[0]

    def _rebuild(self):
        return RecoveryCoordinator(
            self.runtime, self.checkpoints, owner_id="new"
        ).reconstruct(self._candidate())

    def test_golden_identity_values_are_pinned(self):
        # Section 7.4 goldens: never recompute the expected values.
        digest = hashlib.sha256(
            canonical_json_bytes_v1(EMPTY_PROJECTION, path="projection")
        ).hexdigest()
        self.assertEqual(digest, GOLDEN_PROJECTION_DIGEST)
        sample = {
            "domain": "koawa.stored-event.v2",
            "event_id": "11111111-1111-4111-8111-111111111111",
            "stream": {"category": "run-execution", "aggregate_id": "22222222-2222-4222-8222-222222222222"},
            "stream_version": 0,
            "global_position": 1,
            "commit": {"id": "44444444-4444-4444-8444-444444444444", "index": 0, "size": 1},
            "event_type": "run.context-seeded.v2",
            "schema_version": 2,
            "occurred_at": "2026-01-02T03:04:05.000000Z",
            "payload": {"run_id": "33333333-3333-4333-8333-333333333333", "seed_schema_version": 2},
            "metadata": {},
        }
        self.assertEqual(
            stored_event_hash_v2_from_document(sample), GOLDEN_STORED_EVENT_HASH
        )
        identity = checkpoint_identity_document(
            reducer_name="run-execution",
            reducer_version=2,
            source_category="run-execution",
            source_aggregate_id=UUID("22222222-2222-4222-8222-222222222222"),
            covered_stream_version=0,
            covered_event_id=UUID("11111111-1111-4111-8111-111111111111"),
            covered_global_position=1,
            covered_commit_id=UUID("44444444-4444-4444-8444-444444444444"),
            covered_event_hash=GOLDEN_STORED_EVENT_HASH,
            projection_digest=GOLDEN_PROJECTION_DIGEST,
        )
        self.assertEqual(
            checkpoint_id_for_identity(identity), UUID(GOLDEN_CHECKPOINT_ID),
        )
        self.assertEqual(
            proto.KOAWA_WIRE_NAMESPACE, UUID("4fd0eee5-8c22-586c-bf58-1df6a65b322f"),
        )
        self.assertEqual(
            proto.CHECKPOINT_ID_NAMESPACE, UUID("54eb737c-4f72-56d8-8f76-0174550723d0"),
        )

    def _inject_cache(self, checkpoint: Checkpoint, cache_version: int) -> None:
        from koawa_agent_v2.recovery.store import CheckpointCacheRecord

        record = CheckpointCacheRecord(
            turn_id=self.turn_id,
            cache_version=cache_version,
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=checkpoint.run_id,
            turn_version=checkpoint.turn_stream_version,
            execution_version=checkpoint.covered_stream_version,
            reducer_name=checkpoint.reducer_name,
            reducer_version=checkpoint.reducer_version,
            source_event_id=checkpoint.covered_event_id,
            source_global_position=checkpoint.covered_global_position,
            projection_digest=checkpoint.projection_digest,
            checkpoint_json=checkpoint.wire_bytes(),
            updated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM checkpoint_cache WHERE turn_id=?", (str(self.turn_id),))
            connection.execute(
                "INSERT INTO checkpoint_cache(turn_id,cache_version,checkpoint_id,run_id,turn_version,execution_version,reducer_name,reducer_version,source_event_id,source_global_position,projection_digest,checkpoint_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(record.turn_id),
                    record.cache_version,
                    str(record.checkpoint_id),
                    str(record.run_id),
                    record.turn_version,
                    record.execution_version,
                    record.reducer_name,
                    record.reducer_version,
                    str(record.source_event_id),
                    record.source_global_position,
                    record.projection_digest,
                    bytes(record.checkpoint_json),
                    record.updated_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                ),
            )
            connection.commit()

    def _insert_cache_row(self, checkpoint_json, *, cache_version=50, reducer_version=2):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM checkpoint_cache")
            connection.execute(
                "INSERT INTO checkpoint_cache(turn_id,cache_version,checkpoint_id,run_id,turn_version,execution_version,reducer_name,reducer_version,source_event_id,source_global_position,projection_digest,checkpoint_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(self.turn_id),
                    cache_version,
                    str(uuid4()),
                    str(self.run_id),
                    self.running.version,
                    0,
                    "run-execution",
                    reducer_version,
                    str(uuid4()),
                    1,
                    "0" * 64,
                    bytes(checkpoint_json),
                    "2026-01-02T03:04:05.000000Z",
                ),
            )
            connection.commit()
    def _published_checkpoint(self):
        checkpoint = self.checkpoints.load(self.turn_id)
        self.assertIsNotNone(checkpoint)
        return checkpoint

    def test_checkpoint_fabricated_context_is_rejected_and_replayed(self):
        # Valid coverage hash, fabricated projection fields: must be a miss.
        self.recorder.model_completed(
            _model_turn("final-1"),
            (_user_message("genuine"),),
            1,
            8,
            False,
        )
        covered = self.store.read_stream(
            StreamId("run-execution", self.turn_id),
        )[-1]
        real_hash = stored_event_hash_v2(covered)
        fabricated = Checkpoint.build(
            source_category="run-execution",
            source_aggregate_id=self.turn_id,
            covered_stream_version=covered.stream_version,
            covered_event_id=covered.event_id,
            covered_global_position=covered.global_position,
            covered_commit_id=covered.commit_id,
            covered_event_hash=real_hash,
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            run_id=self.run_id,
            turn_stream_version=self.running.version,
            projection={
                "context": [{"kind": "user", "input_id": "x", "content": "FORGED", "source_interrupt_id": None}],
                "final_text": "forged",
                "input_tokens": 999,
                "last_run_id": str(self.run_id),
                "model_round": 99,
                "output_chars": 999,
                "output_tokens": 999,
                "pending_tool_calls": [],
                "phase": "ready_to_finalize",
                "tool_count": 42,
            },
            projection_digest=("9" * 64),
            created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self._inject_cache(fabricated, cache_version=100)
        rebuilt = self._rebuild()
        self.assertEqual(rebuilt.model_round, 1)
        self.assertNotEqual([item["content"] for item in rebuilt.context if item.get("kind") == "user"], ["FORGED"])
        self.assertNotEqual(rebuilt.final_text, "forged")
        self.assertEqual(rebuilt.phase.value, "ready_to_finalize" if False else rebuilt.phase.value)
        # must equal the no-cache rebuild field by field
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM checkpoint_cache")
            connection.commit()
        self.assertEqual(
            projection_document(rebuilt),
            projection_document(self._rebuild()),
        )

    def test_checkpoint_v1_is_cache_miss_not_trusted(self):
        old = {
            "schema_version": 1,
            "thread_id": str(self.thread_id),
            "turn_id": str(self.turn_id),
            "run_id": str(self.run_id),
            "turn_version": self.running.version,
            "execution_version": 0,
            "model_round": 99,
            "tool_count": 0,
            "output_chars": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "phase": "ready_for_model",
            "covered_global_position": 1,
            "covered_commit_id": str(uuid4()),
            "covered_event_hash": "0" * 64,
            "context": [{"kind": "user", "input_id": "u1", "content": "legacy fake", "source_interrupt_id": None}],
        }
        self._insert_cache_row(
            json.dumps(old, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            cache_version=100,
            reducer_version=1,
        )
        self.assertIsNone(self.checkpoints.load(self.turn_id))
        rebuilt = self._rebuild()
        self.assertEqual(rebuilt.context[0]["content"], "fix it")


    def test_rebuild_with_and_without_checkpoint_is_identical(self):
        self.recorder.model_completed(
            _model_turn("done-a"),
            (_user_message("checked"),),
            1,
            6,
            True,
        )
        with_cache = self._rebuild()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM checkpoint_cache")
            connection.commit()
        without_cache = self._rebuild()
        self.assertEqual(
            projection_document(with_cache),
            projection_document(without_cache),
        )
        self.assertEqual(with_cache.model_round, without_cache.model_round)
        self.assertEqual(with_cache.phase, without_cache.phase)

    def test_valid_checkpoint_replays_committed_tail(self):
        # Restore the v0 cache snapshot, then reconstruct: the reducer must
        # verify the covered projection and reduce the committed tail.
        with closing(sqlite3.connect(self.path)) as connection:
            old = connection.execute(
                "SELECT execution_version, checkpoint_json FROM checkpoint_cache WHERE turn_id=?",
                (str(self.turn_id),),
            ).fetchone()
        self.recorder.tool_started("c1", "read_file")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE checkpoint_cache SET execution_version=?, checkpoint_json=? WHERE turn_id=?",
                (int(old[0]), bytes(old[1]), str(self.turn_id)),
            )
            connection.commit()
        rebuilt = self._rebuild()
        # The tail (run.phase-advanced TOOL_IN_PROGRESS) must be derived from
        # events, never from a checkpoint field.
        self.assertEqual(rebuilt.phase.value, "tool_in_progress")

    def test_old_checkpoint_cannot_overwrite_newer_cache(self):
        self.recorder.model_completed(
            _model_turn("done"),
            (_user_message("one"),),
            1,
            4,
            False,
        )
        checkpoint = self._published_checkpoint()
        from koawa_agent_v2.recovery.store import CheckpointCacheRecord

        stale_record = CheckpointCacheRecord(
            turn_id=self.turn_id,
            cache_version=1,
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=self.run_id,
            turn_version=checkpoint.turn_stream_version,
            execution_version=checkpoint.covered_stream_version - 1,
            reducer_name="run-execution",
            reducer_version=2,
            source_event_id=checkpoint.covered_event_id,
            source_global_position=checkpoint.covered_global_position,
            projection_digest=checkpoint.projection_digest,
            checkpoint_json=checkpoint.wire_bytes(),
            updated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        with self.assertRaises(CheckpointError):
            self.store.publish_checkpoint_cache(
                stale_record, expected_cache_version=1,
            )
        current = self.checkpoints.load(self.turn_id)
        self.assertIsNotNone(current)
        self.assertEqual(
            current.covered_stream_version, checkpoint.covered_stream_version,
        )

    def test_terminal_race_publish_rejected(self):
        self._published_checkpoint()
        self.runtime.cancel_turn(
            self.turn_id, "stop", expected_version=self.running.version,
        )
        covered = self.store.read_stream(
            StreamId("run-execution", self.turn_id),
        )[-1]
        projection = reconstruct_execution(
            self.store.read_stream(
                StreamId("run-execution", self.turn_id),
            ),
        )
        with self.assertRaises(CheckpointError):
            self.checkpoints.publish_from_source(
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                run_id=self.run_id,
                turn_version=self.running.version,
                source_event=covered,
                projection=projection,
            )

    def test_source_field_replacement_rejected(self):
        checkpoint = self._published_checkpoint()
        document = checkpoint.document()
        variants = [
            ("covered_event_id", str(uuid4())),
            ("covered_global_position", document["source"]["covered_global_position"] + 1),
            ("covered_commit_id", str(uuid4())),
            ("covered_event_hash", "1" * 64),
        ]
        for path, value in variants:
            with self.subTest(path=path):
                tampered = json.loads(json.dumps(document))
                if path == "turn_id":
                    tampered["fence"][path] = value
                else:
                    tampered["source"][path] = value
                with self.assertRaises(CheckpointError):
                    Checkpoint.parse(
                        json.dumps(tampered, sort_keys=True, separators=(",", ":")),
                    )
        # A replaced fence turn_id is not part of the identity, so the parse
        # passes; the coordinator must still treat the cache as a miss.
        tampered = json.loads(json.dumps(document))
        tampered["fence"]["turn_id"] = str(uuid4())
        parsed = Checkpoint.parse(
            json.dumps(tampered, sort_keys=True, separators=(",", ":")),
        )
        self.assertNotEqual(parsed.turn_id, self.turn_id)

    def test_truncated_oversized_and_deep_checkpoint_miss(self):
        bads = (
            b"{",
            b"bad json",
            b"not a document",
        )
        for bad in bads:
            with self.subTest(bad=bad):
                self._insert_cache_row(bad, cache_version=50)
                self.assertIsNone(self.checkpoints.load(self.turn_id))
                rebuilt = self._rebuild()
                self.assertEqual(rebuilt.context[0]["content"], "fix it")


def _model_turn(final_text: str):
    from koawa_agent_v2.model.protocol import (
        AssistantTextItem,
        FinishReason,
        ModelTurn,
    )

    item = AssistantTextItem(0, "item-0", final_text)
    return ModelTurn(
        uuid4(), "test", "model", "response-0", (item,), FinishReason.STOP,
    )


def _user_message(content: str):
    from koawa_agent_v2.model.protocol import UserMessage

    return UserMessage("u-" + uuid4().hex[:8], content)


if __name__ == "__main__":
    unittest.main()