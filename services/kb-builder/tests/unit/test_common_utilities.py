import logging

from agentic_kb_builder.domain.content_hasher import content_hash
from agentic_kb_builder.structured_logging import get_logger


def test_content_hash_is_deterministic() -> None:
    assert content_hash("hello") == content_hash("hello")
    assert content_hash(b"hello") == content_hash("hello")
    assert (
        content_hash("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert content_hash("hello") != content_hash("hello ")


def test_get_logger_returns_logger() -> None:
    assert isinstance(get_logger("kb.test"), logging.Logger)
