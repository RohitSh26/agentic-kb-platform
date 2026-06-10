"""Deterministic normalization + hashing for connector content.

Same source state must yield the same normalized text and content_hash on any
machine (connectors rule). Pure stdlib; no locale- or platform-dependent steps.
"""

import hashlib
import unicodedata

__all__ = ["content_hash", "normalize_code", "normalize_text", "normalized_content_hash"]


def content_hash(content: str | bytes) -> str:
    """Return the canonical hex digest used as ``content_hash`` across the platform.

    Same input always yields the same digest; strings are UTF-8 encoded first.
    """
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str) -> str:
    """Normalize text deterministically: NFC, LF line endings, no trailing whitespace.

    Lines are right-stripped, leading/trailing blank lines dropped, and non-empty
    output always ends with exactly one newline.
    """
    unified = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    body = "\n".join(line.rstrip() for line in unified.split("\n")).strip("\n")
    return f"{body}\n" if body else ""


def normalize_code(text: str) -> str:
    """Conservative normalization for source code: line endings only.

    Code evidence must stay an exact snippet at a source version, so no
    whitespace stripping or unicode normalization is applied.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalized_content_hash(text: str) -> str:
    return content_hash(normalize_text(text))
