"""D7 recovery decisions for orphaned durable tool claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from ..recovery.redaction import REDACTED
from .protocol import (
    AuthoritativeLookup,
    LookupOutcome,
    LookupResult,
    RecoveryMode,
    ToolExecutionRecord,
    ToolExecutionState,
)
from .store import ToolLedgerStore


class ToolRecoveryManager:
    """Reconcile D6 pending calls without ever blindly replaying risky writes."""

    def __init__(
        self,
        ledger: ToolLedgerStore,
        lookups: Mapping[str, AuthoritativeLookup] | None = None,
    ) -> None:
        if not isinstance(ledger, ToolLedgerStore):
            raise TypeError("ledger must be ToolLedgerStore")
        copied = dict(lookups or {})
        if not all(isinstance(name, str) and callable(value) for name, value in copied.items()):
            raise TypeError("lookups must map tool names to callables")
        self._ledger = ledger
        self._lookups = copied

    def reconcile_pending(
        self,
        turn_id: UUID,
        pending_tool_calls: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Return true only when a new Run can safely consume every pending call."""

        pending = tuple(pending_tool_calls)
        if not pending:
            return False
        for document in pending:
            try:
                model_turn_id = UUID(document["model_turn_id"])
                call_id = document["call_id"]
            except (KeyError, TypeError, ValueError, AttributeError):
                return False
            record = self._ledger.load_for_call(turn_id, model_turn_id, call_id)
            item = document.get("item")
            arguments_json = (
                item.get("arguments_json")
                if isinstance(item, Mapping)
                else None
            )
            arguments_unavailable = (
                isinstance(arguments_json, str)
                and REDACTED in arguments_json
            )
            if record is None or record.state is ToolExecutionState.PREPARED:
                # D6 may have persisted TOOL_IN_PROGRESS immediately before the
                # LedgerExecutor prepared the call. With no claim, no handler was run.
                if arguments_unavailable:
                    return False
                continue
            if record.state in (ToolExecutionState.SUCCEEDED, ToolExecutionState.FAILED):
                continue
            if record.state is ToolExecutionState.OUTCOME_UNKNOWN:
                if (
                    record.profile.recovery_mode
                    is RecoveryMode.AUTHORITATIVE_QUERY
                ):
                    if not self._reconcile_query(record):
                        return False
                    continue
                return False
            if record.state is not ToolExecutionState.CLAIMED:
                return False
            if record.profile.recovery_mode is RecoveryMode.RETRY:
                # The next active Run will append a new claim epoch under its own
                # exact Turn fence before invoking the handler.
                if arguments_unavailable:
                    return False
                continue
            if record.profile.recovery_mode is RecoveryMode.MANUAL:
                self._ledger.mark_outcome_unknown(
                    record,
                    "orphaned_unqueryable_claim",
                )
                return False

            if not self._reconcile_query(record):
                return False
        return True

    def _reconcile_query(self, record: ToolExecutionRecord) -> bool:
        lookup = self._lookups.get(record.tool_name)
        if lookup is None:
            self._mark_unknown(record, "authoritative_lookup_missing")
            return False
        try:
            decision = lookup(record)
        except Exception:
            self._mark_unknown(record, "authoritative_lookup_failed")
            return False
        if not isinstance(decision, LookupResult):
            self._mark_unknown(record, "authoritative_lookup_invalid")
            return False
        if decision.outcome is LookupOutcome.APPLIED:
            if decision.result is None:  # guarded by LookupResult
                return False
            if record.state is ToolExecutionState.OUTCOME_UNKNOWN:
                self._ledger.resolve_unknown_result(
                    record,
                    decision.result,
                    actor="recovery",
                )
            else:
                self._ledger.commit_result(
                    record,
                    decision.result,
                    actor="recovery",
                )
            return True
        if decision.outcome is LookupOutcome.NOT_APPLIED:
            if record.state is ToolExecutionState.OUTCOME_UNKNOWN:
                self._ledger.resolve_unknown_not_applied(
                    record,
                    actor="recovery",
                )
            else:
                self._ledger.release_not_applied(record)
            return True
        self._mark_unknown(record, "authoritative_lookup_unknown")
        return False

    def _mark_unknown(self, record: ToolExecutionRecord, reason: str) -> None:
        if record.state is ToolExecutionState.CLAIMED:
            self._ledger.mark_outcome_unknown(record, reason)
