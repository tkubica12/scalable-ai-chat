import os
import json
import logging
import numpy as np
import uvicorn
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.identity import DefaultAzureCredential
from azure.cosmos import CosmosClient, PartitionKey, exceptions
from azure.monitor.opentelemetry import configure_azure_monitor
from azure.ai.inference.aio import EmbeddingsClient
from opentelemetry import trace

# Load local .env when in development
load_dotenv()

# Read configuration from environment
COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT")
COSMOS_DATABASE_NAME = os.getenv("COSMOS_DATABASE_NAME", "memory")
COSMOS_CONVERSATIONS_CONTAINER_NAME = os.getenv("COSMOS_CONVERSATIONS_CONTAINER_NAME", "conversations")
COSMOS_USER_MEMORIES_CONTAINER_NAME = os.getenv("COSMOS_USER_MEMORIES_CONTAINER_NAME", "user-memories")

# Azure AI Inference endpoint for embeddings
AZURE_AI_EMBEDDINGS_ENDPOINT = os.getenv("AZURE_AI_EMBEDDINGS_ENDPOINT")

if not COSMOS_ENDPOINT:
    raise RuntimeError("Missing required environment variable COSMOS_ENDPOINT")
if not AZURE_AI_EMBEDDINGS_ENDPOINT:
    raise RuntimeError("Missing required environment variable AZURE_AI_EMBEDDINGS_ENDPOINT")

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
conversations_container = database.get_container_client(COSMOS_CONVERSATIONS_CONTAINER_NAME)
user_memories_container = database.get_container_client(COSMOS_USER_MEMORIES_CONTAINER_NAME)

# Initialize embeddings client with proper AAD authentication
embeddings_client = EmbeddingsClient(
    endpoint=AZURE_AI_EMBEDDINGS_ENDPOINT,
    credential=credential,
    api_version="2024-10-21",
    credential_scopes=["https://cognitiveservices.azure.com/.default"]
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

# Helper functions
async def generate_embedding(text: str) -> List[float]:
    """Generate text embedding using Azure AI Inference."""
    try:
        # Use the correct API - pass input as a list of strings
        response = await embeddings_client.embed(input=[text])
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

# REST API Endpoints

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "memory-api"}

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