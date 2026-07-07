"""One PUBLIC model-endpoint resolver shared by every build-plane LLM consumer.

Both the ``ChatModelClient`` (the relationship judge) and the ``docify`` Graphify adapter
need to turn the SAME build env into a concrete endpoint (provider, base_url/azure specifics,
api_key, model, max_tokens). Historically each re-derived this, and ``docify`` reached into
``chat_model_client._PROVIDER_DEFAULTS`` (a private name). This module is the single public
home for that resolution so neither consumer touches the other's internals.

Accepted ``{env_prefix}_PROVIDER`` values (``env_prefix`` defaults to ``LLM``; docify
passes ``DOC_LLM``): ANY string is accepted — this resolver never rejects a provider name.
- ``azure`` and ``anthropic_foundry`` get dedicated branches (below).
- Everything else (``ollama`` / ``groq`` / ``openai`` / any custom name) is generic
  OpenAI-compatible: ``{env_prefix}_BASE_URL``, ``{env_prefix}_API_KEY``, ``{env_prefix}_MODEL``.
  The api key falls back to ``GROQ_API_KEY`` when ``{env_prefix}_API_KEY`` is unset (matching
  ``scripts/kb_agent.py`` and review-panel's ``ModelClient`` shim), then to the provider's
  built-in default (only ``ollama`` has one — a dummy key, since it needs no real auth).
- ``azure`` (Azure OpenAI deployment): ``AZURE_OPENAI_ENDPOINT``, ``AZURE_OPENAI_API_KEY``,
  ``AZURE_OPENAI_DEPLOYMENT`` (the deployment IS the model), ``AZURE_OPENAI_API_VERSION``
  (default ``2024-06-01``). No ``GROQ_API_KEY`` fallback — Azure always needs its own key.

A missing key/deployment fails loudly (a RuntimeError) — the build never silently drops work.
The api_key lives on the returned dataclass but is NEVER logged (rule python.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# provider -> (default base_url, default api_key, default model) for OpenAI-compatible providers.
# Public so both the ChatModelClient and the docify adapter share ONE source of truth instead of
# duplicating the map. ``azure`` is intentionally absent: it resolves via the AZURE_OPENAI_* env.
PROVIDER_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "ollama": ("http://localhost:11434/v1", "ollama", "llama3.1"),
    "groq": ("https://api.groq.com/openai/v1", "", "llama-3.1-8b-instant"),
    "openai": ("https://api.openai.com/v1", "", "gpt-4o-mini"),
}

# The provider value that routes to Azure OpenAI's native SDK / backend instead of the
# OpenAI-compatible base_url path.
AZURE_PROVIDER = "azure"

# Claude models hosted on Azure AI Foundry. They use the Anthropic SDK's AnthropicFoundry
# client + the Messages API (NOT OpenAI chat.completions), so they route to a separate client
# path. Config reuses the generic LLM_* vars: LLM_BASE_URL is the
# https://<resource>.services.ai.azure.com/anthropic endpoint, LLM_MODEL is the deployment name.
ANTHROPIC_FOUNDRY_PROVIDER = "anthropic_foundry"

_DEFAULT_AZURE_API_VERSION = "2024-06-01"


@dataclass(frozen=True)
class ModelEndpoint:
    """A fully-resolved model endpoint that both LLM consumers can build a client from.

    For OpenAI-compatible providers ``base_url`` + ``api_key`` + ``model`` are authoritative
    and the azure fields are empty. For ``azure`` the ``azure_endpoint`` / ``azure_api_version``
    fields drive the Azure SDK and ``model`` is the deployment name (``base_url`` is unused).
    """

    provider: str
    base_url: str
    api_key: str
    model: str
    max_tokens: int
    azure_endpoint: str = ""
    azure_api_version: str = ""

    @property
    def is_azure(self) -> bool:
        return self.provider == AZURE_PROVIDER

    @property
    def is_anthropic_foundry(self) -> bool:
        return self.provider == ANTHROPIC_FOUNDRY_PROVIDER


def resolve_endpoint_from_env(*, max_tokens_default: int, env_prefix: str = "LLM") -> ModelEndpoint:
    """Resolve the model endpoint from the build env (the ONE provider resolution).

    ``max_tokens_default`` lets each consumer keep its own historical ``*_MAX_TOKENS`` fallback
    (the judge and docify differ). ``env_prefix`` selects the generic var family: the default
    ``"LLM"`` reads ``LLM_PROVIDER`` / ``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL``;
    docify passes ``"DOC_LLM"`` to run documents on a SEPARATE model from the agent/judge (e.g.
    Claude-on-Foundry can't extract docs — Graphify speaks OpenAI's API). The azure branch keeps
    its own ``AZURE_OPENAI_*`` namespace regardless of prefix. Raises a clear RuntimeError when a
    required credential/deployment is missing so work is never silently dropped.
    """
    provider = os.environ.get(f"{env_prefix}_PROVIDER", "ollama").lower()
    max_tokens = int(os.environ.get(f"{env_prefix}_MAX_TOKENS", str(max_tokens_default)))

    if provider == AZURE_PROVIDER:
        azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", azure_endpoint),
                ("AZURE_OPENAI_API_KEY", api_key),
                ("AZURE_OPENAI_DEPLOYMENT", model),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"azure provider requires {', '.join(missing)} to be set")
        return ModelEndpoint(
            provider=provider,
            base_url="",
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            azure_endpoint=azure_endpoint,
            azure_api_version=os.environ.get(
                "AZURE_OPENAI_API_VERSION", _DEFAULT_AZURE_API_VERSION
            ),
        )

    if provider == ANTHROPIC_FOUNDRY_PROVIDER:
        base_url = os.environ.get(f"{env_prefix}_BASE_URL", "")
        api_key = os.environ.get(f"{env_prefix}_API_KEY", "")
        model = os.environ.get(f"{env_prefix}_MODEL", "")
        missing = [
            name
            for name, value in (
                (f"{env_prefix}_BASE_URL", base_url),  # the .../anthropic Foundry endpoint
                (f"{env_prefix}_API_KEY", api_key),
                (f"{env_prefix}_MODEL", model),  # the Claude deployment, e.g. claude-sonnet-4-6
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"anthropic_foundry provider requires {', '.join(missing)} to be set"
            )
        return ModelEndpoint(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
        )

    default_base, default_key, default_model = PROVIDER_DEFAULTS.get(
        provider, PROVIDER_DEFAULTS["ollama"]
    )
    base_url = os.environ.get(f"{env_prefix}_BASE_URL", default_base)
    # {env_prefix}_API_KEY wins; else GROQ_API_KEY (matching kb_agent.py and review-panel's
    # ModelClient shim, so all three consumers resolve a key the same way); else the
    # provider's own default (only ollama has one — a dummy key, no real auth needed).
    api_key = (
        os.environ.get(f"{env_prefix}_API_KEY") or os.environ.get("GROQ_API_KEY") or default_key
    )
    if not api_key:
        raise RuntimeError(f"{env_prefix}_API_KEY is required for provider {provider!r}")
    model = os.environ.get(f"{env_prefix}_MODEL", default_model)
    return ModelEndpoint(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
    )


def llm_http_client() -> httpx.AsyncClient | None:
    """The httpx client every LLM SDK (OpenAI / Azure / Anthropic) should use, or None for
    the SDK's secure default.

    Behind a corporate TLS-inspecting proxy (e.g. Zscaler) the provider cert chains to the
    corporate CA, so the call fails with ``CERTIFICATE_VERIFY_FAILED``. The SDKs build their
    httpx client from ``certifi`` and do NOT read ``SSL_CERT_FILE``, so we thread the trust
    material in EXPLICITLY:

    1. ``LLM_SSL_VERIFY=false`` -> verification DISABLED (insecure; last resort).
    2. else a CA bundle from ``LLM_CA_CERT`` / ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` ->
       ``verify=<that bundle>`` so the corporate CA (e.g. ZscalerRootCA.pem) is trusted.
    3. else None -> the SDK's secure certifi default (TLS never weakened unless opted in).
    """
    if os.environ.get("LLM_SSL_VERIFY", "true").strip().lower() in ("0", "false", "no", "off"):
        logger.warning("event=llm_ssl_verify_disabled msg=TLS-verification-OFF-insecure")
        return httpx.AsyncClient(verify=False)
    ca_bundle = (
        os.environ.get("LLM_CA_CERT")
        or os.environ.get("SSL_CERT_FILE")
        or os.environ.get("REQUESTS_CA_BUNDLE")
    )
    if ca_bundle:
        logger.info("event=llm_ca_cert_used path=%s", ca_bundle)
        return httpx.AsyncClient(verify=ca_bundle)
    return None


__all__ = [
    "ANTHROPIC_FOUNDRY_PROVIDER",
    "AZURE_PROVIDER",
    "PROVIDER_DEFAULTS",
    "ModelEndpoint",
    "llm_http_client",
    "resolve_endpoint_from_env",
]
