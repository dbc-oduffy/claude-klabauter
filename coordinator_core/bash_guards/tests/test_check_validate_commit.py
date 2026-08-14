"""Parity tests for ``check_validate_commit`` Checks 5 and 8 (coordinator_core.
bash_guards.dispatch_checks) -- T4a-g2, the checks left OUT OF SCOPE by the
T4a-g1 first pass now that ``coordinator_core.session.scope.compute_scope``
gives Check 5 a native scope-set to compare against.

Oracle: DoE-claude's retired ``coordinator/hooks/scripts/validate-commit.sh``
(deleted 2026-07-20, DoE ``e91827a7``):
  - Check 5 -- scoped-staging warn: a staged file NOT in this
    session's scope set (``compute_scope``) is WARNED on by default, and
    DENIED under ``COORDINATOR_SCOPE_STRICT=1`` (Phase 5's strict-mode
    branch is now live, not dormant -- see the dispatch_checks module
    docstring "KNOWN PORTING GAPS", CLOSED entry g4-M1, and
    ``TestCheckFiveStrictModePromotion`` below).
  - Check 8 -- frontmatter-mutation subject discipline: a staged
    ``tasks/plans|state/handoffs|docs/plans`` ``.md`` file whose staged diff
    touches a load-bearing frontmatter key (status/deployment_state/
    consumed_by/shipped_in/predecessor/kind) is WARNED on unless the commit
    subject names the changed key or a lifecycle verb (pickup/handoff/
    consume/ship/abandon/supersede). Also warn-only in this port (the bash
    ``COORDINATOR_FRONTMATTER_STRICT`` deny branch is likewise a dormant gap,
    see the module docstring).

Both checks surface via the single warn-only flush at the end of
``check_validate_commit`` -- an ``_advisory()`` (``permissionDecision:
"allow"``) envelope with the accumulated warning text in
``additionalContext``, NEVER a ``"deny"`` verdict. This suite asserts both
the positive (warning text + allow-not-deny) and negative (clean input,
no warning) cases for each check independently.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.bash_guards import dispatch as bash_dispatch
from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES
from coordinator_core.session import core
from coordinator_core.session import scope as session_scope
from coordinator_core.session.scope import OwnerFact


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> str:
    root = str(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _push_started_at_to_future(root: str, sid: str) -> None:
    """Set the session's started_at file to an hour in the future so the
    scope.compute_scope mtime fallback does NOT auto-adopt freshly-staged
    files into this session's own scope (mirrors the real-world case of a
    file staged before this session began)."""
    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    future = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
    )
    (sdir / "started_at").write_text(
        future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Check 5 -- scoped staging warn
# ---------------------------------------------------------------------------


class TestCheckFiveScopedStaging:
    def test_foreign_unowned_file_warns_orphan(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "SCOPE:" in out["additionalContext"]
        assert "foo.txt" in out["additionalContext"]
        # AC6: the rendered advisory renames away from "orphan" to
        # "unknown owner" (no-claim-found is the only class that renders
        # that literal token, and it must state outright this is not a
        # safety verdict). AC9: only the label substring changes here --
        # the OUTCOME (still warns) is unchanged from before this plan.
        assert "unknown owner" in out["additionalContext"]
        assert "orphan" not in out["additionalContext"]

        # AC6a: the scope-warnings.log `owner:` column is a documented
        # sibling-repo (DoE) surface and is explicitly UNCHANGED by this
        # plan -- the machine token stays the literal "orphan" even though
        # the operator-facing sentence above no longer says it.
        log = Path(root) / ".git" / "coordinator-sessions" / sid / "scope-warnings.log"
        assert log.is_file()
        assert "foreign-staged | foo.txt | owner:orphan" in log.read_text(encoding="utf-8")

    def test_foreign_file_owned_by_another_session_warns_with_owner(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        other_sid = "other-sess"
        assert core.init(sid, cwd=root)
        assert core.init(other_sid, cwd=root)
        _push_started_at_to_future(root, sid)

        other_touched = Path(root) / ".git" / "coordinator-sessions" / other_sid / "touched.txt"
        other_touched.write_text("foo.txt\n", encoding="utf-8")

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "SCOPE:" in out["additionalContext"]
        assert "session %s" % other_sid in out["additionalContext"]

    def test_own_scoped_file_no_scope_warning(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")
        touched = Path(root) / ".git" / "coordinator-sessions" / sid / "touched.txt"
        touched.write_text("foo.txt\n", encoding="utf-8")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None

    def test_dispatched_agent_touched_file_no_scope_warning(self, tmp_path):
        """A file edited ONLY by a subagent this EM dispatched (never by an
        EM-context tool call, so it never lands in the EM's own
        touched.txt) must NOT be warned as foreign/orphan. compute_scope()
        alone has no notion of `.agents/<agent_id>/touched.txt` -- Check 5
        must union `my_agent_touched(session_id, "broadened")` in, matching
        coordinator-safe-commit's own default-path scope computation, or
        every subagent-only edit misattributes as "owned by orphan" (the
        observed false-positive pattern this test pins down)."""
        root = _init_repo(tmp_path)
        sid = "em-sess"
        agent_id = "abcdef0123456789"  # bare-hex unnamed-agent shape
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        # Simulate track_touched_files' agent-keyed write: the dispatched
        # subagent's own touched.txt, back-pointered to the dispatching EM
        # session -- never appended to the EM's own touched.txt.
        agent_dir = Path(root) / ".git" / "coordinator-sessions" / ".agents" / agent_id
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text(sid + "\n", encoding="utf-8")
        (agent_dir / "touched.txt").write_text("foo.txt\n", encoding="utf-8")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None

    def test_mtime_fallback_orphan_no_warning(self, tmp_path):
        """AC12 regression pin. Unlike every other test in this class, this
        one does NOT call ``_push_started_at_to_future`` -- ``started_at``
        stays at its real (past) value, so ``compute_scope()``'s mtime
        fallback stays LIVE for a staged, mtime-fresh, nobody-claimed file.
        Pre-C1a that file lands in ``my_scope`` directly; post-C1a it moves
        to ``orphans`` instead (my_scope only narrows). Check 5 must forgive
        exactly this mtime-fresh orphan subset, so this must pass unchanged
        on both sides of that narrowing."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None

    def test_no_session_id_skips_scope_check_entirely(self, tmp_path):
        root = _init_repo(tmp_path)
        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', "", cwd=root
        )
        assert result is None

    def test_staged_vanished_untracked_file_still_warns_though_absent_from_orphans(
        self, tmp_path
    ):
        """C7 verification (2026-08-05-touched-sibling-escape-and-suppressed-
        trailer, `check_validate_commit` against C1's narrowed `my_scope`),
        named case (a): a never-committed file that is `git add`-staged and
        then deleted from disk before `check_validate_commit` runs is
        invisible to `compute_scope`'s dirty scan ENTIRELY -- neither `git
        diff --name-only HEAD` nor `git ls-files --others --exclude-
        standard` reports a path that never existed at HEAD and no longer
        exists on disk -- so it never reaches `scope_result.orphans` at
        all, not even as an unforgiven stale entry (confirmed empirically:
        both git probes return empty for this exact shape). This predates
        C1 and is orthogonal to it: C1 only narrows which Step-1
        `touched.txt` candidates survive into `my_scope`/`orphans`, it does
        not touch Step 2's dirty-scan definition that makes this shape
        invisible in the first place. Check 5's warn loop below keys off
        `staged_file not in my_scope` (never off `orphans` membership), so
        this file still warns exactly as any other foreign staged file
        would -- the Check 5 orphan-forgiveness block inside
        `check_validate_commit` is simply never consulted for a path that
        never became an orphan, and that absence does not widen `my_scope`
        or suppress the warning. Distinct from
        test_tracked_then_deleted_file_hits_mtime_epoch_zero_in_forgiveness_
        loop below, which exercises AC10's literal `mtime_epoch()==0`
        mechanism directly -- this test's shape is invisible to
        `compute_scope` for an earlier, different reason and never reaches
        that call at all."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)

        (tmp_path / "scratch.txt").write_text("temp\n", encoding="utf-8")
        _git(root, "add", "scratch.txt")
        (tmp_path / "scratch.txt").unlink()

        scope_result = session_scope.compute_scope(sid, cwd=root)
        assert "scratch.txt" not in scope_result.orphans
        assert "scratch.txt" not in scope_result.my_scope

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add scratch"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "SCOPE:" in out["additionalContext"]
        assert "scratch.txt" in out["additionalContext"]

    def test_tracked_then_deleted_file_hits_mtime_epoch_zero_in_forgiveness_loop(
        self, tmp_path
    ):
        """AC10 case (a)'s literal mechanism (`docs/plans/2026-08-05-touched-
        sibling-escape-and-suppressed-trailer.md`, line 328/757-762): "mtime_
        epoch of a non-existent file is 0 ... so compute_scope Step 2 never
        re-adds it as an orphan". Unlike
        test_staged_vanished_untracked_file_still_warns_though_absent_from_
        orphans (a never-tracked, staged-then-deleted file, invisible to
        Step 2's dirty scan entirely), THIS shape -- a file tracked at HEAD,
        then deleted and staged -- DOES enter `dirty_files`/`scope_result.
        orphans`, so it exercises `core.mtime_epoch()`'s `0` return directly
        inside the Check 5 orphan-forgiveness block's `>= started_epoch`
        comparison (see that block's C7-verification comment in
        `dispatch_checks.py`, immediately above `check_validate_commit`'s
        Check 5 warn loop)."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)

        (tmp_path / "tracked.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "tracked.txt")
        _git(root, "commit", "-q", "-m", "seed tracked.txt")

        _git(root, "rm", "-q", "tracked.txt")

        scope_result = session_scope.compute_scope(sid, cwd=root)
        assert "tracked.txt" in scope_result.orphans
        assert "tracked.txt" not in scope_result.my_scope

        result = dispatch_checks.check_validate_commit(
            'git commit -m "rm tracked"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "SCOPE:" in out["additionalContext"]
        assert "tracked.txt" in out["additionalContext"]

    def test_strict_mode_denies_staged_vanished_untracked_file(
        self, tmp_path, monkeypatch
    ):
        """AC10 case (a), strict-mode half: AC10's own text names BOTH
        halves of case (a)'s expected behaviour -- default-mode warn (pinned
        above by
        test_staged_vanished_untracked_file_still_warns_though_absent_from_
        orphans) AND `COORDINATOR_SCOPE_STRICT=1` deny. Runs the full
        dispatch guard_chain (`bash_dispatch.evaluate_payload_json`),
        mirroring `TestCheckFiveStrictModePromotion.
        test_strict_mode_denies_sibling_owned_staged_deletion`'s pattern, so
        a sibling hard-deny identity guard positioned ahead of
        validate-commit in the chain cannot mask this check and make the
        test pass for the wrong reason."""
        monkeypatch.setenv("COORDINATOR_SCOPE_STRICT", "1")
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)

        (tmp_path / "scratch.txt").write_text("temp\n", encoding="utf-8")
        _git(root, "add", "scratch.txt")
        (tmp_path / "scratch.txt").unlink()

        scope_result = session_scope.compute_scope(sid, cwd=root)
        assert "scratch.txt" not in scope_result.orphans
        assert "scratch.txt" not in scope_result.my_scope

        raw = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "add scratch"'},
            "session_id": sid,
            "cwd": root,
        })
        result = bash_dispatch.evaluate_payload_json(raw)

        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "scratch.txt" in out["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# Check 5 -- COORDINATOR_SCOPE_STRICT=1 promotion (g4-M1). Models the
# sibling-still-live-session shape (a live sibling session's touched.txt
# claims the target), not a random never-touched orphan -- the orphan shape
# proves a weaker claim (see the plan body's "Corrected rationale"). Runs the
# FULL dispatch guard_chain (bash_dispatch.evaluate_payload_json) under an
# EM-context payload (no agent_id) rather than calling check_validate_commit
# directly, so a sibling hard-deny identity guard positioned ahead of
# validate-commit in the chain (e.g. the subagent-commit enforcement gate)
# cannot mask this check and make the test pass for the wrong reason.
# ---------------------------------------------------------------------------


class TestCheckFiveStrictModePromotion:
    def _stage_sibling_owned_deletion(self, tmp_path, root, sid, other_sid):
        assert core.init(sid, cwd=root)
        assert core.init(other_sid, cwd=root)
        _push_started_at_to_future(root, sid)

        # The file pre-exists in the repo (committed) and is claimed by a
        # still-LIVE sibling session's touched.txt -- this session (sid) then
        # stages that same file's DELETION, which is git-dirty and enters
        # this session's mtime-dirty candidate set, then gets subtracted back
        # out by the sibling's live touched.txt claim (scope.py:340-401).
        (tmp_path / "sibling.txt").write_text("owned by sibling\n", encoding="utf-8")
        _git(root, "add", "sibling.txt")
        _git(root, "commit", "-q", "-m", "seed sibling.txt")

        other_touched = Path(root) / ".git" / "coordinator-sessions" / other_sid / "touched.txt"
        other_touched.write_text("sibling.txt\n", encoding="utf-8")

        _git(root, "rm", "-q", "sibling.txt")

    def _em_payload(self, root: str, sid: str, command: str) -> str:
        # EM-context: no `agent_id` field at all -- resolve_effective_types
        # then resolves agent_id/agent_type/subagent_type all to "", which is
        # the EM/main-loop identity, not a Sonnet/Haiku subagent.
        return json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": sid,
            "cwd": root,
        })

    def test_strict_mode_denies_sibling_owned_staged_deletion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCOPE_STRICT", "1")
        root = _init_repo(tmp_path)
        sid, other_sid = "my-sess", "other-sess"
        self._stage_sibling_owned_deletion(tmp_path, root, sid, other_sid)

        raw = self._em_payload(root, sid, 'git commit -m "rm sibling"')
        result = bash_dispatch.evaluate_payload_json(raw)

        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "sibling.txt" in out["permissionDecisionReason"]

    def test_non_strict_mode_only_warns_same_shape(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SCOPE_STRICT", raising=False)
        root = _init_repo(tmp_path)
        sid, other_sid = "my-sess", "other-sess"
        self._stage_sibling_owned_deletion(tmp_path, root, sid, other_sid)

        raw = self._em_payload(root, sid, 'git commit -m "rm sibling"')
        result = bash_dispatch.evaluate_payload_json(raw)

        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "SCOPE:" in out["additionalContext"]
        assert "sibling.txt" in out["additionalContext"]

    def test_mtime_fallback_orphan_strict_mode_no_deny(self, tmp_path, monkeypatch):
        """AC12 regression pin, strict-mode side. Same mtime-fallback-live
        fixture as ``TestCheckFiveScopedStaging.
        test_mtime_fallback_orphan_no_warning`` (no ``_push_started_at_to_
        future`` call), but under ``COORDINATOR_SCOPE_STRICT=1`` -- an
        orphan must never DENY either, on both sides of C1a's narrowing.

        Unlike its class siblings, this test calls ``check_validate_commit``
        directly rather than running the full dispatch guard_chain: it
        asserts an ABSENCE of a deny from this one function specifically, so
        there is no sibling-hard-deny-guard masking risk in this direction
        (masking would only hide a deny this function itself produces, and
        there isn't one to hide)."""
        monkeypatch.setenv("COORDINATOR_SCOPE_STRICT", "1")
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None


# ---------------------------------------------------------------------------
# Check 5 -- owner-attribution rendering, all six classes plus the AC7
# contested rendering (docs/plans/2026-08-03-check5-owner-attribution-
# liveness.md, chunk C5). ``_format_owner_sentence``/``_format_owner_token``
# are exercised directly for the classes a live git fixture cannot cheaply
# reach (agent-race, unreadable) -- see that function's own docstring for
# the six-class enumeration this suite pins one-for-one.
# ---------------------------------------------------------------------------


class TestFormatOwnerSentenceSixClasses:
    def test_no_claim_found_renders_unknown_owner_states_not_a_verdict(self):
        sentence = dispatch_checks._format_owner_sentence(None, {})
        assert "unknown owner" in sentence
        assert "orphan" not in sentence
        assert "not a safety verdict" in sentence.lower()

    def test_no_claim_found_token_stays_orphan_ac6a(self):
        # AC6a: the no-claim-found MACHINE token (scope-warnings.log `owner:`
        # column) stays the literal "orphan" -- only the operator sentence
        # above renames. Changing this breaks a documented sibling-repo
        # surface (test_foreign_unowned_file_warns_orphan:100).
        assert dispatch_checks._format_owner_token(None) == "orphan"

    def test_live_peer_renders_confirmed_live_with_basis(self):
        fact = OwnerFact(owner="peer-sid", liveness="live", claim_source="session")
        sentence = dispatch_checks._format_owner_sentence(
            fact, {"peer-sid": (True, "stable-pid", None)}
        )
        assert "peer-sid" in sentence
        assert "confirmed live" in sentence
        assert "stable-pid" in sentence
        assert "orphan" not in sentence
        assert "unknown owner" not in sentence

    def test_dead_peer_renders_no_longer_live_distinct_from_live(self):
        fact = OwnerFact(owner="peer-sid", liveness="dead", claim_source="session")
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "peer-sid" in sentence
        assert "no longer live" in sentence
        assert "confirmed live" not in sentence
        assert "orphan" not in sentence
        assert "unknown owner" not in sentence

    def test_peers_dispatched_agent_attributes_to_owning_session_not_unknown(self):
        """AC5 -- the asymmetry that made the incident's dominant path
        invisible: a claim from a peer's dispatched agent must attribute to
        that agent's owning session, never to unknown-owner."""
        fact = OwnerFact(owner="em-sid", liveness="live", claim_source="agent")
        sentence = dispatch_checks._format_owner_sentence(
            fact, {"em-sid": (True, "recency-window", 12)}
        )
        assert "em-sid" in sentence
        assert "dispatched agent" in sentence
        assert "unknown owner" not in sentence
        assert "orphan" not in sentence

    def test_unresolved_agent_race_renders_distinctly_not_unknown_owner(self):
        """AC6: agent-race gets its OWN pin and must not collapse into
        class 6's "unknown owner" -- it is a positive, recent, on-disk
        claim, just not yet resolvable to a session id."""
        fact = OwnerFact(
            owner="abcdef0123456789", liveness="undetermined", claim_source="agent-race"
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "abcdef0123456789" in sentence
        assert "unknown owner" not in sentence
        assert "orphan" not in sentence

    def test_claims_unreadable_renders_distinctly_not_unknown_owner(self):
        """AC6: claims-unreadable likewise gets its own pin and must not
        collapse into "unknown owner"."""
        fact = OwnerFact(
            owner="sibling-sid", liveness="undetermined", claim_source="unreadable"
        )
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "sibling-sid" in sentence
        assert "unreadable" in sentence
        assert "unknown owner" not in sentence
        assert "orphan" not in sentence

    def test_liveness_undetermined_renders_third_contested_class(self):
        """AC7: an unresolvable liveness verdict on an otherwise-resolved
        session claim degrades to a THIRD rendering -- "liveness
        undetermined -- treated as contested" -- never toward "unknown
        owner", and never asserted as flatly live or dead (which would
        collide with AC4's live/dead distinguishability)."""
        fact = OwnerFact(owner="peer-sid", liveness="undetermined", claim_source="session")
        sentence = dispatch_checks._format_owner_sentence(fact, {})
        assert "peer-sid" in sentence
        assert "CONTESTED" in sentence
        assert "confirmed live" not in sentence
        assert "no longer live" not in sentence
        assert "unknown owner" not in sentence

    def test_orphan_absent_from_every_owner_class_rendering(self):
        """AC6: the word "orphan" must appear in NO operator-facing Check 5
        rendered message, across all six classes plus the AC7 contested
        rendering."""
        facts = [
            None,
            OwnerFact("peer-sid", "live", "session"),
            OwnerFact("peer-sid", "dead", "session"),
            OwnerFact("em-sid", "live", "agent"),
            OwnerFact("abcdef0123456789", "undetermined", "agent-race"),
            OwnerFact("sibling-sid", "undetermined", "unreadable"),
            OwnerFact("peer-sid", "undetermined", "session"),
        ]
        for fact in facts:
            sentence = dispatch_checks._format_owner_sentence(fact, {})
            assert "orphan" not in sentence, sentence

    def test_all_owner_class_renderings_stay_within_shipped_message_budget(self):
        """AC11: every one of the six renderings stays within
        guard-message-size-discipline's shipped budget, read from that
        plan's ``MESSAGE_PROSE_CAP_BYTES`` constant -- never a hard-coded
        number here."""
        facts = [
            None,
            OwnerFact("peer-sid", "live", "session"),
            OwnerFact("peer-sid", "dead", "session"),
            OwnerFact("em-sid", "live", "agent"),
            OwnerFact("abcdef0123456789", "undetermined", "agent-race"),
            OwnerFact("sibling-sid", "undetermined", "unreadable"),
            OwnerFact("peer-sid", "undetermined", "session"),
        ]
        for fact in facts:
            sentence = dispatch_checks._format_owner_sentence(fact, {})
            assert len(sentence.encode("utf-8")) <= MESSAGE_PROSE_CAP_BYTES, sentence


class TestCheckFiveOwnerAttributionIntegration:
    def test_ac2_property_monkeypatches_compute_scope_matches_rendered_label(
        self, tmp_path, monkeypatch
    ):
        """AC2 as a PROPERTY, not a spelling: monkeypatch ``compute_scope``
        to return a KNOWN attribution and assert the rendered label matches
        it exactly -- a surviving local scan of any form (``os.listdir``,
        ``os.scandir``, ``Path.iterdir``, ``glob``) would produce a
        mismatching label (since it cannot see this injected id at all) and
        fail this test."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        injected = session_scope.ScopeResult(
            my_scope=[],
            skipped=[],
            orphans=["foo.txt"],
            attribution={
                "foo.txt": OwnerFact("injected-owner-9f3", "live", "session")
            },
        )
        monkeypatch.setattr(session_scope, "compute_scope", lambda *a, **kw: injected)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert "injected-owner-9f3" in out["additionalContext"]

    def test_ac10_strict_deny_renders_same_attribution_as_advisory(
        self, tmp_path, monkeypatch
    ):
        """AC10: the strict-mode deny and the warn-only advisory render from
        ONE formatter, so the two cannot drift. Monkeypatch that shared
        formatter to a distinguishable marker and assert the SAME marker
        appears verbatim in both the advisory's ``additionalContext`` and
        the strict-mode deny's ``permissionDecisionReason``."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        marker = "OWNER-SENTENCE-MARKER-7d3c"
        monkeypatch.setattr(
            dispatch_checks, "_format_owner_sentence", lambda *a, **kw: marker
        )

        advisory = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert advisory is not None
        assert marker in advisory["hookSpecificOutput"]["additionalContext"]

        monkeypatch.setenv("COORDINATOR_SCOPE_STRICT", "1")
        denied = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert denied is not None
        denied_out = denied["hookSpecificOutput"]
        assert denied_out["permissionDecision"] == "deny"
        assert marker in denied_out["permissionDecisionReason"]

    def test_explicit_pathspec_naming_peer_file_still_warns(self, tmp_path):
        """AC8a negative spec: ``git commit -- <peer file>`` explicitly
        naming a live peer's staged file as the commit's own pathspec must
        STILL warn -- pathspec presence alone must never suppress Check 5
        (C4's own negative spec: a peer file is only dropped from the warn
        set when it is NOT named by the commit's own pathspec; a peer file
        that IS the pathspec target still resolves into ``commit_scope``
        and still warns)."""
        root = _init_repo(tmp_path)
        sid, other_sid = "my-sess", "other-sess"
        assert core.init(sid, cwd=root)
        assert core.init(other_sid, cwd=root)
        _push_started_at_to_future(root, sid)

        other_touched = (
            Path(root) / ".git" / "coordinator-sessions" / other_sid / "touched.txt"
        )
        other_touched.write_text("peer.txt\n", encoding="utf-8")

        (tmp_path / "peer.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "peer.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add peer" -- peer.txt', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "peer.txt" in out["additionalContext"]

    def test_ac7_raising_compute_scope_still_warns_contested(
        self, tmp_path, monkeypatch
    ):
        """AC7 residual fix: a raising ``compute_scope()`` must degrade
        toward CONTESTED, not toward silence. Pre-fix, ``scope_result``
        landed ``None`` and the warn loop was ``[] if my_scope is None else
        commit_scope`` -- Check 5 was skipped entirely. Assert the warning
        still fires and its rendering is the CONTESTED "claims-unreadable"
        class (never absent, and never collapsed into "unknown owner",
        which would misreport a resolvable-in-principle claim as no claim
        at all)."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        def _raise(*a, **kw):
            raise RuntimeError("compute_scope exploded")

        monkeypatch.setattr(session_scope, "compute_scope", _raise)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "SCOPE:" in out["additionalContext"]
        assert "foo.txt" in out["additionalContext"]
        assert "unreadable this call" in out["additionalContext"]
        assert "unknown owner" not in out["additionalContext"]

    def test_ac7_raising_compute_scope_strict_mode_does_not_deny(
        self, tmp_path, monkeypatch
    ):
        """Hard-constraint tradeoff call (this dispatch's EM guidance, not
        decided by the plan): a raising ``compute_scope()`` stays warn-only
        even under ``COORDINATOR_SCOPE_STRICT=1``. Promoting this arm to a
        deny would turn one exception into a repo-wide commit outage (every
        staged file across every commit would render CONTESTED and DENY).
        That strict-mode question is left for a PM ruling -- this test pins
        the warn-only behavior actually implemented here."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        def _raise(*a, **kw):
            raise RuntimeError("compute_scope exploded")

        monkeypatch.setattr(session_scope, "compute_scope", _raise)
        monkeypatch.setenv("COORDINATOR_SCOPE_STRICT", "1")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "unreadable this call" in out["additionalContext"]
        assert "unknown owner" not in out["additionalContext"]


# ---------------------------------------------------------------------------
# Check 8 -- frontmatter mutation subject discipline
# ---------------------------------------------------------------------------


def _stage_frontmatter_change(root: str, tmp_path: Path, rel_path: str, old: str, new: str) -> None:
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(old, encoding="utf-8")
    _git(root, "add", rel_path)
    _git(root, "commit", "-q", "-m", "seed frontmatter file")
    target.write_text(new, encoding="utf-8")
    _git(root, "add", rel_path)


class TestCheckEightFrontmatterMutation:
    _OLD = "---\nstatus: draft\nkind: plan\n---\nbody\n"
    _NEW = "---\nstatus: ready\nkind: plan\n---\nbody\n"

    def test_mutation_without_lifecycle_subject_warns(self, tmp_path):
        root = _init_repo(tmp_path)
        _stage_frontmatter_change(root, tmp_path, "docs/plans/2026-07-16-foo.md", self._OLD, self._NEW)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "tweak plan"', "no-session", cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "FRONTMATTER-MUTATION:" in out["additionalContext"]
        assert "docs/plans/2026-07-16-foo.md" in out["additionalContext"]

    def test_mutation_with_lifecycle_verb_no_warning(self, tmp_path):
        root = _init_repo(tmp_path)
        _stage_frontmatter_change(root, tmp_path, "docs/plans/2026-07-16-foo.md", self._OLD, self._NEW)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "handoff: flip status to ready"', "no-session", cwd=root
        )
        assert result is None or "FRONTMATTER-MUTATION:" not in result["hookSpecificOutput"].get(
            "additionalContext", ""
        )

    def test_mutation_with_key_name_in_subject_no_warning(self, tmp_path):
        root = _init_repo(tmp_path)
        _stage_frontmatter_change(root, tmp_path, "docs/plans/2026-07-16-foo.md", self._OLD, self._NEW)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "status: bump to ready"', "no-session", cwd=root
        )
        assert result is None or "FRONTMATTER-MUTATION:" not in result["hookSpecificOutput"].get(
            "additionalContext", ""
        )

    def test_non_sensitive_body_change_no_warning(self, tmp_path):
        root = _init_repo(tmp_path)
        _stage_frontmatter_change(
            root,
            tmp_path,
            "docs/plans/2026-07-16-foo.md",
            self._OLD,
            "---\nstatus: draft\nkind: plan\n---\nbody with an edit\n",
        )

        result = dispatch_checks.check_validate_commit(
            'git commit -m "tweak body prose"', "no-session", cwd=root
        )
        assert result is None or "FRONTMATTER-MUTATION:" not in result["hookSpecificOutput"].get(
            "additionalContext", ""
        )

    def test_non_frontmatter_path_no_warning(self, tmp_path):
        root = _init_repo(tmp_path)
        _stage_frontmatter_change(root, tmp_path, "docs/other/2026-07-16-foo.md", self._OLD, self._NEW)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "tweak plan"', "no-session", cwd=root
        )
        assert result is None or "FRONTMATTER-MUTATION:" not in result["hookSpecificOutput"].get(
            "additionalContext", ""
        )

    def test_never_denies(self, tmp_path):
        root = _init_repo(tmp_path)
        _stage_frontmatter_change(root, tmp_path, "docs/plans/2026-07-16-foo.md", self._OLD, self._NEW)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "tweak plan"', "no-session", cwd=root
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] != "deny"
