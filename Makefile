SERVICES := kb-builder mcp-server
TEST_DATABASE_URL ?= postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_kb_test

.PHONY: sync lint types test verify migrate-test-db eval-run \
	sync-evals lint-evals types-evals test-evals verify-evals \
	$(foreach s,$(SERVICES),sync-$(s) lint-$(s) types-$(s) test-$(s) verify-$(s))

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

# kb-builder owns the schema: mcp-server integration tests require a database
# migrated here first (mcp-server never runs alembic).
migrate-test-db:
	cd services/kb-builder && DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic upgrade head

test-kb-builder:
	cd services/kb-builder && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

test-mcp-server:
	cd services/mcp-server && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

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

test-evals:
	cd evals && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run pytest

verify-evals: lint-evals types-evals test-evals
	@true

eval-run:
	cd evals && TEST_DATABASE_URL=$(TEST_DATABASE_URL) uv run python run.py
