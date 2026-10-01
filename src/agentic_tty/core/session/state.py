"""会话状态机与形态。"""

from __future__ import annotations

from enum import StrEnum

from ..errors import SessionStateError


class SessionState(StrEnum):
    """会话生命周期状态。"""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    CLOSED = "closed"


_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CREATED: frozenset({SessionState.STARTING, SessionState.CLOSED}),
    SessionState.STARTING: frozenset(
        {SessionState.RUNNING, SessionState.EXITED, SessionState.CLOSED}
    ),
    SessionState.RUNNING: frozenset({SessionState.EXITED, SessionState.CLOSED}),
    SessionState.EXITED: frozenset({SessionState.CLOSED}),
    SessionState.CLOSED: frozenset(),
}


def check_transition(src: SessionState, dst: SessionState) -> None:
    """非法迁移直接报错，避免状态被静默改坏。"""
    if dst not in _TRANSITIONS[src]:
        raise SessionStateError(f"非法状态迁移: {src} → {dst}")
