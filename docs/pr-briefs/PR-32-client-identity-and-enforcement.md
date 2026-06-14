# PR-32 — Client/app identity + scopes + official-client receipt enforcement

## Why

Per-user bearer identity can't enforce "platform-trusted" against agents we don't control. The
enforceable boundary needs a registered **client/app identity** with scopes and a
`verification_required` policy, so a host can require that only receipt-bearing answers are surfaced
as platform-trusted (ADR-0011). Phase 4.

## Scope

- **Client/app identity (auth extension):** registered `client_id` with scopes/capabilities and a
  per-client `verification_required` policy. Distinct from the per-user subject — a request carries
  both. Registration/admin is config-driven (no secret literals; secrets by reference only).
- **Receipt binding:** `verify_answer` stamps the validated `client_id` into the receipt (the field
  reserved since phase 0). Receipt validity is scoped to the client it was issued to.
- **Official-client enforcement:** a broker policy mode where, for a `verification_required` client,
  evidence/answers are only marked platform-trusted when accompanied by a valid receipt for that
  client. Surface a clear, structured denial otherwise (no silent pass).
- Scope checks compose with existing ACL + trust filters (defence in depth).
- Tests: a `verification_required` client without a valid receipt is not granted platform-trust; a
  valid receipt for client A doesn't satisfy client B; scopes gate tool access; ACL + trust + scope
  all enforced together; no secret material in logs.

## Do NOT

- Do not weaken user-level ACLs — client identity is *additional*, not a replacement.
- Do not hardcode client secrets; reference by env/Key Vault name only.
- Do not make verification mandatory for clients that didn't opt into `verification_required`.

## Acceptance criteria

- [ ] Requests carry client + user identity; `client_id` is stamped into receipts and scopes them.
- [ ] A `verification_required` client gets platform-trust only with a valid, client-matched receipt.
- [ ] Scopes + ACL + trust filters compose; cross-client receipt reuse is rejected.
- [ ] No secret value in code, fixtures, or logs; `make verify` green.
