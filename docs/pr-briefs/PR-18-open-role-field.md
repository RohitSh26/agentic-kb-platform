# PR-18 — Open the `read_pack` role field to team-defined agents

## Why

The product is the framework, not the six example agents: adopting teams bring their own
specialists, and the broker governs them by authenticated identity, not by name. One schema
detail contradicts that story today: `ReadPackRequest.role` is a closed `Literal` of exactly
our six role names (`mcp/tool_schemas/evidence.py`). A team's `security_auditor` must claim to
be one of our six roles to read the shared Evidence Pack — even though the broker never
branches on the value (it is logged and echoed, nothing more; authorization, budgets, and
ledger attribution all bind to the session subject).

## Scope

- Replace the `AgentRole` `Literal` with a free-form string type, **charset-guarded** the same
  way as `run_id`: the value lands verbatim in `key=value` audit logs
  (`broker.read_pack ... role=%s`), so spaces/newlines/`=`/quotes must stay unrepresentable
  (log-line forgery guard). Pattern: `^[A-Za-z0-9._-]{1,64}$`.
- Update `docs/contracts/mcp-tools-contract.md`: `role` is free-form (team-defined roles
  welcome), charset-guarded, and remains a view/correlation field only.
- Tests in `tests/contract/test_tool_schemas.py`: a non-canonical role (`security_auditor`)
  validates; forgery charsets are rejected. One integration assertion that `read_pack` serves a
  team-defined role end-to-end.
- Dev-guide note (doc 02 §broker identity paragraph).

## Do NOT

- Do not make the broker branch on `role` — it stays metadata. Per-role *views* remain a
  recorded follow-up.
- Do not touch `agent_name` (already free-form; never logged or persisted — the ledger stores
  the session subject).
- Do not change budgets here (that is PR-19).

## Acceptance criteria

- [ ] `ReadPackRequest(role="security_auditor")` validates; the six canonical names still do.
- [ ] `role="a b"`, `role="x\nstatus=ok"`, `role="r=1"`, 65+ chars are schema-rejected.
- [ ] `context.read_pack` round-trips a team-defined role against a live pack.
- [ ] Contract doc updated; no behavior change anywhere else; `make verify` green.
