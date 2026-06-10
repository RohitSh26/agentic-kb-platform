"""Version constants referenced by build cache keys.

Bump a constant whenever the corresponding schema, prompt, or algorithm changes
in a way that must invalidate cached generations or embeddings.
"""

OUTPUT_SCHEMA_VERSION = "1.0.0"
MCP_SCHEMA_VERSION = "1.0.0"
PROMPT_VERSION = "1.0.0"
CHUNKER_VERSION = "1.0.0"
# 1.1.0: graphify now emits artifacts (PR-06); invalidates PR-04-era cache rows
# whose artifact mappings are empty.
GRAPHIFY_VERSION = "1.1.0"
PARSER_CONFIG_VERSION = "1.0.0"
