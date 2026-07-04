# Scalable Chat Front Service

This service implements the **Front Service** component of the scalable chat architecture.

Key responsibilities:
- Exposes an HTTP endpoint (`POST /api/session/start`) to initiate a chat session and provide a `sessionId`.
- Exposes `POST /api/runs`, `GET /api/runs/{runId}`, and `POST /api/runs/{runId}/cancel` for durable agent runs.
- Keeps `POST /api/chat` as a compatibility wrapper that creates a run internally.
- Stores run metadata and cancellation intent in Redis.
- Enqueues incoming runs onto the Azure Service Bus `user-messages` topic.

Configuration:
- Uses `DefaultAzureCredentials` from `azure-identity` for authentication in Azure.
- Local development: configuration loaded from `.env` via `python-dotenv`.
- Environment variables:
  - `SERVICEBUS_FULLY_QUALIFIED_NAMESPACE` (e.g., `mysb.servicebus.windows.net`)
  - `SERVICEBUS_USER_MESSAGES_TOPIC` (topic to send user questions to workers)
  - `REDIS_HOST`, `REDIS_PORT`, `REDIS_SSL` (run metadata and cancellation state)
  - `RUN_TTL_SECONDS` (run metadata and mapping TTL; default 86400)
  - `LOG_LEVEL` (e.g., `INFO`, `DEBUG`, `WARNING`)

Endpoints:
- `POST /api/session/start`:
  - Request: (No body needed)
  - Response: `{"sessionId": "<generated-uuid>"}`
- `POST /api/runs`:
  - Request Body: `{"threadId":"<thread-id>","userId":"user_001","input":{"messages":[{"role":"user","content":"Hello"}],"attachments":[]},"capabilities":{"text":true,"toolEvents":true}}`
  - Response: `{"runId":"run_...","threadId":"<thread-id>","status":"queued","eventsUrl":"/api/runs/run_.../events"}`
- `GET /api/runs/{runId}`:
  - Response: current run metadata from Redis.
- `POST /api/runs/{runId}/cancel`:
  - Response: cancellation status.
- `POST /api/chat`:
  - Compatibility wrapper for old clients. Returns the old fields plus `runId` and `eventsUrl`.

The OpenAPI spec is available at `/openapi.json` and UI docs at `/docs` when running the FastAPI application.

Architecture:
```mermaid
flowchart LR
  Client -->|POST /api/session/start| Front(Front Service)
  Front -->|sessionId| Client

  Client -->|POST /api/chat (message, sessionId, messageId)| Front
  Front -->|enqueue (text, sessionId, messageId)| SBUserMessages(Service Bus: user-messages Topic)
  Front -->|SSE Stream| Client

  WorkerService -->|publish (token/EOS, sessionId, messageId)| SBTokenStreams(Service Bus: token-streams Topic)
  SBTokenStreams -->|receive| Front
  Front -->|SSE data (token) or __END__| Client
```

## Message Flow for /api/chat:

1.  **Client to Front Service**: Client sends `POST /api/chat` with `message`, `sessionId`, and `messageId`.
2.  **Front Service**: 
    a.  Validates input.
    b.  Creates an `asyncio.Queue` for the `sessionId` if one doesn't exist (used to buffer tokens for the SSE stream).
    c.  Constructs a `ServiceBusMessage` containing the `text`, `sessionId`, and `messageId`. The `sessionId` is also set as the `session_id` property of the Service Bus message for potential session-aware processing if the `user-messages` topic/subscription is configured for sessions.
    d.  Sends this message to the `SERVICEBUS_USER_MESSAGES_TOPIC`.
    e.  Returns an SSE `StreamingResponse` to the client, which will pull tokens from the session-specific `asyncio.Queue`.
3.  **Front Service (Background Listener)**:
    a.  Continuously listens to its subscription on `SERVICEBUS_TOKEN_STREAMS_TOPIC`.
    b.  When a message arrives (containing `token` or `end_of_stream`, along with `sessionId` and `messageId` from the worker):
        i.  It looks up the `asyncio.Queue` for the `sessionId`.
        ii. Puts the `{"token": "...", "messageId": "..."}` or `{"end_of_stream": True, "messageId": "..."}` dictionary into that queue.
4.  **Front Service (SSE Generator for Client)**:
    a.  The `token_stream_generator` for the specific `/api/chat` request awaits items from its `sessionId`'s `asyncio.Queue`.
    b.  When a `{"token": "..."}` item appears, it sends `data: {"token": "..."}\n\n` to the client.
    c.  When an `{"end_of_stream": True, "messageId": "..."}` item appears that matches the `initial_messageId` of the stream, it sends `data: __END__\n\n` to the client and closes that specific message stream.
