"""Deterministic prompt-injection scan over retrieved text.

Retrieved content is untrusted (invariant 6): it can never change tool
policy, identity, or instructions. This scan does not enforce that — the
broker's architecture does (no retrieved text ever reaches a policy or
identity decision). It marks injection-style content so consumers and the
audit log can see it; flagged text is returned verbatim, never rewritten.
Regex-only on purpose: no model calls in the broker (V1), and deterministic
flags are testable and tunable from audit logs.
"""

import re
from dataclasses import dataclass

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b.{0,60}?"
            r"\b(?:previous|prior|above|earlier|all|any|system)\b.{0,60}?"
            r"\b(?:instructions?|prompts?|rules?|directives?|policies)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"new\s+system\s+prompt|you\s+are\s+now\b|act\s+as\s+(?:the\s+)?(?:system|admin)",
            re.IGNORECASE,
        ),
    ),
    (
        "system_role_marker",
        re.compile(r"^\s*(?:system|assistant|developer)\s*:", re.IGNORECASE | re.MULTILINE),
    ),
    (
        "chat_template_token",
        re.compile(r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|\[INST\]|<<SYS>>"),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"\b(?:reveal|print|show|output|leak|exfiltrate|repeat)\b.{0,60}?"
            r"\b(?:system\s+prompt|secrets?|credentials?|api\s+keys?|passwords?|tokens?)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        # RTL/LTR overrides, isolates, zero-width characters, BOM — used to
        # hide instructions from human reviewers while models still read them
        "unicode_obfuscation",
        re.compile("[\\u202a-\\u202e\\u2066-\\u2069\\u200b-\\u200f\\ufeff]"),
    ),
)


@dataclass(frozen=True)
class InjectionScan:
    flagged: bool
    signals: tuple[str, ...]


def scan_for_injection(*texts: str) -> InjectionScan:
    """Scan one or more text fields; signals are the union of matched patterns."""
    signals = tuple(
        name for name, pattern in _PATTERNS if any(pattern.search(text) for text in texts)
    )
    return InjectionScan(flagged=bool(signals), signals=signals)
