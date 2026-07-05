import os
import json
import logging
import numpy as np
import uvicorn
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.cosmos import CosmosClient, PartitionKey, exceptions
from azure.monitor.opentelemetry import configure_azure_monitor
from openai import AsyncAzureOpenAI
from opentelemetry import trace
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

# Load local .env when in development
load_dotenv()

# Read configuration from environment
COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT")
COSMOS_DATABASE_NAME = os.getenv("COSMOS_DATABASE_NAME", "memory")
COSMOS_CONVERSATIONS_CONTAINER_NAME = os.getenv("COSMOS_CONVERSATIONS_CONTAINER_NAME", "conversations")
COSMOS_USER_MEMORIES_CONTAINER_NAME = os.getenv("COSMOS_USER_MEMORIES_CONTAINER_NAME", "user-memories")

# Azure OpenAI endpoint for embeddings
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

if not COSMOS_ENDPOINT:
    raise RuntimeError("Missing required environment variable COSMOS_ENDPOINT")
if not AZURE_OPENAI_ENDPOINT:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_ENDPOINT")
if not AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME:
    raise RuntimeError("Missing required environment variable AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME")

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

# Configure OpenAI instrumentation
enable_content_recording = os.getenv("OTEL_INSTRUMENTATION_OPENAI_V2_RECORD_MESSAGE_CONTENT", "false").lower() == "true"
OpenAIInstrumentor().instrument(
    capture_content=enable_content_recording
)

tracer = trace.get_tracer(__name__)

# Initialize Azure credentials and clients
credential = DefaultAzureCredential()
cosmos_client = CosmosClient(COSMOS_ENDPOINT, credential)
database = cosmos_client.get_database_client(COSMOS_DATABASE_NAME)
conversations_container = database.get_container_client(COSMOS_CONVERSATIONS_CONTAINER_NAME)
user_memories_container = database.get_container_client(COSMOS_USER_MEMORIES_CONTAINER_NAME)

# Initialize OpenAI client with AAD authentication
token_provider = get_bearer_token_provider(
    credential, "https://cognitiveservices.azure.com/.default"
)

openai_client = AsyncAzureOpenAI(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    azure_ad_token_provider=token_provider,
    api_version=AZURE_OPENAI_API_VERSION,
)

# FastAPI app
app = FastAPI(
    title="Scalable Chat Memory API",
    version="0.1.0",
    description="API for managing conversation memories and user profiles in Cosmos DB",
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
class ConversationSummary(BaseModel):
    sessionId: str
    userId: str
    summary: str
    timestamp: datetime
    themes: List[str]
    persons: List[str]
    places: List[str]
    user_sentiment: str
    vector_embedding: Optional[List[float]] = None

class UserMemory(BaseModel):
    userId: str
    output_preferences: Optional[List[str]] = None
    personal_preferences: Optional[List[str]] = None
    assistant_preferences: Optional[List[str]] = None
    knowledge: Optional[List[str]] = None
    interests: Optional[List[str]] = None
    dislikes: Optional[List[str]] = None
    family_and_friends: Optional[List[str]] = None
    work_profile: Optional[List[str]] = None
    goals: Optional[List[str]] = None
    last_updated: Optional[datetime] = None

class MemorySearchRequest(BaseModel):
    query: str
    limit: Optional[int] = 10

class MemorySearchResult(BaseModel):
    sessionId: str
    summary: str
    timestamp: datetime
    themes: List[str]
    persons: List[str]
    places: List[str]
    user_sentiment: str
    relevance_score: Optional[float] = None

class ConversationMemoryUpdate(BaseModel):
    sessionId: str
    userId: str
    summary: str
    themes: List[str] = []
    persons: List[str] = []
    places: List[str] = []
    user_sentiment: str = "neutral"
    vector_embedding: Optional[List[float]] = None

class UserMemoryUpdate(BaseModel):
    userId: str
    updates: Dict[str, Any]

class McpRequest(BaseModel):
    """Minimal JSON-RPC request body for the MCP Streamable HTTP endpoint."""

    jsonrpc: str = "2.0"
    id: Optional[str | int] = None
    method: str
    params: Dict[str, Any] = {}

# Helper functions
async def generate_embedding(text: str) -> List[float]:
    """
    Generate text embedding using Azure OpenAI.
    
    Args:
        text: The text to generate embeddings for
        
    Returns:
        List[float]: Vector embedding for the text
        
    Raises:
        Exception: If embedding generation fails
    """
    try:
        response = await openai_client.embeddings.create(
            input=[text],
            model=AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT_NAME
        )
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"Failed to generate embedding: {e}")
        return []

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    
    vec1_np = np.array(vec1)
    vec2_np = np.array(vec2)
    
    norm1 = np.linalg.norm(vec1_np)
    norm2 = np.linalg.norm(vec2_np)
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return float(np.dot(vec1_np, vec2_np) / (norm1 * norm2))

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

async def get_user_memory_payload(user_id: str) -> Dict[str, Any]:
    """Return user memory as a JSON-serializable dictionary for REST and MCP callers."""
    memory = await get_user_memories(user_id)
    return memory.model_dump(mode="json")

async def search_conversation_history_payload(user_id: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Return conversation search results as JSON-serializable dictionaries."""
    results = await search_conversation_memories(user_id, MemorySearchRequest(query=query, limit=limit))
    return [result.model_dump(mode="json") for result in results]

# REST API Endpoints

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "memory-api"}

@app.get("/mcp")
async def mcp_metadata():
    """Return MCP endpoint metadata for discovery and health probes."""
    return {
        "service": "memory-api",
        "transport": "streamable-http-json-rpc",
        "protocolVersion": "2025-11-25",
        "tools": ["get_user_memory", "search_conversation_history"],
    }

@app.post("/mcp")
async def mcp_endpoint(rpc: McpRequest, request: Request):
    """Handle minimal MCP JSON-RPC initialize, tool, and resource requests."""
    validate_mcp_request_security(request)

    if rpc.method == "initialize":
        return mcp_result(rpc.id, {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "scalable-chat-memory-api", "version": "0.1.0"},
        })

    if rpc.method == "tools/list":
        return mcp_result(rpc.id, {
            "tools": [
                {
                    "name": "get_user_memory",
                    "description": "Retrieve structured long-term memory for a user.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"userId": {"type": "string"}},
                        "required": ["userId"],
                    },
                },
                {
                    "name": "search_conversation_history",
                    "description": "Search a user's remembered conversation summaries.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "userId": {"type": "string"},
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["userId", "query"],
                    },
                },
            ]
        })

    if rpc.method == "tools/call":
        tool_name = rpc.params.get("name")
        arguments = rpc.params.get("arguments", {})
        if tool_name == "get_user_memory":
            payload = await get_user_memory_payload(arguments["userId"])
        elif tool_name == "search_conversation_history":
            payload = await search_conversation_history_payload(
                arguments["userId"],
                arguments["query"],
                int(arguments.get("limit", 5)),
            )
        else:
            return mcp_error(rpc.id, -32601, f"Unknown tool: {tool_name}")
        return mcp_result(rpc.id, {"content": [{"type": "json", "json": payload}]})

    if rpc.method == "resources/list":
        return mcp_result(rpc.id, {
            "resources": [
                {
                    "uri": "memory://users/{userId}",
                    "name": "User memory",
                    "description": "Structured user memory resource template.",
                    "mimeType": "application/json",
                }
            ]
        })

    if rpc.method == "resources/read":
        uri = rpc.params.get("uri", "")
        prefix = "memory://users/"
        if not uri.startswith(prefix):
            return mcp_error(rpc.id, -32602, "Unsupported memory resource URI")
        payload = await get_user_memory_payload(uri.removeprefix(prefix))
        return mcp_result(rpc.id, {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload)}]})

    return mcp_error(rpc.id, -32601, f"Unsupported MCP method: {rpc.method}")

@app.get("/api/memory/users/{user_id}/memories", response_model=UserMemory)
async def get_user_memories(user_id: str):
    """
    Retrieve structured memories for a specific user.
    
    Args:
        user_id: The user ID to get memories for
        
    Returns:
        UserMemory: User's structured memories
        
    Raises:
        HTTPException: If retrieval fails
    """
    with tracer.start_as_current_span("get_user_memories"):
        # Add custom dimensions to current span for observability
        current_span = trace.get_current_span()
        if current_span.is_recording():
            current_span.set_attribute("app.user_id", user_id)
            current_span.set_attribute("app.operation", "get_user_memories")
        
        try:
            # Query user memories
            query = "SELECT * FROM c WHERE c.userId = @userId"
            items = list(user_memories_container.query_items(
                query=query,
                parameters=[{"name": "@userId", "value": user_id}],
                enable_cross_partition_query=True
            ))
            
            if not items:
                # Add result information to span
                if current_span.is_recording():
                    current_span.set_attribute("app.memories_found", False)
                # Return empty user memory structure
                return UserMemory(
                    userId=user_id,
                    last_updated=datetime.now(timezone.utc)
                )
            
            # Add result information to span
            if current_span.is_recording():
                current_span.set_attribute("app.memories_found", True)
            
            memory_data = items[0]
            return UserMemory(**memory_data)
            
        except exceptions.CosmosHttpResponseError as e:
            if current_span.is_recording():
                current_span.set_attribute("app.error", "cosmos_error")
            logger.error(f"Error retrieving user memories for {user_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to retrieve user memories")

@app.delete("/api/memory/users/{user_id}/memories")
async def delete_user_memories(user_id: str):
    """Delete all structured memories for a specific user."""
    with tracer.start_as_current_span("delete_user_memories"):
        try:
            # Query for existing user memories
            query = "SELECT * FROM c WHERE c.userId = @userId"
            items = list(user_memories_container.query_items(
                query=query,
                parameters=[{"name": "@userId", "value": user_id}],
                enable_cross_partition_query=True
            ))
            
            if not items:
                # No memories found for this user
                raise HTTPException(status_code=404, detail="No user memories found")
            
            # Delete the user memory document
            memory_doc = items[0]
            user_memories_container.delete_item(
                item=memory_doc["id"],
                partition_key=memory_doc["userId"]
            )
            
            logger.info(f"Deleted user memories for user {user_id}")
            return {"status": "success", "message": f"User memories deleted for user {user_id}"}
            
        except exceptions.CosmosResourceNotFoundError:
            raise HTTPException(status_code=404, detail="No user memories found")
        except exceptions.CosmosHttpResponseError as e:
            logger.error(f"Error deleting user memories for {user_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete user memories")

@app.post("/api/memory/users/{user_id}/conversations/search", response_model=List[MemorySearchResult])
async def search_conversation_memories(user_id: str, search_request: MemorySearchRequest):
    """
    Search conversational memories for a specific user.
    
    Args:
        user_id: The user ID to search memories for
        search_request: Search query and parameters
        
    Returns:
        List[MemorySearchResult]: Matching conversation memories
        
    Raises:
        HTTPException: If search fails
    """
    with tracer.start_as_current_span("search_conversation_memories"):
        # Add custom dimensions to current span for observability
        current_span = trace.get_current_span()
        if current_span.is_recording():
            current_span.set_attribute("app.user_id", user_id)
            current_span.set_attribute("app.operation", "search_conversation_memories")
            current_span.set_attribute("app.search_query", search_request.query)
            current_span.set_attribute("app.search_limit", search_request.limit)
        
        try:
            # Generate embedding for the search query
            query_embedding = await generate_embedding(search_request.query)
            
            if query_embedding:
                # Add embedding information to span
                if current_span.is_recording():
                    current_span.set_attribute("app.search_method", "vector_similarity")
                    
                # Use CosmosDB vector similarity search if embeddings are available
                query = """
                    SELECT c.sessionId, c.summary, c.timestamp, c.themes, c.persons, 
                           c.places, c.user_sentiment, c.vector_embedding,
                           VectorDistance(c.vector_embedding, @queryVector) AS distance
                    FROM c 
                    WHERE c.userId = @userId AND c.vector_embedding != null
                    ORDER BY VectorDistance(c.vector_embedding, @queryVector)
                    OFFSET 0 LIMIT @limit
                """
                
                items = list(conversations_container.query_items(
                    query=query,
                    parameters=[
                        {"name": "@userId", "value": user_id},
                        {"name": "@queryVector", "value": query_embedding},
                        {"name": "@limit", "value": search_request.limit}
                    ],
                    enable_cross_partition_query=True
                ))
            else:
                # Add fallback information to span
                if current_span.is_recording():
                    current_span.set_attribute("app.search_method", "text_based")
                    
                # Fallback to text-based search
                query = """
                    SELECT c.sessionId, c.summary, c.timestamp, c.themes, c.persons, 
                           c.places, c.user_sentiment, c.vector_embedding
                    FROM c 
                    WHERE c.userId = @userId AND (
                        CONTAINS(LOWER(c.summary), LOWER(@searchText)) OR
                        ARRAY_CONTAINS(c.themes, @searchText, true) OR
                        ARRAY_CONTAINS(c.persons, @searchText, true) OR
                        ARRAY_CONTAINS(c.places, @searchText, true)
                    )
                    ORDER BY c.timestamp DESC
                    OFFSET 0 LIMIT @limit
                """
                
                items = list(conversations_container.query_items(
                    query=query,
                    parameters=[
                        {"name": "@userId", "value": user_id},
                        {"name": "@searchText", "value": search_request.query},
                        {"name": "@limit", "value": search_request.limit}
                    ],
                    enable_cross_partition_query=True
                ))
            
            results = []
            for item in items:
                # Calculate relevance score
                relevance_score = 1.0
                if query_embedding and item.get("vector_embedding"):
                    # Convert distance to similarity score (1 - normalized distance)
                    distance = item.get("distance", 1.0)
                    relevance_score = max(0.0, 1.0 - distance)
                elif not query_embedding:
                    # Text-based relevance scoring
                    summary_text = item["summary"].lower()
                    query_text = search_request.query.lower()
                    if query_text in summary_text:
                        relevance_score = 0.8
                    else:
                        relevance_score = 0.5
                
                result = MemorySearchResult(
                    sessionId=item["sessionId"],
                    summary=item["summary"],
                    timestamp=datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")),
                    themes=item.get("themes", []),
                    persons=item.get("persons", []),
                    places=item.get("places", []),
                    user_sentiment=item.get("user_sentiment", "neutral"),
                    relevance_score=relevance_score
                )
                results.append(result)
            
            return results
            
        except exceptions.CosmosHttpResponseError as e:
            logger.error(f"Error searching conversation memories for {user_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to search conversation memories")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8008))
    uvicorn.run(app, host="0.0.0.0", port=port)