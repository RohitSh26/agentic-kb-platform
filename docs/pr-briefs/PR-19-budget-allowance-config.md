# PR-19 — Deployment-time per-agent budget allowances

## Why

Budget *enforcement* is fully server-side (PR-10: lock-serialized check-then-charge), but the
allowance *map* has no deployment story: `budgets.py` promised "role manifests will supply that
map per deployment" and nothing does — `create_app()` ships an empty map, so in production every
subject silently gets the conservative default (1 request / 2,500 tokens). The token-budgets rule
names richer allowances (implementation: 2 requests / 3–4k) that are currently unreachable
without forking server assembly. Adopting teams must be able to grant their own agents their own
allowances by configuration, not by code.

## Scope

- New optional env var **`MCP_AGENT_ALLOWANCES`**: a JSON object mapping authenticated session
  subjects to allowances, e.g.
  `{"impl-agent-client-id": {"max_requests": 2, "max_tokens": 4000}}`.
  Subjects are identifiers (Entra object/client ids), never secrets — same config posture as the
  tenant id and audience.
- Parser lives next to `BudgetPolicy` (`context_broker/budgets.py`): pure function, **fail-fast**
  on malformed JSON, non-object shapes, unknown keys, non-int or negative values, empty subject
  keys. Empty/whitespace value or unset var ⇒ empty map (everyone keeps the conservative
  default). No silent fallback on bad config — a typo must stop the boot, not quietly zero out
  budgets.
- `load_config()` reads the var; `create_app()` builds the `BudgetPolicy`; `build_server()`
  gains an optional `budget_policy` parameter (None ⇒ default policy, current behavior).
- Structured startup log: `event=agent_allowances_loaded subjects=N` (count only).
- `docker-compose.yml` passes the var through (`${MCP_AGENT_ALLOWANCES:-}`) so the compose stack
  can exercise it; empty string is treated as unset.
- Docs: contract note in `docs/contracts/mcp-tools-contract.md` (budgets section), dev-guide
  update, drop the stale "PR-11 role manifests" promise from `budgets.py`'s docstring.

## Do NOT

- Do not change enforcement logic, the conservative default, or the run-budget clamp.
- Do not key allowances by `agent_name`/`role` request fields — subjects only (identity binds to
  the session; this is what makes the budgets unspoofable).
- Do not introduce a config file/Key Vault dependency — one env var, identifiers only.

## Acceptance criteria

- [ ] Valid JSON map ⇒ those subjects get their allowances; unlisted subjects keep the default.
- [ ] Malformed JSON / wrong shape / unknown key / negative or non-int value ⇒ `RuntimeError`
      at startup naming the problem; empty or unset var ⇒ empty map.
- [ ] Integration: a configured subject's second `request_more` is approved while an unlisted
      subject's is denied — driven through a `BudgetPolicy` built by the parser.
- [ ] Compose passthrough present; compose contract test still green (no credential markers).
- [ ] `make verify` green.
