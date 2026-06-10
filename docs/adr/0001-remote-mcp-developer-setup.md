# 0001. Remote MCP server as the developer setup (Option A)

- Status: Accepted
- Date: 2026-06-10
- Deciders: Platform team

## Context
Developers should not run the KB pipeline, Graphify, Wikify, vector storage, or Search locally.
We need a setup that is light to adopt and keeps all knowledge infrastructure centrally managed.

## Decision
Developers connect to a remote MCP server. Their only local footprint is an AI coding client, agent
markdown files in the repo, MCP configuration, company SSO, and a repo checkout.

## Consequences
+ Trivial onboarding; no local KB, vector DB, or cloud keys on developer machines.
+ Enforcement (budgets, ACLs, dedupe) happens server-side regardless of client.
- Requires the remote server to be available and authenticated; offline work is limited to code.

## Alternatives considered
Local KB install (heavy, drift-prone, leaks keys); per-client plugins (fragmented enforcement).
