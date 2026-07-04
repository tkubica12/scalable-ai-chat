import os
import json
import logging
import asyncio
import uuid
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient, ServiceBusSender
from azure.servicebus import ServiceBusMessage
from contextlib import asynccontextmanager
import uvicorn
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
import redis.asyncio as redis
from redis_entraid.cred_provider import create_from_default_azure_credential
from protocol import CreateRunRequest, RunCapabilities, RunInput, RunMetadata, RunResponse, utc_now

# Load local .env when in development
load_dotenv()

# Read configuration from environment
SERVICEBUS_FULLY_QUALIFIED_NAMESPACE = os.getenv("SERVICEBUS_FULLY_QUALIFIED_NAMESPACE")
SERVICEBUS_USER_MESSAGES_TOPIC = os.getenv("SERVICEBUS_USER_MESSAGES_TOPIC")
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))
REDIS_SSL = os.getenv("REDIS_SSL", "true").lower() == "true"
RUN_TTL_SECONDS = int(os.getenv("RUN_TTL_SECONDS", str(24 * 60 * 60)))

if not SERVICEBUS_FULLY_QUALIFIED_NAMESPACE or not SERVICEBUS_USER_MESSAGES_TOPIC or not REDIS_HOST:
    raise RuntimeError("Missing Service Bus configuration in environment variables")

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
SERVICEBUS_SENDER_POOL_SIZE = int(os.getenv("SERVICEBUS_SENDER_POOL_SIZE", "10"))
sender_pool: asyncio.Queue | None = None
redis_client: redis.Redis | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global sb_client, sender_pool, redis_client

    local_sb_client = None
    local_redis_client = None
    
    try:
        logger.info("Application startup: Initializing Service Bus client.")
        local_sb_client = ServiceBusClient(
            fully_qualified_namespace=SERVICEBUS_FULLY_QUALIFIED_NAMESPACE,
            credential=credential
        )
        # Assign to global client
        sb_client = local_sb_client
        # Initialize sender pool
        logger.info(f"Application startup: Creating {SERVICEBUS_SENDER_POOL_SIZE}-size Service Bus sender pool.")
        local_pool: asyncio.Queue = asyncio.Queue()
        for _ in range(SERVICEBUS_SENDER_POOL_SIZE):
            sender = local_sb_client.get_topic_sender(SERVICEBUS_USER_MESSAGES_TOPIC)
            # Open AMQP link once per sender
            await sender.__aenter__()
            lock = asyncio.Lock()
            await local_pool.put((sender, lock))
        sender_pool = local_pool

        logger.info("Application startup: Initializing Redis client.")
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
        # Close all pooled senders
        if sender_pool:
            logger.info("Application shutdown: Closing Service Bus sender pool.")
            while not sender_pool.empty():
                sender, _ = await sender_pool.get()
                # Cleanly close AMQP link for each sender
                await sender.__aexit__(None, None, None)
        # Close client
        logger.info("Application shutdown: Closing Service Bus client.")
        if local_sb_client:
            await local_sb_client.close()
        if local_redis_client:
            await local_redis_client.aclose()
        # Close credentials
        await credential.close()
        # Reset globals
        sb_client = None
        sender_pool = None
        redis_client = None

app = FastAPI(
    title="Scalable Chat Front Service",
    version="0.1.0",
    description="Front-end for scalable chat using SSE and Azure Service Bus",
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

class SessionStartRequest(BaseModel):
    userId: str

class ChatMessage(BaseModel):
    message: str
    sessionId: str
    chatMessageId: str
    userId: str

class ChatResponse(BaseModel):
    success: bool
    chatMessageId: str
    sessionId: str
    message: str = "Message queued for processing"
    runId: str | None = None
    eventsUrl: str | None = None

class SessionStartResponse(BaseModel):
    sessionId: str


def _run_key(run_id: str) -> str:
    return f"run:{run_id}"


def _run_map_key(session_id: str, chat_message_id: str) -> str:
    return f"run-map:{session_id}:{chat_message_id}"


def _latest_user_text(run_request: CreateRunRequest) -> str:
    for message in reversed(run_request.input.messages):
        if message.role == "user":
            return message.content
    raise HTTPException(status_code=400, detail="Run input must include at least one user message.")


async def _send_service_bus_message(payload: dict, message_id: str, session_id: str) -> None:
    global sender_pool
    if not sender_pool:
        raise HTTPException(status_code=503, detail="Service Bus sender pool not initialized.")

    message_to_send = ServiceBusMessage(
        body=json.dumps(payload),
        message_id=message_id,
        session_id=session_id,
    )
    sender, lock = await sender_pool.get()
    try:
        async with lock:
            await sender.send_messages(message_to_send)
    finally:
        await sender_pool.put((sender, lock))


async def _create_run(
    run_request: CreateRunRequest,
    *,
    session_id: str | None = None,
    chat_message_id: str | None = None,
) -> RunResponse:
    global sb_client, redis_client
    if not sb_client:
        raise HTTPException(status_code=503, detail="Service Bus client not initialized.")
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis client not initialized.")

    user_text = _latest_user_text(run_request)
    run_id = f"run_{uuid.uuid4().hex}"
    thread_id = run_request.threadId or session_id or str(uuid.uuid4())
    effective_session_id = session_id or thread_id
    effective_chat_message_id = chat_message_id or f"msg_{uuid.uuid4().hex}"

    metadata = RunMetadata(
        id=run_id,
        runId=run_id,
        threadId=thread_id,
        sessionId=effective_session_id,
        chatMessageId=effective_chat_message_id,
        userId=run_request.userId,
        status="queued",
        createdAt=utc_now(),
        inputSummary=user_text[:500],
        capabilities=run_request.capabilities,
    )
    metadata_json = metadata.model_dump_json()
    await redis_client.setex(_run_key(run_id), RUN_TTL_SECONDS, metadata_json)
    await redis_client.setex(
        _run_map_key(effective_session_id, effective_chat_message_id),
        RUN_TTL_SECONDS,
        run_id,
    )

    payload = {
        "runId": run_id,
        "threadId": thread_id,
        "sessionId": effective_session_id,
        "chatMessageId": effective_chat_message_id,
        "userId": run_request.userId,
        "text": user_text,
        "input": run_request.input.model_dump(),
        "capabilities": run_request.capabilities.model_dump(),
    }
    await _send_service_bus_message(payload, run_id, thread_id)

    logger.info("Created run %s for thread %s and user %s", run_id, thread_id, run_request.userId)
    return RunResponse(
        runId=run_id,
        threadId=thread_id,
        status="queued",
        eventsUrl=f"/api/runs/{run_id}/events",
    )

@app.post("/api/session/start", response_model=SessionStartResponse)
async def start_session(session_request: SessionStartRequest):
    """
    Start a new chat session for a user.
    
    Args:
        session_request: Contains the user ID for the session
        
    Returns:
        SessionStartResponse: Contains the generated session ID
    """
    sessionId = str(uuid.uuid4())
    
    # Add custom dimensions to current span for observability
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("app.user_id", session_request.userId)
        current_span.set_attribute("app.session_id", sessionId)
        current_span.set_attribute("app.operation", "session_start")
    
    logger.info(f"New session started: {sessionId} for user: {session_request.userId}")
    return SessionStartResponse(sessionId=sessionId)


@app.post("/api/runs", response_model=RunResponse)
async def create_run(run_request: CreateRunRequest, request: Request):
    """
    Create a durable agent run and enqueue it for asynchronous processing.
    """
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("app.user_id", run_request.userId)
        current_span.set_attribute("app.operation", "create_run")
        current_span.set_attribute("app.message_count", len(run_request.input.messages))

    try:
        return await _create_run(run_request)
    except HTTPException:
        raise
    except Exception as e:
        if current_span.is_recording():
            current_span.set_attribute("app.error_message", str(e))
        logger.error("Failed to create run: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create run.")


@app.get("/api/runs/{runId}")
async def get_run(runId: str):
    """
    Return current run metadata.
    """
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis client not initialized.")
    run_data = await redis_client.get(_run_key(runId))
    if not run_data:
        raise HTTPException(status_code=404, detail="Run not found.")
    return json.loads(run_data)


@app.post("/api/runs/{runId}/cancel")
async def cancel_run(runId: str):
    """
    Mark a run for explicit cancellation.
    """
    if not redis_client:
        raise HTTPException(status_code=503, detail="Redis client not initialized.")
    run_data = await redis_client.get(_run_key(runId))
    if not run_data:
        raise HTTPException(status_code=404, detail="Run not found.")

    run = json.loads(run_data)
    if run.get("status") not in {"completed", "failed", "cancelled"}:
        run["status"] = "cancelling"
        await redis_client.setex(_run_key(runId), RUN_TTL_SECONDS, json.dumps(run))
    await redis_client.setex(f"run:{runId}:cancel_requested", RUN_TTL_SECONDS, "true")
    return {"runId": runId, "status": run["status"]}

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(chat_message: ChatMessage, request: Request):
    """
    Process a chat message by sending it to the Service Bus topic.
    
    Args:
        chat_message: The chat message to process
        request: FastAPI request object
        
    Returns:
        ChatResponse: Confirmation of message processing
        
    Raises:
        HTTPException: If Service Bus is not available or message sending fails
    """
    # Add custom dimensions to current span for observability
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("app.user_id", chat_message.userId)
        current_span.set_attribute("app.session_id", chat_message.sessionId)
        current_span.set_attribute("app.chat_message_id", chat_message.chatMessageId)
        current_span.set_attribute("app.operation", "chat_message")
        current_span.set_attribute("app.message_length", len(chat_message.message))
    
    logger.info(f"Received message: '{chat_message.message}' for session: {chat_message.sessionId}, chatMessageId: {chat_message.chatMessageId}, userId: {chat_message.userId}")

    try:
        run_response = await _create_run(
            CreateRunRequest(
                threadId=chat_message.sessionId,
                userId=chat_message.userId,
                input=RunInput(messages=[{"role": "user", "content": chat_message.message}]),
                capabilities=RunCapabilities(text=True, toolEvents=True),
            ),
            session_id=chat_message.sessionId,
            chat_message_id=chat_message.chatMessageId,
        )

        # Add success information to span
        if current_span.is_recording():
            current_span.set_attribute("app.servicebus_send_success", True)
            current_span.set_attribute("app.run_id", run_response.runId)
            
        logger.info(f"Message {chat_message.chatMessageId} for session {chat_message.sessionId} sent to topic '{SERVICEBUS_USER_MESSAGES_TOPIC}'")
        
        return ChatResponse(
            success=True,
            chatMessageId=chat_message.chatMessageId,
            sessionId=chat_message.sessionId,
            runId=run_response.runId,
            eventsUrl=run_response.eventsUrl,
        )
        
    except Exception as e:
        # Add error information to span
        if current_span.is_recording():
            current_span.set_attribute("app.servicebus_send_success", False)
            current_span.set_attribute("app.error_message", str(e))
            
        logger.error(f"Failed to send message to Service Bus for session {chat_message.sessionId}, chatMessageId {chat_message.chatMessageId}: {e}")
        raise HTTPException(status_code=500, detail="Failed to process message.")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)