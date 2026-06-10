from agentic_kb_builder.domain.content_hasher import (
    content_hash,
    normalize_code,
    normalize_text,
    normalized_content_hash,
)

COMPOSED = "caf\u00e9"
DECOMPOSED = "cafe\u0301"


def test_normalize_text_line_endings_and_whitespace() -> None:
    assert normalize_text("a\r\nb\r") == "a\nb\n"
    assert normalize_text("a  \nb\t\n") == "a\nb\n"
    assert normalize_text("\n\na\nb\n\n\n") == "a\nb\n"
    assert normalize_text("") == ""
    assert normalize_text("   \n  \n") == ""


def test_normalize_text_unicode_nfc_equivalence() -> None:
    assert COMPOSED != DECOMPOSED
    assert normalize_text(COMPOSED) == normalize_text(DECOMPOSED)
    assert normalized_content_hash(COMPOSED) == normalized_content_hash(DECOMPOSED)


def test_normalized_content_hash_is_deterministic() -> None:
    variants = ["x = 1\r\ny = 2  \n", "x = 1\ny = 2\n", "\nx = 1\ny = 2\n\n"]
    digests = {normalized_content_hash(v) for v in variants}
    assert len(digests) == 1
    assert digests == {content_hash("x = 1\ny = 2\n")}


def test_normalize_code_is_conservative() -> None:
    assert normalize_code("a  \r\nb\r") == "a  \nb\n"
    # trailing whitespace and unicode form are preserved for exact code evidence
    assert normalize_code(DECOMPOSED + "  ") == DECOMPOSED + "  "
    assert normalize_code("a\nb") == "a\nb"
