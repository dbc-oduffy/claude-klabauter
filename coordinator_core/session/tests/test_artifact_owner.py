"""
coordinator_core.session.tests.test_artifact_owner — fixture-based tests for
the artifact-keyed "who's on this?" read.

Spec backlink: `state/handoffs/2026-08-13-live-peer-roster.md`
§ "What this covers" amendment (L52-62).
Spec backlink (claim_dir convention): `docs/plans/2026-08-13-claim-dir-owners
-artifact-owner-answers.md` C1/C2, AC1-AC5/AC7.

Never depends on this machine's real live peer list — every
`reachability.resolve_address` call is monkeypatched or driven by a
`harness_registry.snapshot()` fixture, same discipline as
`coordinator_core/session/tests/test_peer_roster.py`. Likewise, `claim_dir`
coverage below never reads the LIVE `.git/coordinator-sessions/` (this
machine runs 50-70 concurrent sessions — a test reading live claims is a
flake generator) — every claim dir and session record is built in a fresh
`tmp_path` git repo, mirroring `test_claims.py`/`test_stale_claims.py`'s own
`_make_repo`/`_write_session` fixtures, with `cwd` passed explicitly through
every call under test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core.session import artifact_owner
from coordinator_core.session import claims
from coordinator_core.session import core
from coordinator_core.session import harness_registry as hr
from coordinator_core.session import reachability
from coordinator_core.win_portability import no_console_passthrough_kwargs

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _record(name, socket):
    return hr.RegistryRecord(
        pid=1, start_epoch=1000.0, cwd="/repo", name=name, messaging_socket_path=socket
    )


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _write_session(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


def _write_claim_dir(repo, class_, basename, sid, stage=claims.CLAIM_STAGE_APPLY, pid="123"):
    cdir = Path(repo) / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "session_id").write_text(f"{sid}\n", encoding="utf-8")
    (cdir / "pid").write_text(f"{pid}\n", encoding="utf-8")
    (cdir / "claimed_at").write_text(f"{core.now_iso()}\n", encoding="utf-8")
    (cdir / "stage").write_text(f"{stage}\n", encoding="utf-8")
    return cdir


def test_extract_owners_scalar_fields():
    text = (
        "---\n"
        "claimed_by: sid-claimer\n"
        "authoring_session: sid-author\n"
        "created_by_session: sid-creator\n"
        "---\n\nbody\n"
    )
    owners = artifact_owner.extract_owners("state/handoffs/x.md", text)
    by_field = {o.source_field: o.session_id for o in owners}
    assert by_field == {
        "claimed_by": "sid-claimer",
        "authoring_session": "sid-author",
        "created_by_session": "sid-creator",
    }
    # claimed_by leads, per the module's documented fixed ordering.
    assert owners[0].source_field == "claimed_by"


def test_extract_owners_claimed_by_and_authoring_session_both_surface_with_provenance():
    text = (
        "---\n"
        "claimed_by: sid-current-holder\n"
        "authoring_session: sid-original-author\n"
        "---\n\nbody\n"
    )
    owners = artifact_owner.extract_owners("state/handoffs/x.md", text)
    assert len(owners) == 2
    assert owners[0] == artifact_owner.OwnerRecord("sid-current-holder", "claimed_by")
    assert owners[1] == artifact_owner.OwnerRecord("sid-original-author", "authoring_session")
    # The two owner ids are distinct — both must surface, neither dropped.
    assert owners[0].session_id != owners[1].session_id


def test_extract_owners_agent_sessions_entries():
    text = (
        "---\n"
        "agent_sessions:\n"
        '  - "sid-one|working|2026-08-05T16:03:58Z"\n'
        '  - "sid-two|done|2026-08-06T00:00:00Z"\n'
        "---\n\nbody\n"
    )
    owners = artifact_owner.extract_owners("state/handoffs/x.md", text)
    assert [o.session_id for o in owners] == ["sid-one", "sid-two"]
    assert all(o.source_field == "agent_sessions" for o in owners)


def test_extract_owners_subagent_share_dir_convention():
    text = "---\ntitle: x\n---\n\nbody\n"
    owners = artifact_owner.extract_owners(
        "state/subagent-share/abc-123-def/coordinatorexecutor-xyz.md", text
    )
    assert owners == [artifact_owner.OwnerRecord("abc-123-def", "subagent_share_dir")]


def test_extract_owners_no_owner_field_returns_empty():
    text = "---\ntitle: x\n---\n\nbody\n"
    owners = artifact_owner.extract_owners("state/handoffs/x.md", text)
    assert owners == []


def test_resolve_artifact_owner_missing_file_sets_file_error(tmp_path):
    missing = tmp_path / "does-not-exist.md"
    result = artifact_owner.resolve_artifact_owner(str(missing))
    assert result.owners == []
    assert result.file_error is not None


def test_resolve_artifact_owner_no_owner_field_is_empty_without_file_error(tmp_path):
    f = tmp_path / "artifact.md"
    f.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")
    result = artifact_owner.resolve_artifact_owner(str(f))
    assert result.owners == []
    assert result.file_error is None


def test_resolve_artifact_owner_passes_through_each_resolver_outcome(tmp_path, monkeypatch):
    f = tmp_path / "artifact.md"
    f.write_text(
        "---\nclaimed_by: sid-reachable\nauthoring_session: sid-dead\n---\n\nbody\n",
        encoding="utf-8",
    )

    def fake_resolve(owner_id):
        if owner_id == "sid-reachable":
            return reachability.ResolveResult(outcome="reachable", session_id="sid-reachable", address="peer-1")
        return reachability.ResolveResult(outcome="not_reachable")

    monkeypatch.setattr(reachability, "resolve_address", fake_resolve)

    result = artifact_owner.resolve_artifact_owner(str(f))
    assert len(result.owners) == 2
    by_field = {r.owner.source_field: r for r in result.owners}
    assert by_field["claimed_by"].result.outcome == "reachable"
    assert by_field["claimed_by"].result.address == "peer-1"
    assert by_field["authoring_session"].result.outcome == "not_reachable"


def test_resolve_artifact_owner_ambiguous_outcome_passes_through(tmp_path, monkeypatch):
    f = tmp_path / "artifact.md"
    f.write_text("---\nclaimed_by: sid-prefix\n---\n\nbody\n", encoding="utf-8")

    candidates = [
        reachability.Candidate("sid-prefix-1", "n1", "r1", "n1"),
        reachability.Candidate("sid-prefix-2", "n2", "r2", "n2"),
    ]

    def fake_resolve(owner_id):
        return reachability.ResolveResult(outcome="ambiguous", candidates=candidates)

    monkeypatch.setattr(reachability, "resolve_address", fake_resolve)

    result = artifact_owner.resolve_artifact_owner(str(f))
    assert result.owners[0].result.outcome == "ambiguous"
    assert result.owners[0].result.candidates == candidates


def test_not_reachable_is_not_representable_as_unowned(tmp_path, monkeypatch):
    """The single most important behaviour: a recorded-but-dead owner must
    never collapse into the same shape as 'no owner recorded'."""
    f_owned = tmp_path / "owned.md"
    f_owned.write_text("---\nclaimed_by: sid-dead\n---\n\nbody\n", encoding="utf-8")
    f_unowned = tmp_path / "unowned.md"
    f_unowned.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")

    monkeypatch.setattr(
        reachability, "resolve_address", lambda oid: reachability.ResolveResult(outcome="not_reachable")
    )

    owned_result = artifact_owner.resolve_artifact_owner(str(f_owned))
    unowned_result = artifact_owner.resolve_artifact_owner(str(f_unowned))

    # An unowned artifact has NO owners entries at all.
    assert unowned_result.owners == []
    # A not_reachable owner is still a RECORDED owner — one entry present,
    # with the outcome explicitly "not_reachable", never absent/omitted as
    # if the artifact carried no claim.
    assert len(owned_result.owners) == 1
    assert owned_result.owners[0].result.outcome == "not_reachable"
    assert owned_result.owners[0].owner.session_id == "sid-dead"
    # The two results must not be structurally identical.
    assert owned_result.owners != unowned_result.owners


# ---------------------------------------------------------------------------
# claim_dir convention (AC1-AC5, AC7) — sixth owner-extraction convention,
# keyed on the artifact's basename across all three claim classes.
# ---------------------------------------------------------------------------


def test_claim_dir_live_claim_resolves_to_holder_and_address(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _write_session(repo, "sid-live-holder", {"pid": "111", "last_activity": core.now_iso()})
    f = repo / "cross-repo" / "inbox" / "2026-08-13-some-memo.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")
    _write_claim_dir(repo, "memo", "2026-08-13-some-memo.md", "sid-live-holder")

    monkeypatch.setattr(
        reachability,
        "resolve_address",
        lambda oid: reachability.ResolveResult(outcome="reachable", session_id=oid, address="peer-7"),
    )

    result = artifact_owner.resolve_artifact_owner(str(f), cwd=str(repo))

    claim_owners = [r for r in result.owners if r.owner.source_field == "claim_dir"]
    assert len(claim_owners) == 1
    resolution = claim_owners[0]
    assert resolution.owner.session_id == "sid-live-holder"
    assert resolution.owner.claim_live is True
    assert resolution.owner.claim_stage == claims.CLAIM_STAGE_APPLY
    assert resolution.result.outcome == "reachable"
    assert resolution.result.address == "peer-7"


def test_claim_dir_stale_claim_is_recorded_owner_not_empty(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _write_session(repo, "sid-dead-holder", {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"})
    basename = "2026-08-10-stale-handoff.md"
    f = repo / "state" / "handoffs" / basename
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\ntitle: x\n---\n\nbody\n", encoding="utf-8")
    _write_claim_dir(repo, "handoff", basename, "sid-dead-holder")

    monkeypatch.setattr(
        reachability, "resolve_address", lambda oid: reachability.ResolveResult(outcome="not_reachable")
    )

    result = artifact_owner.resolve_artifact_owner(str(f), cwd=str(repo))

    claim_owners = [r for r in result.owners if r.owner.source_field == "claim_dir"]
    assert len(claim_owners) == 1
    resolution = claim_owners[0]
    # Two distinct signals: the claim dir itself reads dead (claim_live),
    # AND resolve_address reads not_reachable -- neither collapses into the
    # other, and the owner is NEVER dropped to an empty owners list.
    assert resolution.owner.claim_live is False
    assert resolution.result.outcome == "not_reachable"
    assert resolution.owner.session_id == "sid-dead-holder"


def test_claim_dir_covers_all_three_classes(tmp_path):
    """Each class is probed with the key its OWN writer uses.

    `claims.claim_plan` refuses a `.md`-suffixed slug outright, so a plan
    claim dir on disk is stem-keyed while handoff/memo dirs keep the
    extension. The fixture mirrors the writers rather than assuming one
    shared spelling -- an earlier revision wrote all three with `.md` and so
    could not observe that the reader missed every real plan claim.
    """
    repo = _make_repo(tmp_path)
    for class_, sid, key in (
        ("handoff", "sid-h", "same-basename.md"),
        ("memo", "sid-m", "same-basename.md"),
        ("plan", "sid-p", "same-basename"),
    ):
        _write_claim_dir(repo, class_, key, sid)

    owners = artifact_owner._extract_claim_dir_owners(
        "state/handoffs/same-basename.md", cwd=str(repo)
    )

    assert [o.session_id for o in owners] == ["sid-h", "sid-m", "sid-p"]
    assert all(o.source_field == "claim_dir" for o in owners)


def test_plan_claim_dir_is_stem_keyed_not_basename_keyed(tmp_path):
    """A plan claimed through `claims.claim_plan`'s own convention resolves.

    Regression pin: the reader probed all three classes with the artifact's
    full `.md` basename, so no plan claim dir ever matched and the "who owns
    this plan?" read returned zero owners for a plan that was demonstrably
    claimed. A `.md`-keyed plan dir is NOT probed -- `claim_plan` cannot
    create one.
    """
    repo = _make_repo(tmp_path)
    _write_claim_dir(repo, "plan", "2026-08-19-some-plan", "sid-plan")

    owners = artifact_owner._extract_claim_dir_owners(
        "docs/plans/2026-08-19-some-plan.md", cwd=str(repo)
    )

    assert [o.session_id for o in owners] == ["sid-plan"]
    assert owners[0].source_field == "claim_dir"


def test_claim_dir_no_claim_leaves_owners_unchanged(tmp_path):
    repo = _make_repo(tmp_path)
    f = repo / "state" / "handoffs" / "unclaimed.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\nclaimed_by: sid-x\n---\n\nbody\n", encoding="utf-8")

    owners = artifact_owner.extract_owners(str(f), f.read_text(encoding="utf-8"), cwd=str(repo))

    assert [o.source_field for o in owners] == ["claimed_by"]


def test_claim_dir_resolves_without_frontmatter_or_a_readable_file(tmp_path):
    """AC5: the claim lookup does not sit behind frontmatter parsing
    succeeding — a missing artifact still resolves its claim-dir owner."""
    repo = _make_repo(tmp_path)
    basename = "2026-08-13-no-frontmatter-here.md"
    _write_claim_dir(repo, "memo", basename, "sid-frontmatterless")
    missing_path = str(Path(repo) / "cross-repo" / "inbox" / basename)  # never written to disk

    result = artifact_owner.resolve_artifact_owner(missing_path, cwd=str(repo))

    assert result.file_error is not None
    claim_owners = [r for r in result.owners if r.owner.source_field == "claim_dir"]
    assert len(claim_owners) == 1
    assert claim_owners[0].owner.session_id == "sid-frontmatterless"


def test_claim_dir_and_claimed_by_both_surface_in_ac4_order(tmp_path):
    repo = _make_repo(tmp_path)
    basename = "2026-08-13-double-owner.md"
    f = repo / "state" / "handoffs" / basename
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\nclaimed_by: sid-frontmatter-holder\n---\n\nbody\n", encoding="utf-8")
    _write_claim_dir(repo, "handoff", basename, "sid-claim-dir-holder")

    owners = artifact_owner.extract_owners(str(f), f.read_text(encoding="utf-8"), cwd=str(repo))

    assert [(o.source_field, o.session_id) for o in owners] == [
        ("claimed_by", "sid-frontmatter-holder"),
        ("claim_dir", "sid-claim-dir-holder"),
    ]


def test_claim_dir_finds_owner_for_a_backslash_separated_path(tmp_path):
    """A Windows-style artifact path handed to a POSIX-running engine must
    still resolve its claim-dir owner -- `os.path.basename` alone would
    silently miss (Windows is first-class; module's own
    `_SUBAGENT_SHARE_DIR_RE` precedent already handles both separators)."""
    repo = _make_repo(tmp_path)
    basename = "2026-08-13-windows-path-handoff.md"
    _write_claim_dir(repo, "handoff", basename, "sid-windows-holder")

    owners = artifact_owner._extract_claim_dir_owners(
        f"state\\handoffs\\{basename}", cwd=str(repo)
    )

    assert [o.session_id for o in owners] == ["sid-windows-holder"]


def test_claimed_by_with_quoted_yaml_scalar_resolves_unquoted_session_id(tmp_path):
    """A quoted YAML scalar owner (`claimed_by: "sid-quoted"`) must resolve
    to `sid-quoted`, not the literal `"sid-quoted"` with quote marks intact
    -- the quoted form previously read back with its quotes still attached
    and so resolved a live session as unreachable (regression, pre-existing
    scalar-extraction path, found while dogfooding the CLI)."""
    repo = _make_repo(tmp_path)
    f = repo / "state" / "handoffs" / "quoted-owner.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('---\nclaimed_by: "sid-quoted"\n---\n\nbody\n', encoding="utf-8")

    owners = artifact_owner.extract_owners(str(f), f.read_text(encoding="utf-8"), cwd=str(repo))

    claimed_by_owners = [o for o in owners if o.source_field == "claimed_by"]
    assert len(claimed_by_owners) == 1
    assert claimed_by_owners[0].session_id == "sid-quoted"
