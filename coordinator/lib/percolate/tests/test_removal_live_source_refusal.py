"""A removal whose source path still exists refuses the round before any write.

Condition of assent from coordinator-content-repo-em (2026-08-26, re-affirmed 2026-10-08 for DR-457's
`manifest.removed` source of deletions), "in the code, not in the procedure". Both known
witnesses of the published-but-never-scanned class are fixtures here.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate.diff_commit import RemovalSourceLiveError, refuse_removals_with_live_source  # noqa: E402

_PUBLISH = Path(__file__).resolve().parents[3] / "bin" / "publish.py"


def test_manifest_removed_entry_with_a_live_source_refuses(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "still_here.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(RemovalSourceLiveError) as excinfo:
        refuse_removals_with_live_source(tmp_path, ["pkg/still_here.py"], "claude-klabauter")

    msg = str(excinfo.value)
    assert "pkg/still_here.py" in msg and "manifest.removed, row claude-klabauter" in msg


def test_refuses_when_a_candidate_is_still_on_disk(tmp_path):
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    live = ".github/scripts/check-persona-names.py"
    (tmp_path / live).write_text("BANNED = []\n", encoding="utf-8")

    with pytest.raises(RemovalSourceLiveError) as excinfo:
        refuse_removals_with_live_source(tmp_path, [live, "gone/from/disk.py"], "row")

    msg = str(excinfo.value)
    assert live in msg
    assert "gone/from/disk.py" not in msg


def test_binary_in_a_declared_directory_is_caught(tmp_path):
    (tmp_path / "coordinator_core" / "warm" / "door").mkdir(parents=True)
    binary = "coordinator_core/warm/door/door.exe"
    (tmp_path / binary).write_bytes(b"MZ\x90\x00")

    with pytest.raises(RemovalSourceLiveError):
        refuse_removals_with_live_source(tmp_path, [binary], "row")


def test_broken_symlink_counts_as_live(tmp_path):
    link = tmp_path / "dangling"
    try:
        link.symlink_to(tmp_path / "missing-target")
    except OSError:
        pytest.skip("symlink creation not permitted on this host")

    with pytest.raises(RemovalSourceLiveError):
        refuse_removals_with_live_source(tmp_path, ["dangling"], "row")


def test_genuine_orphans_pass_through(tmp_path):
    refuse_removals_with_live_source(tmp_path, ["bin/migrated-away.py", "skills/repo-setup/residue/x.md"], "row")


def test_empty_candidate_set_is_a_noop(tmp_path):
    refuse_removals_with_live_source(tmp_path, [], "row")


def test_message_caps_the_list_but_reports_the_true_count(tmp_path):
    names = []
    for i in range(25):
        rel = f"payload-{i:02d}.py"
        (tmp_path / rel).write_text("x\n", encoding="utf-8")
        names.append(rel)

    with pytest.raises(RemovalSourceLiveError) as excinfo:
        refuse_removals_with_live_source(tmp_path, names, "row")

    msg = str(excinfo.value)
    assert "names 25 removal(s)" in msg
    assert "... and 5 more" in msg


def test_the_round_union_calls_the_refusal_per_staged_row():
    tree = ast.parse(_PUBLISH.read_text(encoding="utf-8"))
    union = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_assemble_root_union")
    calls = [
        n for n in ast.walk(union)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "refuse_removals_with_live_source"
    ]
    assert calls, "_assemble_root_union no longer refuses removals with a live source"
