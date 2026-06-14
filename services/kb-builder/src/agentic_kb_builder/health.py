"""Static liveness stub for the build plane.

The build engine has landed, but the build plane runs as a one-shot job, not a
server — there is no health-serving entrypoint yet (a recorded follow-up; see
docker-compose.yml). Build-run status is therefore read directly from the
`kb_build_run` table, not exposed here; this stays a constant liveness response
until a serving entrypoint exists to report the last run's status/kb_version.
"""


def health() -> dict[str, str]:
    return {"status": "ok", "service": "kb-builder"}
