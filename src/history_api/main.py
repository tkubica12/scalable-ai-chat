import os
import json
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.identity import DefaultAzureCredential
from azure.cosmos import CosmosClient, PartitionKey, exceptions
import uvicorn
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

# Load local .env when in development
load_dotenv()

# Read configuration from environment
COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT")
COSMOS_DATABASE_NAME = os.getenv("COSMOS_DATABASE_NAME", "chat")
COSMOS_CONTAINER_NAME = os.getenv("COSMOS_CONTAINER_NAME", "conversations")

if not COSMOS_ENDPOINT:
    raise RuntimeError("Missing Cosmos DB configuration in environment variables")

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

# Initialize Azure credentials and clients
credential = DefaultAzureCredential()
cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential)
database = cosmos_client.get_database_client(COSMOS_DATABASE_NAME)
container = database.get_container_client(COSMOS_CONTAINER_NAME)

# FastAPI app
app = FastAPI(
    title="Scalable Chat History API",
    version="0.1.0",
    description="API for retrieving conversation history from Cosmos DB",
)

# Configure CORS
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
allowed_origins = [origin.strip() for origin in CORS_ORIGINS.split(",") if origin.strip()]
MCP_ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv("MCP_ALLOWED_ORIGINS", CORS_ORIGINS).split(",") if origin.strip()]
MCP_REQUIRE_AUTH = os.getenv("MCP_REQUIRE_AUTH", "false").lower() == "true"
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Pydantic models
class Message(BaseModel):
    messageId: str
    role: str 
    content: str
    timestamp: str  # ISO format string

class Conversation(BaseModel):
    sessionId: str
    userId: str
    title: Optional[str] = None
    lastActivity: str  # ISO format string
    messageCount: int

class ConversationDetail(BaseModel):
    sessionId: str
    userId: str
    title: Optional[str] = None
    messages: List[Message]

class UpdateTitleRequest(BaseModel):
    title: str

class McpRequest(BaseModel):
    """Minimal JSON-RPC request body for the MCP Streamable HTTP endpoint."""

    jsonrpc: str = "2.0"
    id: Optional[str | int] = None
    method: str
    params: Dict[str, Any] = {}

# Helper functions
def get_conversation_from_cosmos(session_id: str) -> Optional[Dict]:
    """Retrieve conversation from Cosmos DB"""
    try:
        item = container.read_item(item=session_id, partition_key=session_id)
        return item
    except exceptions.CosmosResourceNotFoundError:
        return None
    except Exception as e:
        logger.error(f"Failed to retrieve conversation {session_id} from Cosmos DB: {e}")
        return None

def get_user_conversations_from_cosmos(user_id: str, limit: int = 50) -> List[Dict]:
    """Get user's conversations from Cosmos DB"""
    try:
        query = """
        SELECT c.sessionId, c.userId, c.title, c.lastActivity, ARRAY_LENGTH(c.messages) as messageCount
        FROM c 
        WHERE c.userId = @userId 
        ORDER BY c.lastActivity DESC
        OFFSET 0 LIMIT @limit
        """
        items = list(container.query_items(
            query=query,
            parameters=[
                {"name": "@userId", "value": user_id},
                {"name": "@limit", "value": limit}
            ],
            enable_cross_partition_query=True
        ))
        return items
    except Exception as e:
        logger.error(f"Failed to get conversations for user {user_id} from Cosmos DB: {e}")
        return []

def update_conversation_title_in_cosmos(session_id: str, title: str) -> bool:
    """Update conversation title in Cosmos DB"""
    try:
        # Read existing item
        item = container.read_item(item=session_id, partition_key=session_id)
        item['title'] = title
        container.replace_item(item=session_id, body=item)
        return True
    except exceptions.CosmosResourceNotFoundError:
        return False
    except Exception as e:
        logger.error(f"Failed to update title for conversation {session_id}: {e}")
        return False

def validate_mcp_request_security(request: Request) -> None:
    """Validate MCP origin and optional auth requirements."""
    origin = request.headers.get("origin")
    if origin and "*" not in MCP_ALLOWED_ORIGINS and origin not in MCP_ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin is not allowed for MCP access")
    if MCP_REQUIRE_AUTH and not (request.headers.get("authorization") or request.headers.get("x-ms-client-principal")):
        raise HTTPException(status_code=401, detail="MCP access requires authenticated caller context")

def mcp_result(request_id: Optional[str | int], result: Dict[str, Any]) -> Dict[str, Any]:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}

def mcp_error(request_id: Optional[str | int], code: int, message: str) -> Dict[str, Any]:
    """Build a JSON-RPC error response."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

# API endpoints
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "history-api"}

@app.get("/mcp")
async def mcp_metadata():
    """Return MCP endpoint metadata for discovery and health probes."""
    return {
        "service": "history-api",
        "transport": "streamable-http-json-rpc",
        "protocolVersion": "2025-11-25",
        "tools": ["list_conversations", "get_conversation"],
    }

@app.post("/mcp")
async def mcp_endpoint(rpc: McpRequest, request: Request):
    """Handle minimal MCP JSON-RPC initialize, tool, and resource requests."""
    validate_mcp_request_security(request)

    if rpc.method == "initialize":
        return mcp_result(rpc.id, {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "scalable-chat-history-api", "version": "0.1.0"},
        })

    if rpc.method == "tools/list":
        return mcp_result(rpc.id, {
            "tools": [
                {
                    "name": "list_conversations",
                    "description": "List conversations for a user.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"userId": {"type": "string"}, "limit": {"type": "integer"}},
                        "required": ["userId"],
                    },
                },
                {
                    "name": "get_conversation",
                    "description": "Get one conversation with messages.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"userId": {"type": "string"}, "sessionId": {"type": "string"}},
                        "required": ["userId", "sessionId"],
                    },
                },
            ]
        })

    if rpc.method == "tools/call":
        tool_name = rpc.params.get("name")
        arguments = rpc.params.get("arguments", {})
        if tool_name == "list_conversations":
            conversations = await get_user_conversations(arguments["userId"], int(arguments.get("limit", 50)))
            payload = [conversation.model_dump(mode="json") for conversation in conversations]
        elif tool_name == "get_conversation":
            conversation = await get_conversation_messages(arguments["userId"], arguments["sessionId"])
            payload = conversation.model_dump(mode="json")
        else:
            return mcp_error(rpc.id, -32601, f"Unknown tool: {tool_name}")
        return mcp_result(rpc.id, {"content": [{"type": "json", "json": payload}]})

    if rpc.method == "resources/list":
        return mcp_result(rpc.id, {
            "resources": [
                {
                    "uri": "history://users/{userId}/conversations",
                    "name": "User conversations",
                    "description": "Conversation list resource template.",
                    "mimeType": "application/json",
                },
                {
                    "uri": "history://users/{userId}/sessions/{sessionId}",
                    "name": "Conversation detail",
                    "description": "Conversation detail resource template.",
                    "mimeType": "application/json",
                },
            ]
        })

    if rpc.method == "resources/read":
        uri = rpc.params.get("uri", "")
        if uri.startswith("history://users/") and uri.endswith("/conversations"):
            user_id = uri.removeprefix("history://users/").removesuffix("/conversations")
            conversations = await get_user_conversations(user_id)
            payload = [conversation.model_dump(mode="json") for conversation in conversations]
            return mcp_result(rpc.id, {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload)}]})

        marker = "/sessions/"
        if uri.startswith("history://users/") and marker in uri:
            user_part, session_id = uri.removeprefix("history://users/").split(marker, 1)
            conversation = await get_conversation_messages(user_part, session_id)
            return mcp_result(rpc.id, {"contents": [{"uri": uri, "mimeType": "application/json", "text": conversation.model_dump_json()}]})

        return mcp_error(rpc.id, -32602, "Unsupported history resource URI")

    return mcp_error(rpc.id, -32601, f"Unsupported MCP method: {rpc.method}")

@app.get("/conversations/{user_id}", response_model=List[Conversation])
async def get_user_conversations(user_id: str, limit: int = 50):
    """
    Get list of conversations for a user.
    
    Args:
        user_id: The user ID to get conversations for
        limit: Maximum number of conversations to return
        
    Returns:
        List[Conversation]: List of user's conversations
    """
    # Add custom dimensions to current span for observability
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("app.user_id", user_id)
        current_span.set_attribute("app.operation", "get_user_conversations")
        current_span.set_attribute("app.limit", limit)
    
    logger.info(f"Getting conversations for user: {user_id}")
    
    conversations = get_user_conversations_from_cosmos(user_id, limit)
    
    result = []
    for conv in conversations:
        result.append(Conversation(
            sessionId=conv.get('sessionId'),
            userId=conv.get('userId'),
            title=conv.get('title'),
            lastActivity=conv.get('lastActivity', ''),
            messageCount=conv.get('messageCount', 0)
        ))
    
    # Add result metrics to span
    if current_span.is_recording():
        current_span.set_attribute("app.conversations_found", len(result))
    
    logger.info(f"Found {len(result)} conversations for user {user_id}")
    return result

@app.get("/conversations/{user_id}/{session_id}/messages", response_model=ConversationDetail)
async def get_conversation_messages(user_id: str, session_id: str):
    """
    Get all messages for a specific conversation.
    
    Args:
        user_id: The user ID who owns the conversation
        session_id: The conversation session ID
        
    Returns:
        ConversationDetail: Conversation with all messages
        
    Raises:
        HTTPException: If conversation not found or access denied
    """
    # Add custom dimensions to current span for observability
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("app.user_id", user_id)
        current_span.set_attribute("app.session_id", session_id)
        current_span.set_attribute("app.operation", "get_conversation_messages")
    
    logger.info(f"Getting messages for conversation {session_id} (user: {user_id})")
    
    # Get conversation data from Cosmos DB
    conversation_data = get_conversation_from_cosmos(session_id)
    
    if not conversation_data:
        if current_span.is_recording():
            current_span.set_attribute("app.error", "conversation_not_found")
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Verify user owns this conversation
    if conversation_data.get('userId') != user_id:
        if current_span.is_recording():
            current_span.set_attribute("app.error", "access_denied")
        raise HTTPException(status_code=403, detail="Access denied")
        
    messages = []
    for msg in conversation_data.get('messages', []):
        messages.append(Message(
            messageId=msg.get('messageId'),
            role=msg.get('role'),
            content=msg.get('content'),
            timestamp=msg.get('timestamp', '')
        ))
    
    # Add result metrics to span
    if current_span.is_recording():
        current_span.set_attribute("app.messages_found", len(messages))
        current_span.set_attribute("app.conversation_title", conversation_data.get('title', ''))
    
    logger.info(f"Found {len(messages)} messages for conversation {session_id}")
    
    return ConversationDetail(
        sessionId=session_id,
        userId=user_id,
        title=conversation_data.get('title'),
        messages=messages
    )

@app.put("/conversations/{user_id}/{session_id}/title")
async def update_conversation_title(user_id: str, session_id: str, request: UpdateTitleRequest):
    """
    Update conversation title.
    
    Args:
        user_id: The user ID who owns the conversation
        session_id: The conversation session ID
        request: The new title for the conversation
        
    Returns:
        dict: Success confirmation
        
    Raises:
        HTTPException: If conversation not found, access denied, or update fails
    """
    # Add custom dimensions to current span for observability
    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("app.user_id", user_id)
        current_span.set_attribute("app.session_id", session_id)
        current_span.set_attribute("app.operation", "update_conversation_title")
        current_span.set_attribute("app.new_title_length", len(request.title))
    
    logger.info(f"Updating title for conversation {session_id} (user: {user_id}) to '{request.title}'")
    
    # Verify conversation exists and user owns it
    conversation_data = get_conversation_from_cosmos(session_id)
    
    if not conversation_data:
        if current_span.is_recording():
            current_span.set_attribute("app.error", "conversation_not_found")
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    if conversation_data.get('userId') != user_id:
        if current_span.is_recording():
            current_span.set_attribute("app.error", "access_denied")
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Update title in Cosmos DB
    success = update_conversation_title_in_cosmos(session_id, request.title)
    
    if not success:
        if current_span.is_recording():
            current_span.set_attribute("app.error", "update_failed")
        raise HTTPException(status_code=500, detail="Failed to update conversation title")
    
    # Add success information to span
    if current_span.is_recording():
        current_span.set_attribute("app.title_update_success", True)
    
    logger.info(f"Successfully updated title for conversation {session_id}")
    return {"success": True, "message": "Title updated successfully"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)
