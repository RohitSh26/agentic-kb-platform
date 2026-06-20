# Rule: Code-quality charter (all `src/`) — readable · extensible · maintainable

Apply when writing or reviewing code anywhere. Fix **demonstrated** problems; never refactor
speculatively (this charter is bounded by YAGNI + the DRY threshold below).

## Design principles
1. **SRP** — one class/function = one reason to change. If you describe it with "and," split it.
   Watch for god-objects: huge constructors, 400+-line methods, many unrelated branches.
2. **Open/Closed** — a new variant (connector, provider, event type) is a registration or a new
   subclass, never editing a long `if/elif`/`match`. Editing a dispatch block to add a case is a smell.
3. **Liskov** — every implementation of an interface is a true drop-in: same error contract, same
   edge-case behavior. Divergent adapters behind one interface is a violation.
4. **Interface Segregation** — small, purpose-specific interfaces; a consumer never depends on
   methods it doesn't call.
5. **Dependency Inversion** — depend on abstractions (Protocols/interfaces), not concretes. High-level
   policy must not import low-level detail; both depend on the interface. Dependencies point inward
   toward the domain. (Mirrors python.md: tools/builders depend on `SearchClient`/`ModelClient`,
   never the SDK.)
6. **DRY (threshold)** — extract a shared helper only when duplication is real: **≥3 instances** or a
   named extension pain. Two similar lines is fine.
7. **YAGNI / no gold-plating** — build for the requirement in front of you. No feature flags, config
   knobs, or abstractions for things that don't exist yet.
8. **Least astonishment** — code does what its name says; no hidden side effects. Naming carries
   meaning so comments don't have to.
9. **Encapsulation** — never reach into another object's private state; expose a method/property.
10. **Fail at the boundary, trust the interior** — validate untrusted input at edges (MCP tool input,
    connector input, external services); inside trusted code don't re-validate what a type or boundary
    already guaranteed.
11. **Separation of concerns / layering** — domain, application/use-case, infrastructure (DB/HTTP/LLM),
    interface (tools/routes) in distinct layers with a one-directional dependency rule. No business
    logic in tool handlers; no DB/LLM calls in domain.
12. **Composition over inheritance** — wire small collaborators rather than deep hierarchies.

## Patterns to reach for
- **Registry / handler-map over switch** — `dict[type, handler]` or strategy objects; adding a case
  is adding an entry.
- **Strategy** — pluggable algorithms behind one interface (parsers, rankers, providers).
- **Ports & Adapters (hexagonal)** — a Protocol ("port") in the core; each concrete (Postgres, an API
  client) is an adapter behind it. Core never imports an adapter.
- **Factory** — centralize construction + the registry of which concrete to build; keep call sites clean.
- **Dependency injection** — pass collaborators via the constructor; no globals/singletons.
- **Facade / Use-case object** — one entry-point per business operation; keep handlers thin.
- **Result/envelope normalization** — one canonical response + error shape, enforced in one place.
- **Extract Method / Extract Class** — the everyday refactors, when they genuinely reduce coupling.

## Checklist (heuristics)
- **One idiom per concern** repo-wide: logging, error→status mapping, config access, pagination, auth
  checks, response shape. Flag any module doing it differently.
- **No dead code** — delete unused modules/exports/functions after verifying zero importers (grep,
  don't guess).
- **Comments earn their place** — explain *why* (constraint, workaround, invariant), never *what*.
  Default to none. Docstrings on public surfaces only; accurate, current, not essays.
- **Error handling** — one exception taxonomy; never `except: pass` that swallows real errors; log with
  structured fields (don't drop the error value); map exceptions to status in one place.
- **Security is maintainability** — no secrets in logs; no string-interpolated SQL (bound parameters);
  validate at boundaries.
- **Naming** — intention-revealing, consistent suffixes (`*Store`, `*UseCase`, `*Protocol`), no
  non-standard abbreviations.
- **Size is a smell, not a rule** — very long functions / many-parameter constructors signal a missing
  object — but split only when it reduces coupling, not to hit a line count.
