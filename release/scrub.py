#!/usr/bin/env python3
"""Strip internal development references from a product source tree.

Run by make_prod.sh against a checked-out prod tree. Removes references to the internal
development process (decision-record IDs, build-unit IDs, AI-assisted-build mentions) so the
shipped product reveals nothing about how it was built. Touches ONLY comments, docstrings and
markdown prose — never code identifiers — so behaviour is unchanged.

Two passes:
  A. Delete decision/build-unit reference TOKENS (e.g. "ADR-0028", "PR-36", "(ADR-0011 ...)")
     anywhere in a text file. These literals never occur in executable code, so removal is safe.
  B. Drop whole comment / markdown lines that mention the internal build process by name
     (the AI-assist tooling, the dogfooding corpus, the inspiration projects). Code lines are
     left intact; runtime model-provider names stay (they are product features, not process).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".sh", ".cfg", ".ini", ".txt"}

# Reference tokens that only ever appear in prose/comments — safe to delete globally.
_REF_TOKEN = re.compile(
    r"""\(?\b(?:ADR|PR)-\d{1,4}[a-z]?\b   # ADR-0028 / PR-36 / PR-7
        (?:[^)\n]*?\))?                    # optional trailing "...)" of a parenthetical
    """,
    re.VERBOSE,
)
_PHASE = re.compile(r"\bPhase\s+\d[AB]?\b")
# Whole comment/markdown lines mentioning the internal build process get dropped.
_PROCESS_WORDS = re.compile(
    r"\b(?:Claude\s*Code|Claude|Anthropic'?s\s+CLI|dogfood\w*|Headroom|Groq|wikif\w*|"
    r"Copilot\s+CLI|OpenCode|build\s+subagent\w*)\b",
    re.IGNORECASE,
)
_COMMENT_LINE = re.compile(r"^\s*#")
_TIDY = (
    (re.compile(r"\(\s*[;,]?\s*\)"), ""),   # empty parens left by token removal
    (re.compile(r"[ \t]{2,}"), " "),          # collapsed double spaces
    (re.compile(r"\s+([.,;:])"), r"\1"),     # space before punctuation
    (re.compile(r"[ \t]+$"), ""),             # trailing whitespace
)


def _tidy(line: str) -> str:
    for pat, repl in _TIDY:
        line = pat.sub(repl, line)
    return line


def _scrub_text(text: str, *, is_markdown: bool) -> str:
    out: list[str] = []
    for raw in text.splitlines():
        line = _REF_TOKEN.sub("", raw)
        line = _PHASE.sub("", line)
        is_comment = bool(_COMMENT_LINE.match(line))
        # Drop a comment / markdown-prose line that still names the internal build process.
        if (is_comment or is_markdown) and _PROCESS_WORDS.search(line):
            continue
        if line != raw:
            line = _tidy(line)
            # a comment that became empty after token removal is dropped entirely
            if is_comment and line.strip() in {"#", ""}:
                continue
        out.append(line)
    result = "\n".join(out)
    return result + "\n" if text.endswith("\n") else result


def scrub_file(path: Path) -> bool:
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    scrubbed = _scrub_text(original, is_markdown=path.suffix == ".md")
    if scrubbed != original:
        path.write_text(scrubbed, encoding="utf-8")
        return True
    return False


_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}


def _is_scrubbable(path: Path) -> bool:
    if any(part in _SKIP_DIRS for part in path.parts) or not path.is_file():
        return False
    return path.suffix in _TEXT_SUFFIXES or path.name == "Dockerfile"


def main(root: str) -> int:
    base = Path(root)
    changed = sum(1 for p in sorted(base.rglob("*")) if _is_scrubbable(p) and scrub_file(p))
    print(f"scrubbed {changed} files under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
