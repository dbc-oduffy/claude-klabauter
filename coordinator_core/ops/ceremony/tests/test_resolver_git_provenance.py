"""
coordinator_core.ops.ceremony.tests.test_resolver_git_provenance

Regression tests for ``resolver.detect_git_provenance_consumed`` (Detector B --
git-provenance chain-terminal detection, ported from the bash
/workstream-complete SKILL.md Step 0 oracle) and its wiring into
``wsc_resolve.py``'s disposition detection.

Coverage:
  (a) detector_b_positive                 -- archived handoff with NO
                                              consumed_by anywhere, added by a
                                              commit carrying a
                                              ``Session-Id: <sid>`` trailer
                                              -> attributed to sid.
  (b) restoration_commit_spoof_rejected   -- archived handoff whose own
                                              frontmatter ``consumed_by:``
                                              names session Y, re-added by a
                                              restoration commit carrying
                                              session X's trailer -> the
                                              foreign-consumer guard REJECTS
                                              attribution to X; a diagnostic
                                              names Y. The incident-derived AC
                                              this whole port exists for.
  (b2) dual_failure_spoof_diagnostic_wins -- a candidate that is BOTH
                                              malformed (no predecessor: line)
                                              AND foreign-consumed (consumed_by
                                              names a different session) ->
                                              the spoof diagnostic (naming the
                                              real consumer) wins, matching the
                                              oracle's spoof-first guard order.
                                              Review: code-reviewer 2026-07-22
                                              slice1 finding #1.
  (c) guard_pass_through_empty_consumed_by -- a B-hit file with empty/absent
                                              consumed_by binds to sid (the
                                              guard does not over-reject).
  (d) well_formedness_guard_rejects_no_predecessor -- a B-hit file lacking a
                                              ``predecessor:`` line is
                                              rejected with a diagnostic.
  (e) merge_base_unresolvable_warns_no_crash -- no ``origin/main`` -> a loud
                                              warning, empty hits, no crash.

Spec backlink:
  cross-repo/inbox/2026-07-22-claude-central-em-wsc-tail-cutover-contract.md Ask 2
  DoE-claude/coordinator/skills/workstream-complete/SKILL.md Step 0 (Detector B
  + 2026-07-22 foreign-consumer/restoration-commit spoof guard)
  state/lessons/2026-07-22-shared-index-commits-never-bare-git-comm-e04e7a2d648a.yaml
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.resolver import detect_git_provenance_consumed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _init_bare_git_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "detector-b@claude-klabauter.test")
    _git(root, "config", "user.name", "Detector B Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: initial skeleton")


def _init_repo_with_origin(tmp_path: Path) -> Path:
    """A git repo, seeded and committed, with an ``origin`` remote pointing
    at a local bare repo -- so ``git merge-base origin/main HEAD`` resolves
    (mirrors ``test_wsc_tail_parity.py``'s ``wsc_tail_repo`` fixture shape)."""
    root = tmp_path / "repo"
    _init_bare_git_repo(root)

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )
    _git(root, "remote", "add", "origin", str(bare))
    push = _git(root, "push", "-u", "origin", "main")
    assert push.returncode == 0, push.stderr
    return root


def _seed_archived_handoff(
    root: Path,
    name: str,
    *,
    consumed_by: str = "",
    predecessor: bool = True,
) -> Path:
    """Write (uncommitted) an ``archive/handoffs/<name>.md`` fixture file.

    ``consumed_by`` kwarg name kept (call-site DSL, unchanged) but the
    frontmatter key written is ``claimed_by`` — DR-084 P4 (C7) retired
    ``consumed_by`` corpus-wide; ``resolver.detect_git_provenance_consumed``
    is a single-name ``claimed_by`` read.
    """
    path = root / "archive" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['title: "Test Archived Handoff"', "created: 2026-01-01", "status: archived"]
    if predecessor:
        lines.append("predecessor: null")
    if consumed_by:
        lines.append(f"claimed_by: {consumed_by}")
    fm = "\n".join(lines)
    path.write_text(f"---\n{fm}\n---\n\n# Handoff Body\n", encoding="utf-8")
    return path


def _commit_unpushed(root: Path, message: str) -> None:
    """Commit locally WITHOUT pushing -- ``origin/main`` stays at the fixture's
    initial skeleton commit, so the new commit is genuinely ahead of it (the
    real WSC shape: these are the session's own not-yet-shipped commits,
    landed only by the ceremony's own commit pipeline later). Pushing here
    would make ``origin/main`` == HEAD, collapsing the merge-base range to
    empty and hiding every commit Detector B is supposed to scan."""
    _git(root, "add", "-A")
    result = _git(root, "commit", "-m", message)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def repo(tmp_path) -> Path:
    return _init_repo_with_origin(tmp_path)


# ---------------------------------------------------------------------------
# (a) detector_b_positive
# ---------------------------------------------------------------------------


def test_detector_b_positive_attributes_to_sid(repo):
    sid = "sess-detb-001"
    _seed_archived_handoff(repo, "shipped.md")
    _commit_unpushed(repo, f"archive: ship handoff\n\nSession-Id: {sid}")

    hits, warnings = detect_git_provenance_consumed(repo, sid)

    assert warnings == []
    assert [p for p, _fm in hits] == ["archive/handoffs/shipped.md"]


# ---------------------------------------------------------------------------
# (b) restoration-commit spoof regression -- THE incident-derived AC
# ---------------------------------------------------------------------------


def test_restoration_commit_spoof_guard_rejects_foreign_consumer(repo):
    sid_x = "sess-detb-restore-x"
    sid_y = "sess-detb-consumer-y"
    _seed_archived_handoff(repo, "consumed-by-y.md", consumed_by=sid_y)
    _commit_unpushed(repo, f"restore: recover archived handoff\n\nSession-Id: {sid_x}")

    hits, warnings = detect_git_provenance_consumed(repo, sid_x)

    assert hits == []
    assert any(sid_y in w and "consumed-by-y.md" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# (b2) dual-failure -- spoof diagnostic wins over the generic malformed one
# ---------------------------------------------------------------------------


def test_dual_failure_candidate_surfaces_spoof_diagnostic_not_malformed(repo):
    """A candidate that is BOTH malformed (no predecessor: line) AND
    foreign-consumed (consumed_by: names a different session) must surface
    the spoof diagnostic naming the real consumer -- not the generic
    "no predecessor:" one -- matching the oracle's spoof-first guard order
    (SKILL.md Step 0: the well-formedness check lives inside the spoof
    guard's else-branch, so it never even runs on this candidate)."""
    sid_x = "sess-detb-dual-x"
    sid_y = "sess-detb-dual-consumer-y"
    _seed_archived_handoff(
        repo, "dual-failure.md", consumed_by=sid_y, predecessor=False,
    )
    _commit_unpushed(repo, f"restore: recover archived handoff\n\nSession-Id: {sid_x}")

    hits, warnings = detect_git_provenance_consumed(repo, sid_x)

    assert hits == []
    assert any(sid_y in w and "dual-failure.md" in w for w in warnings), warnings
    assert not any("no predecessor" in w for w in warnings), (
        f"malformed diagnostic must not surface when the spoof guard already "
        f"rejected the candidate; got {warnings!r}"
    )


# ---------------------------------------------------------------------------
# (c) guard pass-through -- empty/absent consumed_by does not over-reject
# ---------------------------------------------------------------------------


def test_guard_pass_through_empty_consumed_by_binds_to_sid(repo):
    sid = "sess-detb-passthrough"
    _seed_archived_handoff(repo, "unconsumed.md", consumed_by="")
    _commit_unpushed(repo, f"archive: ship unconsumed handoff\n\nSession-Id: {sid}")

    hits, warnings = detect_git_provenance_consumed(repo, sid)

    assert warnings == []
    assert [p for p, _fm in hits] == ["archive/handoffs/unconsumed.md"]


# ---------------------------------------------------------------------------
# (d) well-formedness guard -- missing predecessor: rejects with a diagnostic
# ---------------------------------------------------------------------------


def test_well_formedness_guard_rejects_missing_predecessor(repo):
    sid = "sess-detb-malformed"
    _seed_archived_handoff(repo, "malformed.md", predecessor=False)
    _commit_unpushed(repo, f"archive: ship malformed handoff\n\nSession-Id: {sid}")

    hits, warnings = detect_git_provenance_consumed(repo, sid)

    assert hits == []
    assert any("malformed.md" in w and "predecessor" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# (e) merge-base unresolvable -- loud warning, no crash, empty hits
# ---------------------------------------------------------------------------


def test_merge_base_unresolvable_warns_loudly_no_crash(tmp_path):
    root = tmp_path / "repo-no-origin"
    _init_bare_git_repo(root)  # no `origin` remote configured at all

    hits, warnings = detect_git_provenance_consumed(root, "sess-detb-no-origin")

    assert hits == []
    assert len(warnings) == 1
    assert "merge-base" in warnings[0]
