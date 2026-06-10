"""Deterministic chunker: same normalized text => same chunks, on any machine.

Splits on blank-line paragraph boundaries and packs paragraphs into chunks of
at most MAX_CHUNK_CHARS; a single oversized paragraph is hard-split. Runs of
3+ newlines collapse to one "\n\n" joiner, so chunk text is NOT always a
verbatim substring of the source — verbatim checks (e.g. fact quotes) must use
the original normalized text. Changing any of this behavior must bump
contracts.versions.CHUNKER_VERSION because the chunker output feeds the
chunk-summary cache key.
"""

from common.hashing import content_hash
from common.logging import get_logger
from contracts.artifact_schemas import Chunk

logger = get_logger("kb_builder.wikify.chunker")

MAX_CHUNK_CHARS = 4000


def _paragraphs(text: str) -> list[str]:
    parts = [part.strip("\n") for part in text.split("\n\n")]
    return [part for part in parts if part.strip()]


def _hard_split(paragraph: str) -> list[str]:
    return [
        paragraph[start : start + MAX_CHUNK_CHARS]
        for start in range(0, len(paragraph), MAX_CHUNK_CHARS)
    ]


def chunk_text(text: str) -> list[Chunk]:
    pieces: list[str] = []
    for paragraph in _paragraphs(text):
        if len(paragraph) > MAX_CHUNK_CHARS:
            pieces.extend(_hard_split(paragraph))
        else:
            pieces.append(paragraph)

    chunks: list[Chunk] = []
    current: list[str] = []
    current_len = 0
    for piece in pieces:
        # +2 accounts for the "\n\n" joiner restored between packed paragraphs.
        added = len(piece) if not current else len(piece) + 2
        if current and current_len + added > MAX_CHUNK_CHARS:
            body = "\n\n".join(current)
            chunks.append(Chunk(index=len(chunks), text=body, chunk_hash=content_hash(body)))
            current = []
            current_len = 0
            added = len(piece)
        current.append(piece)
        current_len += added
    if current:
        body = "\n\n".join(current)
        chunks.append(Chunk(index=len(chunks), text=body, chunk_hash=content_hash(body)))

    logger.info("event=chunked text_chars=%d chunks=%d", len(text), len(chunks))
    return chunks
