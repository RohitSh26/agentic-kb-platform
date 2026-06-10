"""Static health stub; real build-run reporting arrives with the build engine (PR-04)."""


def health() -> dict[str, str]:
    return {"status": "ok", "service": "kb-builder"}
