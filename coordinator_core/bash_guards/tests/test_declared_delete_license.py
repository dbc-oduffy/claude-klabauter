"""Tests for the declared-delete license module (pure, everything under tmp_path)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _declared_delete_license as lic

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-10-25-demo-20261025T000000-abcdef"
ROW = "C1"
SESSION = "sess-emitter"


def _write_run(root: Path, *, writes=("pkg/a.py",), session=SESSION, receipt=True, row=ROW) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT

    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    manifest = {
        "run_dir": str(rec),
        "rows": [
            {
                "id": row,
                "agent_type": "x",
                "model": "m",
                "brief_path": "b.md",
                "writes": list(writes),
                "wave": 0,
            }
        ],
        "review_declared_paths": [],
        "marker_path": "m",
    }
    (rec / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if receipt:
        run_dir = root / RUN_DIR_ROOT
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"{RUN}.workflow.mjs.emitted.json").write_text(
            json.dumps({"session_id": session}), encoding="utf-8"
        )


MARK = f"{lic.MARKER_VAR}={RUN}/{ROW}"


def test_parse_marker_single():
    assert lic.parse_marker(f"{MARK} git rm -q -- a.py") == lic.Marker(RUN, ROW)


def test_parse_marker_absent_and_unparseable():
    assert lic.parse_marker("git rm -q -- a.py") is None
    assert lic.parse_marker("") is None
    assert lic.parse_marker(f"{MARK} git rm 'unterminated") is None


def test_parse_marker_two_distinct_values_ambiguous():
    cmd = f"{MARK} git rm -- a ; {lic.MARKER_VAR}={RUN}/C2 git rm -- b"
    assert lic.parse_marker(cmd) is lic.AMBIGUOUS


def test_parse_marker_same_value_twice_is_single():
    assert lic.parse_marker(f"{MARK} rm -- a ; {MARK} rm -- b") == lic.Marker(RUN, ROW)


def test_parse_marker_not_in_prefix_position():
    assert lic.parse_marker(f"git rm -- a {MARK}") is None
    assert lic.parse_marker(f"export {MARK}; git rm -- a") is None
    assert lic.parse_marker(f"echo {MARK}") is None


def test_parse_marker_after_other_assignment_prefix():
    assert lic.parse_marker(f"FOO=1 {MARK} rm -- a") == lic.Marker(RUN, ROW)


@pytest.mark.parametrize(
    "value",
    ["../evil/C1", "a b/C1", f"{RUN}/", f"{RUN}/C 1", f"{RUN}/..", "nosep", "/C1"],
)
def test_parse_marker_unsafe_value(value):
    assert lic.parse_marker(f"{lic.MARKER_VAR}='{value}' rm -- a") is None


def test_resolve_license_happy(tmp_path):
    _write_run(tmp_path, writes=("pkg/a.py", "./pkg\\b.py"))
    got = lic.resolve_license(tmp_path, lic.Marker(RUN, ROW), SESSION)
    assert got == lic.License(RUN, ROW, frozenset({"pkg/a.py", "pkg/b.py"}))


def test_resolve_license_refusals(tmp_path):
    assert isinstance(lic.resolve_license(tmp_path, lic.Marker(RUN, ROW), SESSION), str)
    _write_run(tmp_path)
    assert isinstance(lic.resolve_license(tmp_path, lic.Marker(RUN, "C9"), SESSION), str)
    assert isinstance(lic.resolve_license(tmp_path, lic.Marker(RUN, ROW), "other"), str)
    assert isinstance(lic.resolve_license(tmp_path, lic.Marker(RUN, ROW), ""), str)
    assert isinstance(lic.resolve_license(tmp_path, lic.Marker("unknown-run", ROW), SESSION), str)


def test_resolve_license_no_receipt(tmp_path):
    _write_run(tmp_path, receipt=False)
    assert isinstance(lic.resolve_license(tmp_path, lic.Marker(RUN, ROW), SESSION), str)


def test_resolve_license_corrupt_manifest(tmp_path):
    _write_run(tmp_path)
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    (tmp_path / RECORD_DIR_ROOT / RUN / "manifest.json").write_text("{not json", encoding="utf-8")
    assert isinstance(lic.resolve_license(tmp_path, lic.Marker(RUN, ROW), SESSION), str)


def test_normalize_operand_relative_forms(tmp_path):
    cwd = str(tmp_path)
    assert lic.normalize_operand("pkg/a.py", cwd, tmp_path) == "pkg/a.py"
    assert lic.normalize_operand("./pkg/a.py", cwd, tmp_path) == "pkg/a.py"
    assert lic.normalize_operand("pkg\\a.py", cwd, tmp_path) == "pkg/a.py"
    assert lic.normalize_operand("a.py", str(tmp_path / "pkg"), tmp_path) == "pkg/a.py"
    assert lic.normalize_operand("a.py", "", tmp_path) == "a.py"


def test_normalize_operand_absolute(tmp_path):
    inside = str(tmp_path / "pkg" / "a.py")
    assert lic.normalize_operand(inside, str(tmp_path), tmp_path) == "pkg/a.py"
    assert lic.normalize_operand(inside.replace("/", "\\"), str(tmp_path), tmp_path) == "pkg/a.py"
    outside = str(tmp_path.parent / "elsewhere" / "a.py")
    assert lic.normalize_operand(outside, str(tmp_path), tmp_path) is None
    assert lic.normalize_operand(str(tmp_path), str(tmp_path), tmp_path) is None


def test_normalize_operand_escape_and_sibling_prefix(tmp_path):
    cwd = str(tmp_path)
    assert lic.normalize_operand("../x.py", cwd, tmp_path) is None
    assert lic.normalize_operand("pkg/../../x.py", cwd, tmp_path) is None
    assert lic.normalize_operand("pkg/../a.py", cwd, tmp_path) == "a.py"
    sibling = str(tmp_path) + "-other/a.py"
    assert lic.normalize_operand(sibling, cwd, tmp_path) is None


@pytest.mark.parametrize("op", ["*.py", "a?.py", "a[1].py", "{a,b}", ":(top)a.py", ":/a.py", "$HOME/a", ""])
def test_normalize_operand_refuses_magic(tmp_path, op):
    assert lic.normalize_operand(op, str(tmp_path), tmp_path) is None


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_tracked_subset_real_index(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "tracked.py").write_text("x", encoding="utf-8")
    (tmp_path / "untracked.py").write_text("y", encoding="utf-8")
    _git(tmp_path, "add", "tracked.py")
    got = lic.tracked_subset(tmp_path, ["tracked.py", "untracked.py", "missing.py"])
    assert got == frozenset({"tracked.py"})


def test_tracked_subset_unreadable_index_is_none(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".git" / "index").write_bytes(b"garbage")
    assert lic.tracked_subset(tmp_path, ["a.py"]) is None


def test_tracked_subset_split_index_is_none(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "t.py").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "t.py")
    (tmp_path / ".git" / "sharedindex.deadbeef").write_bytes(b"")
    assert lic.tracked_subset(tmp_path, ["t.py"]) is None


def test_brief_clause_content():
    clause = lic.brief_clause(RUN, ROW)
    assert "\n" not in clause
    assert f"{lic.MARKER_VAR}={RUN}/{ROW} git rm -q -- <path>" in clause
    assert f"{lic.MARKER_VAR}={RUN}/{ROW} rm -- <path>" in clause
    assert "-r" in clause and "-f" in clause and "not declare" in clause
    assert lic.parse_marker(clause.split("`")[1] + " x") == lic.Marker(RUN, ROW)


def test_module_calls_spawn_nothing(tmp_path, monkeypatch):
    _write_run(tmp_path)
    _git(tmp_path, "init", "-q")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "pkg/a.py")

    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    marker = lic.parse_marker(f"{MARK} git rm -q -- pkg/a.py")
    got = lic.resolve_license(tmp_path, marker, SESSION)
    assert isinstance(got, lic.License)
    rel = lic.normalize_operand("pkg/a.py", str(tmp_path), tmp_path)
    assert rel in got.paths
    assert lic.tracked_subset(tmp_path, [rel]) == frozenset({rel})
    assert lic.brief_clause(RUN, ROW)
