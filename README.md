# Scalable AI Chat

This repository is a teaching demo for a scalable AI chat and agent runtime on Azure. It shows how to split a chat system into independently scalable parts: ingress, durable runs, event streaming, orchestration, memory, history, artifacts, observability, and infrastructure.

The demo uses Azure Container Apps, Service Bus, Redis Enterprise, Cosmos DB, Blob Storage, Microsoft Foundry with GPT-5.4-mini, managed identities, Terraform, and GitHub Actions.

## What this demonstrates

- **Durable runs:** every chat turn is a `run` with state, cancellation, ordered events, usage, safety verdicts, and artifact references.
- **Replayable streaming:** the browser receives typed SSE events from Redis Streams and can reconnect with `Last-Event-ID`.
- **Asynchronous orchestration:** Service Bus decouples user input from model execution and post-run processing.
- **Memory and history:** Cosmos DB stores conversation history and vector-searchable memory; APIs expose both REST and MCP-style tools/resources.
- **Rich artifacts:** controlled table/chart widgets and sandboxed micro-app artifacts are stored in private Blob Storage and rendered by the UI.
- **Cloud-native operations:** all Azure resources are created with Terraform and all container images are built by GitHub Actions with immutable SHA tags.

## Documentation

- [Architecture](./docs/Architecture.md) - why the system is split this way and how the pieces interact.
- [Contracts](./docs/Contracts.md) - run API, event taxonomy, artifact profile, MCP tools, and stored data shapes.
- [How to run](./docs/HowToRun.md) - local validation, image builds, Terraform deployment, and smoke tests.
- [Observability](./docs/Observability.md) - telemetry model and useful KQL queries.

## Repository layout

```text
src/
  front_service/   Public run, cancel, and artifact API
  sse_service/     Replayable Server-Sent Events from Redis Streams
  llm_worker/      Agent runtime, model calls, tools, safety, artifacts
  memory_api/      Memory REST API and MCP endpoint
  memory_worker/   Conversation-to-memory processing
  history_api/     Conversation history REST API and MCP endpoint
  history_worker/  Long-term conversation persistence
  web_client/      Browser demo UI
  agent_client/    CopilotKit/Next.js agent UI scaffold
terraform/         Azure infrastructure
docs/              Human docs and protocol contracts
```
