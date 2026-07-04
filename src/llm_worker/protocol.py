"""Agent UI event helpers for the LLM worker."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_event(
    event_type: str,
    run_id: str,
    thread_id: str,
    sequence: int,
    **payload: Any,
) -> dict[str, Any]:
    """Build a typed agent UI event envelope."""

    return {
        "type": event_type,
        "runId": run_id,
        "threadId": thread_id,
        "sequence": sequence,
        "timestamp": utc_now(),
        **payload,
    }
