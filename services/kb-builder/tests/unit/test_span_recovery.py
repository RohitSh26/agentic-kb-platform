"""Deterministic exact-span recovery (ADR-0018), hermetic — pure ast, no LLM, no DB.

Asserts the span map keys on each symbol's def/class line (Graphify's join key) while
the recovered body_text spans decorators + docstring + body, that bad input degrades
gracefully (no spans, never a fabricated one), and that non-Python suffixes fall back.

search_text tests (ADR-0018 Phase 2) assert: split-identifier words from the name, param
names, decorator names, docstring words, called names, and imported names all appear; the
result is deterministic (same input => same output); non-Python => search_text is None.
"""

from agentic_kb_builder.graphify.span_recovery import (
    build_search_text,
    recover_python_spans,
    recover_spans,
)

SOURCE = (
    "import functools\n"  # 1
    "\n"  # 2
    "\n"  # 3
    "def top():\n"  # 4
    '    """Top docstring."""\n'  # 5
    "    return 1\n"  # 6
    "\n"  # 7
    "\n"  # 8
    "@functools.cache\n"  # 9
    "def decorated():\n"  # 10
    "    return 2\n"  # 11
    "\n"  # 12
    "\n"  # 13
    "class Service:\n"  # 14
    "    @property\n"  # 15
    "    def handle(self):\n"  # 16
    "        return 3\n"  # 17
)


def test_def_line_is_the_key_and_span_includes_docstring() -> None:
    spans = recover_python_spans(file_text=SOURCE, path="m.py")
    (top,) = spans[4]
    assert top.name == "top"
    assert top.def_line == 4
    assert top.span_start == 4 and top.span_end == 6
    assert top.body_text == 'def top():\n    """Top docstring."""\n    return 1'


def test_key_is_def_line_but_body_includes_leading_decorator() -> None:
    spans = recover_python_spans(file_text=SOURCE, path="m.py")
    # Graphify reports the def line (10), not the decorator line (9), so the map keys
    # on 10; the recovered body_text still STARTS at the decorator (span_start 9).
    (decorated,) = spans[10]
    assert decorated.def_line == 10
    assert decorated.span_start == 9 and decorated.span_end == 11
    assert decorated.body_text.startswith("@functools.cache\ndef decorated():")


def test_class_span_covers_whole_body_and_method_keyed_separately() -> None:
    spans = recover_python_spans(file_text=SOURCE, path="m.py")
    (service,) = spans[14]
    assert service.span_start == 14 and service.span_end == 17
    # The method is keyed on its own def line (16), decorator-inclusive start (15).
    (handle,) = spans[16]
    assert handle.span_start == 15 and handle.span_end == 17
    assert handle.body_text.startswith("    @property\n    def handle(self):")


def test_syntax_error_yields_no_spans_not_an_exception() -> None:
    # A file Graphify's tolerant parser still produced nodes for must not abort the
    # build: span recovery degrades to "no spans" (those symbols stay graph-only).
    assert recover_python_spans(file_text="def broken(:\n", path="bad.py") == {}


def test_non_python_suffix_falls_back_to_no_spans() -> None:
    # Python-first (ADR-0018): other languages have no recovery yet -> graph-only.
    assert recover_spans(file_text="func main() {}", suffix=".go", path="main.go") == {}
    # .py / .pyi dispatch to the ast pass.
    assert recover_spans(file_text=SOURCE, suffix=".py", path="m.py") != {}


def test_recovery_is_deterministic() -> None:
    assert recover_python_spans(file_text=SOURCE, path="m.py") == recover_python_spans(
        file_text=SOURCE, path="m.py"
    )


# ---------------------------------------------------------------------------
# ADR-0018 Phase 2: search_text (deterministic retrieval surface)
# ---------------------------------------------------------------------------

SEARCH_TEXT_SOURCE = (
    "import pathlib\n"  # 1
    "\n"  # 2
    "\n"  # 3
    "def validate_token(auth_header: str, *, max_retries: int = 3) -> bool:\n"  # 4
    '    """Verify the session gate token from the authorization header."""\n'  # 5
    "    import os\n"  # 6
    "    result = check_signature(auth_header)\n"  # 7
    "    return result\n"  # 8
    "\n"  # 9
    "\n"  # 10
    "@app.route('/api/login')\n"  # 11
    "def login_handler(request_context):\n"  # 12
    "    pass\n"  # 13
    "\n"  # 14
    "\n"  # 15
    "class AuthMiddleware:\n"  # 16
    "    pass\n"  # 17
)


def test_search_text_contains_split_identifier_words() -> None:
    """Snake-case name splits into words."""
    spans = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    (vt,) = spans[4]
    assert vt.search_text is not None
    words = set(vt.search_text.split())
    # "validate_token" -> ["validate", "token"]
    assert "validate" in words
    assert "token" in words


def test_search_text_contains_docstring_words() -> None:
    """Docstring words appear in search_text even when not in identifier."""
    spans = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    (vt,) = spans[4]
    assert vt.search_text is not None
    words = set(vt.search_text.split())
    # docstring: "Verify the session gate token from the authorization header."
    assert "session" in words
    assert "gate" in words
    assert "verify" in words
    assert "authorization" in words


def test_search_text_contains_param_names() -> None:
    """Function parameter names (split) appear in search_text."""
    spans = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    (vt,) = spans[4]
    assert vt.search_text is not None
    words = set(vt.search_text.split())
    # "auth_header" -> ["auth", "header"]; "max_retries" -> ["max", "retries"]
    assert "auth" in words
    assert "header" in words
    assert "max" in words
    assert "retries" in words


def test_search_text_contains_called_names() -> None:
    """Names of functions called within the span appear in search_text."""
    spans = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    (vt,) = spans[4]
    assert vt.search_text is not None
    words = set(vt.search_text.split())
    # validate_token calls check_signature -> split ["check", "signature"]
    assert "check" in words
    assert "signature" in words


def test_search_text_contains_decorator_names() -> None:
    """Decorator attribute names appear in search_text (split)."""
    spans = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    (lh,) = spans[12]
    assert lh.search_text is not None
    words = set(lh.search_text.split())
    # @app.route -> decorator.attr "route"
    assert "route" in words


def test_search_text_for_camel_case_class() -> None:
    """CamelCase class name splits correctly."""
    spans = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    (am,) = spans[16]
    assert am.search_text is not None
    words = set(am.search_text.split())
    # "AuthMiddleware" -> ["auth", "middleware"]
    assert "auth" in words
    assert "middleware" in words


def test_search_text_is_deterministic() -> None:
    """Same source must always produce the same search_text (connectors rule)."""
    spans_a = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    spans_b = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    for def_line in spans_a:
        for span_a, span_b in zip(spans_a[def_line], spans_b[def_line], strict=True):
            assert span_a.search_text == span_b.search_text


def test_non_python_suffix_yields_no_search_text() -> None:
    """Non-Python files produce no spans at all => no search_text (Python-first)."""
    spans = recover_spans(file_text="func main() {}", suffix=".go", path="main.go")
    assert spans == {}


def test_search_text_is_sorted_and_space_separated() -> None:
    """Determinism: words must be stable-sorted so same source => same string."""
    spans = recover_python_spans(file_text=SEARCH_TEXT_SOURCE, path="m.py")
    (vt,) = spans[4]
    assert vt.search_text is not None
    words = vt.search_text.split()
    assert words == sorted(words), "search_text words must be in sorted order"


def test_build_search_text_directly_with_known_function() -> None:
    """Unit-test build_search_text against a known ast node."""
    import ast

    src = (
        "def get_user_by_id(user_id: int, db_session=None) -> None:\n"
        '    """Fetch user record from the database."""\n'
        "    lookup_record(user_id)\n"
    )
    tree = ast.parse(src)
    (func,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    result = build_search_text(func)
    words = set(result.split())
    assert "get" in words  # from get_user_by_id
    assert "user" in words
    assert "by" in words
    assert "fetch" in words  # from docstring
    assert "database" in words  # from docstring
    assert "lookup" in words  # from called name lookup_record (split)
    assert "record" in words  # from called name lookup_record (split)
    # words are sorted
    word_list = result.split()
    assert word_list == sorted(word_list)
