"""Task-scoped conversation state for bounded clarification workflows."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

TurnRole = Literal["user", "assistant", "system"]
TurnKind = Literal[
    "instruction",
    "clarification_question",
    "clarification_answer",
    "plan_created",
    "plan_confirmation",
    "status",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationTurn(BaseModel):
    turn_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    role: TurnRole
    kind: TurnKind
    content: str
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class TaskSession(BaseModel):
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    job_id: str = Field(min_length=32, max_length=32)
    status: str = "pending"
    original_instruction: str = ""
    current_task_spec_version: int = Field(default=0, ge=0)
    current_plan_version: int = Field(default=0, ge=0)
    current_plan_hash: str = ""
    clarification_round: int = Field(default=0, ge=0)
    max_clarification_rounds: int = Field(default=3, ge=1, le=10)
    task_spec: dict[str, Any] = Field(default_factory=dict)
    turns: list[ConversationTurn] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
