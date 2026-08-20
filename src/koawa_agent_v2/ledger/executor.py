"""The only durable Runtime path from a canonical ToolCall to a handler."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from threading import Lock
from uuid import UUID, uuid4

from ..execution.loop import (
    AgentLoopCancelled,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolExecutor,
)
from ..model.protocol import ToolCallItem, ToolDefinition
from ..approval_service import (
    ApprovalDenied,
    ApprovalRecord,
    ApprovalService,
    ApprovalStatus,
)
from ..control.event_store import StreamId
from ..policy import Decision, PolicyEngine, PolicyVerdict, ResolvedAction
from ..telemetry.trace import TraceStore
from .protocol import (
    DurableToolResult,
    ToolExecutionRecord,
    ToolExecutionState,
    ToolLedgerConflict,
    ToolLedgerError,
    ToolOutcomeBlocked,
    ToolRecoveryProfile,
    RecoveryMode,
)
from .store import ToolLedgerStore


LedgerFaultHook = Callable[[str, ToolExecutionRecord], None]
ActionResolver = Callable[
    [ToolCallItem, ToolExecutionContext, ToolRecoveryProfile, ResolvedAction | None],
    ResolvedAction,
]


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizedToolCall:
    """Process-local, non-persisted ticket issued only after the D9 policy gate."""

    _executor: "LedgerExecutor"
    _token: object
    _issuance: object
    call: ToolCallItem
    context: ToolExecutionContext
    prepared_invocation: object | None
    record: ToolExecutionRecord | None
    early_result: ToolExecutionResult | None
    action: ResolvedAction | None
    verdict: PolicyVerdict | None
    approval: ApprovalRecord | None

    def __repr__(self) -> str:
        return (
            f"AuthorizedToolCall(tool_name={self.call.name!r}, "
            f"early_result={self.early_result is not None}, "
            f"policy_bound={self.action is not None})"
        )


class LedgerExecutor:
    """Write-ahead claim, execute once per claim, and durably record the result."""

    durable_ledger = True

    def __init__(
        self,
        delegate: ToolExecutor,
        ledger: ToolLedgerStore,
        profiles: Mapping[str, ToolRecoveryProfile],
        *,
        fault_hook: LedgerFaultHook | None = None,
        policy_engine: PolicyEngine | None = None,
        approval_service: ApprovalService | None = None,
        action_resolvers: Mapping[str, ActionResolver] | None = None,
        trace_store: TraceStore | None = None,
        correlation_id: object | None = None,
    ) -> None:
        if not hasattr(delegate, "definitions") or not hasattr(delegate, "execute"):
            raise TypeError("delegate must implement ToolExecutor")
        if not isinstance(ledger, ToolLedgerStore):
            raise TypeError("ledger must be ToolLedgerStore")
        definitions = tuple(delegate.definitions())
        if not all(isinstance(item, ToolDefinition) for item in definitions):
            raise TypeError("delegate definitions contain an invalid item")
        copied_profiles = dict(profiles)
        if set(copied_profiles) != {item.name for item in definitions}:
            raise ValueError("profiles must exactly cover tool definitions")
        if not all(isinstance(item, ToolRecoveryProfile) for item in copied_profiles.values()):
            raise TypeError("profiles contain an invalid recovery profile")
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable or None")
        if trace_store is not None and not isinstance(trace_store, TraceStore):
            raise TypeError("trace_store must be TraceStore or None")
        self._delegate = delegate
        self._ledger = ledger
        self._profiles = copied_profiles
        self._definitions = definitions
        self._fault_hook = fault_hook
        self._trace_store = trace_store
        self._correlation_id = correlation_id
        configured = (
            policy_engine is not None,
            approval_service is not None,
            action_resolvers is not None,
        )
        if any(configured) and not all(configured):
            raise ValueError(
                "policy_engine, approval_service and action_resolvers "
                "must be configured together"
            )
        if policy_engine is not None and not isinstance(policy_engine, PolicyEngine):
            raise TypeError("policy_engine must be PolicyEngine or None")
        if approval_service is not None and not isinstance(
            approval_service, ApprovalService
        ):
            raise TypeError("approval_service must be ApprovalService or None")
        copied_resolvers = (
            {} if action_resolvers is None else dict(action_resolvers)
        )
        if policy_engine is not None:
            if set(copied_resolvers) != {item.name for item in definitions}:
                raise ValueError("action_resolvers must exactly cover tool definitions")
            if not all(callable(value) for value in copied_resolvers.values()):
                raise TypeError("action_resolvers contains a non-callable value")
        self._policy_engine = policy_engine
        self._approval_service = approval_service
        self._action_resolvers = copied_resolvers
        self._authorization_token = object()
        self._ticket_lock = Lock()
        self._live_tickets: dict[int, AuthorizedToolCall] = {}
        self._active_claim_tickets: dict[object, AuthorizedToolCall] = {}
        prepared_methods = (
            hasattr(delegate, "prepare"),
            hasattr(delegate, "invoke_prepared"),
            hasattr(delegate, "bind_policy_authority"),
            hasattr(delegate, "discard_prepared"),
        )
        if any(prepared_methods) and not all(prepared_methods):
            raise TypeError("delegate prepared invocation API is incomplete")
        self._prepared_delegate = all(prepared_methods)
        self._registry_authority = object()
        if policy_engine is not None and not self._prepared_delegate:
            raise ValueError("policy requires a schema-gated prepared delegate")
        if self._prepared_delegate:
            delegate.bind_policy_authority(self._registry_authority)

    @property
    def ledger(self) -> ToolLedgerStore:
        return self._ledger

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """Compatibility entry; AgentLoop uses authorize before D6 tool_started."""
        return self.execute_authorized(self.authorize(call, context=context))

    def authorize(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> AuthorizedToolCall:
        """Schema-check, authorize and claim without invoking the handler."""
        if not isinstance(call, ToolCallItem):
            raise TypeError("call must be ToolCallItem")
        if not isinstance(context, ToolExecutionContext):
            raise TypeError("context must be ToolExecutionContext")
        if context.turn_id is None or context.turn_version is None:
            raise ToolLedgerError("durable_turn_identity_required")
        if (
            context.call_ref.model_turn_id != context.model_turn_id
            or context.call_ref.call_id != call.call_id
        ):
            raise ToolLedgerConflict("tool_call_context_mismatch")
        profile = self._profiles.get(call.name)
        if profile is None:
            raise ToolLedgerError("tool_recovery_profile_missing")

        existing = self._ledger.load_for_call(
            context.turn_id,
            context.call_ref.model_turn_id,
            context.call_ref.call_id,
        )
        arguments_were_redacted = (
            context.recovered_call and "[REDACTED]" in call.arguments_json
        )
        if arguments_were_redacted and existing is not None:
            if existing.tool_name != call.name or existing.profile != profile:
                raise ToolLedgerConflict("tool_execution_identity_conflict")
            record = existing
        elif arguments_were_redacted:
            raise ToolOutcomeBlocked("recovered_tool_arguments_unavailable")
        else:
            prepared = (
                self._delegate.prepare(call)
                if self._prepared_delegate
                else None
            )
            early_result = (
                getattr(prepared, "early_result", None)
                if prepared is not None
                else None
            )
            if early_result is not None:
                return self._ticket(
                    call,
                    context,
                    prepared=prepared,
                    record=None,
                    early_result=early_result,
                )
            binding_digest = (
                self._delegate.binding_digest(call.name)
                if self._prepared_delegate
                and hasattr(self._delegate, "binding_digest")
                else None
            )
            record = self._ledger.prepare(
                turn_id=context.turn_id,
                turn_version=context.turn_version,
                run_id=context.run_id,
                model_turn_id=context.call_ref.model_turn_id,
                call_id=context.call_ref.call_id,
                tool_name=call.name,
                arguments_json=call.arguments_json,
                profile=profile,
                binding_digest=binding_digest,
            )
        if arguments_were_redacted:
            prepared = None
        try:
            self._fault("after_prepare", record)
            self._trace("ledger", "prepared", {"tool_name": call.name})
            if record.state in (ToolExecutionState.SUCCEEDED, ToolExecutionState.FAILED):
                if record.result is None:
                    raise ToolLedgerError("durable_tool_result_missing")
                if self._policy_engine is not None:
                    if arguments_were_redacted:
                        return self._ticket(
                            call,
                            context,
                            prepared=prepared,
                            record=record,
                            early_result=_policy_error(
                                "policy_revalidation_unavailable"
                            ),
                        )
                    return self._authorize_terminal(
                        call,
                        context,
                        profile,
                        prepared,
                        record,
                    )
                return self._ticket(
                    call,
                    context,
                    prepared=prepared,
                    record=record,
                )
            if record.state is ToolExecutionState.OUTCOME_UNKNOWN:
                raise ToolOutcomeBlocked("tool_outcome_unknown")
            if arguments_were_redacted:
                raise ToolOutcomeBlocked("recovered_tool_arguments_unavailable")
            if self._policy_engine is None:
                claimed = self._ledger.claim(
                    record,
                    turn_version=context.turn_version,
                    run_id=context.run_id,
                )
                self._fault("after_claim", claimed)
                self._trace("ledger", "claimed", {"tool_name": call.name, "state": "claimed"})
                return self._ticket(
                    call,
                    context,
                    prepared=prepared,
                    record=claimed,
                )
            resolver = self._action_resolvers[call.name]
            first = self._resolve(resolver, call, context, profile, None)
            first_verdict = self._policy_engine.evaluate(first)
            try:
                first_grant = self._require_grant(
                    record, first, first_verdict, context=context
                )
            except ApprovalDenied as error:
                return self._ticket(
                    call,
                    context,
                    prepared=prepared,
                    record=record,
                    early_result=_policy_error(error.code),
                    action=first,
                    verdict=first_verdict,
                )
            # Approval is bound to resolved identities, not only raw arguments.
            # Re-run all path/DNS/resource resolution before the side-effect
            # phase so a changed cwd/symlink/DNS answer cannot inherit a grant.
            final = self._resolve(resolver, call, context, profile, first)
            final_verdict = self._policy_engine.evaluate(final)
            try:
                final_grant = self._require_grant(
                    record, final, final_verdict, context=context
                )
            except ApprovalDenied as error:
                return self._ticket(
                    call,
                    context,
                    prepared=prepared,
                    record=record,
                    early_result=_policy_error(error.code),
                    action=final,
                    verdict=final_verdict,
                )
            if first.action_digest != final.action_digest and first_grant is not None:
                if final_verdict.decision is Decision.ALLOW:
                    drift_verdict = PolicyVerdict(
                        Decision.ASK,
                        "approval_required",
                        final.policy_version,
                        final.action_digest,
                        final_verdict.matched_rule_ids,
                    )
                    try:
                        final_grant = self._require_grant(
                            record, final, drift_verdict, context=context
                        )
                    except ApprovalDenied as error:
                        return self._ticket(
                            call,
                            context,
                            prepared=prepared,
                            record=record,
                            early_result=_policy_error(error.code),
                            action=final,
                            verdict=drift_verdict,
                        )
                    final_verdict = drift_verdict
                # Never carry the old approval object across resolved drift.
                first_grant = None
            if self._approval_service is None:
                raise ToolLedgerError("approval_service_missing")
            claimed = self._approval_service.claim(
                record,
                final,
                final_verdict,
                final_grant,
                context=context,
            )
            self._fault("after_claim", claimed)
            self._trace("ledger", "claimed", {"tool_name": call.name, "state": "claimed"})
            return self._ticket(
                call,
                context,
                prepared=prepared,
                record=claimed,
                action=final,
                verdict=final_verdict,
                approval=final_grant,
            )
        except Exception:
            self._discard_prepared_invocation(prepared)
            raise

    def execute_authorized(
        self,
        authorization: AuthorizedToolCall,
    ) -> ToolExecutionResult:
        """Consume one executor-issued ticket and invoke at most one handler."""
        if not isinstance(authorization, AuthorizedToolCall):
            raise TypeError("authorization must be AuthorizedToolCall")
        if (
            authorization._executor is not self
            or authorization._token is not self._authorization_token
        ):
            raise ToolLedgerError("invalid_policy_authorization")
        with self._ticket_lock:
            if self._live_tickets.get(id(authorization)) is not authorization:
                raise ToolLedgerError("invalid_policy_authorization")
            del self._live_tickets[id(authorization)]
        try:
            if authorization.early_result is not None:
                return authorization.early_result
            record = authorization.record
            if record is None:
                raise ToolLedgerError("authorized_tool_record_missing")
            if record.state in (ToolExecutionState.SUCCEEDED, ToolExecutionState.FAILED):
                if record.result is None:
                    raise ToolLedgerError("durable_tool_result_missing")
                return ToolExecutionResult(record.result.content, record.result.is_error)
            context = authorization.context
            claimed = record
            if claimed.state in (ToolExecutionState.SUCCEEDED, ToolExecutionState.FAILED):
                if claimed.result is None:
                    raise ToolLedgerError("durable_tool_result_missing")
                return ToolExecutionResult(claimed.result.content, claimed.result.is_error)
            if claimed.state is not ToolExecutionState.CLAIMED:
                raise ToolLedgerError("authorized_tool_claim_missing")
            if authorization.action is not None:
                if self._approval_service is None:
                    raise ToolLedgerError("approval_service_missing")
                self._approval_service.begin_execution(
                    claimed,
                    authorization.action,
                    context=context,
                )
            try:
                self._fault("before_handler", claimed)
                delegated_context = replace(
                    context, execution_id=claimed.execution_id
                )
                if self._prepared_delegate:
                    if authorization.prepared_invocation is None:
                        raise ToolLedgerError("prepared_invocation_missing")
                    result = self._delegate.invoke_prepared(
                        authorization.prepared_invocation,
                        context=delegated_context,
                        authority=self._registry_authority,
                    )
                else:
                    result = self._delegate.execute(
                        authorization.call,
                        context=delegated_context,
                    )
                if not isinstance(result, ToolExecutionResult):
                    raise ToolLedgerError("invalid_tool_executor_result")
                self._fault("after_handler", claimed)
            except AgentLoopCancelled:
                raise
            except Exception:
                if claimed.profile.recovery_mode is RecoveryMode.RETRY:
                    raise ToolOutcomeBlocked("tool_retry_required") from None
                try:
                    self._ledger.mark_outcome_unknown(
                        claimed,
                        "handler_raised_after_claim",
                        actor="ledger",
                    )
                except Exception:
                    # A concurrent recovery may already have fenced or resolved
                    # this claim. Never expose the untrusted handler exception.
                    pass
                raise ToolOutcomeBlocked("tool_outcome_unknown") from None

            committed = self._ledger.commit_result(
                claimed,
                DurableToolResult(result.content, result.is_error),
            )
            self._release_active_claim(authorization)
            self._fault("after_result_commit", committed)
            self._trace(
                "tool",
                "result",
                {
                    "tool_name": authorization.call.name,
                    "result_code": "error" if result.is_error else "ok",
                },
            )
            return result
        finally:
            self._discard_prepared(authorization)

    def _ticket(
        self,
        call: ToolCallItem,
        context: ToolExecutionContext,
        *,
        prepared: object | None,
        record: ToolExecutionRecord | None,
        early_result: ToolExecutionResult | None = None,
        action: ResolvedAction | None = None,
        verdict: PolicyVerdict | None = None,
        approval: ApprovalRecord | None = None,
    ) -> AuthorizedToolCall:
        ticket = AuthorizedToolCall(
            self,
            self._authorization_token,
            object(),
            call,
            context,
            prepared,
            record,
            early_result,
            action,
            verdict,
            approval,
        )
        with self._ticket_lock:
            if (
                record is not None
                and record.state is ToolExecutionState.CLAIMED
                and record.claim_token is not None
            ):
                if record.claim_token in self._active_claim_tickets:
                    raise ToolOutcomeBlocked("tool_claim_already_active")
                self._active_claim_tickets[record.claim_token] = ticket
            self._live_tickets[id(ticket)] = ticket
        return ticket

    def _release_active_claim(self, ticket: AuthorizedToolCall) -> None:
        record = ticket.record
        if record is None or record.claim_token is None:
            return
        with self._ticket_lock:
            if self._active_claim_tickets.get(record.claim_token) is ticket:
                del self._active_claim_tickets[record.claim_token]

    def _discard_prepared(self, ticket: AuthorizedToolCall) -> None:
        self._discard_prepared_invocation(ticket.prepared_invocation)

    def _discard_prepared_invocation(self, prepared: object | None) -> None:
        if prepared is None or not self._prepared_delegate:
            return
        self._delegate.discard_prepared(
            prepared,
            authority=self._registry_authority,
        )

    def _authorize_terminal(
        self,
        call: ToolCallItem,
        context: ToolExecutionContext,
        profile: ToolRecoveryProfile,
        prepared: object | None,
        record: ToolExecutionRecord,
    ) -> AuthorizedToolCall:
        resolver = self._action_resolvers[call.name]
        first = self._resolve(resolver, call, context, profile, None)
        final = self._resolve(resolver, call, context, profile, first)
        verdict = self._policy_engine.evaluate(final)
        if first.action_digest != final.action_digest:
            return self._ticket(
                call, context, prepared=prepared, record=record,
                early_result=_policy_error("policy_resource_drift"),
                action=final, verdict=verdict,
            )
        evidence = self._claim_authority(record)
        expected = (
            final.action_digest,
            final.principal.principal_id,
            final.policy_version,
        )
        if evidence is None:
            code = "policy_authorization_evidence_missing"
        elif evidence != expected:
            code = "policy_authorization_evidence_mismatch"
        elif verdict.decision is Decision.DENY:
            code = verdict.code
        else:
            code = ""
        approval: ApprovalRecord | None = None
        if not code and verdict.decision is Decision.ASK:
            if self._approval_service is None:
                code = "approval_service_missing"
            else:
                approval = self._approval_service.load(record.execution_id)
                if not (
                    approval is not None
                    and approval.status is ApprovalStatus.CONSUMED
                    and approval.action_digest == final.action_digest
                    and approval.principal_id == final.principal.principal_id
                    and approval.policy_version == final.policy_version
                    and approval.claim_token == record.claim_token
                ):
                    code = "approval_grant_required"
        return self._ticket(
            call,
            context,
            prepared=prepared,
            record=record,
            early_result=_policy_error(code) if code else None,
            action=final,
            verdict=verdict,
            approval=approval,
        )

    def _claim_authority(
        self,
        record: ToolExecutionRecord,
    ) -> tuple[str, str, str] | None:
        cursor = -1
        stream = StreamId("tool-execution", record.execution_id)
        expected_token = str(record.claim_token)
        matched = None
        while True:
            page = self._ledger.event_store.read_stream(
                stream, after_version=cursor, limit=500
            )
            for event in page:
                if event.event_type not in (
                    "tool.execution-claimed.v1",
                    "tool.execution-reclaimed.v1",
                ):
                    continue
                payload = event.payload
                if payload.get("claim_token") != expected_token:
                    continue
                values = (
                    payload.get("action_digest"),
                    payload.get("principal_id"),
                    payload.get("policy_version"),
                )
                if all(isinstance(value, str) for value in values):
                    matched = values
            if len(page) < 500:
                return matched
            cursor = page[-1].stream_version

    @staticmethod
    def _resolve(
        resolver: ActionResolver,
        call: ToolCallItem,
        context: ToolExecutionContext,
        profile: ToolRecoveryProfile,
        previous: ResolvedAction | None,
    ) -> ResolvedAction:
        try:
            action = resolver(call, context, profile, previous)
        except Exception as exc:
            if hasattr(exc, "code"):
                raise
            raise ToolLedgerError("action_resolution_failed") from None
        if not isinstance(action, ResolvedAction):
            raise ToolLedgerError("invalid_resolved_action")
        if (
            action.tool_name != call.name
            or action.side_effect_class.value != profile.side_effect_class.value
        ):
            raise ToolLedgerConflict("resolved_action_identity_conflict")
        return action

    def _require_grant(
        self,
        record: ToolExecutionRecord,
        action: ResolvedAction,
        verdict: PolicyVerdict,
        *,
        context: ToolExecutionContext,
    ) -> ApprovalRecord | None:
        if self._approval_service is None:
            raise ToolLedgerError("approval_service_missing")
        return self._approval_service.require_grant(
            record,
            action,
            verdict,
            context=context,
            prompt=f"Approve {action.kind.value} {action.tool_name}?",
        )

    def _fault(self, point: str, record: ToolExecutionRecord) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, record)

    def _trace(self, stream: str, kind: str, fields: Mapping[str, object]) -> None:
        if self._trace_store is None:
            return
        correlation_id = self._correlation_id
        if not isinstance(correlation_id, UUID):
            correlation_id = uuid4()
        self._trace_store.append(
            correlation_id=correlation_id,
            stream=stream,
            kind=kind,
            fields=fields,
        )


def _policy_error(code: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        json.dumps(
            {"error": "policy_denied", "code": code},
            sort_keys=True,
            separators=(",", ":"),
        ),
        True,
    )
