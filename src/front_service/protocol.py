"""Run and event models for the front service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


RUN_STATUSES = ("queued", "running", "completed", "failed", "cancelled", "cancelling")


class ChatInputMessage(BaseModel):
    """A user or assistant message accepted by the run API."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class RunInput(BaseModel):
    """Input payload for a run."""

    messages: list[ChatInputMessage]
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class RunCapabilities(BaseModel):
    """Client capabilities for an agent run."""

    text: bool = True
    toolEvents: bool = True
    declarativeArtifacts: bool = False
    sandboxedApps: bool = False


class CreateRunRequest(BaseModel):
    """Request body for POST /api/runs."""

    threadId: str | None = None
    userId: str
    input: RunInput
    capabilities: RunCapabilities = Field(default_factory=RunCapabilities)


class RunResponse(BaseModel):
    """Response returned after a run is created."""

    runId: str
    threadId: str
    status: str
    eventsUrl: str


class RunMetadata(BaseModel):
    """Durable run metadata stored in Redis."""

    id: str
    runId: str
    threadId: str
    sessionId: str | None = None
    chatMessageId: str | None = None
    userId: str
    status: str
    createdAt: str
    startedAt: str | None = None
    completedAt: str | None = None
    lastSequence: int = 0
    inputSummary: str | None = None
    outputSummary: str | None = None
    usage: dict[str, int] | None = None
    artifacts: list[str] = Field(default_factory=list)
    capabilities: RunCapabilities = Field(default_factory=RunCapabilities)


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
