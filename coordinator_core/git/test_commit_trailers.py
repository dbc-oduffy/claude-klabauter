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

from coordinator_core.git import commit_trailers
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
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())


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


def test_artifact_tier_omits_on_genuinely_divergent_multi_artifact_commit(
    tmp_path, monkeypatch
):
    """Two paths in the SAME commit naming two DIFFERENT non-empty
    deliverable_id values is not guessed at -- per producer-contract § 3,
    tier 0 OMITS rather than raises: no Deliverable-Id trailer is stamped,
    and the session tiers below run exactly as if tier 0 found nothing."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_handoff(repo, "state/handoffs/a.md", "dlv-a")
    _write_handoff(repo, "state/handoffs/b.md", "dlv-b")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["state/handoffs/a.md", "state/handoffs/b.md"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


# Review: C6b/AC11's declared-fork-pair canonical-join pin (review-integrator
# P1, coordinatorcode-reviewer-0f04f47d.md) tested `state/deliverable-
# equivalence.yaml` + canonicalize() -- condemned and collapsed to identity
# (plan 2026-08-20-the-close-ceremony-stops-paying-for-the-join, F-1); the
# join this test pinned no longer exists.


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


# ---------------------------------------------------------------------------
# C1 red: a commit pathspec spanning two deliverables must RESOLVE, never
# raise. Inverts the raise assertion at test_artifact_tier_raises_on_
# genuinely_divergent_multi_artifact_commit (line ~442) -- C2 re-points that
# test; this one is the new target behaviour it must land green against.
# ---------------------------------------------------------------------------


def test_multi_deliverable_pathspec_resolves_without_deliverable_trailer(
    tmp_path, monkeypatch
):
    """AC1 + AC2 in one assertion pair: two artifacts naming two DIFFERENT
    non-empty deliverable_id values in the same pathspec must RETURN (not
    raise `DivergentDeliverableIdError`), and the returned arg list must
    carry NO `Deliverable-Id:` trailer at all -- an unresolvable multi-
    deliverable commit omits the trailer rather than guessing or refusing."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_handoff(repo, "state/handoffs/a.md", "dlv-a")
    _write_handoff(repo, "state/handoffs/b.md", "dlv-b")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["state/handoffs/a.md", "state/handoffs/b.md"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def test_single_deliverable_pathspec_unchanged_behaviour(tmp_path, monkeypatch):
    """AC3 companion: the single-deliverable path must be byte-identical to
    its pre-fix behaviour -- the divergent-pathspec fix cannot be graded on
    the multi-deliverable case alone."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_handoff(repo, "state/handoffs/only.md", "dlv-only")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["state/handoffs/only.md"])

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-only" in joined


def test_eleven_distinct_deliverables_at_scale_resolves_without_trailer(
    tmp_path, monkeypatch
):
    """AC11, the `mise` case AT SCALE: eleven artifacts carrying eleven
    distinct deliverable_id values in one pathspec, not the n=2 minimal
    reproduction -- the real autonomous-run workload the PM named. Built
    from real handoff frontmatter shapes (`_write_handoff` -> `_write_plan`),
    not eleven synthetic stubs."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)

    rel_paths = []
    for i in range(11):
        rel_path = f"state/handoffs/mise-{i:02d}.md"
        _write_handoff(repo, rel_path, f"dlv-mise-{i:02d}")
        rel_paths.append(rel_path)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=rel_paths)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def test_emergency_pathspec_with_no_deliverable_id_anywhere_commits_untrailered(
    tmp_path, monkeypatch
):
    """AC12, the EMERGENCY case: a pathspec whose files carry no
    deliverable_id at all must commit untrailered and must never be
    refused. This already passes today (verified: resolves to ``""``) --
    pinned as a regression here because C2 edits the very function that
    produces that ``""``, and a careless early return in the divergence fix
    would turn the emergency path into a refusal instead of a silent
    omission. A test that passes on the first run is the point, not a
    defect in the test."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    (repo / "src").mkdir()
    (repo / "src" / "emergency_fix.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["src/emergency_fix.py"])

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


# --- message-authored Deliverable-Id trailer: read, and shape-guard ------
#
# Closes the unvalidated door reported in cross-repo/inbox/2026-08-20-
# example-retrieval-repo-em-wave-commit-deliverable-id-is-per-session.md. Deliberately
# spawn-free: `commit_scoped`'s guard returns before `_index_blobs`, the
# first leg that touches git.


def _msg(tmp_path, text):
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text(text, encoding="utf-8")
    return f


def test_read_trailer_value_returns_the_trailer_block_value(tmp_path):
    f = _msg(tmp_path, "C1: subject\n\nbody\n\nDeliverable-Id: dlv-a-b-123456\n")
    assert commit_trailers.read_trailer_value(f, "Deliverable-Id:") == "dlv-a-b-123456"


def test_read_trailer_value_ignores_a_body_line_outside_the_trailer_block(tmp_path):
    # Same distinction `_has_trailer_line` draws: a colon-shaped line sitting
    # in the BODY, above a real trailing block, is prose git never parses.
    f = _msg(
        tmp_path,
        "C1: subject\n\nDeliverable-Id: dlv-in-the-body-999999\n\n"
        "Session-Id: 11111111-1111-1111-1111-111111111111\n",
    )
    assert commit_trailers.read_trailer_value(f, "Deliverable-Id:") is None


def test_read_trailer_value_treats_a_blank_value_as_absent(tmp_path):
    f = _msg(tmp_path, "C1: subject\n\nbody\n\nDeliverable-Id:   \n")
    assert commit_trailers.read_trailer_value(f, "Deliverable-Id:") is None


def test_read_trailer_value_degrades_to_none_on_an_unreadable_file(tmp_path):
    assert (
        commit_trailers.read_trailer_value(tmp_path / "nope", "Deliverable-Id:") is None
    )


def test_commit_scoped_refuses_a_branch_name_in_the_deliverable_id_trailer(tmp_path):
    # The exact malformed value observed on example-retrieval-repo's e78b55610.
    from coordinator_core.ops.ceremony import git_native

    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    f = _msg(
        tmp_path,
        "C1: subject\n\nbody\n\nDeliverable-Id: work/machine-a/2026-08-16to18\n",
    )
    result = git_native.commit_scoped(["f.txt"], f, tmp_path)
    assert result.returncode == -1
    assert "work/machine-a/2026-08-16to18" in result.stderr
    assert "dlv-" in result.stderr and "pln-" in result.stderr


def test_commit_scoped_admits_a_pln_prefixed_authored_trailer(tmp_path):
    # `--deliverable-id` is aliased to accept a `pln-` id (C10b, docs/plans/
    # 2026-08-13-spec-backlinks-cite-a-stable-deliverable-id.md); the
    # message-authored route must not be stricter than the parameter one.
    # Passing the shape guard is all that is asserted -- the call proceeds
    # into git and fails there on a non-repo tmp_path, which is precisely
    # the evidence it was not refused BY THE GUARD.
    from coordinator_core.ops.ceremony import git_native

    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    f = _msg(tmp_path, "C1: subject\n\nbody\n\nDeliverable-Id: pln-a-b-123456\n")
    result = git_native.commit_scoped(["f.txt"], f, tmp_path)
    assert "does not match the 'dlv-' or 'pln-' shape convention" not in result.stderr
