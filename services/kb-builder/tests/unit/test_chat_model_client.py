"""ChatModelClient JSON parsing is robust to messy local-model output (no network).

The wikify model contract is strict JSON; small local models (Ollama) are sloppy, so
the parser strips fences/prose and drops malformed concepts/facts rather than failing
the whole generation or inventing fields.
"""

import pytest

from agentic_kb_builder.infrastructure.azure_openai.chat_model_client import _parse_generation


def test_parses_plain_json() -> None:
    raw = (
        '{"summary": "A doc about X.",'
        ' "concepts": [{"name": "X", "description": "the thing"}],'
        ' "facts": [{"statement": "X is Y", "quote": "X is Y"}]}'
    )
    generation = _parse_generation(raw)
    assert generation.summary == "A doc about X."
    assert generation.concepts[0].name == "X"
    assert generation.facts[0].quote == "X is Y"


def test_strips_markdown_fences_and_surrounding_prose() -> None:
    raw = 'Sure! Here is the JSON:\n```json\n{"summary": "S", "concepts": [], "facts": []}\n```'
    generation = _parse_generation(raw)
    assert generation.summary == "S"
    assert generation.concepts == ()
    assert generation.facts == ()


def test_drops_malformed_concepts_and_facts() -> None:
    raw = (
        '{"summary": "S",'
        ' "concepts": [{"name": "ok", "description": "d"}, {"name": "", "description": "x"}],'
        ' "facts": [{"statement": "s", "quote": ""}, "not-an-object"]}'
    )
    generation = _parse_generation(raw)
    assert len(generation.concepts) == 1
    assert generation.facts == ()


def test_non_json_output_raises_a_clear_error() -> None:
    with pytest.raises(ValueError, match="did not return valid JSON"):
        _parse_generation("I cannot help with that.")
