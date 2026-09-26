from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.commit_gates import attribution_gate
from coordinator_core.telemetry import spawn_counter
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ATTR_LINE = "# Review: the Staff Engineer (Finding 1)"


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit(repo: Path, paths: list, message: str = "seed") -> None:
    _git(["add", "--", *paths], repo)
    _git(["commit", "-q", "-m", message], repo)


def test_refuses_added_attribution_line_in_py_file(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "foo.py", "x = 1\n")
    _commit(repo, ["foo.py"])
    _seed_file(repo, "foo.py", f"x = 1\n{_ATTR_LINE}\n")

    outcome = attribution_gate(repo, ["foo.py"])

    assert not outcome.passed
    assert not outcome.skipped
    assert any("foo.py:2:" in d for d in outcome.diagnostics)
    assert any("adds reviewer attribution" in d for d in outcome.diagnostics)


def test_exempt_paths_skip_without_reading(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "state/handoffs/x.md", "seed\n")
    _seed_file(repo, "docs/plans/x.md", "seed\n")
    _commit(repo, ["state/handoffs/x.md", "docs/plans/x.md"])
    _seed_file(repo, "state/handoffs/x.md", f"seed\n{_ATTR_LINE}\n")
    _seed_file(repo, "docs/plans/x.md", f"seed\n{_ATTR_LINE}\n")

    outcome = attribution_gate(
        repo, ["state/handoffs/x.md", "docs/plans/x.md"]
    )

    assert outcome.passed
    assert outcome.skipped
    assert outcome.diagnostics == []


def test_passes_on_unchanged_legacy_line_with_unrelated_edit(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "foo.py", f"{_ATTR_LINE}\nold_line\n")
    _commit(repo, ["foo.py"])
    _seed_file(repo, "foo.py", f"{_ATTR_LINE}\nnew_line\n")

    outcome = attribution_gate(repo, ["foo.py"])

    assert outcome.passed
    assert outcome.diagnostics == []


def test_refuses_edit_to_existing_legacy_attribution_line(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "foo.py", f"{_ATTR_LINE}\n")
    _commit(repo, ["foo.py"])
    _seed_file(repo, "foo.py", "# Review: the Staff Engineer (Finding 2)\n")

    outcome = attribution_gate(repo, ["foo.py"])

    assert not outcome.passed


def test_passes_staged_rename_of_legacy_carrying_file_unmodified(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "old_path.py", f"{_ATTR_LINE}\n")
    _commit(repo, ["old_path.py"])
    _git(["mv", "old_path.py", "new_path.py"], repo)

    outcome = attribution_gate(repo, ["old_path.py", "new_path.py"])

    assert outcome.passed
    assert outcome.diagnostics == []


def test_passes_staged_rename_of_legacy_carrying_file_with_unrelated_edit(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "old_path.py", f"{_ATTR_LINE}\nkeep\n")
    _commit(repo, ["old_path.py"])
    _git(["mv", "old_path.py", "new_path.py"], repo)
    _seed_file(repo, "new_path.py", f"{_ATTR_LINE}\nkeep\nunrelated new line\n")

    outcome = attribution_gate(repo, ["old_path.py", "new_path.py"])

    assert outcome.passed
    assert outcome.diagnostics == []


def test_refuses_new_head_absent_file_that_vacates_nothing(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "unrelated.py", "x = 1\n")
    _commit(repo, ["unrelated.py"])
    _seed_file(repo, "brand_new.py", f"{_ATTR_LINE}\n")

    outcome = attribution_gate(repo, ["brand_new.py"])

    assert not outcome.passed


def test_refuses_moved_file_gaining_attribution_the_rename_source_lacked(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "old_path2.py", "plain\n")
    _commit(repo, ["old_path2.py"])
    _git(["mv", "old_path2.py", "new_path2.py"], repo)
    _seed_file(repo, "new_path2.py", f"plain\n{_ATTR_LINE}\n")

    outcome = attribution_gate(repo, ["old_path2.py", "new_path2.py"])

    assert not outcome.passed


def test_passes_on_doctrine_break_class(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "foo.py", "x = 1\n")
    _commit(repo, ["foo.py"])
    _seed_file(
        repo,
        "foo.py",
        "x = 1\n# Break-class defects are fixed by default, not deferred.\n",
    )

    outcome = attribution_gate(repo, ["foo.py"])

    assert outcome.passed
    assert outcome.diagnostics == []


def test_refuses_added_attribution_line_in_crlf_file(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "coordinator").mkdir(parents=True, exist_ok=True)
    rel = "coordinator/launcher.cmd"
    (repo / rel).write_bytes(b"@echo seed\r\n")
    _commit(repo, [rel])
    (repo / rel).write_bytes(f"@echo seed\r\n{_ATTR_LINE}\r\n".encode("utf-8"))

    outcome = attribution_gate(repo, [rel])

    assert not outcome.passed


def test_prefer_staged_scans_the_staged_blob_not_the_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "foo.py", "plain\n")
    _commit(repo, ["foo.py"])
    _seed_file(repo, "foo.py", f"plain\n{_ATTR_LINE}\n")
    _git(["add", "--", "foo.py"], repo)
    # Worktree reverts to clean content AFTER staging -- the index still
    # carries the attribution-bearing blob.
    _seed_file(repo, "foo.py", "plain\n")

    without_prefer = attribution_gate(repo, ["foo.py"])
    assert without_prefer.passed

    with_prefer = attribution_gate(repo, ["foo.py"], prefer_staged=["foo.py"])
    assert not with_prefer.passed


def test_index_blob_scanned_when_worktree_missing_but_still_staged(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "foo.py", "plain\n")
    _commit(repo, ["foo.py"])
    _seed_file(repo, "foo.py", f"plain\n{_ATTR_LINE}\n")
    _git(["add", "--", "foo.py"], repo)
    (repo / "foo.py").unlink()

    outcome = attribution_gate(repo, ["foo.py"])

    assert not outcome.passed


def test_genuine_deletion_is_not_scanned_as_added_lines(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "baz.py", f"{_ATTR_LINE}\n")
    _commit(repo, ["baz.py"])
    _git(["rm", "-q", "baz.py"], repo)

    outcome = attribution_gate(repo, ["baz.py"])

    assert outcome.passed
    assert outcome.diagnostics == []


def test_attribution_gate_spawns_no_subprocess(tmp_path):
    """0 spawns, asserted through the real audit-hook-based counter
    (`coordinator_core.telemetry.spawn_counter`) rather than a
    `subprocess.Popen`/`run` monkeypatch, which can read 0 by construction
    if the gate reaches git through a helper the patch misses. This repo's
    `LiveTreeAccountant` job-object instrument (used by
    `benchmarks/probe_commit_gates.py`) is Windows-only and would raise
    `NotImplementedError` here -- `spawn_counter` is the cross-platform
    sibling instrument, built for exactly this per-op delta question, and
    keeps this test running on every OS per this plan's own
    multi-os-first-class brightline.
    """
    repo = _init_repo(tmp_path)
    for i in range(6):
        _seed_file(repo, f"m{i}.py", "x = 1\n")
    _commit(repo, [f"m{i}.py" for i in range(6)])
    for i in range(6):
        _seed_file(repo, f"m{i}.py", f"x = 1\n{_ATTR_LINE}\n")

    paths = [f"m{i}.py" for i in range(6)]
    before = spawn_counter.spawn_count()
    outcome = attribution_gate(repo, paths)
    after = spawn_counter.spawn_count()

    assert not outcome.passed
    assert after == before
