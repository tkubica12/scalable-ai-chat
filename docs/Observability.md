# Observability and Monitoring

**Table of Contents**
- [Observability and Monitoring](#observability-and-monitoring)
  - [Overview](#overview)
  - [OpenTelemetry Implementation](#opentelemetry-implementation)
    - [Azure Monitor Integration](#azure-monitor-integration)
    - [Instrumentation Configuration](#instrumentation-configuration)
    - [Custom Span Processors](#custom-span-processors)
  - [Application-Specific Tracing](#application-specific-tracing)
    - [Custom Attributes](#custom-attributes)
    - [Context Propagation](#context-propagation)
    - [Service-Specific Implementation](#service-specific-implementation)
  - [AI-Specific Observability](#ai-specific-observability)
    - [OpenAI SDK Instrumentation](#openai-sdk-instrumentation)
    - [Token Usage Tracking](#token-usage-tracking)
    - [Model Performance Metrics](#model-performance-metrics)
  - [Monitoring Stack](#monitoring-stack)
    - [Azure Application Insights](#azure-application-insights)
    - [Azure AI Foundry](#azure-ai-foundry)
    - [Log Analytics Workspace](#log-analytics-workspace)
  - [Querying and Visualization](#querying-and-visualization)
    - [KQL Queries](#kql-queries)

## Overview

This scalable AI chat application implements comprehensive observability using **OpenTelemetry** as the instrumentation standard, **Azure Application Insights** for system-level monitoring and custom analytics, and **Azure AI Foundry** for AI-specific conversation tracking and content analysis.

The observability strategy focuses on three key areas:

1. **Application Performance Monitoring (APM)**: System-level metrics, request tracing, and performance analysis
2. **AI-Specific Observability**: Token usage, model performance, conversation flows, and content tracking
3. **Business Intelligence**: User behavior analysis, usage patterns, and cost optimization

**Key Benefits:**
- **Distributed Tracing**: End-to-end request tracking across all microservices
- **Custom Application Context**: Enriched telemetry with user, session, and message identifiers
- **AI Performance Insights**: Token consumption, model response times, and conversation quality metrics
- **Cost Optimization**: Detailed token usage analytics for budget management
- **Real-time Monitoring**: Live metrics and alerts for system health

**Major use-cases include:**
- Monitoring system performance and reliability
- Analyzing AI model performance and token usage
- Tracking user interactions and session flows
- Optimizing costs based on usage patterns
- Ensuring compliance with content safety standards
- Providing insights for FinOps by measuring token usage per user, region, model, session, message or any other custom dimension that might be added (company, ring, customer type, environment, etc.)

## OpenTelemetry Implementation

### Azure Monitor Integration

All services use the **Azure Monitor OpenTelemetry Distro** for Python, providing a "one-stop-shop" solution that automatically configures exporters, instrumentation, and telemetry collection:

```python
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

# Configure Azure Monitor with selective instrumentation
configure_azure_monitor(
    enable_live_metrics=True,
    instrumentation_options={
        "azure_sdk": {"enabled": True},      # Azure Service Bus, Cosmos DB, Redis
        "fastapi": {"enabled": True},        # HTTP request/response tracing
        "django": {"enabled": False},        
        "flask": {"enabled": False},         
        "psycopg2": {"enabled": False},      
        "requests": {"enabled": False},      
        "urllib": {"enabled": False},        
        "urllib3": {"enabled": False},       
    }
)

tracer = trace.get_tracer(__name__)
```

### Instrumentation Configuration

Each service is configured with specific instrumentation enabled based on its technology stack:

- **FastAPI Services** (Front Service, History API, Memory API, SSE Service): HTTP instrumentation enabled
- **Worker Services** (LLM Worker, History Worker, Memory Worker): Azure SDK instrumentation for Service Bus and storage
- **All Services**: Azure SDK instrumentation for Cosmos DB, Redis, and Service Bus interactions

### Custom Span Processors

The LLM Worker implements a custom `SpanProcessor` to automatically enrich all spans with application-specific context:

```python
class AppAttributesSpanProcessor(SpanProcessor):
    """
    Custom span processor that adds application-specific attributes to all spans.
    
    This processor automatically adds user_id, session_id, and message_id attributes
    to every span created by this application, pulling the values from context variables.
    """
    
    def on_start(self, span, parent_context=None):
        try:
            # Add context attributes from ContextVar
            user_id = current_user_id.get()
            if user_id:
                span.set_attribute("app.user_id", user_id)
            
            session_id = current_session_id.get()
            if session_id:
                span.set_attribute("app.session_id", session_id)
            
            message_id = current_message_id.get()
            if message_id:
                span.set_attribute("app.chat_message_id", message_id)
                
            # Service identifier for filtering
            span.set_attribute("app.name", "llm-worker")
            
        except Exception as e:
            logger.warning(f"Failed to add application attributes to span: {e}")
```

## Application-Specific Tracing

### Custom Attributes

All services enrich their telemetry with custom `app.*` attributes for better observability:

**Common Attributes:**
- `app.user_id`: User identifier for request correlation
- `app.session_id`: Chat session identifier
- `app.chat_message_id`: Unique message identifier
- `app.operation`: Operation type (e.g., "session_start", "chat_message")
- `app.name`: Service identifier for filtering

**Service-Specific Attributes:**
- `app.message_length`: Character count of user messages
- `app.conversation_length`: Number of messages in conversation
- `app.memory_fetch_duration`: Time spent retrieving user memories
- `app.function_call_count`: Number of function calls in LLM response

### Context Propagation

The system uses Python's `contextvars` for async-safe context propagation:

```python
from contextvars import ContextVar

# Context variables for storing application-specific information
current_user_id: ContextVar[str] = ContextVar('current_user_id', default=None)
current_session_id: ContextVar[str] = ContextVar('current_session_id', default=None)
current_message_id: ContextVar[str] = ContextVar('current_message_id', default=None)

def set_context_attributes(user_id: str = None, session_id: str = None, message_id: str = None):
    """Helper function to set context variables for tracing."""
    if user_id is not None:
        current_user_id.set(user_id)
    if session_id is not None:
        current_session_id.set(session_id)
    if message_id is not None:
        current_message_id.set(message_id)
```

### Service-Specific Implementation

**Front Service:**
```python
# Add custom dimensions to current span
current_span = trace.get_current_span()
if current_span.is_recording():
    current_span.set_attribute("app.user_id", chat_message.userId)
    current_span.set_attribute("app.session_id", chat_message.sessionId)
    current_span.set_attribute("app.chat_message_id", chat_message.chatMessageId)
    current_span.set_attribute("app.operation", "chat_message")
    current_span.set_attribute("app.message_length", len(chat_message.message))
```

**LLM Worker:**
```python
# Set context at the start of message processing
set_context_attributes(
    user_id=message_data.get("userId"),
    session_id=message_data.get("sessionId"),
    message_id=message_data.get("chatMessageId")
)

# All subsequent spans automatically inherit these attributes
```

## AI-Specific Observability

### OpenAI SDK Instrumentation

The application uses the OpenTelemetry OpenAI instrumentation to automatically capture AI-specific metrics:

```python
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

# Enable OpenTelemetry instrumentation for OpenAI SDK
OpenAIInstrumentor().instrument()

# Capture message content for debugging (be mindful of privacy)
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
```

### Token Usage Tracking

The OpenAI instrumentation automatically captures:
- `gen_ai.usage.input_tokens`: Input token count
- `gen_ai.usage.output_tokens`: Output token count  
- `gen_ai.response.model`: Model used for the response
- `gen_ai.request.model`: Model requested
- `gen_ai.request.temperature`: Temperature setting
- `gen_ai.system.message`: System prompt content (when content capture is enabled)

### Model Performance Metrics

Additional AI-specific metrics are captured through custom attributes:
- Response latency from LLM calls
- Function calling frequency and performance
- Memory retrieval impact on response quality
- Streaming token delivery rates

## Monitoring Stack

### Azure Application Insights

**Primary use cases:**
- **Performance Monitoring**: Request/response times, error rates, dependency calls
- **Custom Analytics**: KQL queries for business intelligence and cost analysis
- **Alerting**: Proactive monitoring with custom alert rules
- **Live Metrics**: Real-time application health monitoring

**Key Tables:**
- `requests`: HTTP requests to FastAPI services
- `dependencies`: External service calls (Azure OpenAI, Cosmos DB, Redis, Service Bus)
- `traces`: Distributed trace spans with custom attributes
- `customEvents`: Application-specific events and metrics

### Azure AI Foundry

**Primary use cases:**
- **Conversation Analysis**: Full conversation context and content review
- **Content Monitoring**: Message content analysis and safety monitoring  
- **Model Comparison**: A/B testing different models and configurations
- **User Experience**: Understanding conversation flows and user satisfaction

**Advantages for AI workloads:**
- Native understanding of AI conversation patterns
- Built-in content safety and compliance features
- Model performance benchmarking tools
- Integration with Azure AI services ecosystem

### Log Analytics Workspace

Centralized log aggregation and analysis platform that powers both Application Insights and custom analytics:

```terraform
resource "azurerm_log_analytics_workspace" "main" {
  name                = "logs-${local.base_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_application_insights" "main" {
  name                = "ai-${local.base_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.main.id
}
```

## Querying and Visualization

### KQL Queries

The application includes pre-built KQL queries for common analytics scenarios:

**Token Usage by Application:**
```kusto
dependencies
| where customDimensions["gen_ai.usage.input_tokens"] != "" and customDimensions["gen_ai.usage.output_tokens"] != ""
| project
    timestamp,
    cloud_RoleName,
    cloud_RoleInstance,
    userId = tostring(customDimensions["app.user_id"]),
    sessionId = tostring(customDimensions["app.session_id"]),
    inputTokens = toint(customDimensions["gen_ai.usage.input_tokens"]),
    outputTokens = toint(customDimensions["gen_ai.usage.output_tokens"]),
    model = tostring(customDimensions["gen_ai.response.model"])
| summarize totalInputTokens = sum(inputTokens), totalOutputTokens = sum(outputTokens) by cloud_RoleName
```

**Token Usage Over Time:**
```kusto
dependencies
| where customDimensions["gen_ai.usage.input_tokens"] != "" and customDimensions["gen_ai.usage.output_tokens"] != ""
| project
    timestamp,
    cloud_RoleName,
    inputTokens = toint(customDimensions["gen_ai.usage.input_tokens"]),
    outputTokens = toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize totalInputTokens = sum(inputTokens), totalOutputTokens = sum(outputTokens) by bin(timestamp, 1m), cloud_RoleName
| render timechart
```

**Model Performance Analysis:**
```kusto
dependencies
| where customDimensions["gen_ai.usage.input_tokens"] != "" and customDimensions["gen_ai.usage.output_tokens"] != ""
| project
    timestamp,
    model = tostring(customDimensions["gen_ai.response.model"]),
    inputTokens = toint(customDimensions["gen_ai.usage.input_tokens"]),
    outputTokens = toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize totalInputTokens = sum(inputTokens), totalOutputTokens = sum(outputTokens) by bin(timestamp, 5m), model
| render timechart
```


