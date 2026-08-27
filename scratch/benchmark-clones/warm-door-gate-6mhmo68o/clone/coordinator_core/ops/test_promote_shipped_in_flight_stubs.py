"""
Tests for coordinator_core.ops.promote_shipped_in_flight_stubs.

Port of: promote-shipped-in-flight-stubs.sh (DoE b5a4192c, 2026-07-20), and its
own test harness promote-shipped-in-flight-stubs.test.sh (DoE 3a561713,
2026-07-22, cases T1-T7). T1-T5/T7 are reproduced here functionally, against real git
fixtures and the real "handoff.stamp"/"handoff.transition" op handlers (no
mocking — these ops are pure local-filesystem mutations, no network). T6 (the
bash oracle's stamp-CLI shim) has no Python analog since this port drops the
Node.js legacy transport entirely (see module docstring "Direct-import
design") — the equivalent ABORT-guard behavior is exercised here instead by
monkeypatching this module's own `_stamp` coroutine to simulate a stamp call
that reports success without actually landing `shipped_in`. T4 (a live
consumed/in_flight stub whose deliverable_id has NEVER appeared in any commit
trailer at all — the vacuous "pre-convention strand" case, distinct from T3's
"an unrelated commit exists but the deliverable's own resolving trailer
never landed" framing) is reproduced as test_t4_never_referenced_deliverable_leaves_stub_untouched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import promote_shipped_in_flight_stubs as subject
from coordinator_core.ops.promote_shipped_in_flight_stubs import (
    _fm_field,
    _select_best_sha,
    main,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run(*args, cwd=None, check=True):
    return subprocess.run(
        list(args), cwd=cwd, check=check, capture_output=True, text=True
    )


@pytest.fixture()
def git_fixture(tmp_path):
    """Bare origin.git + working clone `repo`, on branch main, one root commit
    already pushed — mirrors the bash harness's `_make_git_fixture`.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    _run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    _run("git", "clone", "-q", str(origin), str(repo))
    _run("git", "config", "user.email", "test@example.com", cwd=repo)
    _run("git", "config", "user.name", "Test", cwd=repo)
    (repo / ".gitkeep").write_text("")
    _run("git", "add", "--", ".gitkeep", cwd=repo)
    _run("git", "commit", "-q", "-m", "root", cwd=repo)
    _run("git", "push", "-q", "origin", "main", cwd=repo)
    return repo


def _commit(repo, message: str, fname: str) -> str:
    f = repo / fname
    f.write_text(f.read_text() + "x" if f.exists() else "x")
    _run("git", "add", "--", fname, cwd=repo)
    _run("git", "commit", "-q", "-m", message, cwd=repo)
    return _run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()


def _write_stub(
    repo, filename: str, deliverable_id: str, roadmap_id: str, stub_id: str, state: str,
    status: str = "claimed",
):
    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    (handoffs / filename).write_text(
        f"""---
title: "fixture spinoff-roadmap stub"
created: "2026-07-11"
branch: "work/test/2026-07-11"
status: {status}
predecessor: none
kind: spinoff-roadmap
category: infra
summary: "fixture stub for test_promote_shipped_in_flight_stubs.py"
deployment_state: {state}
deliverable_id: "{deliverable_id}"
roadmap_id: "{roadmap_id}"
stub_id: "{stub_id}"
wave: 1
blocks: []
blocked_by: []
---

# fixture stub body
"""
    )
    return handoffs / filename


# ---------------------------------------------------------------------------
# _fm_field — quote-stripping parity
# ---------------------------------------------------------------------------


def test_fm_field_strips_one_matched_quote_pair(tmp_path):
    p = tmp_path / "h.md"
    p.write_text('---\nstatus: consumed\ndeliverable_id: "dlv-abc"\nunquoted: bare\n---\nbody\n')
    assert _fm_field(p, "status") == "consumed"
    assert _fm_field(p, "deliverable_id") == "dlv-abc"
    assert _fm_field(p, "unquoted") == "bare"
    assert _fm_field(p, "missing") == ""


def test_fm_field_no_frontmatter_returns_empty(tmp_path):
    p = tmp_path / "h.md"
    p.write_text("no frontmatter here\n")
    assert _fm_field(p, "status") == ""


# ---------------------------------------------------------------------------
# T1 — in_flight stub whose deliverable IS on fake origin/main -> promoted to
# deployment_state:shipped, with a non-empty shipped_in SHA stamped.
# ---------------------------------------------------------------------------


def test_t1_shipped_deliverable_promotes_stub(git_fixture, monkeypatch, capsys):
    repo = git_fixture
    monkeypatch.chdir(repo)
    sha = _commit(repo, "finish t1 deliverable\n\nResolves: dlv-t1-01", "t1.txt")
    _run("git", "push", "-q", "origin", "main", cwd=repo)

    stub = _write_stub(repo, "2026-07-11_1_t1.md", "dlv-t1-01", "", "t1-01", "in_flight")

    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out

    assert rc == 0
    assert _fm_field(stub, "deployment_state") == "shipped"
    shipped_in = _fm_field(stub, "shipped_in")
    assert shipped_in != ""
    assert shipped_in == sha[:8]
    assert "1 in_flight spinoff-roadmap stubs promoted to shipped" in out


# ---------------------------------------------------------------------------
# DR-084 old-vocabulary tolerance — a pre-cutover stub still carrying
# status:consumed (grandfathered, never written fresh) must still promote;
# the writer's status check tolerates the old name on read.
# ---------------------------------------------------------------------------


def test_t1_variant_old_status_consumed_still_promotes(git_fixture, monkeypatch, capsys):
    repo = git_fixture
    monkeypatch.chdir(repo)
    sha = _commit(repo, "finish t1-old deliverable\n\nResolves: dlv-t1-old-01", "t1old.txt")
    _run("git", "push", "-q", "origin", "main", cwd=repo)

    stub = _write_stub(
        repo, "2026-07-11_1_t1old.md", "dlv-t1-old-01", "", "t1-old-01", "in_flight",
        status="consumed",
    )

    rc = main([], repo_root=str(repo))
    capsys.readouterr()

    assert rc == 0
    assert _fm_field(stub, "deployment_state") == "shipped"
    assert _fm_field(stub, "shipped_in") == sha[:8]


# ---------------------------------------------------------------------------
# T2 — in_flight stub whose deliverable is NOT on origin/main -> UNTOUCHED.
# ---------------------------------------------------------------------------


def test_t2_not_shipped_deliverable_leaves_stub_untouched(git_fixture, monkeypatch, capsys):
    repo = git_fixture
    monkeypatch.chdir(repo)
    _commit(repo, "finish t2 deliverable\n\nResolves: dlv-t2-01", "t2.txt")
    # Deliberately do NOT push — resolving commit stays off origin/main.

    stub = _write_stub(repo, "2026-07-11_1_t2.md", "dlv-t2-01", "", "t2-01", "in_flight")

    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out

    assert rc == 0
    assert _fm_field(stub, "deployment_state") == "in_flight"
    assert _fm_field(stub, "shipped_in") == ""
    assert out.strip() == "no in_flight spinoff-roadmap stubs promoted"


# ---------------------------------------------------------------------------
# T3 — deliverable with NO Resolves: commits (no-resolving-commits) ->
# UNTOUCHED, counted in the AC10 advisory.
# ---------------------------------------------------------------------------


def test_t3_no_resolving_commits_leaves_stub_untouched(git_fixture, monkeypatch, capsys):
    repo = git_fixture
    monkeypatch.chdir(repo)
    _commit(repo, "unrelated work, no trailer", "t3.txt")
    _run("git", "push", "-q", "origin", "main", cwd=repo)

    stub = _write_stub(repo, "2026-07-11_1_t3.md", "dlv-t3-does-not-exist", "", "t3-01", "in_flight")

    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out

    assert rc == 0
    assert _fm_field(stub, "deployment_state") == "in_flight"
    assert "1 in_flight spinoff-roadmap stubs resolved no-resolving-commits" in out


# ---------------------------------------------------------------------------
# T4 — a live consumed/in_flight stub with no shipped deliverable -> UNTOUCHED.
# (Distinct from T3: this deliverable_id has never appeared in ANY commit
# trailer at all — the vacuous "pre-convention strand" case — same
# no-resolving-commits code path as T3, but exercised via a freshly-authored
# deliverable_id with zero repo history referencing it, to assert the "live
# stub, no shipped deliverable" framing independently of T3's "unrelated
# commit exists" framing. Review: code-reviewer F2 — bash oracle's T4 case
# was missing a Python analog despite the docstring claiming T1-T5/T7.)
# ---------------------------------------------------------------------------


def test_t4_never_referenced_deliverable_leaves_stub_untouched(git_fixture, monkeypatch, capsys):
    repo = git_fixture
    monkeypatch.chdir(repo)
    # No unrelated commit at all beyond the fixture's root commit — the
    # deliverable_id never appears in any commit trailer.

    stub = _write_stub(repo, "2026-07-11_1_t4.md", "dlv-t4-never-resolved", "", "t4-01", "in_flight")

    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out

    assert rc == 0
    assert _fm_field(stub, "deployment_state") == "in_flight"
    assert _fm_field(stub, "shipped_in") == ""
    assert "1 in_flight spinoff-roadmap stubs resolved no-resolving-commits" in out


# ---------------------------------------------------------------------------
# Unreadable stub -- hard I/O failure, distinct from "no deliverable_id".
# Must WARN naming the file (matching every sibling skip-path's contract)
# and must NOT be silently treated as a normal skip that leaves
# deployment_state stuck in_flight with no signal (silent-success audit
# 2026-07-22).
# ---------------------------------------------------------------------------


def test_unreadable_stub_warns_and_is_not_treated_as_no_deliverable_id(
    git_fixture, monkeypatch, capsys
):
    repo = git_fixture
    monkeypatch.chdir(repo)

    stub = _write_stub(
        repo, "2026-07-11_1_unreadable.md", "dlv-unreadable-01", "", "unreadable-01", "in_flight"
    )
    # `os.chmod(stub, 0o000)` does not reproduce an unreadable file on
    # Windows: chmod there only toggles the read-only attribute, and the
    # owning user can still open/read a "read-only" file just fine -- the
    # POSIX permission bits this scenario depends on don't exist on that
    # platform. `_fm_field`'s actual contract (production code, untouched)
    # is "catch OSError from Path.read_text and raise _StubUnreadable" --
    # so inject the I/O failure directly at that seam, which is real on
    # every platform, instead of trying to recreate an unreadable file via
    # a permission model Windows doesn't have.
    real_read_text = subject.Path.read_text

    def _fake_read_text(self, *args, **kwargs):
        if self == stub:
            raise PermissionError(13, "Permission denied", str(stub))
        return real_read_text(self, *args, **kwargs)

    # Scoped to the main() call only -- the post-assertions below read the
    # stub back via the real `_fm_field` to confirm it was left untouched.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subject.Path, "read_text", _fake_read_text)
        rc = main([], repo_root=str(repo))
        out, err = capsys.readouterr()

    assert rc == 0
    assert str(stub) in err
    assert "WARNING could not read stub" in err
    # Must not be silently folded into the "no deliverable_id" skip: no
    # no-resolving-commits advisory, no promotion, and the file must still
    # be readable/unchanged (permissions restored) with deployment_state
    # untouched -- i.e. distinguishable from an ordinary clean skip by the
    # stderr WARNING alone.
    assert "no-resolving-commits" not in out
    assert _fm_field(stub, "deployment_state") == "in_flight"


# ---------------------------------------------------------------------------
# T5 — idempotent re-run: run the closer twice on T1's already-promoted
# fixture -> stable (still shipped, no error, no double-mutation).
# ---------------------------------------------------------------------------


def test_t5_idempotent_rerun_is_stable(git_fixture, monkeypatch, capsys):
    repo = git_fixture
    monkeypatch.chdir(repo)
    _commit(repo, "finish t5 deliverable\n\nResolves: dlv-t5-01", "t5.txt")
    _run("git", "push", "-q", "origin", "main", cwd=repo)

    stub = _write_stub(repo, "2026-07-11_1_t5.md", "dlv-t5-01", "", "t5-01", "in_flight")

    rc1 = main([], repo_root=str(repo))
    capsys.readouterr()
    assert rc1 == 0
    shipped_in_1 = _fm_field(stub, "shipped_in")
    assert shipped_in_1 != ""

    rc2 = main([], repo_root=str(repo))
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert _fm_field(stub, "deployment_state") == "shipped"
    assert _fm_field(stub, "shipped_in") == shipped_in_1
    # Already-shipped stub is no longer in_flight — filtered out by the target
    # predicate on the second scan, so zero (new) promotions this run.
    assert out2.strip() == "no in_flight spinoff-roadmap stubs promoted"


# ---------------------------------------------------------------------------
# T7 — AC10 advisory: >=1 stub resolving no-resolving-commits prints the
# aggregate advisory line with the correct count.
# ---------------------------------------------------------------------------


def test_t7_norc_advisory_count(git_fixture, monkeypatch, capsys):
    repo = git_fixture
    monkeypatch.chdir(repo)
    _commit(repo, "unrelated work, no trailer", "t7.txt")
    _run("git", "push", "-q", "origin", "main", cwd=repo)

    _write_stub(repo, "2026-07-11_1_t7a.md", "dlv-t7-never-resolved-a", "", "t7-01", "in_flight")
    _write_stub(repo, "2026-07-11_2_t7b.md", "dlv-t7-never-resolved-b", "", "t7-02", "in_flight")

    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out

    assert rc == 0
    assert (
        "2 in_flight spinoff-roadmap stubs resolved no-resolving-commits "
        "(possible pre-convention strand — manual check)" in out
    )


# ---------------------------------------------------------------------------
# Regression — P4 SHA selection fails open on all-garbage commits[] (backlog
# 2026-07-13-promote-shipped-in-flight-stubs-p4-sha-fails-open.yaml): the old
# DoE bash did `|| echo 0` (fail-open) rather than `|| continue` (fail-closed)
# on an unresolvable commit; the ported _committer_timestamp/_select_best_sha
# reproduced the same fail-open shape by using 0 as a failure sentinel
# indistinguishable from a real timestamp. Fixed by using None as the
# resolution-failure sentinel so an all-garbage input falls closed (best_sha
# == "") instead of selecting the first garbage SHA.
# ---------------------------------------------------------------------------


def test_select_best_sha_falls_closed_on_all_garbage_commits(git_fixture, monkeypatch):
    repo = git_fixture
    monkeypatch.chdir(repo)
    assert _select_best_sha(["not-a-sha", "also-garbage", "0000000"]) == ""


def test_select_best_sha_picks_max_committer_timestamp_among_real_shas(git_fixture, monkeypatch):
    repo = git_fixture
    monkeypatch.chdir(repo)
    # Explicit, well-separated committer dates — same-second commits would
    # make this test's ordering flaky without asserting anything about the
    # regression under test.
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00")
    sha_older = _commit(repo, "older commit", "older.txt")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-06-01T00:00:00")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-06-01T00:00:00")
    sha_newer = _commit(repo, "newer commit", "newer.txt")
    # Garbage entries interleaved must not win over a real, later SHA.
    assert _select_best_sha(["garbage", sha_older, "also-garbage", sha_newer]) == sha_newer


def test_select_best_sha_treats_real_epoch_zero_as_a_winning_candidate(git_fixture, monkeypatch):
    """Review: code-reviewer — _batch_committer_timestamps' docstring warns
    that a REAL committer timestamp of literal epoch 0 must never be
    conflated with "unresolvable" (that conflation is the exact P4 fail-open
    defect class fixed above). Pin it: a genuinely-epoch-0 commit mixed with
    an unresolvable garbage SHA must be selected, not treated as a
    resolution failure that falls closed to "".
    """
    repo = git_fixture
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_COMMITTER_DATE", "1970-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "1970-01-01T00:00:00Z")
    sha_epoch_zero = _commit(repo, "epoch-zero commit", "epoch0.txt")
    assert subject._batch_committer_timestamps([sha_epoch_zero]) == {sha_epoch_zero: 0}
    assert _select_best_sha(["also-garbage", sha_epoch_zero]) == sha_epoch_zero


def test_batch_committer_timestamps_makes_exactly_one_git_call(git_fixture, monkeypatch):
    """C35 — batching regression: resolving N candidate SHAs must spawn ONE
    git process (`git log --no-walk=unsorted --ignore-missing`), not one per
    candidate. Counts real `subprocess.run` invocations rather than asserting
    call args, so the test survives incidental argv reshaping.
    """
    repo = git_fixture
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00")
    sha_a = _commit(repo, "commit a", "a.txt")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-02-01T00:00:00")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-02-01T00:00:00")
    sha_b = _commit(repo, "commit b", "b.txt")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-03-01T00:00:00")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-03-01T00:00:00")
    sha_c = _commit(repo, "commit c", "c.txt")

    real_run = subprocess.run
    calls = []

    def _counting_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subject.subprocess, "run", _counting_run)

    assert _select_best_sha(["garbage-unresolvable", sha_a, sha_b, sha_c]) == sha_c
    assert len(calls) == 1


def test_batch_committer_timestamps_never_reads_a_dropped_sha_as_resolved(git_fixture, monkeypatch):
    """`--ignore-missing` exits 0 with an unresolvable SHA simply absent from
    stdout — pin that absence is reconciled explicitly (a missing key), never
    defaulted to a winning/losing timestamp. Mixing one real SHA with several
    unresolvable ones must both (a) not raise/degrade the batch call and
    (b) never populate the unresolvable SHAs' map entries.
    """
    repo = git_fixture
    monkeypatch.chdir(repo)
    sha_real = _commit(repo, "the only real commit", "real.txt")
    unresolvable = ["deadbeef00", "0000000000", "not-a-sha-at-all"]

    result = subject._batch_committer_timestamps([*unresolvable, sha_real])

    assert set(result.keys()) == {sha_real}
    for missing in unresolvable:
        assert missing not in result

    # And the caller-facing selection still falls closed correctly when
    # ONLY unresolvable candidates are supplied (no real SHA to reconcile
    # against at all).
    assert _select_best_sha(unresolvable) == ""


def test_shipped_token_with_all_garbage_shas_does_not_promote(git_fixture, monkeypatch, capsys):
    """Integration-level regression: a stub whose rollup-derive join reports
    token "shipped" but returns only unresolvable SHAs must NOT be stamped
    with a bogus/truncated garbage SHA — it must fall closed exactly like the
    existing "shipped token with no resolving SHA" WARNING path.
    """
    repo = git_fixture
    monkeypatch.chdir(repo)

    stub = _write_stub(
        repo, "2026-07-11_1_garbage.md", "dlv-garbage-01", "", "garbage-01", "in_flight"
    )

    def _fake_rollup_derive(deliverable_id):
        return "shipped", ["not-a-real-sha", "0000000garbage"]

    monkeypatch.setattr(subject, "_rollup_derive", _fake_rollup_derive)

    rc = main([], repo_root=str(repo))
    out, err = capsys.readouterr()

    assert rc == 0
    assert _fm_field(stub, "deployment_state") == "in_flight"
    assert _fm_field(stub, "shipped_in") == ""
    assert "WARNING shipped token with no resolving SHA" in err
    assert out.strip() == "no in_flight spinoff-roadmap stubs promoted"


# ---------------------------------------------------------------------------
# T6-equivalent — stamp step reports success but does NOT land shipped_in ->
# the closer ABORTS without calling ship (fail-loud guard). No Node.js CLI to
# shim in this port (see module docstring); simulated by monkeypatching this
# module's own `_stamp` coroutine.
# ---------------------------------------------------------------------------


def test_t6_equivalent_aborts_when_shipped_in_fails_to_land(git_fixture, monkeypatch, capsys):
    repo = git_fixture
    monkeypatch.chdir(repo)
    _commit(repo, "finish t6 deliverable\n\nResolves: dlv-t6-01", "t6.txt")
    _run("git", "push", "-q", "origin", "main", cwd=repo)

    stub = _write_stub(repo, "2026-07-11_1_t6.md", "dlv-t6-01", "", "t6-01", "in_flight")

    async def _fake_stamp(stub_abs, sha8, git_common_dir):
        # Reports success but writes nothing — exercises the post-stamp
        # ASSERT-shipped_in-LANDED guard for real.
        return {"exit_code": 0, "applied": True, "message": "fake stamp (no-op write)"}

    monkeypatch.setattr(subject, "_stamp", _fake_stamp)

    rc = main([], repo_root=str(repo))
    err = capsys.readouterr().err

    # AC14: stamp_abort_count > 0 is "candidates present but unjoinable" —
    # this is the loud case, distinct from the quiet zero-candidates and
    # quiet norc_count cases exercised below.
    assert rc != 0
    assert _fm_field(stub, "deployment_state") == "in_flight"
    assert _fm_field(stub, "shipped_in") == ""
    assert "ABORT" in err and "shipped_in did not land after stamp" in err


# ---------------------------------------------------------------------------
# AC14 — the zero-match discriminator, scored per this closer's three
# aggregate counters (state/audits/2026-08-04-terminal-state-closer-exit-
# code-caller-audit.md). One test per row of that audit's table.
# ---------------------------------------------------------------------------


def test_ac14_all_zero_counters_stays_quiet(git_fixture, monkeypatch, capsys):
    """promoted==0, norc_count==0, stamp_abort_count==0 -- nothing in_flight
    to scan at all. This is the "zero candidates at all" case DoE's
    unconditional /workday-start invocation hits most mornings; must stay
    exit 0 or it is a false alarm on a day nothing is wrong.
    """
    repo = git_fixture
    monkeypatch.chdir(repo)
    # No handoffs directory populated with any in_flight roadmap-baton stub.
    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "no in_flight spinoff-roadmap stubs promoted"


def test_ac14_norc_count_positive_stays_quiet(git_fixture, monkeypatch, capsys):
    """norc_count > 0, stamp_abort_count == 0 -- candidate(s) found but not
    yet shipped (unmerged/pre-convention branch work). A real, expected
    state, not a caller-visible failure.
    """
    repo = git_fixture
    monkeypatch.chdir(repo)
    _commit(repo, "unrelated work, no trailer", "ac14norc.txt")
    _run("git", "push", "-q", "origin", "main", cwd=repo)
    _write_stub(
        repo, "2026-07-11_1_ac14norc.md", "dlv-ac14-norc", "", "ac14-norc-01", "in_flight"
    )

    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 in_flight spinoff-roadmap stubs resolved no-resolving-commits" in out


def test_ac14_stamp_abort_count_positive_goes_loud(git_fixture, monkeypatch, capsys):
    """stamp_abort_count > 0 -- a shipped candidate whose shipped_in stamp
    write failed. "Candidates present but unjoinable/unwritable" in the
    AC14 sense: this is the one condition that must exit non-zero.
    """
    repo = git_fixture
    monkeypatch.chdir(repo)
    _commit(repo, "finish ac14-abort deliverable\n\nResolves: dlv-ac14-abort", "ac14abort.txt")
    _run("git", "push", "-q", "origin", "main", cwd=repo)
    _write_stub(
        repo, "2026-07-11_1_ac14abort.md", "dlv-ac14-abort", "", "ac14-abort-01", "in_flight"
    )

    async def _fake_stamp(stub_abs, sha8, git_common_dir):
        return {"exit_code": 0, "applied": True, "message": "fake stamp (no-op write)"}

    monkeypatch.setattr(subject, "_stamp", _fake_stamp)

    rc = main([], repo_root=str(repo))
    err = capsys.readouterr().err
    assert rc != 0
    assert "ABORT" in err and "shipped_in did not land after stamp" in err


# ---------------------------------------------------------------------------
# Non-git-repo / missing repo_root resolution — degrades to exit 0, loud stderr.
# ---------------------------------------------------------------------------


def test_not_a_git_repo_degrades_to_exit_0(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "not inside a git repository" in captured.err


def test_missing_handoffs_dir_is_a_clean_no_op(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "no in_flight spinoff-roadmap stubs promoted"


# ---------------------------------------------------------------------------
# A2 (docs/plans/2026-08-01-baton-spine-information-integrity.md AC4) —
# end-to-end proof that the real "commit.anchors" producer's new `Resolves:`
# trailer (A1, coordinator_core/ops/commit_anchors.py) is what actually
# unsticks this promoter, driven through the real producer rather than a
# hand-authored `Resolves:` commit message (which is what T1-T7 above do,
# and which the plan explicitly warns is NOT sufficient proof — see this
# module's own docstring "prove the CONSEQUENCE rather than assuming it").
#
# Sequence: stage a plan (deliverable_id) + a completion entry
# (archive/completed/*.md) in the same commit exactly as the
# workstream-complete ceremony would, call commit_anchors._handler (the
# real producer) to derive the trailer block, commit with that block
# appended to the message body (mirroring the prepare-commit-msg hook's
# injection), push to origin/main, then run the real promoter against a
# fixture in_flight roadmap-baton stub carrying the same deliverable_id.
# ---------------------------------------------------------------------------


def _completion_event_commit(repo, deliverable_id: str, plan_slug: str) -> str:
    """Stage a docs/plans/<slug>.md (carrying deliverable_id) + an
    archive/completed/*.md completion entry, derive the real "commit.anchors"
    trailer block via the real producer, commit with it appended to the
    message, and return the resulting commit SHA. Mirrors the staged-diff
    shape `coordinator_core/tests/test_commit_anchors.py`'s
    TestResolvesCompletionTrailer.test_completion_event_emits_resolves
    exercises at the handler level alone — this reproduces it end-to-end
    through the actual promoter.
    """
    from coordinator_core.ops.commit_anchors import _handler as _commit_anchors_handler

    plan_dir = repo / "docs" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / f"{plan_slug}.md"
    plan_file.write_text(
        f"""---
title: "fixture plan"
created: 2026-08-01
author: test
status: draft
plan_id: "pln-{plan_slug}"
deliverable_id: "{deliverable_id}"
---

# fixture plan
"""
    )
    _run("git", "add", "--", str(plan_file.relative_to(repo)), cwd=repo)

    completed_dir = repo / "archive" / "completed" / "2026-08"
    completed_dir.mkdir(parents=True, exist_ok=True)
    entry_file = completed_dir / f"2026-08-01-{plan_slug}-done.md"
    entry_file.write_text(
        f'---\ntitle: "Done"\ncreated: 2026-08-01\nchain: "{plan_slug}"\n---\n\nDone.\n'
    )
    _run("git", "add", "--", str(entry_file.relative_to(repo)), cwd=repo)

    common_dir_result = _run(
        "git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo
    )
    common_dir = Path(common_dir_result.stdout.strip())

    reply = _commit_anchors_handler({"session_id": "", "nature": None}, repo_root=common_dir)
    trailers = reply["trailers"]
    assert f"Resolves: {deliverable_id}" in trailers, (
        f"real commit.anchors producer did not emit Resolves: for {deliverable_id} "
        f"(got trailers={trailers!r}) — fixture is not exercising the real producer"
    )

    message = f"finish {plan_slug}\n\n{trailers}\n"
    _run("git", "commit", "-q", "-m", message, cwd=repo)
    return _run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()


def test_ac4_real_producer_end_to_end_promotes_stub(git_fixture, monkeypatch, capsys):
    """AC4: the real `commit.anchors` producer stamps `Resolves: <dlv-id>` at
    a completion event, and that real trailer — not a hand-authored one — is
    what `promote_shipped_in_flight_stubs` reads via `rollup_derive` to
    promote a stranded `in_flight` roadmap-baton stub to terminal
    `deployment_state:shipped` with a landed `shipped_in`.
    """
    repo = git_fixture
    monkeypatch.chdir(repo)

    deliverable_id = "dlv-ac4-e2e-01"
    sha = _completion_event_commit(repo, deliverable_id, "2026-08-01-ac4-fixture-plan")
    _run("git", "push", "-q", "origin", "main", cwd=repo)

    stub = _write_stub(
        repo, "2026-07-11_1_ac4.md", deliverable_id, "", "ac4-01", "in_flight"
    )

    rc = main([], repo_root=str(repo))
    out = capsys.readouterr().out

    assert rc == 0
    assert _fm_field(stub, "deployment_state") == "shipped"
    shipped_in = _fm_field(stub, "shipped_in")
    assert shipped_in != ""
    assert shipped_in == sha[:8]
    assert "1 in_flight spinoff-roadmap stubs promoted to shipped" in out
