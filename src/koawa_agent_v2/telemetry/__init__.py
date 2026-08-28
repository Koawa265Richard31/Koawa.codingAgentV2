"""Lazy telemetry exports keep fault contracts independent of runtime imports."""

from importlib import import_module

_TRACE_EXPORTS = frozenset({
    "BestEffortTraceSink", "TraceDiagnostics", "TraceProbe", "TraceRecord", "TraceSink", "TraceStore",
})


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(name)
    module = import_module(".trace" if name in _TRACE_EXPORTS else ".faults", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value

__all__ = [
    "FAILURE_POINTS",
    "FaultInjector",
    "FAULT_REGISTRY",
    "FAULT_SPECS",
    "FaultPointClass",
    "FaultPoint",
    "FaultPort",
    "FaultSpec",
    "InjectedFault",
    "NO_OP_FAULT_PORT",
    "NoOpFaultPort",
    "RecordingFaultPort",
    "BestEffortTraceSink",
    "TraceDiagnostics",
    "TraceProbe",
    "TraceRecord",
    "TraceSink",
    "TraceStore",
    "classify_failure",
    "using_fault_port",
]
