"""Validate golden agent UI event examples against the repo-local schema profile."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "protocol" / "agent-ui-events.schema.json"
EXAMPLES_DIR = ROOT / "docs" / "protocol" / "examples"


def _validate_event(event: dict, allowed_types: set[str]) -> list[str]:
    errors: list[str] = []
    required = {"type", "runId", "threadId", "sequence", "timestamp"}
    missing = required - event.keys()
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")

    if event.get("type") not in allowed_types:
        errors.append(f"unsupported type: {event.get('type')!r}")
    if not isinstance(event.get("runId"), str) or not re.match(r"^run_[A-Za-z0-9_-]+$", event.get("runId", "")):
        errors.append("runId must start with run_ and contain only URL-safe characters")
    if not isinstance(event.get("threadId"), str) or not event.get("threadId"):
        errors.append("threadId must be a non-empty string")
    if not isinstance(event.get("sequence"), int) or event.get("sequence", 0) < 1:
        errors.append("sequence must be an integer >= 1")

    try:
        datetime.fromisoformat(str(event.get("timestamp")).replace("Z", "+00:00"))
    except ValueError:
        errors.append("timestamp must be an ISO-8601 date-time")

    if event.get("type") == "TextMessageContent":
        if not isinstance(event.get("messageId"), str) or not event.get("messageId"):
            errors.append("TextMessageContent requires messageId")
        if not isinstance(event.get("delta"), str):
            errors.append("TextMessageContent requires string delta")
    if event.get("type") == "Usage" and not isinstance(event.get("usage"), dict):
        errors.append("Usage requires usage object")
    if event.get("type") == "RunError":
        error = event.get("error")
        if not isinstance(error, dict) or not error.get("message"):
            errors.append("RunError requires error.message")

    return errors


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    allowed_types = set(schema["properties"]["type"]["enum"])
    failures: list[str] = []

    for example_path in sorted(EXAMPLES_DIR.glob("*.json")):
        event = json.loads(example_path.read_text(encoding="utf-8"))
        errors = _validate_event(event, allowed_types)
        if errors:
            failures.append(f"{example_path.relative_to(ROOT)}: {'; '.join(errors)}")

    if failures:
        for failure in failures:
            print(failure)
        return 1

    print(f"Validated {len(list(EXAMPLES_DIR.glob('*.json')))} protocol examples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
