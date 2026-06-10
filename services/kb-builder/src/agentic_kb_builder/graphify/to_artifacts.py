"""FileGraph -> canonical code artifact drafts.

Symbols and tests carry the exact snippet plus a 1-based inclusive line span so
L2 evidence can return precise text at a source version. code_file and endpoint
drafts are pointer-only (body_text=None) per the raw-storage policy: the file
content lives at the source, reachable via source_item.
"""

from agentic_kb_builder.domain import CodeArtifactDraft, FileGraph
from agentic_kb_builder.graphify.keys import endpoint_key, file_key, symbol_key, test_key
from agentic_kb_builder.structured_logging import get_logger

logger = get_logger(__name__)


def file_graph_to_artifacts(graph: FileGraph, *, file_text: str) -> tuple[CodeArtifactDraft, ...]:
    # Not splitlines(): that also splits on form feeds and unicode line
    # separators, silently shifting later line numbers and corrupting snippets.
    lines = file_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    drafts = [
        CodeArtifactDraft(key=file_key(graph.path), artifact_type="code_file", title=graph.path)
    ]
    for symbol in graph.symbols:
        drafts.append(
            CodeArtifactDraft(
                key=symbol_key(graph.path, symbol.name),
                artifact_type="code_symbol",
                title=symbol.name,
                body_text=_snippet(lines, symbol.span_start, symbol.span_end, graph.path),
                span_start=symbol.span_start,
                span_end=symbol.span_end,
            )
        )
    for test in graph.tests:
        drafts.append(
            CodeArtifactDraft(
                key=test_key(graph.path, test.name),
                artifact_type="test",
                title=test.name,
                body_text=_snippet(lines, test.span_start, test.span_end, graph.path),
                span_start=test.span_start,
                span_end=test.span_end,
            )
        )
    for endpoint in graph.endpoints:
        drafts.append(
            CodeArtifactDraft(
                key=endpoint_key(graph.path, endpoint.http_method, endpoint.route),
                artifact_type="endpoint",
                title=f"{endpoint.http_method} {endpoint.route}",
            )
        )
    logger.info("event=graphify_artifacts_drafted path=%s count=%d", graph.path, len(drafts))
    return tuple(drafts)


def _snippet(lines: list[str], span_start: int, span_end: int, path: str) -> str:
    # A span past EOF means the parsed graph does not match the fetched file
    # content; storing a truncated snippet would fabricate evidence (invariant 7).
    if span_end > len(lines):
        raise ValueError(f"span {span_start}-{span_end} exceeds {len(lines)} lines in {path}")
    return "\n".join(lines[span_start - 1 : span_end])
