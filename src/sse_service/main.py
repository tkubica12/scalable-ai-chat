import os
import json
import logging
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from contextlib import asynccontextmanager
import uvicorn
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
import redis.asyncio as redis
from redis_entraid.cred_provider import create_from_default_azure_credential
from protocol import sse_frame, utc_now

# Load local .env when in development
load_dotenv()

# Read configuration from environment
SERVICEBUS_FULLY_QUALIFIED_NAMESPACE = os.getenv("SERVICEBUS_FULLY_QUALIFIED_NAMESPACE")
SERVICEBUS_TOKEN_STREAMS_TOPIC = os.getenv("SERVICEBUS_TOKEN_STREAMS_TOPIC")
SERVICEBUS_TOKEN_STREAMS_SUBSCRIPTION = os.getenv("SERVICEBUS_TOKEN_STREAMS_SUBSCRIPTION")
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))
REDIS_SSL = os.getenv("REDIS_SSL", "true").lower() == "true"

if not REDIS_HOST:
    raise RuntimeError("Missing Redis configuration in environment variables")

# Logging configuration
LOG_LEVEL = os.getenv('LOG_LEVEL', 'WARNING').upper()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Azure Monitor configuration
configure_azure_monitor(
    enable_live_metrics=True,
    instrumentation_options={
        "azure_sdk": {"enabled": True},
        "django": {"enabled": False},
        "fastapi": {"enabled": True},
        "flask": {"enabled": False},
        "psycopg2": {"enabled": False},
        "requests": {"enabled": False},
        "urllib": {"enabled": False},
        "urllib3": {"enabled": False},
    }
)
tracer = trace.get_tracer(__name__)

# Initialize Azure credentials
credential = DefaultAzureCredential()

# Global ServiceBusClient
sb_client: ServiceBusClient | None = None
redis_client: redis.Redis | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global sb_client, redis_client
    
    local_sb_client = None
    local_redis_client = None
    
    try:
        if SERVICEBUS_FULLY_QUALIFIED_NAMESPACE and SERVICEBUS_TOKEN_STREAMS_TOPIC and SERVICEBUS_TOKEN_STREAMS_SUBSCRIPTION:
            logger.info("SSE Service startup: Initializing Service Bus client.")
            local_sb_client = ServiceBusClient(
                fully_qualified_namespace=SERVICEBUS_FULLY_QUALIFIED_NAMESPACE,
                credential=credential
            )
            sb_client = local_sb_client
        else:
            logger.warning("Legacy Service Bus stream configuration is incomplete; old stream endpoint will be unavailable.")

        logger.info("SSE Service startup: Initializing Redis client.")
        redis_credential_provider = create_from_default_azure_credential(
            ("https://redis.azure.com/.default",)
        )
        local_redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            ssl=REDIS_SSL,
            ssl_cert_reqs=None,
            credential_provider=redis_credential_provider,
        )
        await local_redis_client.ping()
        redis_client = local_redis_client
        
        yield  # Application runs

    finally:
        # Close client
        logger.info("SSE Service shutdown: Closing Service Bus client.")
        if local_sb_client:
            await local_sb_client.close()
        if local_redis_client:
            await local_redis_client.aclose()
        # Close credentials
        await credential.close()
        # Reset globals
        sb_client = None
        redis_client = None

app = FastAPI(
    title="Scalable Chat SSE Service",
    version="0.1.0",
    description="Server-Sent Events streaming service for scalable chat",
    lifespan=lifespan,
)

# Configure CORS
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
allowed_origins = [origin.strip() for origin in CORS_ORIGINS.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

async def token_stream_generator(sessionId: str, chatMessageId: str):
    """
    Generator that streams tokens for a specific chat message from Service Bus.
    Uses session-aware receiver to get tokens for the specified session.
    """
    logger.info(f"Opening sessionful receiver for token streams for session: {sessionId}, message: {chatMessageId}")
    
    try:
        async with sb_client.get_subscription_receiver(
            SERVICEBUS_TOKEN_STREAMS_TOPIC,
            SERVICEBUS_TOKEN_STREAMS_SUBSCRIPTION,
            session_id=sessionId
        ) as receiver:
            logger.info(f"Token stream receiver opened for session: {sessionId}")
            
            async for sb_msg in receiver:
                logger.debug("Received chunk: %s", sb_msg)
                try:
                    data = json.loads(str(sb_msg))
                    
                    # Only process tokens for the matching chatMessageId
                    if data.get("chatMessageId") != chatMessageId:
                        await receiver.complete_message(sb_msg)
                        continue
                    
                    # End-of-stream signal
                    if data.get("end_of_stream"):
                        yield "data: __END__\n\n"
                        await receiver.complete_message(sb_msg)
                        break
                    
                    # Token data event
                    token = data.get("token")
                    if token is not None:
                        yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
                        await receiver.complete_message(sb_msg)
                        
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode JSON from token stream message: {str(sb_msg)}, error: {e}")
                    await receiver.complete_message(sb_msg)
                except Exception as e:
                    logger.error(f"Error processing token stream message: {e}")
                    await receiver.abandon_message(sb_msg)
                    
    except Exception as e:
        logger.error(f"Error in token stream generator: {e}")
        yield f'data: {{"error": "Stream connection lost"}}\n\n'
    
    logger.info(f"SSE stream generator finished for session: {sessionId}, message: {chatMessageId}")


def _run_events_key(run_id: str) -> str:
    return f"run:{run_id}:events"


def _run_map_key(session_id: str, chat_message_id: str) -> str:
    return f"run-map:{session_id}:{chat_message_id}"


def _decode_stream_event(fields: dict) -> dict:
    data = fields.get(b"data") or fields.get("data")
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    if not isinstance(data, str):
        raise ValueError("Redis stream event is missing JSON data field")
    return json.loads(data)


async def _run_exists(run_id: str) -> bool:
    return bool(redis_client and await redis_client.exists(f"run:{run_id}"))


async def run_event_stream_generator(run_id: str, after_sequence: int, request: Request):
    """
    Stream typed agent events from the Redis Stream hot log.
    """
    if not redis_client:
        yield sse_frame({
            "type": "RunError",
            "runId": run_id,
            "threadId": "unknown",
            "sequence": max(after_sequence + 1, 1),
            "timestamp": utc_now(),
            "error": {"message": "Redis client not initialized"},
        })
        return

    stream_key = _run_events_key(run_id)
    last_stream_id = "0-0"
    sent_any = False

    try:
        existing = await redis_client.xrange(stream_key, min="-", max="+")
        terminal_seen = False
        for stream_id, fields in existing:
            event = _decode_stream_event(fields)
            last_stream_id = stream_id.decode("utf-8") if isinstance(stream_id, bytes) else stream_id
            if event.get("type") in {"RunFinished", "RunError", "RunCancelled"}:
                terminal_seen = True
            if int(event.get("sequence", 0)) > after_sequence:
                sent_any = True
                yield sse_frame(event)
                if event.get("type") in {"RunFinished", "RunError", "RunCancelled"}:
                    return

        if terminal_seen:
            return

        if not existing and not await _run_exists(run_id):
            yield sse_frame({
                "type": "RunError",
                "runId": run_id,
                "threadId": "unknown",
                "sequence": max(after_sequence + 1, 1),
                "timestamp": utc_now(),
                "error": {"message": "Run not found or event stream expired"},
            })
            return

        while not await request.is_disconnected():
            response = await redis_client.xread({stream_key: last_stream_id}, count=10, block=15000)
            if not response:
                yield f": heartbeat {utc_now()}\n\n"
                continue

            for _, messages in response:
                for stream_id, fields in messages:
                    event = _decode_stream_event(fields)
                    last_stream_id = stream_id.decode("utf-8") if isinstance(stream_id, bytes) else stream_id
                    sequence = int(event.get("sequence", 0))
                    if sequence <= after_sequence:
                        continue
                    after_sequence = sequence
                    sent_any = True
                    yield sse_frame(event)
                    if event.get("type") in {"RunFinished", "RunError", "RunCancelled"}:
                        return

        if not sent_any:
            logger.info("Run event stream disconnected before sending events for run %s", run_id)
    except Exception as e:
        logger.error("Error in run event stream for %s: %s", run_id, e)
        yield sse_frame({
            "type": "RunError",
            "runId": run_id,
            "threadId": "unknown",
            "sequence": max(after_sequence + 1, 1),
            "timestamp": utc_now(),
            "error": {"message": "Stream connection lost"},
        })


@app.get("/api/runs/{runId}/events")
async def stream_run_events(runId: str, request: Request):
    """
    Stream AG-UI-shaped run events with SSE event IDs and replay support.
    """
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis client not initialized.")

    try:
        after_sequence = int(request.headers.get("last-event-id", "0") or "0")
    except ValueError:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer sequence.")

    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("app.run_id", runId)
        current_span.set_attribute("app.operation", "stream_run_events")
        current_span.set_attribute("app.last_event_id", after_sequence)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
    }
    return StreamingResponse(
        run_event_stream_generator(runId, after_sequence, request),
        media_type="text/event-stream",
        headers=headers,
    )

@app.get("/api/stream/{sessionId}/{chatMessageId}")
async def stream_tokens(sessionId: str, chatMessageId: str, request: Request):
    """
    Endpoint to stream tokens for a specific chat message via Server-Sent Events.
    
    Args:
        sessionId: The session identifier
        chatMessageId: The specific chat message identifier
        request: FastAPI request object
    
    Returns:
        StreamingResponse: SSE stream of tokens
        
    Raises:
        HTTPException: If Service Bus client not initialized
    """
    global sb_client
    if redis_client:
        mapped_run_id = await redis_client.get(_run_map_key(sessionId, chatMessageId))
        if mapped_run_id:
            run_id = mapped_run_id.decode("utf-8") if isinstance(mapped_run_id, bytes) else mapped_run_id
            return await stream_run_events(run_id, request)

    if not sb_client:
        raise HTTPException(status_code=503, detail="Legacy Service Bus stream is not initialized and no run mapping was found.")

    # Add custom dimensions to current span for observability
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("app.session_id", sessionId)
        current_span.set_attribute("app.chat_message_id", chatMessageId)
        current_span.set_attribute("app.operation", "stream_tokens")

    logger.info(f"Starting SSE stream for session: {sessionId}, chatMessageId: {chatMessageId}")

    # Set up SSE headers
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
    }

    return StreamingResponse(
        token_stream_generator(sessionId, chatMessageId), 
        media_type="text/event-stream",
        headers=headers
    )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "sse-service"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
