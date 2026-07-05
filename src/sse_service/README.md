# SSE Service

This service handles Server-Sent Events (SSE) streaming for the scalable chat application. The primary path streams typed run events from Redis Streams; the legacy path can still receive token messages from Service Bus sessions during migration.

## Features

- **Run event streaming**: Streams `id:`, `event:`, and JSON `data:` SSE frames from `run:{runId}:events`
- **Replay support**: Uses `Last-Event-ID` to replay events after a reconnect
- **Session-aware compatibility streaming**: Uses Service Bus sessions for old `/api/stream/{sessionId}/{chatMessageId}` clients
- **Message filtering**: Filters legacy tokens by `chatMessageId` to ensure proper routing
- **Error handling**: Graceful error handling with client notification
- **Health checks**: Built-in health check endpoint
- **CORS support**: Configurable CORS for cross-origin requests

## Installation

```bash
# Navigate to the SSE service directory
cd src/sse_service

# Install dependencies using uv
uv sync
```

## Configuration

Copy `.env.example` to `.env` and configure the following variables:

- `SERVICEBUS_FULLY_QUALIFIED_NAMESPACE`: Your Azure Service Bus namespace
- `SERVICEBUS_TOKEN_STREAMS_TOPIC`: The topic name for token streams
- `SERVICEBUS_TOKEN_STREAMS_SUBSCRIPTION`: The subscription name for this service
- `REDIS_HOST`, `REDIS_PORT`, `REDIS_SSL`: Redis Stream connection settings
- `CORS_ORIGINS`: Allowed CORS origins (comma-separated)
- `LOG_LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR)

## Running

```bash
# Using uv
uv run python main.py

# Or with uvicorn directly
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

## API Endpoints

### GET /api/runs/{runId}/events

Establishes an SSE connection to stream typed run events. Reconnect with `Last-Event-ID: <sequence>` to resume after the last received event.

Each event frame uses:

```text
id: 42
event: TextMessageContent
data: {"type":"TextMessageContent","runId":"run_...","threadId":"...","sequence":42,"timestamp":"...","delta":"hello"}
```

### GET /api/stream/{sessionId}/{chatMessageId}

Compatibility endpoint. If a Redis run mapping exists, it attaches to the new run event stream. Otherwise, it falls back to legacy Service Bus token streaming.

**Parameters:**
- `sessionId`: The session identifier
- `chatMessageId`: The specific chat message identifier

**Response:** Server-Sent Events stream with the following data formats:
- Token: `data: {"token": "text"}`
- End of stream: `data: __END__`
- Error: `data: {"error": "message"}`

### GET /health

Health check endpoint that returns service status.

**Response:**
```json
{
  "status": "healthy",
  "service": "sse-service"
}
```

## Architecture

The SSE service operates independently from the front service, allowing for better scalability:

1. **Receives requests**: Clients connect to `/api/stream/{sessionId}/{chatMessageId}`
2. **Opens session receiver**: Creates a session-aware Service Bus receiver for the given session
3. **Filters messages**: Only processes tokens matching the requested `chatMessageId`
4. **Streams tokens**: Sends tokens to the client via Server-Sent Events
5. **Handles completion**: Sends end-of-stream signal when the response is complete

## Scaling

The SSE service can be scaled independently based on streaming demand:

- Scale horizontally by running multiple instances
- Each instance handles its own set of SSE connections
- No shared state between instances (stateless design)
- Load balancing across instances for optimal resource utilization

## Error Handling

- Connection errors are handled gracefully
- Malformed messages are logged and skipped
- Client disconnections are detected and resources cleaned up
- Service Bus errors result in appropriate error messages to clients
