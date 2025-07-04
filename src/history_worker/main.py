"""
History Worker Service

This service processes message-completed events from Azure Service Bus, fetches conversation 
data from Redis, and persists it to Cosmos DB with auto-generated titles.

TRACING AND OBSERVABILITY:
This service implements custom OpenTelemetry tracing with application-specific attributes.
All spans created by this application automatically include:
- app.user_id: The user ID from the current context
- app.session_id: The session ID from the current context  
- app.chat_message_id: The message ID from the current context
- app.name: "history-worker" (service identifier)

These attributes are automatically added to ALL spans (including spans created by Azure SDKs
like Cosmos DB, Redis, Service Bus, and AI inference calls) through a custom SpanProcessor.

Usage:
1. Set context at the start of an operation using set_context_attributes()
2. All subsequent spans within that async context will inherit these attributes
3. Context variables use Python's contextvars for proper async isolation
"""

import os
import json
import asyncio
import logging
import signal
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from dotenv import load_dotenv
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanProcessor
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
from openai import AsyncAzureOpenAI
from azure.cosmos.aio import CosmosClient
from azure.cosmos import exceptions
import redis.asyncio as redis
from redis_entraid.cred_provider import create_from_default_azure_credential


# Load .env in development
load_dotenv()

# Set environment variable to capture message content before instrumentation
enable_content_recording = os.getenv("OTEL_INSTRUMENTATION_OPENAI_V2_RECORD_MESSAGE_CONTENT", "false").lower() == "true"

# Enable OpenTelemetry instrumentation for OpenAI SDK with proper configuration
OpenAIInstrumentor().instrument(
    capture_content=enable_content_recording
)

# Context variables for storing application-specific information across spans
current_user_id: ContextVar[str] = ContextVar('current_user_id', default=None)
current_session_id: ContextVar[str] = ContextVar('current_session_id', default=None)
current_message_id: ContextVar[str] = ContextVar('current_message_id', default=None)


def set_context_attributes(user_id: str = None, session_id: str = None, message_id: str = None):
    """
    Helper function to set context variables for tracing.
    
    Args:
        user_id: User ID to set in context
        session_id: Session ID to set in context  
        message_id: Message ID to set in context
    """
    if user_id is not None:
        current_user_id.set(user_id)
    if session_id is not None:
        current_session_id.set(session_id)
    if message_id is not None:
        current_message_id.set(message_id)


def clear_context_attributes():
    """
    Clear all context variables.
    """
    current_user_id.set(None)
    current_session_id.set(None)
    current_message_id.set(None)


class AppAttributesSpanProcessor(SpanProcessor):
    """
    Custom span processor that adds application-specific attributes to all spans.
    
    This processor automatically adds user_id, session_id, and message_id attributes
    to every span created by this application, pulling the values from context variables.
    """
    
    def on_start(self, span, parent_context=None):
        """
        Called when a span is started. Adds application-specific attributes.
        
        Args:
            span: The span that was started
            parent_context: The parent context of the span
        """
        try:
            # Add user_id if available in context
            user_id = current_user_id.get()
            if user_id:
                span.set_attribute("app.user_id", user_id)
            
            # Add session_id if available in context
            session_id = current_session_id.get()
            if session_id:
                span.set_attribute("app.session_id", session_id)
            
            # Add message_id if available in context
            message_id = current_message_id.get()
            if message_id:
                span.set_attribute("app.chat_message_id", message_id)
                
            # Add application name for easier filtering
            span.set_attribute("app.name", "history-worker")
            
        except Exception:
            # Don't fail span creation if attribute setting fails
            # Silently ignore errors to avoid telemetry issues
            pass
    
    def on_end(self, span):
        """Called when a span is ended. No action needed for our use case."""
        pass
    
    def shutdown(self):
        """Called when the processor is shut down. No action needed for our use case."""
        pass
    
    def force_flush(self, timeout_millis=30000):
        """Called to force flush any pending spans. No action needed for our use case."""
        pass


# Service Bus configuration
SERVICEBUS_FULLY_QUALIFIED_NAMESPACE = os.getenv("SERVICEBUS_FULLY_QUALIFIED_NAMESPACE")
SERVICEBUS_MESSAGE_COMPLETED_TOPIC = os.getenv("SERVICEBUS_MESSAGE_COMPLETED_TOPIC")
SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION = os.getenv("SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 10))

if not SERVICEBUS_FULLY_QUALIFIED_NAMESPACE or not SERVICEBUS_MESSAGE_COMPLETED_TOPIC or not SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION:
    raise RuntimeError("Missing Service Bus configuration in environment variables")

# Cosmos DB configuration
COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT")
COSMOS_DATABASE_NAME = os.getenv("COSMOS_DATABASE_NAME", "mydb")
COSMOS_CONTAINER_NAME = os.getenv("COSMOS_CONTAINER_NAME", "mydocuments")

if not COSMOS_ENDPOINT:
    raise RuntimeError("Missing required environment variable COSMOS_ENDPOINT")

# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6380))
REDIS_SSL = os.getenv("REDIS_SSL", "true").lower() == "true"

if not REDIS_HOST:
    raise RuntimeError("Missing required environment variable REDIS_HOST")

# Azure OpenAI configuration for title generation
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
AZURE_OPENAI_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")

if not AZURE_OPENAI_ENDPOINT:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_ENDPOINT")

if not AZURE_OPENAI_DEPLOYMENT_NAME:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_DEPLOYMENT_NAME")

# Logging configuration - set up early to avoid telemetry issues
LOG_LEVEL = os.getenv('LOG_LEVEL', 'WARNING').upper()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Azure Monitor (optional, for observability)
configure_azure_monitor(
    enable_live_metrics=True,
    instrumentation_options={
        "azure_sdk": {"enabled": True},
        "django": {"enabled": False},
        "fastapi": {"enabled": False},
        "flask": {"enabled": False},
        "psycopg2": {"enabled": False},
        "requests": {"enabled": False},
        "urllib": {"enabled": False},
        "urllib3": {"enabled": False},
    }
)

# Add our custom span processor to the global tracer provider
tracer_provider = trace.get_tracer_provider()
if hasattr(tracer_provider, 'add_span_processor'):
    app_attributes_processor = AppAttributesSpanProcessor()
    tracer_provider.add_span_processor(app_attributes_processor)
    logger.info("Custom span processor for application attributes added successfully")
else:
    logger.warning("Could not add custom span processor - tracer provider doesn't support it")

tracer = trace.get_tracer(__name__)

# Shared Azure credentials
shared_credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)

# Global clients - will be initialized in main()
cosmos_client = None
redis_client = None
chat_client = None

# Global shutdown event for graceful shutdown
shutdown_event = asyncio.Event()


async def generate_conversation_title(conversation_data: dict) -> str:
    """
    Generate a conversation title based on the full conversation history using LLM.
    """
    try:
        messages = conversation_data.get("messages", [])
        if not messages:
            return "New Conversation"
        
        # If title already exists, return it
        if conversation_data.get("title"):
            return conversation_data["title"]
        
        # Build conversation summary for title generation
        # Take up to 6 messages (3 exchanges) to capture the conversation essence
        conversation_excerpt = []
        for i, msg in enumerate(messages[:6]):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                conversation_excerpt.append(f"User: {content[:150]}")
            elif role == "assistant":
                conversation_excerpt.append(f"Assistant: {content[:150]}")
        
        if not conversation_excerpt:
            return "New Conversation"
        
        conversation_text = "\n".join(conversation_excerpt)
          # Generate title using LLM with full conversation context
        title_messages = [
            {"role": "system", "content": "You are a helpful assistant that generates concise conversation titles. Analyze the conversation and generate a short, descriptive title (3-6 words) that captures the main topic or theme. Do not use quotes or special characters. Return only the title."},
            {"role": "user", "content": f"Generate a descriptive title for this conversation:\n\n{conversation_text}"}
        ]
        
        completion = await chat_client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT_NAME,
            messages=title_messages, 
            max_tokens=25, 
            temperature=0.3
        )
        generated_title = completion.choices[0].message.content.strip()
        
        # Clean up the title
        generated_title = generated_title.replace('"', '').replace("'", '').replace(':', '')
        if len(generated_title) > 50:
            generated_title = generated_title[:50].strip()
        
        # Ensure title is not empty after cleanup
        if not generated_title:
            generated_title = "New Conversation"
        
        logger.info(f"Generated title: '{generated_title}' for session {conversation_data.get('sessionId')} based on {len(messages)} messages")
        return generated_title
        
    except Exception as e:
        # For any errors, log and return fallback
        # Don't fail the entire conversation persistence for title generation
        logger.warning(f"Error generating conversation title, using fallback: {e}")
        return "New Conversation"


async def fetch_conversation_from_redis(session_id: str) -> dict:
    """
    Fetch conversation data from Redis.
    """
    try:
        redis_key = f"session:{session_id}"
        conversation_data = await redis_client.get(redis_key)
        
        if not conversation_data:
            logger.warning(f"No conversation data found in Redis for session {session_id}")
            return None
        
        conversation = json.loads(conversation_data)
        logger.info(f"Retrieved conversation data from Redis for session {session_id} with {len(conversation.get('messages', []))} messages")
        return conversation
        
    except Exception as e:
        logger.error(f"Error fetching conversation from Redis for session {session_id}: {e}")
        raise


async def persist_conversation_to_cosmos(conversation_data: dict):
    """
    Persist conversation data to Cosmos DB.
    """
    # Ensure required fields
    session_id = conversation_data.get("sessionId")
    user_id = conversation_data.get("userId")
    
    if not session_id or not user_id:
        logger.error(f"Missing required fields in conversation data: sessionId={session_id}, userId={user_id}")
        raise ValueError(f"Missing required fields in conversation data: sessionId={session_id}, userId={user_id}")
    
    # Generate title if not present
    if not conversation_data.get("title"):
        conversation_data["title"] = await generate_conversation_title(conversation_data)
    # Prepare document for Cosmos DB
    cosmos_document = {
        "id": session_id, 
        "sessionId": session_id,
        "userId": user_id, 
        "title": conversation_data.get("title"),
        "createdAt": conversation_data.get("createdAt"),
        "lastActivity": conversation_data.get("lastActivity"),
        "messages": conversation_data.get("messages", []),
        "persistedAt": datetime.now(timezone.utc).isoformat()
    }
    # Get container reference
    database = cosmos_client.get_database_client(COSMOS_DATABASE_NAME)
    container = database.get_container_client(COSMOS_CONTAINER_NAME)
    
    # Upsert document with simple retry on 429 throttling
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await container.upsert_item(body=cosmos_document)
            break  # Success, exit retry loop
        except exceptions.CosmosHttpResponseError as e:
            if e.status_code == 429 and attempt < max_retries - 1:
                # Throttling, wait and retry
                wait_time = (attempt + 1) * 2  # 2, 4, 6 seconds
                logger.warning(f"Cosmos DB throttled (429), retrying in {wait_time} seconds (attempt {attempt + 1})")
                await asyncio.sleep(wait_time)
                continue
            else:
                # Other Cosmos errors or max retries exceeded
                raise
    
    logger.info(f"Successfully persisted conversation {session_id} to Cosmos DB with title: '{cosmos_document['title']}'")


async def process_message_completed_event(sb_client: ServiceBusClient, service_bus_message):
    """
    Process a message-completed event: fetch conversation from Redis and persist to Cosmos DB.
    
    Args:
        sb_client: Service Bus client
        service_bus_message: Message from Service Bus containing completion event
    """
    try:
        message_body_str = str(service_bus_message)
        logger.info(f"Received message-completed event: {message_body_str}")
        message_data = json.loads(message_body_str)
        
        session_id = message_data.get("sessionId")
        user_id = message_data.get("userId")
        chat_message_id = message_data.get("chatMessageId")
        
        # Set context variables so all spans in this operation will include these attributes
        set_context_attributes(session_id=session_id or "unknown", user_id=user_id or "unknown", message_id=chat_message_id or "unknown")
        
        # Start a new span for message processing - it will automatically get the context attributes
        with tracer.start_as_current_span("process_message_completed") as span:
            # Add operation-specific attributes manually
            if span.is_recording():
                span.set_attribute("app.operation", "process_message_completed")
            
            if not session_id:
                if span.is_recording():
                    span.set_attribute("app.error", "missing_session_id")
                logger.error(f"Message missing required sessionId: {message_data}")
                raise ValueError(f"Message missing required sessionId: {message_data}")
            
            logger.info(f"Processing message-completed event for session {session_id}, user {user_id}, chatMessage {chat_message_id}")
            
            # Fetch conversation data from Redis
            conversation_data = await fetch_conversation_from_redis(session_id)
            
            if not conversation_data:
                if span.is_recording():
                    span.set_attribute("app.error", "conversation_not_found_in_redis")
                logger.warning(f"Could not fetch conversation data for session {session_id}")
                raise Exception(f"Could not fetch conversation data for session {session_id}")
            
            # Add conversation metrics to span
            if span.is_recording():
                messages = conversation_data.get("messages", [])
                span.set_attribute("app.message_count", len(messages))
                span.set_attribute("app.has_title", bool(conversation_data.get("title")))
            
            # Persist to Cosmos DB
            await persist_conversation_to_cosmos(conversation_data)
            
            # Add success information to span
            if span.is_recording():
                span.set_attribute("app.persistence_success", True)
                
            logger.info(f"Successfully processed message-completed event for session {session_id}")
            
    except json.JSONDecodeError as e:
        # Start span for error handling - will also get context attributes
        with tracer.start_as_current_span("process_message_error") as error_span:
            if error_span.is_recording():
                error_span.set_attribute("app.error", "json_decode_error")
                error_span.set_attribute("app.operation", "process_message_completed")
        logger.error(f"Error parsing message body as JSON: {e}")
        raise
    except Exception as e:
        # Start span for error handling - will also get context attributes  
        with tracer.start_as_current_span("process_message_error") as error_span:
            if error_span.is_recording():
                error_span.set_attribute("app.error", "processing_error")
                error_span.set_attribute("app.operation", "process_message_completed")
        logger.error(f"Error processing message-completed event: {e}")
        raise


async def _process_and_handle_message(sb_client: ServiceBusClient, msg: ServiceBusMessage, receiver, semaphore: asyncio.Semaphore, logger_instance: logging.Logger):
    """
    Process a message, settle it, and release the concurrency semaphore.
    Simple approach: abandon any error so another worker can try.
    """
    async with semaphore:
        try:
            # Check for shutdown before processing
            if shutdown_event.is_set():
                logger_instance.info("Shutdown event set, abandoning message")
                await receiver.abandon_message(msg)
                return
            
            # Process the message within a span that will get application attributes
            with tracer.start_as_current_span("handle_service_bus_message") as handle_span:
                if handle_span.is_recording():
                    handle_span.set_attribute("app.message_id", msg.message_id)
                    handle_span.set_attribute("app.operation", "handle_service_bus_message")
                
                # Process the message - this will set context variables and create child spans
                await process_message_completed_event(sb_client, msg)
                
                # Complete the message if processing was successful
                await receiver.complete_message(msg)
                logger_instance.debug(f"Message {msg.message_id} completed successfully")
            
        except Exception as e:
            logger_instance.error(f"Error processing message {msg.message_id}: {e}")
            
            # Create an error span that will also get context attributes if they were set
            with tracer.start_as_current_span("handle_service_bus_message_error") as error_span:
                if error_span.is_recording():
                    error_span.set_attribute("app.message_id", msg.message_id)
                    error_span.set_attribute("app.operation", "handle_service_bus_message_error")
                    error_span.set_attribute("app.error", str(e))
                
                try:
                    # Abandon all errors so another worker can try
                    await receiver.abandon_message(msg)
                    logger_instance.warning(f"Message {msg.message_id} abandoned for another worker to try")
                        
                except Exception as settle_error:
                    logger_instance.error(f"Error settling message {msg.message_id}: {settle_error}")
                    # If we can't settle the message, it will be retried automatically


async def setup_signal_handlers():
    """
    Setup signal handlers for graceful shutdown using asyncio.
    """
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        shutdown_event.set()
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


async def wait_for_tasks_completion(active_tasks: set, timeout: int = 60):
    """
    Wait for all active tasks to complete or timeout.
    """
    if not active_tasks:
        return
    
    logger.info(f"Waiting for {len(active_tasks)} active tasks to complete...")
    
    try:
        await asyncio.wait_for(asyncio.gather(*active_tasks, return_exceptions=True), timeout=timeout)
        logger.info("All active tasks completed successfully")
    except asyncio.TimeoutError:
        logger.warning(f"Timeout waiting for tasks to complete after {timeout} seconds")
        # Cancel remaining tasks
        for task in active_tasks:
            if not task.done():
                task.cancel()
        # Wait briefly for cancellation to take effect
        await asyncio.gather(*active_tasks, return_exceptions=True)


async def main():
    global cosmos_client, redis_client, chat_client
    logger.info("Starting History Worker...")
    logger.info(f"Service Bus Namespace: {SERVICEBUS_FULLY_QUALIFIED_NAMESPACE}")
    logger.info(f"Listening for message-completed events on Topic: '{SERVICEBUS_MESSAGE_COMPLETED_TOPIC}', Subscription: '{SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION}'")
    logger.info(f"Cosmos DB Endpoint: {COSMOS_ENDPOINT}")
    logger.info(f"Cosmos Database: {COSMOS_DATABASE_NAME}, Container: {COSMOS_CONTAINER_NAME}")
    logger.info(f"Maximum concurrency for message processing: {MAX_CONCURRENCY}")
    logger.info(f"Redis Host: {REDIS_HOST}:{REDIS_PORT}, SSL: {REDIS_SSL}")
    
    # Setup signal handlers for graceful shutdown
    await setup_signal_handlers()
    
    # Initialize Cosmos DB client
    try:
        cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential=shared_credential)
        logger.info("Cosmos DB client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Cosmos DB client: {e}")
        raise
    
    # Initialize the Redis client with managed identity authentication
    try:
        redis_credential_provider = create_from_default_azure_credential(
            ("https://redis.azure.com/.default",)
        )
        
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            ssl=REDIS_SSL,
            ssl_cert_reqs=None,  # For Azure Managed Redis, SSL cert validation can be relaxed
            credential_provider=redis_credential_provider
        )
        
        await redis_client.ping()
        logger.info("Redis connection established successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize Redis client: {e}")
        raise
      # Initialize the OpenAI Azure client for title generation
    try:
        async def get_azure_token():
            token = await shared_credential.get_token("https://cognitiveservices.azure.com/.default")
            return token.token
        
        chat_client = AsyncAzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            azure_ad_token_provider=get_azure_token,
            api_version=AZURE_OPENAI_API_VERSION
        )
        logger.info("Azure OpenAI client initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Azure OpenAI client: {e}")
        raise
    
    credential = DefaultAzureCredential()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    active_tasks = set()
    
    try:
        while not shutdown_event.is_set():
            try:
                async with ServiceBusClient(
                    fully_qualified_namespace=SERVICEBUS_FULLY_QUALIFIED_NAMESPACE,
                    credential=credential
                ) as sb_client:
                    
                    logger.info(f"Connected to Service Bus, listening for messages on topic '{SERVICEBUS_MESSAGE_COMPLETED_TOPIC}', subscription '{SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION}'")
                    
                    async with sb_client.get_subscription_receiver(
                        topic_name=SERVICEBUS_MESSAGE_COMPLETED_TOPIC,
                        subscription_name=SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION,
                        max_wait_time=30
                    ) as receiver:
                        
                        async for msg in receiver:
                            # Check for shutdown
                            if shutdown_event.is_set():
                                logger.info("Shutdown event set, stopping message reception")
                                await receiver.abandon_message(msg)
                                break
                            
                            # Create a task to process the message
                            task = asyncio.create_task(
                                _process_and_handle_message(sb_client, msg, receiver, semaphore, logger)
                            )
                            active_tasks.add(task)
                            
                            # Remove completed tasks from the set
                            active_tasks = {t for t in active_tasks if not t.done()}
                            
                            logger.debug(f"Active tasks count: {len(active_tasks)}")
                            
            except Exception as e:
                if shutdown_event.is_set():
                    logger.info("Shutdown initiated, stopping message processing")
                    break
                logger.error(f"Error in main message loop: {e}")
                logger.info("Retrying in 5 seconds...")
                await asyncio.sleep(5)
    
    finally:
        logger.info("Shutting down History Worker...")
        
        # Wait for active tasks to complete
        await wait_for_tasks_completion(active_tasks, timeout=60)
        
        # Close clients
        try:
            if redis_client:
                await redis_client.aclose()
                logger.info("Redis client closed")
        except Exception as e:
            logger.error(f"Error closing Redis client: {e}")
        
        try:
            if cosmos_client:
                await cosmos_client.close()
                logger.info("Cosmos DB client closed")
        except Exception as e:
            logger.error(f"Error closing Cosmos DB client: {e}")
        
        logger.info("History Worker shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
