"""会话侧共享数据类型：SessionTurn。

会话历史（runtime.session 的投影/压缩/journal）与召回（runtime.session_recall）
两侧共用的最小数据类型，单独下沉以保持单向依赖——session_recall 只导入本模块，
不反向导入 runtime.session。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SessionTurn:
    """One completed conversational turn, projected under the whitelist."""

    user_input: str
    final_text: str | None = None
    turn_id: UUID | None = None
    status: str | None = None
    error: str | None = None
    changed_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.user_input, str) or not self.user_input.strip():
            raise ValueError("user_input must be non-empty text")
        if self.final_text is not None and (
            not isinstance(self.final_text, str) or not self.final_text.strip()
        ):
            raise ValueError("final_text must be non-empty text or None")
        if self.turn_id is not None and not isinstance(self.turn_id, UUID):
            raise TypeError("turn_id must be UUID or None")
        if self.status is not None and (
            not isinstance(self.status, str) or not self.status.strip()
        ):
            raise ValueError("status must be non-empty text or None")
        if self.error is not None and (
            not isinstance(self.error, str) or not self.error.strip()
        ):
            raise ValueError("error must be non-empty text or None")
        if not isinstance(self.changed_files, tuple) or any(
            not isinstance(path, str) or not path.strip()
            for path in self.changed_files
        ):
            raise ValueError("changed_files must be a tuple of non-empty paths")
