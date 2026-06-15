"""Persistent, file-backed SearchClient for the local development loop (ADR-0017).

Azure AI Search persists the index across builds; the in-memory FakeSearchClient
does not. Running the documented incremental rebuild as a fresh process therefore
started with an empty index, and because an unchanged build upserts nothing
(invariant 4's cost discipline extends to the index), the post-build consistency
gate saw every carried-forward member as ``missing`` and blocked activation.

This client gives the local loop the SAME persistence semantics as Azure behind
the SAME ``SearchClient`` interface: the projection lives in a JSON file, so an
unchanged incremental rebuild still validates against the carried-forward
membership. The file is a derived, rebuildable projection of Postgres — never
truth (invariant 1); delete it (or recreate the database) to force a clean
reprojection on the next build.
"""

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from agentic_kb_builder.indexing.search_document import IndexState, SearchDoc
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)

_PERSIST_VERSION = 1


class LocalFileSearchClient:
    """SearchClient whose index is a JSON file, so it survives across processes.

    Write-through on every mutation and an atomic replace keep the file a faithful
    mirror of the in-memory state even if a build is interrupted mid-write.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._docs: dict[str, SearchDoc] = _load(path)

    async def upsert_docs(self, docs: Sequence[SearchDoc]) -> int:
        for doc in docs:
            self._docs[doc.doc_id] = doc
        self._persist()
        logger.info(
            "event=search_local_upsert count=%d total=%d path=%s",
            len(docs),
            len(self._docs),
            self._path,
        )
        return len(docs)

    async def delete_docs(self, doc_ids: Sequence[str]) -> int:
        removed = sum(1 for doc_id in doc_ids if self._docs.pop(doc_id, None) is not None)
        self._persist()
        logger.info(
            "event=search_local_delete requested=%d removed=%d total=%d",
            len(doc_ids),
            removed,
            len(self._docs),
        )
        return removed

    async def fetch_index_state(self) -> IndexState:
        return IndexState(docs={doc_id: doc.artifact_hash for doc_id, doc in self._docs.items()})

    def _persist(self) -> None:
        payload = {
            "persist_version": _PERSIST_VERSION,
            "docs": {doc_id: doc.model_dump(mode="json") for doc_id, doc in self._docs.items()},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: a temp file in the same directory then os.replace, so an
        # interrupted build never leaves a half-written index file behind.
        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=".tmp-search-index-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


def _load(path: Path) -> dict[str, SearchDoc]:
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return {}
    mapping = cast("dict[str, Any]", parsed)
    docs_raw = cast("dict[str, Any]", mapping.get("docs", {}))
    loaded = {
        str(doc_id): SearchDoc.model_validate(payload) for doc_id, payload in docs_raw.items()
    }
    logger.info("event=search_local_loaded count=%d path=%s", len(loaded), path)
    return loaded
