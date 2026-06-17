"""The live Graphify doc-extraction call, behind an injectable function (ADR-0023 §5).

This is the single seam between our adapter and Graphify's LLM doc pipeline. It materializes
the document to a temp file under ``root`` (Graphify reads from disk) and calls
``extract_files_direct`` against a backend chosen by ``LLM_PROVIDER``:

- OpenAI-compatible providers (ollama / groq / openai, or any ``LLM_BASE_URL``): we register
  an in-process Graphify backend pointed at that endpoint (Graphify's built-in "openai"
  backend hardcodes api.openai.com, so we never reuse it).
- ``azure`` (Azure OpenAI deployment): we use Graphify's BUILT-IN ``azure`` backend, which
  drives the AzureOpenAI SDK and reads ``AZURE_OPENAI_ENDPOINT`` / ``AZURE_OPENAI_API_VERSION``
  from the env (the deployment name is the model). The Azure SDK call path differs from the
  OpenAI-compatible one, so we do NOT fake it with a base_url.

It returns Graphify's raw extraction dict — the trust-sensitive normalization lives in the
pure ``map_doc_extraction``. Unit tests inject a captured-fixture function instead of this one,
so the live LLM is never required by the test suite (the hermetic-test requirement, ADR-0023 §5).
"""

import asyncio
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from agentic_kb_builder.infrastructure.azure_openai.chat_model_client import _PROVIDER_DEFAULTS
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# The in-process Graphify backend name we register an OpenAI-compatible endpoint under.
# Distinct from Graphify's built-in "openai" (which hardcodes api.openai.com) so we never
# collide. ``azure`` instead uses Graphify's own built-in backend name.
BACKEND_NAME = "kb_docify"
_AZURE_BACKEND = "azure"


class DocExtractFn(Protocol):
    """Injectable doc-extraction seam: text in, Graphify's raw extraction dict out.

    The default implementation is ``graphify_doc_extract`` (live LLM). Tests inject a
    captured-fixture function with the same signature so they run hermetically."""

    async def __call__(self, *, text: str, doc_path: str) -> Mapping[str, Any]: ...


def _register_openai_compat_backend(
    *, provider: str, base_url: str, model: str, max_tokens: int
) -> None:
    """Register an OpenAI-compatible endpoint as a Graphify backend in-process (idempotent).

    Reuses the provider->base_url map from ChatModelClient so the doc LLM call uses the SAME
    endpoint resolution as every other model call. The API key is read by Graphify from the
    env var named here (LLM_API_KEY) — never put in the dict and never logged.
    """
    from graphify import llm  # declared dependency (ADR-0012 / ADR-0023)

    llm.BACKENDS[BACKEND_NAME] = {
        "base_url": base_url,
        "default_model": model,
        "env_key": "LLM_API_KEY",
        "model_env_key": "LLM_MODEL",
        "pricing": {"input": 0.0, "output": 0.0},
        "temperature": 0,
        "max_tokens": max_tokens,
    }


def _extract_sync(
    *, backend: str, api_key: str, model: str, text: str, doc_path: str
) -> Mapping[str, Any]:
    """Write one document to a temp tree and run Graphify's LLM extractor over it (sync)."""
    from graphify import llm  # declared dependency

    with tempfile.TemporaryDirectory(prefix="kb-docify-") as tmp:
        root = Path(tmp).resolve()
        fp = root / doc_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(text, encoding="utf-8")
        data = llm.extract_files_direct(
            [fp], backend=backend, api_key=api_key, model=model, root=root
        )
        result = cast("Mapping[str, Any]", data)
        logger.info(
            "event=docify_extracted doc_path=%s nodes=%d input_tokens=%s output_tokens=%s",
            doc_path,
            len(cast("list[object]", result.get("nodes", []))),
            result.get("input_tokens"),
            result.get("output_tokens"),
        )
        return result


def make_graphify_doc_extract(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> DocExtractFn:
    """Build the live doc-extraction function bound to the configured provider.

    Selects the Graphify backend by provider (Azure OpenAI SDK for ``azure``, else an
    in-process OpenAI-compatible backend), then returns a function that extracts one document.
    """
    if provider == _AZURE_BACKEND:
        # Graphify's built-in azure backend uses the AzureOpenAI SDK and reads
        # AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_VERSION from the env; the deployment is
        # the model. Nothing to register — the call path is SDK-based, not base_url-based.
        backend = _AZURE_BACKEND
    else:
        _register_openai_compat_backend(
            provider=provider, base_url=base_url, model=model, max_tokens=max_tokens
        )
        backend = BACKEND_NAME
    # provider is logged for traceability; the key is NEVER logged (rule python.md).
    logger.info(
        "event=docify_backend_registered backend=%s provider=%s model=%s",
        backend,
        provider,
        model,
    )

    async def graphify_doc_extract(*, text: str, doc_path: str) -> Mapping[str, Any]:
        # extract_files_direct is sync (network-bound); to_thread keeps the async path honest.
        return await asyncio.to_thread(
            _extract_sync,
            backend=backend,
            api_key=api_key,
            model=model,
            text=text,
            doc_path=doc_path,
        )

    return graphify_doc_extract


def resolve_endpoint() -> tuple[str, str, str, str, int]:
    """Resolve (provider, endpoint, api_key, model, max_tokens) from the build env.

    Mirrors ChatModelClient.from_env so docify uses the SAME model configuration as every
    other model call. Doc extraction REQUIRES a key/deployment (unlike code extraction); a
    missing one fails loudly here, never silently drops docs.

    - ``azure``: AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT (the
      model); AZURE_OPENAI_API_VERSION is read by Graphify's azure backend from the env.
    - OpenAI-compatible (ollama / groq / openai / custom): LLM_PROVIDER, LLM_BASE_URL,
      LLM_API_KEY, LLM_MODEL.
    """
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8192"))

    if provider == _AZURE_BACKEND:
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", endpoint),
                ("AZURE_OPENAI_API_KEY", api_key),
                ("AZURE_OPENAI_DEPLOYMENT", model),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"docify azure provider requires {', '.join(missing)} to be set"
            )
        return provider, endpoint, api_key, model, max_tokens

    default_base, default_key, default_model = _PROVIDER_DEFAULTS.get(
        provider, _PROVIDER_DEFAULTS["ollama"]
    )
    base_url = os.environ.get("LLM_BASE_URL", default_base)
    api_key = os.environ.get("LLM_API_KEY", default_key)
    if not api_key:
        raise RuntimeError(f"LLM_API_KEY is required for docify provider {provider!r}")
    model = os.environ.get("LLM_MODEL", default_model)
    return provider, base_url, api_key, model, max_tokens


__all__ = [
    "BACKEND_NAME",
    "DocExtractFn",
    "make_graphify_doc_extract",
    "resolve_endpoint",
]
