"""Load reviewer instructions from the canonical agents/*.md manifests at RUNTIME.

The manifests stay the single source of truth (PR-40 scope): frontmatter is
parsed off and only the instruction body is used. No prompt text is copied into
this service.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from review_panel.domain.errors import ReviewPanelError
from review_panel.domain.findings import PANEL_LENSES

#: lens -> manifest file in the canonical agents/ directory
MANIFEST_FILES: Mapping[str, str] = MappingProxyType(
    {lens: f"{lens}_reviewer.md" for lens in PANEL_LENSES}
)
SYNTHESIZER_MANIFEST = "code_reviewer.md"

#: Runtime output scaffolding (schema shape, not reviewer instruction text —
#: the manifests reference review_findings_v1 by name only).
OUTPUT_FORMAT_INSTRUCTION = (
    "OUTPUT FORMAT: respond with ONLY one JSON object matching review_findings_v1 — "
    '{"schema_version": "1.0.0", "verdict": "approve" | "request_changes", '
    '"findings": [{"severity": "blocker" | "major" | "minor" | "note", '
    '"finding": "<what and where>", "evidence_ids": ["<file path or diff hunk>"]}], '
    '"open_questions": ["<question>"]}. '
    "No prose, no markdown fences, no extra keys."
)


class PromptLoadError(ReviewPanelError):
    pass


@dataclass(frozen=True)
class PanelPrompts:
    reviewers: Mapping[str, str]  # lens -> instruction body
    synthesizer: str


def strip_frontmatter(text: str) -> str:
    """Return the manifest body with the leading YAML frontmatter block removed."""
    if not text.startswith("---\n"):
        return text.strip()
    end = text.find("\n---\n", len("---\n") - 1)
    if end == -1:
        raise PromptLoadError("manifest frontmatter never closes")
    return text[end + len("\n---\n") :].strip()


def _load_body(agents_dir: Path, filename: str) -> str:
    path = agents_dir / filename
    try:
        body = strip_frontmatter(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromptLoadError(f"cannot read manifest {path}: {exc}") from exc
    if not body:
        raise PromptLoadError(f"manifest {path} has an empty instruction body")
    return body


def load_panel_prompts(agents_dir: Path) -> PanelPrompts:
    reviewers = {
        lens: _load_body(agents_dir, filename) for lens, filename in MANIFEST_FILES.items()
    }
    return PanelPrompts(
        reviewers=MappingProxyType(reviewers),
        synthesizer=_load_body(agents_dir, SYNTHESIZER_MANIFEST),
    )
