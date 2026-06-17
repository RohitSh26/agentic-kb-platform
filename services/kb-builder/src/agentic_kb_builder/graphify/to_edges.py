"""Confidence weights for code edges mapped from Graphify's whole-tree extraction.

Imports and route declarations are exact AST facts (1.0); call/test-target resolution
can be confused by dynamic dispatch, so it sits below 1.0. Consumed by
``graphify_backend.map_extraction`` when it turns Graphify relations into edge drafts.
"""

IMPORTS_CONFIDENCE = 1.0
EXPOSED_AS_CONFIDENCE = 1.0
# A symbol's containing file is an exact AST fact (the file is the symbol's own key).
DEFINED_IN_CONFIDENCE = 1.0
CALLS_CONFIDENCE = 0.9
TESTS_CONFIDENCE = 0.9
