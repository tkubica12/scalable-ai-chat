"""Validate golden declarative artifact examples against the repo-local profile."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs" / "protocol" / "artifacts" / "examples" / "sales-artifact.json"


def main() -> int:
    artifact = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    errors: list[str] = []
    if artifact.get("kind") != "declarative-widget":
        errors.append("kind must be declarative-widget")
    if artifact.get("mimeType") != "application/vnd.scalable-ai-chat.a2ui+json":
        errors.append("unexpected mimeType")
    components = artifact.get("surface", {}).get("components", [])
    if not components:
        errors.append("surface.components must not be empty")
    allowed = {"TextBlock", "Card", "Table", "Chart", "Form", "StatusTimeline"}
    for component in components:
        if component.get("type") not in allowed:
            errors.append(f"unsupported component: {component.get('type')}")
    if errors:
        for error in errors:
            print(error)
        return 1
    print("Validated 1 artifact example.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
