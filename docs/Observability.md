# Observability

The demo uses OpenTelemetry and Azure Monitor to show how an AI app can be observed across HTTP APIs, queues, Redis, Cosmos DB, Blob Storage, and model calls.

## What to trace

| Concern | Signal |
| --- | --- |
| User flow | `app.user_id`, `app.thread_id`, `app.run_id`, `app.chat_message_id` |
| API health | request count, latency, status code, exceptions |
| Queue behavior | Service Bus receive/send dependencies and worker concurrency |
| Streaming | run event sequence, reconnect position, terminal event |
| Model usage | model name, input tokens, output tokens, total tokens |
| Safety | `app.content_safety.input_verdict`, `app.content_safety.jailbreak` |
| Artifacts | artifact kind, artifact ID, Blob path, content hash |

## Instrumentation

Python services use the Azure Monitor OpenTelemetry distro:

```python
from azure.monitor.opentelemetry import configure_azure_monitor

configure_azure_monitor(
    enable_live_metrics=True,
    instrumentation_options={
        "azure_sdk": {"enabled": True},
        "fastapi": {"enabled": True},
    },
)
```

The LLM worker also enables OpenAI instrumentation and enriches spans with run context through a custom span processor.

## Useful KQL

### Run and token usage

```kusto
dependencies
| where customDimensions["gen_ai.usage.input_tokens"] != ""
| project
    timestamp,
    service = cloud_RoleName,
    runId = tostring(customDimensions["app.run_id"]),
    userId = tostring(customDimensions["app.user_id"]),
    model = tostring(customDimensions["gen_ai.response.model"]),
    inputTokens = toint(customDimensions["gen_ai.usage.input_tokens"]),
    outputTokens = toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize
    runs = dcount(runId),
    inputTokens = sum(inputTokens),
    outputTokens = sum(outputTokens)
  by bin(timestamp, 15m), model
| order by timestamp desc
```

### Blocked safety prompts

```kusto
traces
| where customDimensions["app.content_safety.input_verdict"] == "blocked"
| project
    timestamp,
    service = cloud_RoleName,
    runId = tostring(customDimensions["app.run_id"]),
    userId = tostring(customDimensions["app.user_id"]),
    jailbreak = tostring(customDimensions["app.content_safety.jailbreak"])
| order by timestamp desc
```

### Failed runs

```kusto
traces
| where customDimensions["app.run_id"] != ""
| where severityLevel >= 3 or customDimensions["app.error"] != ""
| project timestamp, service = cloud_RoleName, runId = tostring(customDimensions["app.run_id"]), message
| order by timestamp desc
```

## Operational checks

- Container Apps should show all API and worker revisions as running.
- Redis should contain `run:{runId}` and `run:{runId}:events` while runs are hot.
- Service Bus user-message and completed-message subscriptions should drain under normal load.
- Blob Storage public access must stay disabled; artifact reads go through the Front Service.
- `terraform plan -detailed-exitcode` should return `0` after deployment.
