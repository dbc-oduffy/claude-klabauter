"""An undeclared dirty file the run's requirement names is widened in-run and lands in the same
terminal commit; an undeclared dirty file the requirement does not name stays reported and unstaged."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import in_run_widen
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

from .test_ask_run_product_commit import _REVIEWED, _SID, RUN, _git, _put, _write, repo  # noqa: F401 -- fixture

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NAMED = "tests/test_a.py"
OTHER = "other.py"


def _call(repo, *ids):
    verdict = {"items": [{"id": i, "status": "met", "wired": True} for i in ids or ("R1",)]}
    return terminal_commit._handler(
        {
            "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
            "session_id": _SID, "ledger_verdict": verdict,
        },
        repo_root=repo / ".git",
    )


def _tracked_seed(repo) -> None:
    _write(repo, NAMED, "def test_x():\n    assert False\n")
    _write(repo, OTHER, "o = 0\n")
    _git(repo, "add", NAMED, OTHER)
    _git(repo, "commit", "-q", "-m", "seed tests")


def test_requirement_named_undeclared_file_widens_and_lands_other_stays_unstaged(repo):  # noqa: F811
    _tracked_seed(repo)
    rl.revise(repo, RUN, verbatim=f"make {NAMED}::test_x pass")
    _put(repo, NAMED, "def test_x():\n    assert True\n")
    _put(repo, OTHER, "o = 1\n")
    run_dir = repo / RUN_DIR_ROOT / RUN

    before = _git(repo, "rev-parse", "HEAD").strip()

    out = _call(repo, "R1", "R2")

    assert out.get("committed") is True, out
    assert in_run_widen.widened_paths(run_dir) == [NAMED]
    landed = set(_git(repo, "log", "--name-only", "--format=", f"{before}..HEAD").split())
    assert NAMED in landed and OTHER not in landed
    assert _git(repo, "status", "--porcelain", "--", NAMED).strip() == ""
    assert OTHER in _git(repo, "status", "--porcelain", "--", OTHER)
    assert OTHER in (out.get("undeclared_dirty") or []), out
    assert NAMED not in (out.get("undeclared_dirty") or []), out


def test_unnamed_undeclared_file_is_never_widened(repo):  # noqa: F811
    _tracked_seed(repo)
    _put(repo, NAMED, "def test_x():\n    assert True\n")
    run_dir = repo / RUN_DIR_ROOT / RUN

    _call(repo)

    assert in_run_widen.widened_paths(run_dir) == []
    assert NAMED in _git(repo, "status", "--porcelain", "--", NAMED)


def test_requirement_named_but_sizing_excluded_file_is_never_widened(repo):  # noqa: F811
    from .test_ask_run_product_commit import SIZING

    _tracked_seed(repo)
    _write(
        repo, SIZING,
        (repo / SIZING).read_text(encoding="utf-8").replace(
            "Test intent, verbatim.", f"Make {NAMED} pass; do not touch {OTHER}."
        ),
    )
    _git(repo, "add", SIZING)
    _git(repo, "commit", "-q", "-m", "sizing excludes other")
    rl.revise(repo, RUN, verbatim=f"make {NAMED}::test_x pass, and update {OTHER}")
    _put(repo, NAMED, "def test_x():\n    assert True\n")
    _put(repo, OTHER, "o = 1\n")
    run_dir = repo / RUN_DIR_ROOT / RUN
    before = _git(repo, "rev-parse", "HEAD").strip()

    out = _call(repo, "R1", "R2")

    assert out.get("committed") is True, out
    assert in_run_widen.widened_paths(run_dir) == [NAMED]
    landed = set(_git(repo, "log", "--name-only", "--format=", f"{before}..HEAD").split())
    assert NAMED in landed and OTHER not in landed
    assert OTHER in _git(repo, "status", "--porcelain", "--", OTHER)
