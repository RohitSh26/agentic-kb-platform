"""Local docify smoke test: run document extraction on one file through a real LLM and print it.

Validate the docify step end to
end without a database, the cloud beyond the model call, or a build — it defaults to a local
Ollama server. Run it from `services/kb-builder`:

    uv run python -m agentic_kb_builder.try_docify <path-to-a-file>

Examples (paths are relative to services/kb-builder; use any file you like):

    uv run python -m agentic_kb_builder.try_docify ../../README.md
    uv run python -m agentic_kb_builder.try_docify src/agentic_kb_builder/health.py

Pick the model/provider with env vars (Ollama by default):

    export LLM_MODEL=llama3.1            # or any model you `ollama pull`ed
    LLM_PROVIDER=groq  LLM_API_KEY=...   # Groq / OpenAI / Azure also supported
"""

import argparse
import asyncio
import sys
from pathlib import Path

from agentic_kb_builder.docify import DocExtractor
from agentic_kb_builder.domain import NormalizedContent
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.domain.source_records import SourceRef


async def _run(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        print(f"{path}: file is empty", file=sys.stderr)
        return 1
    content = NormalizedContent(
        source=SourceRef(
            source_type="github_doc",
            source_uri=path.resolve().as_uri(),
            source_version="local",
            path=str(path),
        ),
        text=text,
        content_hash=content_hash(text),
    )
    extractor = DocExtractor.from_env()
    print(f"model: {extractor.model_name}   ({len(text)} chars)\n")
    result = await extractor.extract(content)

    for kind in ("summary", "concept", "source_backed_fact"):
        items = [draft for draft in result.artifacts if draft.artifact_type == kind]
        print(f"== {kind} ({len(items)}) ==")
        for draft in items:
            label = f"[{draft.title}] " if draft.title else ""
            preview = " ".join(draft.body_text.split())
            print(f"  - {label}{preview[:220]}")
        print()
    print(
        "(a concept whose supporting sentence is a verbatim substring of the source becomes a "
        "citable source_backed_fact; otherwise it stays an interpreted concept — invariant 7)"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("file", type=Path, help="path to a text/markdown/code file to docify")
    args = parser.parse_args()
    if not args.file.is_file():
        print(f"not a file: {args.file}", file=sys.stderr)
        return 2
    return asyncio.run(_run(args.file))


if __name__ == "__main__":
    raise SystemExit(main())
