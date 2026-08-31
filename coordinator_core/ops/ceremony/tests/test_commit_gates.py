"""
coordinator_core.ops.ceremony.tests.test_commit_gates

Tests for commit_gates.py -- the native ports of the deleted
`check-workstream-complete-deletion-blocks.sh` and `dirty-tree-gate.sh`
(the C3 chunk of the `wsc_tail` rebuild,
docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Coverage (parity-oracle assertions, per the deleted
`tests/wsc-asic/test-wsc-commit-parity.sh` recovered from
`DoE:85006468^:coordinator/tests/wsc-asic/test-wsc-commit-parity.sh`):
  (c)  deletion_block_gate passes on a well-formed message (Kept block only,
       no staged deletions).
  (c2) deletion_block_gate fails on a malformed message (Deleted-claimed path
       that was never staged for deletion).
  (e)  deletion_block_gate does NOT false-positive on a concurrent sibling's
       staged deletion that falls OUTSIDE gate_paths -- the F3 inverse check
       is scoped, not whole-index.

Plus native additions beyond the recovered oracle (the oracle only exercised
the bash CLI end-to-end; these pin the Python module's own unit-level
contract):
  skip_gate_when_empty         -- empty gate_paths + no Step 2.67 block skips
                                   the gate entirely (never scored ambiguous-pass).
  kept_claim_missing           -- Kept-claimed path absent from HEAD and staged
                                   set is a mismatch.
  kept_line_malformed          -- a Kept-block line with no em-dash separator
                                   is flagged, not silently treated as a path.
  parse_step267_blocks_*       -- blank-line-inside-block grouping, block-header
                                   termination.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C3 (AC10).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_gates as _cg
from coordinator_core.ops.ceremony.commit_gates import (
    GateOutcome,
    ParsedBlocks,
    carry_gate,
    deletion_block_gate,
    has_step267_block,
    op_scope_coverage_gate,
    parse_step267_blocks,
)
from coordinator_core.win_portability import no_console_creationflags

# Real-git spawn is load-bearing: these gates classify real staged/HEAD
# state, which a mocked git cannot faithfully reproduce. Per-test repo
# fixtures since these tests mutate the index and HEAD.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_EM_DASH = " — "


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _make_conflicted_repo(tmp_path: Path) -> Path:
    """A repo whose index carries an unmerged (stage != 0) entry -- an
    ordinary mid-merge-conflict state, not a malformed index. `read_index`
    raises `IndexParseError` on this by contract (git_state.py:47-50); F1
    fixture repos exercise that this is a live, unresolved-merge condition,
    never a crash."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "conflict.md", "base\n")
    _git(["add", "--", "conflict.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()

    _git(["checkout", "-q", "-b", "side"], repo)
    _seed_file(repo, "conflict.md", "side change\n")
    _git(["commit", "-q", "-am", "side change"], repo)

    _git(["checkout", "-q", base_branch], repo)
    _seed_file(repo, "conflict.md", "main change\n")
    _git(["commit", "-q", "-am", "main change"], repo)

    # Left deliberately unresolved -- `git merge` exits non-zero here, which
    # is the point: the index now carries stage-1/2/3 entries for
    # conflict.md, never committed or resolved.
    subprocess.run(["git", "merge", "-q", "side"], cwd=str(repo), capture_output=True, text=True, **no_console_creationflags())
    return repo


# ---------------------------------------------------------------------------
# parse_step267_blocks / has_step267_block
# ---------------------------------------------------------------------------


def test_parse_blocks_deleted_and_kept():
    msg = (
        "subject\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "a/one.md\n"
        "a/two.md\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"a/three.md{_EM_DASH}still needed\n"
        "--- end Step 2.67 blocks ---\n"
    )
    parsed = parse_step267_blocks(msg)
    assert parsed == ParsedBlocks(
        deleted_claimed=["a/one.md", "a/two.md"],
        kept_claimed=["a/three.md"],
        kept_malformed=[],
    )
    assert has_step267_block(msg) is True


def test_parse_blocks_blank_line_inside_block_does_not_terminate():
    msg = (
        "subject\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "a/one.md\n"
        "\n"
        "a/two.md\n"
        "--- end Step 2.67 blocks ---\n"
    )
    parsed = parse_step267_blocks(msg)
    assert parsed.deleted_claimed == ["a/one.md", "a/two.md"]


def test_parse_blocks_malformed_kept_line_flagged():
    msg = (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        "a/no-separator.md\n"
        "--- end Step 2.67 blocks ---\n"
    )
    parsed = parse_step267_blocks(msg)
    assert parsed.kept_claimed == []
    assert parsed.kept_malformed == ["a/no-separator.md"]


def test_has_step267_block_false_on_plain_message():
    assert has_step267_block("subject\n\nprose only\n") is False


# ---------------------------------------------------------------------------
# deletion_block_gate -- assertion (c): PASS on well-formed message
# ---------------------------------------------------------------------------


def test_deletion_gate_assertion_c_passes_well_formed_no_staged_deletions(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "tasks/my-feature/todo.md", "content")
    _git(["add", "--", "tasks/my-feature/todo.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    msg = (
        "workstream-complete: fixture-check\n"
        "\n"
        "Some prose.\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"tasks/my-feature/todo.md{_EM_DASH}still load-bearing for active sibling workstream\n"
        "--- end Step 2.67 blocks ---\n"
    )

    outcome = deletion_block_gate(msg, gate_paths=["tasks/my-feature/todo.md"], cwd=repo)
    assert outcome.passed is True
    assert outcome.diagnostics == []


# ---------------------------------------------------------------------------
# deletion_block_gate -- assertion (c2): FAIL on malformed message
# ---------------------------------------------------------------------------


def test_deletion_gate_assertion_c2_fails_unstaged_deleted_claim(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "tasks/my-feature/todo.md", "content")
    _git(["add", "--", "tasks/my-feature/todo.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    msg = (
        "workstream-complete: fixture-check\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "tasks/nonexistent-path.md\n"
        "\n"
        "--- end Step 2.67 blocks ---\n"
    )

    outcome = deletion_block_gate(msg, gate_paths=["tasks/nonexistent-path.md"], cwd=repo)
    assert outcome.passed is False
    assert any("NOT staged for deletion" in d for d in outcome.diagnostics)


# ---------------------------------------------------------------------------
# deletion_block_gate -- Assertion-1 recognizes a staged RENAME source as
# "staged for deletion" (2026-08-06 fix, live incident: commit `64acc1254`,
# a move-set of changelog/review-trail files into an archive directory,
# refused with "Deleted-claim NOT staged for deletion" for every moved path).
# See `commit_gates._parse_name_status_rename_sources`'s own docstring for
# the full root-cause writeup.
# ---------------------------------------------------------------------------


def test_deletion_gate_assertion_1_recognizes_staged_rename_source(tmp_path):
    repo = _init_repo(tmp_path)
    content = "content block\n" * 40  # long/repetitive enough for git's rename detector
    _seed_file(repo, "week-changelog/2026-07-20.md", content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    old_path = repo / "week-changelog/2026-07-20.md"
    old_path.unlink()
    new_path = repo / "archive/week-changelog/2026-07-20.md"
    new_path.parent.mkdir(parents=True)
    new_path.write_text(content, encoding="utf-8")
    _git(
        [
            "add",
            "--",
            "week-changelog/2026-07-20.md",
            "archive/week-changelog/2026-07-20.md",
        ],
        repo,
    )

    # Confirm git itself paired this into a rename, not a D+A pair -- the
    # precondition this test exists to cover.
    name_status = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "-M"],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    assert name_status.startswith("R")

    msg = (
        "archive changelog\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "week-changelog/2026-07-20.md\n"
        "--- end Step 2.67 blocks ---\n"
    )
    gate_paths = ["week-changelog/2026-07-20.md", "archive/week-changelog/2026-07-20.md"]
    outcome = deletion_block_gate(msg, gate_paths=gate_paths, cwd=repo)
    assert outcome.passed is True
    assert outcome.diagnostics == []


def test_deletion_gate_assertion_3_unrelated_rename_still_needs_no_block(tmp_path):
    """The widened Assertion-1 set must NOT leak into Assertion-3 (F3): an
    ordinary content-preserving rename, staged with no Deleted claim at all
    in the message, must still pass -- exactly as it did before this fix
    (see `commit_gates` module docstring's negative-spec, "Rename lines are
    intentionally excluded", which stays true for Assertion-3)."""
    repo = _init_repo(tmp_path)
    content = "content block\n" * 40
    _seed_file(repo, "a.md", content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "a.md").unlink()
    (repo / "b.md").write_text(content, encoding="utf-8")
    _git(["add", "-A"], repo)

    msg = "rename a.md to b.md\n"
    outcome = deletion_block_gate(msg, gate_paths=["a.md", "b.md"], cwd=repo)
    assert outcome.passed is True
    assert outcome.diagnostics == []


# ---------------------------------------------------------------------------
# deletion_block_gate -- assertion (e): scoped F3, sibling deletion outside
# gate_paths never trips the gate
# ---------------------------------------------------------------------------


def test_deletion_gate_assertion_e_sibling_deletion_outside_scope_not_tripped(tmp_path):
    repo = _init_repo(tmp_path)
    sibling_del_file = "tasks/sibling-session/scratch.md"
    e_commit_file = "state/lessons/e-fixture.yaml"

    _seed_file(repo, sibling_del_file, "sibling scratch")
    _seed_file(repo, e_commit_file, "lesson content")
    _git(["add", "--", sibling_del_file, e_commit_file], repo)
    _git(["commit", "-q", "-m", "seed: e-fixture files"], repo)

    _git(["rm", "-q", sibling_del_file], repo)
    _seed_file(repo, e_commit_file, "updated lesson")
    _git(["add", "--", e_commit_file], repo)

    # No Step 2.67 block at all (subject-only session) -- the message the
    # deleted parity test's wsc-commit.sh caller would compose when only
    # --subject is passed.
    msg = "workstream-complete: e-fixture\n"

    outcome = deletion_block_gate(msg, gate_paths=[e_commit_file], cwd=repo)
    assert outcome.passed is True
    assert outcome.diagnostics == []


# ---------------------------------------------------------------------------
# deletion_block_gate -- native unit additions
# ---------------------------------------------------------------------------


def test_deletion_gate_skip_when_empty_gate_paths_and_no_block(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    outcome = deletion_block_gate("subject only\n", gate_paths=[], cwd=repo)
    assert outcome.passed is True
    assert outcome.skipped is True


def test_deletion_gate_kept_claim_missing_is_mismatch(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    msg = (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"nowhere/ghost.md{_EM_DASH}never existed\n"
        "--- end Step 2.67 blocks ---\n"
    )
    outcome = deletion_block_gate(msg, gate_paths=["nowhere/ghost.md"], cwd=repo)
    assert outcome.passed is False
    assert any("does not exist at HEAD or in staged set" in d for d in outcome.diagnostics)


def test_deletion_gate_malformed_kept_line_is_mismatch(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    msg = (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        "README.md\n"
        "--- end Step 2.67 blocks ---\n"
    )
    outcome = deletion_block_gate(msg, gate_paths=["README.md"], cwd=repo)
    assert outcome.passed is False
    assert any("no em-dash separator" in d for d in outcome.diagnostics)


def test_deletion_gate_f3_inverse_check_trips_on_own_unblocked_deletion(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a/gone.md", "x")
    _git(["add", "--", "a/gone.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["rm", "-q", "a/gone.md"], repo)

    outcome = deletion_block_gate("subject only\n", gate_paths=["a/gone.md"], cwd=repo)
    assert outcome.passed is False
    assert any("no Step 2.67 block" in d for d in outcome.diagnostics)


def test_deletion_gate_unmerged_index_entry_refuses_not_crashes(tmp_path):
    """F1 (code-review, P1): `read_index` raises `IndexParseError` on ANY
    unmerged (stage != 0) index entry -- an ordinary mid-merge-conflict repo
    state. Assertion-2's index read (only reached when the message carries a
    Kept-claim) must degrade to a refusal, never propagate the raise up
    through `commit_pipeline.commit()` and crash the op."""
    repo = _make_conflicted_repo(tmp_path)

    msg = (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"conflict.md{_EM_DASH}still needed\n"
        "--- end Step 2.67 blocks ---\n"
    )
    outcome = deletion_block_gate(msg, gate_paths=["conflict.md"], cwd=repo)
    assert outcome.passed is False
    assert any("staged index unreadable" in d for d in outcome.diagnostics)


# ---------------------------------------------------------------------------
# carry_gate
# ---------------------------------------------------------------------------


def _seed_handoff(repo: Path, rel_path: str, frontmatter_body: str) -> None:
    _seed_file(
        repo,
        rel_path,
        f"---\nstatus: open\n{frontmatter_body}---\n\n# handoff\n",
    )


def test_carry_gate_skips_when_no_handoff_in_gate_paths(tmp_path):
    """AC5: an empty filtered set (no `state/handoffs/*.md` in `gate_paths`)
    skips entirely -- no file read at all."""
    repo = _init_repo(tmp_path)
    outcome = carry_gate(repo, gate_paths=["a/one.md", "state/other/x.md"])
    assert outcome == GateOutcome(passed=True, skipped=True, diagnostics=[])


def test_carry_gate_empty_gate_paths_skips(tmp_path):
    repo = _init_repo(tmp_path)
    outcome = carry_gate(repo, gate_paths=[])
    assert outcome == GateOutcome(passed=True, skipped=True, diagnostics=[])


def test_carry_gate_well_formed_carried_items_passes(tmp_path):
    """AC4: well-formed carried_items (non-terminal `carried`, plus a
    terminal `closed` WITH a disposition_detail) commits normally."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-x.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    description: still open\n"
        "    disposition: carried\n"
        "  - carry_id: c2\n"
        "    description: done\n"
        "    disposition: closed\n"
        "    disposition_detail: closed -- superseded by c3\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-x.md"])
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


def test_carry_gate_absent_carried_items_key_passes(tmp_path):
    """AC4: a handoff with NO `carried_items` key at all is a legitimate
    green -- absence is not the vacuous-pass hazard named in the plan's
    Anti-scope (that hazard concerned authoring-time validation with no
    staged-path precondition; this gate only ever fires on a staged
    handoff)."""
    repo = _init_repo(tmp_path)
    _seed_handoff(repo, "state/handoffs/2026-08-10-y.md", "")
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-y.md"])
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


def test_carry_gate_terminal_disposition_missing_detail_refuses(tmp_path):
    """AC1: a terminal disposition (`blocked`) with no disposition_detail is
    a REFUSAL."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-z.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    description: stuck\n"
        "    disposition: blocked\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-z.md"])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("disposition_detail" in line for line in outcome.diagnostics)


def test_carry_gate_missing_carry_id_refuses(tmp_path):
    """AC2: a missing carry_id refuses, delegating to evaluate_gate rather
    than re-implementing the rule."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-a.md",
        "carried_items:\n"
        "  - description: no id\n"
        "    disposition: carried\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-a.md"])
    assert outcome.passed is False
    assert any("carry_id" in line for line in outcome.diagnostics)


def test_carry_gate_unrecognized_disposition_refuses(tmp_path):
    """AC2: a disposition outside the sanctioned set refuses."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-b.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    description: weird\n"
        "    disposition: not_a_real_disposition\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-b.md"])
    assert outcome.passed is False
    assert any("not_a_real_disposition" in line for line in outcome.diagnostics)


def test_carry_gate_preserves_violation_lines_verbatim(tmp_path):
    """AC3: `evaluate_gate`'s own violation text reaches `diagnostics`
    unchanged, prefixed only with the handoff path -- no re-wording."""
    from coordinator_core.ops.handoff_carry_gate import evaluate_gate

    items = [{"description": "no id", "disposition": "carried"}]
    expected_violation = evaluate_gate(items).violations[0]

    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-c.md",
        "carried_items:\n"
        "  - description: no id\n"
        "    disposition: carried\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-c.md"])
    assert f"state/handoffs/2026-08-10-c.md: {expected_violation}" in outcome.diagnostics


def test_carry_gate_refusal_includes_restage_hint(tmp_path):
    """AC6: a refusal's diagnostics tell the operator the path is left
    unstaged and must be re-staged after the fix."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-d.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    disposition: blocked\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-d.md"])
    assert outcome.passed is False
    assert any("unstaged" in line.lower() for line in outcome.diagnostics)


def test_carry_gate_pass_has_no_restage_hint(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_handoff(repo, "state/handoffs/2026-08-10-e.md", "")
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-e.md"])
    assert outcome.diagnostics == []


def test_carry_gate_unreadable_handoff_refuses(tmp_path):
    """AC7: a staged handoff path that EXISTS on disk but cannot be read
    (here: the path is a directory, not a readable file) is a REFUSAL, not
    a silent pass -- fail-loud matches `evaluate_gate`'s own never-fail-open
    contract. Deliberately NOT a missing path -- see
    `test_carry_gate_staged_deletion_path_absent_from_worktree_passes`
    below (AC8) for why an ABSENT path is a different case (a legitimate
    skip, not this refusal) that must not be conflated with this one."""
    repo = _init_repo(tmp_path)
    handoff_as_dir = repo / "state" / "handoffs" / "2026-08-10-unreadable.md"
    handoff_as_dir.mkdir(parents=True)
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-unreadable.md"])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("could not read handoff" in line for line in outcome.diagnostics)


def test_carry_gate_staged_deletion_path_absent_from_worktree_passes(tmp_path):
    """AC8: a `gate_paths` entry ABSENT from the worktree is the staged-
    deletion signal, not a generic missing-file case -- `compute_gate_paths`
    (`commit_message.py`) returns `[*commit_paths, *deleted_paths]`, so an
    EM-authored `deleted_paths` entry reaches `gate_paths` with no file
    behind it BY DESIGN (a `git rm state/handoffs/*.md`, e.g. `/distill`
    disposal). Refusing here would make deliberate handoff deletion
    impossible tree-wide -- there is nothing to carry-check about a file
    being removed, so this is a legitimate pass, never a refusal. Must key
    on the path genuinely not existing (checked BEFORE
    `read_carried_items` runs), never on catching the `OSError`
    `read_carried_items` itself would raise -- that would also swallow a
    genuinely unreadable EXISTING file (see the AC7 test above) and
    re-open AC7."""
    repo = _init_repo(tmp_path)
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-deleted.md"])
    assert outcome.passed is True
    assert outcome.diagnostics == []


def test_carry_gate_unparseable_frontmatter_refuses(tmp_path):
    """AC7: a handoff with no parseable YAML frontmatter block is a
    REFUSAL."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "state/handoffs/2026-08-10-f.md", "no frontmatter here\n")
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-f.md"])
    assert outcome.passed is False
    assert any("unparseable carried_items" in line for line in outcome.diagnostics)


def test_carry_gate_only_filters_handoff_paths(tmp_path):
    """Non-handoff paths in `gate_paths` are ignored -- only
    `state/handoffs/*.md` entries are read."""
    repo = _init_repo(tmp_path)
    _seed_handoff(repo, "state/handoffs/2026-08-10-g.md", "")
    outcome = carry_gate(
        repo,
        gate_paths=["some/other/file.py", "state/handoffs/2026-08-10-g.md"],
    )
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


# ---------------------------------------------------------------------------
# op_scope_coverage_gate
# ---------------------------------------------------------------------------

_REGISTRY_RELPATH = "coordinator_core/ops/_registry_map.py"
_OP_SCOPES_RELPATH = "coordinator_core/op_scopes.py"


def _seed_registry_map(repo: Path, op_names, *, valid: bool = True) -> None:
    p = repo / _REGISTRY_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        entries = "\n".join(f'    "{name}": "some.module",' for name in op_names)
        p.write_text(
            "from typing import Dict\n\n"
            f"OP_MODULE_MAP: Dict[str, str] = {{\n{entries}\n}}\n",
            encoding="utf-8",
        )
    else:
        # A dict spelled via a name that isn't OP_MODULE_MAP, so the target
        # binding is genuinely absent -- simulates a rename/restructure.
        p.write_text("SOME_OTHER_NAME = {}\n", encoding="utf-8")


def _seed_op_scopes(repo: Path, op_scope_pairs, *, valid: bool = True) -> None:
    p = repo / _OP_SCOPES_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        entries = "\n".join(f'    "{name}": "{scope}",' for name, scope in op_scope_pairs)
        p.write_text(
            "from typing import Dict\n\n"
            f"_OP_KEY_SCOPE: Dict[str, str] = {{\n{entries}\n}}\n",
            encoding="utf-8",
        )
    else:
        p.write_text("SOME_OTHER_NAME = {}\n", encoding="utf-8")


def test_op_scope_gate_skips_when_registry_map_not_staged(tmp_path):
    """Scope filter: `_registry_map.py` absent from `gate_paths` -> skip, no
    file read at all -- a commit that doesn't touch the registry has nothing
    to say."""
    repo = _init_repo(tmp_path)
    outcome = op_scope_coverage_gate(repo, gate_paths=["some/other/file.py"])
    assert outcome == GateOutcome(passed=True, skipped=True, diagnostics=[])


def test_op_scope_gate_refuses_unclassified_op(tmp_path):
    """Refuse: an op registered in OP_MODULE_MAP with no _OP_KEY_SCOPE entry."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["known.op", "unclassified.op"])
    _seed_op_scopes(repo, [("known.op", "none")])
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("unclassified.op" in line for line in outcome.diagnostics)
    assert not any("known.op" in line for line in outcome.diagnostics)


def test_op_scope_gate_passes_when_all_classified(tmp_path):
    """Pass: every registered op has an _OP_KEY_SCOPE entry -- a scope-table
    entry with no registry counterpart (legacy/test-only) does not trip the
    gate; direction is one-way."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a", "op.b"])
    _seed_op_scopes(
        repo,
        [("op.a", "none"), ("op.b", "common_dir"), ("legacy.only.in.scope.table", "none")],
    )
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


def test_op_scope_gate_absence_class_registry_map_deleted_skips(tmp_path):
    """Absence class 1: `_registry_map.py` staged in gate_paths but ABSENT from
    the worktree (a staged deletion) -> skip, not refuse."""
    repo = _init_repo(tmp_path)
    # Never write the file at all -- mirrors a staged deletion where the
    # worktree copy is already gone by gate time.
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome == GateOutcome(passed=True, skipped=True, diagnostics=[])


def test_op_scope_gate_absence_class_op_scopes_missing_refuses(tmp_path):
    """Absence class 2: `op_scopes.py` absent from the worktree -> refuse,
    never a silent pass -- the gate cannot verify coverage without it."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a"])
    # op_scopes.py deliberately never written.
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any(_OP_SCOPES_RELPATH in line for line in outcome.diagnostics)


def test_op_scope_gate_absence_class_dict_not_found_in_registry_refuses(tmp_path):
    """Absence class 3a: OP_MODULE_MAP not found by the AST walk (renamed/
    restructured) -> refuse, naming which dict was not found -- a parse
    failure must never read as 'no violations'."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, [], valid=False)
    _seed_op_scopes(repo, [("op.a", "none")])
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("OP_MODULE_MAP" in line for line in outcome.diagnostics)


def test_op_scope_gate_absence_class_dict_not_found_in_scopes_refuses(tmp_path):
    """Absence class 3b: _OP_KEY_SCOPE not found by the AST walk -> refuse,
    naming which dict was not found."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a"])
    _seed_op_scopes(repo, [], valid=False)
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("_OP_KEY_SCOPE" in line for line in outcome.diagnostics)


def test_op_scope_gate_refuses_on_double_module_level_rebind(tmp_path):
    """P2 hardening: two module-level rebinds of OP_MODULE_MAP (e.g. a
    placeholder `= {}` followed by a later genuine `= {...}` rebind) -> the
    gate cannot know which binding is authoritative and must refuse, naming
    the variable, rather than silently reading the first one `ast.walk`
    reaches."""
    repo = _init_repo(tmp_path)
    p = repo / _REGISTRY_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "from typing import Dict\n\n"
        "OP_MODULE_MAP: Dict[str, str] = {}\n\n"
        'OP_MODULE_MAP = {"op.a": "some.module"}\n',
        encoding="utf-8",
    )
    _seed_op_scopes(repo, [("op.a", "none")])
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("OP_MODULE_MAP" in line and "more than once" in line for line in outcome.diagnostics)


def test_op_scope_gate_same_named_local_does_not_count_as_second_binding(tmp_path):
    """A same-named local variable inside a function is NOT a module-level
    binding and must not trip the multiplicity refusal -- only `tree.body`
    (module-level statements) counts."""
    repo = _init_repo(tmp_path)
    p = repo / _REGISTRY_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "from typing import Dict\n\n"
        'OP_MODULE_MAP: Dict[str, str] = {"op.a": "some.module"}\n\n'
        "def _helper():\n"
        "    OP_MODULE_MAP = {}\n"
        "    return OP_MODULE_MAP\n",
        encoding="utf-8",
    )
    _seed_op_scopes(repo, [("op.a", "none")])
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


def test_op_scope_gate_op_scopes_present_but_unreadable_refuses(tmp_path, monkeypatch):
    """Present-but-unreadable, not absent: `op_scopes.py` exists on disk but
    raises `OSError` on read (permissions, encoding I/O failure, transient FS
    error) -> refuse, naming the path or the read failure -- a predicate the
    gate cannot evaluate must never fall through to 'no violations'."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a"])
    _seed_op_scopes(repo, [("op.a", "none")])

    real_read_text = Path.read_text

    def _flaky_read_text(self, *args, **kwargs):
        if self == repo / _OP_SCOPES_RELPATH:
            raise OSError("simulated unreadable file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any(
        _OP_SCOPES_RELPATH in line and "simulated unreadable file" in line
        for line in outcome.diagnostics
    )


def test_op_scope_gate_registry_map_present_but_unreadable_refuses(tmp_path, monkeypatch):
    """Mirrored present-but-unreadable case on the registry-map side:
    `_registry_map.py` exists but raises `OSError` on read -> refuse, naming
    the path or the read failure, same as the op_scopes.py side."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a"])

    real_read_text = Path.read_text

    def _flaky_read_text(self, *args, **kwargs):
        if self == repo / _REGISTRY_RELPATH:
            raise OSError("simulated unreadable file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any(
        _REGISTRY_RELPATH in line and "simulated unreadable file" in line
        for line in outcome.diagnostics
    )


# ---------------------------------------------------------------------------
# C3 equivalence fixture -- pre-re-point vs post-re-point parity
#
# C3 re-pointed deletion_block_gate's Kept-claim (Assertion-2)
# HEAD-membership leg onto `coordinator_core.git.git_state` (head_blobs) with
# no `git` process, but landed with NO new tests -- the pre-existing tests
# above predate the re-point and were never written to catch it changing an
# answer. This section is that missing equivalence proof: an oracle,
# reconstructed verbatim from the pre-re-point implementation (recovered via
# `git show 69f92af34^:coordinator_core/ops/ceremony/commit_gates.py`), run
# side-by-side with the live gate over the shapes the re-point's own module
# docstring calls out as load-bearing for the Kept-claim leg (a clean path,
# a path with a space, a non-ASCII path, a symlink, and a submodule
# gitlink).
#
# (This section originally also covered `dirty_tree_gate`'s companion
# staged-classification re-point with a matching oracle and a wider shape
# set -- mode-only divergence, content divergence, staged/worktree/add/
# delete, a CRLF EOL-phantom. That half was deleted alongside
# `dirty_tree_gate` itself under the brightline kill bar: the function had
# no production caller, so its equivalence proof went with it. The
# `deletion_block_gate` half below is untouched and still current.)
#
# The oracle reuses `parse_step267_blocks`, `_parse_name_status_deletions`,
# and `_parse_name_status_rename_sources` (helpers the re-point did NOT
# touch) and only re-implements the one leg that changed: the Kept-claim
# HEAD-membership check (was an unscoped `git ls-tree -r HEAD --name-only`
# walk, not the blob-type-filtered `head_blobs()`).
# ---------------------------------------------------------------------------


def _old_deletion_block_gate(
    msg_text: str, gate_paths, *, cwd, whole_index: bool = False
) -> GateOutcome:
    """Oracle: `deletion_block_gate` as it read BEFORE C3 -- Assertion-2's
    HEAD-membership leg was an unscoped `git ls-tree -r HEAD --name-only`
    walk over the WHOLE tree (every path, any object type), not
    `head_blobs()`'s targeted, blob-type-filtered lookup. Assertion-1 and
    Assertion-3 are UNCHANGED by C3 and are reproduced verbatim here only so
    this oracle is a complete, callable stand-in.
    """
    has_block = has_step267_block(msg_text)
    gate_scope = set(gate_paths)

    if not whole_index and not gate_paths and not has_block:
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    parsed = parse_step267_blocks(msg_text)

    name_status_result = _cg.diff_cached_name_status(cwd, find_renames=True)
    all_staged_deletions = _cg._parse_name_status_deletions(name_status_result.stdout)
    staged_deletions = (
        [p for p in all_staged_deletions if p in gate_scope]
        if gate_scope
        else all_staged_deletions
    )
    staged_deletions_set = set(staged_deletions)

    all_rename_sources = _cg._parse_name_status_rename_sources(name_status_result.stdout)
    rename_sources = (
        [p for p in all_rename_sources if p in gate_scope] if gate_scope else all_rename_sources
    )
    staged_or_renamed_away_set = staged_deletions_set | set(rename_sources)

    staged_all_set = set()
    tracked_at_head = set()
    if parsed.kept_claimed:
        name_only_result = _cg._git(["diff", "--cached", "--name-only"], cwd=cwd)
        all_staged = [p for p in name_only_result.stdout.splitlines() if p]
        staged_all = [p for p in all_staged if p in gate_scope] if gate_scope else all_staged
        staged_all_set = set(staged_all)

        ls_tree_result = _cg._git(["ls-tree", "-r", "HEAD", "--name-only"], cwd=cwd)
        tracked_at_head = {p for p in ls_tree_result.stdout.splitlines() if p}

    diagnostics = []
    for path in parsed.deleted_claimed:
        if path not in staged_or_renamed_away_set:
            diagnostics.append(f"Deleted-claim NOT staged for deletion: {path}")

    for path in parsed.kept_claimed:
        if path not in tracked_at_head and path not in staged_all_set:
            diagnostics.append(f"Kept-claim does not exist at HEAD or in staged set: {path}")

    for raw in parsed.kept_malformed:
        diagnostics.append(
            "Kept-line has no em-dash separator (unparseable, expected "
            f"'<path> — <reason>'): {raw}"
        )

    if staged_deletions and not has_block:
        diagnostics.append("Staged deletions present but commit body has no Step 2.67 block:")
        for path in staged_deletions:
            diagnostics.append(f"  {path}")

    return GateOutcome(passed=not diagnostics, skipped=False, diagnostics=diagnostics)


def _git_stdout(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


# --- deletion_block_gate Kept-claim shape builders --------------------------


def _shape_clean(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "clean.txt", "unchanged\n")
    _git(["add", "--", "clean.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _shape_space_path(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a dir/has space.txt", "base\n")
    _git(["add", "--", "a dir/has space.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "a dir/has space.txt", "edited\n")
    return repo


def _shape_nonascii_path(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "café-résumé.txt", "base\n")
    _git(["add", "--", "café-résumé.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "café-résumé.txt", "edited\n")
    return repo


def _shape_symlink(tmp_path):
    """A symlink (mode 120000) tracked at HEAD, SYNTHESISED via a direct
    index write -- this repo runs core.symlinks=false, so a worktree
    symlink would never round-trip through git as one."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    # cacheinfo needs stdin text, not argv -- git hash-object reads stdin.
    proc = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo), input="target.txt", capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )
    blob = proc.stdout.strip()
    _git(["update-index", "--add", "--cacheinfo", f"120000,{blob},link-me"], repo)
    _git(["commit", "-q", "-m", "add symlink"], repo)
    return repo


def _shape_gitlink(tmp_path):
    """A submodule gitlink (mode 160000) tracked at HEAD, SYNTHESISED via a
    direct index write -- this repo's own index contains no 160000 entry at
    all to build a fixture from. The pointed-to sha need not be a real
    submodule commit; git's gitlink object never resolves it locally."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    head_sha = _git_stdout(["rev-parse", "HEAD"], repo)
    _git(["update-index", "--add", "--cacheinfo", f"160000,{head_sha},vendor/sub"], repo)
    _git(["commit", "-q", "-m", "add gitlink"], repo)
    return repo


def _kept_claim_message(path: str) -> str:
    return (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"{path}{_EM_DASH}still needed\n"
        "--- end Step 2.67 blocks ---\n"
    )


@pytest.mark.parametrize(
    "shape,path",
    [
        pytest.param(_shape_clean, "clean.txt", id="clean_path"),
        pytest.param(_shape_space_path, "a dir/has space.txt", id="path_with_space"),
        pytest.param(_shape_nonascii_path, "café-résumé.txt", id="nonascii_path"),
        pytest.param(_shape_symlink, "link-me", id="symlink_120000"),
        pytest.param(_shape_gitlink, "vendor/sub", id="gitlink_160000"),
    ],
)
def test_deletion_gate_kept_claim_matches_pre_c3_oracle(tmp_path, shape, path):
    """AC (undischarged by C3): `deletion_block_gate`'s Assertion-2
    (Kept-claim) re-pointed HEAD-membership leg (`head_blobs`) returns a
    SET-IDENTICAL answer to the pre-re-point (unscoped `git ls-tree -r HEAD
    --name-only`, no type filter) implementation.

    `head_blobs()` was patched (REGRESSION-1 fix) to admit `obj_type ==
    "commit"` (a 160000 gitlink) alongside `"blob"` -- only `"tree"` is
    excluded now -- so `gitlink_160000` matches the oracle here exactly, no
    special-case needed.

    KNOWN, REPORTED DIVERGENCE (REGRESSION-2 fix; not silently dropped --
    see the executor report): the oracle's HEAD-membership leg is an
    unscoped, UNQUOTED-nothing `git ls-tree -r HEAD --name-only` walk --
    default C-quoting applies, so a non-ASCII Kept-claimed path never
    matches the oracle's `tracked_at_head` set and the oracle wrongly
    reports it missing. `nonascii_path` is EXPECTED TO DIVERGE from the
    oracle here in ONE direction only (live.passed=True, oracle.passed=
    False) -- the live gate (`head_blobs`, unquoted `ls-tree -z`) is the
    CORRECT answer, not bug-for-bug parity with the oracle's own quoting bug.
    """
    repo = shape(tmp_path)
    msg = _kept_claim_message(path)
    live = deletion_block_gate(msg, gate_paths=[path], cwd=repo)
    oracle = _old_deletion_block_gate(msg, gate_paths=[path], cwd=repo)

    if path == "café-résumé.txt":
        assert oracle.passed is False, (
            f"oracle={oracle!r} -- expected the oracle's C-quoted ls-tree "
            "lookup to keep missing this non-ASCII path; if this now "
            "passes, the oracle's own bug may have been fixed elsewhere "
            "and this special-case should be removed"
        )
        assert live.passed is True, (
            f"live={live!r} -- expected the live (unquoted `ls-tree -z` via "
            "head_blobs) gate to correctly find this Kept-claim at HEAD"
        )
        return

    assert live.passed == oracle.passed, (
        f"live={live!r} oracle={oracle!r} -- head_blobs() vs unscoped "
        "ls-tree diverged on Kept-claim HEAD membership (see this test's "
        "own docstring)"
    )
    assert live.diagnostics == oracle.diagnostics
