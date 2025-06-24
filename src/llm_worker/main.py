"""
LLM Worker Service

This service processes user messages from Azure Service Bus, generates responses using Azure AI 
inference, and streams results back through Service Bus for real-time chat experiences.

TRACING AND OBSERVABILITY:
This service implements custom OpenTelemetry tracing with application-specific attributes.
All spans created by this application automatically include:
- app.user_id: The user ID from the current context
- app.session_id: The session ID from the current context  
- app.chat_message_id: The message ID from the current context
- app.name: "llm-worker" (service identifier)

These attributes are automatically added to ALL spans (including spans created by Azure SDKs
like Redis, Service Bus, and AI inference calls) through a custom SpanProcessor.

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
import aiohttp
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
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
from jinja2 import Environment, FileSystemLoader


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
            span.set_attribute("app.name", "llm-worker")
            
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


# Service Bus and concurrency configuration
SERVICEBUS_FULLY_QUALIFIED_NAMESPACE = os.getenv("SERVICEBUS_FULLY_QUALIFIED_NAMESPACE")
SERVICEBUS_USER_MESSAGES_TOPIC = os.getenv("SERVICEBUS_USER_MESSAGES_TOPIC")
SERVICEBUS_USER_MESSAGES_SUBSCRIPTION = os.getenv("SERVICEBUS_USER_MESSAGES_SUBSCRIPTION")
SERVICEBUS_TOKEN_STREAMS_TOPIC = os.getenv("SERVICEBUS_TOKEN_STREAMS_TOPIC")
SERVICEBUS_MESSAGE_COMPLETED_TOPIC = os.getenv("SERVICEBUS_MESSAGE_COMPLETED_TOPIC")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 10))


if not SERVICEBUS_FULLY_QUALIFIED_NAMESPACE or not SERVICEBUS_USER_MESSAGES_TOPIC or not SERVICEBUS_USER_MESSAGES_SUBSCRIPTION or not SERVICEBUS_TOKEN_STREAMS_TOPIC:
    raise RuntimeError("Missing Service Bus configuration in environment variables")

if not SERVICEBUS_MESSAGE_COMPLETED_TOPIC:
    raise RuntimeError("Missing SERVICEBUS_MESSAGE_COMPLETED_TOPIC environment variable")


# Logging
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


# Azure OpenAI configuration  
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
AZURE_OPENAI_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")

if not AZURE_OPENAI_ENDPOINT:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_ENDPOINT")

if not AZURE_OPENAI_DEPLOYMENT_NAME:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_DEPLOYMENT_NAME")

# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6380))
REDIS_SSL = os.getenv("REDIS_SSL", "true").lower() == "true"

if not REDIS_HOST:
    raise RuntimeError("Missing required environment variable REDIS_HOST")

# Memory API configuration
MEMORY_API_ENDPOINT = os.getenv("MEMORY_API_ENDPOINT")
MEMORY_API_TIMEOUT = float(os.getenv("MEMORY_API_TIMEOUT", 2.0))

if not MEMORY_API_ENDPOINT:
    logger.warning("MEMORY_API_ENDPOINT not configured. Memory integration will be disabled.")

# System prompt template configuration
SYSTEM_PROMPT_TEMPLATE_PATH = Path(__file__).parent / "system_prompt.j2"


# Shared Azure credentials
shared_credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)

# Global chat client - will be initialized in main()
chat_client = None

# Global Redis client - will be initialized in main()
redis_client = None

# Global shutdown event for graceful shutdown
shutdown_event = asyncio.Event()

# Global Jinja2 environment for system prompt template
jinja_env = Environment(loader=FileSystemLoader(Path(__file__).parent))

async def fetch_user_memory(user_id: str) -> dict:
    """
    Fetch user memory from Memory API with timeout.
    Returns empty dict if API is unavailable or times out.
    """
    if not MEMORY_API_ENDPOINT:
        logger.debug("Memory API endpoint not configured, skipping memory fetch")
        return {}
    
    try:
        timeout = aiohttp.ClientTimeout(total=MEMORY_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = f"{MEMORY_API_ENDPOINT}/api/memory/users/{user_id}/memories"
            logger.debug(f"Fetching memory from: {url}")
            async with session.get(url) as response:
                if response.status == 200:
                    memory_data = await response.json()
                    logger.debug(f"Memory API response: {json.dumps(memory_data, indent=2, default=str)}")
                    logger.info(f"Successfully fetched memory for user {user_id}")
                    return memory_data
                elif response.status == 404:
                    logger.info(f"No memory found for user {user_id}")
                    return {}
                else:
                    logger.warning(f"Memory API returned status {response.status} for user {user_id}")
                    return {}
    except asyncio.TimeoutError:
        logger.warning(f"Memory API timeout ({MEMORY_API_TIMEOUT}s) for user {user_id}")
        return {}
    except Exception as e:
        logger.warning(f"Error fetching memory for user {user_id}: {e}")
        return {}

def generate_system_prompt(user_memory: dict = None) -> str:
    """
    Generate system prompt using Jinja2 template with user memory.
    """
    try:
        logger.debug(f"Generating system prompt with memory: {json.dumps(user_memory, indent=2, default=str) if user_memory else 'None'}")
        template = jinja_env.get_template("system_prompt.j2")
        rendered_prompt = template.render(user_memory=user_memory)
        logger.debug(f"Generated system prompt: {rendered_prompt}")
        return rendered_prompt
    except Exception as e:
        logger.error(f"Error generating system prompt: {e}")
        # Fallback to basic system prompt
        fallback_prompt = "You are a helpful assistant."
        logger.debug(f"Using fallback system prompt: {fallback_prompt}")
        return fallback_prompt

async def get_conversation_history(session_id: str, user_id: str) -> list:
    """
    Retrieve conversation history from Redis.
    Returns a list of messages in OpenAI format for the LLM.
    """
    try:
        redis_key = f"session:{session_id}"
        conversation_data = await redis_client.get(redis_key)
        
        if not conversation_data:
            logger.info(f"No conversation history found for session {session_id}")
            return []
        
        conversation = json.loads(conversation_data)
        
        # Validate session belongs to the user
        if conversation.get("userId") != user_id:
            logger.warning(f"Session {session_id} does not belong to user {user_id}")
            return []        
        # Convert stored messages to OpenAI format
        conversation_messages = []
        for msg in conversation.get("messages", []):
            message_dict = {
                "role": msg["role"],
                "content": msg["content"]
            }
            
            # Add tool_calls if present (for assistant messages)
            if "tool_calls" in msg and msg["tool_calls"]:
                message_dict["tool_calls"] = msg["tool_calls"]
            
            # Add tool_call_id if present (for tool messages)
            if "tool_call_id" in msg:
                message_dict["tool_call_id"] = msg["tool_call_id"]
            
            conversation_messages.append(message_dict)
        
        logger.info(f"Retrieved conversation history with {len(conversation_messages)} messages for session {session_id}")
        return conversation_messages
        
    except Exception as e:
        logger.error(f"Error retrieving conversation history for session {session_id}: {e}")
        return []

async def update_conversation_history(session_id: str, user_id: str, user_message: str, assistant_response: str, chat_message_id: str, system_message: str = None):
    """
    Update conversation history in Redis with new user message and assistant response.
    Creates a new conversation if it doesn't exist.
    """
    try:
        redis_key = f"session:{session_id}"
        current_time = datetime.now(timezone.utc).isoformat()
          # Get existing conversation or create new one
        conversation_data = await redis_client.get(redis_key)
        
        if conversation_data:
            conversation = json.loads(conversation_data)
            logger.info(f"Updating existing conversation for session {session_id}")
        else:
            conversation = {
                "sessionId": session_id,
                "userId": user_id,
                "createdAt": current_time,
                "title": None,  # Will be generated later by history worker
                "messages": []
            }
            logger.info(f"Creating new conversation for session {session_id}")
            
            # Add system message for new conversations
            if system_message:
                system_msg = {
                    "messageId": f"{chat_message_id}_system",
                    "role": "system",
                    "content": system_message,
                    "timestamp": current_time
                }
                conversation["messages"].append(system_msg)
        
        # Update last activity timestamp
        conversation["lastActivity"] = current_time
        
        # Add user message
        user_msg = {
            "messageId": f"{chat_message_id}_user",
            "role": "user",
            "content": user_message,
            "timestamp": current_time
        }
        conversation["messages"].append(user_msg)
        
        # Add assistant response
        assistant_msg = {
            "messageId": f"{chat_message_id}_assistant",
            "role": "assistant", 
            "content": assistant_response,
            "timestamp": current_time
        }
        conversation["messages"].append(assistant_msg)
        
        # Save back to Redis with 24-hour TTL
        await redis_client.setex(
            redis_key,
            24 * 60 * 60,  # 24 hours in seconds
            json.dumps(conversation)
        )
        
        logger.info(f"Updated conversation history for session {session_id} with {len(conversation['messages'])} total messages")
        
    except Exception as e:
        logger.error(f"Error updating conversation history for session {session_id}: {e}")
        # Don't raise the exception as this shouldn't stop message processing

async def publish_message_completed_event(sb_client: ServiceBusClient, session_id: str, user_id: str, chat_message_id: str):
    """
    Publish a message-completed event to notify other services that a conversation interaction is complete.
    This enables asynchronous processing like history persistence and memory extraction.
    """
    try:
        completed_payload = {
            "sessionId": session_id,
            "userId": user_id,
            "chatMessageId": chat_message_id,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "eventType": "message_completed"
        }
        
        completed_message = ServiceBusMessage(
            body=json.dumps(completed_payload),
            session_id=session_id,
            message_id=f"{chat_message_id}_completed"
        )
        
        async with sb_client.get_topic_sender(SERVICEBUS_MESSAGE_COMPLETED_TOPIC) as sender:
            await sender.send_messages(completed_message)
            logger.info(f"Published message-completed event for session {session_id}, chatMessageId {chat_message_id}")
            
    except Exception as e:
        logger.error(f"Error publishing message-completed event for session {session_id}, chatMessageId {chat_message_id}: {e}")
        # Don't raise the exception as this shouldn't stop the main processing flow

async def process_message(sb_client: ServiceBusClient, service_bus_message):
    """
    Handle a single user message: parse, call LLM, and stream tokens to the token streams topic.
    """
    try:
        message_body_str = str(service_bus_message)
        logger.info(f"Received message: {message_body_str}")
        message_data = json.loads(message_body_str)
        
        user_text = message_data.get("text")
        session_id = message_data.get("sessionId")
        chat_message_id = message_data.get("chatMessageId")
        user_id = message_data.get("userId")

        if not all([user_text, session_id, chat_message_id]):
            logger.error(f"Message missing required fields (text, sessionId, chatMessageId): {message_data}")
            # Depending on requirements, might dead-letter this message
            return
        
        # Set context variables so all spans in this operation will include these attributes
        set_context_attributes(
            user_id=user_id or "unknown",
            session_id=session_id,
            message_id=chat_message_id
        )
        
        # Start a new span for message processing - it will automatically get the context attributes
        with tracer.start_as_current_span("process_user_message") as span:
            # Add operation-specific attributes manually
            if span.is_recording():
                span.set_attribute("app.operation", "process_user_message")
                span.set_attribute("app.user_text_length", len(user_text))
            
            logger.info(f"Processing chatMessageId: {chat_message_id} for sessionId: {session_id}, userId: {user_id} - Text: '{user_text}'")
            
            # Get conversation history from Redis
            conversation_history = await get_conversation_history(session_id, user_id)
              # Build messages for LLM
            messages = []
            # Check if history already starts with a system message
            has_system_message_in_history = (
                conversation_history and 
                len(conversation_history) > 0 and 
                conversation_history[0].get("role") == "system"
            )
            
            if not has_system_message_in_history:
                # This is a new conversation, fetch user memory and generate system prompt
                logger.debug("New conversation detected, fetching user memory for system prompt")
                user_memory = await fetch_user_memory(user_id)
                logger.debug(f"Fetched user memory for user {user_id}: {json.dumps(user_memory, indent=2, default=str) if user_memory else 'Empty'}")
                system_message_content = generate_system_prompt(user_memory)
                
                # Add system message with memory context
                messages.append({"role": "system", "content": system_message_content})
                logger.debug("Added system message with user memory context")
            else:
                logger.debug("System message already present in conversation history")
                system_message_content = None  # Don't store system message for existing conversations
            
            # Add conversation history if available
            if conversation_history:
                messages.extend(conversation_history)
            
            # Add current user message
            messages.append({"role": "user", "content": user_text})
            logger.debug(f"Built messages for LLM: {len(messages)} total messages")
            # Debug: Log the complete messages array as JSON
            logger.debug(f"Messages sent to LLM: {json.dumps(messages, indent=2, default=str)}")
            
            logger.info(f"Calling LLM with {len(messages)} messages (including system message and history)")            # Call OpenAI API for chat completions with streaming and tools
            stream = await chat_client.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT_NAME,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},  # Include token usage in the stream
                tools=[conversation_search_tool],
                tool_choice="auto",
                temperature=0.7
            )        # Collect the full assistant response and handle function calls
        assistant_response_tokens = []
        function_calls = {} 
        usage_info = None
        
        async with sb_client.get_topic_sender(SERVICEBUS_TOKEN_STREAMS_TOPIC) as sender:
            async for chunk in stream:
                # Handle token usage information (final chunk)
                if chunk.usage:
                    usage_info = chunk.usage
                    logger.info(f"Token usage: input={chunk.usage.prompt_tokens}, output={chunk.usage.completion_tokens}, total={chunk.usage.total_tokens}")
                
                if chunk.choices and len(chunk.choices) > 0:
                    choice = chunk.choices[0]
                    delta = choice.delta
                    
                    # Handle regular content
                    if delta.content:
                        content_chunk = delta.content
                        assistant_response_tokens.append(content_chunk)
                        
                        token_payload = {
                            "sessionId": session_id,
                            "chatMessageId": chat_message_id,
                            "token": content_chunk
                        }
                        token_message = ServiceBusMessage(
                            body=json.dumps(token_payload),
                            session_id=session_id
                        )
                        await sender.send_messages(token_message)
                        logger.debug(f"Sent token chunk: {content_chunk}")
                      # Handle function calls (accumulate arguments for each tool call)
                    if delta.tool_calls:
                        for tool_call in delta.tool_calls:
                            logger.debug(f"Tool call delta: index={tool_call.index}, id={tool_call.id}, function={tool_call.function}")
                            
                            # Always use index as the primary key for tracking tool calls
                            tool_call_index = tool_call.index
                            
                            if tool_call_index not in function_calls:
                                function_calls[tool_call_index] = {
                                    "id": "",
                                    "index": tool_call_index,
                                    "name": "",
                                    "arguments": ""
                                }
                                logger.debug(f"Initialized function call at index {tool_call_index}")
                            
                            # Update ID if provided in this chunk
                            if tool_call.id:
                                function_calls[tool_call_index]["id"] = tool_call.id
                                logger.debug(f"Set ID for tool call {tool_call_index}: {tool_call.id}")
                            
                            if tool_call.function:
                                if tool_call.function.name:
                                    function_calls[tool_call_index]["name"] = tool_call.function.name
                                    logger.info(f"Function call detected at index {tool_call_index}: {tool_call.function.name}")
                                
                                if tool_call.function.arguments is not None:
                                    logger.debug(f"Adding arguments chunk: '{tool_call.function.arguments}' to call index {tool_call_index}")
                                    function_calls[tool_call_index]["arguments"] += tool_call.function.arguments
                                    logger.debug(f"Current accumulated arguments for index {tool_call_index}: '{function_calls[tool_call_index]['arguments']}'")
                  # Process function calls if any
            if function_calls:
                logger.info(f"Processing {len(function_calls)} function calls")
                logger.debug(f"Complete function calls state: {function_calls}")
                
                # Convert function_calls dict to list for processing
                function_calls_list = list(function_calls.values())
                  # Log each function call for debugging
                for i, func_call in enumerate(function_calls_list):
                    logger.debug(f"Function call {i}: name='{func_call['name']}', id='{func_call['id']}', args='{func_call['arguments'][:100]}{'...' if len(func_call['arguments']) > 100 else ''}'")
                
                # Post-process function calls to ensure proper IDs and validation
                for func_call in function_calls_list:
                    # If ID is empty, generate one based on index
                    if not func_call["id"] or func_call["id"].strip() == "":
                        func_call["id"] = f"call_index_{func_call['index']}"
                        logger.debug(f"Generated ID for tool call at index {func_call['index']}: {func_call['id']}")                # Create assistant message with tool calls first
                assistant_tool_calls = []
                for func_call in function_calls_list:
                    # Only include tool calls that have a valid function name
                    if not func_call["name"] or func_call["name"].strip() == "":
                        logger.warning(f"Skipping tool call with empty name: {func_call}")
                        continue
                        
                    # Create a tool call object for the assistant message
                    tool_call_dict = {
                        "id": func_call["id"],  # ID is guaranteed to be set by post-processing above
                        "type": "function",
                        "function": {
                            "name": func_call["name"],
                            "arguments": func_call["arguments"] or "{}"
                        }
                    }
                    assistant_tool_calls.append(tool_call_dict)
                    logger.debug(f"Added valid tool call: {func_call['name']} with ID {func_call['id']}")
                # Only proceed if we have valid tool calls
                if not assistant_tool_calls:
                    logger.warning("No valid tool calls found after filtering, skipping function call processing")
                else:
                    # Add assistant message with tool calls to conversation
                    assistant_message = {
                        "role": "assistant",
                        "content": "",  # Usually empty when there are tool calls
                        "tool_calls": assistant_tool_calls
                    }
                    messages.append(assistant_message)
                    logger.debug(f"Added assistant message with {len(assistant_tool_calls)} tool calls")                    # Execute function calls and add tool responses
                    for func_call in function_calls_list:
                        # Skip tool calls with empty names (IDs are guaranteed to be set by post-processing)
                        if not func_call["name"] or func_call["name"].strip() == "":
                            logger.warning(f"Skipping invalid tool call during execution: {func_call}")
                            continue                            
                        logger.debug(f"Processing function call: {func_call}")
                        if func_call["name"] == "search_conversation_history":
                            try:
                                logger.debug(f"Raw function arguments: '{func_call['arguments']}'")
                                
                                # Parse arguments with better error handling
                                if not func_call["arguments"] or func_call["arguments"].strip() == "":
                                    logger.warning(f"Empty arguments for search_conversation_history call: {func_call}")
                                    args = {}
                                else:
                                    try:
                                        args = json.loads(func_call["arguments"])
                                    except json.JSONDecodeError as parse_error:
                                        logger.error(f"Failed to parse JSON arguments '{func_call['arguments']}': {parse_error}")
                                        raise parse_error
                                
                                search_query = args.get("search_query", "")
                                limit = args.get("limit", 5)
                                
                                logger.info(f"Executing conversation search: query='{search_query}', limit={limit}")
                                
                                # Execute the search
                                search_result = await search_conversation_history(user_id, search_query, limit)
                                
                                # Add tool message to conversation
                                tool_message = {
                                    "role": "tool",
                                    "content": json.dumps(search_result, indent=2),
                                    "tool_call_id": func_call["id"]
                                }
                                messages.append(tool_message)
                                
                                logger.info(f"Function call result: Found {search_result.get('total_found', 0)} conversations")
                                
                            except json.JSONDecodeError as e:
                                logger.error(f"Error parsing function arguments '{func_call['arguments']}': {e}")
                                error_message = {
                                    "role": "tool",
                                    "content": json.dumps({"error": f"Invalid function arguments: {str(e)}"}),
                                    "tool_call_id": func_call["id"]
                                }
                                messages.append(error_message)
                            except Exception as e:
                                logger.error(f"Error executing function call: {e}")
                                error_message = {
                                    "role": "tool", 
                                    "content": json.dumps({"error": str(e)}),
                                    "tool_call_id": func_call["id"]
                                }
                                messages.append(error_message)
                    
                    # Make another LLM call with the function results
                    logger.info("Making follow-up LLM call with function results")
                    followup_stream = await chat_client.chat.completions.create(
                        model=AZURE_OPENAI_DEPLOYMENT_NAME,
                        messages=messages,
                        stream=True,
                        stream_options={"include_usage": True},
                        tools=[conversation_search_tool],
                        tool_choice="auto",
                        temperature=0.7
                    )                
                    # Process the follow-up response
                    async for chunk in followup_stream:
                        if chunk.usage:
                            usage_info = chunk.usage  # Update usage info with follow-up call
                            logger.info(f"Follow-up Token usage: input={chunk.usage.prompt_tokens}, output={chunk.usage.completion_tokens}, total={chunk.usage.total_tokens}")
                        
                        if chunk.choices and len(chunk.choices) > 0 and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            content_chunk = chunk.choices[0].delta.content
                            assistant_response_tokens.append(content_chunk)
                            
                            token_payload = {
                                "sessionId": session_id,
                                "chatMessageId": chat_message_id,
                                "token": content_chunk
                            }
                            token_message = ServiceBusMessage(
                                body=json.dumps(token_payload),
                                session_id=session_id
                            )
                            await sender.send_messages(token_message)
                            logger.debug(f"Sent follow-up token chunk: {content_chunk}")
            
            # Send end-of-stream sentinel
            eos_payload = {"sessionId": session_id, "chatMessageId": chat_message_id, "end_of_stream": True}
            eos_message = ServiceBusMessage(body=json.dumps(eos_payload), session_id=session_id)
            await sender.send_messages(eos_message)
            logger.info(f"Sent end-of-stream for chatMessageId {chat_message_id}")
        
        # Update conversation history in Redis with the complete interaction
        assistant_response = "".join(assistant_response_tokens)
        
        # Pass system message content only if this is a new conversation (no history)
        system_msg_to_store = system_message_content if not has_system_message_in_history else None
        
        await update_conversation_history(
            session_id, 
            user_id, 
            user_text, 
            assistant_response, 
            chat_message_id,
            system_msg_to_store
        )
          # Publish message-completed event for downstream processing (history, memory, etc.)
        await publish_message_completed_event(sb_client, session_id, user_id, chat_message_id)

    except json.JSONDecodeError as e:
        # Create an error span that will also get context attributes
        with tracer.start_as_current_span("process_message_json_error") as error_span:
            if error_span.is_recording():
                error_span.set_attribute("app.error", "json_decode_error")
                error_span.set_attribute("app.operation", "process_message")
        logger.error(f"Failed to decode JSON from user message: {message_body_str}, error: {e}")
        # Potentially dead-letter the message
    except Exception as e:
        # Create an error span that will also get context attributes
        with tracer.start_as_current_span("process_message_error") as error_span:
            if error_span.is_recording():
                error_span.set_attribute("app.error", "processing_error")
                error_span.set_attribute("app.operation", "process_message")
        logger.error(f"Error processing message (id: {service_bus_message.message_id if service_bus_message else 'N/A'}): {e}")
        # Potentially re-raise or handle to allow the message to be abandoned/dead-lettered by the caller
        raise

async def _process_and_handle_message(sb_client: ServiceBusClient, msg: ServiceBusMessage, receiver, semaphore: asyncio.Semaphore, logger_instance: logging.Logger):
    """
    Process a message, settle it, and release the concurrency semaphore.
    """
    try:
        # Process the message within a span that will get application attributes
        with tracer.start_as_current_span("handle_service_bus_message") as handle_span:
            if handle_span.is_recording():
                handle_span.set_attribute("app.message_id", msg.message_id)
                handle_span.set_attribute("app.operation", "handle_service_bus_message")
            
            # Process the message - this will set context variables and create child spans
            await process_message(sb_client, msg)
            
        await receiver.complete_message(msg) # Reverted to use receiver.complete_message()
        logger_instance.info(f"Successfully processed and completed message id: {msg.message_id}")
    except Exception as e:
        logger_instance.error(f"Unhandled exception during message processing for msg_id {msg.message_id}. Error: {e}. Abandoning message.")
        
        # Create an error span that will also get context attributes if they were set
        with tracer.start_as_current_span("handle_service_bus_message_error") as error_span:
            if error_span.is_recording():
                error_span.set_attribute("app.message_id", msg.message_id)
                error_span.set_attribute("app.operation", "handle_service_bus_message_error")
                error_span.set_attribute("app.error", str(e))
            
            try:
                await receiver.abandon_message(msg) # Reverted to use receiver.abandon_message()
            except Exception as abandon_e:
                logger_instance.error(f"Failed to abandon message {msg.message_id}. Error: {abandon_e}")
    finally:
        semaphore.release()

async def setup_signal_handlers():
    """
    Setup signal handlers for graceful shutdown using asyncio.
    """
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        logger.info("Received shutdown signal. Initiating graceful shutdown...")
        loop.call_soon_threadsafe(shutdown_event.set)
    
    # Add signal handlers for SIGTERM and SIGINT
    # On Windows, only SIGINT is supported, SIGTERM is not available
    signals_to_handle = [signal.SIGINT]
    if sys.platform != 'win32':
        signals_to_handle.append(signal.SIGTERM)
    
    for sig in signals_to_handle:
        try:
            loop.add_signal_handler(sig, signal_handler)
            logger.info(f"Added signal handler for {sig}")
        except (NotImplementedError, RuntimeError):
            # Fallback for platforms that don't support add_signal_handler
            logger.warning(f"Signal handler for {sig} not supported on this platform, using fallback")
            signal.signal(sig, lambda s, f: signal_handler())

async def wait_for_tasks_completion(active_tasks: set, timeout: int = 240):
    """
    Wait for all active tasks to complete within the given timeout.
    Args:
        active_tasks: Set of active asyncio tasks
        timeout: Maximum time to wait in seconds (default 4 minutes)
    """
    if not active_tasks:
        return
    
    logger.info(f"Waiting for {len(active_tasks)} active tasks to complete (timeout: {timeout}s)...")
    try:
        await asyncio.wait_for(
            asyncio.gather(*active_tasks, return_exceptions=True),
            timeout=timeout
        )
        logger.info("All active tasks completed successfully")
    except asyncio.TimeoutError:
        logger.warning(f"Timeout reached ({timeout}s). Some tasks may not have completed.")
        # Cancel remaining tasks
        for task in active_tasks:
            if not task.done():
                task.cancel()
        # Wait a bit for cancellation to take effect
        await asyncio.sleep(1)

async def search_conversation_history(user_id: str, search_query: str, limit: int = 5) -> dict:
    """
    Search previous conversations for a user using semantic search.
    Returns a structured response with conversation summaries and metadata.
    """
    if not MEMORY_API_ENDPOINT:
        logger.debug("Memory API endpoint not configured, skipping conversation history search")
        return {"conversations": [], "message": "Memory API not available"}
    
    # Handle empty search queries
    if not search_query or search_query.strip() == "":
        logger.warning("Empty search query provided to search_conversation_history")
        return {"conversations": [], "message": "Empty search query provided"}
    
    try:
        timeout = aiohttp.ClientTimeout(total=MEMORY_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = f"{MEMORY_API_ENDPOINT}/api/memory/users/{user_id}/conversations/search"
            payload = {
                "query": search_query.strip(),
                "limit": max(1, min(10, limit))  # Ensure limit is between 1 and 10
            }
            
            logger.debug(f"Searching conversation history: {url}, payload: {payload}")
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    conversations = await response.json()
                    logger.info(f"Found {len(conversations)} relevant conversations for user {user_id}")
                    
                    # Format the response for the LLM
                    formatted_conversations = []
                    for conv in conversations:
                        formatted_conv = {
                            "summary": conv["summary"],
                            "themes": conv["themes"],
                            "timestamp": conv["timestamp"],
                            "relevance_score": conv.get("relevance_score", 0.0),
                            "user_sentiment": conv.get("user_sentiment", "neutral"),
                            "persons_mentioned": conv.get("persons", []),
                            "places_mentioned": conv.get("places", [])
                        }
                        formatted_conversations.append(formatted_conv)
                    
                    return {
                        "conversations": formatted_conversations,
                        "total_found": len(conversations),
                        "search_query": search_query
                    }
                elif response.status == 404:
                    logger.info(f"No conversation history found for user {user_id}")
                    return {"conversations": [], "message": "No previous conversations found"}
                else:
                    logger.warning(f"Memory API returned status {response.status} for conversation search")
                    return {"conversations": [], "message": f"Search failed with status {response.status}"}
                    
    except asyncio.TimeoutError:
        logger.warning(f"Memory API timeout ({MEMORY_API_TIMEOUT}s) for conversation search")
        return {"conversations": [], "message": "Search timeout"}
    except Exception as e:
        logger.warning(f"Error searching conversation history for user {user_id}: {e}")
        return {"conversations": [], "message": f"Search error: {str(e)}"}

# Define the function tool for LLM (OpenAI format)
conversation_search_tool = {
    "type": "function",
    "function": {
        "name": "search_conversation_history",
        "description": """Search through the user's previous conversations using semantic search. This tool finds relevant past conversations based on topics, themes, or context rather than exact keyword matching.

Use this tool when:
- User references something they discussed before
- User asks about previous topics or conversations
- You need context from past interactions
- User wants to continue a previous discussion

The tool returns conversation summaries with:
- Brief summary of each conversation
- Main themes and topics discussed
- People and places mentioned
- User sentiment during the conversation
- Relevance score (how well it matches the search query)
- Timestamp of the conversation

Example response format:
{
  "conversations": [
    {
      "summary": "User discussed their vacation plans to Japan, asking about travel tips and cultural customs",
      "themes": ["travel", "Japan", "vacation planning", "cultural advice"],
      "timestamp": "2024-12-01T10:30:00Z",
      "relevance_score": 0.85,
      "user_sentiment": "excited",
      "persons_mentioned": ["Sakura (local guide)"],
      "places_mentioned": ["Tokyo", "Kyoto", "Mount Fuji"]
    }
  ],
  "total_found": 1,
  "search_query": "Japan travel plans"
}""",
        "parameters": {
            "type": "object",
            "properties": {
                "search_query": {
                    "type": "string",
                    "description": "Semantic search query describing what to look for in previous conversations. Use natural language describing topics, themes, or context rather than exact keywords. Examples: 'vacation planning', 'work stress discussion', 'family conversation', 'technical programming questions'"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of most relevant conversations to return (1-10). Use smaller numbers (1-3) for specific searches, larger numbers (5-10) for broader context gathering",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5
                }
            },
            "required": ["search_query"]
        }
    }
}

async def main():
    global chat_client, redis_client
    logger.info("Starting LLM worker...")
    logger.info(f"Service Bus Namespace: {SERVICEBUS_FULLY_QUALIFIED_NAMESPACE}")
    logger.info(f"Listening for user messages on Topic: '{SERVICEBUS_USER_MESSAGES_TOPIC}', Subscription: '{SERVICEBUS_USER_MESSAGES_SUBSCRIPTION}'")
    logger.info(f"Sending token streams to Topic: '{SERVICEBUS_TOKEN_STREAMS_TOPIC}'")
    logger.info(f"Sending completion events to Topic: '{SERVICEBUS_MESSAGE_COMPLETED_TOPIC}'")
    logger.info(f"Maximum concurrency for message processing: {MAX_CONCURRENCY}")
    logger.info(f"Redis Host: {REDIS_HOST}:{REDIS_PORT}, SSL: {REDIS_SSL}")
    
    # Setup signal handlers for graceful shutdown
    await setup_signal_handlers()
    
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
        
        # Test Redis connection
        await redis_client.ping()
        logger.info("Redis connection established successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize Redis client: {e}")
        raise      # Initialize the OpenAI Azure client
    async def get_azure_token():
        token = await shared_credential.get_token("https://cognitiveservices.azure.com/.default")
        return token.token
    
    chat_client = AsyncAzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_ad_token_provider=get_azure_token,
        api_version=AZURE_OPENAI_API_VERSION
    )
    
    credential = DefaultAzureCredential()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    active_tasks = set()
    
    try:
        while not shutdown_event.is_set():
            try:
                async with ServiceBusClient(fully_qualified_namespace=SERVICEBUS_FULLY_QUALIFIED_NAMESPACE, credential=credential) as sb_client:
                    async with sb_client.get_subscription_receiver(SERVICEBUS_USER_MESSAGES_TOPIC, SERVICEBUS_USER_MESSAGES_SUBSCRIPTION) as receiver:
                        logger.info("LLM Worker connected and listening for messages.")
                        
                        # Main message processing loop with timeout to allow periodic shutdown checks
                        while not shutdown_event.is_set():
                            try:
                                # Receive messages with a timeout to allow shutdown checks
                                received_messages = await asyncio.wait_for(
                                    receiver.receive_messages(max_message_count=1, max_wait_time=5),
                                    timeout=10
                                )
                                
                                if not received_messages:
                                    continue  # No messages received, check shutdown and retry
                                
                                for msg in received_messages:
                                    # Check for shutdown signal before processing new messages
                                    if shutdown_event.is_set():
                                        logger.info("Shutdown signal received. Stopping message processing.")
                                        # Abandon the current message so it can be processed by another worker
                                        await receiver.abandon_message(msg)
                                        break
                                    
                                    await semaphore.acquire() # Wait for an available slot
                                    task = asyncio.create_task(
                                        _process_and_handle_message(sb_client, msg, receiver, semaphore, logger)
                                    )
                                    active_tasks.add(task)
                                    # Remove task from set upon completion to prevent memory leak over long runs
                                    task.add_done_callback(active_tasks.discard)
                            except asyncio.TimeoutError:
                                # Timeout is expected, continue to check shutdown event
                                continue
            except Exception as e:
                if shutdown_event.is_set():
                    logger.info("Shutdown in progress, ignoring connection error.")
                    break
                logger.error(f"Exception in LLM worker main connection/receive loop: {e}. Retrying in 10 seconds...")
                # Clean up any completed tasks
                active_tasks = {task for task in active_tasks if not task.done()}
                await asyncio.sleep(10) # Wait before retrying connection    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Initiating graceful shutdown...")
        shutdown_event.set()
    
    finally:
        logger.info("Shutting down gracefully...")
        # Wait for all active tasks to complete with a 4-minute timeout
        await wait_for_tasks_completion(active_tasks, timeout=240)
        
        # Close the chat client to clean up aiohttp session
        if chat_client:
            try:
                await chat_client.close()
                logger.info("Chat client closed successfully")
            except Exception as e:
                logger.warning(f"Error closing chat client: {e}")
        
        # Close the Redis client
        if redis_client:
            try:
                await redis_client.aclose()
                logger.info("Redis client closed successfully")
            except Exception as e:
                logger.warning(f"Error closing Redis client: {e}")
        
        logger.info("LLM worker shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
