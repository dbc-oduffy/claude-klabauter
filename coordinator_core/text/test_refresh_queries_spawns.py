"""Characterization tests for coordinator_core.text.refresh_queries.

Unit 1 (parsing/traversal, below) is Golden-oracle-derived: fixtures were
hand-run against the real node oracle (coordinator/bin/refresh-queries.js,
Coordinator-content-repo) on 2026-07-17 BEFORE this port was authored (per the porter
brief's characterization-first requirement — no pre-existing
characterization test covered this script). Expected outputs/exit-codes/
stderr text asserted there are transcribed directly from that oracle run,
not re-derived from this module's own implementation — independent
re-derivation, not a self-check. These Unit-1 tests need no node and are
unaffected by the de-node conversion below.

Unit 2 (below) exercises `process_file`/`main` end-to-end against the NATIVE
queryRecords()+formatRecords() path (2026-07-22 de-node port — see
refresh_queries.py's "query-records.js dependency" module-docstring note).
The prior de-node Gate A frozen-golden bridge-replay machinery
(`_BridgeReplay`, `_goldens/refresh_queries/*.json`) is retired along with
the node bridge it stood in for: there is no longer a subprocess boundary to
freeze a golden answer against, so these tests use real on-disk record
fixtures (`state/handoffs/*.md`, a deterministically-empty `--where`) and
assert against the file's own on-disk content directly — no node, no
recorded golden, no mock.

TYPE_DISPLAY renderer byte-parity is exercised separately, in
`test_query_record_display.py` (one fixture per ported type, expected
strings transcribed from `bin/query-records.js`'s TYPE_DISPLAY table with
line-number citations — never derived by spawning node).

Oracle commands used to derive the ORIGINAL Unit-1 fixtures (for reproduction):
    node coordinator/bin/refresh-queries.js --root <workdir>
    node coordinator/bin/refresh-queries.js --root <workdir> --check
    node coordinator/bin/refresh-queries.js --root <workdir> --files <a>,<b>,<c>
    node coordinator/bin/refresh-queries.js --root <workdir> --bogus-flag

Port source: coordinator/bin/refresh-queries.js (coordinator-content-repo)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.text.refresh_queries import walk_md


# ---------------------------------------------------------------------------
# Unit 1 — parsing / traversal helpers (no node required)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Unit 2 — refresh/check logic + orchestration, against the NATIVE
# queryRecords()+formatRecords() path (see module docstring). Real on-disk
# record fixtures, no node, no golden replay.
# ---------------------------------------------------------------------------


def _write(p: Path, content: str) -> Path:
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Roadmap-callout archive-follow (cross-repo/inbox/2026-08-04-market-
# intelligence-em-baton-terminal-state-not-cleared-programmatically.md
# defect 2) — a `handoff where=roadmap_id=...` callout must render an
# archived baton's TRUE terminal state and a live archive/ link, never a
# stale `in_flight` against a dead `state/handoffs/` path.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Session self-claim (coordinator_core.session.claims.self_claim) — see
# refresh_queries.py's module-docstring "Session self-claim" note.
# ---------------------------------------------------------------------------


def _git_init(root: Path) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)


def test_walk_md_in_a_git_worktree_is_one_ls_files_spawn(tmp_path, monkeypatch):
    import coordinator_core.git.run as git_run

    _git_init(tmp_path)
    for d in ("sub", "node_modules", "scratch-x", "archive"):
        (tmp_path / d).mkdir()
    _write(tmp_path / "top.md", "x")
    _write(tmp_path / "sub" / "a.md", "x")
    _write(tmp_path / "sub" / "t.txt", "x")
    _write(tmp_path / "node_modules" / "n.md", "x")
    _write(tmp_path / "scratch-x" / "s.md", "x")
    _write(tmp_path / "archive" / "z.md", "x")
    _write(tmp_path / "ignored.md", "x")
    _write(tmp_path / ".gitignore", "ignored.md\n")

    calls = []
    real = git_run.run_git

    def _counting(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", _counting)
    stamps: dict = {}
    found = {os.path.relpath(p, tmp_path) for p in walk_md(str(tmp_path), stamps=stamps)}

    assert found == {"top.md", os.path.join("sub", "a.md")}
    assert len(stamps) == 2
    assert len(calls) == 1 and "ls-files" in calls[0]


def test_walk_md_in_a_git_worktree_stats_no_listed_file_individually(tmp_path, monkeypatch):
    import subprocess

    _git_init(tmp_path)
    (tmp_path / "sub").mkdir()
    for i in range(5):
        _write(tmp_path / "sub" / f"f{i}.md", "x" * i)
    _write(tmp_path / "gone.md", "x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "gone.md"], check=True)
    (tmp_path / "gone.md").unlink()

    real = os.lstat
    md_lstats = []

    def _counting(path, *a, **kw):
        if str(path).endswith(".md"):
            md_lstats.append(str(path))
        return real(path, *a, **kw)

    stamps: dict = {}
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "lstat", _counting)
        found = walk_md(str(tmp_path), stamps=stamps)

    names = sorted(os.path.relpath(p, tmp_path) for p in found)
    assert names == [os.path.join("sub", f"f{i}.md") for i in range(5)]  # tracked-but-deleted file absent
    for p in found:
        st = os.lstat(p)
        assert stamps[p] == (st.st_size, st.st_mtime_ns)
    # Only the vanished entry may cost an individual stat.
    assert [m for m in md_lstats if not m.endswith("gone.md")] == []
