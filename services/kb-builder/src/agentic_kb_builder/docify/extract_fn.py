"""The live Graphify doc-extraction call, behind an injectable function (ADR-0023 §5).

This is the single seam between our adapter and Graphify's LLM doc pipeline. It reads the
SAME model endpoint as every other build-plane LLM call (the shared
``llm_endpoint.resolve_endpoint_from_env``), picks the Graphify backend for that endpoint
(the one isolated seam below), then materializes the document to a temp file under ``root``
(Graphify reads from disk) and calls ``extract_files_direct``.

Backend choice by provider:
- OpenAI-compatible (ollama / groq / openai, or any ``LLM_BASE_URL``): Graphify's built-in
  "openai" backend hardcodes api.openai.com, so we register an in-process backend pointed at
  our endpoint instead (see ``_graphify_backend_name`` — the ONLY place we touch Graphify's
  internal registry).
- ``azure`` (Azure OpenAI deployment): Graphify's BUILT-IN ``azure`` backend drives the
  AzureOpenAI SDK and reads ``AZURE_OPENAI_ENDPOINT`` / ``AZURE_OPENAI_API_VERSION`` from the
  env (the deployment IS the model). Its call path differs from the OpenAI-compatible one, so
  we do NOT fake it with a base_url.

It returns Graphify's raw extraction dict — the trust-sensitive normalization lives in the
pure ``map_doc_extraction``. Unit tests inject a captured-fixture function instead of this one,
so the live LLM is never required by the test suite (the hermetic-test requirement, ADR-0023 §5).
"""

import asyncio
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from agentic_kb_builder.infrastructure.azure_openai.llm_endpoint import (
    AZURE_PROVIDER,
    ModelEndpoint,
    resolve_endpoint_from_env,
)
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# The in-process Graphify backend name we register an OpenAI-compatible endpoint under.
# Distinct from Graphify's built-in "openai" (which hardcodes api.openai.com) so we never
# collide. ``azure`` instead uses Graphify's own built-in backend name (``AZURE_PROVIDER``).
BACKEND_NAME = "kb_docify"

# Docify's historical LLM_MAX_TOKENS fallback (larger than the judge's) — kept so the
# generation-cache key / model identity stays stable.
_DOCIFY_MAX_TOKENS_DEFAULT = 8192


class DocExtractFn(Protocol):
    """Injectable doc-extraction seam: text in, Graphify's raw extraction dict out.

    The default implementation is ``graphify_doc_extract`` (live LLM). Tests inject a
    captured-fixture function with the same signature so they run hermetically."""

    async def __call__(self, *, text: str, doc_path: str) -> Mapping[str, Any]: ...


def resolve_endpoint() -> ModelEndpoint:
    """Resolve the docify model endpoint from the build env (thin shared pass-through).

    Doc extraction REQUIRES a key/deployment (unlike code extraction); the shared resolver
    fails loudly on a missing one, so docs are never silently dropped.
    """
    return resolve_endpoint_from_env(max_tokens_default=_DOCIFY_MAX_TOKENS_DEFAULT)


def _graphify_backend_name(endpoint: ModelEndpoint) -> str:
    """Return the Graphify backend name to drive ``endpoint``, registering one if needed.

    This is the ONLY place we touch Graphify's internal backend registry. Graphify exposes no
    public API to point a backend at a custom endpoint, so we register one in-process here; if
    a public API appears, change ONLY this function.

    - ``azure``: use Graphify's BUILT-IN ``azure`` backend (SDK-based; reads AZURE_OPENAI_* from
      the env, deployment IS the model). Nothing to register.
    - otherwise: register an in-process OpenAI-compatible backend pointed at our base_url. The
      API key is read by Graphify from the env var named here (``LLM_API_KEY``) — never placed
      in the dict and never logged.
    """
    if endpoint.provider == AZURE_PROVIDER:
        return AZURE_PROVIDER

    from graphify import llm  # declared dependency (ADR-0012 / ADR-0023)

    llm.BACKENDS[BACKEND_NAME] = {
        "base_url": endpoint.base_url,
        "default_model": endpoint.model,
        "env_key": "LLM_API_KEY",
        "model_env_key": "LLM_MODEL",
        "pricing": {"input": 0.0, "output": 0.0},
        "temperature": 0,
        "max_tokens": endpoint.max_tokens,
    }
    return BACKEND_NAME


def _extract_sync(
    *, backend: str, api_key: str, model: str, text: str, doc_path: str
) -> Mapping[str, Any]:
    """Write one document to a temp tree and run Graphify's LLM extractor over it (sync)."""
    from graphify import llm  # declared dependency (the call seam; see _graphify_backend_name)

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


def make_graphify_doc_extract(endpoint: ModelEndpoint) -> DocExtractFn:
    """Build the live doc-extraction function bound to the resolved ``endpoint``.

    Picks the Graphify backend (the isolated seam), then returns a function that extracts one
    document. The API key is captured in the closure and NEVER logged (rule python.md).
    """
    backend = _graphify_backend_name(endpoint)
    api_key = endpoint.api_key
    model = endpoint.model
    # provider is logged for traceability; the key is NEVER logged (rule python.md).
    logger.info(
        "event=docify_backend_registered backend=%s provider=%s model=%s",
        backend,
        endpoint.provider,
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


__all__ = [
    "BACKEND_NAME",
    "DocExtractFn",
    "make_graphify_doc_extract",
    "resolve_endpoint",
]
