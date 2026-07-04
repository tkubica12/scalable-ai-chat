# Scalable AI Chat
This project aims to showcase key design patterns for building scalable AI chat applications. Most AI apps today start as monolithic applications that are difficult to scale. Most frameworks such as LangChain or Semantic Kernel tend to showcase examples that are not scalable. Although Lang Graph or Autogen come with modularity and scalability in mind, examples are still mostly monolithic. This repository is about learning architecture patterns, not about providing a complete solution or production-ready code.

This repo is leveraging cloud scale-to-zero services in Azure, fully automated with Terraform and automated CI/CD pipelines using GitHub Actions. Code as well as documentation and architecture documents are build with help of GitHub Copilot and are meant to showcase how to leverage AI assistance and agents when building more complex applications beyond simple chatbots.

## Current capabilities

The chat path now uses a durable run abstraction in addition to the original compatibility endpoints. `front_service` exposes `POST /api/runs`, `GET /api/runs/{runId}`, and `POST /api/runs/{runId}/cancel`; `sse_service` exposes `GET /api/runs/{runId}/events` with replayable SSE frames backed by Redis Streams. The LLM worker emits typed AG-UI-shaped lifecycle, text, tool, usage, cancellation, and error events while still publishing the legacy token stream during migration.

Container build workflows publish both `latest` and immutable `${{ github.sha }}` GHCR tags. Terraform provisions Azure Container Apps, Service Bus, Redis, Cosmos DB vector containers, private Blob artifact storage, and the newer Microsoft Foundry resource/project model.

## [Architecture](./docs/Architecture.md)
Architecture overview of the project, evolving together with the code (in fact, architecture is created first and then used as context for GitHub Copilot when doing AI-assisted development).

## [How to run](./docs/HowToRun.md)
Instructions on how to deploy and run the project, including Terraform deployment to Azure and local testing.

## Results
TBD

## Using GitHub Copilot in more complex projects
TBD
