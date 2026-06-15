"""Version constants referenced by build cache keys.

Bump a constant whenever the corresponding schema, prompt, or algorithm changes
in a way that must invalidate cached generations or embeddings.
"""

from typing import Final

OUTPUT_SCHEMA_VERSION: Final = "1.0.0"
PROMPT_VERSION: Final = "1.0.0"
CHUNKER_VERSION: Final = "1.0.0"
# 1.1.0: graphify now emits artifacts (PR-06); invalidates PR-04-era cache rows
# whose artifact mappings are empty.
GRAPHIFY_VERSION: Final = "1.1.0"
PARSER_CONFIG_VERSION: Final = "1.0.0"
# Relation ontology version stamped on every linker edge
# (docs/contracts/relation-ontology.md). Bumping it is part of the
# relationship-judgment cache key and re-evaluates affected edges.
RELATION_SCHEMA_VERSION: Final = 1
# Prompt version for the phase-3B relationship judge (ChatModelClient
# .generate_relationship_judgment). Part of the relationship-judgment cache key —
# bumping it re-judges every cached candidate pair.
JUDGE_PROMPT_VERSION: Final = "1.0.0"
