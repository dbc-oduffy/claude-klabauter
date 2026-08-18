"""coordinator_core.git.test_commit_trailers -- tier-3 (claimed-plan)
Deliverable-Id resolution coverage for `commit_trailers.py`.

Closes the same-session plan-execute residual named in
`archive/specs/2026-08/2026-08-01-deliverable-id-carry-onto-executing-
handoff.md`'s execution note: a session that claims a PLAN and executes it
WITHOUT a handoff never populates `pickup.deliverable_id`, so tiers 1/1a of
`_resolve_deliverable_id` always miss for that session's chunk commits. This
module pins the new tier-3 fallback (`resolve_claimed_plan_path()` -> the
claimed plan's own `deliverable_id` frontmatter field) added to close that
door, plus a regression test for the import-cycle trap named in
`coordinator_core/session/claimed_plan.py`'s negative-spec.

Spec backlink: DR-207 DD#1; archive/specs/2026-08/2026-08-01-deliverable-id-
carry-onto-executing-handoff.md execution note.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.git.commit_trailers import compute_missing_trailer_args
from coordinator_core.session.claimed_plan import list_held_plan_claims
from coordinator_core.win_portability import no_console_creationflags

# Real git is load-bearing: compute_missing_trailer_args resolves via
# `git rev-parse --git-dir` (coordinator_core.git.repo_root), and the tier-3
# fallback under test reads plan frontmatter through a real
# `.git/coordinator-sessions/<sid>/session-shape.json` on disk -- neither is
# reproducible against a mocked git-dir seam.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "12121212-1212-4121-8121-121212121212"


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _write_shape(repo: Path, sid: str, shape: dict) -> None:
    shape_dir = repo / ".git" / "coordinator-sessions" / sid
    shape_dir.mkdir(parents=True, exist_ok=True)
    (shape_dir / "session-shape.json").write_text(json.dumps(shape), encoding="utf-8")


def _write_plan(repo: Path, rel_path: str, fm_extra: str = 'deliverable_id: "dlv-plan-value"\n') -> None:
    plan_path = repo / rel_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\ntitle: example plan\n{fm_extra}---\n\n# Example plan\n",
        encoding="utf-8",
    )


def _write_plan_with_scope(
    repo: Path,
    rel_path: str,
    deliverable_id: str,
    scope_paths: list,
) -> None:
    """Same shape as ``_write_plan``, plus a ``scope:`` frontmatter block
    (``coordinator_core.ops.extract_scope_paths``'s ``  - <path>`` list
    shape) -- the input the scope-match tier
    (``resolve_deliverable_id_from_scope_match``) reads via
    ``_read_plan_scope_paths``."""
    plan_path = repo / rel_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    scope_block = "\n".join(f"  - {p}" for p in scope_paths)
    plan_path.write_text(
        f"---\ntitle: example plan\ndeliverable_id: \"{deliverable_id}\"\n"
        f"scope:\n{scope_block}\n---\n\n# Example plan\n",
        encoding="utf-8",
    )


def _write_plan_claim(repo: Path, sid: str, plan_stem: str, claimed_at: str) -> None:
    """Write a durable plan-claim record at the exact path
    ``coordinator_core.session.claimed_plan.list_held_plan_claims`` scans:
    ``<repo>/.git/coordinator-sessions/plan-claims/<plan_stem>/{session_id,
    claimed_at}`` (``plan_claim_dir``'s convention -- keyed on the plan's
    filename minus ``.md``). ``list_held_plan_claims`` reports the held plan
    back as ``docs/plans/<plan_stem>.md`` unconditionally, so callers of this
    helper must place their plan fixture at that same path via
    ``_write_plan``/``_write_plan_with_scope``."""
    claim_dir = repo / ".git" / "coordinator-sessions" / "plan-claims" / plan_stem
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


def _msg_file(repo: Path, text: str = "chore: land trailers\n") -> Path:
    p = repo / "MSG"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# (i) pickup tier populated -> unchanged behaviour, claimed-plan tier never
# consulted.
# ---------------------------------------------------------------------------

def test_pickup_tier_wins_claimed_plan_never_consulted(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(
        repo, _SID,
        {
            "pickup": {"deliverable_id": "dlv-from-pickup"},
            "plan": {"path": "docs/plans/should-not-be-read.md"},
        },
    )
    # No plan file written at all -- if the claimed-plan tier were consulted
    # this would raise/omit rather than silently succeed with the wrong id.
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert "--trailer" in args
    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


# ---------------------------------------------------------------------------
# (ii) pickup tier empty + claimed plan resolvable -> the plan's
# deliverable_id is returned.
# ---------------------------------------------------------------------------

def test_claimed_plan_tier_used_when_pickup_empty(tmp_path, monkeypatch):
    """The tier-(a) shape pointer needs a BACKING tier-(b) claim to be honoured.

    Seeding only ``session-shape.json`` used to be enough here, because tier (a)
    answered alone. It no longer is: ``claimed_plan._backed_by_claim`` (landed
    4365117ae) rejects a shape pointer with no claim behind it, on the grounds
    that ``claims.claim_plan`` writes the claim dir unconditionally BEFORE
    attempting the best-effort shape write -- so a pointer outside the claim
    store outlived its own claim and names a shipped or abandoned plan. This
    fixture therefore seeds both rungs, which is the only shape a live claim
    ever actually has on disk.
    """
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/example.md"}})
    _write_plan(repo, "docs/plans/example.md", 'deliverable_id: "dlv-plan-value"\n')
    _write_plan_claim(repo, _SID, "example", "2026-08-13T10:00:00Z")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-plan-value" in joined


# ---------------------------------------------------------------------------
# (iii) pickup empty + no claimed plan -> "", no trailer.
# ---------------------------------------------------------------------------

def test_no_pickup_no_claimed_plan_no_trailer(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    # No session-shape.json at all.
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert "Session-Id:" in joined  # session-id resolution still succeeds


# ---------------------------------------------------------------------------
# (iv) claimed plan resolvable but its deliverable_id absent/null/unreadable
# -> "", no trailer, no crash.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fm_extra",
    [
        "",  # deliverable_id absent entirely
        "deliverable_id: null\n",  # literal null
        "deliverable_id: \n",  # blank scalar
    ],
    ids=["absent", "null", "blank"],
)
def test_claimed_plan_missing_deliverable_id_no_crash(tmp_path, monkeypatch, fm_extra):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/example.md"}})
    _write_plan(repo, "docs/plans/example.md", fm_extra)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


def test_claimed_plan_file_missing_no_crash(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/does-not-exist.md"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


# ---------------------------------------------------------------------------
# (v) regression: the deferred import must NOT be hoisted to module scope --
# importing coordinator_core.git.commit_trailers, then forcing the full ops
# eager sweep, must leave all four ops the execution note named still
# registered. Run in a subprocess for a clean module cache (importing
# coordinator_core.ops even once in-process is enough to poison a shared
# session for any other test relying on registry state).
# ---------------------------------------------------------------------------

def test_ops_registry_survives_commit_trailers_import():
    script = (
        "import coordinator_core.git.commit_trailers\n"
        "import coordinator_core.ops as ops\n"
        "ops._eager_import_all()\n"
        "from coordinator_core.ipc import get_op_handler\n"
        "names = ['handoff.normalize', 'handoff.author_fork', "
        "'handoff.correct_body', 'handoff.scaffold_from_queue']\n"
        "missing = [n for n in names if get_op_handler(n) is None]\n"
        "assert not missing, missing\n"
        "print('OK')\n"
    )
    cp = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    assert "OK" in cp.stdout


# ---------------------------------------------------------------------------
# (vi) tier-0 (artifact-first) coverage -- 2026-08-04 cross-repo memo,
# defect 2: a multi-baton session's commits must each resolve to the
# COMMITTED ARTIFACT's own deliverable_id, not the session's (last-write-
# wins) pickup record.
# ---------------------------------------------------------------------------

def _write_handoff(repo: Path, rel_path: str, deliverable_id: str) -> None:
    _write_plan(repo, rel_path, f'deliverable_id: "{deliverable_id}"\n')


def test_artifact_tier_resolves_each_of_three_commits_independently(tmp_path, monkeypatch):
    """The exact reported scenario: one session, three commits, three
    artifacts with three different deliverable_id values -> three
    DIFFERENT correct trailers, regardless of which pickup claim the
    session-shape record resolved last."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    # Session-shape pickup tier resolves to a THIRD, unrelated value --
    # simulating the reported "last-claim-wins" session state. Tier 0 must
    # win over it whenever a pathspec is supplied.
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-qsent-03"}})

    _write_handoff(repo, "state/handoffs/roadmap-qsent-02.md", "dlv-qsent-02")
    _write_handoff(repo, "state/handoffs/roadmap-qsent-03.md", "dlv-qsent-03")
    _write_handoff(repo, "state/handoffs/qsent-05-rebase.md", "dlv-qsent-05")

    for rel_path, expected in (
        ("state/handoffs/roadmap-qsent-02.md", "dlv-qsent-02"),
        ("state/handoffs/roadmap-qsent-03.md", "dlv-qsent-03"),
        ("state/handoffs/qsent-05-rebase.md", "dlv-qsent-05"),
    ):
        msg = _msg_file(repo, f"archive handoff: {rel_path}\n")
        args = compute_missing_trailer_args(msg, repo, paths=[rel_path])
        joined = " ".join(args)
        assert f"Deliverable-Id: {expected}" in joined, (rel_path, joined)


def test_no_paths_arg_is_byte_identical_to_before_the_fix(tmp_path, monkeypatch):
    """Omitting `paths` entirely (every caller not yet updated to pass its
    own pathspec) must reproduce the pre-fix session-only resolution with
    zero behaviour change -- tier 0 never activates without a pathspec."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


def test_artifact_tier_falls_back_to_session_when_paths_carry_no_deliverable_id(
    tmp_path, monkeypatch
):
    """Re-cut (C3, AC5/AC6): this fixture's session-fallback assertion is
    correct ONLY because the session holds AT MOST ONE plan claim -- when
    ``session_holds_multiple_plan_claims`` is True, this same commit shape
    must OMIT the trailer instead (see
    ``test_two_claims_code_only_commit_omits_deliverable_id_keeps_session_id``
    below). Prior to this re-cut, that precondition held only because the
    fixture wrote zero plan claims at all and the ambiguity gate was never
    exercised -- this version claims exactly ONE plan explicitly, so the
    single-claim precondition is asserted rather than merely accidental."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    # Exactly ONE plan claim held -- the precondition under which the
    # session-keyed pickup tier is even reachable (`session_holds_multiple_
    # plan_claims` is False for a single claim). Its scope deliberately does
    # NOT cover the committed paths below, so the scope-match tier abstains
    # too, exercising the fall-through to the pickup tier specifically.
    _write_plan_with_scope(
        repo, "docs/plans/only-claim.md", "dlv-only-claim", ["some/unrelated/path.md"]
    )
    _write_plan_claim(repo, _SID, "only-claim", "2026-08-01T00:00:00Z")
    assert len(list_held_plan_claims(repo)) == 1, "precondition: exactly one plan claim held"

    # A committed path with no frontmatter at all, and one that does not
    # exist on disk -- neither carries a deliverable_id, so tier 0 must
    # yield nothing and the session tier must still answer.
    (repo / "README.md").write_text("no frontmatter here\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["README.md", "does/not/exist.md"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


# ---------------------------------------------------------------------------
# (vii) multi-claim coverage that did not exist anywhere in the tree (C3,
# AC7): a session holding TWO plan claims and the scope-match tier (C2 § (1),
# `resolve_deliverable_id_from_scope_match`) and ambiguity gate (C2 § (2),
# `session_holds_multiple_plan_claims`) it feeds.
# ---------------------------------------------------------------------------


def test_two_claims_code_only_commit_omits_deliverable_id_keeps_session_id(
    tmp_path, monkeypatch
):
    """AC7: a session holding two plan claims, committing a code-only
    pathspec that falls inside NEITHER plan's scope -- no tier can
    disambiguate, so the ambiguity gate fires: NO Deliverable-Id trailer is
    emitted, while Session-Id still is (the gate is scoped to
    Deliverable-Id resolution only, never Session-Id)."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["docs/plans/claim-a-scope.md"]
    )
    _write_plan_with_scope(
        repo, "docs/plans/claim-b.md", "dlv-claim-b", ["docs/plans/claim-b-scope.md"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")
    _write_plan_claim(repo, _SID, "claim-b", "2026-08-02T00:00:00Z")
    assert len(list_held_plan_claims(repo)) == 2, "precondition: two plan claims held"

    (repo / "some_code.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["some_code.py"])

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def test_two_claims_scope_match_covered_by_exactly_one_plan_wins(tmp_path, monkeypatch):
    """Scope-match tier (C2 § (1)): two claims held, the committed pathspec
    is strictly covered by exactly ONE plan's ``scope:`` -- that plan's own
    ``deliverable_id`` wins, even though the session holds multiple claims
    (the scope-match tier runs BEFORE the ambiguity gate)."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["src/only_in_a.py"]
    )
    _write_plan_with_scope(
        repo, "docs/plans/claim-b.md", "dlv-claim-b", ["src/only_in_b.py"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")
    _write_plan_claim(repo, _SID, "claim-b", "2026-08-02T00:00:00Z")

    (repo / "src").mkdir()
    (repo / "src" / "only_in_a.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["src/only_in_a.py"])

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-claim-a" in joined


def test_two_claims_scope_match_covered_by_both_plans_omits(tmp_path, monkeypatch):
    """Scope-match ambiguity: two claims whose scopes BOTH cover the
    committed pathspec -- the scope-match tier abstains (never picks among
    multiple covering plans), and since the session holds more than one
    claim, the ambiguity gate then omits the trailer entirely rather than
    falling through to a session-keyed tier."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["src/shared.py"]
    )
    _write_plan_with_scope(
        repo, "docs/plans/claim-b.md", "dlv-claim-b", ["src/shared.py"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")
    _write_plan_claim(repo, _SID, "claim-b", "2026-08-02T00:00:00Z")

    (repo / "src").mkdir()
    (repo / "src" / "shared.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["src/shared.py"])

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def test_artifact_tier_raises_on_genuinely_divergent_multi_artifact_commit(
    tmp_path, monkeypatch
):
    """Two paths in the SAME commit naming two DIFFERENT non-empty
    deliverable_id values is not guessed at -- fails loud, matching the
    posture `coordinator_core.ops.deliverable_carry.DivergentDeliverableIdError`
    already established for its own plan-vs-predecessor divergent join."""
    from coordinator_core.ops.deliverable_carry import DivergentDeliverableIdError

    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_handoff(repo, "state/handoffs/a.md", "dlv-a")
    _write_handoff(repo, "state/handoffs/b.md", "dlv-b")
    msg = _msg_file(repo)

    with pytest.raises(DivergentDeliverableIdError):
        compute_missing_trailer_args(
            msg, repo, paths=["state/handoffs/a.md", "state/handoffs/b.md"]
        )


def test_declared_fork_pair_stamps_a_raw_id_never_the_synthesized_canonical_winner(
    tmp_path, monkeypatch
):
    """C6b/AC11 pin (review-integrator P1, coordinatorcode-reviewer-0f04f47d.md):
    two staged artifacts carrying a DECLARED fork pair's loser and winner raw
    ids collapse to one canonical id for the equality check (no
    DivergentDeliverableIdError), but the trailer this stamps must be a RAW
    value some staged artifact actually carries -- never the synthesized
    canonical winner conjured purely by the join. The raw value is chosen
    deterministically by sorted repo-relative path, so the loser's path
    (sorted first) always wins here."""
    from coordinator_core.ops.deliverable_equivalence import _reset_equivalence_map_cache

    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)

    state_dir = repo / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "deliverable-equivalence.yaml").write_text(
        "entries:\n"
        "  - loser: dlv-loser\n"
        "    winner: dlv-winner\n"
        "    adjudicated_at: \"2026-08-10T00:00:00Z\"\n",
        encoding="utf-8",
    )
    _write_handoff(repo, "state/handoffs/aa-loser.md", "dlv-loser")
    _write_handoff(repo, "state/handoffs/bb-winner.md", "dlv-winner")
    msg = _msg_file(repo)

    _reset_equivalence_map_cache()
    try:
        args = compute_missing_trailer_args(
            msg,
            repo,
            paths=["state/handoffs/aa-loser.md", "state/handoffs/bb-winner.md"],
        )
    finally:
        _reset_equivalence_map_cache()

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-loser" in joined
    assert "dlv-winner" not in joined


# ---------------------------------------------------------------------------
# Sentinel tier REMOVED (KS-1, 2026-08-07): a `.current-session-id` sentinel,
# live or stale/well-formed, must now be ignored entirely -- resolution
# returns "" and NO trailers are stamped when neither env tier is set. Env
# tiers 1/2 are unaffected and still win outright.
# ---------------------------------------------------------------------------

_SENTINEL_SID = "99887766-1122-4334-8ee5-aabbccddeeff"


def _write_sentinel(repo: Path, sid: str) -> None:
    sentinel_dir = repo / ".git" / "coordinator-sessions"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    (sentinel_dir / ".current-session-id").write_text(sid, encoding="utf-8")


def _write_session_meta(repo: Path, sid: str, meta: dict) -> None:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_sentinel_stale_session_ignored_no_trailers(tmp_path, monkeypatch):
    """A sentinel naming a session whose last_activity is far in the past
    (stale) is ignored entirely -- no Session-Id, no Deliverable-Id."""
    import datetime as _dt

    repo = _init_repo(tmp_path)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _write_sentinel(repo, _SENTINEL_SID)
    stale_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
    stale_iso = stale_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_session_meta(
        repo, _SENTINEL_SID, {"pid": "999999", "last_activity": stale_iso}
    )
    _write_shape(repo, _SENTINEL_SID, {"pickup": {"deliverable_id": "dlv-stale"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert args == []


def test_sentinel_live_session_still_ignored_no_trailers(tmp_path, monkeypatch):
    """A sentinel naming a session with RECENT (live-looking) last_activity
    is STILL ignored -- the tier is gone, not merely liveness-gated."""
    import datetime as _dt

    repo = _init_repo(tmp_path)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _write_sentinel(repo, _SENTINEL_SID)
    fresh_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_session_meta(
        repo, _SENTINEL_SID, {"pid": "999999", "last_activity": fresh_iso}
    )
    _write_shape(repo, _SENTINEL_SID, {"pickup": {"deliverable_id": "dlv-live"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert args == []


def test_sentinel_no_session_dir_no_trailers(tmp_path, monkeypatch):
    """A sentinel naming a session with NO session directory at all (never
    initialized, or already reaped) is also ignored -- "" throughout."""
    repo = _init_repo(tmp_path)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _write_sentinel(repo, _SENTINEL_SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert args == []


# ---------------------------------------------------------------------------
# (viii) `_has_trailer_line` must agree with git's own trailer parser --
# state/bug-backlog/2026-08-10-a-hand-written-deliverable-id-line-in-th-
# b56f7e4630fe.yaml: a hand-written `Deliverable-Id:` line sitting in the
# message BODY (separated from the trailing trailer block by a blank line)
# must NOT suppress the engine's own emission.
# ---------------------------------------------------------------------------


def _git_trailer_value(repo: Path, msg_file: Path, key: str) -> str:
    cp = subprocess.run(
        ["git", "interpret-trailers", "--parse", str(msg_file)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    for line in cp.stdout.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


def test_body_mention_does_not_suppress_emission(tmp_path, monkeypatch):
    """A hand-written `Deliverable-Id:` line in the message BODY (blank line
    before the real trailing trailer block) must not be treated as "already
    present" -- the engine still emits its own trailer, and git's own
    trailer parser (not a substring check) finds it."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-real"}})
    msg = _msg_file(
        repo,
        "chore: land trailers\n\n"
        "Deliverable-Id: dlv-hand-written-in-body\n\n"
        "Co-Authored-By: Someone <someone@example.com>\n",
    )

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-real" in joined

    # Confirm against git's own parser, not merely our own predicate.
    cp = subprocess.run(
        ["git", "interpret-trailers", *args, str(msg)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    out_msg = tmp_path / "OUT_MSG"
    out_msg.write_text(cp.stdout, encoding="utf-8")
    assert _git_trailer_value(repo, out_msg, "Deliverable-Id") == "dlv-real"


def test_trailer_block_id_still_deduplicates(tmp_path, monkeypatch):
    """A `Deliverable-Id` already correctly present INSIDE the trailing
    trailer block must not be emitted twice."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-real"}})
    msg = _msg_file(
        repo,
        "chore: land trailers\n\n"
        "Deliverable-Id: dlv-real\n"
        "Co-Authored-By: Someone <someone@example.com>\n",
    )

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


# ---------------------------------------------------------------------------
# (ix) `Absorbed-From` derivation (C1/C4, docs/plans/2026-08-16-authorship-
# survives-the-sweep.md) -- reproduction of the `e7360c2c5` shape: a path in
# the committer's pathspec was actually claimed (and authored) by a
# different, live peer session, and the commit must disclose that rather
# than silently crediting the commit's own `Session-Id`.
# ---------------------------------------------------------------------------

_PEER_SID = "peer-session-b1b1b1"


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write_touched(repo: Path, sid: str, lines: list) -> None:
    """Same shape as ``coordinator_core/ops/ceremony/tests/
    test_scoped_git_commit_recent_edit_warn.py``'s helper of the same name
    -- one ``T <iso8601> <path>`` claim-event line per entry."""
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _touch_line(verb: str, path: str, ts: str) -> str:
    return "%s %s %s" % (verb, ts, path)


def _write_live_meta(repo: Path, sid: str, *, live: bool) -> None:
    """``meta.json`` for ``session_liveness.session_live``'s Layer-2 recency
    gate -- ``live=True`` writes a fresh ``last_activity`` (inside the
    30-minute window), ``live=False`` writes one far enough in the past to
    read dead."""
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    from coordinator_core.session import core as _session_core

    last_activity = _session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    (sdir / "meta.json").write_text(
        '{"pid": 1, "last_activity": "%s"}\n' % last_activity,
        encoding="utf-8",
    )


def test_absorbed_from_emitted_for_live_peer_claimant(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    (repo / "hot.py").write_text("x = 1\n", encoding="utf-8")
    _write_touched(repo, _PEER_SID, [_touch_line("T", "hot.py", "2026-08-13T10:00:00Z")])
    _write_live_meta(repo, _PEER_SID, live=True)
    _write_shape(repo, _PEER_SID, {"pickup": {"deliverable_id": "dlv-peer-work"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["hot.py"])

    joined = " ".join(args)
    assert f"Absorbed-From: {_PEER_SID} dlv-peer-work hot.py" in joined


def test_absorbed_from_not_emitted_for_callers_own_claim(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    (repo / "own.py").write_text("x = 1\n", encoding="utf-8")
    _write_touched(repo, _SID, [_touch_line("T", "own.py", "2026-08-13T10:00:00Z")])
    _write_live_meta(repo, _SID, live=True)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-own-work"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["own.py"])

    joined = " ".join(args)
    assert "Absorbed-From:" not in joined


def test_absorbed_from_not_emitted_for_non_live_claimant(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    (repo / "cold.py").write_text("x = 1\n", encoding="utf-8")
    _write_touched(repo, _PEER_SID, [_touch_line("T", "cold.py", "2026-08-13T10:00:00Z")])
    _write_live_meta(repo, _PEER_SID, live=False)
    _write_shape(repo, _PEER_SID, {"pickup": {"deliverable_id": "dlv-peer-work"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["cold.py"])

    joined = " ".join(args)
    assert "Absorbed-From:" not in joined


def test_absorbed_from_not_emitted_for_unclaimed_path(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    (repo / "untouched.py").write_text("x = 1\n", encoding="utf-8")
    # No touched.txt entry anywhere for this path.
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["untouched.py"])

    joined = " ".join(args)
    assert "Absorbed-From:" not in joined


def test_absorbed_from_emitted_even_when_session_and_deliverable_id_already_present(
    tmp_path, monkeypatch
):
    """Trap 1 (anti-scope): a message that already carries `Session-Id` and
    `Deliverable-Id` trailers -- exactly the swept-hunk case -- must NOT
    suppress `Absorbed-From`. The early-return / idempotency checks that
    gate the other two trailers must not gate this one."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    (repo / "hot.py").write_text("x = 1\n", encoding="utf-8")
    _write_touched(repo, _PEER_SID, [_touch_line("T", "hot.py", "2026-08-13T10:00:00Z")])
    _write_live_meta(repo, _PEER_SID, live=True)
    _write_shape(repo, _PEER_SID, {"pickup": {"deliverable_id": "dlv-peer-work"}})
    msg = _msg_file(
        repo,
        "chore: land trailers\n\n"
        f"Session-Id: {_SID}\n"
        "Deliverable-Id: dlv-already-here\n",
    )

    args = compute_missing_trailer_args(msg, repo, paths=["hot.py"])

    joined = " ".join(args)
    assert "Session-Id:" not in joined  # already present -- correctly suppressed
    assert "Deliverable-Id:" not in joined  # already present -- correctly suppressed
    assert f"Absorbed-From: {_PEER_SID} dlv-peer-work hot.py" in joined


def test_absorbed_from_degrades_to_no_trailer_when_index_raises(tmp_path, monkeypatch):
    """Trap 3 (anti-scope): any exception in the derivation -- here, a
    raising `claim_index.lookup()` -- degrades to no `Absorbed-From`
    trailer and must never fail (or even affect) the commit's other
    trailers."""
    from coordinator_core.session import claim_index as claim_index_module

    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    (repo / "hot.py").write_text("x = 1\n", encoding="utf-8")

    def _raise(*a, **kw):
        raise RuntimeError("simulated claim_index failure")

    monkeypatch.setattr(claim_index_module, "lookup", _raise)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["hot.py"])

    joined = " ".join(args)
    assert "Absorbed-From:" not in joined
    # The rest of the commit's trailers are unaffected -- the derivation's
    # own failure never propagates.
    assert f"Session-Id: {_SID}" in joined
    assert "Deliverable-Id: dlv-from-pickup" in joined


def test_absorbed_from_swept_hunk_case_resolves_to_claimant(tmp_path, monkeypatch):
    """The exact incident this closes: committer session A runs the commit,
    but the pathspec's true author is a different, live claimant session
    B -- the record must resolve to B, not to A."""
    session_a = _SID
    session_b = _PEER_SID
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_a)
    (repo / "coordinator_core" / "contract").mkdir(parents=True, exist_ok=True)
    target = "coordinator_core/contract/apply_base.py"
    (repo / target).write_text("x = 1\n", encoding="utf-8")
    _write_touched(repo, session_b, [_touch_line("T", target, "2026-08-15T22:50:38Z")])
    _write_live_meta(repo, session_b, live=True)
    _write_shape(
        repo, session_b,
        {"pickup": {"deliverable_id": "dlv-constantly-warm-engine-to-retire-per-inv-1dd353"}},
    )
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=[target])

    joined = " ".join(args)
    assert (
        f"Absorbed-From: {session_b} "
        f"dlv-constantly-warm-engine-to-retire-per-inv-1dd353 {target}"
    ) in joined
    # Session-Id still names the committer (A), unchanged -- Absorbed-From
    # is additive, never a replacement for it.
    assert f"Session-Id: {session_a}" in joined


# ---------------------------------------------------------------------------
# (x) AC2b (PM ruling, 2026-08-16): identity is compared at OWNER-SESSION
# granularity on BOTH sides. `claim_index` already folds a dispatched
# agent's claims to its owning session via the `.agents/<id>/em-session-
# id.txt` back-pointer; the COMMITTER side must fold the same way before
# the self-comparison, or a dispatched commit-agent committing its own EM's
# claimed path false-positives an `Absorbed-From` onto the EM's own work.
# ---------------------------------------------------------------------------


def _write_agent_owner_pointer(repo: Path, agent_id: str, owner_sid: str) -> None:
    """`<sessions-dir>/.agents/<agent_id>/em-session-id.txt` -- the exact
    back-pointer `claim_index._agent_owner_sid` reads, first line names the
    owning session."""
    agent_dir = _sessions_dir(repo) / ".agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "em-session-id.txt").write_text(owner_sid + "\n", encoding="utf-8")


_COMMIT_AGENT_ID = "agent-9c9c9c9c"


def test_absorbed_from_not_emitted_when_commit_agent_folds_to_owning_em_session(
    tmp_path, monkeypatch
):
    """AC2b pin: `coordinator:git-commit-agent` runs the commit under its
    own agent id, but that agent is back-pointed to EM session `_SID`, which
    is also the path's claimant. Folded to owner-session granularity, both
    sides are `_SID` -- no `Absorbed-From` (this would be a false positive
    on the EM's own team's work if the committer side were left unfolded)."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _COMMIT_AGENT_ID)
    _write_agent_owner_pointer(repo, _COMMIT_AGENT_ID, _SID)
    (repo / "own.py").write_text("x = 1\n", encoding="utf-8")
    _write_touched(repo, _SID, [_touch_line("T", "own.py", "2026-08-13T10:00:00Z")])
    _write_live_meta(repo, _SID, live=True)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["own.py"])

    joined = " ".join(args)
    assert "Absorbed-From:" not in joined


def test_absorbed_from_still_emitted_for_a_genuine_peer_when_committer_is_an_agent(
    tmp_path, monkeypatch
):
    """Contrast case: the committer is a dispatched agent (back-pointed to
    EM session `_SID`), but the claimant is a DIFFERENT, unrelated live
    session -- folding the committer to its owner must not suppress a
    genuine peer disclosure."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _COMMIT_AGENT_ID)
    _write_agent_owner_pointer(repo, _COMMIT_AGENT_ID, _SID)
    (repo / "hot.py").write_text("x = 1\n", encoding="utf-8")
    _write_touched(repo, _PEER_SID, [_touch_line("T", "hot.py", "2026-08-13T10:00:00Z")])
    _write_live_meta(repo, _PEER_SID, live=True)
    _write_shape(repo, _PEER_SID, {"pickup": {"deliverable_id": "dlv-peer-work"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["hot.py"])

    joined = " ".join(args)
    assert f"Absorbed-From: {_PEER_SID} dlv-peer-work hot.py" in joined


def test_env_var_tiers_win_regardless_of_sentinel(tmp_path, monkeypatch):
    """$CLAUDE_SESSION_ID / $CLAUDE_CODE_SESSION_ID win outright regardless
    of whether a sentinel file or session directory exists at all."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    # No session dir, no meta.json, no session-shape.json for _SID at all.
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert f"Session-Id: {_SID}" in joined


# ---------------------------------------------------------------------------
# `session_id_override` -- state/bug-backlog/2026-08-18-scoped-git-commit-
# stamps-a-foreign-session-id-8d21f0c4e7b9.yaml. The caller's own
# already-resolved committing-session identity must win over the blind
# `$CLAUDE_SESSION_ID`/`$CLAUDE_CODE_SESSION_ID` env read -- the two can
# legitimately disagree on a shared, many-concurrent-session process
# (`scoped_git_commit.py::_resolve_committing_session_id` honors an
# explicit `params["session_id"]` override this module's blind env read has
# no way to see).
# ---------------------------------------------------------------------------

_OTHER_LIVE_SID = "e77424be-b452-43bd-a995-e12d60168cb6"


def test_session_id_override_wins_over_ambient_env(tmp_path, monkeypatch):
    """The invoking session's OWN resolved id, passed explicitly, must be
    stamped -- never the ambient env value a concurrently-live peer session
    happens to have set in this process."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _OTHER_LIVE_SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, session_id_override=_SID)

    joined = " ".join(args)
    assert f"Session-Id: {_SID}" in joined
    assert _OTHER_LIVE_SID not in joined


def test_session_id_override_falls_back_to_env_when_not_uuid_shaped(tmp_path, monkeypatch):
    """A garbage/non-UUID override is treated as no override at all --
    same fail-safe direction the blind env read already applies to itself,
    never silently stamped."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, session_id_override="scoped-git-commit-not-a-real-session"
    )

    joined = " ".join(args)
    assert f"Session-Id: {_SID}" in joined


def test_session_id_override_none_reproduces_prior_behaviour(tmp_path, monkeypatch):
    """`session_id_override=None` (the default) must be byte-identical to
    every pre-existing caller's resolution -- the blind env read."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert f"Session-Id: {_SID}" in joined
