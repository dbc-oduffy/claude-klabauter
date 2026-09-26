"""
coordinator_core.session.tests.test_claim_relinquishment — C1, C2 AND C3 of
DR-205 (docs/plans/2026-09-26-claim-relinquishment-is-not-liveness-cla.md):
``claim_holder_relinquished``, the marker reader, and the derived-fallback
evaluator in ``coordinator_core.session.liveness`` (C1); the marker WRITER,
``claims.release_or_relinquish_artifact`` (C2); ``claims.take_over_claim``,
the fail-loud takeover verb with the double-read TOCTOU bracket (C3).

``claim_holder_relinquished`` is NOT a liveness source (DR-205 (a), (f)): it
never calls ``session_live``, ``claim_holder_live``, ``core.stable_pid_alive``,
or ``core.pid_alive``. ``TestNeverTouchesLiveness`` pins that by monkeypatching
all four to raise.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claims, core, liveness
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID_A = "11111111-1111-1111-1111-111111111111"
_SID_STALE = "22222222-2222-2222-2222-222222222222"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _make_claim_dir(repo: Path, class_: str, basename: str, session_id: str) -> Path:
    sessions_dir = Path(core.sessions_dir(str(repo)))
    cdir = sessions_dir / f"{class_}-claims" / basename
    cdir.mkdir(parents=True)
    (cdir / "session_id").write_text(session_id, encoding="utf-8")
    (cdir / "pid").write_text("999999", encoding="utf-8")
    return cdir


def _write_handoff(
    repo: Path,
    relpath: str,
    *,
    authoring_session: str,
    governing_plan: str,
) -> Path:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"authoring_session: {authoring_session}\n"
        f"governing_plan: {governing_plan}\n"
        "---\n\n# handoff\n",
        encoding="utf-8",
    )
    return path


def _commit(repo: Path, relpath: str) -> None:
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", f"add {relpath}")


class TestMarker:
    def test_marker_hit(self, tmp_path):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        (cdir / "relinquished.json").write_text(
            json.dumps({"session_id": _SID_A, "at": "2026-09-26T00:00:00Z"}),
            encoding="utf-8",
        )
        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is not None
        assert evidence.kind == "marker"
        assert evidence.holder_sid == _SID_A
        assert evidence.at == "2026-09-26T00:00:00Z"

    def test_marker_stale_sid_ignored(self, tmp_path):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        (cdir / "relinquished.json").write_text(
            json.dumps({"session_id": _SID_STALE, "at": "2026-09-26T00:00:00Z"}),
            encoding="utf-8",
        )
        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is None

    def test_pid_only_dir_is_none(self, tmp_path):
        repo = _init_repo(tmp_path)
        sessions_dir = Path(core.sessions_dir(str(repo)))
        cdir = sessions_dir / "plan-claims" / "legacy-plan"
        cdir.mkdir(parents=True)
        (cdir / "pid").write_text("123", encoding="utf-8")
        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is None

    def test_zero_git_spawns_on_marker_path(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        (cdir / "relinquished.json").write_text(
            json.dumps({"session_id": _SID_A}), encoding="utf-8"
        )

        def _explode(*args, **kwargs):
            raise AssertionError("subprocess.run must not be called on the marker path")

        monkeypatch.setattr(subprocess, "run", _explode)
        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is not None
        assert evidence.kind == "marker"


class TestDerivedFallback:
    def test_derived_hit(self, tmp_path):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        _write_handoff(
            repo,
            "state/handoffs/2026-09-26-a.md",
            authoring_session=_SID_A,
            governing_plan="docs/plans/my-plan.md",
        )
        _commit(repo, "state/handoffs/2026-09-26-a.md")

        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is not None
        assert evidence.kind == "derived"
        assert evidence.holder_sid == _SID_A
        assert evidence.handoff == "state/handoffs/2026-09-26-a.md"

    def test_derived_miss_on_uncommitted_handoff(self, tmp_path):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        _write_handoff(
            repo,
            "state/handoffs/2026-09-26-a.md",
            authoring_session=_SID_A,
            governing_plan="docs/plans/my-plan.md",
        )
        # deliberately not committed

        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is None

    def test_derived_miss_on_governing_plan_mismatch(self, tmp_path):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        _write_handoff(
            repo,
            "state/handoffs/2026-09-26-a.md",
            authoring_session=_SID_A,
            governing_plan="docs/plans/some-other-plan.md",
        )
        _commit(repo, "state/handoffs/2026-09-26-a.md")

        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is None

    def test_derived_never_attempted_for_non_plan_class(self, tmp_path):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "handoff", "my-plan", _SID_A)
        _write_handoff(
            repo,
            "state/handoffs/2026-09-26-a.md",
            authoring_session=_SID_A,
            governing_plan="docs/plans/my-plan.md",
        )
        _commit(repo, "state/handoffs/2026-09-26-a.md")

        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is None

    def test_scan_error_is_none(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)

        def _fake_collect(worktree_root):
            return [], ["state/handoffs: boom"]

        monkeypatch.setattr(
            "coordinator_core.ops.handoff_children._collect_handoff_paths",
            _fake_collect,
        )
        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is None


class TestNeverTouchesLiveness:
    def test_liveness_predicates_never_called(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        _write_handoff(
            repo,
            "state/handoffs/2026-09-26-a.md",
            authoring_session=_SID_A,
            governing_plan="docs/plans/my-plan.md",
        )
        _commit(repo, "state/handoffs/2026-09-26-a.md")

        def _boom(*args, **kwargs):
            raise AssertionError("must not be called")

        monkeypatch.setattr(liveness, "session_live", _boom)
        monkeypatch.setattr(liveness, "claim_holder_live", _boom)
        monkeypatch.setattr(core, "stable_pid_alive", _boom)
        monkeypatch.setattr(core, "pid_alive", _boom)

        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is not None
        assert evidence.kind == "derived"


class TestReleaseOrRelinquishArtifact:
    """C2: ``claims.release_or_relinquish_artifact`` calls ``release_artifact``
    unchanged, then writes ``relinquished.json`` only for the env-precedence
    mismatch D1 names -- a plan claim ``release_artifact`` left in place
    (its holder-identity check did not match) whose recorded ``session_id``
    is nonetheless one of THIS process's own identity tiers
    (``core.session_env_candidates()``)."""

    def test_marker_written_on_env_tier_mismatch(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)

        # COORDINATOR_SESSION_ID outranks CLAUDE_CODE_SESSION_ID in
        # core.SESSION_ENV_PRECEDENCE, so `release_artifact`'s own
        # `resolve_session_id` resolves to the top-tier id and does not
        # match the claim's recorded holder (_SID_A) -- `claim_held_by_me`
        # no-ops and the claim survives. Both vars are nonetheless THIS
        # process's own env, so `session_env_candidates()` sees both.
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "top-tier-sid")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID_A)

        result = claims.release_or_relinquish_artifact(
            "plan", "my-plan", baton_repo_root=str(repo)
        )

        assert result is True
        assert cdir.is_dir(), "env-tier mismatch must leave the claim in place"
        marker = json.loads((cdir / "relinquished.json").read_text(encoding="utf-8"))
        assert marker["session_id"] == _SID_A
        assert "at" in marker

    def test_no_marker_for_a_foreign_holder(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_STALE)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID_A)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        result = claims.release_or_relinquish_artifact(
            "plan", "my-plan", baton_repo_root=str(repo)
        )

        assert result is True
        assert cdir.is_dir()
        assert not (cdir / "relinquished.json").exists()

    def test_no_marker_under_warm_served_without_identity(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "top-tier-sid")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID_A)

        with core.warm_served_request(True):
            result = claims.release_or_relinquish_artifact(
                "plan", "my-plan", baton_repo_root=str(repo)
            )

        assert result is True
        assert cdir.is_dir()
        assert not (cdir / "relinquished.json").exists()

    def test_normal_release_still_removes_dir_and_writes_no_marker(
        self, tmp_path, monkeypatch
    ):
        repo = _init_repo(tmp_path)
        monkeypatch.chdir(repo)
        monkeypatch.setenv("CLAUDE_SESSION_ID", _SID_A)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        assert claims.claim_artifact("plan", "my-plan", baton_repo_root=str(repo)) is True
        cdir = claims.claim_dir_for("plan", "my-plan", baton_repo_root=str(repo))
        assert cdir is not None and cdir.is_dir()

        result = claims.release_or_relinquish_artifact(
            "plan", "my-plan", baton_repo_root=str(repo)
        )

        assert result is True
        assert not cdir.is_dir(), "the holder's own release must still remove the claim dir"


class TestWriteClaimMetaTakeover:
    """The C3 addition to ``_write_claim_meta``: ``takeover=None`` (the
    default) writes exactly the four pre-existing files, byte-for-byte;
    ``takeover=<dict>`` additionally writes the three one-field-per-file
    takeover records, each readable through ``_read_claim_field``."""

    def test_no_takeover_keyword_writes_exactly_four_files(self, tmp_path):
        cdir = tmp_path / "cdir"
        cdir.mkdir()
        claims._write_claim_meta(cdir, _SID_A)
        assert sorted(p.name for p in cdir.iterdir()) == [
            "claimed_at",
            "pid",
            "session_id",
            "stage",
        ]

    def test_takeover_keyword_writes_justification_and_prior_sid(self, tmp_path):
        cdir = tmp_path / "cdir"
        cdir.mkdir()
        claims._write_claim_meta(
            cdir,
            _SID_A,
            takeover={
                "taken_from_session_id": _SID_STALE,
                "takeover_evidence": "marker relinquished.json",
                "justification": "handed off in the incident channel",
            },
        )
        assert claims._read_claim_field(cdir, "taken_from_session_id") == _SID_STALE
        assert claims._read_claim_field(cdir, "justification") == (
            "handed off in the incident channel"
        )
        assert claims._read_claim_field(cdir, "takeover_evidence") == (
            "marker relinquished.json"
        )


class TestTakeOverClaim:
    """C3: ``claims.take_over_claim`` -- the fail-loud takeover verb, plan
    class only, with a double-read TOCTOU bracket (D3)."""

    def test_take_over_refuses_live_holder_without_evidence(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        _make_claim_dir(repo, "plan", "my-plan", _SID_STALE)
        monkeypatch.setattr(liveness, "claim_holder_live", lambda *a, **k: True)

        ok = claims.take_over_claim(
            "my-plan", "handed off", baton_repo_root=str(repo)
        )
        assert ok is False

    def test_take_over_refuses_pid_only_not_live(self, tmp_path):
        repo = _init_repo(tmp_path)
        sessions_dir = Path(core.sessions_dir(str(repo)))
        cdir = sessions_dir / "plan-claims" / "legacy-plan"
        cdir.mkdir(parents=True)
        (cdir / "pid").write_text("999999", encoding="utf-8")

        ok = claims.take_over_claim(
            "legacy-plan", "handed off", baton_repo_root=str(repo)
        )
        assert ok is False

    def test_take_over_marker_grants_and_records_justification(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        (cdir / "relinquished.json").write_text(
            json.dumps({"session_id": _SID_A, "at": "2026-09-26T00:00:00Z"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", _SID_STALE)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        ok = claims.take_over_claim(
            "my-plan", "handed off in the incident channel", baton_repo_root=str(repo)
        )
        assert ok is True
        assert claims._read_claim_field(cdir, "session_id") == _SID_STALE
        assert claims._read_claim_field(cdir, "taken_from_session_id") == _SID_A
        assert claims._read_claim_field(cdir, "justification") == (
            "handed off in the incident channel"
        )
        assert claims._read_claim_field(cdir, "takeover_evidence").startswith("marker")

    def test_take_over_derived_fallback_holder_crashed_after_handoff_commit(
        self, tmp_path, monkeypatch
    ):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        _write_handoff(
            repo,
            "state/handoffs/2026-09-26-a.md",
            authoring_session=_SID_A,
            governing_plan="docs/plans/my-plan.md",
        )
        _commit(repo, "state/handoffs/2026-09-26-a.md")
        # The holder is still reported live -- the takeover must grant on the
        # derived evidence regardless (DR-205 (d): liveness never decides a
        # grant, only the refusal's wording).
        monkeypatch.setattr(liveness, "claim_holder_live", lambda *a, **k: True)
        monkeypatch.setenv("CLAUDE_SESSION_ID", _SID_STALE)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        ok = claims.take_over_claim(
            "my-plan", "holder crashed after committing its handoff", baton_repo_root=str(repo)
        )
        assert ok is True
        assert claims._read_claim_field(cdir, "session_id") == _SID_STALE
        assert claims._read_claim_field(cdir, "takeover_evidence").startswith("derived")

    def test_take_over_aborts_when_holder_changes_between_reads(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        (cdir / "relinquished.json").write_text(
            json.dumps({"session_id": _SID_A}), encoding="utf-8"
        )

        real = liveness.claim_holder_relinquished

        def _racing(cdir_arg, cwd=None):
            evidence = real(cdir_arg, cwd)
            # Simulate a concurrent takeover landing between Read 1 and
            # Read 2: the claim dir's recorded holder changes underneath us.
            (Path(cdir_arg) / "session_id").write_text(_SID_STALE, encoding="utf-8")
            return evidence

        monkeypatch.setattr(liveness, "claim_holder_relinquished", _racing)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "33333333-3333-3333-3333-333333333333")
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        ok = claims.take_over_claim(
            "my-plan", "handed off", baton_repo_root=str(repo)
        )
        assert ok is False
        # The racer's write is untouched -- this abort must never mutate the
        # claim dir it did not win.
        assert claims._read_claim_field(cdir, "session_id") == _SID_STALE

    def test_take_over_aborts_when_marker_removed_between_reads(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        cdir = _make_claim_dir(repo, "plan", "my-plan", _SID_A)
        (cdir / "relinquished.json").write_text(
            json.dumps({"session_id": _SID_A}), encoding="utf-8"
        )

        real = liveness.claim_holder_relinquished

        def _vanishing(cdir_arg, cwd=None):
            evidence = real(cdir_arg, cwd)
            (Path(cdir_arg) / "relinquished.json").unlink()
            return evidence

        monkeypatch.setattr(liveness, "claim_holder_relinquished", _vanishing)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "33333333-3333-3333-3333-333333333333")
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        ok = claims.take_over_claim(
            "my-plan", "handed off", baton_repo_root=str(repo)
        )
        assert ok is False
        assert claims._read_claim_field(cdir, "session_id") == _SID_A


class TestDerivedKeyMatchesClaimArtifact:
    def test_plan_basename_matches_claim_artifact(self, tmp_path, monkeypatch):
        """Pins that the plan-subject key (docs/plans/<basename>.md) this
        fallback derives from a claim dir's basename equals the exact
        basename ``claim_artifact`` writes for a real plan claim — so the
        derived fallback's key cannot silently drift from what a plan claim
        is actually keyed on and always-miss."""
        repo = _init_repo(tmp_path)
        monkeypatch.chdir(repo)
        monkeypatch.setenv("CLAUDE_SESSION_ID", _SID_A)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        plan_stem = "2026-09-26-example-plan"
        ok = claims.claim_artifact("plan", plan_stem, baton_repo_root=str(repo))
        assert ok is True

        cdir = claims.claim_dir_for("plan", plan_stem, baton_repo_root=str(repo))
        assert cdir is not None
        assert cdir.is_dir()
        assert cdir.name == plan_stem

        _write_handoff(
            repo,
            "state/handoffs/2026-09-26-b.md",
            authoring_session=_SID_A,
            governing_plan=f"docs/plans/{cdir.name}.md",
        )
        _commit(repo, "state/handoffs/2026-09-26-b.md")

        evidence = liveness.claim_holder_relinquished(str(cdir))
        assert evidence is not None
        assert evidence.kind == "derived"
