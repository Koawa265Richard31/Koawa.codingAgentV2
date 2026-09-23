"""D2 的有界模型—工具 Agent Loop。

Loop 只接收已经完成并通过 ``ModelStreamAssembler`` 校验的 ModelTurn，随后
决定结束或调用 D3 将实现的 ToolExecutor 端口。它不负责 D1 生命周期落库；
``execution/worker.py`` 才是两层之间的适配边界。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from threading import Event
from typing import Callable, Iterable, Protocol, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from ..telemetry.trace import TraceProbe, TraceSink

from ..model.protocol import (
    AssistantMessage,
    AssistantTextItem,
    BlockedItem,
    FinishReason,
    InstructionMessage,
    ModelCallRef,
    ModelContextItem,
    ModelError,
    ModelRequest,
    ModelStreamEvent,
    ModelTurn,
    PublicReasoningSummaryItem,
    ReasoningSummaryEcho,
    ToolCallEcho,
    ToolCallItem,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
)
from ..model.stream import ModelStreamAssembler, StreamLimits


_ERROR_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class AgentLoopError(Exception):
    """可以安全写入 D1 failure outcome 的稳定 Loop 错误。"""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
            raise ValueError("invalid agent loop error code")
        self.code = code
        super().__init__(code)


class AgentLoopCancelled(AgentLoopError):
    """协作式取消边界；partial 模型输出不能成为成功结果。"""

    def __init__(self) -> None:
        super().__init__("agent_loop_cancelled")


class AgentLoopRecoveryBlocked(AgentLoopError):
    """A durable tool claim needs recovery/query/operator action."""


class AgentLoopApprovalWaiting(AgentLoopError):
    """Policy durably suspended the Turn before any tool claim or side effect."""


class ModelClient(Protocol):
    """Provider adapter 需要实现的同步 typed-stream 端口。"""

    def stream(self, request: ModelRequest) -> Iterable[ModelStreamEvent]:
        """返回一条以 typed terminal 结束的 canonical event stream。"""


@dataclass(frozen=True, slots=True, repr=False)
class ToolExecutionResult:
    """D3 Tool Registry 返回给 Loop 的最小模型可见结果。"""

    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or "\x00" in self.content:
            raise ValueError("tool result content is invalid")
        try:
            self.content.encode("utf-8", "strict")
        except UnicodeError:
            # Provider request adapters最终都要编码为 UTF-8；在工具结果边界拒绝
            # 未配对 surrogate，避免到下一轮 transport 才出现延迟失败。
            raise ValueError("tool result content is invalid") from None
        if not isinstance(self.is_error, bool):
            raise TypeError("is_error must be bool")

    def __repr__(self) -> str:
        return (
            f"ToolExecutionResult(content_length={len(self.content)}, "
            f"is_error={self.is_error})"
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """工具调用的本地执行身份；D7 ledger 会扩展其副作用语义。"""

    run_id: UUID
    model_turn_id: UUID
    model_round: int
    call_ref: ModelCallRef
    turn_id: UUID | None = None
    turn_version: int | None = None
    execution_id: UUID | None = None
    # PSEC semantics-C: ancestor turn ids whose canary seeds this execution's
    # J2 scan must also test (delegation chain).  Empty = own-turn scan only.
    ancestor_turn_ids: tuple[UUID, ...] = ()
    recovered_call: bool = False
    progress_guard: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def check_progress(self) -> None:
        """长工具在自己的阻塞循环中复查取消与当前 Run ownership。"""
        if self.progress_guard is not None:
            self.progress_guard()


class ToolExecutor(Protocol):
    """D3 Tool Registry 实现的定义与执行单一入口。"""

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回模型可见工具定义的冻结快照。"""

    def execute(
        self,
        call: ToolCallItem,
        *,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """执行一个完整调用；普通工具失败用 ``is_error=True`` 返回。"""


class CompletionGate(Protocol):
    """D5 验证闸门：模型文本不能替代真实测试与 diff 证据。"""

    def assert_complete(self, run_id: UUID) -> None:
        """证据不足时抛出带稳定 code 的 ``AgentLoopError``。"""


class DurableExecutionSink(Protocol):
    """D6 typed-fact sink; called only for complete canonical values."""
    def model_completed(self, turn: ModelTurn, projected: Sequence[ModelContextItem], model_round: int, output_chars: int, has_tools: bool) -> None: ...
    def tool_started(self, call_id: str, tool_name: str) -> None: ...
    def tool_completed(self, result: ToolResultMessage, tool_count: int) -> None: ...


class CancellationToken:
    """线程安全的协作式取消标记。"""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise AgentLoopCancelled()


@dataclass(frozen=True, slots=True)
class AgentLoopLimits:
    """约束一个 Turn 内的模型轮数、工具数和累计文本。"""

    max_model_rounds: int = 32
    max_tool_calls: int = 128
    max_total_output_chars: int = 8_000_000

    def __post_init__(self) -> None:
        for name in (
            "max_model_rounds",
            "max_tool_calls",
            "max_total_output_chars",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True, repr=False)
class AgentLoopResult:
    """一次 Loop 的结果；D6 recorder 可从 typed facts 跨进程重建。"""

    final_text: str
    context: tuple[ModelContextItem, ...]
    model_turns: tuple[ModelTurn, ...]
    model_rounds: int
    tool_calls: int

    def __repr__(self) -> str:
        return (
            f"AgentLoopResult(final_text_length={len(self.final_text)}, "
            f"context_count={len(self.context)}, model_rounds={self.model_rounds}, "
            f"tool_calls={self.tool_calls})"
        )


ModelEventSink = Callable[[ModelStreamEvent], None]
OwnershipGuard = Callable[[], None]


class AgentLoop:
    """完整验证模型回合后，确定性执行工具或返回最终答案。"""

    def __init__(
        self,
        client: ModelClient,
        *,
        tool_executor: ToolExecutor | None = None,
        completion_gate: CompletionGate | None = None,
        claim_gate: bool = False,
        limits: AgentLoopLimits | None = None,
        stream_limits: StreamLimits | None = None,
        trace_sink: TraceSink | None = None,
        trace_store: object | None = None,
        correlation_id: object | None = None,
        memory: object | None = None,
        compaction_sink: object | None = None,
        ancestor_turn_ids: tuple[UUID, ...] = (),
    ) -> None:
        if not hasattr(client, "stream"):
            raise TypeError("client must implement ModelClient")
        if tool_executor is not None and (
            not hasattr(tool_executor, "execute")
            or not hasattr(tool_executor, "definitions")
        ):
            raise TypeError("tool_executor must implement ToolExecutor")
        if completion_gate is not None and not hasattr(
            completion_gate, "assert_complete"
        ):
            raise TypeError("completion_gate must implement CompletionGate")
        if not isinstance(claim_gate, bool):
            raise TypeError("claim_gate must be bool")
        self._client = client
        self._tool_executor = tool_executor
        self._completion_gate = completion_gate
        self._claim_gate = claim_gate
        # PSEC semantics-C (T7-b): ancestor canary seed tokens are derived
        # per-call from these turn ids (HMAC, never stored on the context or
        # in events).  Empty by default — single-agent runs scan own turn only.
        if not all(isinstance(item, UUID) for item in ancestor_turn_ids):
            raise TypeError("ancestor_turn_ids must be a tuple of UUIDs")
        self._ancestor_turn_ids = tuple(ancestor_turn_ids)
        self._successful_writes: frozenset[str] = frozenset()
        definitions = (
            tuple(tool_executor.definitions())
            if tool_executor is not None
            else ()
        )
        if not all(isinstance(item, ToolDefinition) for item in definitions):
            raise TypeError("tool executor definitions contain an invalid item")
        if len({item.name for item in definitions}) != len(definitions):
            raise ValueError("tool executor definitions contain duplicate names")
        self._tool_definitions = definitions
        self._available_tools = frozenset(item.name for item in definitions)
        # I6 §8.8: the snapshot consumed by the most recent model round.
        self._last_snapshot = None
        self._limits = limits or AgentLoopLimits()
        self._stream_limits = stream_limits or StreamLimits()
        if trace_sink is not None and trace_store is not None:
            raise ValueError("use only one of trace_sink or trace_store")
        if trace_sink is not None and not callable(getattr(trace_sink, "emit", None)):
            raise TypeError("trace_sink must implement TraceSink")
        # trace_store is a compatibility path for older tests/callers. Runtime
        # assembly exclusively supplies the failure-isolated TraceSink.
        if trace_store is not None and not callable(getattr(trace_store, "append", None)):
            raise TypeError("trace_store must provide append()")
        self._trace_sink = trace_sink
        self._legacy_trace_store = trace_store
        self._correlation_id = correlation_id
        if memory is not None:
            from ..runtime.memory import MemoryConfig

            if not isinstance(memory, MemoryConfig):
                raise TypeError("memory must be MemoryConfig or None")
        if compaction_sink is not None and not callable(
            getattr(compaction_sink, "compact", None)
        ):
            raise TypeError("compaction_sink must implement compact() or None")
        self._memory = memory
        self._compaction_sink = compaction_sink
        self._compaction_epoch = 0

    def bind_compaction_sink(self, sink) -> None:
        """Audit F12: attach this RUN's durable recorder as the compaction sink.

        The loop is assembled once per process while DurableExecutionRecorder
        instances are created per run by TurnWorker.execute; the worker binds
        the fresh recorder before loop.run and clears it on every exit path.
        Turns are executed one at a time per assembled loop, so the slot never
        holds two live recorders.
        """
        if sink is not None and not callable(getattr(sink, "compact", None)):
            raise TypeError("compaction_sink must implement compact()")
        self._compaction_sink = sink

    def clear_compaction_sink(self) -> None:
        self._compaction_sink = None

    def context_chars(self, context: Sequence[ModelContextItem]) -> int:
        """Canonical UTF-8 byte+char budget of the request projection (D23 §5.4)."""
        chars = 0
        for item in context:
            if isinstance(item, (UserMessage, InstructionMessage)):
                # E1: trusted instructions are part of the request and must
                # be metered with everything else.
                chars += len(item.content)
            elif isinstance(item, AssistantMessage):
                # AssistantMessage wraps an AssistantTextItem (.text), while
                # scripted-test doubles may expose .content directly.
                chars += len(item.item.text)
            elif isinstance(item, ReasoningSummaryEcho):
                chars += len(item.item.summary)
            elif isinstance(item, ToolCallEcho):
                chars += len(item.item.arguments_json)
            elif isinstance(item, ToolResultMessage):
                chars += len(item.content)
        return chars

    def _maybe_compact(self, context: list[ModelContextItem]) -> None:
        """D23 §5.3/§5.4 safe-point preflight before the next model request.

        When the projected context exceeds the soft budget and every safety
        condition holds (no pending calls, durable sink present, compaction
        enabled), the oldest closed groups are replaced through the durable
        compaction sink.  If no safe group exists or the result still exceeds
        the hard budget, the run fails closed with
        ``context_capacity_exhausted`` instead of sending an oversized request.
        """
        memory = self._memory
        if memory is None or not memory.in_run_compaction_enabled:
            return
        if self.durable_tool_execution and self._pending_tool_calls():
            return  # open calls may never be compressed
        # Hardening 2026-09-19 (E1/E2): the gate meters the FULL request -
        # context items (including trusted instructions, counted above) plus
        # the pinned tool-definition schemas - and runs regardless of whether
        # a compaction sink is bound, so a sink-less path can no longer
        # silently bypass the budget.
        current = self.context_chars(context) + self._definitions_chars()
        reserve = memory.request_context_reserve_chars
        soft = memory.request_context_soft_chars
        hard = memory.request_context_hard_chars
        target = memory.compaction_target_chars
        if current + reserve <= soft:
            return
        if self._compaction_sink is None:
            # Nothing on this path is compressible: refuse the oversized
            # request instead of sending it (fail-closed, same contract).
            if current + reserve > hard:
                raise AgentLoopError("context_capacity_exhausted")
            return
        from ..execution.compaction import (
            CompactionError,
            anchors_are_preserved,
            parse_closed_groups,
            select_compressible,
        )

        map_fn = getattr(self._compaction_sink, "source_versions_for", None)
        if map_fn is None:
            raise AgentLoopError("compaction_source_versions_unavailable")
        # D23 §5.4: compact repeatedly (bounded by epoch cap) until the soft
        # budget is met; any single overrun of the hard budget fails closed.
        while self._compaction_epoch < memory.max_compaction_epochs_per_run:
            try:
                groups = parse_closed_groups(context)
                selected = select_compressible(
                    groups, keep_recent=memory.in_run_keep_groups
                )
            except CompactionError:
                raise AgentLoopError("compaction_source_invalid") from None
            if not selected:
                if self.context_chars(context) + reserve > hard:
                    raise AgentLoopError("context_capacity_exhausted")
                return
            if not anchors_are_preserved(groups, selected):
                raise AgentLoopError("compaction_anchor_violation")
            # Audit F20: instruction/user items are compaction anchors - the
            # recorder refuses any source span that contains one.  Session
            # prefixes (agents note, reminders, prior turns, older replacement
            # blocks) inject anchors BEFORE the run's own tool groups, so a
            # naive oldest-first batch would span across them and die with
            # compaction_source_range_missing on the second compaction.  Trim
            # the selection to the anchor-free tail after the LAST anchor.
            last_anchor = max(
                (
                    index
                    for index, item in enumerate(context)
                    if isinstance(item, (InstructionMessage, UserMessage))
                ),
                default=-1,
            )
            selected = [
                group
                for group in selected
                if group.first_context_index > last_anchor
            ]
            if not selected:
                if self.context_chars(context) + reserve > hard:
                    raise AgentLoopError("context_capacity_exhausted")
                return
            # Compact up to a bounded batch of the oldest closed groups.  The
            # batch never crosses an earlier replacement (parse_closed_groups
            # treats replacements as boundaries), and source_versions_for
            # clamps the range to the version-contiguous run.
            batch = selected[:8]
            first = batch[0].first_context_index
            last = batch[-1].last_context_index
            # D13-D23-001: keep bounded result semantics - error markers plus
            # a first diagnostic line per result - so a compacted failure
            # cannot become indistinguishable from a success.
            semantic_lines: list[str] = []
            for group in batch:
                for result in group.results:
                    marker = "error" if result.is_error else "ok"
                    stripped = result.content.strip()
                    head = stripped.splitlines()[0][:100] if stripped else "(empty)"
                    semantic_lines.append(f"[result {marker}] {head}")
            semantic_lines = semantic_lines[:4]
            semantic_block = "".join(
                f"[untrusted-result-fact]{line}[/untrusted-result-fact]\n"
                for line in semantic_lines
            )
            content = "\n".join(
                (
                    "[run-history-compaction epoch=%d]" % (self._compaction_epoch + 1),
                    "[untrusted-history-summary]compacted closed tool groups: %d (%s)"
                    % (
                        len(batch),
                        ",".join(
                            sorted({
                                call.item.name
                                for group in batch
                                for call in group.calls
                            })
                        ),
                    ),
                    "[/untrusted-history-summary]",
                    semantic_block,
                    "[authoritative-execution-state]tool_count=%d[/authoritative-execution-state]"
                    % self._tool_count_known(),
                    "[/run-history-compaction]",
                )
            )
            replacement = UserMessage(
                input_id=f"run:compact:{self._compaction_epoch + 1}",
                content=content,
            )
            self._compaction_epoch += 1
            first_version, last_version = map_fn(first, last)
            self._compaction_sink.compact(
                epoch=self._compaction_epoch,
                source_first_version=first_version,
                source_last_version=last_version,
                # None lets the durable recorder compute the range digest
                # from its own source context (an explicit "" is rejected).
                source_event_ids_digest=None,
                replacement=replacement,
                resulting_context_digest="",
                target_chars=target,
            )
            # The durable sink replaced its own projection by version range;
            # adopt its authoritative context so loop/recorder stay aligned.
            sync = getattr(self._compaction_sink, "synced_context", None)
            if sync is None:
                raise AgentLoopError("compaction_source_versions_unavailable")
            context[:] = list(sync())
            if self.context_chars(context) + reserve <= soft:
                return
        # Epoch cap reached while still over budget -> fail closed.
        if self.context_chars(context) + reserve > hard:
            raise AgentLoopError("context_capacity_exhausted")

    def _tool_count_known(self) -> int:
        sink = self._compaction_sink
        return int(getattr(sink, "tool_count", 0) or 0)

    def _definitions_chars(self) -> int:
        """E1/E2: pinned tool-definition schemas ride every request and must
        count against the capacity gate even though they are not context
        items."""
        return sum(
            len(definition.input_schema_json)
            for definition in (self._tool_definitions or ())
        )

    def _pending_tool_calls(self) -> bool:
        sink = self._compaction_sink
        return bool(getattr(sink, "pending_calls", ()))

    @property
    def durable_tool_execution(self) -> bool:
        # Local import avoids the Loop/LedgerExecutor module cycle while ensuring
        # a raw executor cannot opt into the durable path with a forged marker.
        from ..ledger.executor import LedgerExecutor

        return isinstance(self._tool_executor, LedgerExecutor)

    @property
    def has_tools(self) -> bool:
        return bool(self._available_tools)

    @property
    def last_snapshot(self):
        """The frozen ToolCatalogSnapshot bound to the last model round."""

        return self._last_snapshot

    def _take_snapshot(self):
        """Atomically snapshot the catalog for THIS model round (§8.8).

        An executor with a catalog_snapshot() accessor pins definitions,
        profiles, resolvers and semantic bindings for exactly the model
        request built next and every tool call that follows it.
        """
        executor = self._tool_executor
        snapshot = None
        if executor is not None and hasattr(executor, "catalog_snapshot"):
            try:
                snapshot = executor.catalog_snapshot()
            except Exception:
                snapshot = None
        return snapshot

    def _trace(self, stream: str, kind: str, fields: dict[str, object]) -> None:
        if self._trace_sink is None and self._legacy_trace_store is None:
            return
        correlation_id = self._correlation_id
        if not isinstance(correlation_id, UUID):
            from uuid import uuid4

            correlation_id = uuid4()
        if self._trace_sink is not None:
            self._trace_sink.emit(TraceProbe(correlation_id, stream, kind, fields))
        else:
            self._legacy_trace_store.append(
                correlation_id=correlation_id,
                stream=stream,
                kind=kind,
                fields=fields,
            )

    def run(
        self,
        *,
        run_id: UUID,
        turn_id: UUID | None = None,
        turn_version: int | None = None,
        input_items: Sequence[ModelContextItem],
        provider: str,
        model: str,
        max_output_tokens: int = 4096,
        cancellation: CancellationToken | None = None,
        event_sink: ModelEventSink | None = None,
        ownership_guard: OwnershipGuard | None = None,
        durable_sink: DurableExecutionSink | None = None,
        initial_model_rounds: int = 0,
        initial_tool_calls: int = 0,
        initial_output_chars: int = 0,
        resume_tool_calls: Sequence[ToolCallEcho] = (),
    ) -> AgentLoopResult:
        """运行有界模型—工具循环，直到产生合法 final 或明确失败。"""
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be UUID")
        if (turn_id is None) != (turn_version is None):
            raise ValueError("turn_id and turn_version must be provided together")
        if turn_id is not None and not isinstance(turn_id, UUID):
            raise TypeError("turn_id must be UUID or None")
        if turn_version is not None and (
            not isinstance(turn_version, int)
            or isinstance(turn_version, bool)
            or turn_version < 0
        ):
            raise ValueError("turn_version must be an integer >= 0 or None")
        if self.durable_tool_execution and self.has_tools and turn_id is None:
            raise AgentLoopError("durable_turn_identity_required")
        token = cancellation or CancellationToken()
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable or None")
        context: list[ModelContextItem] = list(input_items)
        model_turns: list[ModelTurn] = []
        self._successful_writes = frozenset()
        total_tool_calls = initial_tool_calls
        total_output_chars = initial_output_chars

        # A completed ModelTurn is already durable, but its remaining tools may not
        # have started before the crash. Execute only those exact canonical calls.
        for echo in tuple(resume_tool_calls):
            token.raise_if_cancelled()
            if ownership_guard is not None: ownership_guard()
            call = echo.item
            if call.name not in self._available_tools or self._tool_executor is None:
                raise AgentLoopError("resume_tool_unavailable")
            if total_tool_calls + 1 > self._limits.max_tool_calls:
                raise AgentLoopError("max_tool_calls_exceeded")
            execution_context = ToolExecutionContext(
                run_id=run_id, model_turn_id=echo.call_ref.model_turn_id,
                model_round=initial_model_rounds, call_ref=echo.call_ref,
                turn_id=turn_id, turn_version=turn_version,
                ancestor_turn_ids=self._ancestor_turn_ids,
                recovered_call=True,
                progress_guard=lambda: _check_tool_progress(token, ownership_guard),
            )
            authorization = None
            if self.durable_tool_execution:
                try:
                    authorization = self._tool_executor.authorize(
                        call,
                        context=execution_context,
                    )
                except Exception as exc:
                    if bool(getattr(exc, "approval_waiting", False)):
                        raise AgentLoopApprovalWaiting(
                            getattr(exc, "code", "approval_waiting")
                        ) from None
                    if bool(getattr(exc, "recovery_blocked", False)):
                        raise AgentLoopRecoveryBlocked(
                            getattr(exc, "code", "tool_recovery_blocked")
                        ) from None
                    raise AgentLoopError(
                        getattr(exc, "code", "tool_executor_failed")
                    ) from None
            if (
                durable_sink is not None
                and (
                    authorization is None
                    or authorization.early_result is None
                )
            ):
                durable_sink.tool_started(call.call_id, call.name)
            try:
                result = (
                    self._tool_executor.execute_authorized(authorization)
                    if authorization is not None
                    else self._tool_executor.execute(call, context=execution_context)
                )
            except AgentLoopCancelled: raise
            except Exception as exc:
                if bool(getattr(exc, "recovery_blocked", False)):
                    raise AgentLoopRecoveryBlocked(
                        getattr(exc, "code", "tool_recovery_blocked")
                    ) from None
                raise AgentLoopError("tool_executor_failed") from None
            if not isinstance(result, ToolExecutionResult): raise AgentLoopError("invalid_tool_executor_result")
            message = ToolResultMessage(echo.call_ref, result.content, result.is_error)
            if call.name == "apply_patch" and not message.is_error:
                self._successful_writes = self._successful_writes | frozenset({"apply_patch"})
            context.append(message); total_tool_calls += 1
            if durable_sink is not None: durable_sink.tool_completed(message, total_tool_calls)

        for model_round in range(initial_model_rounds + 1, self._limits.max_model_rounds + 1):
            token.raise_if_cancelled()
            if ownership_guard is not None:
                ownership_guard()
            # D23 §5.3/§5.4: compact at the safe point BEFORE the next request
            # is constructed; an oversized context fails closed instead of
            # being sent.
            self._maybe_compact(context)
            # I6 §8.8: ONE snapshot pins definitions for this request and
            # every tool call produced by this response.
            snapshot = self._take_snapshot()
            self._last_snapshot = snapshot
            round_definitions = (
                self._tool_definitions
                if snapshot is None
                else snapshot.definitions
            )
            model_turn_id = _model_turn_id(run_id, model_round)
            self._trace("model", "round", {"kind": "round"})
            try:
                request = ModelRequest(
                    model_turn_id=model_turn_id,
                    provider=provider,
                    model=model,
                    input_items=tuple(context),
                    tool_definitions=round_definitions,
                    max_output_tokens=max_output_tokens,
                )
            except (TypeError, ValueError) as exc:
                raise AgentLoopError("invalid_model_request") from exc
            turn = self._collect_turn(
                request,
                token,
                event_sink,
                ownership_guard,
            )
            model_turns.append(turn)
            if turn.model_turn_id != model_turn_id:
                raise AgentLoopError("model_turn_identity_mismatch")
            if turn.provider != provider:
                raise AgentLoopError("model_provider_identity_mismatch")

            turn_chars = sum(
                len(item.text)
                if isinstance(item, AssistantTextItem)
                else len(item.summary)
                if isinstance(item, PublicReasoningSummaryItem)
                else 0
                for item in turn.output_items
            )
            total_output_chars += turn_chars
            if total_output_chars > self._limits.max_total_output_chars:
                raise AgentLoopError("total_output_limit_exceeded")
            if any(isinstance(item, BlockedItem) for item in turn.output_items):
                raise AgentLoopError("blocked_model_output")

            projected, calls = _project_turn(turn)
            if durable_sink is not None:
                durable_sink.model_completed(turn, projected, model_round, total_output_chars, bool(calls))
            if turn.finish_reason is FinishReason.STOP:
                context.extend(projected)
                if not turn.final_text.strip():
                    raise AgentLoopError("empty_final_answer")
                if self._claim_gate:
                    from ..runtime.claim_gate import claim_gate_allows

                    if not claim_gate_allows(turn.final_text, self._successful_writes):
                        # D22 F1: 声称改了文件却没有成功写工具 → 防无工具幻觉完成。
                        raise AgentLoopError("claimed_change_without_tool")
                if self._completion_gate is not None:
                    try:
                        self._completion_gate.assert_complete(run_id)
                    except AgentLoopError:
                        raise
                    except Exception:
                        raise AgentLoopError("finalization_gate_failed") from None
                return AgentLoopResult(
                    final_text=turn.final_text,
                    context=tuple(context),
                    model_turns=tuple(model_turns),
                    model_rounds=model_round,
                    tool_calls=total_tool_calls,
                )

            if turn.finish_reason is not FinishReason.TOOL_CALLS:
                raise AgentLoopError(f"model_finish_{turn.finish_reason.value}")
            if model_round == self._limits.max_model_rounds:
                # 没有预算发送 ToolResult 的下一轮时，不产生本轮工具副作用。
                raise AgentLoopError("max_model_rounds_exceeded")
            if not calls:
                raise AgentLoopError("tool_finish_without_calls")
            if any(call.name not in self._available_tools for call in calls):
                # 全量验证后再执行第一个，避免后置 hallucinated call 导致部分副作用。
                raise AgentLoopError("unknown_tool_requested")
            if total_tool_calls + len(calls) > self._limits.max_tool_calls:
                raise AgentLoopError("max_tool_calls_exceeded")
            if self._tool_executor is None:
                raise AgentLoopError("tool_executor_unavailable")

            context.extend(projected)
            results: list[ToolResultMessage] = []
            for call in calls:
                token.raise_if_cancelled()
                if ownership_guard is not None:
                    ownership_guard()
                call_ref = ModelCallRef(turn.model_turn_id, call.call_id)
                execution_context = ToolExecutionContext(
                    run_id=run_id,
                    model_turn_id=turn.model_turn_id,
                    model_round=model_round,
                    call_ref=call_ref,
                    turn_id=turn_id,
                    turn_version=turn_version,
                    ancestor_turn_ids=self._ancestor_turn_ids,
                    progress_guard=lambda: _check_tool_progress(
                        token, ownership_guard
                    ),
                )
                authorization = None
                if self.durable_tool_execution:
                    try:
                        authorization = self._tool_executor.authorize(
                            call,
                            context=execution_context,
                        )
                    except Exception as exc:
                        if bool(getattr(exc, "approval_waiting", False)):
                            raise AgentLoopApprovalWaiting(
                                getattr(exc, "code", "approval_waiting")
                            ) from None
                        if bool(getattr(exc, "recovery_blocked", False)):
                            raise AgentLoopRecoveryBlocked(
                                getattr(exc, "code", "tool_recovery_blocked")
                            ) from None
                        raise AgentLoopError(
                            getattr(exc, "code", "tool_executor_failed")
                        ) from None
                if (
                    durable_sink is not None
                    and (
                        authorization is None
                        or authorization.early_result is None
                    )
                ):
                    durable_sink.tool_started(call.call_id, call.name)
                try:
                    result = (
                        self._tool_executor.execute_authorized(authorization)
                        if authorization is not None
                        else self._tool_executor.execute(
                            call,
                            context=execution_context,
                        )
                    )
                except AgentLoopCancelled:
                    raise
                except Exception as exc:
                    if bool(getattr(exc, "recovery_blocked", False)):
                        raise AgentLoopRecoveryBlocked(
                            getattr(exc, "code", "tool_recovery_blocked")
                        ) from None
                    raise AgentLoopError("tool_executor_failed") from None
                if not isinstance(result, ToolExecutionResult):
                    raise AgentLoopError("invalid_tool_executor_result")
                result_message = ToolResultMessage(
                        call_ref=call_ref,
                        content=result.content,
                        is_error=result.is_error,
                    )
                if call.name == "apply_patch" and not result_message.is_error:
                    self._successful_writes = self._successful_writes | frozenset({"apply_patch"})
                results.append(result_message)
                total_tool_calls += 1
                if durable_sink is not None:
                    durable_sink.tool_completed(result_message, total_tool_calls)
            context.extend(results)

        raise AgentLoopError("max_model_rounds_exceeded")

    def _collect_turn(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        event_sink: ModelEventSink | None,
        ownership_guard: OwnershipGuard | None,
    ) -> ModelTurn:
        """完整消费 Provider iterable；wire progress 处同时检查取消与 ownership。"""
        assembler = ModelStreamAssembler(self._stream_limits)

        def check_progress() -> None:
            cancellation.raise_if_cancelled()
            if ownership_guard is not None:
                ownership_guard()

        try:
            controlled_stream = getattr(self._client, "stream_controlled", None)
            events = (
                controlled_stream(request, progress_guard=check_progress)
                if callable(controlled_stream)
                else self._client.stream(request)
            )
            for event in events:
                check_progress()
                assembler.accept(event)
                if event_sink is not None:
                    try:
                        event_sink(event)
                    except Exception:
                        raise AgentLoopError("model_event_sink_failed") from None
            check_progress()
            return assembler.finish()
        except (AgentLoopError, AgentLoopCancelled):
            raise
        except ModelError:
            check_progress()
            raise
        except Exception:
            # 先重查 trusted control；若仍持有 Run，才把 Provider 原错归类并脱敏。
            check_progress()
            raise AgentLoopError("model_client_failed") from None


def _project_turn(
    turn: ModelTurn,
) -> tuple[list[ModelContextItem], list[ToolCallItem]]:
    """把完成 OutputItem 白名单投影为下一轮模型上下文。"""
    projected: list[ModelContextItem] = []
    calls: list[ToolCallItem] = []
    for item in turn.output_items:
        if isinstance(item, AssistantTextItem):
            projected.append(
                AssistantMessage(
                    source_provider=turn.provider,
                    model_turn_id=turn.model_turn_id,
                    item=item,
                )
            )
        elif isinstance(item, PublicReasoningSummaryItem):
            projected.append(
                ReasoningSummaryEcho(
                    source_provider=turn.provider,
                    model_turn_id=turn.model_turn_id,
                    item=item,
                )
            )
        elif isinstance(item, ToolCallItem):
            call_ref = ModelCallRef(turn.model_turn_id, item.call_id)
            projected.append(
                ToolCallEcho(
                    source_provider=turn.provider,
                    call_ref=call_ref,
                    item=item,
                )
            )
            calls.append(item)
        else:
            raise AgentLoopError("blocked_model_output")
    return projected, calls


def _model_turn_id(run_id: UUID, model_round: int) -> UUID:
    """从 Run 与轮次派生稳定 model_turn_id，便于命令级重试关联。"""
    return uuid5(
        NAMESPACE_URL,
        f"koawa-agent-v2:{run_id}:model-turn:{model_round}",
    )


def _check_tool_progress(
    cancellation: CancellationToken,
    ownership_guard: OwnershipGuard | None,
) -> None:
    cancellation.raise_if_cancelled()
    if ownership_guard is not None:
        ownership_guard()
