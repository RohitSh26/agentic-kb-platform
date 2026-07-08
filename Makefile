SERVICES := kb-builder mcp-server review-panel
TEST_DATABASE_URL ?= postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_kb_test

.PHONY: sync lint types test verify migrate-test-db sync-github-agents eval-run eval-all dashboard demo \
	sync-evals lint-evals types-evals test-evals verify-evals \
	$(foreach s,$(SERVICES),sync-$(s) lint-$(s) types-$(s) test-$(s) verify-$(s))

# Hermetic local end-to-end demo (Postgres + uv only; no Ollama/Azure). See docs/dev-guide/tutorials/04-review-a-pull-request.md.
demo:
	./scripts/e2e-local.sh

sync: $(foreach s,$(SERVICES),sync-$(s)) sync-evals
lint: $(foreach s,$(SERVICES),lint-$(s)) lint-evals
types: $(foreach s,$(SERVICES),types-$(s)) types-evals
test: $(foreach s,$(SERVICES),test-$(s)) test-evals
verify: $(foreach s,$(SERVICES),verify-$(s)) verify-evals

# Static pattern rules: the explicit target list keeps these usable from .PHONY
# (GNU make skips implicit-rule search for phony targets).
$(SERVICES:%=sync-%): sync-%:
	cd services/$* && uv sync

$(SERVICES:%=lint-%): lint-%:
	cd services/$* && uv run ruff check . && uv run ruff format --check .

$(SERVICES:%=types-%): types-%:
	cd services/$* && uv run pyright

# VS Code's Copilot extension discovers repo agents from .github/agents/ — this
# regenerates that deployment from the committed .copilot/agents/ renderings
# (filenames = frontmatter names, so agents:/handoffs: references resolve).
# check_parity.py pins the two byte-identical.
sync-github-agents:
	python3 -c "import pathlib,re; src=pathlib.Path('.copilot/agents'); dst=pathlib.Path('.github/agents'); dst.mkdir(parents=True,exist_ok=True); [dst.joinpath(re.search(r'^name:\\s*(\\S+)',f.read_text(),re.M).group(1)+'.agent.md').write_text(f.read_text()) for f in sorted(src.glob('*.agent.md')) if f.name!='_template.agent.md']"

# kb-builder owns the schema: mcp-server integration tests require a database
# migrated here first (mcp-server never runs alembic). test-mcp-server and
# test-evals depend on migrate-test-db because kb-builder's own suite
# DOWNGRADES the shared test DB to base on teardown — without the dependency,
# `make verify` (kb-builder first, mcp-server after) fails with hundreds of
# missing-table errors. Found live by the consolidated eval runner's T0
# (2026-07-05); dev-guide documents the same interaction for manual runs.
migrate-test-db:
	cd services/kb-builder && DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic upgrade head

test-kb-builder:
	cd services/kb-builder && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

test-mcp-server: migrate-test-db
	cd services/mcp-server && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

# review-panel needs no registry migration: its checkpointer + draft store
# bootstrap the dedicated review_panel schema themselves (DB-backed tests skip
# without TEST_DATABASE_URL).
test-review-panel:
	cd services/review-panel && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

$(SERVICES:%=verify-%): verify-%: lint-% types-% test-%
	@true

# evals is a dev-only project at the repo root, not a service; its e2e test and
# the benchmark run need a registry migrated by kb-builder (make migrate-test-db).
sync-evals:
	cd evals && uv sync

lint-evals:
	cd evals && uv run ruff check . && uv run ruff format --check .

types-evals:
	cd evals && uv run pyright

test-evals: migrate-test-db
	cd evals && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

verify-evals: lint-evals types-evals test-evals
	@true

eval-run:
	cd evals && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run python run.py

# Consolidated T0-T4 report (docs/architecture/evaluation-system.md). DATABASE_URL (T2/T3) and
# LLM creds (T3) are read from the calling shell's environment if exported; unavailable tiers
# SKIP with a stated reason rather than failing. Pass --with-gates for T0, --tiers to narrow.
eval-all:
	cd evals && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run python run_all.py

# Read-only operator dashboard (ADR-0014 Phase 1, docs/contracts/observability-dashboard.md):
# static HTML + Markdown from the v_* dashboard views. DATABASE_URL (a real registry; the
# renderer only SELECTs) is read from the calling shell if exported, else TEST_DATABASE_URL.
dashboard:
	cd evals && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run python run.py --dashboard
