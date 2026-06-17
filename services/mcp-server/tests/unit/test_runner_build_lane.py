"""Deterministic BUILD-lane helpers in the runner (no DB/LLM).

These pin the M1 contract pieces that must never silently regress: the runtime reads ONLY
the broker-selected files that EXIST (a missing target/test is a hard stop), it parses a
unified diff out of model output, it caps the files it reads, and it treats a "paste the
file" request as a failure signal (judge adjustment #5).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[4] / "scripts" / "agent_runner.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_runner", _RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module defines a @dataclass, and dataclasses resolves
    # type hints via sys.modules[cls.__module__] at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load()


def _pack(targets=(), tests=(), deps=()):
    def refs(paths):
        return [{"path": p, "reason": "r", "confidence": 0.9, "est_tokens": 10} for p in paths]

    return {
        "target_files": refs(targets),
        "test_files": refs(tests),
        "dependency_files": refs(deps),
    }


def _touch(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x = 1\n")


def test_resolve_keeps_only_existing_files(tmp_path: Path) -> None:
    _touch(tmp_path, "src/a.py")
    _touch(tmp_path, "tests/test_a.py")
    _touch(tmp_path, "src/dep.py")
    pack = _pack(
        targets=["src/a.py", "src/ghost.py"],  # ghost does not exist
        tests=["tests/test_a.py"],
        deps=["src/dep.py", "src/missing_dep.py"],
    )

    resolved = runner._resolve_build_files(pack, tmp_path)

    assert resolved.target == ["src/a.py"]
    assert resolved.test == ["tests/test_a.py"]
    assert resolved.dependency == ["src/dep.py"]
    assert not resolved.missing_target
    assert not resolved.missing_test
    assert resolved.primary_test == "tests/test_a.py"


def test_missing_target_or_test_is_flagged(tmp_path: Path) -> None:
    _touch(tmp_path, "tests/test_a.py")
    # target proposed but absent on disk; test present
    resolved = runner._resolve_build_files(
        _pack(targets=["src/a.py"], tests=["tests/test_a.py"]), tmp_path
    )
    assert resolved.missing_target  # the hard-stop condition (no model call)

    _touch(tmp_path, "src/a.py")
    # test proposed (naming convention) but absent on disk
    resolved2 = runner._resolve_build_files(
        _pack(targets=["src/a.py"], tests=["tests/test_ghost.py"]), tmp_path
    )
    assert resolved2.missing_test


def test_all_files_caps_total_reads(tmp_path: Path) -> None:
    for i in range(8):
        _touch(tmp_path, f"src/f{i}.py")
    pack = _pack(
        targets=["src/f0.py"],
        tests=["src/f1.py"],
        deps=[f"src/f{i}.py" for i in range(2, 8)],
    )
    resolved = runner._resolve_build_files(pack, tmp_path)
    assert len(resolved.all_files) == runner._MAX_FULL_FILES
    # target + test come first, never dropped for a dependency
    assert resolved.all_files[0] == "src/f0.py"
    assert resolved.all_files[1] == "src/f1.py"


def test_rel_to_workspace_handles_absolute_and_relative(tmp_path: Path) -> None:
    # a local-FS KB returns file:///abs/path -> absolute; it must be relativised so diffs apply
    abs_inside = str(tmp_path / "src" / "a.py")
    assert runner._rel_to_workspace(abs_inside, tmp_path) == "src/a.py"
    # already-relative (github-sourced KB) passes through unchanged
    assert runner._rel_to_workspace("src/a.py", tmp_path) == "src/a.py"
    # absolute path OUTSIDE the workspace is rejected (never edit outside the repo)
    assert runner._rel_to_workspace("/etc/passwd", tmp_path) is None


def test_resolve_relativises_absolute_kb_paths(tmp_path: Path) -> None:
    _touch(tmp_path, "src/a.py")
    _touch(tmp_path, "tests/test_a.py")
    pack = _pack(
        targets=[str(tmp_path / "src" / "a.py")],  # absolute, as a local-FS KB returns
        tests=[str(tmp_path / "tests" / "test_a.py")],
    )
    resolved = runner._resolve_build_files(pack, tmp_path)
    assert resolved.target == ["src/a.py"]
    assert resolved.test == ["tests/test_a.py"]
    assert not resolved.missing_target and not resolved.missing_test


def test_parse_file_blocks_extracts_whole_files() -> None:
    raw = (
        "Here you go:\n"
        '<<<FILE path="src/a.py">\n'
        "x = 2\n"
        "y = 3\n"
        "<<<END_FILE>>>\n"
        '<<<FILE path="tests/test_a.py">\n'
        "assert True\n"
        "<<<END_FILE>>>\n"
        "Done."
    )
    blocks = runner._parse_file_blocks(raw)
    assert set(blocks) == {"src/a.py", "tests/test_a.py"}
    assert blocks["src/a.py"] == "x = 2\ny = 3"
    assert "Here you go" not in blocks["src/a.py"]


def test_parse_file_blocks_none_without_blocks() -> None:
    assert runner._parse_file_blocks("I need to see more of the codebase first.") == {}


def test_normalise_block_paths_relativises(tmp_path: Path) -> None:
    blocks = {
        "a/src/a.py": "x = 1",  # git-style a/ prefix
        str(tmp_path / "src" / "b.py"): "y = 1",  # absolute (local-FS KB)
    }
    out = runner._normalise_block_paths(blocks, tmp_path)
    assert "src/a.py" in out
    assert "src/b.py" in out


def test_validate_rejects_out_of_boundary_path(tmp_path: Path) -> None:
    _touch(tmp_path, "src/dep.py")
    blocks = {"src/dep.py": "x = 1\n"}
    accepted, errors = runner._validate_file_blocks(blocks, {"src/a.py"}, tmp_path)
    assert accepted == {}
    assert any("not in the writable set" in e for e in errors)


def test_validate_rejects_placeholder_and_bad_python(tmp_path: Path) -> None:
    writable = {"src/a.py", "src/b.py"}
    blocks = {
        "src/a.py": "def f():\n    # ... existing code ...\n    pass\n",  # placeholder
        "src/b.py": "def f(:\n    pass\n",  # syntax error
    }
    accepted, errors = runner._validate_file_blocks(blocks, writable, tmp_path)
    assert accepted == {}
    assert any("placeholder" in e for e in errors)
    assert any("not valid Python" in e for e in errors)


def test_validate_rejects_suspicious_shrink(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n" * 100)  # large existing file
    accepted, errors = runner._validate_file_blocks({"src/a.py": "x = 1\n"}, {"src/a.py"}, tmp_path)
    assert accepted == {}
    assert any("shrank" in e for e in errors)


def test_validate_accepts_a_valid_full_file(tmp_path: Path) -> None:
    _touch(tmp_path, "src/a.py")
    new = "def f() -> int:\n    return 2\n"
    accepted, errors = runner._validate_file_blocks({"src/a.py": new}, {"src/a.py"}, tmp_path)
    assert accepted == {"src/a.py": new}
    assert errors == []


@pytest.mark.parametrize(
    "text",
    [
        "Please paste the full file so I can edit it.",
        "I need the complete contents of the module.",
        "Can you share the source of http_client.py?",
        "provide the entire file and I'll continue",
    ],
)
def test_paste_request_is_detected(text: str) -> None:
    assert runner._PASTE_REQUEST_RE.search(text) is not None


def test_test_command_is_deterministic_not_model_invented() -> None:
    cmd = runner.BUILD_TEST_CMD.format(test="tests/test_a.py")
    assert "tests/test_a.py" in cmd
    assert "pytest" in cmd
