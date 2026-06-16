"""_match_import_files: resolve a Python import to in-build file_keys by dotted suffix.

A repo file lives under a source-tree prefix (services/kb-builder/src/...) but an
import names only the package-relative module, so resolution must be by SUFFIX, not an
exact full-path match (ADR-0020 §4).
"""

from agentic_kb_builder.application.build_runner import _match_import_files

# (file dotted-path, file_key) — note the real-repo source-tree prefix before the package.
_REAL_REPO = [
    (
        "services.kb-builder.src.agentic_kb_builder.graphify.keys",
        "file:services/kb-builder/src/agentic_kb_builder/graphify/keys.py",
    ),
    (
        "services.kb-builder.src.agentic_kb_builder.graphify.write",
        "file:services/kb-builder/src/agentic_kb_builder/graphify/write.py",
    ),
]


def test_suffix_match_resolves_package_relative_import_under_a_prefix() -> None:
    # The crux: `import agentic_kb_builder.graphify.keys` must resolve even though the
    # file is under services/kb-builder/src/ — an exact full-path match would fail here.
    assert _match_import_files(_REAL_REPO, "agentic_kb_builder.graphify.keys") == [
        "file:services/kb-builder/src/agentic_kb_builder/graphify/keys.py"
    ]


def test_from_import_module_resolves() -> None:
    # `from agentic_kb_builder.graphify import write` -> module "agentic_kb_builder.graphify.write".
    assert _match_import_files(_REAL_REPO, "agentic_kb_builder.graphify.write") == [
        "file:services/kb-builder/src/agentic_kb_builder/graphify/write.py"
    ]


def test_stdlib_or_third_party_module_does_not_match() -> None:
    assert _match_import_files(_REAL_REPO, "functools") == []
    assert _match_import_files(_REAL_REPO, "httpx") == []


def test_ambiguous_suffix_returns_multiple_so_caller_drops_it() -> None:
    files = [
        ("a.pkg.util", "file:a/pkg/util.py"),
        ("b.pkg.util", "file:b/pkg/util.py"),
    ]
    # Two files share the ".pkg.util" suffix -> caller sees len != 1 and drops (no edge).
    assert len(_match_import_files(files, "pkg.util")) == 2


def test_exact_module_root_path_still_matches() -> None:
    # A file at the module root (no prefix) resolves by equality (exact is a suffix case).
    assert _match_import_files([("pkg.util", "file:pkg/util.py")], "pkg.util") == [
        "file:pkg/util.py"
    ]
