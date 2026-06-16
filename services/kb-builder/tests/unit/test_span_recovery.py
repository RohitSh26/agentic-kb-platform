"""Deterministic exact-span recovery (ADR-0018) and import extraction (ADR-0020),
hermetic — pure ast, no LLM, no DB.

Asserts the span map keys on each symbol's def/class line (Graphify's join key) while
the recovered body_text spans decorators + docstring + body, that bad input degrades
gracefully (no spans, never a fabricated one), and that non-Python suffixes fall back.

Import-extraction tests assert: ``import a.b`` and ``from a.b import c`` are captured,
relative imports are skipped, non-Python suffixes return empty, syntax errors return
empty, and the result is deterministic.
"""

from agentic_kb_builder.graphify.span_recovery import (
    extract_import_modules,
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
# ADR-0020: import extraction
# ---------------------------------------------------------------------------

IMPORT_SOURCE = (
    "import a.b\n"
    "import functools\n"
    "from a.b import c\n"
    "from . import sibling\n"  # relative — must be skipped
    "from ..pkg import util\n"  # relative — must be skipped
)


def test_import_simple_captures_dotted_name() -> None:
    mods = extract_import_modules(file_text="import a.b\n", suffix=".py", path="m.py")
    assert "a.b" in mods


def test_from_import_captures_package_not_attribute() -> None:
    # `from a.b import c` -> module "a.b" (the package), not "a.b.c"
    mods = extract_import_modules(file_text="from a.b import c\n", suffix=".py", path="m.py")
    assert "a.b" in mods
    assert "a.b.c" not in mods


def test_relative_imports_are_skipped() -> None:
    mods = extract_import_modules(file_text=IMPORT_SOURCE, suffix=".py", path="m.py")
    # Relative imports (level > 0) must not appear
    assert "sibling" not in mods
    assert "util" not in mods
    assert "pkg" not in mods


def test_non_python_suffix_returns_empty() -> None:
    mods = extract_import_modules(file_text="import a.b\n", suffix=".go", path="main.go")
    assert mods == ()


def test_syntax_error_returns_empty_not_exception() -> None:
    mods = extract_import_modules(file_text="def broken(:\n", suffix=".py", path="bad.py")
    assert mods == ()


def test_import_extraction_is_deterministic() -> None:
    a = extract_import_modules(file_text=IMPORT_SOURCE, suffix=".py", path="m.py")
    b = extract_import_modules(file_text=IMPORT_SOURCE, suffix=".py", path="m.py")
    assert a == b


def test_all_absolute_imports_captured() -> None:
    mods = extract_import_modules(file_text=IMPORT_SOURCE, suffix=".py", path="m.py")
    assert "a.b" in mods
    assert "functools" in mods
