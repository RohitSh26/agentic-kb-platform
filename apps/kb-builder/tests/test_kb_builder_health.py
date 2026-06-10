from kb_builder import health


def test_health() -> None:
    assert health() == {"status": "ok", "service": "kb-builder"}
