"""land_run.foreign_halted_withheld: a hold that cannot be computed holds every wanted path.

Also the review_reverify peer-withheld keep, the halted-record review-edit hold, and the hunk differ's process-time bound on a land_run.py-sized file (`own_hunk_stage`, `run_hunk_claims`).
"""


from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo as product_repo  # noqa: F401  (fixture)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import repo  # noqa: F401  (fixture)
from coordinator_core.ops.tests.test_review_reverify import BASE, PREFIX, SESSION, world  # noqa: F401  (fixture)

import pytest
from pathlib import Path

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_LINES = 4000
_BAR_SECONDS = 0.15  # DR-344 bar is 500ms; process time, never wall clock


_PASS = {"status_before": "pass", "status_after": "pass", "failing": []}
_NO_TESTS = "PARTIAL: C1\nNot done: pytest not run; the likeness pytest fence bars the executor from running tests.\n"


_PRE_SEED = "".join(f"line{i}\n" for i in range(1, 13))
_PRE_FIX = _PRE_SEED.replace("line4\n", "fixed-four\n")
_PRE_REL = "pkg/guard.py"


def _crlf_repo(tmp_path):
    import subprocess

    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.win_portability import no_console_creationflags

    for cmd in (("init", "-q"), ("config", "user.email", "t@e.com"), ("config", "user.name", "t"),
                ("config", "commit.gpgsign", "false"), ("config", "core.autocrlf", "true")):
        subprocess.run(["git", *cmd], cwd=str(tmp_path), check=True, capture_output=True, **no_console_creationflags())
    (tmp_path / _PRE_REL).parent.mkdir()
    (tmp_path / _PRE_REL).write_bytes(_PRE_SEED.encode())
    for cmd in (("add", _PRE_REL), ("commit", "-q", "-m", "seed")):
        subprocess.run(["git", *cmd], cwd=str(tmp_path), check=True, capture_output=True, **no_console_creationflags())
    # The worktree carries a pre-existing CRLF hunk, as autocrlf checkouts do; the baseline is its raw hash.
    (tmp_path / _PRE_REL).write_bytes(_PRE_FIX.replace("\n", "\r\n").encode())
    common = resolve_git_common_dir(tmp_path)
    raw = (tmp_path / _PRE_REL).read_bytes()
    return {
        "baseline": {_PRE_REL: git_objects.write_object(common, b"blob", raw)},
        "measured": git_objects.write_object(common, b"blob", _PRE_FIX.encode()),
    }


def test_crlf_baseline_pre_existing_hunk_is_not_the_runs_own(tmp_path: Path) -> None:
    seen = _crlf_repo(tmp_path)

    assert land_run._peer_dirt_only(tmp_path, _PRE_REL, seen["baseline"])


def test_verified_pre_existing_hunk_is_recognised_only_while_the_worktree_matches_the_measure(tmp_path: Path) -> None:
    seen = _crlf_repo(tmp_path)

    assert land_run._verified_preexisting(tmp_path, _PRE_REL, seen["measured"], seen["baseline"])
    assert not land_run._verified_preexisting(tmp_path, _PRE_REL, "0" * 40, seen["baseline"])
    assert not land_run._verified_preexisting(tmp_path, _PRE_REL, None, seen["baseline"])
    (tmp_path / _PRE_REL).write_bytes(_PRE_FIX.replace("line9\n", "late-nine\n").replace("\n", "\r\n").encode())
    assert not land_run._verified_preexisting(tmp_path, _PRE_REL, seen["measured"], seen["baseline"])


def test_a_run_own_hunk_is_never_taken_for_a_verified_pre_existing_hunk(tmp_path: Path) -> None:
    seen = _crlf_repo(tmp_path)
    own = _PRE_FIX.replace("line10\n", "own-ten\n")
    (tmp_path / _PRE_REL).write_bytes(own.replace("\n", "\r\n").encode())
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir

    measured = git_objects.write_object(resolve_git_common_dir(tmp_path), b"blob", own.encode())

    assert not land_run._verified_preexisting(tmp_path, _PRE_REL, measured, seen["baseline"])


def test_mismatch_detail_names_the_differing_line_and_the_cause_specific_remedy(tmp_path: Path) -> None:
    seen = _crlf_repo(tmp_path)
    blobs = {_PRE_REL: seen["measured"]}

    held = land_run._mismatch_detail(tmp_path, _PRE_REL, blobs, {})
    assert "hand-commit this path whole" in held and held.startswith(_PRE_REL)

    (tmp_path / _PRE_REL).write_bytes(_PRE_FIX.replace("line9\n", "late-nine\n").replace("\n", "\r\n").encode())
    drifted = land_run._mismatch_detail(tmp_path, _PRE_REL, blobs, {})
    assert "first difference at line 9: measured 'line9', landing 'late-nine'" in drifted
    assert "re-run dispatch.measured_rev" in drifted and "hand-commit" not in drifted


from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo as ask_run_repo  # noqa: E402,F401


# ---- tranche chain hand-off --------------------------------------------------------------------
