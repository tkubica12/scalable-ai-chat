# Refactor plan: modern scalable AI chat and agent UI architecture

This repository should stay focused on its original teaching goal: showing how a large-scale AI chat/agent system can be decomposed so ingestion, orchestration, streaming, memory, history, artifacts, observability, and infrastructure can scale independently. The current implementation already demonstrates that pattern well with Azure Container Apps, Azure Service Bus, Redis, Cosmos DB, Azure OpenAI/Foundry, managed identities, Terraform, and GitHub Actions.

The part that is now dated is not the separation of concerns. It is the public protocol and rich-output model. The current browser protocol is a bespoke `{"token": "..."}` SSE stream with a `__END__` sentinel, no event IDs, no resume, no explicit cancellation, no durable run resource, no typed tool/artifact events, and no standard MCP/AG-UI/A2UI/MCP Apps surface. The refactor should modernize the wire contract and add rich artifacts without collapsing the architecture back into a monolith.

## Strategic direction

Build a modern **agent runtime demo** with these principles:

1. **Keep Service Bus for durable asynchronous work**, not for last-mile token delivery. Service Bus remains the backbone for `user-messages` and post-run fan-out such as history, memory, analytics, and external notifications.
2. **Add a durable `run` abstraction**. A chat turn becomes a run with `queued | running | completed | failed | cancelled` state, event sequence numbers, timestamps, usage, safety verdicts, and artifact references. Streaming is only a live view over durable run state.
3. **Adopt an AG-UI-shaped event contract for agent-to-UI streaming**. Use AG-UI lifecycle, text, tool-call, state, and custom event concepts as the public event taxonomy. Implement a strict repo-local profile first; avoid betting the whole repo on one young SDK.
4. **Use Streamable-HTTP-style browser streaming, implemented as POST/GET plus SSE event streams**, as the default browser transport: typed JSON events, `id:` fields, `Last-Event-ID` replay, heartbeats, cancellation, and a polling fallback. "Streamable HTTP" is a protocol pattern, not a replacement for SSE; the current MCP Streamable HTTP spec itself uses HTTP POST/GET and may return `text/event-stream` for streaming server messages.
5. **Use MCP Streamable HTTP for agent-to-tool integration**, not for the browser stream. Memory/history/artifact capabilities should become real MCP tools/resources while keeping REST compatibility for the web client during migration.
6. **Do not reinvent the agent frontend if a current framework covers the need.** Migrate the demo UI from the current hand-written Svelte client to a CopilotKit-based React/Next.js frontend unless implementation-time validation proves a mature Svelte adapter exists. CopilotKit is currently the most aligned option because it is built around AG-UI, has prebuilt chat/sidebar components, supports static generative UI, documents A2UI rendering, and has MCP Apps middleware.
7. **Add GenUI through CopilotKit-backed tiers**:
   - **Tier 1: static/frontend-tool UI** via CopilotKit actions/tools and prebuilt React components.
   - **Tier 2: A2UI-style declarative widgets** rendered through CopilotKit/A2UI renderer or a small approved component catalog if the renderer is insufficient.
   - **Tier 3: MCP Apps-style sandboxed micro-apps** through CopilotKit MCP Apps middleware for richer HTML/JavaScript artifacts in isolated iframes.
8. **Run on Azure with scalability-first platform components**: Azure Container Apps, Service Bus, Redis Streams, Cosmos vector search, Blob artifacts, new Microsoft Foundry resource/projects, managed identities, Terraform, GitHub Actions, and OpenTelemetry. Front Door, WAF, and APIM are optional production hardening patterns, not core demo work.

## Current architecture context

Current components:

| Area | Current implementation | Main limitation to address |
|---|---|---|
| Browser ingress | `front_service` accepts `/api/session/start` and `/api/chat`, queues messages to Service Bus. | No durable run API; session/message IDs are client-visible but not enough for resumable execution. |
| Streaming | `sse_service` exposes `GET /api/stream/{sessionId}/{chatMessageId}`, reads token messages from Service Bus sessions, emits raw SSE `data:` frames. | Bespoke event shape, no `id:`, no `Last-Event-ID`, no heartbeat, no explicit cancellation, possible loss after Service Bus message completion, session-lock contention. |
| Orchestration | `llm_worker` consumes `user-messages`, calls Azure OpenAI streaming, manually accumulates tool calls, writes tokens to `token-streams`, updates Redis, emits `message-completed`. | Monolithic worker, manual tool loop, tool progress not visible to UI, no durable event log or cancellation. |
| Memory | `memory_api` and `memory_worker` use Cosmos DB vector search and Azure OpenAI embeddings. Docs call it "Memory API/MCP", but it is REST only. | No actual MCP server/resources/tools. |
| History | `history_worker` persists completed conversations; `history_api` reads Cosmos. | Duplicated schema/read logic; docs and code disagree in places about Redis-vs-Cosmos read path. |
| Artifacts/GenUI | None. | No CopilotKit/AG-UI frontend, A2UI rendering, MCP Apps embedding, code interpreter outputs, artifact storage, CSP, or signed delivery. |
| Azure infra | ACA, Service Bus, Redis Enterprise, Cosmos vector search, legacy hub-based Foundry resources, Storage, Log Analytics/App Insights. | Uses classic Foundry hub/project resources; SDK versions are broad/old; no artifact container; workflows only build/push `latest`. |

## Architecture decisions

### ADR-001: Use Streamable-HTTP-style browser streaming, with SSE as the event stream encoding

**Decision:** Use the same architectural shape as modern Streamable HTTP protocols: HTTP `POST` starts or advances work, HTTP `GET` can attach to a server-to-client event stream, and the event stream is encoded as standards-compliant SSE (`text/event-stream`) with typed JSON events, `id:` fields, and `Last-Event-ID` replay.

**Why:** "Streamable HTTP" and "SSE" are not mutually exclusive. MCP Streamable HTTP is the best current reference point: it defines one HTTP endpoint that accepts client messages via `POST`; when a server response needs to stream, it returns `Content-Type: text/event-stream`; clients can also `GET` to open an SSE stream; resumability uses SSE event IDs plus `Last-Event-ID`. Therefore the plan should not say "SSE versus Streamable HTTP" as if they were separate transport choices. The real change is to stop using a proprietary one-off SSE stream and implement the modern Streamable-HTTP pattern with SSE as the browser-compatible streaming representation.

**Implementation profile for this repo:**

- `POST /api/runs` starts a durable run and returns `runId`.
- `GET /api/runs/{runId}/events` attaches to the run event stream.
- `POST /api/runs/{runId}/input` can later support human-in-the-loop input, artifact actions, or resumed interaction.
- `POST /api/runs/{runId}/cancel` explicitly cancels; closing the stream never means cancellation.
- Events use `id: <sequence>`, `event: <type>`, `data: <json>`.

**Not selected:** raw WebSockets as the default. WebSockets are useful for bidirectional collaboration, live steering, and low-latency client-to-server events, but they add reconnect, auth, and connection-management complexity that is not required for normal token streaming.

**Not selected:** WebRTC for this refactor. WebRTC belongs in a future voice/realtime-media module, not a text-first scalable chat demo.

**Optional future transport:** Azure Web PubSub/WebSockets can be added later for bidirectional collaboration, live steering, or voice-adjacent control paths. It should carry the same typed events, not introduce a second event schema.

### ADR-002: Introduce a durable run resource

**Decision:** Add a `runId` for every chat turn and store run metadata plus an ordered event log. Use Redis Streams for hot replay/tailing and Cosmos DB for durable run summaries/final events.

**Why:** Modern agent systems increasingly separate "execution" from "stream". The user may refresh, disconnect, cancel, or poll later. A durable run object enables reconnect, replay, cancellation, post-run processing, and testability.

**Not selected:** continuing to treat the SSE connection as the source of truth. It cannot survive browser disconnects, container restarts, or last-mile delivery races.

### ADR-003: Replace proprietary token frames with an AG-UI-compatible profile

**Decision:** Define `docs/protocol/agent-ui-events.schema.json` and Python/JS helpers for an AG-UI-inspired event profile. Minimum events:

| Category | Events |
|---|---|
| Lifecycle | `RunStarted`, `RunFinished`, `RunError`, `RunCancelled` |
| Text | `TextMessageStart`, `TextMessageContent`, `TextMessageEnd` |
| Tool | `ToolCallStart`, `ToolCallArgs`, `ToolCallEnd`, `ToolCallResult` |
| State | `StateSnapshot`, `StateDelta` |
| Artifact | `ArtifactCreated`, `ArtifactDelta`, `ArtifactFinalized` |
| Control | `Heartbeat`, `Usage`, `SafetyVerdict` |

**Why:** AG-UI is state of the art for agent-to-user event semantics, but still younger than MCP. A strict subset gives interoperability and clarity without forcing a full SDK dependency.

**Not selected:** full immediate AG-UI SDK migration. Do this only after the repo-local event contract is stable and validated.

### ADR-004: Use MCP Streamable HTTP for tools and resources

**Decision:** Expose memory/history/artifact capabilities as MCP Streamable HTTP tools/resources in addition to existing REST APIs during migration.

**Why:** MCP is the current standard for agent-to-tool/data connectivity. The current "Memory API/MCP" label is misleading because the service is REST only. A real MCP endpoint lets the worker stop hard-coding bespoke REST tool calls and prepares the demo for external MCP clients.

**Not selected:** inventing a repo-specific tool RPC protocol.

### ADR-005: Use CopilotKit as the frontend stack unless implementation-time validation proves it insufficient

**Decision:** Replace the hand-written Svelte chat client with a CopilotKit-based React/Next.js frontend, unless a mature first-party CopilotKit Svelte integration is verified before implementation. Use CopilotKit's prebuilt chat/sidebar/headless primitives instead of rebuilding event reducers, chat UI, tool rendering, thread persistence, and GenUI orchestration from scratch.

**Why:** CopilotKit is currently the strongest fit for the frontend layer:

- It is built around AG-UI as the agent-to-frontend event/state protocol.
- It provides out-of-the-box chat surfaces (`CopilotChat`, `CopilotSidebar`, `CopilotPopup`) and headless primitives.
- It supports static generative UI through frontend tools/actions.
- It documents A2UI rendering via an A2UI renderer package/pattern.
- It documents MCP Apps support through `@ag-ui/mcp-apps-middleware`, allowing MCP tools that return `ui://` resources to render inside the app.
- It avoids spending the refactor budget on a custom frontend framework.

**Caveat:** CopilotKit's MCP Apps and A2UI support is newer than its core AG-UI/chat support. The coding agent must validate package maturity, examples, and API names during implementation. Package versions observed during planning: `@copilotkit/react-core`/`react-ui`/`runtime` 1.62.2, `@ag-ui/core`/`client` 0.0.57, `@ag-ui/mcp-apps-middleware` 1.54.1, and `@copilotkit/a2ui-renderer` 0.0.3.

**Not selected:** continuing to enhance the current Svelte UI manually. That would require building the AG-UI event reducer, chat widgets, A2UI renderer, iframe bridge, tool lifecycle UI, and persistence integration ourselves.

### ADR-006: Use A2UI-style declarative widgets for safe GenUI and MCP Apps-style iframes for rich apps

**Decision:** Implement three rich-output levels through the frontend stack:

1. Static/tool-rendered UI with prebuilt React components.
2. A2UI-style JSON component/data messages rendered by a trusted renderer/catalog.
3. MCP Apps-style sandboxed HTML resources for complex interactive micro-apps.

**Why:** A2UI is safer for common UI because the model emits data, not executable code. MCP Apps are better for rich one-off apps where HTML/JS is needed, as long as iframe sandboxing, CSP, origin checks, and signed asset delivery are enforced. CopilotKit can orchestrate all three through AG-UI instead of forcing the repo to build all frontend plumbing itself.

**Not selected:** only PNG/static outputs from code interpreter. Static images are still useful for durable files and exports, but they should be treated as one artifact type, not the primary "modern UI" approach.

### ADR-007: Keep Azure Container Apps and Terraform as the deployment foundation

**Decision:** Continue using Azure Container Apps with HTTP/KEDA scaling, Terraform, managed identities, Service Bus, Redis, Cosmos DB, Storage, and GitHub Actions.

**Why:** This is aligned with the repo purpose and Azure guidance. Add missing production-grade pieces as optional modules rather than replacing the core stack.

### ADR-008: Migrate from legacy hub-based Foundry to new Microsoft Foundry resource and projects

**Decision:** Remove the current `Microsoft.MachineLearningServices/workspaces` hub and project resources from the primary Terraform path. Use the new Microsoft Foundry model where the `Microsoft.CognitiveServices/accounts` resource with `kind = "AIServices"` is the Foundry resource and Foundry projects are child resources under it. Use the latest AzureRM resources if available in the chosen provider version; otherwise use AzAPI against the current `Microsoft.CognitiveServices/accounts/projects` API. Do not keep the hub/project pattern except as a documented legacy fallback.

**Why:** The current `terraform/ai.foundry.tf` creates a classic AI hub and hub-based project. Microsoft guidance now describes new Foundry projects as child resources under the Foundry resource, with access to the Foundry API, unified project endpoint, agents, evaluations, models, indexes, data, observability, security, and trust. The plan must explicitly migrate because new generative AI/model-centric capabilities are only available through the Foundry resource and Foundry projects.

**Implementation requirements:**

- Keep or create the `AIServices` Foundry resource.
- Create a new Foundry project under the Foundry resource.
- Recreate connections at the Foundry resource/project level as needed.
- Update code to use the Foundry project endpoint through `azure-ai-projects` `AIProjectClient` for project/agent/evaluation configuration.
- Use the OpenAI-compatible client for model calls where appropriate, with Microsoft Entra authentication.
- Preserve existing model deployments where possible; if deployments must be recreated, document cutover and rollback.

### ADR-009: Upgrade SDKs and observability instrumentation before feature work

**Decision:** Make SDK/instrumentation modernization an explicit early phase. Use the unified OpenAI Python SDK with Microsoft Entra authentication; use Foundry SDK clients for project/agent/evaluation setup; update Azure Monitor/OpenTelemetry packages and GenAI instrumentation.

**Current repo gap:** Python services declare broad or older constraints such as `openai>=1.50.0`, `azure-identity>=1.20.0`, `azure-monitor-opentelemetry>=1.6.7`, and `opentelemetry-instrumentation-openai-v2>=0.54b1`. Current package checks during planning showed `openai` 2.44.0, `azure-identity` 1.25.3, `azure-monitor-opentelemetry` 1.8.9, `azure-ai-projects` 2.3.0, and `opentelemetry-instrumentation-openai-v2` 1.62.2 available. The coding agent must verify exact latest compatible versions at implementation time and update `uv.lock` files.

**Authentication requirement:** No API keys for Azure OpenAI/Foundry calls. Use `DefaultAzureCredential`/managed identity and `get_bearer_token_provider(..., "https://cognitiveservices.azure.com/.default")` with the OpenAI-compatible client.

## Target architecture

```text
Browser / client
  |
  | HTTPS
  v
Web client (CopilotKit React/Next.js app, hosted in ACA or static hosting)
  |
  +--> Agent Gateway / Front Service (ACA public ingress for demo)
  |      - POST /api/runs
  |      - GET  /api/runs/{runId}
  |      - POST /api/runs/{runId}/cancel
  |      - queues durable work to Service Bus
  |
  +--> Stream Service (ACA)
         - GET /api/runs/{runId}/events
         - SSE with id:, event:, data:
         - replays from Redis Streams using Last-Event-ID

Service Bus
  |
  +--> user-messages topic
  |      v
  |   LLM Worker / Agent Runtime (ACA, KEDA)
  |      - reads conversation state
  |      - calls Microsoft Foundry / Azure OpenAI with Entra auth
  |      - calls MCP tools/resources
  |      - writes typed events to Redis Streams
  |      - persists final conversation/run summary
  |
  +--> message-completed / run-completed topic
         +--> History Worker
         +--> Memory Worker
         +--> Artifact post-processors

Hot state:
  Redis Enterprise
    - session:{sessionId}
    - run:{runId}
    - run:{runId}:events as Redis Stream with TTL/maxlen
    - optional semantic cache backend if a later gateway/cache phase is enabled

Durable state:
  Cosmos DB
    - history conversations
    - memory summaries + vector embeddings
    - run summaries / final events / artifact manifests

Artifacts:
  Blob Storage private artifacts container
  Artifact API mints user-delegation SAS URLs
  Optional CDN/Front Door can cache immutable public-safe bundles in production

Tools:
  Memory API keeps REST and adds /mcp Streamable HTTP
  History API keeps REST and adds /mcp resources
  Artifact API exposes tools/resources for artifact manifests

Cross-cutting:
  Managed identity + RBAC, Key Vault, Azure Monitor OpenTelemetry,
  Content Safety, Foundry evaluations, SDK version hygiene, optional APIM/Front Door production hardening.
```

## Public protocol shape

### Create a run

`POST /api/runs`

Request:

```json
{
  "threadId": "session-or-thread-id",
  "userId": "user_001",
  "input": {
    "messages": [
      { "role": "user", "content": "Show me a chart of monthly sales" }
    ],
    "attachments": []
  },
  "capabilities": {
    "text": true,
    "toolEvents": true,
    "declarativeArtifacts": true,
    "sandboxedApps": true
  }
}
```

Response:

```json
{
  "runId": "run_...",
  "threadId": "session-or-thread-id",
  "status": "queued",
  "eventsUrl": "/api/runs/run_.../events"
}
```

### Stream events

`GET /api/runs/{runId}/events`

SSE frame example:

```text
id: 42
event: TextMessageContent
data: {"type":"TextMessageContent","runId":"run_...","threadId":"...","messageId":"msg_...","delta":"hello","sequence":42,"timestamp":"2026-07-04T08:00:00Z"}

```

Rules:

- Every event has `type`, `runId`, `threadId`, `sequence`, and `timestamp`.
- `sequence` is monotonic per run.
- `id:` equals `sequence`.
- Client reconnects with `Last-Event-ID`.
- Server replays events after the requested sequence from Redis Streams; if the hot stream expired, it returns the persisted run summary and a typed `RunError` or `RunFinished` terminal event as appropriate.
- Heartbeat events are emitted while the run is active and no other event has been sent recently.

### Cancel a run

`POST /api/runs/{runId}/cancel`

Rules:

- Cancellation is explicit. Closing the SSE stream is not cancellation.
- `llm_worker` must observe cancellation before starting a model request, between streamed chunks, before tool calls, and before artifact generation.
- Cancellation emits `RunCancelled` and settles Service Bus messages correctly.

## Implementation plan for the coding agent

Each phase must leave the repo in a working state. Keep REST compatibility unless the phase explicitly removes it. Add tests alongside new behavior because the repo currently has almost no automated validation beyond container builds.

### Phase 0: Baseline cleanup and safety rails

**Goal:** Make the current system testable before changing behavior.

Tasks:

1. Add a minimal `docs/protocol/README.md` describing current protocol gaps and the new target event profile.
2. Add `.gitignore` entries for Terraform state if missing and plan removal of committed `terraform.tfstate*` from source control in a separate commit.
3. Update CI workflows to tag container images with immutable tags in addition to `latest`: `${{ github.sha }}` and optionally semver tags.
4. Add a lightweight validation workflow or document the exact local validation commands:
   - Python services: `uv run python -m compileall .`
   - Web client: `npm ci && npm run build`
   - Terraform: `terraform fmt -check` and `terraform validate`
5. Verify no workflow uses long-lived Azure secrets for Azure deployment. Prefer GitHub OIDC when Azure deployment workflows are added.

Testable outcome:

- Existing services still build.
- `git grep terraform.tfstate` only finds ignored/untracked local state after cleanup.
- Workflow output shows immutable GHCR tags in addition to `latest`.

### Phase 0.5: SDK, authentication, instrumentation, and Foundry platform modernization

**Goal:** Modernize the platform foundation before adding protocol and UI features.

Tasks:

1. Update Python dependencies across services and regenerate `uv.lock` files:
   - `openai` to the latest compatible 2.x line (2.44.0 observed during planning).
   - `azure-identity` to the latest compatible line (1.25.3 observed).
   - `azure-monitor-opentelemetry` to the latest compatible line (1.8.9 observed).
   - `opentelemetry-instrumentation-openai-v2` to the latest compatible line (1.62.2 observed).
   - add `azure-ai-projects` latest compatible line (2.3.0 observed) where Foundry project/agent/evaluation operations are needed.
2. Standardize Azure OpenAI/Foundry model calls on the unified OpenAI SDK using Microsoft Entra authentication:
   - no Azure OpenAI keys,
   - `DefaultAzureCredential`/managed identity,
   - bearer token provider for `https://cognitiveservices.azure.com/.default`.
3. Use `AIProjectClient(endpoint=<foundry_project_endpoint>, credential=DefaultAzureCredential())` for Foundry project, agent, and evaluation configuration.
4. Update OpenTelemetry setup to the current Azure Monitor distro and latest GenAI/OpenAI instrumentation package. Keep custom `app.user_id`, `app.session_id`, `app.run_id`, and `app.chat_message_id` attributes, but align model/token attributes with current GenAI semantic conventions.
5. Replace classic hub-based Foundry Terraform:
   - keep/create `Microsoft.CognitiveServices/accounts` with `kind = "AIServices"` as the Foundry resource,
   - remove primary use of `Microsoft.MachineLearningServices/workspaces` `kind = "Hub"` and `kind = "Project"`,
   - create Foundry project as a child of the Foundry resource using current AzureRM resources if available, otherwise AzAPI and the current `Microsoft.CognitiveServices/accounts/projects` API,
   - recreate Foundry connections at account/project level as needed.
6. Keep existing model deployments if possible. If resource recreation is required, document migration and rollback.

Testable outcome:

- All Python services compile and start with upgraded dependencies.
- No service requires an Azure OpenAI API key.
- A smoke test calls the deployed model through the OpenAI-compatible SDK with Entra auth.
- A smoke test creates or reads the Foundry project through `AIProjectClient`.
- Terraform no longer creates legacy hub/project resources in the primary path.
- OpenTelemetry traces still include model name, token usage, user/session/run IDs, and errors.

### Phase 1: Shared run and event schemas

**Goal:** Define stable contracts before touching runtime behavior.

Tasks:

1. Create `docs/protocol/agent-ui-events.schema.json`.
2. Create Python Pydantic models for run/event envelopes in a shared package or copied minimal module used by `front_service`, `sse_service`, and `llm_worker`.
3. Create TypeScript/JSDoc or JSON-schema-derived helpers in `src/web_client/src/lib/protocol`.
4. Define event types listed in ADR-003.
5. Add golden JSON examples under `docs/protocol/examples/`.
6. Add tests that validate golden events against the schema.

Testable outcome:

- A schema validation command passes for all golden examples.
- Python event serialization round-trips without `as any`-style shortcuts or manual JSON string escaping.
- The web client can parse all golden examples.

### Phase 2: Durable run API in `front_service`

**Goal:** Replace "send chat message and separately stream by chatMessageId" with a durable run creation API while preserving old endpoints.

Tasks:

1. Add `POST /api/runs` to `front_service`.
2. Generate `runId` server-side.
3. Store initial run metadata in Redis and enqueue a Service Bus `user-message` payload containing `runId`, `threadId`, `userId`, input messages, capabilities, and correlation IDs.
4. Keep `/api/session/start` and `/api/chat` as compatibility wrappers that internally create runs.
5. Add `GET /api/runs/{runId}` returning current status and summary.
6. Add `POST /api/runs/{runId}/cancel` that marks cancellation intent in Redis.

Testable outcome:

- Calling `POST /api/runs` returns `runId` and `eventsUrl`.
- Existing web client flow still works through compatibility endpoints.
- A queued run can be retrieved via `GET /api/runs/{runId}`.
- A cancellation request changes run state to `cancelling` or `cancelled`.

### Phase 3: Redis Streams event log and modern Stream Service

**Goal:** Move last-mile streaming from Service Bus sessions to replayable Redis Streams.

Tasks:

1. Add Redis Stream helpers:
   - `append_run_event(runId, event)`
   - `read_run_events(runId, afterSequence, blockMs)`
   - TTL/maxlen policy for hot streams.
2. Update `sse_service` into a more generic `stream_service` while preserving the old image/name until Terraform is migrated.
3. Implement `GET /api/runs/{runId}/events`.
4. Emit standards-compliant SSE:
   - `id: <sequence>`
   - `event: <type>`
   - `data: <json>`
   - heartbeat comments or `Heartbeat` events.
5. Support `Last-Event-ID`.
6. Do not complete or drop an event until it is in Redis Stream. The stream endpoint reads from Redis, not from Service Bus.
7. Keep `/api/stream/{sessionId}/{chatMessageId}` as a compatibility adapter that resolves old IDs to a run if possible, or clearly deprecates it.

Testable outcome:

- A stream can disconnect after event N and reconnect with `Last-Event-ID: N`, receiving N+1 onward.
- Token content containing quotes, newlines, Unicode, and control characters is valid JSON because it is produced with `json.dumps`.
- No Service Bus `token-streams` dependency is required for the new endpoint.
- Load test with the existing Locust client or an updated run-event client shows streams complete without lost final events.

### Phase 4: LLM worker emits typed events and observes cancellation

**Goal:** Convert the worker from token-message publisher to event producer.

Tasks:

1. Update Service Bus input payload handling to require `runId`.
2. On message receive, emit `RunStarted`.
3. Before model call, emit `TextMessageStart`.
4. For each model text delta, emit `TextMessageContent`.
5. When the assistant message is complete, emit `TextMessageEnd`.
6. Emit `Usage` when token usage is available.
7. Emit `ToolCallStart`, `ToolCallArgs`, `ToolCallEnd`, and `ToolCallResult` around conversation search/tool calls.
8. Emit `RunFinished`, `RunError`, or `RunCancelled`.
9. Check cancellation state:
   - before starting model call,
   - inside stream loop,
   - before executing tools,
   - before artifact/code interpreter work.
10. Persist the final assistant response and event summary to Redis/Cosmos as appropriate.
11. Keep old `token-streams` publishing behind a compatibility flag only if needed for old client support.

Testable outcome:

- A single chat turn produces a complete ordered event sequence:
  `RunStarted -> TextMessageStart -> TextMessageContent* -> TextMessageEnd -> Usage? -> RunFinished`.
- A tool-using turn includes tool events visible in the stream.
- Cancelling a long run stops further text deltas and emits `RunCancelled`.
- Existing memory/history workers still receive completion events.

### Phase 5: Frontend migration to CopilotKit

**Goal:** Stop reinventing agent frontend infrastructure. Move from the current hand-written Svelte client to a CopilotKit-based React/Next.js frontend unless implementation-time validation finds a mature Svelte-compatible CopilotKit path.

Tasks:

1. Create a new React/Next.js web client under `src/web_client` or a parallel `src/agent_client` during migration.
2. Add CopilotKit packages:
   - `@copilotkit/react-core`
   - `@copilotkit/react-ui`
   - `@copilotkit/runtime`
   - AG-UI packages required by the selected integration
   - `@ag-ui/mcp-apps-middleware` when MCP Apps are enabled
   - `@copilotkit/a2ui-renderer` or current successor if A2UI rendering is used.
3. Use CopilotKit prebuilt chat/sidebar components first; only go headless for surfaces the demo specifically needs.
4. Connect CopilotKit to this repo's Agent Gateway/runtime. Prefer native AG-UI compatibility over a custom event adapter. If an adapter is required, keep it thin and documented.
5. Preserve existing demo capabilities: user selection, history, memory panel, new chat/session/thread behavior.
6. Add cancellation, tool lifecycle display, run status, usage display, and error rendering through CopilotKit-supported primitives.
7. Add frontend tests using golden AG-UI/run event fixtures where custom adapters remain.
8. Keep the old Svelte client only as a temporary compatibility UI until CopilotKit parity is reached, then remove it.

Testable outcome:

- The CopilotKit frontend can complete a text chat run through the new run/event API.
- Tool lifecycle events are visible without custom chat rendering.
- Cancelling stops the run and updates UI state.
- History and memory panels remain available.
- `npm run build` succeeds for the new frontend.
- Any retained custom event adapter is covered by golden event fixture tests.

### Phase 6: Real MCP endpoint for memory and history

**Goal:** Replace the misleading "Memory API/MCP" aspiration with an actual MCP Streamable HTTP implementation.

Tasks:

1. Add `/mcp` endpoint to `memory_api` using the Python MCP SDK if feasible.
2. Expose tools/resources:
   - `get_user_memory(userId)`
   - `search_conversation_history(userId, query, limit)`
   - resource template for user memory summaries.
3. Add `/mcp` endpoint or resource layer to `history_api`:
   - `list_conversations(userId)`
   - `get_conversation(sessionId)`
4. Keep REST endpoints for the web client and migration safety.
5. Update `llm_worker` tool execution to call the MCP client path behind a feature flag, then make MCP the default after tests pass.
6. Implement MCP security basics:
   - Origin validation.
   - Entra-authenticated access when deployed.
   - No unauthenticated remote tool access.

Testable outcome:

- MCP inspector can initialize against `/mcp` and list tools/resources.
- `llm_worker` can execute memory search through MCP and produce the same answer as the previous REST path.
- REST compatibility tests still pass.

### Phase 7: GenUI with CopilotKit, A2UI-style declarative widgets, and frontend tools

**Goal:** Add safe rich UI while using CopilotKit primitives before custom renderers.

Tasks:

1. Implement static/tool-rendered UI first with CopilotKit frontend actions/tools and approved React components for common cards, tables, charts, forms, and progress views.
2. Define `docs/protocol/artifacts/a2ui-profile.schema.json` for a small A2UI-inspired profile only where static tool rendering is not flexible enough:
   - `createSurface`
   - `updateComponents`
   - `updateDataModel`
   - `deleteSurface`
3. Prefer CopilotKit's A2UI renderer if it is mature enough; otherwise implement the smallest approved renderer/catalog in React:
   - `TextBlock`
   - `Card`
   - `Table`
   - `Chart`
   - `Form`
   - `StatusTimeline`
4. Add `ArtifactCreated` and `ArtifactDelta` event support.
5. Teach `llm_worker` to emit a declarative artifact through a controlled tool, not by directly trusting arbitrary model JSON.
6. Validate all artifact JSON against the schema before streaming it to the browser.
7. Add fallback rendering for invalid artifacts: show a text explanation plus validation error telemetry, not a broken UI.

Testable outcome:

- A prompt such as "show a table and chart of example sales data" produces a streamed text answer plus a validated chart/table artifact.
- Invalid artifact JSON is rejected and logged without executing code or breaking the chat.
- Artifact rendering tests pass from golden event fixtures.

### Phase 8: Artifact storage and Artifact API

**Goal:** Add durable, scalable artifact storage and signed delivery.

Tasks:

1. Extend Terraform storage with a private `artifacts` Blob container.
2. Add `artifact_api` or extend an existing API with:
   - create artifact manifest,
   - upload finalized artifact,
   - mint short-lived user-delegation SAS URL,
   - retrieve artifact metadata by `artifactId`.
3. Store artifact manifests in Cosmos DB with:
   - `artifactId`,
   - `runId`,
   - `threadId`,
   - `userId`,
   - `type`,
   - `version`,
   - `contentHash`,
   - `blobPath`,
   - `createdAt`,
   - `csp`,
   - `provenance`.
4. Use immutable, content-addressed or version-suffixed paths.
5. Emit `ArtifactFinalized` with manifest reference and signed URL only when needed by the browser.

Testable outcome:

- A generated artifact persists after the run is complete.
- Browser can fetch it through a short-lived signed URL.
- Expired URLs fail closed.
- Blob public access remains disabled.

### Phase 9: Code interpreter and static file artifacts

**Goal:** Support modern data-science artifacts without relying on deprecated Azure OpenAI Assistants API Code Interpreter.

Tasks:

1. Choose one Azure sandbox path:
   - Preferred managed path: Microsoft Foundry Agent Service Code Interpreter.
   - Preferred infra-controlled path: Azure Container Apps dynamic sessions code-interpreter pool.
2. Add Terraform and service code for the selected sandbox.
3. Add a controlled tool such as `generate_chart_artifact(dataset, chartSpec)`.
4. Persist PNG/CSV/HTML outputs immediately to Blob Storage through Artifact API.
5. Emit both a text explanation and `ArtifactFinalized` events.
6. Add size/type limits and malware/content checks where applicable.

Testable outcome:

- A prompt can produce a PNG or CSV artifact from generated sample data.
- The sandbox output is not treated as durable until persisted to Blob.
- Tool execution errors emit `ToolCallResult` with error details and `RunError` only when the run cannot continue.

### Phase 10: Tier 2 MCP Apps-style sandboxed micro-apps

**Goal:** Add rich interactive HTML/JS artifacts safely.

Tasks:

1. Add an iframe artifact host component in the CopilotKit/React frontend.
2. Enforce iframe sandbox attributes. Do not allow broad `allow-same-origin` unless a specific artifact requires it and the origin is isolated.
3. Implement a postMessage JSON-RPC bridge modeled on MCP Apps:
   - host-to-app `ui/initialize`,
   - host-to-app `ui/notifications/tool-result`,
   - app-to-host controlled action messages.
4. Generate CSP from artifact manifest metadata:
   - `connectDomains`,
   - `resourceDomains`,
   - `frameDomains`.
5. Serve app bundles from Blob Storage using immutable paths. Add CDN/Front Door only if a later production-hardening phase explicitly enables it.
6. Add one pilot micro-app, such as an interactive chart dashboard or mini kanban board.

Testable outcome:

- The pilot micro-app renders in a sandboxed iframe.
- postMessage origin checks reject messages from wrong origins.
- CSP blocks unexpected network calls.
- Updating the micro-app creates a new versioned URI instead of mutating a cached URI.

### Phase 11: Safety, evaluations, and optional production hardening

**Goal:** Emphasize AI scalability and quality controls without making network perimeter services mandatory.

Tasks:

1. Add Azure AI Content Safety checks:
   - prompt shields / jailbreak detection on input,
   - harm category checks,
   - output safety/protected material checks as appropriate.
2. Add Foundry evaluation dataset and a CI or scheduled evaluation job.
3. Emit safety and evaluation metadata into OpenTelemetry.
4. Document APIM AI Gateway as optional production hardening, not a core demo phase:
   - token limits,
   - quotas,
   - backend load balancing/circuit breaker,
   - optional semantic cache backed by Redis.
5. Document Azure Front Door + WAF as optional production edge hardening, not a core demo phase.

Testable outcome:

- A jailbreak test prompt is flagged with `app.content_safety.*` span attributes.
- Evaluation job fails when a golden test score drops below threshold.
- Optional APIM/Front Door modules, if implemented, do not change the run/event protocol and can be enabled without application code rewrites.

### Phase 12: Optional Web PubSub/WebSocket module

**Goal:** Demonstrate when WebSockets are useful without replacing the default SSE path.

Tasks:

1. Add an optional Terraform module for Azure Web PubSub.
2. Add a feature flag to route run events to Web PubSub groups keyed by `runId` or `threadId`.
3. Add browser support for Web PubSub as an alternative transport.
4. Demonstrate bidirectional features:
   - live steering,
   - collaborative artifact edits,
   - typing/interrupt events.

Testable outcome:

- SSE path remains default and passing.
- Web PubSub path can stream the same typed events.
- A bidirectional client event changes a running artifact or sends an interrupt.

## Data model changes

### Run document

```json
{
  "id": "run_...",
  "runId": "run_...",
  "threadId": "thread_...",
  "sessionId": "legacy-session-id",
  "userId": "user_001",
  "status": "completed",
  "createdAt": "2026-07-04T08:00:00Z",
  "startedAt": "2026-07-04T08:00:01Z",
  "completedAt": "2026-07-04T08:00:12Z",
  "lastSequence": 84,
  "inputSummary": "...",
  "outputSummary": "...",
  "usage": {
    "inputTokens": 123,
    "outputTokens": 456,
    "totalTokens": 579
  },
  "artifacts": ["artifact_..."],
  "safety": {
    "inputVerdict": "allowed",
    "outputVerdict": "allowed"
  }
}
```

### Artifact manifest

```json
{
  "id": "artifact_...",
  "artifactId": "artifact_...",
  "runId": "run_...",
  "threadId": "thread_...",
  "userId": "user_001",
  "kind": "declarative-widget",
  "mimeType": "application/vnd.scalable-ai-chat.a2ui+json",
  "version": "v1",
  "contentHash": "sha256-...",
  "blobPath": "artifacts/user_001/run_.../artifact_.../v1/widget.json",
  "createdAt": "2026-07-04T08:00:00Z",
  "csp": {
    "connectDomains": [],
    "resourceDomains": [],
    "frameDomains": []
  },
  "provenance": {
    "model": "gpt-4.1-mini",
    "tool": "generate_widget_artifact"
  }
}
```

## Testing strategy

Add tests where the current repo lacks coverage. Do not rely only on manual browser testing.

| Layer | Required tests |
|---|---|
| Protocol schemas | Validate all golden event/artifact examples. |
| Stream service | Reconnect from `Last-Event-ID`, heartbeat, terminal events, malformed run IDs, expired streams. |
| LLM worker | Event ordering, cancellation, tool-call event emission, error event emission, no manual JSON escaping. |
| Web client | CopilotKit integration smoke tests, cancellation UI, artifact rendering fallbacks, and golden event fixtures only for custom adapters. |
| MCP endpoints | Initialize/list/call tools through MCP inspector or automated MCP client tests. |
| Artifacts | Manifest validation, signed URL expiry, CSP/origin checks, iframe bridge rejection cases. |
| Terraform | `terraform fmt -check`, `terraform validate`, targeted `terraform plan`. |
| Load | Updated Locust test for `POST /api/runs` + streaming events; reconnect simulation. |

## Migration compatibility rules

1. Keep old REST endpoints until the new run/event API has web-client and test-client coverage.
2. Keep old Service Bus topics during transition, but make `token-streams` optional/deprecated once Redis Streams power the new stream endpoint.
3. Keep REST memory/history APIs after MCP is added; the web UI should not need to speak MCP directly.
4. Use feature flags:
   - `ENABLE_RUN_API`
   - `ENABLE_REDIS_EVENT_STREAM`
   - `ENABLE_MCP_TOOLS`
   - `ENABLE_DECLARATIVE_ARTIFACTS`
   - `ENABLE_SANDBOXED_APPS`
   - `ENABLE_OPTIONAL_AI_GATEWAY`
5. Remove compatibility only in a final cleanup phase after tests and docs are updated.

## Documentation updates required

Update these files as the implementation progresses:

- `README.md`: new architecture summary and demo capabilities.
- `docs/Architecture.md`: replace bespoke SSE diagrams with run/event architecture; include protocol decision matrix.
- `docs/HowToRun.md`: run API, local development, artifact storage, feature flags.
- `docs/Observability.md`: run/event telemetry, SDK/instrumentation versions, safety verdicts, Foundry evaluation telemetry, artifact telemetry.
- Service READMEs: new endpoints, environment variables, SDK versions, and validation commands.
- Terraform docs: new Foundry resource/project migration, Content Safety, artifacts container, dynamic sessions if selected, and optional Front Door/APIM modules only if implemented.

## Key references used for this plan

- AG-UI overview and event model: https://docs.ag-ui.com/introduction and https://docs.ag-ui.com/concepts/events
- MCP transports, including Streamable HTTP replacing HTTP+SSE: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- CopilotKit docs and GenUI/MCP Apps material: https://docs.copilotkit.ai/ and https://www.copilotkit.ai/blog/bring-mcp-apps-into-your-own-app-with-copilotkit-and-ag-ui
- CopilotKit Generative UI 2026 overview: https://www.copilotkit.ai/blog/the-developer-s-guide-to-generative-ui-in-2026
- OpenAI Apps SDK / MCP Apps resource and iframe bridge concepts: https://developers.openai.com/apps-sdk/build/mcp-server
- A2UI protocol v0.9.1: https://a2ui.org/specification/v0.9.1-a2ui/
- Azure Web PubSub overview, including AI chatbot token streaming and large-scale connections: https://learn.microsoft.com/en-us/azure/azure-web-pubsub/overview
- Azure Container Apps scaling/KEDA: https://learn.microsoft.com/en-us/azure/container-apps/scale-app
- Azure Cosmos DB vector search and DiskANN: https://learn.microsoft.com/en-us/azure/cosmos-db/nosql/vector-search
- Azure OpenTelemetry in Container Apps/Application Insights: https://learn.microsoft.com/en-us/azure/container-apps/opentelemetry-agents
- Microsoft Foundry migration from hub-based projects to Foundry projects: https://learn.microsoft.com/azure/foundry-classic/how-to/migrate-project
- Terraform for Microsoft Foundry resources: https://learn.microsoft.com/azure/foundry/how-to/create-resource-terraform
- Microsoft Foundry SDKs and OpenAI-compatible endpoints: https://learn.microsoft.com/azure/foundry/how-to/develop/sdk-overview
- Microsoft Foundry OpenAPI tools for agents: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/openapi
