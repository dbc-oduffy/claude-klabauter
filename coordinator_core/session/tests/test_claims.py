"""
coordinator_core.session.tests.test_claims — parity tests for
coordinator_core.session.claims, the Python engine port of the CLAIMS module of
coordinator-session.sh (the claim primitives lived in the MAIN bash file, not
a lib/session/ sub-module).

Port of: coordinator-session.sh (coordinator-claude e34f2484, 2026-07-22).

Oracle bash functions cited per test class:
  - atomic_dedup_append   -> cs_atomic_dedup_append
  - claim_artifact        -> cs_claim_artifact — THE primitive
  - claim_handoff/memo    -> claim_artifact wrapper
  - claim_plan            -> claim_artifact (C3 shape instrumentation)
  - release_artifact      -> release_artifact
  - clear_claim_if_dead   -> clear_claim_if_dead
  - my_agent_touched      -> _cs_my_agent_touched (Q17)
  - self_claim            -> cs_self_claim

REGRESSION MATRIX FOCUS (from the bash contract comments):
  - PLAN-CLASS-ONLY re-entrancy: a same-session re-claim of a PLAN is ACCEPTED
    (execute-plan + workstream-complete seams), while a same-session re-claim of
    a HANDOFF or MEMO is REJECTED (T16a/T18c — the claim-lock pid-death
    false-positive regression net).
  - Dead-holder takeover vs live-holder rejection, both keyed on the HOLDER's
    OWN metadata (never the caller's pid/sid).

Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § claims.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.session import claims, core, scope, shape
from coordinator_core.ops.session import safe_commit_offer

# Every test in this file builds its repo via `_make_repo(tmp_path)`, spawning
# real git (init/config/add/commit) because the production code under test --
# `core.git_root()` and claims'/scope's ls-files/status seams -- reads real
# git state that no mock stands in for. `tmp_path` is function-scoped and
# many tests write claim/session state under reused session ids, so the
# repo fixture stays per-test rather than hoisted to module scope. The spawn
# ratchet's `_BASELINE` is shrink-only pre-existing residue and is explicitly
# not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _write_session(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


def _fresh(pid="1"):
    return {"pid": pid, "last_activity": core.now_iso()}


def _stale(pid="1"):
    return {"pid": pid, "last_activity": "2000-01-01T00:00:00Z"}


def _make_claim(repo, cls, basename, session_id=None, pid=None):
    cdir = Path(repo) / ".git" / "coordinator-sessions" / f"{cls}-claims" / basename
    cdir.mkdir(parents=True, exist_ok=True)
    if session_id is not None:
        (cdir / "session_id").write_text(session_id)
    if pid is not None:
        (cdir / "pid").write_text(str(pid))
    (cdir / "claimed_at").write_text(core.now_iso())
    return cdir


def _claim_dir(repo, cls, basename):
    return Path(repo) / ".git" / "coordinator-sessions" / f"{cls}-claims" / basename


def _write_handoff(repo, basename, status="claimed", deployment_state="in_flight"):
    hdir = Path(repo) / "state" / "handoffs"
    hdir.mkdir(parents=True, exist_ok=True)
    hpath = hdir / basename
    hpath.write_text(
        "---\n"
        "title: t\n"
        "created: 2026-07-24\n"
        "branch: work/t/2026-07-24\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: test fixture handoff\n"
        f"status: {status}\n"
        f"deployment_state: {deployment_state}\n"
        "claimed_by: 11111111-1111-4111-8111-111111111111\n"
        "claimed_at: 2000-01-01T00:00:00Z\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    return hpath


def _self_lstart():
    result = subprocess.run(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="],
        capture_output=True,
        text=True,
    )
    lstart = result.stdout.strip()
    assert lstart, "ps -p <self> -o lstart= must succeed on a live test process"
    return lstart


def _set_me(monkeypatch, sid="me-sid"):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    # self_claim's fast path reads only these two; clear them so claim tests use
    # the COORDINATOR_SESSION_ID tier-1 resolution deterministically.
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _clear_all_sid_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


# ---------------------------------------------------------------------------
# atomic_dedup_append (786-801)
# ---------------------------------------------------------------------------


class TestAtomicDedupAppend:
    def test_append_creates_and_writes(self, tmp_path):
        f = tmp_path / "touched.txt"
        f.write_text("")
        assert claims.atomic_dedup_append(str(f), "a/b.py") is True
        lines = f.read_text().splitlines()
        assert len(lines) == 1
        verb, ts, path = scope.parse_touch_event(lines[0])
        assert verb == "T"
        assert path == "a/b.py"
        assert ts is not None

    def test_dedup_no_double_line(self, tmp_path):
        f = tmp_path / "touched.txt"
        f.write_text("a/b.py\n")
        assert claims.atomic_dedup_append(str(f), "a/b.py") is True
        assert f.read_text() == "a/b.py\n"

    def test_distinct_entries_both_land(self, tmp_path):
        # T21 lost-update pin: two DISTINCT paths must both survive — the
        # incident this function exists to prevent (the prior mktemp+sort+mv
        # pattern let concurrent writers silently drop one). Only the line
        # FORMAT changed (bare path -> event line); the "both survive"
        # property is asserted exactly as strongly as before.
        f = tmp_path / "touched.txt"
        f.write_text("")
        claims.atomic_dedup_append(str(f), "one.py")
        claims.atomic_dedup_append(str(f), "two.py")
        lines = f.read_text().splitlines()
        assert len(lines) == 2
        parsed = [scope.parse_touch_event(line) for line in lines]
        assert [(verb, path) for verb, _ts, path in parsed] == [
            ("T", "one.py"),
            ("T", "two.py"),
        ]

    def test_whole_line_match_only(self, tmp_path):
        # Dedup semantics CHANGED by design: no longer whole-line identity
        # (grep -qxF); now "is the LAST event recorded for this path already
        # T?". A substring path is a DIFFERENT path, so it must never dedup
        # against an existing entry — that part of the old test's intent
        # survives unchanged.
        f = tmp_path / "touched.txt"
        f.write_text("a/bcd.py\n")
        claims.atomic_dedup_append(str(f), "a/bc")
        lines = f.read_text().splitlines()
        assert len(lines) == 2
        assert scope.parse_touch_event(lines[0])[2] == "a/bcd.py"
        verb, _ts, path = scope.parse_touch_event(lines[1])
        assert (verb, path) == ("T", "a/bc")

    def test_legacy_bare_line_already_claimed_no_redundant_append(self, tmp_path):
        # A path whose ONLY record is a bare legacy line (pre-event-log
        # corpus) parses as CLAIMED (parse_touch_event's fail-safe: verb
        # 'T', unknown time) -> self_claim-style append must NOT add a
        # redundant line for it. Legacy-corpus compatibility guarantee.
        f = tmp_path / "touched.txt"
        f.write_text("legacy/path.py\n")
        assert claims.atomic_dedup_append(str(f), "legacy/path.py") is True
        assert f.read_text().splitlines() == ["legacy/path.py"]

    def test_released_path_gets_fresh_t_appended(self, tmp_path):
        # A path whose LAST event is R (released) must get a fresh T
        # appended — the whole point of the event log over deletion.
        f = tmp_path / "touched.txt"
        f.write_text(scope.format_touch_event("R", "was/released.py") + "\n")
        assert claims.atomic_dedup_append(str(f), "was/released.py") is True
        lines = f.read_text().splitlines()
        assert len(lines) == 2
        assert scope.parse_touch_event(lines[0])[0] == "R"
        verb, _ts, path = scope.parse_touch_event(lines[1])
        assert (verb, path) == ("T", "was/released.py")

    def test_missing_file_falls_through_to_append(self, tmp_path):
        f = tmp_path / "sub" / "touched.txt"
        # parent missing -> the append open fails -> silent True, no crash.
        assert claims.atomic_dedup_append(str(f), "x") is True

    def test_required_args_raise(self, tmp_path):
        with pytest.raises(ValueError):
            claims.atomic_dedup_append("", "x")
        with pytest.raises(ValueError):
            claims.atomic_dedup_append(str(tmp_path / "t.txt"), "")


# ---------------------------------------------------------------------------
# claim_artifact (864-962) — THE primitive
# ---------------------------------------------------------------------------


class TestClaimArtifact:
    def test_required_args_raise(self):
        with pytest.raises(ValueError):
            claims.claim_artifact("", "b")
        with pytest.raises(ValueError):
            claims.claim_artifact("handoff", "")

    def test_success_creates_dir_and_meta(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_artifact("handoff", "hb-1", cwd=str(repo)) is True
        cdir = _claim_dir(repo, "handoff", "hb-1")
        assert cdir.is_dir()
        assert (cdir / "session_id").read_text().strip() == "me-sid"
        assert (cdir / "pid").read_text().strip() == str(os.getpid())
        assert (cdir / "claimed_at").read_text().strip()

    def test_unresolvable_sid_fails(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _clear_all_sid_env(monkeypatch)
        # No sentinel, no env -> resolve_session_id == "" -> fail loud.
        assert claims.claim_artifact("handoff", "hb-x", cwd=str(repo)) is False

    def test_bad_baton_root_fails_loud(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        nonrepo = tmp_path / "not-a-git-repo"
        nonrepo.mkdir()
        assert (
            claims.claim_artifact(
                "handoff", "hb-b", baton_repo_root=str(nonrepo), cwd=str(repo)
            )
            is False
        )

    def test_baton_mode_claim_lands_under_baton_repo(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        baton_dir = tmp_path / "baton"
        baton_dir.mkdir()
        baton = _make_repo(baton_dir)
        _set_me(monkeypatch)
        assert (
            claims.claim_artifact(
                "memo", "m1", baton_repo_root=str(baton), cwd=str(repo)
            )
            is True
        )
        assert _claim_dir(baton, "memo", "m1").is_dir()
        # And NOT under the cwd repo.
        assert not _claim_dir(repo, "memo", "m1").exists()

    def test_live_holder_rejection_concurrent_pickup(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "handoff", "h-live", session_id="other-sid")
        _write_session(repo, "other-sid", _fresh())
        assert claims.claim_artifact("handoff", "h-live", cwd=str(repo)) is False
        # Holder untouched — we did NOT steal a live peer's claim.
        assert (
            _claim_dir(repo, "handoff", "h-live") / "session_id"
        ).read_text().strip() == "other-sid"

    def test_rename_relocates_the_claim_basename_key(self, tmp_path, monkeypatch):
        """Fix-side regression net for the incident this test's old
        `_KNOWN_DEFECT` name documented: the claim dir was keyed purely on
        the CURRENT basename (module negative-spec, "BASENAME-ONLY"), with
        no stable id surviving a rename. Renaming a handoff via a bare
        `git mv` — with no relocation of its claim dir — let a second
        session claim the artifact under its NEW basename cleanly, because
        no claim dir sat there: the same baton held by two sessions
        simultaneously.

        Pinned incident: state/bug-backlog/2026-08-10-two-sessions-held-one-
        baton-the-claim-di-1d9d62d1d8af.yaml (session fe113177 held
        "2026-08-10-untitled.md" claimed+live; the file was renamed to
        "...review-owed-close-boundary-fix-then-cap.md"; session e47b89 then
        claimed the NEW basename cleanly and both sessions worked the same
        baton concurrently).

        FIX (this dispatch): a rename performed through the sanctioned
        entrypoint (`claims.relocate_handoff_claim`, modeled on
        `percolate/rewrite_basename.py::_do_rename` +
        `session/scope.py::relocate_touched_path`) carries the claim dir
        along with the basename change, so the second session's claim
        attempt under the new basename now correctly collides with the
        still-live first claim and is REJECTED. This does not retroactively
        fix a rename performed OUTSIDE the entrypoint (a bare, un-tooled
        `git mv`) — see `relocate_artifact_claim`'s own negative-spec.
        """
        repo = _make_repo(tmp_path)
        old_basename = "2026-08-10-untitled.md"
        new_basename = "2026-08-10-review-owed-close-boundary-fix-then-cap.md"

        _set_me(monkeypatch, sid="fe113177")
        assert claims.claim_artifact("handoff", old_basename, cwd=str(repo)) is True
        _write_session(repo, "fe113177", _fresh())

        # The file is renamed via the sanctioned entrypoint, which relocates
        # the claim dir alongside the basename change.
        assert claims.relocate_handoff_claim(old_basename, new_basename, cwd=str(repo)) is True

        assert not _claim_dir(repo, "handoff", old_basename).exists()
        assert _claim_dir(repo, "handoff", new_basename).is_dir()
        assert (
            _claim_dir(repo, "handoff", new_basename) / "session_id"
        ).read_text().strip() == "fe113177"

        # A second, DIFFERENT live session attempts to claim the artifact
        # under its NEW basename — now correctly REJECTED, because the
        # relocated claim dir is visible there.
        _set_me(monkeypatch, sid="e47b898b")
        assert claims.claim_artifact("handoff", new_basename, cwd=str(repo)) is False

        # The original session's claim is untouched.
        assert (
            _claim_dir(repo, "handoff", new_basename) / "session_id"
        ).read_text().strip() == "fe113177"

    def test_relocate_collision_returns_false_not_raises(self, tmp_path, monkeypatch):
        """Destination collision (a DIFFERENT claim already sits at
        new_basename) stays a `False` return, reserved for exactly this
        refuse-to-clobber case — never conflated with an `OSError`."""
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch, sid="fe113177")
        assert claims.claim_artifact("handoff", "h-old", cwd=str(repo)) is True
        _write_session(repo, "fe113177", _fresh())
        _set_me(monkeypatch, sid="other-sid")
        _make_claim(repo, "handoff", "h-new", session_id="other-sid")
        assert (
            claims.relocate_artifact_claim("handoff", "h-old", "h-new", cwd=str(repo))
            is False
        )

    def test_relocate_os_failure_raises_not_false(self, tmp_path, monkeypatch):
        """Review: code-reviewer P2 — a genuine `os.replace` failure must be
        distinguishable from the collision `False` above: it now raises
        `ClaimRelocationError` instead of returning a conflated `False`."""
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch, sid="fe113177")
        assert claims.claim_artifact("handoff", "h-old2", cwd=str(repo)) is True
        _write_session(repo, "fe113177", _fresh())

        def _boom(*_a, **_kw):
            raise OSError("simulated os.replace failure")

        monkeypatch.setattr(claims.os, "replace", _boom)
        with pytest.raises(claims.ClaimRelocationError):
            claims.relocate_artifact_claim("handoff", "h-old2", "h-new2", cwd=str(repo))

    def test_dead_holder_takeover(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "handoff", "h-dead", session_id="other-sid")
        _write_session(repo, "other-sid", _stale())
        assert claims.claim_artifact("handoff", "h-dead", cwd=str(repo)) is True
        # Took it over — the claim now records MY sid.
        assert (
            _claim_dir(repo, "handoff", "h-dead") / "session_id"
        ).read_text().strip() == "me-sid"

    # ---- PLAN-CLASS-ONLY re-entrancy: the T16a/T18c regression matrix ----

    def test_plan_class_reentrant_same_session_accepted(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        # I already hold my OWN plan claim, and I am LIVE.
        _make_claim(repo, "plan", "p-1", session_id="me-sid")
        _write_session(repo, "me-sid", _fresh())
        # Re-claim of my own plan in the same live session -> ACCEPTED.
        assert claims.claim_artifact("plan", "p-1", cwd=str(repo)) is True

    def test_handoff_same_session_reclaim_rejected_T16a(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        # I hold my OWN handoff claim and I am LIVE — but handoff has NO
        # re-entrant branch, so the live-holder check fires against MYSELF.
        _make_claim(repo, "handoff", "h-me", session_id="me-sid")
        _write_session(repo, "me-sid", _fresh())
        assert claims.claim_artifact("handoff", "h-me", cwd=str(repo)) is False

    def test_memo_same_session_reclaim_rejected_T18c(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "memo", "m-me", session_id="me-sid")
        _write_session(repo, "me-sid", _fresh())
        assert claims.claim_artifact("memo", "m-me", cwd=str(repo)) is False

    def test_plan_reentrant_only_when_same_sid_dead_other_takes_over(
        self, tmp_path, monkeypatch
    ):
        # A DIFFERENT dead sid holding a plan is a normal stale-takeover, not the
        # re-entrant branch (which requires the SAME sid).
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "plan", "p-other", session_id="other-sid")
        _write_session(repo, "other-sid", _stale())
        assert claims.claim_artifact("plan", "p-other", cwd=str(repo)) is True
        assert (
            _claim_dir(repo, "plan", "p-other") / "session_id"
        ).read_text().strip() == "me-sid"

    def test_legacy_pid_only_live_contention_message_does_not_say_not_confirmed(
        self, tmp_path, monkeypatch, capsys
    ):
        # Legacy pid-only claim dir (no session_id file): claim_holder_live's
        # fallback is core.pid_alive(held_pid) directly -- so a True verdict
        # here means held_pid WAS just confirmed live by that exact check,
        # not merely "recorded at claim time". The pre-fix message printed
        # "recorded-at-claim-time PID <n> (not confirmed live)" for this
        # branch too, contradicting the liveness verdict we're inside.
        # os.getpid() is used as the "held" pid because it is guaranteed
        # alive for the duration of this test process.
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        held_pid = str(os.getpid())
        _make_claim(repo, "handoff", "legacy-x", session_id=None, pid=held_pid)
        assert claims.claim_artifact("handoff", "legacy-x", cwd=str(repo)) is False
        err = capsys.readouterr().err
        assert f"live PID {held_pid}" in err
        assert "not confirmed live" not in err
        assert "confirmed via legacy pid-liveness check" in err


# ---------------------------------------------------------------------------
# claim_handoff / claim_memo passthrough (970-971)
# ---------------------------------------------------------------------------


class TestClaimWrappers:
    def test_claim_handoff_passthrough(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_handoff("hw-1", cwd=str(repo)) is True
        assert _claim_dir(repo, "handoff", "hw-1").is_dir()

    def test_claim_memo_passthrough(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_memo("mw-1", cwd=str(repo)) is True
        assert _claim_dir(repo, "memo", "mw-1").is_dir()


# ---------------------------------------------------------------------------
# claim_plan (980-1011) — one-arg + C3 shape instrumentation
# ---------------------------------------------------------------------------


class TestClaimPlan:
    def _shape(self, repo, sid):
        f = (
            Path(repo)
            / ".git"
            / "coordinator-sessions"
            / sid
            / "session-shape.json"
        )
        return json.loads(f.read_text()) if f.is_file() else None

    def test_claim_plan_success_writes_scope_mode(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        plan = repo / "docs" / "plans" / "my-plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("---\nscope_mode: full\n---\n# My Plan\n")
        assert claims.claim_plan("my-plan", cwd=str(repo)) is True
        assert _claim_dir(repo, "plan", "my-plan").is_dir()
        shape = self._shape(repo, "me-sid")
        assert shape["plan"]["path"] == "docs/plans/my-plan.md"
        assert shape["plan"]["scope_mode"] == "full"

    def test_claim_plan_null_scope_when_no_plan_file(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_plan("ghost-plan", cwd=str(repo)) is True
        shape = self._shape(repo, "me-sid")
        assert shape["plan"]["path"] == "docs/plans/ghost-plan.md"
        assert shape["plan"]["scope_mode"] is None

    def test_claim_plan_quoted_scope_mode_stripped(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        plan = repo / "docs" / "plans" / "q-plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text('scope_mode: "light"\n')
        assert claims.claim_plan("q-plan", cwd=str(repo)) is True
        shape = self._shape(repo, "me-sid")
        assert shape["plan"]["scope_mode"] == "light"

    def test_claim_plan_returns_false_when_claim_fails(self, tmp_path, monkeypatch):
        # Live OTHER holder -> underlying claim_artifact fails -> no shape write.
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "plan", "busy", session_id="other-sid")
        _write_session(repo, "other-sid", _fresh())
        assert claims.claim_plan("busy", cwd=str(repo)) is False

    def test_claim_plan_rejects_full_path_arg(self, tmp_path, monkeypatch, capsys):
        # DEFECT 1(b) regression: a path-shaped slug (docs/plans/foo.md) must
        # be rejected loud, not silently mkdir'd into a bogus nested claim
        # dir. Rejected BEFORE any claim dir is created.
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_plan("docs/plans/2026-07-26-some-plan.md", cwd=str(repo)) is False
        assert not _claim_dir(repo, "plan", "docs").exists()
        err = capsys.readouterr().err
        assert "bare plan slug" in err

    def test_claim_plan_rejects_flag_shaped_slug(self, tmp_path, monkeypatch, capsys):
        # A `--help`-typo slug must be rejected loud, not silently mkdir'd
        # into a real claim dir nobody can find or reason about. Rejected
        # BEFORE any claim dir is created.
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_plan("--help", cwd=str(repo)) is False
        assert not _claim_dir(repo, "plan", "--help").exists()
        err = capsys.readouterr().err
        assert "bare plan slug" in err

    def test_claim_plan_rejects_empty_slug(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_plan("", cwd=str(repo)) is False
        assert not _claim_dir(repo, "plan", "").exists()

    def test_claim_plan_rejects_windows_path_separator(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_plan("docs\\plans\\2026-07-26-some-plan.md", cwd=str(repo)) is False

    def test_claim_plan_rejects_bare_md_suffix(self, tmp_path, monkeypatch):
        # No path separator, but still not a bare slug -- ".md" suffix alone
        # is enough to reject (matches the d5-emitter defect shape: a
        # caller might strip the directory but forget the extension).
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_plan("2026-07-26-some-plan.md", cwd=str(repo)) is False

    def test_claim_plan_still_succeeds_on_genuine_stale_takeover(self, tmp_path, monkeypatch):
        # A valid bare slug held by a genuinely dead session must still take
        # over successfully -- the reject-path-shaped-input fix must NOT
        # weaken this.
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "plan", "old-plan", session_id="dead-sid", pid="99999999")
        _write_session(repo, "dead-sid", _stale(pid="99999999"))
        assert claims.claim_plan("old-plan", cwd=str(repo)) is True
        assert _claim_dir(repo, "plan", "old-plan").is_dir()

    @pytest.mark.skipif(
        os.name == "nt",
        reason="fixture built via POSIX `ps -o lstart=`; see test_liveness.py's TestSessionLiveGoldenDiff",
    )
    def test_claim_plan_contention_message_uses_liveness_basis_pid_not_stale_claim_pid(
        self, tmp_path, monkeypatch, capsys
    ):
        # Discriminating case: the claim dir's recorded `pid` (a long-dead
        # value written at claim time) differs from the holding session's
        # registry `stable_pid` (this test process's own live pid) -- the
        # LIVENESS VERDICT rests on stable_pid (session_live Layer 1), but
        # the old message text printed the stale claim-dir pid instead. A
        # test asserting only "some pid appears" would pass against the old
        # broken behaviour; this asserts the LIVE stable_pid appears and the
        # stale claim-dir pid is labelled as recorded-at-claim-time, not
        # presented as a live process to `ps -p`.
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        stale_claim_pid = "424242"
        _make_claim(repo, "plan", "busy", session_id="other-sid", pid=stale_claim_pid)
        lstart = _self_lstart()
        epoch = core.lstart_to_epoch(lstart)
        assert epoch > 0
        _write_session(
            repo,
            "other-sid",
            {
                "pid": "999",
                "last_activity": core.now_iso(),
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": lstart,
                "stable_pid_start_epoch": str(epoch),
            },
        )
        assert claims.claim_plan("busy", cwd=str(repo)) is False
        err = capsys.readouterr().err
        assert str(os.getpid()) in err
        assert stale_claim_pid not in err
        assert "other-sid" in err

    def test_claim_plan_still_succeeds_on_valid_slug(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.claim_plan("2026-07-26-a-fine-slug", cwd=str(repo)) is True
        assert _claim_dir(repo, "plan", "2026-07-26-a-fine-slug").is_dir()

    def test_claim_artifact_mkdir_failure_after_stale_takeover_is_loud_and_false(
        self, tmp_path, monkeypatch
    ):
        # DEFECT 1(a): a genuine post-takeover mkdir failure (not the reject
        # path -- a real OSError, e.g. the parent claims dir replaced by a
        # file) must return False, stderr, and never a silent success.
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        claims_dir = Path(repo) / ".git" / "coordinator-sessions" / "plan-claims"
        claims_dir.mkdir(parents=True)
        # Pre-create the basename as a FILE (not a dir) sibling one level up
        # so shutil.rmtree's post-takeover os.mkdir collides with a
        # non-removable obstruction -- monkeypatch os.mkdir to always raise
        # after the first EEXIST-fallthrough, isolating the "mkdir keeps
        # failing after takeover" branch deterministically.
        import coordinator_core.session.claims as claims_mod

        real_mkdir = os.mkdir
        calls = {"n": 0}

        def _flaky_mkdir(path, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                # first call (line ~251): pretend EEXIST so we fall to the
                # stale-takeover branch.
                raise OSError("simulated EEXIST")
            # second call (post-rmtree takeover attempt): keep failing.
            raise OSError("simulated post-takeover mkdir failure")

        monkeypatch.setattr(claims_mod.os, "mkdir", _flaky_mkdir)
        result = claims_mod.claim_plan("2026-07-26-flaky-plan", cwd=str(repo))
        assert result is False


# ---------------------------------------------------------------------------
# release_artifact (1030-1062)
# ---------------------------------------------------------------------------


class TestReleaseArtifact:
    def test_required_args_raise(self):
        with pytest.raises(ValueError):
            claims.release_artifact("", "b")
        with pytest.raises(ValueError):
            claims.release_artifact("memo", "")

    def test_holder_releases(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "memo", "r1", session_id="me-sid")
        assert claims.release_artifact("memo", "r1", cwd=str(repo)) is True
        assert not _claim_dir(repo, "memo", "r1").exists()

    def test_non_holder_is_noop_success(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "memo", "r2", session_id="other-sid")
        assert claims.release_artifact("memo", "r2", cwd=str(repo)) is True
        # NOT the holder — the claim must remain.
        assert _claim_dir(repo, "memo", "r2").is_dir()

    def test_absent_claim_is_noop_success(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert claims.release_artifact("memo", "nope", cwd=str(repo)) is True

    def test_bad_baton_root_is_noop_success(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        assert (
            claims.release_artifact(
                "memo", "x", baton_repo_root=str(tmp_path / "nope"), cwd=str(repo)
            )
            is True
        )

    def test_plan_release_clears_the_session_shape_pointer(
        self, tmp_path, monkeypatch
    ):
        """`claim_plan` writes `plan.path` into session-shape.json; until this
        landed, nothing unwrote it, so a released plan stayed resolvable via
        `claimed_plan.resolve_claimed_plan_path`'s tier (a) and `/handoff`
        after a shipped plan failed loud on a DivergentDeliverableIdError
        (coordinator-claude-em memo, 2026-08-10)."""
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "plan", "p-shipped", session_id="me-sid")
        shape.session_shape_set(
            "me-sid",
            {"plan": {"path": "docs/plans/p-shipped.md", "scope_mode": "feature"}},
            str(repo),
        )
        assert claims.release_artifact("plan", "p-shipped", cwd=str(repo)) is True
        assert not _claim_dir(repo, "plan", "p-shipped").exists()
        parsed = json.loads(shape.session_shape_read("me-sid", str(repo)))
        # Cleared to `{}`, NEVER `None` — ceremony.session_instructions does
        # `setdefault("plan", {})[...]  = scope_override`, which subscripts an
        # explicit null.
        assert parsed["plan"] == {}

    def test_plan_release_leaves_a_pointer_naming_a_different_plan(
        self, tmp_path, monkeypatch
    ):
        """Releasing one plan must not blank a shape pointer that names
        another — a session may hold several plan claims."""
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "plan", "p-one", session_id="me-sid")
        shape.session_shape_set(
            "me-sid", {"plan": {"path": "docs/plans/p-two.md"}}, str(repo)
        )
        assert claims.release_artifact("plan", "p-one", cwd=str(repo)) is True
        parsed = json.loads(shape.session_shape_read("me-sid", str(repo)))
        assert parsed["plan"]["path"] == "docs/plans/p-two.md"

    def test_non_holder_plan_release_leaves_the_pointer_alone(
        self, tmp_path, monkeypatch
    ):
        """The no-op release path (not the holder) must not reach the shape
        write either — the claim is a live peer's, and so is the pointer's
        meaning."""
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        _make_claim(repo, "plan", "p-peer", session_id="other-sid")
        shape.session_shape_set(
            "me-sid", {"plan": {"path": "docs/plans/p-peer.md"}}, str(repo)
        )
        assert claims.release_artifact("plan", "p-peer", cwd=str(repo)) is True
        assert _claim_dir(repo, "plan", "p-peer").is_dir()
        parsed = json.loads(shape.session_shape_read("me-sid", str(repo)))
        assert parsed["plan"]["path"] == "docs/plans/p-peer.md"


# ---------------------------------------------------------------------------
# clear_claim_if_dead (1097-1157)
# ---------------------------------------------------------------------------


class TestClearClaimIfDead:
    def test_required_args_raise(self):
        with pytest.raises(ValueError):
            claims.clear_claim_if_dead("", "b")
        with pytest.raises(ValueError):
            claims.clear_claim_if_dead("plan", "")

    def test_invalid_class_returns_false(self, tmp_path):
        assert claims.clear_claim_if_dead("bogus", "x", cwd=str(tmp_path)) is False

    def test_live_holder_refused(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "c-live", session_id="11111111-1111-4111-8111-111111111111")
        _write_session(repo, "11111111-1111-4111-8111-111111111111", _fresh())
        assert claims.clear_claim_if_dead("handoff", "c-live", cwd=str(repo)) is False
        assert _claim_dir(repo, "handoff", "c-live").is_dir()

    def test_dead_holder_cleared(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "c-dead", session_id="11111111-1111-4111-8111-111111111111")
        _write_session(repo, "11111111-1111-4111-8111-111111111111", _stale())
        assert claims.clear_claim_if_dead("handoff", "c-dead", cwd=str(repo)) is True
        assert not _claim_dir(repo, "handoff", "c-dead").exists()

    def test_dead_handoff_claim_reconciles_frontmatter(self, tmp_path):
        # Reap/ship bug fix 1: clear_claim_if_dead must ALSO flip the handoff's
        # own frontmatter (status:claimed -> open), not just clear the lock-dir.
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "h1.md", session_id="11111111-1111-4111-8111-111111111111")
        _write_session(repo, "11111111-1111-4111-8111-111111111111", _stale())
        hpath = _write_handoff(repo, "h1.md")
        assert claims.clear_claim_if_dead("handoff", "h1.md", cwd=str(repo)) is True
        assert not _claim_dir(repo, "handoff", "h1.md").exists()
        text = hpath.read_text(encoding="utf-8")
        assert "status: open" in text
        assert "claimed_by" not in text

    def test_dead_handoff_claim_wrong_deployment_state_skips(self, tmp_path, capsys):
        # Explicit precondition: deployment_state outside {in_flight,
        # ready_to_fire} SKIPS the frontmatter step (no crash) but still
        # clears the lock-dir.
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "h2.md", session_id="11111111-1111-4111-8111-111111111111")
        _write_session(repo, "11111111-1111-4111-8111-111111111111", _stale())
        hpath = _write_handoff(repo, "h2.md", deployment_state="shipped")
        assert claims.clear_claim_if_dead("handoff", "h2.md", cwd=str(repo)) is True
        assert not _claim_dir(repo, "handoff", "h2.md").exists()
        assert "status: claimed" in hpath.read_text(encoding="utf-8")
        assert "skipping frontmatter reconcile" in capsys.readouterr().err

    def test_absent_claim_idempotent_success(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert claims.clear_claim_if_dead("plan", "ghost", cwd=str(repo)) is True

    def test_missing_sessions_dir_idempotent_success(self, tmp_path):
        repo = _make_repo(tmp_path)
        # No coordinator-sessions dir at all -> no claim can exist -> success.
        assert claims.clear_claim_if_dead("memo", "x", cwd=str(repo)) is True

    def test_legacy_pid_only_dead_pid_cleared(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        # No session_id file -> legacy branch; dead pid -> not live -> cleared,
        # with a DISTINCT stderr note.
        _make_claim(repo, "plan", "legacy", pid=2**31 - 1)
        assert claims.clear_claim_if_dead("plan", "legacy", cwd=str(repo)) is True
        assert not _claim_dir(repo, "plan", "legacy").exists()
        assert "legacy pid-only claim" in capsys.readouterr().err

    def test_bad_baton_root_fails_loud(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert (
            claims.clear_claim_if_dead(
                "plan", "x", baton_repo_root=str(tmp_path / "nope"), cwd=str(repo)
            )
            is False
        )


# ---------------------------------------------------------------------------
# class_ == "artifact" (PATH-TOUCH claim plane widening)
#
# cross-repo/inbox/2026-08-11-coordinator-claude-em-dead-claim-on-a-non-plan-
# artifact-has-no-clear-path.md -- who-claims-path answers over the
# PATH-TOUCH plane (claim_index / touched.txt T-R events), a DIFFERENT
# store than the mkdir-based handoff/memo/plan claim-record store the
# classes above manage. These tests exercise the class_=="artifact"
# widening of clear_claim_if_dead / release_artifact onto that plane.
# ---------------------------------------------------------------------------


def _write_touch_claim(repo, sid, path, when=None):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    touched = sdir / "touched.txt"
    with open(touched, "a", encoding="utf-8") as fh:
        fh.write(scope.format_touch_event("T", path, when) + "\n")
    return touched


class TestClearClaimIfDeadArtifactClass:
    _TARGET = "coordinator/commands/workday-start.md"

    def test_dead_claimant_on_non_classed_path_clears(self, tmp_path):
        repo = _make_repo(tmp_path)
        sid = "11111111-1111-4111-8111-111111111111"
        _write_session(repo, sid, _stale())
        touched = _write_touch_claim(repo, sid, self._TARGET)
        assert claims.clear_claim_if_dead("artifact", self._TARGET, cwd=str(repo)) is True
        verb, _ts, path = scope.parse_touch_event(
            touched.read_text(encoding="utf-8").splitlines()[-1]
        )
        assert (verb, path) == ("R", self._TARGET)

    def test_live_claimant_on_non_classed_path_refuses(self, tmp_path):
        # Negative control: a live claimant must NOT be cleared.
        repo = _make_repo(tmp_path)
        sid = "22222222-2222-4222-8222-222222222222"
        _write_session(repo, sid, _fresh())
        touched = _write_touch_claim(repo, sid, self._TARGET)
        assert claims.clear_claim_if_dead("artifact", self._TARGET, cwd=str(repo)) is False
        verb, _ts, _path = scope.parse_touch_event(
            touched.read_text(encoding="utf-8").splitlines()[-1]
        )
        assert verb == "T"  # untouched -- still claimed

    def test_unclaimed_path_is_clean_noop(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert claims.clear_claim_if_dead("artifact", "some/never/touched.md", cwd=str(repo)) is True

    def test_legacy_classed_forms_unchanged(self, tmp_path):
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "c-dead", session_id="33333333-3333-4333-8333-333333333333")
        _write_session(repo, "33333333-3333-4333-8333-333333333333", _stale())
        assert claims.clear_claim_if_dead("handoff", "c-dead", cwd=str(repo)) is True
        assert not _claim_dir(repo, "handoff", "c-dead").exists()

        _make_claim(repo, "memo", "m-live", session_id="44444444-4444-4444-8444-444444444444")
        _write_session(repo, "44444444-4444-4444-8444-444444444444", _fresh())
        assert claims.clear_claim_if_dead("memo", "m-live", cwd=str(repo)) is False
        assert _claim_dir(repo, "memo", "m-live").is_dir()

        assert claims.clear_claim_if_dead("bogus", "x", cwd=str(repo)) is False


class TestReleaseArtifactArtifactClass:
    _TARGET = "coordinator/commands/workday-start.md"

    def test_holder_self_releases(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        touched = _write_touch_claim(repo, "me-sid", self._TARGET)
        assert claims.release_artifact("artifact", self._TARGET, cwd=str(repo)) is True
        verb, _ts, path = scope.parse_touch_event(
            touched.read_text(encoding="utf-8").splitlines()[-1]
        )
        assert (verb, path) == ("R", self._TARGET)

    def test_non_holder_path_is_noop(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _set_me(monkeypatch)
        touched = _write_touch_claim(repo, "other-sid", self._TARGET)
        assert claims.release_artifact("artifact", self._TARGET, cwd=str(repo)) is True
        verb, _ts, _path = scope.parse_touch_event(
            touched.read_text(encoding="utf-8").splitlines()[-1]
        )
        assert verb == "T"  # a peer's claim -- release_artifact never touches it


# ---------------------------------------------------------------------------
# list_claims_by_session
# ---------------------------------------------------------------------------


class TestListClaimsBySession:
    def test_list_claims_by_session_no_sessions_dir_returns_empty(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert claims.list_claims_by_session("me-sid", cwd=str(repo)) == []

    def test_list_claims_by_session_no_git_repo_returns_empty(self, tmp_path):
        assert claims.list_claims_by_session("me-sid", cwd=str(tmp_path)) == []

    def test_list_claims_by_session_single_matching_handoff_claim(self, tmp_path):
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "h1.md", session_id="me-sid")
        assert claims.list_claims_by_session("me-sid", cwd=str(repo)) == [
            ("handoff-claims", "h1.md")
        ]

    def test_list_claims_by_session_matches_span_all_three_classes(self, tmp_path):
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "h1.md", session_id="me-sid")
        _make_claim(repo, "memo", "m1.md", session_id="me-sid")
        _make_claim(repo, "plan", "p1", session_id="me-sid")
        result = claims.list_claims_by_session("me-sid", cwd=str(repo))
        assert sorted(result) == sorted(
            [
                ("handoff-claims", "h1.md"),
                ("memo-claims", "m1.md"),
                ("plan-claims", "p1"),
            ]
        )

    def test_list_claims_by_session_other_sessions_claims_excluded(self, tmp_path):
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "mine.md", session_id="me-sid")
        _make_claim(repo, "handoff", "theirs.md", session_id="other-sid")
        assert claims.list_claims_by_session("me-sid", cwd=str(repo)) == [
            ("handoff-claims", "mine.md")
        ]

    def test_list_claims_by_session_missing_session_id_file_not_a_match(self, tmp_path):
        # pid-only legacy claim, no session_id file at all -> never matches,
        # regardless of queried sid (this is the ownership-index FILTER
        # discipline, not _read_holder's tolerant diagnostic fallback).
        repo = _make_repo(tmp_path)
        _make_claim(repo, "plan", "legacy", pid=123)
        assert claims.list_claims_by_session("me-sid", cwd=str(repo)) == []

    def test_list_claims_by_session_empty_session_id_file_not_a_match(self, tmp_path):
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "empty.md", session_id="")
        assert claims.list_claims_by_session("me-sid", cwd=str(repo)) == []

    def test_list_claims_by_session_survives_archive_style_state_ship_and_archive_do_not_release(
        self, tmp_path
    ):
        # Review: code-reviewer slice 2 (2026-07-27), Finding 1 — this test is
        # FIXTURE-ONLY: it hand-writes a shipped-looking handoff file next to
        # a claim dir and reads the claim dir straight back; it never calls
        # `handoff_transition._ship` or `wsc_commit._native_cs_release_artifact`,
        # the actual ship/archive call sites the lifecycle docstring on
        # `list_claims_by_session_checked` describes. It proves
        # `list_claims_by_session` can read a directory it was just told to
        # create — NOT that the real ship/archive code leaves the claim dir
        # alone. See the companion
        # `test_list_claims_by_session_survives_real_ship_call_site` below for
        # the version that actually drives `_ship`; the ship-step's plan-only
        # release wiring inside `wsc_commit.py` itself
        # (`_native_cs_release_artifact(common_dir, sid, "plan", ...)` at
        # `wsc_commit.py:3471`) remains unexercised by any test — invoking it
        # requires the full close-ceremony pipeline (PipelineContext, commit
        # outcome plumbing), assessed as disproportionate scaffolding for a
        # unit test here; that half of the guarantee is verified by
        # file:line citation in the docstring, not by execution.
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "shipped-and-archived.md", session_id="me-sid")
        _write_handoff(
            repo,
            "shipped-and-archived.md",
            status="consumed",
            deployment_state="shipped",
        )
        assert claims.list_claims_by_session("me-sid", cwd=str(repo)) == [
            ("handoff-claims", "shipped-and-archived.md")
        ]

    def test_list_claims_by_session_survives_real_ship_call_site(self, tmp_path):
        """Drives the REAL `handoff_transition._ship` mutator (the function
        the archive path's `post_commit_stamp_and_ship` calls to flip
        `deployment_state` to `shipped`) against a claimed handoff, then
        asserts the handoff-claims record — read via the real
        `list_claims_by_session` — survives. This is the actual regression
        guard the fixture-only sibling test above overclaimed: if a future
        `_ship` change started calling `release_artifact`/`claims.` on a
        handoff claim, THIS test would catch it, because it genuinely calls
        `_ship`, not a hand-fabricated stand-in for its output."""
        from coordinator_core.ops import handoff_transition

        repo = _make_repo(tmp_path)
        sid = "me-sid"
        basename = "real-ship-call-site.md"
        hpath = _write_handoff(repo, basename)
        _make_claim(repo, "handoff", basename, session_id=sid)

        # shipped_in must already be on disk before deployment_state flips to
        # shipped (stamp-before-ship ordering, see handoff_transition._ship's
        # own docstring) — simulate the real stamp step's prior write.
        hpath.write_text(
            hpath.read_text(encoding="utf-8").replace(
                "deployment_state: in_flight\n",
                "deployment_state: in_flight\nshipped_in: deadbeef1234\nshipped_in_kind: ship-commit\n",
            ),
            encoding="utf-8",
        )

        result = handoff_transition._ship(str(hpath), worktree=repo, repo_root=repo)
        assert result.get("exit_code") == 0, result
        assert "deployment_state: shipped" in hpath.read_text(encoding="utf-8")

        assert claims.list_claims_by_session(sid, cwd=str(repo)) == [
            ("handoff-claims", basename)
        ]


# ---------------------------------------------------------------------------
# list_claims_by_session_checked
# ---------------------------------------------------------------------------


class TestListClaimsBySessionChecked:
    def test_unresolvable_cwd_returns_empty_matches_and_one_error(self, tmp_path):
        matches, errors = claims.list_claims_by_session_checked(
            "me-sid", cwd=str(tmp_path)
        )
        assert matches == []
        assert len(errors) == 1
        assert errors[0]

    def test_normal_in_repo_call_returns_matches_and_no_errors(self, tmp_path):
        repo = _make_repo(tmp_path)
        _make_claim(repo, "handoff", "h1.md", session_id="me-sid")
        matches, errors = claims.list_claims_by_session_checked(
            "me-sid", cwd=str(repo)
        )
        assert matches == [("handoff-claims", "h1.md")]
        assert errors == []


class TestMyAgentTouched:
    def _make_agent(self, repo, aid, em_sid, paths):
        adir = Path(repo) / ".git" / "coordinator-sessions" / ".agents" / aid
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "em-session-id.txt").write_text(em_sid + "\n")
        (adir / "touched.txt").write_text("\n".join(paths) + "\n")
        return adir

    def test_required_arg_raises(self):
        with pytest.raises(ValueError):
            claims.my_agent_touched("")

    def test_no_agents_dir_returns_empty(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert claims.my_agent_touched("s", cwd=str(repo)) == []

    def test_own_session_agents_collected(self, tmp_path):
        repo = _make_repo(tmp_path)
        self._make_agent(repo, "a1", "my-sid", ["src/a.py", "src/b.py"])
        out = claims.my_agent_touched("my-sid", mode="exact", cwd=str(repo))
        assert out == ["src/a.py", "src/b.py"]

    def test_exact_mode_excludes_other_sessions(self, tmp_path):
        repo = _make_repo(tmp_path)
        self._make_agent(repo, "a1", "my-sid", ["mine.py"])
        self._make_agent(repo, "a2", "sibling-sid", ["theirs.py"])
        out = claims.my_agent_touched("my-sid", mode="exact", cwd=str(repo))
        assert out == ["mine.py"]

    def test_broadened_mode_includes_live_sibling(self, tmp_path):
        repo = _make_repo(tmp_path)
        # A LIVE sibling session; its agent's paths join the candidate set in
        # broadened mode (sentinel-pollution recovery).
        _write_session(repo, "live-sib", _fresh())
        self._make_agent(repo, "a1", "my-sid", ["mine.py"])
        self._make_agent(repo, "a2", "live-sib", ["sib.py"])
        out = claims.my_agent_touched("my-sid", mode="broadened", cwd=str(repo))
        assert set(out) == {"mine.py", "sib.py"}

    def test_malformed_backpointer_soft_skipped(self, tmp_path):
        repo = _make_repo(tmp_path)
        adir = Path(repo) / ".git" / "coordinator-sessions" / ".agents" / "a1"
        adir.mkdir(parents=True)
        (adir / "em-session-id.txt").write_text("")  # empty -> skip
        (adir / "touched.txt").write_text("x.py\n")
        assert claims.my_agent_touched("my-sid", cwd=str(repo)) == []

    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="chmod 0o000 permission denial is not reliable on Windows or as root",
    )
    def test_unreadable_agents_dir_does_not_silently_read_as_zero_subdirs(self, tmp_path, capsys):
        """Silent-enumeration guard: Path.glob() silently swallows
        PermissionError even on a flat, non-recursive pattern (empirically
        re-verified: a chmod-000 dir yields an empty iterator from glob(), no
        exception) — an unreadable .agents/ dir must log a warning naming the
        failure, distinguishing it from a genuinely-empty .agents/ dir (which
        logs nothing). Attribution is still advisory (FAIL-OPEN): the call
        must not raise, and returns [] either way."""
        repo = _make_repo(tmp_path)
        self._make_agent(repo, "a1", "my-sid", ["mine.py"])
        agents_dir = Path(repo) / ".git" / "coordinator-sessions" / ".agents"

        original_mode = agents_dir.stat().st_mode
        os.chmod(agents_dir, 0o000)
        try:
            out = claims.my_agent_touched("my-sid", mode="exact", cwd=str(repo))
        finally:
            os.chmod(agents_dir, original_mode)

        assert out == []
        err = capsys.readouterr().err
        assert str(agents_dir) in err, (
            "an unreadable .agents/ dir must log a warning naming the "
            f"unscannable path; stderr was: {err!r}"
        )

    def test_journal_format_yields_paths_not_raw_lines(self, tmp_path):
        """`touched.txt` is a `T`/`R` event journal, and this reader must
        project it like every other one. Returning the raw
        `'T <ISO> <path>'` line makes the whole sub-agent fan-out leg of
        `safe_commit_offer`'s `safe_paths` contribute strings that match no
        file — reported cross-repo by example-market-data-repo-em, 2026-08-04."""
        repo = _make_repo(tmp_path)
        self._make_agent(
            repo,
            "a1",
            "my-sid",
            [
                "T 2026-08-03T23:18:56.014950Z src/a.py",
                "T 2026-08-03T23:19:07.182209Z src/b.py",
            ],
        )
        out = claims.my_agent_touched("my-sid", mode="exact", cwd=str(repo))
        assert out == ["src/a.py", "src/b.py"]

    def test_journal_release_event_is_honoured(self, tmp_path):
        """An `R` event appended by `release_committed_claims` into the
        agent's OWN dir must drop that path — the release is invisible if
        the lines are carried raw."""
        repo = _make_repo(tmp_path)
        self._make_agent(
            repo,
            "a1",
            "my-sid",
            [
                "T 2026-08-03T23:18:56.014950Z src/released.py",
                "T 2026-08-03T23:18:57.000000Z src/kept.py",
                "R 2026-08-03T23:20:00.000000Z src/released.py",
            ],
        )
        out = claims.my_agent_touched("my-sid", mode="exact", cwd=str(repo))
        assert out == ["src/kept.py"]

    def test_reclaim_after_release_is_claimed_again(self, tmp_path):
        repo = _make_repo(tmp_path)
        self._make_agent(
            repo,
            "a1",
            "my-sid",
            [
                "T 2026-08-03T23:18:56.014950Z src/a.py",
                "R 2026-08-03T23:20:00.000000Z src/a.py",
                "T 2026-08-03T23:21:00.000000Z src/a.py",
            ],
        )
        assert claims.my_agent_touched("my-sid", mode="exact", cwd=str(repo)) == [
            "src/a.py"
        ]

    def test_path_with_spaces_survives_projection(self, tmp_path):
        repo = _make_repo(tmp_path)
        self._make_agent(
            repo, "a1", "my-sid", ["T 2026-08-03T23:18:56.014950Z docs/a b c.md"]
        )
        assert claims.my_agent_touched("my-sid", mode="exact", cwd=str(repo)) == [
            "docs/a b c.md"
        ]

    def test_empty_agents_dir_is_genuinely_silent(self, tmp_path, capsys):
        """The negative case for the above: a genuinely-empty (but readable)
        .agents/ dir must NOT log anything — only an actual scan failure
        warns."""
        repo = _make_repo(tmp_path)
        agents_dir = Path(repo) / ".git" / "coordinator-sessions" / ".agents"
        agents_dir.mkdir(parents=True)

        out = claims.my_agent_touched("my-sid", mode="exact", cwd=str(repo))

        assert out == []
        assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# self_claim (726-758)
# ---------------------------------------------------------------------------


class TestSelfClaim:
    def test_required_arg_raises(self):
        with pytest.raises(ValueError):
            claims.self_claim("")

    def test_fast_path_env_sid_appends(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "fast-sid")
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "fast-sid"
        sdir.mkdir(parents=True)
        (sdir / "touched.txt").write_text("")
        assert claims.self_claim("edited/file.py", cwd=str(repo)) is True
        lines = (sdir / "touched.txt").read_text().splitlines()
        assert len(lines) == 1
        verb, _ts, path = scope.parse_touch_event(lines[0])
        assert (verb, path) == ("T", "edited/file.py")

    def test_fast_path_session_dir_absent_noop(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "ghost-sid")
        # No session dir -> nothing to claim against, but still True (fail-open).
        assert claims.self_claim("x.py", cwd=str(repo)) is True

    def test_fallback_exactly_one_live_claims(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _clear_all_sid_env(monkeypatch)
        _write_session(repo, "solo-live", _fresh())
        assert claims.self_claim("f.py", cwd=str(repo)) is True
        touched = (
            Path(repo)
            / ".git"
            / "coordinator-sessions"
            / "solo-live"
            / "touched.txt"
        )
        lines = touched.read_text().splitlines()
        assert len(lines) == 1
        verb, _ts, path = scope.parse_touch_event(lines[0])
        assert (verb, path) == ("T", "f.py")

    def test_fallback_zero_live_skips(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _clear_all_sid_env(monkeypatch)
        # Only a stale session -> zero live -> skip, still True.
        _write_session(repo, "stale-only", _stale())
        assert claims.self_claim("f.py", cwd=str(repo)) is True
        touched = (
            Path(repo)
            / ".git"
            / "coordinator-sessions"
            / "stale-only"
            / "touched.txt"
        )
        assert not touched.exists()

    def test_fallback_ambiguous_two_live_skips(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _clear_all_sid_env(monkeypatch)
        _write_session(repo, "live-a", _fresh())
        _write_session(repo, "live-b", _fresh())
        assert claims.self_claim("f.py", cwd=str(repo)) is True
        base = Path(repo) / ".git" / "coordinator-sessions"
        assert not (base / "live-a" / "touched.txt").exists()
        assert not (base / "live-b" / "touched.txt").exists()


# ---------------------------------------------------------------------------
# self_claim — absolute-path normalization (coordinator-claude security-audit 2026-07-31:
# self_claim previously appended `path` verbatim with no normalization at
# all, the live gap distinct from scope.touch()'s existing guard — see
# claims.self_claim's docstring and scope.normalize_touch_path).
# ---------------------------------------------------------------------------


class TestSelfClaimAbsolutePathNormalization:
    def test_absolute_untracked_path_inside_repo_normalized_to_relative(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "abs-sid")
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "abs-sid"
        sdir.mkdir(parents=True)
        (sdir / "touched.txt").write_text("")
        (Path(repo) / "src").mkdir()
        target = Path(repo) / "src" / "new.py"
        target.write_text("y")  # untracked -> relpath branch
        assert claims.self_claim(str(target), cwd=str(repo)) is True
        lines = (sdir / "touched.txt").read_text().splitlines()
        assert len(lines) == 1
        verb, _ts, path = scope.parse_touch_event(lines[0])
        assert (verb, path) == ("T", "src/new.py")

    def test_absolute_tracked_path_normalized_via_git_ls_files(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "abs-sid2")
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "abs-sid2"
        sdir.mkdir(parents=True)
        (sdir / "touched.txt").write_text("")
        target = Path(repo) / "README.md"  # tracked -> git ls-files branch
        assert claims.self_claim(str(target), cwd=str(repo)) is True
        lines = (sdir / "touched.txt").read_text().splitlines()
        assert len(lines) == 1
        verb, _ts, path = scope.parse_touch_event(lines[0])
        assert (verb, path) == ("T", "README.md")

    def test_still_absolute_path_skipped_never_written_raw(
        self, tmp_path, monkeypatch
    ):
        """The refresh-queries.py self_claim(file_path) call site always
        passes an absolute path (coordinator-claude security-audit 2026-07-31 finding);
        this asserts that even when normalization cannot resolve it to
        repo-relative, the raw absolute path is never appended."""
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "abs-sid3")
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "abs-sid3"
        sdir.mkdir(parents=True)
        (sdir / "touched.txt").write_text("")

        def _boom(*a, **k):
            raise ValueError("simulated relpath failure")

        from coordinator_core.session import scope as scope_mod

        monkeypatch.setattr(scope_mod.os.path, "relpath", _boom)
        outside = "/totally/outside/xyz.py"
        assert claims.self_claim(outside, cwd=str(repo)) is True
        lines = (sdir / "touched.txt").read_text().splitlines()
        assert outside not in lines
        assert lines == []


def _write_successor(repo, basename, predecessor_basename, field="predecessor"):
    """Write a handoff naming *predecessor_basename* via *field*, live by default.

    Pass a basename prefixed with ``archive/handoffs/<month>/`` to place the
    successor in the archive tree instead — the shape a resolved chain actually
    has on disk, and the one the reaper regression below turns on.
    """
    if basename.startswith("archive/"):
        hpath = Path(repo) / basename
    else:
        hpath = Path(repo) / "state" / "handoffs" / basename
    hpath.parent.mkdir(parents=True, exist_ok=True)
    hpath.write_text(
        "---\n"
        "title: successor\n"
        "created: 2026-07-24\n"
        "branch: work/t/2026-07-24\n"
        + (
            # additional_predecessors is the one array-shaped edge field
            # (dag.EDGE_KIND_META marks it multi:True).
            f"{field}:\n  - state/handoffs/{predecessor_basename}\n"
            if field == "additional_predecessors"
            else f"{field}: state/handoffs/{predecessor_basename}\n"
        )
        +
        "category: infra\n"
        "summary: test fixture successor\n"
        "status: claimed\n"
        "deployment_state: shipped\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    return hpath


class TestReconcileDeadHandoffClaimFrontmatter:
    """The succeeded-baton carve-out (2026-07-30 break-class fix).

    Three claude-klabauter chains were resurrected by the crash-orphan reaper: it unclaimed
    a predecessor whose successor had already shipped and archived, restoring
    ``status: open`` + ``pickup_ready: true`` AND destroying the predecessor-side
    ``claimed`` evidence DR-242's gate needs, which left the baton permanently
    unclosable by d6 / ``handoff.archive_transition`` mode="supersede".
    """

    @staticmethod
    def _sessions_dir(repo):
        return Path(repo) / ".git" / "coordinator-sessions"

    @staticmethod
    def _status_of(hpath):
        for line in hpath.read_text(encoding="utf-8").splitlines():
            if line.startswith("status:"):
                return line.split(":", 1)[1].strip()
        return None

    @staticmethod
    def _reaped_from_session_of(hpath):
        for line in hpath.read_text(encoding="utf-8").splitlines():
            if line.startswith("reaped_from_session:"):
                return line.split(":", 1)[1].strip()
        return None

    def test_unclaims_a_dead_baton_with_no_successor(self, tmp_path):
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(repo, "pred.md")
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._status_of(hpath) == "open"

    def test_reaped_from_session_matches_the_dead_holders_sid(self, tmp_path):
        """AC3(a): the unclaimed handoff carries reaped_from_session == the
        dead holder's sid. ``_write_handoff``'s fixture ``claimed_by:
        11111111-1111-4111-8111-111111111111`` is exactly the frontmatter value ``_unclaim``'s
        resolution order prefers over the claim dir's own metadata."""
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(repo, "pred.md")
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._reaped_from_session_of(hpath) == "11111111-1111-4111-8111-111111111111"

    def test_reaped_from_session_falls_back_to_claim_dir_holder(self, tmp_path):
        """When the handoff frontmatter carries no claimed_by/consumed_by sid,
        the reap path's own claim-dir-sourced fallback (_read_holder's
        session_id read) is what lands in reaped_from_session."""
        repo = _make_repo(tmp_path)
        hdir = Path(repo) / "state" / "handoffs"
        hdir.mkdir(parents=True, exist_ok=True)
        hpath = hdir / "pred.md"
        hpath.write_text(
            "---\n"
            "title: t\n"
            "created: 2026-07-24\n"
            "branch: work/t/2026-07-24\n"
            "predecessor: none\n"
            "category: infra\n"
            "summary: test fixture handoff\n"
            "status: claimed\n"
            "deployment_state: in_flight\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )
        _make_claim(repo, "handoff", "pred.md", session_id="22222222-2222-4222-8222-222222222222", pid="123")
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._reaped_from_session_of(hpath) == "22222222-2222-4222-8222-222222222222"

    def test_reaped_from_session_absent_when_no_claim_dir_evidence_recoverable(
        self, tmp_path
    ):
        """Review: coordinator:code-reviewer — a dead claim with no
        claimed_by/consumed_by frontmatter AND no session_id file in the
        claim dir must unclaim successfully with reaped_from_session left
        unset entirely, never written as the literal _read_holder sentinel
        "unknown" (a non-empty string that would otherwise pass _unclaim's
        non-empty-string resolution check as if it were a real sid)."""
        repo = _make_repo(tmp_path)
        hdir = Path(repo) / "state" / "handoffs"
        hdir.mkdir(parents=True, exist_ok=True)
        hpath = hdir / "pred.md"
        hpath.write_text(
            "---\n"
            "title: t\n"
            "created: 2026-07-24\n"
            "branch: work/t/2026-07-24\n"
            "predecessor: none\n"
            "category: infra\n"
            "summary: test fixture handoff\n"
            "status: claimed\n"
            "deployment_state: in_flight\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )
        claim_dir = self._sessions_dir(repo) / "handoff-claims" / "pred.md"
        claim_dir.mkdir(parents=True, exist_ok=True)
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._status_of(hpath) == "open"
        assert self._reaped_from_session_of(hpath) is None

    def test_skips_a_baton_whose_successor_is_archived(self, tmp_path):
        """The observed shape: successor shipped and swept, predecessor still live."""
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(repo, "pred.md")
        _write_successor(repo, "archive/handoffs/2026-07/succ.md", "pred.md")
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._status_of(hpath) == "claimed"
        assert self._reaped_from_session_of(hpath) is None

    def test_skips_a_baton_with_a_live_successor(self, tmp_path):
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(repo, "pred.md")
        _write_successor(repo, "succ.md", "pred.md")
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._status_of(hpath) == "claimed"
        assert self._reaped_from_session_of(hpath) is None

    def test_skips_a_baton_named_via_additional_predecessors(self, tmp_path):
        """d6 fires one supersede per fan-in predecessor, so the edge counts here too."""
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(repo, "pred.md")
        _write_successor(repo, "succ.md", "pred.md", field="additional_predecessors")
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._status_of(hpath) == "claimed"

    def test_a_fork_is_not_a_successor_and_does_not_block_the_unclaim(self, tmp_path):
        """d6 does not fire for a fork; a fork's parent is still genuinely reapable."""
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(repo, "pred.md")
        _write_successor(repo, "fork.md", "pred.md", field="forked_from")
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._status_of(hpath) == "open"

    def test_indeterminate_enumeration_skips_rather_than_unclaims(
        self, tmp_path, monkeypatch
    ):
        repo = _make_repo(tmp_path)
        hpath = _write_handoff(repo, "pred.md")
        monkeypatch.setattr(
            claims, "_handoff_has_named_successor", lambda *_a, **_k: None
        )
        claims.reconcile_dead_handoff_claim_frontmatter(
            "pred.md", self._sessions_dir(repo)
        )
        assert self._status_of(hpath) == "claimed"


# ---------------------------------------------------------------------------
# backfill_reaped_from_session (C5)
# ---------------------------------------------------------------------------


def _write_parked_handoff(repo, basename, park_note):
    hdir = Path(repo) / "state" / "handoffs"
    hdir.mkdir(parents=True, exist_ok=True)
    hpath = hdir / basename
    lines = [
        "---",
        "title: t",
        "created: 2026-07-24",
        "branch: work/t/2026-07-24",
        "predecessor: none",
        "category: infra",
        "summary: test fixture handoff",
        "status: open",
        "deployment_state: ready_to_fire",
    ]
    if park_note is not None:
        lines.append(f"park_note: {park_note}")
    lines.append("---")
    lines.append("body")
    hpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return hpath


class TestBackfillReapedFromSession:
    """C5: parse the dead sid out of pre-C2 park_note text and write it as a
    real ``reaped_from_session`` field — write / skip / skip-and-report."""

    @staticmethod
    def _reaped_from_session_of(hpath):
        for line in hpath.read_text(encoding="utf-8").splitlines():
            if line.startswith("reaped_from_session:"):
                return line.split(":", 1)[1].strip()
        return None

    def test_writes_the_sid_parsed_from_a_parseable_park_note(self, tmp_path):
        repo = _make_repo(tmp_path)
        hpath = _write_parked_handoff(
            repo,
            "parseable.md",
            "claim released by crash-orphan reaper — holder "
            "cb90a56e-33f1-4992-b665-c2af3070c00c died without resolving; "
            "returned to pool",
        )
        result = claims.backfill_reaped_from_session(Path(repo))
        assert result["written"] == ["state/handoffs/parseable.md"]
        assert result["skipped"] == []
        assert result["errors"] == []
        assert (
            self._reaped_from_session_of(hpath)
            == "cb90a56e-33f1-4992-b665-c2af3070c00c"
        )

    def test_skips_a_baton_already_carrying_the_field(self, tmp_path):
        repo = _make_repo(tmp_path)
        hpath = _write_parked_handoff(
            repo,
            "already.md",
            "claim released by crash-orphan reaper — holder "
            "cb90a56e-33f1-4992-b665-c2af3070c00c died without resolving",
        )
        text = hpath.read_text(encoding="utf-8").replace(
            "deployment_state: ready_to_fire",
            "deployment_state: ready_to_fire\n"
            "reaped_from_session: already-recorded-sid",
        )
        hpath.write_text(text, encoding="utf-8")

        result = claims.backfill_reaped_from_session(Path(repo))
        assert result["written"] == []
        assert result["skipped"] == ["state/handoffs/already.md"]
        assert result["errors"] == []
        assert self._reaped_from_session_of(hpath) == "already-recorded-sid"

    def test_skips_and_reports_a_malformed_note(self, tmp_path, capsys):
        repo = _make_repo(tmp_path)
        hpath = _write_parked_handoff(
            repo,
            "malformed.md",
            "claim-release REVERTED 2026-07-30 — the crash-orphan reaper "
            "returned this baton to the pool at 796ebf5b, but holder "
            "517027e6 had already minted its successor...",
        )
        result = claims.backfill_reaped_from_session(Path(repo))
        assert result["written"] == []
        assert result["skipped"] == ["state/handoffs/malformed.md"]
        assert result["errors"] == []
        assert self._reaped_from_session_of(hpath) is None
        captured = capsys.readouterr()
        assert "malformed.md" in captured.err

    def test_skips_a_baton_with_no_park_note_at_all(self, tmp_path):
        """The unrecoverable class: reaped via the empty-note path before C2."""
        repo = _make_repo(tmp_path)
        hpath = _write_parked_handoff(repo, "no-note.md", None)
        result = claims.backfill_reaped_from_session(Path(repo))
        assert result["written"] == []
        assert result["skipped"] == ["state/handoffs/no-note.md"]
        assert result["errors"] == []
        assert self._reaped_from_session_of(hpath) is None

    def test_does_not_write_a_matching_park_note_under_archive_handoffs(self, tmp_path):
        """Boundary pin: the live-only scope (Part 1 fix) — an archived record
        carrying an otherwise-parseable park_note must never be targeted."""
        repo = _make_repo(tmp_path)
        adir = Path(repo) / "archive" / "handoffs" / "2026-07"
        adir.mkdir(parents=True, exist_ok=True)
        apath = adir / "archived-parked.md"
        apath.write_text(
            "\n".join(
                [
                    "---",
                    "title: t",
                    "created: 2026-07-24",
                    "branch: work/t/2026-07-24",
                    "predecessor: none",
                    "category: infra",
                    "summary: test fixture archived handoff",
                    "status: open",
                    "deployment_state: ready_to_fire",
                    "park_note: claim released by crash-orphan reaper — holder "
                    "cb90a56e-33f1-4992-b665-c2af3070c00c died without resolving; "
                    "returned to pool",
                    "---",
                    "body",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        result = claims.backfill_reaped_from_session(Path(repo))
        assert "archive/handoffs/2026-07/archived-parked.md" not in result["written"]
        assert "archive/handoffs/2026-07/archived-parked.md" not in result["skipped"]
        for _rel, msg in result["errors"]:
            assert "archived-parked.md" not in msg
        assert "reaped_from_session:" not in apath.read_text(encoding="utf-8")

    def test_second_run_is_byte_identical(self, tmp_path):
        repo = _make_repo(tmp_path)
        hpath = _write_parked_handoff(
            repo,
            "parseable.md",
            "claim released by crash-orphan reaper — holder "
            "cb90a56e-33f1-4992-b665-c2af3070c00c died without resolving",
        )
        claims.backfill_reaped_from_session(Path(repo))
        after_first = hpath.read_text(encoding="utf-8")
        result = claims.backfill_reaped_from_session(Path(repo))
        assert result["written"] == []
        assert result["skipped"] == ["state/handoffs/parseable.md"]
        assert hpath.read_text(encoding="utf-8") == after_first


# ---------------------------------------------------------------------------
# relocate_touched_path (scope.py) — docs/plans/2026-08-06-relocation-
# re-declares-the-touch-claim.md, C2. Cases (1)/(2) drive assertions through
# safe_commit_offer.compute_offer rather than the helper's internals -- the
# point under test is user-visible commit-offer behaviour, not touched.txt
# byte shape.
# ---------------------------------------------------------------------------


def _add_and_commit_tracked(repo, rel, content):
    path = Path(repo) / rel
    path.write_text(content)
    subprocess.run(["git", "add", rel], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"add {rel}"], cwd=repo, check=True)
    return path


class TestRelocateTouchedPath:
    def test_relocated_tracked_file_leaves_both_halves_claimed(self, tmp_path):
        """THE LOAD-BEARING CASE: a tracked file A this session T-claimed is
        relocated to B via relocate_touched_path. Both A (now a deletion in
        `git diff --name-only HEAD`) and B (the new untracked path) must
        land in compute_offer's safe_paths -- A's CONTINUED membership after
        the move is what proves the claim restatement leaves both halves of
        the rename claimed, not just the new path. Cannot pass before C1
        (relocate_touched_path) exists -- ImportError."""
        repo = _make_repo(tmp_path)
        _add_and_commit_tracked(repo, "a.py", "original\n")
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))

        scope.relocate_touched_path("mine", "a.py", "b.py", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "a.py" in offer["safe_paths"]
        assert "b.py" in offer["safe_paths"]

    def test_raw_shutil_move_strands_the_new_path_unclaimed(self, tmp_path):
        """CHARACTERIZATION, NOT PERTURBATION: same tracked-file-A-claimed
        setup, but the relocation is a raw shutil.move rather than the
        helper. Pre-existing stranded shape: A stays in safe_paths (its own
        T-claim never expires just because the file is gone), B is EXCLUDED
        with reason "untouched by this session" because nothing ever wrote a
        claim for the new path. This test is GREEN BOTH BEFORE AND AFTER C1
        lands -- C1 does not change what a raw shutil.move does, it only
        gives callers an alternative that re-declares the claim. This is a
        characterization of pre-existing behaviour, not a red-before/
        green-after proof."""
        repo = _make_repo(tmp_path)
        _add_and_commit_tracked(repo, "a.py", "original\n")
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))

        shutil.move(str(Path(repo) / "a.py"), str(Path(repo) / "b.py"))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "a.py" in offer["safe_paths"]
        assert "b.py" not in offer["safe_paths"]
        assert {"path": "b.py", "reason": "untouched by this session"} in offer[
            "excluded"
        ]

    def test_unclaimed_source_writes_no_claim_event(self, tmp_path):
        """The conditional-claim precondition: relocating a path this
        session never touch()-claimed performs the move but writes NO claim
        event at all -- fabricating a T for the destination would be an
        over-claim on a file this session may never have written."""
        repo = _make_repo(tmp_path)
        (Path(repo) / "c.py").write_text("untracked\n")
        core.init("mine", cwd=str(repo))

        scope.relocate_touched_path("mine", "c.py", "d.py", cwd=str(repo))

        assert not (Path(repo) / "c.py").exists()
        assert (Path(repo) / "d.py").exists()
        touched_path = Path(core.session_dir("mine", cwd=str(repo))) / "touched.txt"
        if touched_path.exists():
            lines = touched_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                _verb, _ts, path = scope.parse_touch_event(line)
                assert path not in ("c.py", "d.py")

    def test_phantom_live_peer_moves_file_writes_no_claims(self, tmp_path):
        """The phantom-live-peer guard: relocating for a session id with no
        session dir on disk still performs the move (the file operation is
        unconditional) but writes no claim events anywhere, and does not
        materialize a session dir for the ghost id as a side effect."""
        repo = _make_repo(tmp_path)
        (Path(repo) / "e.py").write_text("ghost\n")

        scope.relocate_touched_path("ghost", "e.py", "f.py", cwd=str(repo))

        assert not (Path(repo) / "e.py").exists()
        assert (Path(repo) / "f.py").exists()
        assert not os.path.isdir(core.session_dir("ghost", cwd=str(repo)))

    def test_dst_pre_exists_moves_but_writes_no_claim(self, tmp_path):
        """Destination carve-out (a): dst_rel already exists on disk before
        the move. The move still happens (shutil.move's overwrite semantics
        are unchanged), but no claim event is written for dst_rel — this
        session did not create the pre-existing destination content.
        (Review: code-reviewer, sidecar coordinatorcode-reviewer-9d2370bf.md
        Finding 2 — this carve-out had no test.)"""
        repo = _make_repo(tmp_path)
        _add_and_commit_tracked(repo, "a.py", "original\n")
        (Path(repo) / "b.py").write_text("pre-existing destination\n")
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))

        scope.relocate_touched_path("mine", "a.py", "b.py", cwd=str(repo))

        assert not (Path(repo) / "a.py").exists()
        assert (Path(repo) / "b.py").read_text() == "original\n"
        touched_path = Path(core.session_dir("mine", cwd=str(repo))) / "touched.txt"
        lines = touched_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            _verb, _ts, path = scope.parse_touch_event(line)
            assert path != "b.py"

    def test_dst_under_dot_archive_moves_but_writes_no_claim(self, tmp_path):
        """Destination carve-out (b): dst_rel resolves under the
        .archive/ anti-scope prefix via _dst_is_claimable. The move still
        happens, but no claim event is written for the destination — .archive
        is not trackable repo content a touched.txt claim can meaningfully
        cover. (Review: code-reviewer, sidecar
        coordinatorcode-reviewer-9d2370bf.md Finding 2 — this carve-out had
        no test.)"""
        repo = _make_repo(tmp_path)
        _add_and_commit_tracked(repo, "a.py", "original\n")
        (Path(repo) / ".archive").mkdir()
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))

        scope.relocate_touched_path(
            "mine", "a.py", ".archive/a.py", cwd=str(repo)
        )

        assert not (Path(repo) / "a.py").exists()
        assert (Path(repo) / ".archive" / "a.py").exists()
        touched_path = Path(core.session_dir("mine", cwd=str(repo))) / "touched.txt"
        lines = touched_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            _verb, _ts, path = scope.parse_touch_event(line)
            assert path != ".archive/a.py"


# ---------------------------------------------------------------------------
# restate_touched_path (scope.py) — the public, move-free single-path
# counterpart to restate_touched_tree, closing the duplication where
# coordinator_core.ops.research_dir_restructure previously carried its own
# private copy of this same logic. Assertions go through
# safe_commit_offer.compute_offer, matching the sibling relocate_touched_path
# class above, plus direct filesystem existence checks proving no move
# occurred (the entire point of this helper vs. relocate_touched_path).
# ---------------------------------------------------------------------------


class TestRestateTouchedPath:
    def test_claimed_source_restated_onto_destination_source_stays_claimed(
        self, tmp_path
    ):
        """THE LOAD-BEARING CASE: a tracked file A this session T-claimed is
        restated onto B via restate_touched_path (no move performed by the
        caller). Both A (the original claim, left standing for
        release_committed_claims to retire later) and B (the newly-claimed
        destination) land in compute_offer's safe_paths."""
        repo = _make_repo(tmp_path)
        _add_and_commit_tracked(repo, "a.py", "original\n")
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))

        scope.restate_touched_path("mine", "a.py", "b.py", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "a.py" in offer["safe_paths"]
        assert "b.py" in offer["safe_paths"]

    def test_unclaimed_source_writes_no_claim_event(self, tmp_path):
        """Restating an unclaimed source writes no claim event at all --
        fabricating a T for the destination would be an over-claim on a
        file this session may never have written."""
        repo = _make_repo(tmp_path)
        (Path(repo) / "c.py").write_text("untracked\n")
        core.init("mine", cwd=str(repo))

        scope.restate_touched_path("mine", "c.py", "d.py", cwd=str(repo))

        touched_path = Path(core.session_dir("mine", cwd=str(repo))) / "touched.txt"
        if touched_path.exists():
            lines = touched_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                _verb, _ts, path = scope.parse_touch_event(line)
                assert path not in ("c.py", "d.py")

    def test_phantom_live_peer_writes_no_claims(self, tmp_path):
        """The phantom-live-peer guard: restating for a session id with no
        session dir on disk writes no claim events anywhere and does not
        materialize a session dir for the ghost id as a side effect."""
        repo = _make_repo(tmp_path)
        (Path(repo) / "e.py").write_text("ghost\n")

        scope.restate_touched_path("ghost", "e.py", "f.py", cwd=str(repo))

        assert not os.path.isdir(core.session_dir("ghost", cwd=str(repo)))

    def test_performs_no_filesystem_move(self, tmp_path):
        """restate_touched_path is move-free by design: the source file
        stays exactly where it is and no destination file is created --
        unlike relocate_touched_path, which always performs a shutil.move.
        """
        repo = _make_repo(tmp_path)
        _add_and_commit_tracked(repo, "a.py", "original\n")
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))

        scope.restate_touched_path("mine", "a.py", "b.py", cwd=str(repo))

        assert (Path(repo) / "a.py").exists()
        assert not (Path(repo) / "b.py").exists()


# ---------------------------------------------------------------------------
# relocate_touched_path — directory-aware widening (state/bug-backlog/
# 2026-08-06-relocating-a-directory-strands-touch-cla-3878d0fc0ca0.yaml).
# A single-path claim restatement cannot reach a claim on a path NESTED
# under the moved directory -- these cases exercise the walking branch that
# closes that gap. Case (1) is the load-bearing one and is proved through
# safe_commit_offer.compute_offer, matching the sibling single-path class
# above; (2)/(3) additionally inspect touched.txt directly where the
# assertion under test IS an absence of writes, which compute_offer alone
# cannot observe as cleanly.
# ---------------------------------------------------------------------------


class TestRelocateTouchedPathDirectory:
    def test_nested_claimed_file_ends_up_claimed_at_new_path_both_halves(
        self, tmp_path
    ):
        """A file two levels under a moved directory, T-claimed by this
        session, is claimed at its post-move path AND its pre-move path
        stays claimed -- both halves of the rename, proving the walk
        restates a claim rather than merely tolerating the move. The
        directory itself was never itself a touched.txt entry (touch-claims
        are keyed on files only); only the nested descendant's claim
        matters here."""
        repo = _make_repo(tmp_path)
        (Path(repo) / "dir" / "sub").mkdir(parents=True)
        _add_and_commit_tracked(repo, "dir/sub/a.py", "original\n")
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "dir/sub/a.py", cwd=str(repo))

        scope.relocate_touched_path("mine", "dir", "dir2", cwd=str(repo))

        assert not (Path(repo) / "dir").exists()
        assert (Path(repo) / "dir2" / "sub" / "a.py").exists()

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "dir/sub/a.py" in offer["safe_paths"]
        assert "dir2/sub/a.py" in offer["safe_paths"]

    def test_unclaimed_sibling_in_same_tree_gets_no_claim(self, tmp_path):
        """A directory holding one claimed file and one unclaimed sibling:
        only the claimed file's new path is restated -- the sibling's new
        path must never be fabricated into a claim just because it shared a
        moved parent directory with a genuinely-claimed file (an over-claim
        here is how one session's wrap sweeps a peer's — or its own
        never-written — file into its commit)."""
        repo = _make_repo(tmp_path)
        (Path(repo) / "dir").mkdir()
        _add_and_commit_tracked(repo, "dir/a.py", "claimed\n")
        (Path(repo) / "dir" / "b.py").write_text("never touched\n")
        subprocess.run(["git", "add", "dir/b.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add dir/b.py"], cwd=repo, check=True
        )
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "dir/a.py", cwd=str(repo))

        scope.relocate_touched_path("mine", "dir", "dir2", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "dir2/a.py" in offer["safe_paths"]
        assert "dir2/b.py" not in offer["safe_paths"]

    def test_directory_with_no_claimed_files_writes_no_events(self, tmp_path):
        """A directory containing no currently-claimed file at all: the move
        happens, but touched.txt gains no new entry for anything under the
        destination -- the walk finds nothing to restate and must not write
        an empty/no-op event just because a directory move occurred."""
        repo = _make_repo(tmp_path)
        (Path(repo) / "dir").mkdir()
        (Path(repo) / "dir" / "a.py").write_text("never touched\n")
        subprocess.run(["git", "add", "dir/a.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add dir/a.py"], cwd=repo, check=True
        )
        core.init("mine", cwd=str(repo))

        touched_path = Path(core.session_dir("mine", cwd=str(repo))) / "touched.txt"
        before = touched_path.read_text(encoding="utf-8") if touched_path.exists() else ""

        scope.relocate_touched_path("mine", "dir", "dir2", cwd=str(repo))

        assert not (Path(repo) / "dir").exists()
        assert (Path(repo) / "dir2" / "a.py").exists()
        after = touched_path.read_text(encoding="utf-8") if touched_path.exists() else ""
        assert after == before


# ---------------------------------------------------------------------------
# release_phantom_claims (scope.py) -- state/bug-backlog/2026-08-06-a-phantom
# -touch-claim-from-an-interrupte-c21f5bbdd077.yaml. The discriminator under
# test is "absent from disk AND not tracked at HEAD" (release) vs "absent
# from disk AND tracked at HEAD" (retain -- a real, git-representable
# deletion). Assertions go through safe_commit_offer.compute_offer, matching
# the sibling relocate_touched_path classes above -- the point under test is
# user-visible commit-offer membership, not touched.txt byte shape.
# ---------------------------------------------------------------------------


class TestReleasePhantomClaims:
    def test_phantom_claim_on_never_created_path_is_released(self, tmp_path):
        """THE LOAD-BEARING CASE: a T-claim on a path that was NEVER created
        on disk (the exact residue of a crash between relocate_touched_path's
        T(dst) append and its shutil.move) -- not on disk, not tracked at
        HEAD. release_phantom_claims must release it, and it must then be
        ABSENT from compute_offer's safe_paths -- it never re-surfaces."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "ghost.py", cwd=str(repo))
        assert not (Path(repo) / "ghost.py").exists()

        scope.release_phantom_claims("mine", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "ghost.py" not in offer["safe_paths"]

    def test_tracked_file_deleted_stays_claimed_as_a_deletion(self, tmp_path):
        """THE HAZARD CASE: a claimed file this session legitimately deleted
        is also absent from disk -- but it IS tracked at HEAD, so there is a
        real git-representable deletion for a future commit to capture.
        release_phantom_claims must NOT release it; it must stay in
        compute_offer's safe_paths as the pending deletion it is."""
        repo = _make_repo(tmp_path)
        _add_and_commit_tracked(repo, "a.py", "original\n")
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "a.py", cwd=str(repo))
        os.remove(str(Path(repo) / "a.py"))

        scope.release_phantom_claims("mine", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "a.py" in offer["safe_paths"]

    def test_untracked_file_deleted_is_released_same_as_a_phantom(self, tmp_path):
        """The collapse case named in the bug-backlog entry: a claimed file
        that existed only as an UNTRACKED write, now deleted, is byte-
        indistinguishable from a genuine phantom (absent from disk, absent
        from HEAD) -- there was never a `git rm` for its removal to
        represent, so nothing is lost by releasing it exactly like a
        phantom. Documents the designed behavior for this shape."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (Path(repo) / "untracked.py").write_text("scratch\n")
        scope.touch("mine", "untracked.py", cwd=str(repo))
        os.remove(str(Path(repo) / "untracked.py"))

        scope.release_phantom_claims("mine", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "untracked.py" not in offer["safe_paths"]

    def test_claimed_file_present_on_disk_is_left_alone(self, tmp_path):
        """A claimed file that IS on disk -- dirty or clean -- is never this
        function's concern; it is untouched regardless of tracked state."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (Path(repo) / "present.py").write_text("still here\n")
        scope.touch("mine", "present.py", cwd=str(repo))

        scope.release_phantom_claims("mine", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "present.py" in offer["safe_paths"]

    def test_staged_then_deleted_path_stays_claimed(self, tmp_path):
        """Finding-1 regression guard (review sidecar
        coordinatorcode-reviewer-5e45cd5a.md): a path this session `git
        add`-ed (staged, never committed) and then deleted from disk --
        NOT tracked at HEAD, but staged in the index. release_phantom_claims
        must NOT release it: the index still holds real content a plain
        `git commit` will land regardless of whether this claim survives."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        staged = Path(repo) / "staged.py"
        staged.write_text("new content\n")
        subprocess.run(["git", "add", "staged.py"], cwd=repo, check=True)
        scope.touch("mine", "staged.py", cwd=str(repo))
        os.remove(str(staged))

        scope.release_phantom_claims("mine", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "staged.py" in offer["safe_paths"]

    def test_pathspec_magic_filename_classifies_correctly(self, tmp_path):
        """Finding-2 regression guard: a claimed path containing pathspec
        magic (a leading `!`) must still be classified correctly rather
        than misparsed as a git pathspec pattern. Tracked-at-HEAD case: the
        file is committed, then this session claims and deletes it -- it
        must stay claimed as a real deletion, not be swept up by (or
        wrongly excluded from) an unescaped pathspec match."""
        repo = _make_repo(tmp_path)
        _add_and_commit_tracked(repo, "!weird.py", "original\n")
        core.init("mine", cwd=str(repo))
        scope.touch("mine", "!weird.py", cwd=str(repo))
        os.remove(str(Path(repo) / "!weird.py"))

        scope.release_phantom_claims("mine", cwd=str(repo))

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert "!weird.py" in offer["safe_paths"]

    def test_no_touched_claims_is_a_silent_no_op(self, tmp_path):
        """A session with no `T`-claimed paths at all (freshly init()-ed,
        never touch()-ed anything) -- returns without raising and without
        writing any event."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        touched_path = Path(core.session_dir("mine", cwd=str(repo))) / "touched.txt"
        before = touched_path.read_text(encoding="utf-8") if touched_path.exists() else ""

        scope.release_phantom_claims("mine", cwd=str(repo))

        after = touched_path.read_text(encoding="utf-8") if touched_path.exists() else ""
        assert after == before

    def test_untracked_phantom_claims_spawn_git_root_once_not_per_entry(
        self, tmp_path, monkeypatch
    ):
        """C2 (docs/plans/2026-08-08-touched-path-normalize-spawn-diet.md):
        ``release_phantom_claims`` already computes ``root =
        core.git_root(cwd)`` once before its N-entry loop over claimed
        paths; before C2, each loop iteration's
        ``normalize_touch_path(raw_path, cwd)`` call re-derived that same
        root itself on every untracked-path miss, so ``core.git_root`` spawn
        count scaled with entry count (1 + N) rather than staying constant.
        With the loop-known ``root`` now threaded through as
        ``normalize_touch_path``'s ``root=`` parameter, ``core.git_root`` is
        called exactly once for this call regardless of how many phantom
        entries are claimed -- spawns scale with the number of DISTINCT
        roots (one, here), not with entry count."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        for name in ("ghost1.py", "ghost2.py", "ghost3.py"):
            scope.touch("mine", name, cwd=str(repo))
            assert not (Path(repo) / name).exists()

        calls = {"git_root": 0}
        real_git_root = core.git_root

        def _counted_git_root(cwd=None):
            calls["git_root"] += 1
            return real_git_root(cwd)

        monkeypatch.setattr(scope.core, "git_root", _counted_git_root)

        scope.release_phantom_claims("mine", cwd=str(repo))

        assert calls["git_root"] == 1

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        for name in ("ghost1.py", "ghost2.py", "ghost3.py"):
            assert name not in offer["safe_paths"]
