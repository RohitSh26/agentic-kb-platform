#!/usr/bin/env bash
# PostToolUse(Edit|Write) formatter. Best-effort: never fail the turn on a format hiccup.
# Reads the tool-call JSON on stdin and formats the touched Python file.
set -uo pipefail

payload="$(cat)"
path="$(printf '%s' "$payload" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null || true)"

[ -z "${path:-}" ] && exit 0
case "$path" in
  *.py)
    command -v ruff >/dev/null 2>&1 && ruff format "$path" >/dev/null 2>&1 || true
    command -v ruff >/dev/null 2>&1 && ruff check --fix "$path" >/dev/null 2>&1 || true
    ;;
esac
exit 0
