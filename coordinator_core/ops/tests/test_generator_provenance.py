"""
Tests for coordinator_core.ops.generator_provenance.

Negative-spec: fixture modules under `tmp_path` only — never the real repo's
generator modules — and discovery is never exercised via import (see the
`sys.modules` assertion below), matching AC6.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from coordinator_core.ops.generator_provenance import _extract_generates, discover_generators
from coordinator_core.ops.staleness_git import Verdict

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

# C2 backfill targets -- the five real generator modules
# docs/plans/2026-08-13-generator-output-staleness-detector.md § Tasks id C2
# added a `GENERATES` declaration to. Read directly via `ast.parse` +
# `_extract_generates` (rather than `discover_generators`, which additionally
# gates on write-behaviour detection that some of these thin CLI trampolines
# don't themselves trip -- see C2's own report) so this asserts the
# declaration itself, independent of that gate.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_C2_TARGETS = (
    "coordinator_core/orientation/regenerate_cache.py",
    "coordinator/bin/generate-repomap.py",
    "coordinator/bin/generate-exec-summary.py",
    "coordinator/bin/regenerate-known-red-registry.py",
    "coordinator_core/contract/cockpit_schema/emit_schema.py",
)


def _write(root: Path, rel_path: str, content: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_declared_generator_resolves_its_pairs(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("x", encoding="utf-8")

    _write(
        tmp_path,
        "coordinator_core/gen_declared.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]},
]

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_declared.py"]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict is None
    assert len(record.pairs) == 1
    pair = record.pairs[0]
    assert pair.artifact == "out.json"
    assert pair.stamp_key == "generated_at"
    assert pair.sources == ("src.txt",)


def test_undeclared_generator_reports_undeclared_not_skipped(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_undeclared.py",
        """
from pathlib import Path

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_undeclared.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED


def test_generates_empty_list_is_declared_empty_not_undeclared(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_empty.py",
        """
from pathlib import Path

GENERATES = []

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_empty.py"]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict is None
    assert record.pairs == ()


def test_undeclared_tracked_write_names_the_tracked_path_in_detail(tmp_path):
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "a@b"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    _write(
        tmp_path,
        "coordinator_core/gen_tracked_literal.py",
        """
from pathlib import Path

def run():
    Path("out.json").write_text("{}")
""",
    )
    _write(tmp_path, "out.json", "{}")

    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "commit", "-qm", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_tracked_literal.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "writes tracked path" in matches[0].detail
    assert "out.json" in matches[0].detail


def test_unresolved_write_names_unresolved_path_expression(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_variable_target.py",
        """
from pathlib import Path

def run(target):
    Path(target).write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_variable_target.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.WRITE_TARGET_UNRESOLVED
    assert "unresolved path expression" in matches[0].detail


def test_generates_empty_list_message_unchanged_by_basis_threading(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_empty_message.py",
        """
from pathlib import Path

GENERATES = []

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_empty_message.py"]
    assert len(matches) == 1
    assert (
        matches[0].detail
        == "coordinator_core/gen_empty_message.py declares GENERATES = [] (declared-empty, no artifacts)"
    )


def test_known_writer_with_no_generates_is_named_undeclared_by_ast_sweep(tmp_path):
    _write(
        tmp_path,
        "bin/write_surface_manifest.py",
        """
import json

def run():
    with open("manifest.json", "w") as fh:
        json.dump({}, fh)
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "bin/write_surface_manifest.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "write_surface_manifest.py" in matches[0].detail


# A hand-picked sample of real known-writer modules OUTSIDE the C2 five, so
# this regression coverage would catch a write-detection defect landing on
# any module in the sweep -- not only the five it already knows about. This
# is the P1 fix's own proof: `doctor.py` does `hooks_json_path.open("w", ...)`
# (bound-receiver form) and was invisible to `discover_generators` before the
# `_call_is_write` argument-slicing fix.
# Review: coordinator:code-reviewer -- P2, the C2-only real-tree check would
# not have caught the P1 defect since doctor.py isn't in the C2 five.
_OTHER_REAL_WRITERS = (
    "coordinator_core/ops/doctor.py",
)


def test_real_tree_resolves_all_five_c2_declared_generators():
    """The defect this chunk fixes: three of the five C2-declared generators
    were entirely absent from `discover_generators`' sweep of the real repo
    (thin CLI shims whose write is delegated and therefore invisible to
    write-behaviour detection), so their `GENERATES` was never read. This is
    the case whose absence let that ship -- it must exercise the real repo,
    not a fixture."""
    records = discover_generators(_REPO_ROOT)
    found = {r.generator for r in records}
    for rel in _C2_TARGETS:
        assert rel in found, f"{rel} missing from the real-tree sweep"
        matches = [r for r in records if r.generator == rel]
        assert len(matches) == 1
        assert matches[0].verdict is None, f"{rel} resolved with verdict {matches[0].verdict!r}"
        assert len(matches[0].pairs) >= 1


def test_real_tree_discovers_writers_outside_the_c2_five(tmp_path):
    """P1 regression -- the real-tree check above only re-verifies the five
    known C2 targets and would not have caught the bound-receiver `.open("w")`
    mis-slice defect, since none of the five use that call shape. This
    exercises a real, on-disk module outside the five (`doctor.py`, a known
    writer via `hooks_json_path.open("w", encoding="utf-8")`) and asserts it
    is discovered at all -- not asserting the total UNDECLARED count, which
    is a moving number."""
    records = discover_generators(_REPO_ROOT)
    found = {r.generator for r in records}
    for rel in _OTHER_REAL_WRITERS:
        assert rel in found, f"{rel} missing from the real-tree sweep"


def test_declared_generator_with_no_detectable_write_still_resolves(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_declared_no_write.py",
        """
GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]},
]

def run():
    delegate_elsewhere()
""",
    )
    (tmp_path / "src.txt").write_text("x", encoding="utf-8")

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_declared_no_write.py"]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict is None
    assert len(record.pairs) == 1
    assert record.pairs[0].artifact == "out.json"


def test_untracked_write_target_not_reported_as_undeclared_generator(tmp_path):
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    _write(
        tmp_path,
        "coordinator_core/gen_untracked.py",
        """
from pathlib import Path

def run():
    Path("runtime_cache/state.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_untracked.py"]
    assert matches == []


def test_fdopen_write_mode_is_detected_as_writer(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_fdopen.py",
        """
import os

def run():
    fd = os.open("out.json", os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_fdopen.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.WRITE_TARGET_UNRESOLVED


def test_receiver_style_write_text_reads_target_from_receiver_call(tmp_path):
    """C1c regression -- `_call_target_str` was narrowed to `call.args[0]`
    on the `.write_text(...)` call itself, which for a receiver-style write
    is the CONTENT argument, not the path. The literal lives in the
    receiver's own `Path(...)` call. Committed writer + committed artifact
    must be discovered, matching the chunk brief's repro exactly."""
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "a@b"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    _write(
        tmp_path,
        "coordinator_core/undeclared_writer.py",
        """
from pathlib import Path

Path('out.json').write_text('{}')
""",
    )
    _write(tmp_path, "out.json", "{}")

    subprocess.run(
        ["git", "add", "coordinator_core/undeclared_writer.py", "out.json"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "commit", "-qm", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/undeclared_writer.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED


def test_builtin_open_write_mode_not_misread_as_target(tmp_path):
    """C1c regression -- `open(<literal>, "w")` target is the builtin call's
    own `args[0]`; the mode string `"w"` must never be read as the target
    (the defect C1b's narrowing originally fixed, kept correct here)."""
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    _write(
        tmp_path,
        "coordinator_core/gen_builtin_open.py",
        """
def run():
    with open("manifest.txt", "w") as fh:
        fh.write("x")
""",
    )
    _write(tmp_path, "manifest.txt", "x")

    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "a@b"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "commit", "-qm", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_builtin_open.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED


def test_receiver_style_write_to_untracked_target_not_discovered(tmp_path):
    """C1c regression -- proves the target is read from the RIGHT node
    (the receiver's literal) rather than merely defaulting to unknown: an
    untracked receiver-style target must still be filtered out by the
    tracked-path gate, exactly as the builtin form already was."""
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    _write(
        tmp_path,
        "coordinator_core/gen_receiver_untracked.py",
        """
from pathlib import Path

def run():
    Path("runtime_cache/state.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_receiver_untracked.py"]
    assert matches == []


def test_os_fdopen_target_is_never_read_as_path_literal(tmp_path):
    """C1c regression -- `os.fdopen(fd, "w")`'s `args[0]` is a file
    descriptor expression, never a path literal, and must resolve to an
    unresolved (still-counted) target rather than a bogus literal string."""
    _write(
        tmp_path,
        "coordinator_core/gen_fdopen_no_literal.py",
        """
import os

def run():
    fd = os.open("out.json", os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_fdopen_no_literal.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.WRITE_TARGET_UNRESOLVED


def test_bound_receiver_open_write_mode_is_detected(tmp_path):
    """P1 regression -- `<receiver>.open("w", ...)` on a bound variable (not
    a `Path(...)` literal) is a bound-method call with an implicit receiver:
    the mode lives at `args[0]`, not `args[1]` as for `os.fdopen(fd, "w")`.
    `_call_is_write` previously read `args[1:]` for both shapes, so this was
    silently missed -- the exact shape live in `doctor.py:406`
    (`hooks_json_path.open("w", encoding="utf-8")`)."""
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _write(
        tmp_path,
        "coordinator_core/gen_bound_open.py",
        """
from pathlib import Path

def run(target_path):
    with target_path.open("w", encoding="utf-8") as fh:
        fh.write("{}")
""",
    )
    _write(tmp_path, "manifest.json", "{}")
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "a@b"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "commit", "-qm", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_bound_open.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.WRITE_TARGET_UNRESOLVED


def test_json_dump_bare_name_import_is_detected(tmp_path):
    """P3 regression -- `from json import dump; dump(...)` is a bare-name
    call form the `ast.Name` branch of `_call_is_write` never checked
    (only `open`/`fdopen`), so it was silently missed."""
    _write(
        tmp_path,
        "coordinator_core/gen_json_dump_bare.py",
        """
from json import dump

def run():
    with open("manifest.json", "w") as fh:
        dump({}, fh)
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_json_dump_bare.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED


def test_json_dump_aliased_import_is_detected(tmp_path):
    """P3 regression -- `import json as j; j.dump(...)` is missed by a
    hardcoded `func.value.id == "json"` check."""
    _write(
        tmp_path,
        "coordinator_core/gen_json_dump_alias.py",
        """
import json as j

def run():
    with open("manifest.json", "w") as fh:
        j.dump({}, fh)
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_json_dump_alias.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED


def test_sweep_imports_nothing(tmp_path):
    module_name = "gen_should_never_be_imported"
    before = set(sys.modules)

    _write(
        tmp_path,
        f"coordinator_core/{module_name}.py",
        """
from pathlib import Path

raise RuntimeError("this module must never be imported by the sweep")

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]},
]

def run():
    Path("out.json").write_text("{}")
""",
    )

    discover_generators(tmp_path)

    assert module_name not in sys.modules
    assert set(sys.modules) - before == set()


def test_test_module_variable_write_not_discovered(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/ops/tests/test_fixture_writer.py",
        """
def run(target_path):
    target_path.write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/ops/tests/test_fixture_writer.py"]
    assert matches == []


def test_test_module_tracked_literal_write_still_discovered(tmp_path):
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "a@b"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    _write(
        tmp_path,
        "coordinator_core/ops/tests/test_tracked_writer.py",
        """
from pathlib import Path

def run():
    Path("out.json").write_text("{}")
""",
    )
    _write(tmp_path, "out.json", "{}")

    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "commit", "-qm", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/ops/tests/test_tracked_writer.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "writes tracked path" in matches[0].detail


def test_test_module_with_generates_resolves_pairs_normally(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("x", encoding="utf-8")

    _write(
        tmp_path,
        "coordinator_core/ops/tests/test_declared_generator.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]},
]

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [
        r for r in records if r.generator == "coordinator_core/ops/tests/test_declared_generator.py"
    ]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict is None
    assert len(record.pairs) == 1
    assert record.pairs[0].artifact == "out.json"


def test_non_test_module_variable_write_still_discovered(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_variable_target_control.py",
        """
from pathlib import Path

def run(target):
    Path(target).write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [
        r for r in records if r.generator == "coordinator_core/gen_variable_target_control.py"
    ]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.WRITE_TARGET_UNRESOLVED
    assert "unresolved path expression" in matches[0].detail


def test_missing_pyproject_falls_back_to_default_test_glob(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/ops/tests/test_no_pyproject.py",
        """
def run(target_path):
    target_path.write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/ops/tests/test_no_pyproject.py"]
    assert matches == []


def test_malformed_pyproject_falls_back_to_default_test_glob(tmp_path):
    _write(tmp_path, "pyproject.toml", "this is not [ valid toml")
    _write(
        tmp_path,
        "coordinator_core/ops/tests/test_malformed_pyproject.py",
        """
def run(target_path):
    target_path.write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [
        r for r in records if r.generator == "coordinator_core/ops/tests/test_malformed_pyproject.py"
    ]
    assert matches == []


def test_r1_json_dump_to_stdout_not_counted(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_stdout_dump.py",
        """
import json
import sys

def run():
    json.dump({}, sys.stdout, indent=2)
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_stdout_dump.py"]
    assert matches == []


def test_r2_json_dump_into_local_handle_not_independent_but_open_still_counts(tmp_path):
    subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _write(
        tmp_path,
        "coordinator_core/gen_dump_local_handle.py",
        """
import json

def run():
    with open("manifest.json", "w") as fh:
        json.dump({}, fh)
""",
    )
    _write(tmp_path, "manifest.json", "{}")
    subprocess.run(
        ["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_dump_local_handle.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED


def test_r3_mkstemp_replace_idiom_resolves_to_tracked_destination(tmp_path):
    subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _write(
        tmp_path,
        "coordinator_core/gen_mkstemp_replace.py",
        """
import os
import tempfile

def run():
    fd, tmp_name = tempfile.mkstemp(dir=".")
    with os.fdopen(fd, "w") as f:
        f.write("{}")
    os.replace(tmp_name, "out.json")
""",
    )
    _write(tmp_path, "out.json", "{}")
    subprocess.run(
        ["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=str(tmp_path), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_mkstemp_replace.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "out.json" in matches[0].detail


def test_r3_mkstemp_replace_idiom_stays_unresolved_when_dest_not_literal(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_mkstemp_replace_dynamic.py",
        """
import os
import tempfile

def run(dest):
    fd, tmp_name = tempfile.mkstemp(dir=".")
    with os.fdopen(fd, "w") as f:
        f.write("{}")
    os.replace(tmp_name, dest)
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_mkstemp_replace_dynamic.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.WRITE_TARGET_UNRESOLVED


def test_r4_tmp_sibling_fstring_never_drops_the_site(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_tmp_sibling.py",
        """
import os

def run(artifact_path):
    tmp_path = f"{artifact_path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as fh:
        fh.write("{}")
    os.replace(tmp_path, artifact_path)
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_tmp_sibling.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.WRITE_TARGET_UNRESOLVED


def test_r5_tempfile_gettempdir_base_excluded(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_tempdir_base.py",
        """
import tempfile
from pathlib import Path

def run():
    (Path(tempfile.gettempdir()) / "out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_tempdir_base.py"]
    assert matches == []


def test_r5_sessions_dir_base_excluded(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_sessions_dir_base.py",
        """
from pathlib import Path

def sessions_dir():
    ...

def run():
    (Path(sessions_dir()) / "out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_sessions_dir_base.py"]
    assert matches == []


def test_r6_conftest_module_variable_write_not_discovered(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/conftest.py",
        """
def run(target_path):
    target_path.write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/conftest.py"]
    assert matches == []


def test_r6_testing_directory_module_variable_write_not_discovered(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/testing/some_fixture_helper.py",
        """
def run(target_path):
    target_path.write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/testing/some_fixture_helper.py"]
    assert matches == []


_CANARY = "coordinator_core/ops/deliverable_ledger_write.py"


def test_regression_guard_five_declared_generators_and_canary_survive_narrowing():
    """Mandatory regression guard (spike_amendments) -- the five declared
    generators keep their verdicts and pairs, and the canary real emitter
    (a naive .tmp-suffix exclusion's known false-positive victim) must
    remain in the population, never silently dropped by R1-R6."""
    records = discover_generators(_REPO_ROOT)
    by_name = {r.generator: r for r in records}

    for rel in _C2_TARGETS:
        assert rel in by_name, f"{rel} missing from the real-tree sweep after R1-R6"
        record = by_name[rel]
        assert record.verdict is None, f"{rel} resolved with verdict {record.verdict!r}"
        assert len(record.pairs) >= 1

    assert _CANARY in by_name, f"{_CANARY} vanished from the population -- narrowing regression"
    assert by_name[_CANARY].verdict == Verdict.WRITE_TARGET_UNRESOLVED


def test_ac10_malformed_sources_variants_resolve_to_undeclared(tmp_path):
    (tmp_path / "present.txt").write_text("x", encoding="utf-8")

    _write(
        tmp_path,
        "coordinator_core/gen_empty_sources.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": []},
]

def run():
    Path("out.json").write_text("{}")
""",
    )
    _write(
        tmp_path,
        "coordinator_core/gen_notalist_sources.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": "present.txt"},
]

def run():
    Path("out.json").write_text("{}")
""",
    )
    _write(
        tmp_path,
        "coordinator_core/gen_absent_source.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["does-not-exist.txt"]},
]

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    by_name = {r.generator: r for r in records}

    assert by_name["coordinator_core/gen_empty_sources.py"].verdict == Verdict.UNDECLARED
    assert by_name["coordinator_core/gen_notalist_sources.py"].verdict == Verdict.UNDECLARED
    assert by_name["coordinator_core/gen_absent_source.py"].verdict == Verdict.UNDECLARED


def test_c2_all_five_generators_declare_generates():
    """docs/plans/2026-08-13-generator-output-staleness-detector.md § Tasks
    id C2 -- each of the five backfilled generators carries a well-formed,
    non-malformed `GENERATES` whose `sources` resolve on disk. None reads
    UNDECLARED (absent, malformed, or empty-string/type-invalid fields)."""
    import ast

    for rel in _C2_TARGETS:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"{rel} not found on disk"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        generates = _extract_generates(tree)

        assert generates != "__MALFORMED__", f"{rel}: GENERATES is not a literal"
        assert generates is not None, f"{rel}: no GENERATES declared (reads UNDECLARED)"
        assert isinstance(generates, list) and len(generates) >= 1, (
            f"{rel}: GENERATES is not a non-empty list"
        )

        for entry in generates:
            assert isinstance(entry, dict), f"{rel}: GENERATES entry is not a mapping: {entry!r}"
            artifact = entry.get("artifact")
            stamp_key = entry.get("stamp_key")
            sources = entry.get("sources")
            assert isinstance(artifact, str) and artifact, f"{rel}: missing artifact"
            assert isinstance(stamp_key, str) and stamp_key, f"{rel}: missing stamp_key"
            assert isinstance(sources, list) and len(sources) > 0, f"{rel}: sources not a non-empty list"
            for source in sources:
                assert isinstance(source, str) and source, f"{rel}: malformed sources entry {source!r}"
                assert (_REPO_ROOT / source).exists(), (
                    f"{rel}: declared sources path {source!r} does not exist on disk"
                )

    # The two thin CLI trampolines must never declare their own path as
    # `sources` -- that would yield a since-range that is empty essentially
    # forever (§ Mechanism correction). The other three are themselves the
    # real implementation locus, so their own path IS the correct `sources`.
    _thin_shims = (
        "coordinator/bin/generate-repomap.py",
        "coordinator/bin/generate-exec-summary.py",
    )
    for rel in _thin_shims:
        path = _REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for entry in _extract_generates(tree):
            assert rel not in entry["sources"], (
                f"{rel}: sources names the declaring shim's own path, never the real "
                "implementation locus"
            )


def test_c2_emit_schema_declaration_leaves_committed_bytes_unchanged():
    """docs/plans/2026-08-13-generator-output-staleness-detector.md § Tasks id
    C2 -- `emit_schema.py` gets a `GENERATES` declaration and NOTHING ELSE.
    Verified against the module's own EXISTING committed-emit-drift
    comparison (`test_committed_emit_drift.py`, `_reorder`/`_KEY_ORDER`'s raw
    string compare) as the oracle, per the chunk brief -- not a freshly
    derived comparison here."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "coordinator_core/contract/cockpit_schema/tests/test_committed_emit_drift.py",
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        "committed-emit-drift oracle failed after emit_schema.py's C2 declaration-only "
        f"change:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _git_init_and_commit_all(root: Path) -> None:
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "a@b"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(
            cmd,
            cwd=str(root),
            check=True,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(root),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "commit", "-qm", "init"],
        cwd=str(root),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_valid_mutates_reports_mutates_declared_leaves_unresolved_no_pairs(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/mutator_valid.py",
        """
from pathlib import Path

MUTATES = ["state/*.yaml"]

def run(target):
    Path(target).write_text("{}")
""",
    )
    _write(tmp_path, "state/foo.yaml", "x: 1\n")
    _git_init_and_commit_all(tmp_path)

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/mutator_valid.py"]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.pairs == ()
    assert record.mutates == ("state/*.yaml",)


def test_mutates_and_generates_together_pairs_resolve_from_generates_both_honoured(tmp_path):
    (tmp_path / "src.txt").write_text("x", encoding="utf-8")
    _write(
        tmp_path,
        "coordinator_core/mutator_and_generator.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]},
]
MUTATES = ["state/*.yaml"]

def run():
    Path("out.json").write_text("{}")
""",
    )
    _write(tmp_path, "state/foo.yaml", "x: 1\n")
    _git_init_and_commit_all(tmp_path)

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/mutator_and_generator.py"]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict is None
    assert len(record.pairs) == 1
    assert record.pairs[0].artifact == "out.json"
    assert record.mutates == ("state/*.yaml",)


def test_mutates_non_list_resolves_undeclared(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/mutator_not_a_list.py",
        """
from pathlib import Path

MUTATES = "state/**/*.yaml"

def run(target):
    Path(target).write_text("{}")
""",
    )
    _write(tmp_path, "state/foo.yaml", "x: 1\n")
    _git_init_and_commit_all(tmp_path)

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/mutator_not_a_list.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "MUTATES" in matches[0].detail


def test_mutates_empty_list_resolves_undeclared(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/mutator_empty_list.py",
        """
from pathlib import Path

MUTATES = []

def run(target):
    Path(target).write_text("{}")
""",
    )
    _write(tmp_path, "state/foo.yaml", "x: 1\n")
    _git_init_and_commit_all(tmp_path)

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/mutator_empty_list.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "MUTATES" in matches[0].detail


def test_mutates_non_string_element_resolves_undeclared(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/mutator_bad_element.py",
        """
from pathlib import Path

MUTATES = ["state/**/*.yaml", 7]

def run(target):
    Path(target).write_text("{}")
""",
    )
    _write(tmp_path, "state/foo.yaml", "x: 1\n")
    _git_init_and_commit_all(tmp_path)

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/mutator_bad_element.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "MUTATES" in matches[0].detail


def test_mutates_non_literal_resolves_undeclared(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/mutator_non_literal.py",
        """
from pathlib import Path

MUTATES = [str(x) for x in range(1)]

def run(target):
    Path(target).write_text("{}")
""",
    )
    _write(tmp_path, "state/foo.yaml", "x: 1\n")
    _git_init_and_commit_all(tmp_path)

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/mutator_non_literal.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "MUTATES" in matches[0].detail


def test_mutates_matching_nothing_tracked_resolves_undeclared_naming_patterns(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/mutator_no_match.py",
        """
from pathlib import Path

MUTATES = ["nonexistent-dir/**/*.md"]

def run(target):
    Path(target).write_text("{}")
""",
    )
    _write(tmp_path, "state/foo.yaml", "x: 1\n")
    _git_init_and_commit_all(tmp_path)

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/mutator_no_match.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "nonexistent-dir/**/*.md" in matches[0].detail


def test_module_with_neither_declaration_unaffected_by_mutates_addition(tmp_path):
    _write(
        tmp_path,
        "coordinator_core/gen_neither.py",
        """
from pathlib import Path

def run():
    Path("out.json").write_text("{}")
""",
    )

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/gen_neither.py"]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert matches[0].mutates == ()


def test_regression_guard_five_declared_generators_and_canary_unaffected_by_mutates():
    """MUTATES adoption must not disturb the artifact-declaring generators.

    This guard deliberately does NOT assert a tree-wide `MUTATES_DECLARED`
    count. It once asserted zero, which held only while nothing had adopted
    the form; every legitimate corpus-mutator declaration then broke it, so
    the count was measuring adoption rather than regression. What must stay
    true is narrower and permanent: the artifact-declaring generators keep
    resolving their pairs, the canary stays visible, and neither is ever
    reclassified as a mutator -- a module with a fixed tracked artifact owes
    `GENERATES`, and `MUTATES` is never a substitute for it (DR-305).
    """
    records = discover_generators(_REPO_ROOT)
    by_name = {r.generator: r for r in records}

    for rel in _C2_TARGETS:
        assert rel in by_name
        assert by_name[rel].verdict is None
        assert len(by_name[rel].pairs) >= 1
        assert by_name[rel].verdict != Verdict.MUTATES_DECLARED

    assert _CANARY in by_name
    assert by_name[_CANARY].verdict == Verdict.WRITE_TARGET_UNRESOLVED


def _write_mutator_with_pattern(tmp_path: Path, module_name: str, pattern: str) -> str:
    rel = f"coordinator_core/{module_name}.py"
    _write(
        tmp_path,
        rel,
        f"""
from pathlib import Path

MUTATES = {pattern!r}

def run(target):
    Path(target).write_text("{{}}")
""",
    )
    _write(tmp_path, "state/sub/foo.yaml", "x: 1\n")
    _write(tmp_path, "docs/plans/foo.md", "x\n")
    _git_init_and_commit_all(tmp_path)
    return rel


def test_mutates_catch_all_double_star_slash_star_resolves_undeclared(tmp_path):
    rel = _write_mutator_with_pattern(tmp_path, "mutator_catch_all", ["**/*"])
    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == rel]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "**/*" in matches[0].detail


def test_mutates_bare_star_resolves_undeclared(tmp_path):
    rel = _write_mutator_with_pattern(tmp_path, "mutator_bare_star", ["*"])
    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == rel]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "*" in matches[0].detail


def test_mutates_bare_double_star_resolves_undeclared(tmp_path):
    rel = _write_mutator_with_pattern(tmp_path, "mutator_bare_double_star", ["**"])
    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == rel]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "**" in matches[0].detail


def test_mutates_literal_segment_and_extension_still_declared(tmp_path):
    rel = _write_mutator_with_pattern(tmp_path, "mutator_literal_segment", ["state/**/*.yaml"])
    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == rel]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.MUTATES_DECLARED
    assert matches[0].mutates == ("state/**/*.yaml",)


def test_mutates_literal_extension_only_still_declared(tmp_path):
    rel = _write_mutator_with_pattern(tmp_path, "mutator_literal_extension", ["**/*.py"])
    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == rel]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.MUTATES_DECLARED
    assert matches[0].mutates == ("**/*.py",)


def test_mutates_broad_element_poisons_otherwise_valid_list(tmp_path):
    rel = _write_mutator_with_pattern(
        tmp_path, "mutator_mixed_list", ["state/**/*.yaml", "**/*"]
    )
    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == rel]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert "**/*" in matches[0].detail


def test_mutates_bare_literal_filename_resolves_undeclared_not_mutates_declared(tmp_path):
    """P2-1: a concrete, wildcard-free filename in MUTATES is a fixed
    artifact in disguise -- it must not be accepted as MUTATES_DECLARED
    even though it carries a literal segment and matches a tracked path.
    `git init` is used (not a bare tmp_path fixture) so this genuinely
    exercises the tracked-match rule rather than the `tracked is None`
    fail-open path -- see the sidecar's vacuous-test check."""
    rel = _write_mutator_with_pattern(tmp_path, "mutator_bare_literal", ["state/sub/foo.yaml"])
    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == rel]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.UNDECLARED
    assert matches[0].mutates == ()
    assert "state/sub/foo.yaml" in matches[0].detail
    assert "concrete path" in matches[0].detail
    assert "GENERATES" in matches[0].detail


def test_mutates_wildcard_bearing_pattern_still_declared(tmp_path):
    """Companion to the bare-literal rejection above: a pattern carrying a
    real wildcard alongside its literal segment must still be accepted --
    the new rule must not regress the already-accepted forms."""
    rel = _write_mutator_with_pattern(tmp_path, "mutator_wildcard_ok", ["state/*.yaml"])
    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == rel]
    assert len(matches) == 1
    assert matches[0].verdict == Verdict.MUTATES_DECLARED
    assert matches[0].mutates == ("state/*.yaml",)


def test_mutates_invalid_bare_literal_does_not_swallow_valid_generates(tmp_path):
    """P2-2: an invalid MUTATES (here, P2-1's bare-literal case) must not
    short-circuit a co-declared valid GENERATES -- the GENERATES pairs
    still resolve, and the MUTATES problem is still reported, not silently
    dropped either way."""
    (tmp_path / "src.txt").write_text("x", encoding="utf-8")
    _write(
        tmp_path,
        "coordinator_core/mutator_bad_and_generator_good.py",
        """
from pathlib import Path

GENERATES = [
    {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]},
]
MUTATES = ["state/foo.yaml"]

def run():
    Path("out.json").write_text("{}")
""",
    )
    _write(tmp_path, "state/foo.yaml", "x: 1\n")
    _git_init_and_commit_all(tmp_path)

    records = discover_generators(tmp_path)
    matches = [r for r in records if r.generator == "coordinator_core/mutator_bad_and_generator_good.py"]
    assert len(matches) == 1
    record = matches[0]
    assert record.verdict is None
    assert len(record.pairs) == 1
    assert record.pairs[0].artifact == "out.json"
    assert record.mutates == ()
    assert "state/foo.yaml" in record.detail
    assert "concrete path" in record.detail
