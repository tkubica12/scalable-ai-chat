# Contracts

This file holds protocol, API, event, artifact, MCP, and data-shape details. Keep [Architecture](./Architecture.md) focused on system design.

## Run API

### Create a run

`POST /api/runs`

```json
{
  "threadId": "thread_001",
  "userId": "user_001",
  "input": {
    "messages": [
      { "role": "user", "content": "Show a table and chart of example sales data." }
    ],
    "attachments": []
  },
  "capabilities": {
    "text": true,
    "toolEvents": true,
    "declarativeArtifacts": true,
    "sandboxedApps": true
  }
}
```

Response:

```json
{
  "runId": "run_...",
  "threadId": "thread_001",
  "status": "queued",
  "eventsUrl": "/api/runs/run_.../events"
}
```

### Read a run

`GET /api/runs/{runId}`

Returns run metadata: status, timestamps, last sequence, usage, safety, and artifact IDs.

### Cancel a run

`POST /api/runs/{runId}/cancel`

Cancellation is explicit. The worker emits `RunCancelled` when it observes the cancellation flag.

### Stream events

`GET /api/runs/{runId}/events`

SSE frame:

```text
id: 42
event: TextMessageContent
data: {"type":"TextMessageContent","runId":"run_...","threadId":"thread_001","messageId":"msg_...","delta":"hello","sequence":42,"timestamp":"2026-07-04T08:00:00Z"}
```

Rules:

- `id` equals `sequence`.
- Reconnect with `Last-Event-ID`.
- Events are JSON, not manually escaped strings.
- Terminal event is one of `RunFinished`, `RunError`, or `RunCancelled`.

## Event types

| Group | Events |
| --- | --- |
| Lifecycle | `RunStarted`, `RunFinished`, `RunError`, `RunCancelled` |
| Text | `TextMessageStart`, `TextMessageContent`, `TextMessageEnd` |
| Tools | `ToolCallStart`, `ToolCallArgs`, `ToolCallEnd`, `ToolCallResult` |
| State | `StateSnapshot`, `StateDelta` |
| Artifacts | `ArtifactCreated`, `ArtifactDelta`, `ArtifactFinalized` |
| Control | `Heartbeat`, `Usage`, `SafetyVerdict` |

## Artifact API

`GET /api/artifacts/{artifactId}`

Returns:

```json
{
  "manifest": {
    "artifactId": "artifact_...",
    "runId": "run_...",
    "kind": "declarative-widget",
    "mimeType": "application/vnd.scalable-ai-chat.a2ui+json",
    "contentHash": "sha256-...",
    "blobPath": "artifacts/user_001/run_.../artifact_.../v1/widget.json",
    "artifactUrl": "/api/artifacts/artifact_..."
  },
  "artifact": {}
}
```

Artifacts are stored in a private Blob container. The Front Service reads them with managed identity.

## Artifact profiles

### Declarative widget

Approved components:

- `TextBlock`
- `Card`
- `Table`
- `Chart`
- `Form`
- `StatusTimeline`

The current demo generates a table and bar chart for a sales prompt.

### Sandboxed app

A sandboxed app artifact contains static HTML and CSP metadata. The browser renders it in an iframe with restrictive sandbox attributes.

## MCP endpoints

Memory API and History API expose `/mcp` JSON-RPC endpoints.

Supported common methods:

- `initialize`
- `tools/list`
- `tools/call`
- `resources/list`
- `resources/read`

Memory tools:

- `get_user_memory(userId)`
- `search_conversation_history(userId, query, limit)`

History tools:

- `list_conversations(userId, limit)`
- `get_conversation(userId, sessionId)`

## Stored data shapes

### Run

```json
{
  "id": "run_...",
  "runId": "run_...",
  "threadId": "thread_...",
  "userId": "user_001",
  "status": "completed",
  "createdAt": "2026-07-04T08:00:00Z",
  "startedAt": "2026-07-04T08:00:01Z",
  "completedAt": "2026-07-04T08:00:12Z",
  "lastSequence": 84,
  "usage": {
    "inputTokens": 123,
    "outputTokens": 456,
    "totalTokens": 579
  },
  "artifacts": ["artifact_..."],
  "safety": {
    "inputVerdict": "allowed"
  }
}
```

### Artifact manifest

```json
{
  "id": "artifact_...",
  "artifactId": "artifact_...",
  "runId": "run_...",
  "threadId": "thread_...",
  "userId": "user_001",
  "kind": "declarative-widget",
  "mimeType": "application/vnd.scalable-ai-chat.a2ui+json",
  "version": "v1",
  "contentHash": "sha256-...",
  "blobPath": "artifacts/user_001/run_.../artifact_.../v1/widget.json",
  "createdAt": "2026-07-04T08:00:00Z",
  "csp": {
    "connectDomains": [],
    "resourceDomains": [],
    "frameDomains": []
  },
  "provenance": {
    "model": "gpt-5.4-mini",
    "tool": "generate_sales_artifact"
  }
}
```
