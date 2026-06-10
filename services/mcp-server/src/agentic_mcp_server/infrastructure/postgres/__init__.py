"""Postgres access for the runtime plane.

The Knowledge Registry schema is owned by kb-builder (see
docs/contracts/postgres-knowledge-registry.md); this service never runs
migrations. It reads registry tables and writes only runtime-owned rows
(retrieval_event, with the PR-10 broker).
"""
