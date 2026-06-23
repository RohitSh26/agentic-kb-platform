"""The durable model-output cache is an optimization, never load-bearing for build success.

If its storage is unavailable (tables not migrated, transient DB error) a get/put must DEGRADE —
return a miss / no-op and log once — never raise into the build. Pure unit test (no DB).
"""

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from agentic_kb_builder.domain.docify_artifacts import DocExtractionResult
from agentic_kb_builder.domain.embedding_port import EmbeddingResult
from agentic_kb_builder.infrastructure.postgres.durable_output_cache import (
    PostgresDurableOutputCache,
)

_DB_DOWN = OperationalError("SELECT 1", {}, Exception("connection refused"))


class _RaisingSession:
    async def __aenter__(self) -> "_RaisingSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        raise _DB_DOWN

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise _DB_DOWN

    async def commit(self) -> None:
        raise _DB_DOWN


def _cache_with_broken_storage() -> PostgresDurableOutputCache:
    cache = PostgresDurableOutputCache.__new__(PostgresDurableOutputCache)
    cache._degraded = False  # type: ignore[attr-defined]
    cache._factory = lambda: _RaisingSession()  # type: ignore[attr-defined]
    return cache


async def test_get_doc_extraction_degrades_to_miss() -> None:
    cache = _cache_with_broken_storage()
    assert await cache.get_doc_extraction("k") is None  # a miss, not an exception


async def test_put_doc_extraction_does_not_raise() -> None:
    cache = _cache_with_broken_storage()
    # the build calls this right after paying the model; it must never abort the build.
    await cache.put_doc_extraction(
        cache_key="k",
        input_hash="h",
        prompt_version="1",
        model_name="m",
        model_params_hash="p",
        output_schema_version="1",
        result=DocExtractionResult(artifacts=()),
    )


async def test_embedding_paths_degrade() -> None:
    cache = _cache_with_broken_storage()
    assert await cache.get_embedding(text_hash="t", embedding_model="e") is None
    await cache.put_embedding(
        text_hash="t", embedding_model="e", result=EmbeddingResult(embedding_hash="h", vector=[0.1])
    )


async def test_warning_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    cache = _cache_with_broken_storage()
    with caplog.at_level("WARNING"):
        await cache.get_doc_extraction("a")
        await cache.get_doc_extraction("b")
        await cache.put_embedding(
            text_hash="t", embedding_model="e",
            result=EmbeddingResult(embedding_hash="h", vector=[0.1]),
        )
    unavailable = [r for r in caplog.records if "durable_cache_unavailable" in r.getMessage()]
    assert len(unavailable) == 1  # loud once, then degrade silently
