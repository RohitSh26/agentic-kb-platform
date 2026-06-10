.PHONY: sync lint types test verify

sync:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

types:
	uv run pyright

test:
	uv run pytest

verify: lint types test
