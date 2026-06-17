"""One PUBLIC model-endpoint resolver shared by every build-plane LLM consumer.

Both the ``ChatModelClient`` (the relationship judge) and the ``docify`` Graphify adapter
need to turn the SAME build env into a concrete endpoint (provider, base_url/azure specifics,
api_key, model, max_tokens). Historically each re-derived this, and ``docify`` reached into
``chat_model_client._PROVIDER_DEFAULTS`` (a private name). This module is the single public
home for that resolution so neither consumer touches the other's internals.

Supported providers (``LLM_PROVIDER``, default ``ollama``):
- OpenAI-compatible (``ollama`` / ``groq`` / ``openai`` / any custom ``LLM_BASE_URL``):
  ``LLM_BASE_URL``, ``LLM_API_KEY``, ``LLM_MODEL``.
- ``azure`` (Azure OpenAI deployment): ``AZURE_OPENAI_ENDPOINT``, ``AZURE_OPENAI_API_KEY``,
  ``AZURE_OPENAI_DEPLOYMENT`` (the deployment IS the model), ``AZURE_OPENAI_API_VERSION``
  (default ``2024-06-01``).

A missing key/deployment fails loudly (a RuntimeError) — the build never silently drops work.
The api_key lives on the returned dataclass but is NEVER logged (rule python.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

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


def resolve_endpoint_from_env(*, max_tokens_default: int) -> ModelEndpoint:
    """Resolve the model endpoint from the build env (the ONE provider resolution).

    ``max_tokens_default`` lets each consumer keep its own historical ``LLM_MAX_TOKENS``
    fallback (the judge and docify differ) without changing any other behavior. Raises a clear
    RuntimeError when a required credential/deployment is missing so work is never silently
    dropped.
    """
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", str(max_tokens_default)))

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
        base_url = os.environ.get("LLM_BASE_URL", "")
        api_key = os.environ.get("LLM_API_KEY", "")
        model = os.environ.get("LLM_MODEL", "")
        missing = [
            name
            for name, value in (
                ("LLM_BASE_URL", base_url),  # the .../anthropic Foundry endpoint
                ("LLM_API_KEY", api_key),
                ("LLM_MODEL", model),  # the Claude deployment name, e.g. claude-sonnet-4-6
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
    base_url = os.environ.get("LLM_BASE_URL", default_base)
    api_key = os.environ.get("LLM_API_KEY", default_key)
    if not api_key:
        raise RuntimeError(f"LLM_API_KEY is required for provider {provider!r}")
    model = os.environ.get("LLM_MODEL", default_model)
    return ModelEndpoint(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
    )


__all__ = [
    "ANTHROPIC_FOUNDRY_PROVIDER",
    "AZURE_PROVIDER",
    "PROVIDER_DEFAULTS",
    "ModelEndpoint",
    "resolve_endpoint_from_env",
]
