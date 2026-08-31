"""
coordinator_core.tests.test_archive_stamp_claimant_identity — coverage for
`archive_stamp._record_claimant_identity_best_effort`, the claim-time stamp of
the claiming SESSION's harness identity (`claimed_by_name`)
written beside the unchanged UUID-carrying `claimed_by`/`picked_up_by` on both
the handoff claim path (`cs_claim_handoff`) and the memo claim path
(`cs_claim_memo_stamp`).

PM ruling, 2026-08-30. The gap this closes is narrow and worth stating, because
the resolver it complements has shipped since 2026-08-13: while the claimant is
LIVE, `session.resolve_address` already answers "who holds this baton" and no
stamp is needed. Once the claiming session EXITS, its registry record is gone,
the UUID resolves to `not_reachable`/`no-live-record`, and a claimed, in-flight
baton is left with an owner nobody can even name.

This module pins:

  1. `cs_claim_handoff` writes both fields beside an unchanged `claimed_by`.
  2. `cs_claim_memo_stamp` writes both fields on its own claim path too.
  3. A registry record carrying a name but NO socket (the harness omits
     `messagingSocketPath` whenever its cross-session-inbox gate is off —
     measured 44/44 records on 2026-08-14) stamps the name and omits the
     address key entirely. No sentinel, no empty string.
  4. `self_record()` returning None omits the key entirely.
  5. A raising `self_record()` is non-fatal: the claim still lands rc=0 with
     the lifecycle transition intact and no name written.
  6. Re-claiming RECONCILES `claimed_by_name` to the id beside it, and an
     unresolvable name CLEARS a stale one rather than leaving it standing.
     REVERSED 2026-08-31 from "never overwrites — the first claimant's identity
     is the forensic record" — that rule could not hold: the claim transition
     overwrites `claimed_by` in the same operation, so leaving the name behind
     manufactured a record that never existed. Incident and retraction record:
     `state/bug-backlog/2026-08-31-claim-handoff-takes-a-foreign-claim-and-drop-releases-nothing.yaml`.
     `human_claimant` keeps insert-when-absent: the operating human does not
     change when the claim does.
  6b. `cs_claim_handoff` REFUSES a baton held by a different LIVE session, fails
     open on an indeterminate liveness read, and does not refuse a DEAD holder
     (takeover of a dead claim is the ordinary recovery path).
  7. A `self_record()` naming a DIFFERENT session than the one being stamped falls
     through to `harness_registry.lookup(claimant_sid)` and stamps the CLAIMANT's
     name, on both claim paths. `self_record()` is an ambient `CLAUDE_PID` read;
     inside a warm server that is the environment of whichever session started the
     server, so a warm-served claim resolved an uninvolved live peer's name beside a
     correct carried id. Reported cross-repo by doe-claude-em 2026-08-30 and
     reproduced same-repo on that memo's own claim stamp: `picked_up_by` named the
     claimant while `claimed_by_name` named a peer that had never seen the artifact.
     Omitting on mismatch would be a fail-safe, not a fix — it would blank the field
     on every warm-served claim, which is the majority, and retire the capability it
     exists for. The field is omitted only when NEITHER leg resolves a record (8).
  8. Both legs failing omits the field — the established unresolvable degrade.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ops.handoff_transition  # noqa: F401 — @register_op side effect
import coordinator_core.ops.memo_transition  # noqa: F401 — @register_op side effect
import coordinator_core.ops.session.record_pickup  # noqa: F401 — @register_op side effect

import coordinator_core.archive_stamp as arstamp
from coordinator_core.session import harness_registry
from coordinator_core.tests._fixtures import init_repo as _init_repo
from coordinator_core.tests._fixtures import run_git as _git

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_DEFAULT_TEST_SESSION_ID = "22222222-2222-2222-2222-222222222222"
_NAME = "claude-klabauter-4f"
_SOCKET = r"\\.\pipe\LOCAL\cc-msg-9d6c94f6b101ab51917ba9f"


def _seed_handoff(repo: Path, name: str, extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: ready_to_fire\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_memo(repo: Path, name: str, extra: str = "") -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: fyi\n"
        "status: open\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _record(name, socket) -> harness_registry.RegistryRecord:
    return harness_registry.RegistryRecord(
        pid=1,
        start_epoch=1000.0,
        cwd=None,
        name=name,
        messaging_socket_path=socket,
    )


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    # Isolate from the human-claimant stamp, which shares the claim path.
    monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {})


def _set_self_record(monkeypatch, value):
    if callable(value):
        monkeypatch.setattr(harness_registry, "self_record", value)
    else:
        monkeypatch.setattr(harness_registry, "self_record", lambda: value)


class TestHandoffClaimStampsIdentity:
    def test_name_lands_beside_an_unchanged_claimed_by(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h1.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        # The UUID stays the durable identity, unchanged.
        assert "claimed_by: sess-abc" in text
        assert f"claimed_by_name: {_NAME}" in text

    def test_record_without_socket_still_stamps_the_name(self, tmp_path, monkeypatch):
        """The harness omits messagingSocketPath whenever its cross-session-inbox
        gate is off — measured 44/44 records on 2026-08-14. The name stamp must never
        have depended on a socket the harness routinely never wrote."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h2.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, None)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert f"claimed_by_name: {_NAME}" in text

    def test_absent_registry_record_omits_the_name_key(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h3.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, None)

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-abc" in text
        assert "claimed_by_name" not in text

    def test_raising_resolver_is_non_fatal_and_the_claim_still_lands(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h4.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")

        def _boom():
            raise RuntimeError("registry read failed")

        _set_self_record(monkeypatch, _boom)

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        # The lifecycle transition is intact — identity is advisory, never a gate.
        assert "status: claimed" in text
        assert "claimed_by: sess-abc" in text
        assert "claimed_by_name" not in text

    def test_a_stale_name_is_reconciled_to_the_id_beside_it(self, tmp_path, monkeypatch):
        """`claimed_by_name` must agree with `claimed_by`, so a re-claim rewrites it.

        REVERSES `test_existing_identity_is_never_overwritten` (2026-08-30): the
        claim transition overwrites `claimed_by` unconditionally in the SAME
        operation, so leaving the name behind manufactures a record that never
        existed. Incident record:
        `state/bug-backlog/2026-08-31-claim-handoff-takes-a-foreign-claim-and-drop-releases-nothing.yaml`.
        The PM ruling of 2026-08-30 established the FIELD; never-overwrite was an
        implementation choice made alongside it, and is what is reversed here.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "h5.md",
            extra="claimed_by_name: claude-klabauter-old\nclaimed_by_address: old-socket\n",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-abc" in text
        assert _NAME in text
        assert "claude-klabauter-old" not in text

    def test_an_unresolvable_name_clears_a_stale_one_rather_than_leaving_it(
        self, tmp_path, monkeypatch
    ):
        """No name for this claimant means the field is REMOVED, not left standing.

        A wrong-but-plausible name is worse than an absent one: it reads as a
        coherent holder and suppresses the lookup an empty field would prompt.

        MODELLED, not merely reasoned. A record resolving with no name is a
        first-class state in this plane: `session/reachability.py :: _resolve_one`
        returns None on `not record.name`, so that branch would be dead code if
        the state were unreachable. Incident and retraction record:
        `state/bug-backlog/2026-08-31-the-session-meta-json-answers-name-with-b07c2bf5d06c.yaml`.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "h6.md", extra="claimed_by_name: claude-klabauter-stale\n",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-nameless")
        _set_self_record(monkeypatch, None)
        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup",
            lambda _sid: None,
            raising=False,
        )

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-nameless" in text
        assert "claude-klabauter-stale" not in text
        assert "claimed_by_name" not in text

    def test_a_raising_lookup_leaves_an_existing_name_untouched(self, tmp_path, monkeypatch):
        """Finding 1/5, P1: a `harness_registry.lookup` RAISE is "cannot tell",
        not "this claimant genuinely has no name" — the two must not collapse
        into the same removal branch. The sibling test above (`lookup` returning
        `None` cleanly) cannot catch this: it never distinguishes a raise from a
        clean miss, which is exactly the gap the destructive removal arm had.
        Pinned against the unfixed behaviour: it deletes the field."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "h6b.md", extra="claimed_by_name: claude-klabauter-real\n",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-registry-down")
        _set_self_record(monkeypatch, None)

        def _boom(_sid):
            raise RuntimeError("registry unreachable")

        monkeypatch.setattr(
            "coordinator_core.session.harness_registry.lookup", _boom, raising=False
        )

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-registry-down" in text
        # A transient registry error must leave the pre-existing name alone —
        # deleting it here would strand exactly the identity this stamp exists
        # to preserve, on nothing more than a momentary read failure.
        assert "claimed_by_name: claude-klabauter-real" in text

    def test_foreign_ambient_record_resolves_the_claimant_by_id(self, tmp_path, monkeypatch):
        """The warm-door shape: the carried id is the caller's, the ambient record is
        the server owner's. The claimant's own record still resolves by id, so the
        right name lands — omitting would blank the field on most claims."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h-foreign.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-caller")
        _set_self_record(monkeypatch, ("sess-server-owner", _record("peer-name", _SOCKET)))
        monkeypatch.setattr(
            harness_registry,
            "lookup",
            lambda sid: _record(_NAME, _SOCKET) if sid == "sess-caller" else None,
        )

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-caller" in text
        # The CLAIMANT is named, resolved by id — not the server owner, and not blank.
        assert f"claimed_by_name: {_NAME}" in text
        assert "peer-name" not in text


class TestMemoClaimStampsIdentity:
    def test_name_lands_beside_an_unchanged_picked_up_by(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m1.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_memo_stamp(str(mp))

        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "picked_up_by: sess-abc" in text
        assert f"claimed_by_name: {_NAME}" in text

    def test_foreign_ambient_record_resolves_the_claimant_by_id(self, tmp_path, monkeypatch):
        """Same warm-door shape on the memo path — the one the cross-repo report was
        actually observed on."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m-foreign.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-caller")
        _set_self_record(monkeypatch, ("sess-server-owner", _record("peer-name", _SOCKET)))
        monkeypatch.setattr(
            harness_registry,
            "lookup",
            lambda sid: _record(_NAME, _SOCKET) if sid == "sess-caller" else None,
        )

        rc = arstamp.cs_claim_memo_stamp(str(mp))

        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "picked_up_by: sess-caller" in text
        assert f"claimed_by_name: {_NAME}" in text
        assert "peer-name" not in text


class TestTheRemovedAddressField:
    def test_the_address_field_is_not_reintroduced(self, tmp_path, monkeypatch):
        """Kira, 2026-08-30: `claimed_by_address` had zero consumers and its
        staleness-comparison rationale did not hold — the fresh
        `resolve_address(claimed_by)` decides liveness on its own, so the stamped
        operand contributed nothing. Reintroducing it would re-reverse a
        negative-spec whose named failure is messaging an unrelated session that
        inherited a recycled socket. A future stamp needs a consumer a bare UUID
        cannot serve; this pins the absence until there is one."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h-no-addr.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert f"claimed_by_name: {_NAME}" in text
        assert "claimed_by_address" not in text
        assert _SOCKET not in text

    def test_both_legs_unresolvable_omits_the_field(self, tmp_path, monkeypatch):
        """The established degrade survives the fallback: an ambient record that is
        not the claimant AND no registry entry for the claimant leaves the field
        unset, never a peer's name and never an empty key."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m-unresolvable.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-caller")
        _set_self_record(monkeypatch, ("sess-server-owner", _record("peer-name", _SOCKET)))
        monkeypatch.setattr(harness_registry, "lookup", lambda sid: None)

        rc = arstamp.cs_claim_memo_stamp(str(mp))

        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "picked_up_by: sess-caller" in text
        assert "claimed_by_name" not in text
        assert "peer-name" not in text


class TestBestEffortContractHolds:
    """Two properties the 2026-08-30 correctness pass found asserted in prose and
    covered by no test."""

    def test_raising_operating_person_resolver_does_not_abort_the_claim(self, tmp_path, monkeypatch):
        """P1. `resolve_operating_person()` sat ahead of every try/except. Both
        call sites invoke this stamp as the LAST statement, after the claim
        transition has already been written to disk — so a raise here meant the
        claim landed AND the caller raised, which is exactly the "no failure path
        aborts the caller's claim" property the contract promises. Pre-existing in
        the pre-fold code; the fold is what put it in scope."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h-boom-human.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")

        def _boom():
            raise RuntimeError("person resolver failed")

        monkeypatch.setattr(arstamp, "resolve_operating_person", _boom)
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "status: claimed" in text
        assert "human_claimant" not in text
        # The independently-resolved field still lands — one resolver failing
        # must not take the other down with it.
        assert f"claimed_by_name: {_NAME}" in text

    def test_written_key_order_follows_anchors_not_the_fields_list(self, tmp_path, monkeypatch):
        """P2. The fold collapsed two sequential `locked_rmw` passes into one
        field list, and inverting the relative order was the silent failure that
        could have caused. Every other assertion here is a substring check, which
        cannot see ordering at all.

        WHAT THIS ACTUALLY PINS, which is not what the finding assumed. The
        reviewer traced list-append order (`human_claimant` first) and concluded
        the file order matched. It does not: written order follows each field's
        ANCHOR, not its position in `fields`. `claimed_by_name` anchors on
        `claimed_by` and lands right after it; `human_claimant` anchors on
        `picked_up_by`, which a handoff does not carry at all, so it appends at
        the end. `claimed_by_name` therefore precedes `human_claimant` in the
        file while following it in the list.

        That is NOT a regression — the pre-fold two-pass code resolved the same
        anchors and produced the same bytes. It means the fold was safe for a
        reason other than the one the trace gave, and a list-order assertion
        would have passed for the wrong reason or failed for no reason."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h-order.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {"github": "octocat"})
        _set_self_record(monkeypatch, ("sess-abc", _record(_NAME, _SOCKET)))

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert text.index("claimed_by_name:") < text.index("human_claimant:")


class TestForeignLiveClaimRefusal:
    """`cs_claim_handoff` refuses a baton held by a DIFFERENT LIVE session.

    The mutual-exclusion check lived only on the `pickup-assemble brief` path,
    and brief also fires the pickup-supersede defect whenever the caller already
    holds any other claim. So a session reasoning correctly about that defect
    routes around brief, reaches `claim-handoff`, and silently takes a live
    peer's baton. The safe-looking door was the unchecked one.
    """

    def test_a_live_foreign_holder_is_refused(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h7.md", extra="claimed_by: sess-peer\n")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-mine")
        monkeypatch.setattr(
            "coordinator_core.session.liveness.session_live",
            lambda sid, cwd=None: sid == "sess-peer",
            raising=False,
        )

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 1
        # The refusal must not have mutated anything on the way out.
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-peer" in text
        assert "sess-mine" not in text

    def test_a_dead_foreign_holder_is_not_refused(self, tmp_path, monkeypatch):
        """Takeover of a DEAD holder is the ordinary recovery path. Refusing it
        would strand every baton whose holder exited uncleanly, so only a
        confirmed-live holder blocks."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h8.md", extra="claimed_by: sess-dead\n")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-mine")
        monkeypatch.setattr(
            "coordinator_core.session.liveness.session_live",
            lambda sid, cwd=None: False,
            raising=False,
        )

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        assert "claimed_by: sess-mine" in hp.read_text(encoding="utf-8")

    def test_an_indeterminate_liveness_read_fails_open(self, tmp_path, monkeypatch):
        """A liveness error must not make claiming impossible — a blocked claim
        is a worse failure than the contention this detects, which the operator
        can still see in the frontmatter."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h9.md", extra="claimed_by: sess-unknown\n")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-mine")

        def _boom(sid, cwd=None):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(
            "coordinator_core.session.liveness.session_live", _boom, raising=False
        )

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0

    def test_a_non_utf8_handoff_fails_open_instead_of_crashing(self, tmp_path, monkeypatch):
        """Finding 2, P1. `UnicodeDecodeError` is a `ValueError` subclass, not an
        `OSError` — a bare `except OSError` around `_foreign_live_holder_refusal`'s
        fence-bounded read let a non-UTF-8 handoff crash the caller outright
        instead of failing open, as the function's own docstring promises ("an
        unparseable frontmatter ... must not make claiming impossible").

        Exercises `_foreign_live_holder_refusal` directly rather than through
        `cs_claim_handoff`: the full claim path's own frontmatter MUTATION read
        (`handoff_transition._claim` -> `locked_write.locked_rmw`) opens with
        plain `encoding="utf-8"` and would itself raise on a corrupt handoff —
        a separate, pre-existing gap on an unrelated read path, out of this
        finding's scope (the module docstring's Negative-spec: this module does
        not reimplement frontmatter-mutation logic). This test isolates the one
        read this finding actually fixed.

        Pinned against the unfixed behaviour: it raises `UnicodeDecodeError`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h-badenc.md", extra="claimed_by: sess-peer\n")
        raw = hp.read_bytes()
        hp.write_bytes(raw + b"\xff\xfe garbage\n")

        result = arstamp._foreign_live_holder_refusal(str(hp), "sess-mine")

        assert result is None

    def test_reclaiming_my_own_live_claim_is_not_contention(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h10.md", extra="claimed_by: sess-mine\n")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-mine")
        monkeypatch.setattr(
            "coordinator_core.session.liveness.session_live",
            lambda sid, cwd=None: True,
            raising=False,
        )

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
