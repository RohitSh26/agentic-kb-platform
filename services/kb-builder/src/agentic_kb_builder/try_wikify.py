"""Local wikify smoke test: run wikify on one file through a real LLM and print it.

Validate the wikify step end to end without a database, the cloud, or spend —
it defaults to a local Ollama server. Run it from `services/kb-builder`:

    uv run python -m agentic_kb_builder.try_wikify <path-to-a-file>

Examples (paths are relative to services/kb-builder; use any file you like):

    uv run python -m agentic_kb_builder.try_wikify ../../README.md
    uv run python -m agentic_kb_builder.try_wikify src/agentic_kb_builder/health.py

Pick the model/provider with env vars (Ollama by default):

    export LLM_MODEL=llama3.1            # or any model you `ollama pull`ed
    LLM_PROVIDER=groq  LLM_API_KEY=...   # Groq / OpenAI / Azure also supported
"""

import argparse
import asyncio
import sys
from pathlib import Path

from agentic_kb_builder.domain import NormalizedContent
from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.domain.source_records import SourceRef
from agentic_kb_builder.infrastructure.azure_openai.chat_model_client import ChatModelClient
from agentic_kb_builder.wikify.generate import WikifyGenerator


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
    client = ChatModelClient.from_env()
    print(f"model: {client.model_name}   ({len(text)} chars)\n")
    drafts = await WikifyGenerator(client).wikify(content)

    for kind in ("chunk", "summary", "concept", "source_backed_fact"):
        items = [draft for draft in drafts if draft.artifact_type == kind]
        print(f"== {kind} ({len(items)}) ==")
        for draft in items:
            label = f"[{draft.title}] " if draft.title else ""
            preview = " ".join(draft.body_text.split())
            print(f"  - {label}{preview[:220]}")
        print()
    print(
        "(facts whose quote was not found verbatim in the source were dropped — "
        "watch for event=wikify_fact_dropped warnings above)"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("file", type=Path, help="path to a text/markdown/code file to wikify")
    args = parser.parse_args()
    if not args.file.is_file():
        print(f"not a file: {args.file}", file=sys.stderr)
        return 2
    return asyncio.run(_run(args.file))


if __name__ == "__main__":
    raise SystemExit(main())
