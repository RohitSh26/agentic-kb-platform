"""Deterministic content hashing for cache keys and incremental-build change detection."""

import hashlib

__all__ = ["content_hash"]


def content_hash(content: str | bytes) -> str:
    """Return the canonical hex digest used as ``content_hash`` across the platform.

    Same input always yields the same digest; strings are UTF-8 encoded first.
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()
