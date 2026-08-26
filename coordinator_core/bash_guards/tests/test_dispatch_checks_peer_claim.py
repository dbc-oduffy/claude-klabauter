"""Adversarial parity tests for ``_rm_peer_claim_of`` (coordinator_core.
bash_guards.dispatch_checks) — the Q24 liveness-ordering fix.

Oracle: ``_rm_peer_claim_of`` / ``_gr_peer_claim_of`` in DoE-claude's retired
``coordinator/hooks/scripts/block-destructive-rm.sh`` /
``block-destructive-git-revert.sh`` (both deleted 2026-07-16, DoE
``2f8b8450``; identical decision trees; the Python port serves both callers
with one shared function). The bash tree PREFERS
the canonical ``cs_live_session_ids`` liveness source per-sid (a sid is
"covered" by canonical iff it has a ``meta.json``) and only degrades to the
30-minute ``touched.txt`` mtime backstop for sids canonical does not cover.

Incidents this guards against:
  - 2026-07-10: an EM's blanket ``COORDINATOR_ALLOW_RM=1`` deleted a LIVE
    peer session's uncommitted work because the guard could not tell a live
    peer from a stale one — motivated the peer-claim hardening itself.
    TestLivePeerStaleMtimeIsContested reproduces the shape the mtime-only
    port (pre-fix) would have wrongly missed: a peer that IS live (in the
    canonical live-set) but whose touched.txt mtime is > 30 minutes old.
    Mtime-only logic would have skipped it as "stale" and allowed the
    destroy; canonical-first logic still blocks it.
  - 2026-05-28: origin of block-destructive-rm.sh's general uncommitted-work
    guard (a destructive rm must not silently destroy git-unrecoverable
    work). TestDeadSessionRecentMtime reproduces the ungated backstop shape
    this general guard depends on — a session dir canonical does not know
    about (no meta.json) but whose touched.txt is fresh must still be
    treated as contested.

Monkeypatches ``coordinator_core.session.liveness.live_session_ids`` — the
correct seam for testing dispatch_checks' USE of the liveness module, not the
liveness module's own logic (that lives in
coordinator_core/session/tests/test_liveness.py).

Review: code-reviewer (Finding 3) -- ``_rm_peer_claim_of`` now imports
``liveness`` and ``resolve_session_id`` LAZILY, inside the function body, at
each call (module-level imports were removed so an ImportError in the
session package can't take down every hard-deny guard -- see
dispatch_checks.py's module docstring). Patching
``dispatch_checks.liveness`` / ``dispatch_checks.resolve_session_id`` no
longer has any effect (those module attributes don't exist anymore); the
correct seam is now the ORIGIN modules themselves
(``coordinator_core.session.liveness`` and ``coordinator_core.session.core``,
imported here as ``session_liveness`` / ``session_core``), since the lazy
``from ... import ...`` re-resolves the current attribute off that module
object on every call.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session import liveness as session_liveness
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.session.scope import format_touch_event


def _parse_fixture_line(line):
    """Parse one `touched_lines` entry -- either a bare repo-relative path
    (this suite's own fail-safe-CLAIMED shorthand) or a full
    `format_touch_event`-produced `'<verb> <iso8601> <path>'` line -- into
    `(verb, timestamp, path)` for `touch_record.append_event`."""
    parts = line.split(None, 2)
    if len(parts) == 3 and parts[0] in ("T", "R"):
        verb, ts_str, path = parts
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            return verb, ts, path
        except ValueError:
            pass
    return "T", 0.0, line


def _write_session(root, sid, *, meta=None, touched_lines=None, touched_mtime=None):
    """C7b: `_rm_peer_claim_of` now reads claim CONTENT exclusively through
    `touch_record.project_live_claims` against `touch-record.jsonl` -- the
    AC21 union with legacy `touched.txt` content is deleted. `touched.txt`
    itself is still written and still stamped with `touched_mtime` because
    the function's mtime BACKSTOP (for a sid canonical liveness does not
    cover) still stats that literal filename, unchanged by this chunk --
    only the CLAIM-content read moved to the seam. `touch-record.jsonl` is
    written alongside it, translated from the same `touched_lines`, so the
    seam sees the same claims this fixture used to encode directly.

    C3 (this chunk): the mtime BACKSTOP itself moved off a literal
    `touched.txt` stat onto `liveness.newest_record_mtime(sid_dir)` -- the
    newest mtime among ALL of the dir's regular files, not one hardcoded
    name. `touch-record.jsonl` is therefore ALSO stamped with
    `touched_mtime` here (whenever one is given) so this fixture's
    deliberately-staled/fresh mtime is still what the backstop observes --
    leaving it at its natural "just written" mtime would silently make
    every "stale" fixture read as fresh again post-cutover.
    """
    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    if meta is not None:
        (sdir / "meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    if touched_lines is not None:
        touched = sdir / "touched.txt"
        touched.write_text("\n".join(touched_lines) + "\n", encoding="utf-8")
        if touched_mtime is not None:
            os.utime(touched, (touched_mtime, touched_mtime))
        record_sink = sdir / "touch-record.jsonl"
        for line in touched_lines:
            verb, ts, path = _parse_fixture_line(line)
            touch_record.append_event(
                record_sink, session_id=sid, agent_id=None, verb=verb, path=path,
                timestamp=ts,
            )
        if touched_mtime is not None:
            os.utime(record_sink, (touched_mtime, touched_mtime))
    return sdir


class TestLivePeerStaleMtimeIsContested:
    """2026-07-10 incident shape: canonical-live but mtime-stale peer must
    still block — this is exactly the case the mtime-only port would miss."""

    def test_blocks_live_peer_despite_stale_mtime(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time() - 3600,  # 1h stale — well past the 30m backstop window
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"

    def test_nested_target_under_touched_dir_is_contested(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=["docs/plans"],
            touched_mtime=time.time() - 3600,
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        target = str(tmp_path / "docs" / "plans" / "sub" / "file.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"


class TestCanonicalDeadPeerNotContested:
    """A sid canonical KNOWS about (has meta.json) but rules dead must be
    skipped entirely — no mtime fallback for a sid canonical has already
    ruled on, even if touched.txt is fresh."""

    def test_dead_peer_with_fresh_mtime_not_contested(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "dead-sess",
            meta={"stable_pid": "9999"},
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time(),  # fresh, but canonical says dead
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset()
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == ""


class TestDeadSessionRecentMtime:
    """2026-05-28 incident shape (block-destructive-rm.sh's general
    uncommitted-work guard): a session dir canonical does NOT cover (no
    meta.json — outside canonical's scan scope) degrades to the mtime
    backstop. Recent touched.txt still protects the path."""

    def test_uncovered_sid_recent_mtime_contested(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "legacy-sess",
            meta=None,  # no meta.json -> outside canonical's scan scope
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time() - 60,  # 1 minute ago
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset()
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "legacy-sess"

    def test_uncovered_sid_stale_mtime_not_contested(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "legacy-sess",
            meta=None,
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time() - 3600,  # 1h ago, past the 30m backstop
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset()
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == ""


class TestDegradeToMtimeWhenCanonicalSourceYieldsNothing:
    """Global degrade path: liveness.live_session_ids raising / returning
    empty must not silently allow a live peer's claim to be missed — every
    uncovered sid still degrades to the mtime backstop (fail-safe)."""

    def test_liveness_raises_degrades_every_sid_to_mtime(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta=None,
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time() - 60,
        )

        def _boom(cwd=None):
            raise RuntimeError("canonical liveness source unavailable")

        monkeypatch.setattr(session_liveness, "live_session_ids", _boom)
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"

    def test_liveness_raises_still_degrades_covered_sid_to_mtime(self, tmp_path, monkeypatch):
        # Review: code-reviewer (Finding 2) -- the sibling "raises" test above
        # uses meta=None (uncovered sid, already-correct path pre-fix). This
        # exercises a COVERED sid (meta.json present) whose canon_covers gate
        # was, pre-fix, computed from meta.json presence alone -- silently
        # excluding it from the raise-degrades-everything guarantee. Fails
        # against pre-fix code (returns "" because canon_covers=True,
        # sid not in live_sids=frozenset() -> continue); passes once
        # `canon_covers = live_ok and os.path.isfile(meta_path)` gates on the
        # raise via `live_ok`.
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "12345"},
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time() - 60,  # fresh
        )

        def _boom(cwd=None):
            raise RuntimeError("canonical liveness source unavailable")

        monkeypatch.setattr(session_liveness, "live_session_ids", _boom)
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"

    def test_empty_live_set_with_no_meta_json_uses_mtime_backstop(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta=None,
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time() - 3600,
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset()
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == ""


class TestSelfIdentityExclusion:
    """2026-07-16 bug-backlog 2026-07-16-dispatch-checks-py-peer-claim-guard-
    omit-811b3291dd8a.yaml (code-reviewer Finding 3): the caller's OWN sid
    must never be reported as a contesting peer of its OWN claim. Mirrors
    bash's ``cur_sid`` / ``cs_resolve_session_id`` self-exclusion in
    ``_rm_peer_claim_of`` / ``_gr_peer_claim_of``.

    Monkeypatches ``coordinator_core.session.core.resolve_session_id`` --
    Review: code-reviewer (Finding 3) -- ``_rm_peer_claim_of`` now does a
    LAZY ``from coordinator_core.session.core import resolve_session_id``
    inside its own body at each call, so it re-resolves the current
    attribute off ``coordinator_core.session.core`` (``session_core`` here)
    on every invocation; patching that origin module (as opposed to the old
    pre-refactor seam of patching a module-level name already bound inside
    ``dispatch_checks``) is now the correct and only effective seam.
    """

    def test_self_claim_not_contested(self, tmp_path, monkeypatch):
        # Fails before the fix (returns "my-sid" -- a session's own claim on
        # its own just-created path is wrongly reported as a LIVE PEER
        # claim); passes after (returns "" -- not contested, self may
        # delete its own file).
        root = str(tmp_path)
        _write_session(
            root,
            "my-sid",
            meta={"stable_pid": "1111"},
            touched_lines=["docs/plans/my-scratch.md"],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"my-sid"})
        )
        monkeypatch.setattr(session_core, "resolve_session_id", lambda cwd=None: "my-sid")
        target = str(tmp_path / "docs" / "plans" / "my-scratch.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == ""

    def test_different_live_peer_still_contested_alongside_self_exclusion(self, tmp_path, monkeypatch):
        # Regression guard: self-exclusion must not accidentally widen to
        # exclude a genuine, DIFFERENT live peer.
        root = str(tmp_path)
        _write_session(
            root,
            "my-sid",
            meta={"stable_pid": "1111"},
            touched_lines=["docs/plans/my-scratch.md"],
            touched_mtime=time.time(),
        )
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "2222"},
            touched_lines=["docs/plans/shared-work.md"],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({"my-sid", "peer-sess"}),
        )
        monkeypatch.setattr(session_core, "resolve_session_id", lambda cwd=None: "my-sid")
        target = str(tmp_path / "docs" / "plans" / "shared-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"

    def test_unresolvable_cur_sid_does_not_false_allow_live_peer(self, tmp_path, monkeypatch):
        # Fail-closed direction: an unresolvable/ambiguous identity
        # (resolve_session_id -> "") must exclude NOTHING -- a genuine live
        # peer stays contested.
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "2222"},
            touched_lines=["docs/plans/shared-work.md"],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        monkeypatch.setattr(session_core, "resolve_session_id", lambda cwd=None: "")
        target = str(tmp_path / "docs" / "plans" / "shared-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"


class TestLazyImportErrorDegradesFailClosed:
    """Review: code-reviewer (Finding 3) -- ``_rm_peer_claim_of`` imports
    ``liveness`` / ``resolve_session_id`` LAZILY, per-call. An ImportError of
    either underlying session module must degrade in the SAME safe direction
    as a raising call (never widen to "allow" for this not-overridable
    destructive-guard path).

    Simulated via ``sys.modules[<name>] = None`` PLUS deleting the
    already-resolved attribute off the parent ``coordinator_core.session``
    package -- Python raises ``ImportError`` for a ``from ... import ...``
    when the target submodule is present in ``sys.modules`` but bound to
    ``None``; but since this test file's own module-level import has already
    set that submodule as an attribute on the parent package, that attribute
    must ALSO be removed or ``from coordinator_core.session import
    liveness`` finds it via the parent's ``__dict__`` before consulting
    ``sys.modules`` at all.
    """

    def test_liveness_import_error_degrades_to_mtime_backstop(self, tmp_path, monkeypatch):
        import sys
        import coordinator_core.session as _session_pkg

        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "12345"},  # covered sid (has meta.json)
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time() - 60,  # fresh, well within the 30m backstop
        )
        monkeypatch.delattr(_session_pkg, "liveness", raising=False)
        monkeypatch.setitem(sys.modules, "coordinator_core.session.liveness", None)
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        # An ImportError here must land exactly like a raising
        # live_session_ids call: live_ok=False forces canon_covers=False
        # for EVERY sid (even a covered one), falling through to the mtime
        # backstop -- which still contests a fresh touched.txt. Fail-CLOSED:
        # the destructive guard is never silently disabled by a broken
        # session-package import.
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"

    def test_resolve_session_id_import_error_excludes_nothing(self, tmp_path, monkeypatch):
        import sys
        import coordinator_core.session as _session_pkg

        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "2222"},
            touched_lines=["docs/plans/shared-work.md"],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        monkeypatch.delattr(_session_pkg, "core", raising=False)
        monkeypatch.setitem(sys.modules, "coordinator_core.session.core", None)
        target = str(tmp_path / "docs" / "plans" / "shared-work.md")
        # An ImportError resolving cur_sid must degrade to "" (exclude
        # nothing) -- the same fail-closed direction as an unresolvable
        # identity -- so a genuine live peer stays contested, never a
        # false-ALLOW.
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"


class TestEventLinePeerClaim:
    """Review: code-reviewer (Finding 2) -- event-line coverage. Before this
    class, no test in this suite exercised a real ``T``/``R``-prefixed line
    through ``_rm_peer_claim_of``'s projection or its fallbacks; every prior
    fixture was a bare legacy path (see other classes in this file). Built
    with ``format_touch_event`` (not hand-written literals) so a fixture
    cannot drift from the emitter.
    """

    def test_contests_peer_whose_last_event_is_claim(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=[format_touch_event("T", "docs/plans/live-work.md")],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"

    def test_does_not_contest_peer_whose_last_event_is_stale_release(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        released_path = tmp_path / "docs" / "plans" / "live-work.md"
        # No file on disk at released_path -> project_peer_claims' mtime
        # check sees it as not-currently-dirty, so the R stands unreverted.
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=[format_touch_event("R", "docs/plans/live-work.md")],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        assert dispatch_checks._rm_peer_claim_of(str(released_path), root) == ""

    def test_projection_failure_fallback_still_contests_claimed_path(self, tmp_path, monkeypatch):
        # Finding 1's fallback path: force the per-sid projection to fail and
        # assert the except-arm still contests a T-claimed path by parsing
        # the path field out of the raw event line, rather than silently
        # skipping the sid (fail-open).
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=[format_touch_event("T", "docs/plans/live-work.md")],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )

        def _boom(lines, path_mtimes, challenger_t_events=None):
            raise RuntimeError("projection blew up")

        monkeypatch.setattr(session_scope, "project_peer_claims", _boom)
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"


class TestLegacyTouchedTxtOnlyUnion:
    """C7c never landed -- `session/claims.py :: self_claim` still appends
    the old `touched.txt` dialect via `atomic_dedup_append`. A peer whose
    ONLY claim record is that legacy file (no `touch-record.jsonl` entry at
    all) must still be contested. Regression: pre-fix this returns "" --
    the jsonl seam alone projects zero claims for a sid with no jsonl file.
    """

    def test_legacy_only_claim_is_contested(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        sdir = Path(root) / ".git" / "coordinator-sessions" / "peer-sess"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "meta.json").write_text(json.dumps({"stable_pid": "1234"}) + "\n", encoding="utf-8")
        touched = sdir / "touched.txt"
        touched.write_text("docs/plans/live-work.md\n", encoding="utf-8")
        mtime = time.time()
        os.utime(touched, (mtime, mtime))
        # Deliberately no touch-record.jsonl written -- the whole point of
        # this fixture is a sid whose ONLY claim record is the legacy file.
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"

    def test_jsonl_only_claim_still_works_no_regression(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time(),
        )
        # _write_session writes both seams; delete the legacy file so only
        # touch-record.jsonl remains, exercising the pure-jsonl path.
        os.remove(Path(root) / ".git" / "coordinator-sessions" / "peer-sess" / "touched.txt")
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"

    def test_union_of_disjoint_and_overlapping_paths(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        sdir = _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=["docs/plans/jsonl-only.md"],
            touched_mtime=time.time(),
        )
        # Overwrite touched.txt with a disjoint legacy-only path plus one
        # overlapping with the jsonl seam, to assert the union (not either
        # side alone) governs.
        (sdir / "touched.txt").write_text(
            "docs/plans/legacy-only.md\ndocs/plans/jsonl-only.md\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        legacy_target = str(tmp_path / "docs" / "plans" / "legacy-only.md")
        jsonl_target = str(tmp_path / "docs" / "plans" / "jsonl-only.md")
        assert dispatch_checks._rm_peer_claim_of(legacy_target, root) == "peer-sess"
        assert dispatch_checks._rm_peer_claim_of(jsonl_target, root) == "peer-sess"

    def test_unreadable_legacy_file_fails_closed(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        sdir = _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=["docs/plans/jsonl-only.md"],
            touched_mtime=time.time(),
        )
        legacy = sdir / "touched.txt"

        def _boom(lines):
            raise RuntimeError("legacy projection blew up")

        monkeypatch.setattr(session_scope, "project_self_scope", _boom)
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        # An unrelated target: a legacy-read failure must fail CLOSED
        # (return sid), never widen to "unclaimed" for ANY target on this
        # sid -- never "narrow" the claimed set.
        target = str(tmp_path / "docs" / "plans" / "wholly-unrelated.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == "peer-sess"
        assert legacy.exists()


class TestNoContestPaths:
    def test_no_sessions_dir_returns_empty(self, tmp_path):
        root = str(tmp_path)
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == ""

    def test_unrelated_touched_path_not_contested(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            "peer-sess",
            meta={"stable_pid": "1234"},
            touched_lines=["docs/other/unrelated.md"],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sess"})
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == ""

    def test_archive_and_agents_dirs_skipped(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _write_session(
            root,
            ".archive",
            meta={"stable_pid": "1"},
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time(),
        )
        _write_session(
            root,
            ".agents",
            meta={"stable_pid": "1"},
            touched_lines=["docs/plans/live-work.md"],
            touched_mtime=time.time(),
        )
        monkeypatch.setattr(
            session_liveness,
            "live_session_ids",
            lambda cwd=None: frozenset({".archive", ".agents"}),
        )
        target = str(tmp_path / "docs" / "plans" / "live-work.md")
        assert dispatch_checks._rm_peer_claim_of(target, root) == ""
