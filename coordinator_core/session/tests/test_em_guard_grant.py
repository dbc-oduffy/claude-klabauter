"""
coordinator_core.session.tests.test_em_guard_grant — tests for
coordinator_core.session.em_guard_grant, the EM-exercisable session-scoped
grant for a bounded tier of hard-deny guards (docs/plans/
2026-08-13-em-exercisable-in-band-grant-route.md § C1).

Grant round-trip / validation / liveness tests below are PURE PYTHON — no
git spawn. `core.session_dir` / `core.resolve_session_id` /
`liveness.session_live` are monkeypatched directly onto the module objects
(the same seam-patching pattern `test_block_subagent_commit.py` uses for
identity resolution) rather than spawning a real git repo the way
`test_claude_md_grant.py` does — this file needs no spawn-ratchet
admission as a result.

``TestAC7UnanswerableLegNeverCleared`` is the one exception: pinning AC-7
requires the real ``ceremony.scoped_git_commit`` op end to end (the
`unanswerable` leg is that op's own internal state, not something this
module's mechanism can be asked about in isolation), so that one test
spawns a real git repo and is individually marked
``@pytest.mark.spawns_process`` per
``coordinator_core/tests/test_no_new_spawning_tests.py`` Rule 2 (a
per-function marker satisfies the ratchet without a module-wide
``pytestmark``, keeping the rest of this file spawn-free).

Two mandatory test conventions (§ Verification, both from
``test_scoped_git_commit_ownership.py``'s DR-260 block): (1) never a
literal session id for anything that touches ``guard_unlock_sentinel`` —
``sentinel_path()`` resolves under the real, shared platform temp dir, and
this box runs 50-70 concurrent sessions, so ``_unique_sid(prefix)`` mints
``f"{prefix}-{uuid.uuid4().hex[:12]}"``; (2) every sentinel this file mints
is removed in a ``finally:``, best-effort, swallowing ``OSError``.

Spec backlink: docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md § C1
Precedent: coordinator_core/session/tests/test_claude_md_grant.py
Precedent: coordinator_core/ops/ceremony/tests/test_scoped_git_commit_ownership.py
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_subagent_commit
from coordinator_core.bash_guards import dispatch as bash_dispatch
from coordinator_core.session import core
from coordinator_core.session import em_guard_grant as eg
from coordinator_core.session import guard_unlock_sentinel


def _unique_sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _cleanup_sentinel(session_id: str, guard_name: str) -> None:
    try:
        guard_unlock_sentinel.sentinel_path(session_id, guard_name).unlink()
    except OSError:
        pass


class _FakeSessionSeam:
    """Monkeypatches `core.session_dir` / `core.resolve_session_id` /
    `liveness.session_live` so every test in this class runs against a
    plain `tmp_path` directory tree -- no real git repo, no subprocess
    spawn. Sessions default to LIVE; `kill(sid)` flips one to dead."""

    def __init__(self, monkeypatch, tmp_path: Path, default_sid: str):
        self._root = tmp_path
        self._default_sid = default_sid
        self._dead = set()
        monkeypatch.setattr(eg.core, "session_dir", self._session_dir)
        monkeypatch.setattr(eg.core, "resolve_session_id", self._resolve_sid)
        monkeypatch.setattr(eg.liveness, "session_live", self._session_live)

    def _session_dir(self, sid, cwd=None):
        d = self._root / "sessions" / sid
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _resolve_sid(self, cwd=None):
        return self._default_sid

    def _session_live(self, sid, cwd=None):
        return sid not in self._dead

    def kill(self, sid: str) -> None:
        self._dead.add(sid)


# ---------------------------------------------------------------------------
# write_em_guard_grant -- allowlist validation, verbatim reason, ordering
# ---------------------------------------------------------------------------


class TestWriteEmGuardGrantValidation:
    def test_name_outside_allowlist_raises_using_the_real_withheld_guard(
        self, tmp_path, monkeypatch
    ):
        """scoped_git_commit_claim_conflict is a REAL, analysed guard name
        deliberately withheld from wave-1 -- it must be rejected exactly
        like a nonsense string, never silently no-op'd."""
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        with pytest.raises(ValueError):
            eg.write_em_guard_grant(
                "scoped_git_commit_claim_conflict", "let me past", session_id="s1"
            )

    def test_empty_reason_raises(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        with pytest.raises(ValueError):
            eg.write_em_guard_grant("bump-foreign-repo-write", "", session_id="s1")

    def test_whitespace_only_reason_raises(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        with pytest.raises(ValueError):
            eg.write_em_guard_grant("bump-foreign-repo-write", "   \n\t  ", session_id="s1")

    def test_unresolvable_session_returns_false_not_raise(self, tmp_path):
        ok = eg.write_em_guard_grant(
            "bump-foreign-repo-write",
            "ask",
            session_id="s1",
            cwd=str(tmp_path / "not-a-repo"),
        )
        assert ok is False


class TestWriteEmGuardGrantRecord:
    def test_reason_stored_verbatim(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        weird = "  crossing into ../sibling-repo for a joint fix\ttab\nnewline  "
        ok = eg.write_em_guard_grant("bump-foreign-repo-write", weird, session_id="s1")
        assert ok is True
        record = eg.read_em_guard_grant(session_id="s1")
        assert record["reason"] == weird
        assert record["guard_name"] == "bump-foreign-repo-write"
        assert record["granted_by"] == "em"
        assert record["session_id"] == "s1"
        assert "granted_at" in record

    def test_atomic_write_no_temp_file_left_behind(self, tmp_path, monkeypatch):
        seam = _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        eg.write_em_guard_grant("bump-foreign-repo-write", "ask", session_id="s1")
        sdir = Path(seam._session_dir("s1"))
        leftovers = [p for p in sdir.iterdir() if p.name.startswith(eg._GRANT_FILENAME + ".")]
        assert leftovers == []

    def test_record_written_before_sentinel(self, tmp_path, monkeypatch):
        """The record must exist on disk before the sentinel is minted --
        pin the ORDER directly by asserting the record is readable the
        instant the sentinel first appears (a crash between the two must
        never leave a sentinel with no record)."""
        seam = _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        guard_name = "bump-foreign-repo-write"
        sentinel = guard_unlock_sentinel.sentinel_path("s1", guard_name)
        observed = {}

        real_touch = Path.touch

        def _tracking_touch(self, *a, **k):
            if self == sentinel:
                observed["record_present_at_sentinel_mint"] = (
                    eg.read_em_guard_grant(session_id="s1") is not None
                )
            return real_touch(self, *a, **k)

        monkeypatch.setattr(Path, "touch", _tracking_touch)
        try:
            ok = eg.write_em_guard_grant("bump-foreign-repo-write", "ask", session_id="s1")
            assert ok is True
            assert observed["record_present_at_sentinel_mint"] is True
        finally:
            _cleanup_sentinel("s1", guard_name)
        del seam

    def test_sentinel_lands_exactly_where_sentinel_path_computes(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        guard_name = "bump-outside-repo-write"
        expected = guard_unlock_sentinel.sentinel_path("s1", guard_name)
        try:
            eg.write_em_guard_grant(guard_name, "ask", session_id="s1")
            assert expected.is_file()
        finally:
            _cleanup_sentinel("s1", guard_name)


# ---------------------------------------------------------------------------
# read_em_guard_grant / check_em_guard_grant -- liveness, no-glob, round trip
# ---------------------------------------------------------------------------


class TestReadCheckEmGuardGrant:
    def test_round_trip_grant_then_check_true(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        guard_name = "bump-foreign-repo-write"
        try:
            eg.write_em_guard_grant(guard_name, "ask", session_id="s1")
            granted, record = eg.check_em_guard_grant(guard_name, session_id="s1")
            assert granted is True
            assert record["guard_name"] == guard_name
        finally:
            _cleanup_sentinel("s1", guard_name)

    def test_check_wrong_guard_name_reads_ungranted(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        try:
            eg.write_em_guard_grant("bump-foreign-repo-write", "ask", session_id="s1")
            granted, record = eg.check_em_guard_grant("bump-outside-repo-write", session_id="s1")
            assert granted is False
            assert record is not None
        finally:
            _cleanup_sentinel("s1", "bump-foreign-repo-write")

    def test_dead_session_grant_reads_ungranted(self, tmp_path, monkeypatch):
        seam = _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        guard_name = "bump-foreign-repo-write"
        try:
            eg.write_em_guard_grant(guard_name, "ask", session_id="s1")
            seam.kill("s1")
            granted, record = eg.check_em_guard_grant(guard_name, session_id="s1")
            assert granted is False
            assert record is not None
        finally:
            _cleanup_sentinel("s1", guard_name)

    def test_sibling_session_grant_does_not_authorize_caller_no_glob(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        guard_name = "bump-foreign-repo-write"
        try:
            eg.write_em_guard_grant(guard_name, "sibling's own ask", session_id="s-sibling")
            granted, record = eg.check_em_guard_grant(guard_name, session_id="s-caller")
            assert granted is False
            assert record is None
        finally:
            _cleanup_sentinel("s-sibling", guard_name)

    def test_absent_file_reads_ungranted(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        granted, record = eg.check_em_guard_grant("bump-foreign-repo-write", session_id="s1")
        assert granted is False
        assert record is None

    def test_malformed_json_reads_ungranted(self, tmp_path, monkeypatch):
        seam = _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        gfile = Path(seam._session_dir("s1")) / eg._GRANT_FILENAME
        gfile.write_text("{not valid json", encoding="utf-8")
        granted, record = eg.check_em_guard_grant("bump-foreign-repo-write", session_id="s1")
        assert granted is False
        assert record is None

    def test_unknown_granted_by_reads_ungranted(self, tmp_path, monkeypatch):
        seam = _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        gfile = Path(seam._session_dir("s1")) / eg._GRANT_FILENAME
        gfile.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "s1",
                    "granted_by": "subagent",
                    "granted_at": core.now_iso(),
                    "guard_name": "bump-foreign-repo-write",
                    "reason": "ask",
                }
            ),
            encoding="utf-8",
        )
        granted, record = eg.check_em_guard_grant("bump-foreign-repo-write", session_id="s1")
        assert granted is False
        assert record is not None


class TestConsumeRoundTripOneShot:
    """AC-2: the mechanism this module writes into is one-shot -- true True
    once, False on a second call -- pinned directly against
    `guard_unlock_sentinel.consume`, the exact primitive that clears the
    write this grant authorizes."""

    def test_consume_returns_true_once_then_false(self, tmp_path, monkeypatch):
        _FakeSessionSeam(monkeypatch, tmp_path, "s1")
        guard_name = "bump-foreign-repo-write"
        try:
            eg.write_em_guard_grant(guard_name, "ask", session_id="s1")
            assert guard_unlock_sentinel.consume("s1", guard_name) is True
            assert guard_unlock_sentinel.consume("s1", guard_name) is False
        finally:
            _cleanup_sentinel("s1", guard_name)


# ---------------------------------------------------------------------------
# Subset invariant: every _GRANTABLE_GUARDS member is actually consumable
# ---------------------------------------------------------------------------


class TestGrantableGuardsSubsetInvariant:
    """A dead allowlist entry reads as an affordance and grants nothing --
    every member of `_GRANTABLE_GUARDS` must actually be sentinel-eligible:
    `fail_closed=True` (unconditionally eligible), or an explicit member of
    `bash_guards.dispatch._SENTINEL_ELIGIBLE_ADVISORY_GUARDS`."""

    def test_two_members_not_three(self):
        assert eg._GRANTABLE_GUARDS == {
            "bump-foreign-repo-write",
            "bump-outside-repo-write",
        }
        assert "scoped_git_commit_claim_conflict" not in eg._GRANTABLE_GUARDS

    def test_every_grantable_guard_is_sentinel_eligible(self):
        chain = bash_dispatch._build_guard_chain(
            cmd="echo em-guard-grant-subset-invariant-probe",
            session_id="em-guard-grant-subset-invariant-probe",
            cwd="/tmp",
            payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
            policy_file=None,
            host_is_windows=None,
        )
        chain_by_name = {entry.name: entry for entry in chain}
        for name in eg._GRANTABLE_GUARDS:
            entry = chain_by_name.get(name)
            assert entry is not None, f"{name} is not a registered bash guard"
            eligible = entry.fail_closed or name in bash_dispatch._SENTINEL_ELIGIBLE_ADVISORY_GUARDS
            assert eligible, f"{name} is in _GRANTABLE_GUARDS but not sentinel-eligible"


# ---------------------------------------------------------------------------
# AC-14: a sentinel for scoped_git_commit_claim_conflict never composes
# with a subagent commit -- direct-sentinel technique (minted via
# guard_unlock_sentinel, bypassing the grant CLI's allowlist entirely,
# since this guard is not a wave-1 member).
# ---------------------------------------------------------------------------


def _commit_payload(agent_id="deadbeef0123", session_id="sess1"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'x'"},
        "session_id": session_id,
        "cwd": None,
        "agent_id": agent_id,
    }


class TestAC14SubagentCommitNeverComposesWithGrant:
    def test_live_sentinel_for_claim_conflict_does_not_unblock_subagent_commit(
        self, monkeypatch
    ):
        sid = _unique_sid("sess-subagent")
        guard_name = "scoped_git_commit_claim_conflict"
        sentinel = guard_unlock_sentinel.sentinel_path(sid, guard_name)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
        monkeypatch.setattr(
            block_subagent_commit, "_resolve_subagent_identity", lambda raw, session: raw
        )
        try:
            result = block_subagent_commit.check(_commit_payload(session_id=sid))
            assert result is not None
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        finally:
            _cleanup_sentinel(sid, guard_name)


# AC-7 WITHDRAWN, 2026-08-13 — its test lived here and was removed with it.
# It pinned "a grant never clears `_check_claim_conflicts`'s `unanswerable`
# leg". That function was DELETED outright by
# `docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-that-rejects-it.md`
# (PM-authorized): a path-touch claim is a swimlane courtesy, not a safety
# mechanism, so the whole hard-deny goes rather than being narrowed. There is no
# longer an `unanswerable` leg for a grant to clear, so the invariant is moot by
# construction — the same reasoning that withdrew this plan's C10. Removed rather
# than left xfailing: a permanently-xfailing test that also spawns would need
# spawn-ratchet admission to pin behaviour that no longer exists.
#
# AC-14 is NOT affected and its test remains below/above: that one pins
# `block_subagent_commit.py` refusing a subagent commit even with a live
# sentinel, which is independent of the deleted gate.

