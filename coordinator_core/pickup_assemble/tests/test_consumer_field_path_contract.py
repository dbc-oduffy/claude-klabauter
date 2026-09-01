"""
coordinator_core.pickup_assemble.tests.test_consumer_field_path_contract

Purpose: pins six `brief()` decision-object field paths as a
**consumer-facing contract**. A sibling repo (doe-claude-em) ships skill
prose in the coordinator-claude repo — the `pickup` and `workstream-complete`
skills — citing these six paths BY NAME as the reader's verification route at
the fleet's two scarcest-context gates. That prose does not live in this
repo and this test cannot see it; this test is the tripwire on OUR side of
the boundary: a rename of any of these six paths is a breaking change to
that downstream consumer and must land as a red test HERE, prompting a
deprecation cycle, rather than silently drifting the field shape out from
under prose the consumer already shipped.

Two tiers, deliberately not collapsed into one list:

  ALWAYS-EMITTED (present on every `brief()` call over a plain handoff,
  regardless of baton shape):
    1. gates.claim_grant.held_by_self
    2. directives[].already_satisfied
    3. gates.branch.current_branch
    4. gates.liveness_signal
    5. preflight.completeness_batches[]
    7. sizing_disposition.value

  SHAPE-CONDITIONAL (must NOT be asserted against an arbitrary brief — only
  present when the baton points at a stamped plan; legitimately ABSENT from
  a plain handoff brief, per `compute_execution_stamp_match`'s own
  docstring: "`None` when the artifact carries neither an
  `execution_authorized_sha` of its own nor a pointer to a plan that
  carries one"):
    6. gates.execution_stamp_match.delta_class

Asserting path 6 against the plain-handoff fixture used for paths 1-5 would
fail for the wrong reason (shape mismatch, not a rename) — so this file
builds a SECOND, stamped-plan-shaped fixture (a handoff carrying its own
correctly-computed `execution_authorized_sha`, verdict "match") specifically
to exercise path 6. Building that second fixture was achievable cheaply
(`coordinator_core.frontmatter.primitives.canonical_body_sha` computes the
exact stamp `compute_execution_stamp_match` expects), so path 6 is pinned
via a real `verdict: "match"` brief rather than merely documented as
untestable.

Both fixtures are built through the real `brief()` path over a real git
worktree fixture (mirroring `test_claim_state_reads.py`'s harness
convention, deliberately NOT imported from it — see this repo's C11
dispatch-brief-style out-of-scope convention: a concurrent editor owns that
file) — never a hand-rolled decision-object dict.

This is a presence/shape contract: every assertion below checks that a path
RESOLVES on the decision object, never what value it carries (beyond the
minimum needed to select the right `execution_stamp_match` verdict branch
for path 6). Over-constraining values here would make this test fragile for
reasons the downstream consumer does not care about.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_consumer_field_path_contract.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.frontmatter.primitives import canonical_body_sha

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


# ---------------------------------------------------------------------------
# Minimal, self-contained git harness — deliberately NOT imported from
# test_claim_state_reads.py (a concurrent executor owns that directory's
# other new file right now) or the peer-dirty
# coordinator_core/test_pickup_assemble.py.
# ---------------------------------------------------------------------------


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _write_handoff(repo: Path, name: str, fm_extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "Test Handoff"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
        f"{fm_extra}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _write_plan(repo: Path, name: str, fm_extra: str = "") -> Path:
    """A plan document, under `docs/plans/` where a plan actually lives.

    Distinct from `_write_handoff` on PATH, which is the whole point:
    `compute_execution_stamp_match`'s no-pointer fallback is now guarded on
    the artifact really being a plan, so only an artifact written here can
    exercise the "the artifact IS the plan" branch.
    """
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "Test Plan"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: approved\n"
        f"{fm_extra}"
    )
    path.write_text(f"---\n{fm}---\n\n# Plan\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


# ---------------------------------------------------------------------------
# Tier 1 — always-emitted (paths 1-5)
# ---------------------------------------------------------------------------


def test_always_emitted_paths_resolve_on_plain_handoff_brief(tmp_path, monkeypatch):
    """The always-emitted paths must resolve on ANY plain-handoff
    `brief()` call — no stamped-plan shape required."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_handoff(repo, "h1.md")

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, decisions={})
    do = result.decision_object

    # 1. gates.claim_grant.held_by_self
    assert "held_by_self" in do["gates"]["claim_grant"]

    # 2. directives[].already_satisfied
    assert do["directives"], "expected at least one directive on a plain handoff brief"
    for directive in do["directives"]:
        assert "already_satisfied" in directive

    # 3. gates.branch.current_branch
    assert "current_branch" in do["gates"]["branch"]

    # 4. gates.liveness_signal
    assert "liveness_signal" in do["gates"]

    # 5. preflight.completeness_batches[]
    assert "completeness_batches" in do["preflight"]
    assert isinstance(do["preflight"]["completeness_batches"], list)

    # 7. sizing_disposition.value — the axis `plan`'s carve-out keys on.
    # Always emitted, including on the `unsized` arm this fixture lands on:
    # an absent key is indistinguishable from "not computed" to the reader,
    # and the whole point is that the EM is never left to look.
    assert "sizing_disposition" in do
    assert do["sizing_disposition"]["value"] in ("execution", "sized", "unsized")
    assert "basis" in do["sizing_disposition"]
    assert "warning" in do["sizing_disposition"]


# ---------------------------------------------------------------------------
# Tier 2 — shape-conditional (path 6)
# ---------------------------------------------------------------------------


def test_execution_stamp_match_delta_class_absent_on_plain_handoff(tmp_path, monkeypatch):
    """Negative control: `gates.execution_stamp_match` (and therefore
    `.delta_class`) is legitimately ABSENT from a plain handoff that carries
    no `execution_authorized_sha` and no `## Plan to Execute` pointer — this
    is the shape this path must NOT be asserted against, per
    `compute_execution_stamp_match`'s own docstring."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_handoff(repo, "h1.md")

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, decisions={})

    assert "execution_stamp_match" not in result.decision_object["gates"]


def test_execution_stamp_match_delta_class_present_on_stamped_plan_baton(tmp_path, monkeypatch):
    """Path 6 — `gates.execution_stamp_match.delta_class` — pinned
    CONDITIONALLY, over a handoff that POINTS at a plan carrying a
    correctly-computed `execution_authorized_sha`. That resolves to verdict
    "match" and therefore always carries the `delta_class` key (value `None`
    on a match -- the key's PRESENCE is what is pinned here, never its
    value, per this module's presence/shape framing).

    The fixture was a lone handoff carrying its OWN stamp and no pointer
    until 2026-08-31, on the reasoning that the no-pointer fallback reached
    the same gate shape more cheaply. It did, but that shape is the
    mirror-mismatch defect itself: a handoff mirroring its plan's stamp,
    hashed against its own body, then re-stamped to agree with itself.
    Guarding that fallback on the artifact really being a plan turned this
    fixture red -- correctly. A pin that can only be satisfied by the defect
    still existing has to move when the defect does.

    Repointing it at a plan document directly did not work either, and the
    reason is worth recording: `brief()` classifies a `docs/plans/` artifact
    down a different route that never reaches this gate. So the fixture is
    the canonical shape instead -- pointer plus plan, two files -- which is
    both what the engine should accept and what the wild actually contains.
    The consumer contract this file protects is path 6, unchanged and still
    pinned; only the shape reaching it moved.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    # The CANONICAL shape: a handoff that POINTS at its plan, and a plan
    # that carries the stamp. `compute_execution_stamp_match` reads the
    # pointer (`governing_plan:` frontmatter, one of its two conventions)
    # and hashes the PLAN -- which is what the gate is for. The pointing
    # artifact never has to be the hash target.
    #
    # Two-pass write: compute the canonical body-sha of the intended final
    # plan text, then bake it into that plan's frontmatter as
    # `execution_authorized_sha` so live-computed and stamped hashes match
    # ("verdict": "match") on the first commit. The stamp only needs
    # computing once: `frontmatter_body_text` excludes the frontmatter
    # entirely, so the body hash is identical whether the placeholder or
    # the real sha sits in that field.
    plan = _write_plan(repo, "target.md", fm_extra="execution_authorized_sha: PENDING\n")
    plan_template = plan.read_text(encoding="utf-8")
    stamped_sha = canonical_body_sha(plan_template)
    plan.write_text(plan_template.replace("PENDING", stamped_sha), encoding="utf-8")
    _git(repo, "add", str(plan.relative_to(repo)))
    _git(repo, "commit", "-m", "stamp the plan")

    _write_handoff(
        repo, "stamped.md", fm_extra="governing_plan: docs/plans/target.md\n"
    )

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)

    result = pa.brief("state/handoffs/stamped.md", repo_root=repo, decisions={})
    do = result.decision_object

    assert "execution_stamp_match" in do["gates"], (
        "fixture did not resolve to a stamped-plan-shaped brief — "
        f"gates keys were: {sorted(do['gates'].keys())}"
    )
    assert "delta_class" in do["gates"]["execution_stamp_match"]
    # Review: coordinator:code-reviewer — self-check that the fixture actually
    # exercises the "match" verdict path this test's docstring claims, not
    # merely that the key is present under some other verdict.
    assert do["gates"]["execution_stamp_match"]["verdict"] == "match"

def test_a_handoff_mirroring_a_plan_stamp_yields_no_gate(tmp_path, monkeypatch):
    """A handoff carrying `execution_authorized_sha` and NO pointer emits
    no stamp gate at all.

    This is the mirror-mismatch defect, and both halves of it are pinned
    here. The handoff mirrors its plan's stamp for human readability while
    naming that plan by neither convention the gate reads -- a baton that
    identifies its plan by `deliverable_id` alone. The old fallback treated
    "no pointer" as "this artifact IS the plan", hashed the handoff's own
    body against a sha that recorded the PLAN's, and produced a mismatch
    that fed `unstampable`'s re-stamp directive -- which rewrote the
    handoff's field to match the handoff's body, destroying the only thing
    the mirror recorded. Afterwards the gate read `match` and said "matches
    the current plan body" over an artifact that is not a plan and was
    never compared to one.

    `None` is the honest answer: there is no plan reachable, so there is
    nothing to verify, and `None` contributes neither a directive nor a
    judgment point. Asserting the ABSENCE of the gate key is the point --
    a future fallback that resurrects a guess would show up here as a gate
    appearing, whatever verdict it carried.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    # A stamp that is genuinely SOME OTHER document's -- the mirror case.
    # Any well-formed sha that is not this handoff's own body hash will do;
    # deriving it from different text keeps the intent legible.
    foreign_sha = canonical_body_sha("---\ntitle: other\n---\n\nA different body.\n")
    _write_handoff(
        repo,
        "mirrored.md",
        fm_extra=f"execution_authorized_sha: {foreign_sha}\n",
    )

    monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)

    result = pa.brief("state/handoffs/mirrored.md", repo_root=repo, decisions={})
    gates = result.decision_object["gates"]

    assert "execution_stamp_match" not in gates, (
        "a handoff mirroring a plan stamp must not produce a plan-authorization "
        f"verdict; got gate keys: {sorted(gates)}"
    )

    # And nothing may promote a re-stamp of the handoff off the back of it.
    assert not [
        d for d in result.decision_object["directives"]
        if "stamp" in str(d.get("id", "")).lower()
    ]
