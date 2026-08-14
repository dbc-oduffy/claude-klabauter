"""tests/test_handoff_has_live_children.py — Tests for bin/handoff-has-live-children.py.

Native-Python successor to the retired test-handoff-has-live-children.bats harness
(2026-07-19 Windows de-bash campaign, W1b). Exercises the veneer by spawning it under
the same interpreter running this test — no bash/bats on the critical path.

Covers (parity with the retired bats suite):
  (a) --exclude threaded: a candidate referenced only by the excluded successor exits 1
      (safe-to-archive); without --exclude it exits 0 (has live children).
  (b) error paths exit 2 (not exit 0 or a traceback): missing-arg, candidate-not-found,
      unknown-flag.
  (c) --edge-kinds passthrough: narrowing to predecessor+additional_predecessors still
      detects a predecessor-field reference.

Fixtures build a synthetic git worktree under pytest's own `tmp_path` — a real
`git init` repo with `state/handoffs/` underneath — and write the candidate/successor
handoffs there. The veneer's own `_resolve_repo_root()` does candidate-dir discovery
(`git -C <candidate-dir> rev-parse --show-toplevel`) specifically so a fixture repo
works exactly like a production one; there is no need to write into this repo's own
tracked `state/handoffs/` (see coordinator_core/tests/test_handoff_children.py, which
isolates the same op the same way). tmp_path's own per-test cleanup means no explicit
teardown is needed either.

Spec backlink: docs/plans/2026-06-30-chain-review-coverage-dag-consumer.md § C1 / AC2 / AC12
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Wave 1, unit W1b)

Run: python3 -m pytest coordinator/bin/tests/test_handoff_has_live_children.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from coordinator_core.win_portability import no_console_creationflags

_HERE = os.path.dirname(os.path.abspath(__file__))
VENEER = os.path.normpath(os.path.join(_HERE, "..", "handoff-has-live-children.py"))


def _run_veneer(*args):
    proc = subprocess.run(
        [sys.executable, VENEER, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        **no_console_creationflags(),
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Fixtures — an isolated, disposable git worktree under tmp_path (never the
# real tracked state/handoffs/): candidate/successor handoffs write here.
#
# 3e818e6b converted this file's filename to test_*.py so pytest would
# collect it, but left the pre-conversion hand-rolled main()-supplied-args
# shape in place: the test functions took plain positional parameters
# (`candidate`, `successor`, `tmp_candidate`) that only ever got values from
# main()'s own ad hoc harness, never from pytest. Under real pytest
# collection those parameter names are read as fixture requests, and none
# were defined — a setup ERROR on all four, not an assertion failure. This
# is the actual conversion the 63-file migration owed this file.
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """A throwaway `git init`-ed worktree with a state/handoffs/ subtree — the
    veneer resolves repo_root via `git -C <candidate-dir> rev-parse
    --show-toplevel`, a real git call, so this needs an actual repo, not just
    a `.git/` directory stand-in (unlike coordinator_core's op-level test,
    which calls the async op directly and never shells out to git)."""
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def candidate(repo):
    path = repo / "state" / "handoffs" / "test-candidate.md"
    path.write_text(
        "---\n"
        "title: \"py-test candidate\"\n"
        "created: 2026-07-19\n"
        "branch: work/test/2026-07-19\n"
        "status: active\n"
        "predecessor: none\n"
        "category: test\n"
        "summary: \"Temporary python-port test fixture -- safe to delete.\"\n"
        "---\n"
        "# Test candidate\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def successor(repo, candidate):
    path = repo / "state" / "handoffs" / "test-successor.md"
    path.write_text(
        "---\n"
        "title: \"py-test successor\"\n"
        "created: 2026-07-19\n"
        "branch: work/test/2026-07-19\n"
        "status: active\n"
        "predecessor: {candidate_basename}\n"
        "category: test\n"
        "summary: \"Temporary python-port test fixture -- safe to delete.\"\n"
        "---\n"
        "# Test successor\n".format(candidate_basename=os.path.basename(candidate)),
        encoding="utf-8",
    )
    return str(path)


# ---------------------------------------------------------------------------
# (b) Error paths — exit 2 (not 0, not a traceback)
# ---------------------------------------------------------------------------

def test_missing_arg_exits_2():
    rc, _, _ = _run_veneer()
    assert rc == 2, "expected exit 2 (missing arg), got {}".format(rc)


def test_candidate_not_found_exits_2():
    rc, _, _ = _run_veneer("/tmp/hlc-py-test-nonexistent-{}.md".format(int(time.time())))
    assert rc == 2, "expected exit 2 (candidate not found), got {}".format(rc)


def test_unknown_flag_exits_2(candidate):
    rc, _, _ = _run_veneer("--unknown-flag", candidate)
    assert rc == 2, "expected exit 2 (unknown flag), got {}".format(rc)


# ---------------------------------------------------------------------------
# (a) --exclude threaded, and (c) --edge-kinds passthrough
# ---------------------------------------------------------------------------

def test_baseline_has_live_children(candidate, successor):
    del successor  # referenced implicitly via the live handoff set on disk
    rc, out, err = _run_veneer(candidate)
    assert rc == 0, "expected exit 0 (has live children), got {} stdout={!r} stderr={!r}".format(rc, out, err)


def test_exclude_successor_safe_to_archive(candidate, successor):
    rc, out, err = _run_veneer("--exclude", successor, candidate)
    assert rc == 1, "expected exit 1 (safe to archive), got {} stdout={!r} stderr={!r}".format(rc, out, err)


def test_edge_kinds_passthrough(candidate, successor):
    del successor
    rc, out, err = _run_veneer("--edge-kinds", "predecessor,additional_predecessors", candidate)
    assert rc == 0, "expected exit 0 (predecessor-field reference detected), got {} stdout={!r} stderr={!r}".format(rc, out, err)
