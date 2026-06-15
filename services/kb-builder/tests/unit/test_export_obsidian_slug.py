"""Unit tests for the Obsidian exporter's slug function (no DB)."""

from agentic_kb_builder.export_obsidian import slugify


def test_slug_is_filesystem_safe() -> None:
    # Path separators and other unsafe chars never survive — they are non-alnum
    # and collapse to a single hyphen.
    slug = slugify("src/foo/bar.py: the Service", fallback="fb")
    assert "/" not in slug
    assert "\\" not in slug
    assert ":" not in slug
    assert slug == "src-foo-bar-py-the-service"


def test_slug_is_deterministic() -> None:
    assert slugify("Hello World", fallback="fb") == slugify("Hello World", fallback="fb")
    assert slugify("Hello World", fallback="fb") == "hello-world"


def test_slug_lowercases_and_collapses_runs() -> None:
    assert slugify("  Multiple   Spaces!!! ", fallback="fb") == "multiple-spaces"


def test_slug_unicode_is_transliterated() -> None:
    # NFKD strips accents to ASCII; café -> cafe.
    assert slugify("Café Münchën", fallback="fb") == "cafe-munchen"


def test_slug_falls_back_when_nothing_printable() -> None:
    # All-symbol / all-non-ascii titles leave nothing — fall back deterministically.
    assert slugify("日本語", fallback="abc123") == "abc123"
    assert slugify("!!! @@@ ###", fallback="abc123") == "abc123"
    assert slugify("", fallback="abc123") == "abc123"


def test_slug_is_length_bounded() -> None:
    slug = slugify("word " * 100, fallback="fb")
    assert len(slug) <= 80
    # No trailing hyphen from the truncation.
    assert not slug.endswith("-")
