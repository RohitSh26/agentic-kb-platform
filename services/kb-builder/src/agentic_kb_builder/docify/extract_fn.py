"""The live Graphify doc-extraction call, behind an injectable function (ADR-0023 §5).

This is the single seam between our adapter and Graphify's LLM doc pipeline. It registers a
Graphify backend IN-PROCESS pointed at our configured endpoint (Graphify's built-in "openai"
backend hardcodes api.openai.com), materializes the document to a temp file under ``root``
(Graphify reads from disk), and calls ``extract_files_direct``. It returns Graphify's raw
extraction dict — the trust-sensitive normalization lives in the pure ``map_doc_extraction``.

Unit tests inject a captured-fixture function instead of this one, so the live LLM is never
required by the test suite (the hermetic-test requirement, ADR-0023 §5).
"""

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from agentic_kb_builder.infrastructure.azure_openai.chat_model_client import _PROVIDER_DEFAULTS
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

# The in-process Graphify backend name we register our endpoint under. Distinct from
# Graphify's built-in "openai" (which hardcodes api.openai.com) so we never collide.
BACKEND_NAME = "kb_docify"


class DocExtractFn(Protocol):
    """Injectable doc-extraction seam: text in, Graphify's raw extraction dict out.

    The default implementation is ``graphify_doc_extract`` (live LLM). Tests inject a
    captured-fixture function with the same signature so they run hermetically."""

    async def __call__(self, *, text: str, doc_path: str) -> Mapping[str, Any]: ...


def _register_backend(*, provider: str, base_url: str, model: str, max_tokens: int) -> None:
    """Register our endpoint as a Graphify backend in-process (idempotent).

    Reuses the provider->base_url map from ChatModelClient so the doc LLM call uses the
    SAME endpoint resolution as every other model call. The API key is read by Graphify
    from the env var named here (LLM_API_KEY) — never passed positionally into the
    registration dict and never logged.
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
    # provider is logged for traceability; the key is NEVER logged (rule python.md).
    logger.info(
        "event=docify_backend_registered backend=%s provider=%s model=%s",
        BACKEND_NAME,
        provider,
        model,
    )


def make_graphify_doc_extract(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
) -> DocExtractFn:
    """Build the live doc-extraction function bound to a configured endpoint.

    Registers the backend once, then returns a function that materializes one document to
    a temp file under a fresh root and runs Graphify's LLM extractor over it.
    """
    _register_backend(provider=provider, base_url=base_url, model=model, max_tokens=max_tokens)

    async def graphify_doc_extract(*, text: str, doc_path: str) -> Mapping[str, Any]:
        from graphify import llm  # declared dependency

        with tempfile.TemporaryDirectory(prefix="kb-docify-") as tmp:
            root = Path(tmp).resolve()
            fp = root / doc_path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(text, encoding="utf-8")
            # extract_files_direct is sync (network-bound); run it without blocking the
            # loop. asyncio.to_thread keeps the build's async path honest.
            import asyncio

            data = await asyncio.to_thread(
                llm.extract_files_direct,
                [fp],
                backend=BACKEND_NAME,
                api_key=api_key,
                model=model,
                root=root,
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

    return graphify_doc_extract


def resolve_endpoint() -> tuple[str, str, str, str, int]:
    """Resolve (provider, base_url, api_key, model, max_tokens) from the build env.

    Mirrors ChatModelClient.from_env's OpenAI-compatible-provider branch so docify uses the
    SAME LLM_PROVIDER / LLM_API_KEY / LLM_MODEL configuration. Doc extraction REQUIRES a key
    (unlike code extraction); a missing key fails loudly here, never silently drops docs.
    """
    import os

    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    default_base, default_key, default_model = _PROVIDER_DEFAULTS.get(
        provider, _PROVIDER_DEFAULTS["ollama"]
    )
    base_url = os.environ.get("LLM_BASE_URL", default_base)
    api_key = os.environ.get("LLM_API_KEY", default_key)
    if not api_key:
        raise RuntimeError(f"LLM_API_KEY is required for docify provider {provider!r}")
    model = os.environ.get("LLM_MODEL", default_model)
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8192"))
    return provider, base_url, api_key, model, max_tokens


__all__ = [
    "BACKEND_NAME",
    "DocExtractFn",
    "make_graphify_doc_extract",
    "resolve_endpoint",
]
