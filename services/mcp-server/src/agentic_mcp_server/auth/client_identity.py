"""Client/app identity + scopes + per-client verification policy (ADR-0011 phase 4).

Per-user bearer identity (``Requester``) cannot enforce "platform-trusted" against
agents we do not control. The enforceable boundary needs a registered **client/app
identity**: a ``client_id`` with scopes/capabilities and a per-client
``verification_required`` policy, so a host can require that only receipt-bearing
answers are surfaced as platform-trusted.

A request carries BOTH a per-user subject (``Requester``) and a client identity
(``ClientIdentity``). The two are independent and composed (defence in depth):
client scopes are ADDITIONAL to the user-level ACLs, never a replacement.

How the client is authenticated stays abstract/pluggable: the client is identified
by an authenticated credential/claim (the ``client_id`` on the verified bearer token,
the same way ``Requester`` is resolved from the token's subject/team claims). The
registry maps that authenticated ``client_id`` to its scopes + policy. Registration
is config-driven (``MCP_CLIENT_REGISTRY``); NO secret value ever lives here — any
per-client secret is referenced by an env/Key Vault NAME only, never a value.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

#: Default scope grant for a client present in the registry with no explicit
#: ``scopes`` list — and the grant for the (anonymous) registry-absent client.
#: An empty scope set still authenticates; it simply gates no scope-guarded tool.
_NO_SCOPES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ClientIdentity:
    """The resolved app/client behind a request, alongside the per-user ``Requester``.

    ``client_id`` is the authenticated client credential/claim. ``scopes`` are the
    client's granted capabilities (ADDITIONAL to user ACLs). ``verification_required``
    is the per-client official-client policy: when true, the broker only marks
    evidence/answers platform-trusted for this client when accompanied by a valid,
    client-matched receipt. ``registered`` distinguishes a configured client from the
    fail-open anonymous identity a registry-absent client receives.
    """

    client_id: str
    scopes: frozenset[str] = _NO_SCOPES
    verification_required: bool = False
    registered: bool = False

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class _ClientPolicy:
    """One registry entry: a client's scopes + verification policy (no secrets)."""

    scopes: frozenset[str]
    verification_required: bool


def _empty_policies() -> dict[str, "_ClientPolicy"]:
    return {}


@dataclass(frozen=True)
class ClientRegistry:
    """Config-driven registry: authenticated ``client_id`` -> scopes + policy.

    A client present in the registry resolves to its configured ``ClientIdentity``.
    A client ABSENT from the registry resolves to an unregistered identity with no
    scopes and ``verification_required=False`` — verification is NEVER made mandatory
    for a client that did not opt in (brief: non-opted-in clients are unaffected).
    """

    policies: Mapping[str, _ClientPolicy] = field(default_factory=_empty_policies)

    def resolve(self, client_id: str) -> ClientIdentity:
        policy = self.policies.get(client_id)
        if policy is None:
            # Unregistered client: authenticates, but gains no scopes and is NOT
            # subject to verification_required. Behaviour for it is unchanged.
            return ClientIdentity(client_id=client_id)
        return ClientIdentity(
            client_id=client_id,
            scopes=policy.scopes,
            verification_required=policy.verification_required,
            registered=True,
        )


#: Recognised keys for a client registry entry. ``secret_env`` is the NAME of the
#: env/Key Vault entry holding any client secret — its VALUE is NEVER read or stored
#: here (the registry holds identifiers + policy only). Listing it is optional and
#: documentary: it lets a deployment record where a secret lives without exposing it.
_ENTRY_KEYS = {"scopes", "verification_required", "secret_env"}
#: Forbid value-shaped keys outright: a secret VALUE must never appear in config.
_FORBIDDEN_ENTRY_KEYS = {"secret", "client_secret", "key", "password", "credential"}


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"MCP_CLIENT_REGISTRY: duplicate key {key!r}")
        result[key] = value
    return result


def parse_client_registry(raw: str | None) -> ClientRegistry:
    """Parse the ``MCP_CLIENT_REGISTRY`` deployment value into a ``ClientRegistry``.

    Shape: a JSON object ``{client_id: {scopes?: [str], verification_required?: bool,
    secret_env?: str}}``. Identifiers + policy only — NEVER a secret value. A
    ``secret_env`` entry names where a secret lives; a value-shaped key (``secret``,
    ``client_secret``, ``key``, ``password``, ``credential``) fails the boot.

    Fail-fast: a typo in client config stops the boot rather than silently granting
    (or denying) the wrong scopes. Unset / empty / whitespace ⇒ empty registry
    (every client resolves to the unregistered, non-verification-required identity).
    """
    if raw is None or not raw.strip():
        return ClientRegistry()
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"MCP_CLIENT_REGISTRY is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("MCP_CLIENT_REGISTRY must be a JSON object of client_id -> policy")
    # object_pairs_hook returns dict[str, object] at every level; pyright sees
    # json.loads as Any, so pin the shape here once.
    parsed_obj = cast("dict[str, object]", parsed)
    policies: dict[str, _ClientPolicy] = {}
    for client_id, raw_entry in parsed_obj.items():
        if not client_id.strip() or client_id != client_id.strip():
            raise RuntimeError(f"MCP_CLIENT_REGISTRY: empty or padded client_id key {client_id!r}")
        if not isinstance(raw_entry, dict):
            raise RuntimeError(f"MCP_CLIENT_REGISTRY[{client_id!r}] must be an object")
        entry = cast("dict[str, object]", raw_entry)
        forbidden = _FORBIDDEN_ENTRY_KEYS.intersection(entry)
        if forbidden:
            raise RuntimeError(
                f"MCP_CLIENT_REGISTRY[{client_id!r}] must not carry secret values "
                f"(forbidden keys {sorted(forbidden)}); reference a secret by env "
                f"NAME via 'secret_env' instead"
            )
        unknown = set(entry) - _ENTRY_KEYS
        if unknown:
            raise RuntimeError(
                f"MCP_CLIENT_REGISTRY[{client_id!r}] has unknown keys {sorted(unknown)}"
            )
        scopes = _parse_scopes(client_id, entry.get("scopes", []))
        verification_required = _parse_verification_required(
            client_id, entry.get("verification_required", False)
        )
        secret_env = entry.get("secret_env")
        if secret_env is not None and (not isinstance(secret_env, str) or not secret_env.strip()):
            raise RuntimeError(
                f"MCP_CLIENT_REGISTRY[{client_id!r}].secret_env must be a non-empty "
                f"env/Key Vault NAME (string), never a secret value"
            )
        policies[client_id] = _ClientPolicy(
            scopes=scopes, verification_required=verification_required
        )
    return ClientRegistry(policies=policies)


def _parse_scopes(client_id: str, value: object) -> frozenset[str]:
    if not isinstance(value, list):
        raise RuntimeError(f"MCP_CLIENT_REGISTRY[{client_id!r}].scopes must be a list of strings")
    scopes: set[str] = set()
    for item in cast("list[object]", value):
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(
                f"MCP_CLIENT_REGISTRY[{client_id!r}].scopes must be non-empty strings"
            )
        scopes.add(item)
    return frozenset(scopes)


def _parse_verification_required(client_id: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(
            f"MCP_CLIENT_REGISTRY[{client_id!r}].verification_required must be a boolean"
        )
    return value
