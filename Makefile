SERVICES := kb-builder mcp-server
TEST_DATABASE_URL ?= postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_kb_test

.PHONY: sync lint types test verify migrate-test-db \
	$(foreach s,$(SERVICES),sync-$(s) lint-$(s) types-$(s) test-$(s) verify-$(s))

sync: $(foreach s,$(SERVICES),sync-$(s))
lint: $(foreach s,$(SERVICES),lint-$(s))
types: $(foreach s,$(SERVICES),types-$(s))
test: $(foreach s,$(SERVICES),test-$(s))
verify: $(foreach s,$(SERVICES),verify-$(s))

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
