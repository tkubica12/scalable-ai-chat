"""
Memory Worker Service

This service processes message-completed events from Azure Service Bus, analyzes conversation 
data for memory extraction, and stores user memories and conversation summaries in Cosmos DB.

TRACING AND OBSERVABILITY:
This service implements custom OpenTelemetry tracing with application-specific attributes.
All spans created by this application automatically include:
- app.user_id: The user ID from the current context
- app.session_id: The session ID from the current context  
- app.chat_message_id: The message ID from the current context
- app.name: "memory-worker" (service identifier)

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
from typing import List, Literal
from dotenv import load_dotenv
from azure.identity.aio import DefaultAzureCredential
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanProcessor
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
from openai import AsyncAzureOpenAI
import redis.asyncio as redis
from redis_entraid.cred_provider import create_from_default_azure_credential
from azure.cosmos.aio import CosmosClient
from azure.cosmos import PartitionKey
from pydantic import BaseModel, Field, ConfigDict

# Load .env in development
load_dotenv()

# Enable OpenTelemetry instrumentation for OpenAI SDK
OpenAIInstrumentor().instrument()

# Set environment variable to capture message content
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")

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
            span.set_attribute("app.name", "memory-worker")
            
        except Exception as e:
            # Don't fail span creation if attribute setting fails
            logger.warning(f"Failed to add application attributes to span: {e}")
    
    def on_end(self, span):
        """Called when a span is ended. No action needed for our use case."""
        pass
    
    def shutdown(self):
        """Called when the processor is shut down. No action needed for our use case."""
        pass
    
    def force_flush(self, timeout_millis=30000):
        """Called to force flush any pending spans. No action needed for our use case."""
        pass

# Pydantic models for structured output
class ConversationSummary(BaseModel):
    """Structured output model for conversation analysis"""
    model_config = ConfigDict(extra="forbid")
    
    summary: str = Field(description="A concise paragraph summarizing the conversation")
    themes: List[str] = Field(description="Array of key topics discussed (max 5)")
    persons: List[str] = Field(description="Array of people mentioned by name (excluding the user and assistant)")
    places: List[str] = Field(description="Array of specific locations mentioned")
    user_sentiment: Literal["positive", "neutral", "negative"] = Field(description="Overall user sentiment")

class UserMemoryUpdates(BaseModel):
    """Structured output model for user memory updates."""
    model_config = ConfigDict(extra="forbid")
    
    output_preferences: List[str] = Field(description="Array of strings of User's preferred output styles")
    personal_preferences: List[str] = Field(description="Array of strings of how user prefers to be addressed")
    assistant_preferences: List[str] = Field(description="Array of strings of User's preferences for assistant behavior")
    knowledge: List[str] = Field(description="Array of strings of topics where user demonstrates understanding")
    interests: List[str] = Field(description="Array of strings of user's hobbies and interests")
    dislikes: List[str] = Field(description="Array of strings of things user explicitly dislikes")
    family_and_friends: List[str] = Field(description="Array of strings of personal connections user mentions")
    work_profile: List[str] = Field(description="Array of strings of professional information user shares")
    goals: List[str] = Field(description="Array of strings of user's stated objectives or aspirations")

# Service Bus configuration
SERVICEBUS_FULLY_QUALIFIED_NAMESPACE = os.getenv("SERVICEBUS_FULLY_QUALIFIED_NAMESPACE")
SERVICEBUS_MESSAGE_COMPLETED_TOPIC = os.getenv("SERVICEBUS_MESSAGE_COMPLETED_TOPIC")
SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION = os.getenv("SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 10))

if not SERVICEBUS_FULLY_QUALIFIED_NAMESPACE or not SERVICEBUS_MESSAGE_COMPLETED_TOPIC or not SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION:
    raise RuntimeError("Missing Service Bus configuration in environment variables")

# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6380))
REDIS_SSL = os.getenv("REDIS_SSL", "true").lower() == "true"

if not REDIS_HOST:
    raise RuntimeError("Missing required environment variable REDIS_HOST")

# Azure OpenAI configuration  
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
AZURE_OPENAI_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME")

if not AZURE_OPENAI_ENDPOINT:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_ENDPOINT")

if not AZURE_OPENAI_DEPLOYMENT_NAME:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_DEPLOYMENT_NAME")

if not AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME")

# CosmosDB configuration
COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT")
COSMOS_DATABASE_NAME = os.getenv("COSMOS_DATABASE_NAME", "memory_db")
COSMOS_CONTAINER_NAME_CONVERSATIONS = os.getenv("COSMOS_CONTAINER_NAME_CONVERSATIONS", "conversations")
COSMOS_CONTAINER_NAME_USER_MEMORIES = os.getenv("COSMOS_CONTAINER_NAME_USER_MEMORIES", "user-memories")

if not COSMOS_ENDPOINT:
    raise RuntimeError("Missing required environment variable COSMOS_ENDPOINT")

# Logging
LOG_LEVEL = os.getenv('LOG_LEVEL', 'WARNING').upper()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Azure Monitor
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
redis_client = None
chat_client = None
cosmos_client = None

# Global shutdown event for graceful shutdown
shutdown_event = asyncio.Event()

async def generate_vector_embedding(text: str) -> List[float]:
    """
    Generate vector embedding for the given text using Azure OpenAI.
    
    Args:
        text: The text to generate embeddings for
        
    Returns:
        List[float]: Vector embedding for the text
        
    Raises:
        Exception: If embedding generation fails
    """
    try:
        response = await chat_client.embeddings.create(
            input=[text],
            model=AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Error generating vector embedding: {e}")
        return []


async def extract_conversation_summary(conversation_data: dict) -> dict:
    """
    Extract conversation summary and metadata using LLM with structured outputs.
    """
    try:
        messages = conversation_data.get("messages", [])
        if not messages:
            default_summary = ConversationSummary(
                summary="Empty conversation",
                themes=[],
                persons=[],
                places=[],
                user_sentiment="neutral"
            )
            return default_summary.model_dump()
        
        # Build conversation text for analysis
        conversation_text = ""
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            conversation_text += f"{role}: {content}\n"
        
        # Prepare LLM prompt for structured output
        system_prompt = """
You are a conversation analyzer. Analyze the following conversation and extract key information.

Focus on:
- Creating a concise paragraph summary of the conversation
- Identifying key topics/themes discussed (maximum 5)
- Finding people mentioned by name (excluding the user and assistant)
- Locating specific places or locations mentioned
- Determining the overall user sentiment

Focus on factual information and avoid speculation. 
It is OK to return empty field if not applicable. 
Return structured data following the specified schema.
"""

        user_prompt = f"Analyze this conversation:\n\n{conversation_text}"
        response = await chat_client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1,
            max_tokens=1000,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "ConversationAnalysis", 
                    "schema": ConversationSummary.model_json_schema(),
                    "description": "Structured analysis of a conversation including summary, themes, persons, places, and user sentiment",
                    "strict": True
                }
            }
        )
        
        # Parse the structured response
        content = response.choices[0].message.content
        try:
            # Validate and parse using Pydantic
            analysis = ConversationSummary.model_validate_json(content)
            return analysis.model_dump()
        except Exception as e:
            logger.warning(f"Failed to parse structured LLM response: {e}, content: {content}")
            # Return default values with proper structure
            default_summary = ConversationSummary(
                summary="Failed to analyze conversation",
                themes=[],
                persons=[],
                places=[],
                user_sentiment="neutral"
            )
            return default_summary.model_dump()
            
    except Exception as e:
        logger.error(f"Error extracting conversation summary: {e}")
        return {
            "summary": "Error analyzing conversation",
            "themes": [],
            "persons": [],
            "places": [],
            "user_sentiment": "neutral"
        }


async def extract_user_memory_updates(conversation_data: dict, existing_memory: dict) -> dict:
    """
    Extract updates to user memory based on the conversation using structured outputs.
    """
    try:
        messages = conversation_data.get("messages", [])
        if not messages:
            return {}
        
        # Build conversation text for analysis
        conversation_text = ""
        user_messages = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            conversation_text += f"{role}: {content}\n"
            if role == "user":
                user_messages.append(content)
        
        if not user_messages:
            return {}
        
        # Prepare LLM prompt for user memory extraction
        existing_memory_json = json.dumps(existing_memory, indent=2)
        system_prompt = f"""
You are a user memory extractor. Based on the conversation, identify any new information about the user that should be added to their memory profile.

Current user memory profile (if any):
{existing_memory_json}

From the conversation, extract ONLY NEW information in these categories:
- output_preferences: User's preferred output styles (length, detail, format)
- personal_preferences: How user prefers to be addressed (name, pronouns, tone)
- assistant_preferences: User's preferences for assistant behavior (name, style)
- knowledge: Topics where user demonstrates understanding (add to existing)
- interests: User's hobbies, interests, subjects they enjoy (add to existing)
- dislikes: Topics, styles, or things user explicitly dislikes (add to existing)
- family_and_friends: Personal connections user mentions (merge with existing)
- work_profile: Professional information user shares (merge with existing)
- goals: User's stated objectives or aspirations (add to existing)

All extracted information should be from user messages in the conversation. Do not include assistant messages or system prompts. Those are provided for context only.

If new and existing information overlaps, merge them intelligently. For example, if user mentions a new interest that is similar to an existing one, combine them.

IMPORTANT: You must provide values for ALL fields in the response. If there is no information for a category, provide an empty array [] for lists.
"""

        user_prompt = f"Extract new user memory information from this conversation:\n\n{conversation_text}"
        response = await chat_client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1,
            max_tokens=1000,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "UserMemoryUpdates",
                    "schema": UserMemoryUpdates.model_json_schema(),
                    "description": "Updates to user memory based on conversation analysis",
                    "strict": True
                }
            }
        )
          # Parse the structured response
        content = response.choices[0].message.content
        try:
            # Validate and parse using Pydantic
            memory_updates = UserMemoryUpdates.model_validate_json(content)
            # Only return non-empty fields
            updates_dict = memory_updates.model_dump()
            # Filter out empty values - for nested objects, check if they have any fields
            filtered_updates = {}
            for k, v in updates_dict.items():
                if isinstance(v, list) and len(v) > 0:
                    filtered_updates[k] = v
                elif isinstance(v, dict) and len(v) > 0:
                    filtered_updates[k] = v
                elif isinstance(v, str) and v.strip():
                    filtered_updates[k] = v
            return filtered_updates
        except Exception as e:
            logger.warning(f"Failed to parse structured user memory updates: {e}, content: {content}")
            return {}
            
    except Exception as e:
        logger.error(f"Error extracting user memory updates: {e}")
        return {}


async def store_conversation_memory(session_id: str, user_id: str, analysis: dict):
    """
    Store conversation memory to CosmosDB.
    """
    try:
        # Generate text for embedding (combining all extracted fields)
        embedding_text = f"""Summary: {analysis['summary']}
Themes: {', '.join(analysis['themes'])}
Persons: {', '.join(analysis['persons'])}
Places: {', '.join(analysis['places'])}
User sentiment: {analysis['user_sentiment']}"""
        
        # Generate vector embedding
        vector_embedding = await generate_vector_embedding(embedding_text)
        
        # Create conversation document
        conversation_doc = {
            "id": f"{session_id}_{user_id}",
            "userId": user_id,
            "sessionId": session_id,
            "summary": analysis["summary"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "themes": analysis["themes"],
            "persons": analysis["persons"],
            "places": analysis["places"],
            "user_sentiment": analysis["user_sentiment"],
            "vector_embedding": vector_embedding
        }
          # Get conversations container
        database = cosmos_client.get_database_client(COSMOS_DATABASE_NAME)
        container = database.get_container_client(COSMOS_CONTAINER_NAME_CONVERSATIONS)
        
        # Upsert the document
        await container.upsert_item(conversation_doc)
        logger.info(f"Stored conversation memory for session {session_id} in CosmosDB")
                
    except Exception as e:
        logger.error(f"Error storing conversation memory to CosmosDB: {e}")


async def update_user_memory(user_id: str, updates: dict):
    """
    Update user memory directly in CosmosDB.
    Memory fields are REPLACED with LLM output (not merged) since LLM already does consolidation.
    Only system fields like userId, id, timestamp are preserved.
    """
    if not updates:
        return
        
    try:
        # Get user memories container
        database = cosmos_client.get_database_client(COSMOS_DATABASE_NAME)
        container = database.get_container_client(COSMOS_CONTAINER_NAME_USER_MEMORIES)
        
        # Try to get existing user memory document
        try:
            existing_doc = await container.read_item(item=user_id, partition_key=user_id)
        except:
            # Create new document if doesn't exist
            existing_doc = {
                "id": user_id,
                "userId": user_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "output_preferences": [],
                "personal_preferences": [],
                "assistant_preferences": [],
                "knowledge": [],
                "interests": [],
                "dislikes": [],
                "family_and_friends": [],
                "work_profile": [],
                "goals": []
            }
        
        # Memory field names that should be replaced (not merged) since LLM does consolidation
        memory_fields = {
            "output_preferences", "personal_preferences", "assistant_preferences",
            "knowledge", "interests", "dislikes", "family_and_friends", 
            "work_profile", "goals"
        }
        
        # Update document with LLM output - REPLACE memory fields, preserve system fields
        for key, value in updates.items():
            if key in memory_fields:
                # Replace memory fields entirely with LLM's consolidated output
                existing_doc[key] = value
            else:
                # For non-memory fields, preserve existing logic (shouldn't happen with current schema)
                existing_doc[key] = value
        
        # Update timestamp
        existing_doc["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        # Upsert the document
        await container.upsert_item(existing_doc)
        logger.info(f"Updated user memory for user {user_id} in CosmosDB - replaced fields: {list(updates.keys())}")
                
    except Exception as e:
        logger.error(f"Error updating user memory in CosmosDB: {e}")


async def get_existing_user_memory(user_id: str) -> dict:
    """
    Get existing user memory from CosmosDB.
    """
    try:        
        # Get user memories container
        database = cosmos_client.get_database_client(COSMOS_DATABASE_NAME)
        container = database.get_container_client(COSMOS_CONTAINER_NAME_USER_MEMORIES)
        
        # Try to read user memory document
        try:
            existing_memory = await container.read_item(item=user_id, partition_key=user_id)
            return existing_memory
        except:
            # Return empty memory structure if document doesn't exist
            return {
                "output_preferences": [],
                "personal_preferences": [],
                "assistant_preferences": [],
                "knowledge": [],
                "interests": [],
                "dislikes": [],
                "family_and_friends": [],
                "work_profile": [],
                "goals": []
            }
                
    except Exception as e:
        logger.error(f"Error getting existing user memory from CosmosDB: {e}")
        return {
            "output_preferences": [],
            "personal_preferences": [],
            "assistant_preferences": [],
            "knowledge": [],
            "interests": [],
            "dislikes": [],
            "family_and_friends": [],
            "work_profile": [],
            "goals": []
        }


async def get_conversation_from_redis(session_id: str) -> dict:
    """
    Fetch conversation data from Redis.
    """
    try:
        conversation_json = await redis_client.get(f"session:{session_id}")
        if conversation_json:
            return json.loads(conversation_json)
        else:
            logger.warning(f"No conversation found in Redis for session {session_id}")
            return {}
    except Exception as e:
        logger.error(f"Error fetching conversation from Redis: {e}")
        return {}


async def process_completed_message(message_body: dict):
    """
    Process a completed message and extract/store memories.
    
    Args:
        message_body: Dictionary containing the completed message data
    """
    try:
        session_id = message_body.get("sessionId")
        user_id = message_body.get("userId")
        chat_message_id = message_body.get("chatMessageId")
        
        # Set context variables so all spans in this operation will include these attributes
        set_context_attributes(
            user_id=user_id or "unknown",
            session_id=session_id or "unknown",
            message_id=chat_message_id or "unknown"
        )
        
        # Start a new span for message processing - it will automatically get the context attributes
        with tracer.start_as_current_span("process_completed_message") as span:
            # Add operation-specific attributes manually
            if span.is_recording():
                span.set_attribute("app.operation", "process_completed_message")
            
            if not session_id or not user_id:
                if span.is_recording():
                    span.set_attribute("app.error", "missing_required_fields")
                logger.error(f"Missing sessionId or userId in message: {message_body}")
                return
            
            logger.info(f"Processing memory extraction for session {session_id}, user {user_id}")
            
            # Fetch conversation data from Redis
            conversation_data = await get_conversation_from_redis(session_id)
            if not conversation_data:
                if span.is_recording():
                    span.set_attribute("app.error", "conversation_not_found")
                logger.error(f"No conversation data found for session {session_id}")
                return
            
            # Add conversation metrics to span
            if span.is_recording():
                messages = conversation_data.get("messages", [])
                span.set_attribute("app.message_count", len(messages))
            
            # Extract conversation summary and metadata
            analysis = await extract_conversation_summary(conversation_data)
            
            # Store conversation memory
            await store_conversation_memory(session_id, user_id, analysis)
            
            # Get existing user memory
            existing_memory = await get_existing_user_memory(user_id)
            
            # Extract user memory updates
            memory_updates = await extract_user_memory_updates(conversation_data, existing_memory)
              # Update user memory if there are changes
            if memory_updates:
                await update_user_memory(user_id, memory_updates)
                
                # Add memory update metrics to span
                if span.is_recording():
                    span.set_attribute("app.memory_updates_applied", True)
                    span.set_attribute("app.memory_update_categories", list(memory_updates.keys()))
                    
                logger.info(f"Extracted memory updates for user {user_id}: {list(memory_updates.keys())}")
            else:
                if span.is_recording():
                    span.set_attribute("app.memory_updates_applied", False)
                logger.info(f"No new memory information found for user {user_id}")
                
    except Exception as e:
        # Create an error span that will also get context attributes
        with tracer.start_as_current_span("process_completed_message_error") as error_span:
            if error_span.is_recording():
                error_span.set_attribute("app.error", "processing_error")
                error_span.set_attribute("app.operation", "process_completed_message")
        logger.error(f"Error processing completed message: {e}")
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
                message_body = json.loads(str(msg))
                logger_instance.info(f"Received message: {message_body}")
                await process_completed_message(message_body)
                
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
    Setup signal handlers for graceful shutdown.
    """
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}. Initiating graceful shutdown...")
        shutdown_event.set()
    
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
    """
    Main application entry point.
    """
    global redis_client, chat_client, cosmos_client
    
    logger.info("Starting Memory Worker service...")
    
    # Setup signal handlers
    await setup_signal_handlers()
    
    # Initialize clients outside the Service Bus loop
    try:
        redis_credential_provider = create_from_default_azure_credential(
            ("https://redis.azure.com/.default",)
        )
        
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            ssl=REDIS_SSL,
            ssl_cert_reqs=None,  # For Azure Managed Redis, SSL cert validation can be relaxed
            credential_provider=redis_credential_provider,
            protocol=3,
            health_check_interval=30,
        )
        
        # Test Redis connection
        await redis_client.ping()
        logger.info("Redis connection established")
          # Initialize OpenAI Azure client
        async def get_azure_token():
            token = await shared_credential.get_token("https://cognitiveservices.azure.com/.default")
            return token.token
        
        chat_client = AsyncAzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            azure_ad_token_provider=get_azure_token,
            api_version=AZURE_OPENAI_API_VERSION        )
        logger.info("OpenAI client initialized")
        
        # Initialize CosmosDB client
        cosmos_client = CosmosClient(
            url=COSMOS_ENDPOINT,
            credential=shared_credential
        )
        logger.info("CosmosDB client initialized")
        
    except Exception as e:
        logger.error(f"Failed to initialize clients: {e}")
        sys.exit(1)
    
    # Initialize semaphore for concurrency control and task tracking
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    active_tasks = set()
    
    try:
        while not shutdown_event.is_set():
            try:
                async with ServiceBusClient(
                    fully_qualified_namespace=SERVICEBUS_FULLY_QUALIFIED_NAMESPACE,
                    credential=shared_credential
                ) as servicebus_client:
                    
                    logger.info(f"Connected to Service Bus, listening for messages on topic '{SERVICEBUS_MESSAGE_COMPLETED_TOPIC}', subscription '{SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION}'")
                    
                    async with servicebus_client.get_subscription_receiver(
                        topic_name=SERVICEBUS_MESSAGE_COMPLETED_TOPIC,
                        subscription_name=SERVICEBUS_MESSAGE_COMPLETED_SUBSCRIPTION,
                        max_wait_time=30,
                        max_concurrent_calls=MAX_CONCURRENCY
                    ) as receiver:
                        
                        logger.info("Memory Worker is running and listening for messages...")
                        
                        async for msg in receiver:
                            # Check for shutdown
                            if shutdown_event.is_set():
                                logger.info("Shutdown event set, stopping message reception")
                                await receiver.abandon_message(msg)
                                break
                            
                            # Create a task to process the message
                            task = asyncio.create_task(
                                _process_and_handle_message(servicebus_client, msg, receiver, semaphore, logger)
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
        logger.info("Shutting down Memory Worker service...")
        
        # Wait for active tasks to complete during shutdown
        await wait_for_tasks_completion(active_tasks, timeout=60)
          # Cleanup resources
        try:
            if redis_client:
                await redis_client.aclose()
                logger.info("Redis client closed")
        except Exception as e:
            logger.error(f"Error closing Redis client: {e}")
        
        try:
            if chat_client:
                await chat_client.aclose()
                logger.info("Chat client closed")
        except Exception as e:
            logger.error(f"Error closing chat client: {e}")
            
        try:
            if cosmos_client:
                await cosmos_client.aclose()
                logger.info("CosmosDB client closed")
        except Exception as e:
            logger.error(f"Error closing CosmosDB client: {e}")
        
        logger.info("Memory Worker service stopped")


if __name__ == "__main__":
    asyncio.run(main())
