"""`export_obsidian` — render the built KB as a browsable Obsidian vault.

Nodes (knowledge_artifact) become one Markdown note each; edges (knowledge_edge)
become Obsidian `[[wikilinks]]` so the graph can be explored as linked notes
instead of SQL or Graphviz. Read-only over Postgres; writes nothing back.

Usage (from services/kb-builder, with a migrated DATABASE_URL set):

    uv run python -m agentic_kb_builder.export_obsidian --out ./vault [--kb-version X]

Without `--kb-version` the exporter targets the active kb_version. Artifacts and
edges are scoped by interval membership — a row belongs to a version iff
`valid_from_seq <= S < invalidated_at_seq` where S is that run's `build_seq` — NOT
by label equality. (Label equality is wrong post-: an unchanged source's
artifacts keep the label of the build that first wrote them, while later builds
advance the active label, so a label filter silently drops the carried-over nodes
and edges.) Output is deterministic (stable ordering + stable slugs), so re-running
on the same KB yields byte-identical files; the out dir is cleaned first (idempotent).
"""

import argparse
import asyncio
import re
import sys
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentic_kb_builder.infrastructure.postgres.models import (
    KbBuildRun,
    KnowledgeArtifact,
    KnowledgeEdge,
    SourceItem,
)
from agentic_kb_builder.infrastructure.postgres.session import create_engine, create_session_factory
from agentic_kb_builder.structured_logging import configure_logging, get_logger

logger = get_logger(__name__)

# Map artifact_type -> vault folder. Unknown types fall back to a slug of the type
# (see _type_folder) so the export never drops an artifact.
_TYPE_FOLDERS: dict[str, str] = {
    "concept": "concepts",
    "summary": "docs",
    "doc": "docs",
    "code_file": "code",
    "code": "code",
    "commit": "commits",
    "work_item": "work-items",
}

_SLUG_MAX_LEN = 80
_PLACEHOLDER_BODY = "_(no body text)_"


def slugify(value: str, *, fallback: str) -> str:
    """Deterministic, filesystem-safe slug of a title.

    NFKD-normalise unicode to ASCII, lowercase, collapse any run of
    non-alphanumeric characters to a single hyphen, and trim. Path separators and
    other unsafe characters can never survive (they are non-alphanumeric). Returns
    `fallback` when nothing printable remains (e.g. an all-unicode/all-symbol
    title), so a slug is always non-empty and stable for the same input.
    """
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    slug = slug[:_SLUG_MAX_LEN].strip("-")
    return slug or fallback


def _short_id(artifact_id: object) -> str:
    """First hex group of a UUID — a stable, collision-resistant suffix/fallback."""
    return str(artifact_id).split("-", 1)[0]


def _type_folder(artifact_type: str) -> str:
    return _TYPE_FOLDERS.get(artifact_type, slugify(artifact_type, fallback="misc"))


@dataclass
class _Note:
    """Everything needed to render one artifact's note and resolve links to it."""

    artifact_id: str
    artifact_type: str
    title: str
    slug: str
    folder: str
    body_text: str | None
    kb_version: str
    source_uri: str | None
    acl_teams: list[str]
    trust: str | None


@dataclass
class _ExportResult:
    notes_written: int = 0
    by_type: dict[str, int] = field(default_factory=dict)


def _assign_slugs(artifacts: list[KnowledgeArtifact]) -> dict[str, _Note]:
    """Build a note per artifact with a unique slug.

    Iterate in a stable order (already sorted by caller); on a slug collision
    within a type folder, append a short artifact-id suffix so every note lands at
    a distinct, deterministic path.
    """
    notes: dict[str, _Note] = {}
    seen: dict[tuple[str, str], int] = defaultdict(int)
    for artifact in artifacts:
        artifact_id = str(artifact.artifact_id)
        short = _short_id(artifact_id)
        title = artifact.title or short
        base_slug = slugify(title, fallback=short)
        folder = _type_folder(artifact.artifact_type)
        key = (folder, base_slug)
        slug = f"{base_slug}-{short}" if seen[key] else base_slug
        seen[key] += 1
        notes[artifact_id] = _Note(
            artifact_id=artifact_id,
            artifact_type=artifact.artifact_type,
            title=title,
            slug=slug,
            folder=folder,
            body_text=artifact.body_text,
            kb_version=artifact.kb_version,
            source_uri=None,  # filled in by caller from source_item join
            acl_teams=list(artifact.acl_teams),
            trust=None,  # frontmatter `trust` is derived from incident edges below
        )
    return notes


def _yaml_scalar(value: str) -> str:
    """Quote a YAML scalar so titles/uris with colons or special chars stay valid."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_scalar(v) for v in values) + "]"


def _render_frontmatter(note: _Note) -> list[str]:
    lines = [
        "---",
        f"id: {_yaml_scalar(note.artifact_id)}",
        f"type: {_yaml_scalar(note.artifact_type)}",
        f"title: {_yaml_scalar(note.title)}",
        f"kb_version: {_yaml_scalar(note.kb_version)}",
        f"source_uri: {_yaml_scalar(note.source_uri) if note.source_uri else 'null'}",
        f"acl_teams: {_yaml_list(note.acl_teams)}",
        f"trust: {_yaml_scalar(note.trust) if note.trust else 'null'}",
        "---",
    ]
    return lines


def _wikilink(note: _Note) -> str:
    # Use folder/slug as the link target with the title as display text, so links
    # resolve unambiguously even when two folders share a slug, and read nicely.
    return f"[[{note.folder}/{note.slug}|{note.title}]]"


def _render_note(
    note: _Note,
    *,
    outgoing: list[tuple[KnowledgeEdge, _Note | None]],
    incoming: list[tuple[KnowledgeEdge, _Note | None]],
) -> str:
    lines = _render_frontmatter(note)
    lines.append("")
    lines.append(f"# {note.title}")
    lines.append("")
    body = note.body_text if note.body_text else _PLACEHOLDER_BODY
    lines.append(body.rstrip("\n"))
    lines.append("")
    lines.append("## Links")
    lines.append("")
    if not outgoing and not incoming:
        lines.append("_(no links)_")
        lines.append("")
        return "\n".join(lines) + "\n"

    for edge, target in outgoing:
        label = f"{edge.edge_type} [{edge.trust_class}]"
        link = _wikilink(target) if target else f"`{edge.to_artifact_id}` (out of scope)"
        lines.append(f"- {label} → {link}")
    for edge, source in incoming:
        label = f"{edge.edge_type} [{edge.trust_class}]"
        link = _wikilink(source) if source else f"`{edge.from_artifact_id}` (out of scope)"
        lines.append(f"- {link} {label} →")
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_index(result: _ExportResult, kb_version: str) -> str:
    lines = [
        "---",
        "title: KB Map of Content",
        f"kb_version: {_yaml_scalar(kb_version)}",
        "---",
        "",
        "# Knowledge Base — Map of Content",
        "",
        f"kb_version: `{kb_version}` · {result.notes_written} notes",
        "",
        "## Types",
        "",
    ]
    for artifact_type in sorted(result.by_type):
        folder = _type_folder(artifact_type)
        count = result.by_type[artifact_type]
        lines.append(f"- **{artifact_type}** ({count}) → `{folder}/`")
    lines.append("")
    return "\n".join(lines) + "\n"


async def _resolve_target(session: AsyncSession, kb_version: str | None) -> tuple[str, int] | None:
    """Resolve the export target to (kb_version label, build_seq cutoff S).

    Default (kb_version=None) is the active run; an explicit label selects that
    historical run. Returns None when no such run exists. A label is not unique
    (only build_seq is) — a retried build can reuse it — so for an explicit label
    we take the highest build_seq, the only meaningful cutoff. The active case has
    exactly one row (partial unique index), so the order is a no-op there.
    """
    stmt = select(KbBuildRun.kb_version, KbBuildRun.build_seq)
    stmt = stmt.where(
        KbBuildRun.status == "active" if kb_version is None else KbBuildRun.kb_version == kb_version
    ).order_by(KbBuildRun.build_seq.desc())
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return str(row[0]), int(row[1])


def _is_member(
    model: type[KnowledgeArtifact] | type[KnowledgeEdge], build_seq: int
) -> ColumnElement[bool]:
    """ interval-membership predicate: live at cutoff S=build_seq."""
    return (model.valid_from_seq <= build_seq) & (
        model.invalidated_at_seq.is_(None) | (model.invalidated_at_seq > build_seq)
    )


async def _load_artifacts(session: AsyncSession, build_seq: int) -> list[KnowledgeArtifact]:
    # Scope by.
    # Stable order: artifact_type then title then id, so re-runs are identical.
    result = await session.execute(
        select(KnowledgeArtifact)
        .where(_is_member(KnowledgeArtifact, build_seq))
        .order_by(
            KnowledgeArtifact.artifact_type,
            KnowledgeArtifact.title,
            KnowledgeArtifact.artifact_id,
        )
    )
    return list(result.scalars().all())


async def _load_source_uris(
    session: AsyncSession, source_ids: Sequence[uuid.UUID]
) -> dict[str, str]:
    if not source_ids:
        return {}
    result = await session.execute(
        select(SourceItem.source_id, SourceItem.source_uri).where(
            SourceItem.source_id.in_(source_ids)
        )
    )
    return {str(sid): uri for sid, uri in result.tuples()}


async def _load_edges(session: AsyncSession, build_seq: int) -> list[KnowledgeEdge]:
    result = await session.execute(
        select(KnowledgeEdge)
        .where(_is_member(KnowledgeEdge, build_seq))
        .order_by(
            KnowledgeEdge.from_artifact_id,
            KnowledgeEdge.to_artifact_id,
            KnowledgeEdge.edge_type,
            KnowledgeEdge.edge_id,
        )
    )
    return list(result.scalars().all())


def _clean_out_dir(out: Path) -> None:
    """Remove any previously-exported notes so the export is idempotent.

    Only deletes `index.md` and `*.md` under type folders we manage, plus those
    now-empty folders — never the user's whole directory.
    """
    index = out / "index.md"
    if index.exists():
        index.unlink()
    for child in sorted(out.iterdir()) if out.exists() else []:
        if not child.is_dir():
            continue
        for note in sorted(child.glob("*.md")):
            note.unlink()
        if not any(child.iterdir()):
            child.rmdir()


async def export_obsidian(
    session: AsyncSession, *, out: Path, kb_version: str, build_seq: int
) -> _ExportResult:
    """Write the Obsidian vault for `kb_version` (members at cutoff S=build_seq)."""
    logger.info(
        "event=obsidian_export_started kb_version=%s build_seq=%d out=%s",
        kb_version,
        build_seq,
        out,
    )
    artifacts = await _load_artifacts(session, build_seq)
    notes = _assign_slugs(artifacts)

    source_ids = [a.source_id for a in artifacts]
    source_uris = await _load_source_uris(session, source_ids)
    for artifact in artifacts:
        note = notes[str(artifact.artifact_id)]
        note.source_uri = source_uris.get(str(artifact.source_id))

    edges = await _load_edges(session, build_seq)
    outgoing: dict[str, list[tuple[KnowledgeEdge, _Note | None]]] = defaultdict(list)
    incoming: dict[str, list[tuple[KnowledgeEdge, _Note | None]]] = defaultdict(list)
    for edge in edges:
        frm = str(edge.from_artifact_id)
        to = str(edge.to_artifact_id)
        # An edge endpoint may fall outside this kb_version's artifacts; record it
        # as "out of scope" rather than dropping the relationship silently.
        outgoing[frm].append((edge, notes.get(to)))
        incoming[to].append((edge, notes.get(frm)))

    out.mkdir(parents=True, exist_ok=True)
    _clean_out_dir(out)

    result = _ExportResult()
    by_type: dict[str, int] = defaultdict(int)
    for artifact in artifacts:
        note = notes[str(artifact.artifact_id)]
        folder = out / note.folder
        folder.mkdir(parents=True, exist_ok=True)
        rendered = _render_note(
            note,
            outgoing=outgoing.get(note.artifact_id, []),
            incoming=incoming.get(note.artifact_id, []),
        )
        (folder / f"{note.slug}.md").write_text(rendered, encoding="utf-8")
        result.notes_written += 1
        by_type[note.artifact_type] += 1

    result.by_type = dict(by_type)
    (out / "index.md").write_text(_render_index(result, kb_version), encoding="utf-8")

    logger.info(
        "event=obsidian_export_finished kb_version=%s notes=%d edges=%d out=%s",
        kb_version,
        result.notes_written,
        len(edges),
        out,
    )
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export_obsidian", description=__doc__.splitlines()[0] if __doc__ else ""
    )
    parser.add_argument("--out", required=True, help="output directory for the Obsidian vault")
    parser.add_argument(
        "--kb-version",
        default=None,
        help="kb_version to export (default: the active kb_version)",
    )
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    configure_logging()
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            target = await _resolve_target(session, args.kb_version)
            if target is None:
                logger.error("event=obsidian_export_no_target kb_version=%s", args.kb_version)
                missing = args.kb_version or "active"
                print(f"no {missing} kb_version; pass --kb-version to export a specific one")
                return 1
            kb_version, build_seq = target
            result = await export_obsidian(
                session, out=Path(args.out), kb_version=kb_version, build_seq=build_seq
            )
    finally:
        await engine.dispose()
    print(f"kb_version : {kb_version}")
    print(f"notes      : {result.notes_written}")
    print(f"out        : {args.out}")
    return 0


def main() -> int:
    return asyncio.run(_main(_parse_args(sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
