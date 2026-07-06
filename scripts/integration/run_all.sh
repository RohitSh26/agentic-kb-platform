#!/usr/bin/env bash
# Full host-integration matrix (docs/runbooks/host-integration-test-plan.md):
# T1 preflight → Copilot CLI runner → OpenCode runner → T5 governance → grader.
#
#   EVIDENCE_DIR=/abs/path scripts/integration/run_all.sh
#
# Preconditions: agentic_kb_full built + active (dev-guide 04), copilot + gh
# authed, opencode installed, repo-root .env with the Groq credential
# (LLM_API_KEY — loaded in-process, never printed).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVIDENCE_DIR="${EVIDENCE_DIR:-${TMPDIR:-/tmp}/host-integration-evidence-$(date +%Y%m%d-%H%M%S)}"
export EVIDENCE_DIR

"${HERE}/preflight.sh"
"${HERE}/run_copilot.sh"
"${HERE}/run_opencode.sh"
"${HERE}/t5_governance.sh"

python3 "${HERE}/grade.py" \
    --evidence "${EVIDENCE_DIR}" \
    --repo "$(cd "${HERE}/../.." && pwd)" \
    --out "${EVIDENCE_DIR}/report.md"
