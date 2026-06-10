# 0007. V1 resource exclusions and add-when triggers

- Status: Accepted
- Date: 2026-06-10
- Deciders: Platform team

## Context
Over-engineering cloud resources early creates cost and operational burden before the agentic KB
pattern is proven. We commit to a lean footprint and explicit re-entry conditions.

## Decision
Exclude from V1: Azure Functions, Event Grid, Service Bus/Event Hub, Redis, API Management, Blob
Storage, a dedicated graph database, local SQLite as a production store, and streaming ingestion.
Adding any of these requires a new accepted ADR citing the trigger below and a safe-add plan that
keeps the MCP interface unchanged.

## Add-when triggers
- Blob: raw archives/PDFs/images/large graph artifacts. Keep Postgres metadata; move bodies to Blob
  with pointers.
- Graph DB: deep traversal/path/impact/analytics at runtime. Replace the Postgres edge backend behind
  MCP graph tools.
- Redis: high MCP QPS or evidence-pack hot-cache latency. Runtime cache only, never source of truth.
- API Management: multi-team quotas/portal/gateway. Place in front of MCP without changing agents.
- Event-driven ingestion: nightly not fresh enough. Webhooks/Functions/Service Bus for changed
  sources only.

## Consequences
+ Predictable cost; decisions are evidence-gated.
- Some capabilities (deep graph, real-time) are intentionally deferred.
