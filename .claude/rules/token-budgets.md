# Rule: Token budgets (services/mcp-server, evals)

Default V1 budgets (tune with logs, record changes in an ADR if structural):

- Full run context budget (orchestrator): 12k–18k tokens
- Initial Evidence Pack: 6k–8k tokens
- Implementation agent extra: 2 requests / 3k–4k tokens total
- Test agent extra: 1 request / 1.5k–2.5k tokens
- Code reviewer extra: 1 request / 1.5k–2.5k tokens
- Delivery planner extra: 1 request / 1k–1.5k tokens
- PR planner extra: 1 request / 1k–1.5k tokens
- ADR writer extra: 2 requests / 3k tokens (implementation band; ADR-0030)
- Infra code extra: 2 requests / 3k tokens (implementation band; ADR-0030)
- Bug reviewer extra: 1 request / 2k tokens (ADR-0030 review panel)
- Security reviewer extra: 1 request / 2k tokens (ADR-0030 review panel)
- Quality reviewer extra: 1 request / 2k tokens (ADR-0030 review panel)
- Test coverage reviewer extra: 1 request / 2k tokens (ADR-0030 review panel)
- Max evidence cards per retrieval: 3–5 after internal rerank
- Semantic duplicate threshold: start 0.88–0.92, tune from logs

Budgets are enforced in the Context Broker, surfaced in the ledger, and asserted in tests.
