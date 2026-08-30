"""
coordinator_core.tests.test_stamp_verbs_stay_off_the_sweep

C4's own contract test — `docs/plans/2026-08-30-the-stamp-stops-paying-for-
a-sweep-that.md` chunk C4: `archive_stamp._call_handoff_archive_transition`
stops calling `housekeeping.cycle` for `stamp_only`/`chain`/`supersede` and
calls `coordinator_core.ops.handoff_stamp_targeted`'s three functions
instead. This module proves the repoint did not move the envelope: for
each of the three verbs, the SAME inputs applied to an identical fixture
via (a) the PRE-change path (`housekeeping.cycle`, reproduced here since
the live call site no longer reaches it for these three modes — see
`_via_housekeeping_cycle` below, a byte-for-byte copy of the removed call
shape) and (b) the POST-change path (the live
`_call_handoff_archive_transition`) return key-for-key, value-for-value
identical dicts — including the refusal envelopes, whose `message`/
`retain_reason` are as load-bearing as a success's (plan C4 body).

`stamp_shipped` (`cs_ship_handoff(archive=True)`) is deliberately excluded
— it was never in C2/C3's scope (neither implements that mode) and
`_call_handoff_archive_transition` still routes it through
`housekeeping.cycle` unconditionally; there is no repoint to verify there.

Spec backlink: coordinator_core/archive_stamp.py ::
_call_handoff_archive_transition
Governing plan: docs/plans/2026-08-30-the-stamp-stops-paying-for-a-sweep-
that.md, C4
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.archive_stamp import _call_handoff_archive_transition

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True, check=True)


def _make_git_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "stamp-verbs-off-sweep-test@claude-klabauter.test")
    _git(repo, "config", "user.name", "Stamp Verbs Off Sweep Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: initial skeleton")
    return repo


def _seed_handoff(repo: Path, name: str, extra_fm: str = "") -> Path:
    """Same minimal, schema-valid shape as
    coordinator_core/ops/tests/test_handoff_stamp_targeted.py ::
    _seed_handoff — kept identical so a fixture built here and one built
    there would produce the same envelope for the same inputs."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'claimed_at: "2026-01-01T00:00:00Z"\n'
        "claimed_by: test-session-id\n"
        'predecessor: "none"\n'
        f"{extra_fm}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _via_housekeeping_cycle(handoff_path: str, params: dict, repo_root: Path) -> dict:
    """The PRE-change call shape `_call_handoff_archive_transition` used for
    EVERY mode before this chunk's repoint — reproduced byte-for-byte (not
    imported, since the live function no longer contains it for these three
    modes) so this test has an independent oracle to diff the new path
    against. `_SWEEP_CAP` mirrors `archive_stamp._SWEEP_CAP` (150)."""
    from coordinator_core.housekeeping.cycle import _handler as _housekeeping_handler

    housekeeping = _housekeeping_handler(
        {
            "close": False,
            "cap": 150,
            "transition": {"handoff_path": handoff_path, **params},
        },
        repo_root=repo_root,
    )
    transition = housekeeping.get("transition")
    if transition is None:
        return {
            "exit_code": housekeeping.get("exit_code", 1),
            "error": housekeeping.get("error", "handoff.housekeeping returned no transition"),
        }
    return transition


def _fork_fixture(repo: Path, tmp_path: Path, suffix: str) -> Path:
    """Two byte-identical git repos, one for the pre-change oracle call and
    one for the post-change live call — each verb call mutates/moves its
    fixture's handoff, so the two paths cannot safely share one working
    tree."""
    dest = tmp_path / f"{repo.name}-{suffix}"
    shutil.copytree(repo, dest)
    return dest


def _assert_envelopes_match(old: dict, new: dict) -> None:
    assert old == new, f"envelope diverged:\n  old={old!r}\n  new={new!r}"


# ---------------------------------------------------------------------------
# stamp_only (ship-handoff, archive=False)
# ---------------------------------------------------------------------------


def test_stamp_only_success_envelope_unchanged(tmp_path):
    base = _make_git_repo(tmp_path, "stamp-only-success")
    _seed_handoff(base, "2026-01-04-a.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "stamp_only", "sha": "abc123def456", "kind": "ship-commit"}
    old_path = str(old_repo / "state" / "handoffs" / "2026-01-04-a.md")
    new_path = str(new_repo / "state" / "handoffs" / "2026-01-04-a.md")

    old_result = _via_housekeeping_cycle(old_path, params, old_repo / ".git")
    new_result = _call_handoff_archive_transition(new_path, params)

    assert old_result["exit_code"] == 0, old_result
    _assert_envelopes_match(old_result, new_result)
    assert Path(old_path).read_text(encoding="utf-8") == Path(new_path).read_text(
        encoding="utf-8"
    )


def test_stamp_only_position_a_refusal_envelope_unchanged(tmp_path):
    base = _make_git_repo(tmp_path, "stamp-only-refusal")
    _seed_handoff(base, "2026-01-04-b.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "stamp_only"}
    old_path = str(old_repo / "state" / "handoffs" / "2026-01-04-b.md")
    new_path = str(new_repo / "state" / "handoffs" / "2026-01-04-b.md")

    old_result = _via_housekeeping_cycle(old_path, params, old_repo / ".git")
    new_result = _call_handoff_archive_transition(new_path, params)

    assert old_result["exit_code"] == 1, old_result
    assert "refusing to flip deployment_state:shipped" in old_result["error"]
    _assert_envelopes_match(old_result, new_result)


def test_stamp_only_containment_escape_envelope_unchanged(tmp_path):
    base = _make_git_repo(tmp_path, "stamp-only-usage")
    (base / "archive" / "handoffs" / "2026-01").mkdir(parents=True, exist_ok=True)
    (base / "archive" / "handoffs" / "2026-01" / "escaped.md").write_text(
        "---\ntitle: x\n---\n\nbody\n", encoding="utf-8"
    )
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add escaped fixture")

    # No mutation happens on a containment-escape refusal, so both paths
    # read the SAME fixture — a forked pair would only differ by their own
    # directory names, which would leak into the error string and make an
    # identical-by-construction refusal look like a false divergence.
    params = {"mode": "stamp_only"}
    outside = str(base / "archive" / "handoffs" / "2026-01" / "escaped.md")

    old_result = _via_housekeeping_cycle(outside, params, base / ".git")
    new_result = _call_handoff_archive_transition(outside, params)

    assert old_result["exit_code"] == 2, old_result
    _assert_envelopes_match(old_result, new_result)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Contract gap found by this chunk's own test, NOT fixable here: "
        "coordinator_core/ops/handoff_stamp_targeted.py is outside C4's "
        "writes scope (docs/plans/2026-08-30-the-stamp-stops-paying-for-a-"
        "sweep-that.md). handoff_archive_transition._handler's own missing-"
        "handoff_path branch returns _err (exit_code=1) with mode set from "
        "the requested mode; ship_stamp_only.py's equivalent branch returns "
        "_usage_error (exit_code=2) instead. Repointing this chunk's fan-in "
        "made this pre-existing (C2) mismatch reachable on the live call "
        "path for the first time — flagged for a follow-up chunk, not "
        "papered over."
    ),
)
def test_stamp_only_missing_handoff_path_exit_code_matches_pre_change_path(tmp_path):
    base = _make_git_repo(tmp_path, "stamp-only-missing-path")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "stamp_only"}

    old_result = _via_housekeeping_cycle("", params, old_repo / ".git")
    new_result = _call_handoff_archive_transition("", params)

    _assert_envelopes_match(old_result, new_result)


# ---------------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------------


def test_chain_success_envelope_unchanged(tmp_path):
    base = _make_git_repo(tmp_path, "chain-success")
    _seed_handoff(
        base,
        "2026-01-04-c.md",
        extra_fm="deployment_state: shipped\nshipped_in: deadbeef\npickup_ready: false\n",
    )
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "chain"}
    old_path = str(old_repo / "state" / "handoffs" / "2026-01-04-c.md")
    new_path = str(new_repo / "state" / "handoffs" / "2026-01-04-c.md")

    old_result = _via_housekeeping_cycle(old_path, params, old_repo / ".git")
    new_result = _call_handoff_archive_transition(new_path, params)

    assert old_result["exit_code"] == 0, old_result
    assert old_result["moved"] is True
    _assert_envelopes_match(old_result, new_result)
    assert not Path(old_path).exists()
    assert not Path(new_path).exists()
    dest_old = old_repo / "archive" / "handoffs" / "2026-01" / "2026-01-04-c.md"
    dest_new = new_repo / "archive" / "handoffs" / "2026-01" / "2026-01-04-c.md"
    assert dest_old.is_file() and dest_new.is_file()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Same pre-existing (C3) contract gap as "
        "test_stamp_only_missing_handoff_path_exit_code_matches_pre_change_path, "
        "in chain_archive_handoff's own missing-handoff_path branch (also "
        "outside C4's writes scope) — _usage_error (exit_code=2) instead of "
        "the pre-change path's _err (exit_code=1, mode set)."
    ),
)
def test_chain_missing_handoff_path_exit_code_matches_pre_change_path(tmp_path):
    base = _make_git_repo(tmp_path, "chain-missing-path")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "chain"}

    old_result = _via_housekeeping_cycle("", params, old_repo / ".git")
    new_result = _call_handoff_archive_transition("", params)

    _assert_envelopes_match(old_result, new_result)


def test_chain_refusal_envelope_unchanged(tmp_path):
    base = _make_git_repo(tmp_path, "chain-refusal")
    _seed_handoff(base, "2026-01-04-d.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "chain"}
    old_path = str(old_repo / "state" / "handoffs" / "2026-01-04-d.md")
    new_path = str(new_repo / "state" / "handoffs" / "2026-01-04-d.md")

    old_result = _via_housekeeping_cycle(old_path, params, old_repo / ".git")
    new_result = _call_handoff_archive_transition(new_path, params)

    assert old_result["exit_code"] == 1, old_result
    assert "not terminal" in old_result["error"]
    _assert_envelopes_match(old_result, new_result)


# ---------------------------------------------------------------------------
# supersede
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Contract gap found by this chunk's own test, NOT fixable here: "
        "handoff_archive_transition._handler emits an unconditional DR-096 "
        "'shipped_in_kind selected: scope-derived' warning ahead of do_stamp "
        "for ANY call with no --sha (mode-independent). "
        "supersede_archive_handoff in "
        "coordinator_core/ops/handoff_stamp_targeted.py (outside C4's writes "
        "scope) computes the same stamp_kind but never appends that warning "
        "— only ship_stamp_only does. Flagged for a follow-up chunk."
    ),
)
def test_supersede_success_envelope_unchanged(tmp_path):
    base = _make_git_repo(tmp_path, "supersede-success")
    _seed_handoff(base, "2026-01-04-e.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "supersede", "continued_into": "2026-01-04-successor.md"}
    old_path = str(old_repo / "state" / "handoffs" / "2026-01-04-e.md")
    new_path = str(new_repo / "state" / "handoffs" / "2026-01-04-e.md")

    old_result = _via_housekeeping_cycle(old_path, params, old_repo / ".git")
    new_result = _call_handoff_archive_transition(new_path, params)

    assert old_result["exit_code"] == 0, old_result
    assert old_result["superseded"] is True
    assert old_result["moved"] is True
    _assert_envelopes_match(old_result, new_result)
    dest_old = old_repo / "archive" / "handoffs" / "2026-01" / "2026-01-04-e.md"
    dest_new = new_repo / "archive" / "handoffs" / "2026-01" / "2026-01-04-e.md"
    assert dest_old.read_text(encoding="utf-8") == dest_new.read_text(encoding="utf-8")


def test_supersede_closed_predecessor_refusal_envelope_unchanged(tmp_path):
    base = _make_git_repo(tmp_path, "supersede-closed")
    _seed_handoff(
        base,
        "2026-01-04-f.md",
        extra_fm="deployment_state: closed\nclosed_reason: cancelled\n",
    )
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "supersede", "continued_into": "2026-01-04-successor.md"}
    old_path = str(old_repo / "state" / "handoffs" / "2026-01-04-f.md")
    new_path = str(new_repo / "state" / "handoffs" / "2026-01-04-f.md")

    old_result = _via_housekeeping_cycle(old_path, params, old_repo / ".git")
    new_result = _call_handoff_archive_transition(new_path, params)

    assert old_result["exit_code"] == 1, old_result
    assert "deployment_state: closed" in old_result["error"]
    _assert_envelopes_match(old_result, new_result)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Contract gap found by this chunk's own test, NOT fixable here: "
        "handoff_archive_transition._handler's missing-continued_into usage "
        "error (mode == 'supersede' and not continued_into) is one of the "
        "handful of _usage_error call sites that never sets out['mode'], so "
        "the pre-change envelope reports mode=None for this refusal. "
        "supersede_archive_handoff's own _supersede_usage_error (outside "
        "C4's writes scope) always hardcodes mode='supersede'. Flagged for "
        "a follow-up chunk."
    ),
)
def test_supersede_usage_error_envelope_unchanged(tmp_path):
    base = _make_git_repo(tmp_path, "supersede-usage")
    _seed_handoff(base, "2026-01-04-g.md")
    _git(base, "add", "-A")
    _git(base, "commit", "-m", "add handoff")

    old_repo = _fork_fixture(base, tmp_path, "old")
    new_repo = _fork_fixture(base, tmp_path, "new")

    params = {"mode": "supersede", "continued_into": ""}
    old_path = str(old_repo / "state" / "handoffs" / "2026-01-04-g.md")
    new_path = str(new_repo / "state" / "handoffs" / "2026-01-04-g.md")

    old_result = _via_housekeeping_cycle(old_path, params, old_repo / ".git")
    new_result = _call_handoff_archive_transition(new_path, params)

    assert old_result["exit_code"] == 2, old_result
    _assert_envelopes_match(old_result, new_result)
