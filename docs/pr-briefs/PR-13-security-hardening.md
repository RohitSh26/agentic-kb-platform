# PR-13 — Security hardening

## Scope
Managed identity, Key Vault for residual secrets, RBAC/ACL retrieval filtering, prompt-injection
handling, and audit logging.

## Context
docs/architecture §12, §14. .claude/agents/security-auditor. ADR-0001.

## Files to create / change
- `apps/mcp-server/src/auth/rbac.py` (filter retrieval by requester authorization BEFORE returning).
- `src/context_broker/untrusted.py` (wrap + mark retrieved content; injection detection).
- `src/telemetry/audit.py` (log every context expansion + source access).
- infra: managed identity bindings; Key Vault only where managed identity can't be used.

## Contracts
ACL metadata on source_item/knowledge_artifact; retrieval results carry an authorization decision.

## Acceptance criteria
- A requester sees only artifacts their team is authorized for.
- Retrieved text cannot alter tool policy/identity/instructions; injection-style content is marked.
- All expansions/source access are audited. No Search/OpenAI keys reachable from agent surfaces.

## Required tests
- ACL filtering, injection neutralization, audit completeness, no-secret-leakage scan.

## Do NOT
- Add API Management in this PR (that's an ADR-gated future addition).

## Kickoff prompt
"Implement PR-13. ACL retrieval filtering, untrusted-content wrapping + injection detection, audit
logging, managed identity. Run security-auditor; address every high/critical finding."
