"""DocExtractor: one document -> Graphify LLM extraction -> mapped doc artifacts (artifacts-only).

Implements the build runner's DocExtractor seam (ADR-0023). The generation-cache gate lives
in the runner, so this is only reached on a cache miss; it makes exactly one model call per
changed document. The Graphify extraction function is an INJECTABLE dependency (default = the
live ``graphify_doc_extract``) so unit/integration tests inject a captured-fixture function and
never hit a live LLM (the hermetic-test seam, ADR-0023 §5).
"""

import hashlib

from agentic_kb_builder.docify.docify_backend import map_doc_extraction
from agentic_kb_builder.docify.extract_fn import (
    DocExtractFn,
    make_graphify_doc_extract,
    resolve_endpoint,
)
from agentic_kb_builder.domain import DocExtractionResult, NormalizedContent
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


def _doc_path(content: NormalizedContent) -> str:
    """Repo-relative path Graphify writes/reports the document under.

    Falls back to a stable per-source filename when the source has no path (e.g. an
    ado_card), so Graphify always has a real file to read and ``source_file`` comes back
    matching ``known_doc_path`` in the mapper."""
    path = content.source.path
    if path:
        return path
    return f"doc-{content.content_hash[:16]}.md"


class DocExtractor:
    """Graphify-backed document extractor; emits our doc artifacts + edges."""

    def __init__(
        self,
        extract_fn: DocExtractFn,
        *,
        model_name: str,
        model_params_hash: str,
    ) -> None:
        self._extract_fn = extract_fn
        self._model_name = model_name
        self._model_params_hash = model_params_hash

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_params_hash(self) -> str:
        return self._model_params_hash

    @classmethod
    def from_env(cls) -> "DocExtractor":
        """Construct the real DocExtractor from the same env as ChatModelClient.from_env.

        Resolves the OpenAI-compatible endpoint (LLM_PROVIDER / LLM_API_KEY / LLM_MODEL),
        registers the Graphify backend, and derives a deterministic model identity for the
        generation-cache key. The API key is never stored on the instance or logged."""
        endpoint = resolve_endpoint()
        extract_fn = make_graphify_doc_extract(endpoint)
        model_name = f"{endpoint.provider}:{endpoint.model}"
        model_params_hash = hashlib.sha256(
            f"{model_name}|temp=0|max_tokens={endpoint.max_tokens}|backend=graphify_doc".encode()
        ).hexdigest()[:16]
        return cls(extract_fn, model_name=model_name, model_params_hash=model_params_hash)

    async def extract(self, content: NormalizedContent) -> DocExtractionResult:
        """Extract one document and map it to our trust contract.

        One LLM call (via the injected function), then the PURE mapper re-derives trust.
        ACL is NOT applied here — the write path stamps the source ACL onto every row."""
        doc_path = _doc_path(content)
        logger.info(
            "event=docify_started source_uri=%s doc_path=%s model=%s",
            content.source.source_uri,
            doc_path,
            self._model_name,
        )
        data = await self._extract_fn(text=content.text, doc_path=doc_path)
        result = map_doc_extraction(
            data,
            source_text=content.text,
            known_doc_path=doc_path,
        )
        logger.info(
            "event=docify_generated source_uri=%s artifacts=%d",
            content.source.source_uri,
            len(result.artifacts),
        )
        return result


__all__ = ["DocExtractor"]
