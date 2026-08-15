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

Run: cd X:/claude-klabauter && python -m pytest
coordinator_core/pickup_assemble/tests/test_consumer_field_path_contract.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
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


# ---------------------------------------------------------------------------
# Tier 1 — always-emitted (paths 1-5)
# ---------------------------------------------------------------------------


def test_always_emitted_paths_resolve_on_plain_handoff_brief(tmp_path, monkeypatch):
    """The five always-emitted paths must resolve on ANY plain-handoff
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
    CONDITIONALLY, over a baton shaped to carry its own correctly-computed
    `execution_authorized_sha` (a "the artifact IS the plan" stamp, per
    `compute_execution_stamp_match`'s no-pointer fallback branch), which
    resolves to verdict "match" and therefore always carries the
    `delta_class` key (value `None` on a match — the key's PRESENCE is what
    is pinned here, never its value, per this module's presence/shape
    framing). Building a full `## Plan to Execute` pointer + separate plan
    file was not needed to exercise this key: the no-pointer branch reaches
    the identical `{..., "delta_class": ..., ...}` gate shape more cheaply.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    # Two-pass write: compute the canonical body-sha of the intended final
    # file text, then bake it into the frontmatter as
    # `execution_authorized_sha` so live-computed and stamped hashes match
    # ("verdict": "match") on the FIRST commit — `compute_execution_stamp_
    # match` hashes the artifact's own committed body against this field.
    placeholder = _write_handoff(repo, "stamped.md", fm_extra="execution_authorized_sha: PENDING\n")
    final_text_template = placeholder.read_text(encoding="utf-8")
    # The stamp only needs to be computed once: `execution_authorized_sha`
    # is a frontmatter field, and `frontmatter_body_text` excludes the
    # frontmatter entirely — so the body hash is identical whether the
    # placeholder or the real sha string sits in that field.
    stamped_sha = canonical_body_sha(final_text_template)
    final_text = final_text_template.replace("PENDING", stamped_sha)
    placeholder.write_text(final_text, encoding="utf-8")
    _git(repo, "add", str(placeholder.relative_to(repo)))
    _git(repo, "commit", "-m", "stamp")

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
