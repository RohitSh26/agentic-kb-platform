#!/usr/bin/env bash
# PreToolUse(Bash) guard. Reads the tool-call JSON on stdin.
# Exit 0 = allow. Exit 2 = block (stderr is shown to Claude as the reason).
# Deterministic safety net for the few things prompts must never get wrong.
set -euo pipefail

payload="$(cat)"
cmd="$(printf '%s' "$payload" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null || true)"

block() { echo "BLOCKED by pre-bash-guard: $1" >&2; exit 2; }

# Never destroy the working tree or force-push.
case "$cmd" in
  *"rm -rf"*)              block "destructive recursive delete" ;;
  *"git push --force"*)   block "force push" ;;
  *"git push -f"*)        block "force push" ;;
  *"DROP DATABASE"*)      block "dropping a database" ;;
  *"TRUNCATE"*)           block "TRUNCATE outside a migration" ;;
esac

# Guard against the excluded-V1 resources sneaking in via package installs.
if printf '%s' "$cmd" | grep -Eiq 'uv (add|pip install).*(redis|azure-servicebus|azure-eventgrid|azure-storage-blob|azure-functions)'; then
  block "this package pulls in a V1-excluded resource. Write an ADR first (see docs/adr/0007-..)."
fi

exit 0
