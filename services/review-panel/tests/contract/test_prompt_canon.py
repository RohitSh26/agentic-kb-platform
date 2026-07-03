"""Prompts come from the canonical agents/*.md manifests at RUNTIME (PR-40).

The manifests stay the single source of truth: this suite loads the real five
files from the repo checkout and pins that only the instruction BODY is used
(frontmatter parsed off, nothing copied into the service).
"""

from panel_test_support import AGENTS_DIR

from review_panel.domain.findings import PANEL_LENSES
from review_panel.graph.prompts import MANIFEST_FILES, SYNTHESIZER_MANIFEST, load_panel_prompts

FRONTMATTER_KEYS = ("name:", "version:", "allowed_tools:", "output_schema:", "max_context_")


def test_all_five_canonical_manifests_load() -> None:
    prompts = load_panel_prompts(AGENTS_DIR)
    assert set(prompts.reviewers) == set(PANEL_LENSES)
    assert MANIFEST_FILES == {
        "bug": "bug_reviewer.md",
        "security": "security_reviewer.md",
        "quality": "quality_reviewer.md",
        "test_coverage": "test_coverage_reviewer.md",
    }
    assert SYNTHESIZER_MANIFEST == "code_reviewer.md"


def test_bodies_are_instructions_with_frontmatter_stripped() -> None:
    prompts = load_panel_prompts(AGENTS_DIR)
    for lens, body in {**dict(prompts.reviewers), "synthesizer": prompts.synthesizer}.items():
        assert len(body) > 100, f"{lens} body suspiciously short"
        assert not body.startswith("---"), f"{lens} frontmatter not stripped"
        for key in FRONTMATTER_KEYS:
            assert not any(line.startswith(key) for line in body.splitlines()), (
                f"{lens} body still carries frontmatter key {key}"
            )


def test_no_prompt_text_is_copied_into_the_service() -> None:
    """The service source must not embed manifest instruction text."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "review_panel"
    prompts = load_panel_prompts(AGENTS_DIR)
    first_sentences = [body.split("\n")[0][:60] for body in prompts.reviewers.values()]
    first_sentences.append(prompts.synthesizer.split("\n")[0][:60])
    for path in src.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for sentence in first_sentences:
            assert sentence not in source, f"{path} embeds manifest text: {sentence!r}"
