"""LocalFileSearchClient persists the projection across instances (ADR-0017).

The bug it fixes: the in-memory FakeSearchClient starts empty in every new
process, so the documented incremental rebuild (a fresh `python -m ... build`
invocation) found the index empty and the consistency gate reported every
carried-forward member as missing. A second client instance over the same file
stands in for that second process.
"""

import uuid
from pathlib import Path

from agentic_kb_builder.indexing.search_document import SearchDoc
from agentic_kb_builder.infrastructure.local_search import LocalFileSearchClient


def _doc(artifact_hash: str, *, embedding: tuple[float, ...] | None = None) -> SearchDoc:
    artifact_id = uuid.uuid4()
    return SearchDoc(
        doc_id=SearchDoc.doc_id_for(artifact_id),
        artifact_id=artifact_id,
        artifact_type="summary",
        source_type="github_code",
        source_uri="https://example.test/x",
        title="t",
        body_text="b",
        kb_version="kb",
        knowledge_kind="source_backed",
        authority_score=0.5,
        freshness_score=0.5,
        artifact_hash=artifact_hash,
        embedding=embedding,
    )


async def test_index_state_survives_a_new_client_instance(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    doc = _doc("hash-1", embedding=(0.1, 0.2, 0.3))

    writer = LocalFileSearchClient(path)
    assert await writer.upsert_docs([doc]) == 1

    # A fresh instance == a fresh process: it must see the persisted doc + hash.
    reader = LocalFileSearchClient(path)
    state = await reader.fetch_index_state()
    assert state.docs == {doc.doc_id: "hash-1"}


async def test_unchanged_rebuild_upserts_nothing_yet_index_stays_complete(
    tmp_path: Path,
) -> None:
    # Mirrors the incremental flow: run 1 populates the file; run 2 (a new client)
    # upserts nothing, but the persisted index still holds the carried-forward docs.
    path = tmp_path / "index.json"
    docs = [_doc(f"h{i}") for i in range(5)]
    await LocalFileSearchClient(path).upsert_docs(docs)

    second_run = LocalFileSearchClient(path)
    # No changes ⇒ no upserts on the second run.
    state = await second_run.fetch_index_state()
    assert set(state.docs) == {d.doc_id for d in docs}


async def test_delete_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    keep, drop = _doc("keep"), _doc("drop")
    await LocalFileSearchClient(path).upsert_docs([keep, drop])

    assert await LocalFileSearchClient(path).delete_docs([drop.doc_id]) == 1

    state = await LocalFileSearchClient(path).fetch_index_state()
    assert set(state.docs) == {keep.doc_id}


async def test_missing_file_loads_as_empty(tmp_path: Path) -> None:
    client = LocalFileSearchClient(tmp_path / "does-not-exist.json")
    assert (await client.fetch_index_state()).docs == {}
