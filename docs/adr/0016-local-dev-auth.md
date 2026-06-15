# ADR-0016 — Local developer auth for exercising MCP tools

## Status

**Accepted** (2026-06-15) — ratified by the platform owner; **Option B implemented**. An opt-in,
OFF-by-default local-dev verifier (`MCP_LOCAL_DEV_AUTH`) lets a developer exercise the tools against a
built KB, with hard guardrails enforced at boot: it refuses a real `MCP_ENTRA_TENANT_ID`, refuses a
non-loopback `MCP_HTTP_HOST`, logs `event=local_dev_auth_enabled` loudly whenever active, and the dev
identity still flows through the normal ACL/scope/trust path. Production (flag unset) is byte-for-byte
unchanged — this is NOT an auth-off switch (invariant 6). Env vars: `MCP_LOCAL_DEV_AUTH`,
`MCP_LOCAL_DEV_SUBJECT` (default `local-dev`), `MCP_LOCAL_DEV_TEAMS` (csv, default `local-dev-team`),
`MCP_LOCAL_DEV_CLIENT_ID` (default = subject). Extends ADR-0001 and is bound by invariant 6.

## Context

A developer on a separate machine who has already **built** a KB (Postgres populated, one
`kb_version` active) wants to **use** it through the MCP tools (`context.create_pack` →
`context.open_evidence` → `graph.get_neighbors` → `context.verify_answer`). The runtime is
fail-closed Entra ID: `build_entra_verifier` (auth/entra.py) wires a JWKS `JWTVerifier`, and
`current_requester` / `current_client_identity` (context_broker/dependencies.py) raise
`ToolError("no authenticated session")` when no bearer is present. Every tool therefore requires a
valid Entra-issued access token whose `aud` matches `MCP_ENTRA_AUDIENCE` and whose issuer matches
the configured tenant.

That is correct for production but creates friction for a local "did my build work, can I query it?"
loop: the developer must stand up (or be granted access to) a real Entra app registration just to
call a tool against a Postgres that lives entirely on their laptop.

Today's honest, no-new-code answer (documented in docs/dev-guide/05-running-the-mcp-server.md):
**point the server at a real Entra tenant and acquire a token** (device-code or client-credentials
via MSAL / `az account get-access-token`). This keeps the production path and the dev path
identical, which is its own virtue. The question this ADR raises is whether to *also* offer a
narrower local path.

## Decision (proposed — pick one)

### Option A — Real Entra only (status quo, no code)

Keep exactly one verifier. Local use requires a real tenant + audience and a real token. Documented
in dev-guide/05. **This is the default if this ADR is not ratified.**

- Pros: one code path; nothing to mis-configure into production; invariant 6 untouched by
  construction (no second verifier exists to leak).
- Cons: a laptop-only "use my freshly built KB" loop still needs cloud identity; highest friction
  for the exact scenario task #108 targets.

### Option B — An explicitly opt-in, clearly-labelled **local dev verifier**, OFF by default

Add a second `TokenVerifier` implementation (mirrors the test `FakeVerifier`) that mints a fixed
local identity from a self-signed/dev token. It is selected **only** when a dedicated env flag is
set, and it **refuses to run** in any configuration that looks production-like. Concrete guardrails
(all required, fail-closed):

1. **Off by default.** `create_app()` builds `build_entra_verifier` unless
   `MCP_LOCAL_DEV_AUTH=1` is *explicitly* set. Unset / any other value ⇒ Entra, no exceptions.
2. **Refuses to co-exist with a real tenant.** If `MCP_LOCAL_DEV_AUTH=1` AND
   `MCP_ENTRA_TENANT_ID` is anything other than the documented all-zeros local placeholder, the
   server **raises at startup** (`RuntimeError`) — you cannot point dev auth at a real tenant.
3. **Refuses a public bind.** With dev auth on, refuse to start unless the bind host is loopback
   (`127.0.0.1` / `localhost`), so it can never be reached off the machine.
4. **Loud and ledgered.** Logs `event=local_dev_auth_ENABLED level=WARNING` on every boot and
   stamps a `dev_auth=true` marker on each `retrieval_event`, so a dev-auth session is never
   mistaken for a real one in the ledger.
5. **Never in the production image path.** The Dockerfile CMD and infra/ never set the flag; a
   contract test asserts the flag is absent from compose, the Dockerfile, and infra.

- Pros: a true laptop-only loop (build → serve → call tools) with zero cloud identity; still
  exercises the *same* broker, budgets, ACLs, evidence, and ledger — only the identity seam changes.
- Cons: introduces a second auth path. The guardrails above are load-bearing; a regression that
  weakens any of them is a security regression. Requires a focused test suite (flag-off ⇒ Entra;
  flag-on + real tenant ⇒ refuse; flag-on + public bind ⇒ refuse; ledger marker present).

## Recommendation

Ship **Option A documentation now** (done — dev-guide/05) and treat **Option B as Proposed** pending
owner ratification. Option B touches the auth boundary (auth/, dependencies.py) which is owned by
sibling PRs and is invariant-6 territory; it must not be implemented opportunistically. If ratified,
it lands as its own PR with the five guardrails above and its own tests, **not** folded into the
"run the server" change.

## Consequences

- Until ratified, the only way to call a tool locally is a real Entra token (dev-guide/05 §Auth).
- If Option B is accepted, a follow-up PR owns the verifier + guardrails + tests and updates
  dev-guide/05 with the `MCP_LOCAL_DEV_AUTH` loop.
- Either way, the production path stays fail-closed Entra with no auth-off switch.

## Open question for the owner

Ratify Option B (local dev verifier with the five guardrails) or keep Option A (real Entra only)?
