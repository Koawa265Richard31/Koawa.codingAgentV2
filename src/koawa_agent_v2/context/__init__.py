"""D13 repository context, budget, retrieval, and compaction."""

from .budget import ContextBudget, fit_within_budget
from .index import IndexLimits, IndexedFile, RepositoryIndex
from .retrieval import ContextItem, ContextRetriever
from .compaction import (
    AuthoritativeProjection,
    Compactor,
    ToolCallPair,
    rebuild_after_restart,
)

__all__ = [
    "AuthoritativeProjection",
    "Compactor",
    "ContextBudget",
    "ContextItem",
    "ContextRetriever",
    "IndexLimits",
    "IndexedFile",
    "RepositoryIndex",
    "ToolCallPair",
    "fit_within_budget",
    "rebuild_after_restart",
]
