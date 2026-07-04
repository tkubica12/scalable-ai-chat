# Agent UI protocol profile

The original browser stream used one-off SSE frames such as `{"token":"..."}` and an `__END__` sentinel. That shape is easy to demo, but it does not provide durable runs, event IDs, replay after reconnect, typed tool/artifact events, cancellation, or a clear path to AG-UI/CopilotKit style clients.

This repository now exposes a repo-local AG-UI-shaped profile:

- `POST /api/runs` creates a durable run and queues work.
- `GET /api/runs/{runId}` returns run metadata and status.
- `POST /api/runs/{runId}/cancel` records explicit cancellation intent.
- `GET /api/runs/{runId}/events` streams standards-compliant SSE with `id:`, `event:`, and JSON `data:` fields.

Every event has `type`, `runId`, `threadId`, `sequence`, and `timestamp`. `sequence` is monotonic per run and is also used as the SSE `id`, so clients can reconnect with `Last-Event-ID` and receive only later events from the Redis Stream hot log.

The legacy endpoints remain available during migration:

- `POST /api/session/start`
- `POST /api/chat`
- `GET /api/stream/{sessionId}/{chatMessageId}`

The legacy chat endpoint creates a run internally and returns `runId`/`eventsUrl` in addition to the old response fields.
