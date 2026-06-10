# Contract: agent outputs

> Each runtime agent manifest in `agents/` declares an output schema; the MCP
> runtime validates agent outputs against it before accepting them.
> `AGENT_OUTPUT_SCHEMA_VERSION = "1.0.0"`.

## Base rules (all agent outputs)

- Frozen, `extra="forbid"`, versioned: every output carries
  `schema_version: "1.0.0"`.
- **Every claim cites evidence IDs** from the run's Evidence Pack. A claim
  without evidence must be downgraded to an open question — agents never invent
  files, classes, APIs, endpoints, or storage details.
- Outputs are produced by agents that only ever saw broker-mediated evidence;
  they contain no secrets and no direct data-store references.

## Status

V1 defines the base model only (`AgentOutputModel`); concrete per-role output
schemas (implementation, test, code-review, delivery-planning) land with the
agent manifests (PR-11) and must be added to this document when they do.
The Python definition lives with its validator in mcp-server once PR-11 lands;
until then this document is the contract of record.
