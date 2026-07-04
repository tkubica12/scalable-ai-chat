"""Agent UI event helpers for the stream service."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sse_frame(event: dict[str, Any]) -> str:
    """Serialize an agent event as a standards-compliant SSE frame."""

    sequence = event["sequence"]
    event_type = event["type"]
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n"
