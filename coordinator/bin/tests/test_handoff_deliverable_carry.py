"""test_handoff_deliverable_carry — pytest coverage for handoff-deliverable-carry.py.

Purpose: exercises the deliverable_id/initiative-FK carry-or-mint cascade ported out
of example-doctrine-repo coordinator/skills/handoff/SKILL.md's `# C3d — deliverable_id
auto-inheritance (D1 carry-not-remint rule)` bash block, into
coordinator/bin/handoff-deliverable-carry.py.

Coverage:
  1. Carry from plan frontmatter — plan carries deliverable_id -> echoed unchanged
     (carry path), initiative also carried from plan.
  2. Carry falls back to predecessor when plan lacks deliverable_id/initiative.
  3. Mint-from-slug when neither plan nor predecessor supplies a deliverable_id.
  4. Missing plan/predecessor paths degrade gracefully (mint-from-slug, empty
     initiative) rather than raising.
  5. CLI subprocess invocation end-to-end: stdout carries exactly the two
     `DLVR_ID=`/`INITIATIVE_ID=` assignment lines the `eval "$(...)"` calling
     convention depends on.
  6. Dropped-join fail-loud — active plan present but yields no deliverable_id and
     predecessor also yields nothing -> raises `DroppedDeliverableJoinError` (library
     level) / exits 4 (CLI level), instead of silently degrading to mint-from-slug.
  7. Divergent-join fail-loud — plan AND predecessor both yield a non-empty
     deliverable_id and the two values disagree -> exits 5 (CLI level), naming both
     values in stderr, instead of silently picking one. Library-level coverage
     (`DivergentDeliverableIdError`, both-values-and-paths message, no-winner-picked)
     lives in coordinator_core/ops/test_deliverable_carry.py.

Spec backlink: coordinator/skills/handoff/SKILL.md § Deliverable-spine threading
               (D1 carry-not-remint) — example-doctrine-repo, C3d
               docs/decisions/DR-207-deliverable-spine-initiative-entity.md DD#1
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys

import pytest

from coordinator_core.win_portability import no_console_creationflags

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLI = os.path.normpath(os.path.join(_HERE, "..", "handoff-deliverable-carry.py"))
_LIB_DIR = os.path.normpath(os.path.join(_HERE, "..", "lib"))

if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("handoff_deliverable_carry", _CLI)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)  # type: ignore[union-attr]

from cc_invoke import _resolve_claude_klabauter_root, child_env  # noqa: E402


def _write_frontmatter(path, **fields):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# body")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _load_ops():
    claude_klabauter_root = _resolve_claude_klabauter_root()
    if claude_klabauter_root not in sys.path:
        sys.path.insert(0, claude_klabauter_root)
    from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field
    from coordinator_core.ops.mint_deliverable_id import mint

    return read_frontmatter_field, mint


def test_carry_from_plan_frontmatter(tmp_path):
    read_frontmatter_field, mint = _load_ops()
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan, deliverable_id="dlv-existing-thing-abc123", initiative="init-foo")

    dlvr_id, initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(plan), None
    )

    assert dlvr_id == "dlv-existing-thing-abc123"
    assert initiative_id == "init-foo"


def test_falls_back_to_predecessor_when_plan_lacks_fields(tmp_path):
    read_frontmatter_field, mint = _load_ops()
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan)  # no deliverable_id, no initiative
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(predecessor, deliverable_id="dlv-predecessor-xyz789", initiative="init-bar")

    dlvr_id, initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(plan), str(predecessor)
    )

    assert dlvr_id == "dlv-predecessor-xyz789"
    assert initiative_id == "init-bar"


def test_mints_fresh_slug_when_no_parent_id_discoverable(tmp_path):
    read_frontmatter_field, mint = _load_ops()

    dlvr_id, initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, None
    )

    today = datetime.date.today().strftime("%Y%m%d")
    assert dlvr_id.startswith(f"dlv-{today}-handoff-")
    assert initiative_id == ""


def test_missing_plan_and_predecessor_paths_degrade_gracefully(tmp_path):
    read_frontmatter_field, mint = _load_ops()
    nonexistent_plan = str(tmp_path / "does-not-exist-plan.md")
    nonexistent_predecessor = str(tmp_path / "does-not-exist-predecessor.md")

    dlvr_id, initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, nonexistent_plan, nonexistent_predecessor
    )

    today = datetime.date.today().strftime("%Y%m%d")
    assert dlvr_id.startswith(f"dlv-{today}-handoff-")
    assert initiative_id == ""


def test_active_plan_with_deliverable_id_carries_no_failure(tmp_path):
    """Case 1 — active plan names a deliverable_id -> unchanged carry path, no failure."""
    read_frontmatter_field, mint = _load_ops()
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan, deliverable_id="dlv-populated-thing-abc123")

    dlvr_id, _initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(plan), None
    )

    assert dlvr_id == "dlv-populated-thing-abc123"


def test_active_plan_without_deliverable_id_and_no_predecessor_fails_loud(tmp_path):
    """Case 2 — active plan present but carries NO deliverable_id, no predecessor
    to fall back to -> this is a dropped join, must fail loud (not silently mint)."""
    read_frontmatter_field, mint = _load_ops()
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan)  # no deliverable_id

    with pytest.raises(_module.DroppedDeliverableJoinError):
        _module.resolve_deliverable_and_initiative(read_frontmatter_field, mint, str(plan), None)


def test_no_active_plan_and_no_predecessor_stays_benign_mint(tmp_path):
    """Case 3 — nothing to carry from at all -> unchanged benign mint-from-slug."""
    read_frontmatter_field, mint = _load_ops()

    dlvr_id, initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, None
    )

    today = datetime.date.today().strftime("%Y%m%d")
    assert dlvr_id.startswith(f"dlv-{today}-handoff-")
    assert initiative_id == ""


def test_cli_subprocess_exits_dropped_join_fail_when_plan_lacks_deliverable_id(tmp_path):
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan, initiative="init-only-no-deliverable")

    proc = subprocess.run(
        [sys.executable, _CLI, "resolve", "--plan-file", str(plan)],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 4
    assert "dropped deliverable_id join" in proc.stderr
    assert "carry path" not in proc.stderr
    assert "mint-from-slug path" not in proc.stderr


def test_cli_subprocess_emits_shell_assignment_lines(tmp_path):
    plan = tmp_path / "plan.md"
    _write_frontmatter(plan, deliverable_id="dlv-cli-check-111222", initiative="init-cli")

    proc = subprocess.run(
        [sys.executable, _CLI, "resolve", "--plan-file", str(plan)],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, proc.stderr
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines == [
        "DLVR_ID=dlv-cli-check-111222",
        "INITIATIVE_ID=init-cli",
    ]
    assert "carry path" in proc.stderr


def test_cli_subprocess_exits_divergent_join_fail_when_plan_and_predecessor_disagree(tmp_path):
    plan = tmp_path / "plan.md"
    predecessor = tmp_path / "predecessor.md"
    _write_frontmatter(plan, deliverable_id="dlv-plan-side-111")
    _write_frontmatter(predecessor, deliverable_id="dlv-predecessor-side-222")

    proc = subprocess.run(
        [sys.executable, _CLI, "resolve", "--plan-file", str(plan), "--predecessor", str(predecessor)],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 5
    assert "divergent deliverable_id join" in proc.stderr
    assert "dlv-plan-side-111" in proc.stderr
    assert "dlv-predecessor-side-222" in proc.stderr
    assert proc.stdout == ""


def test_cli_missing_subcommand_exits_nonzero():
    proc = subprocess.run(
        [sys.executable, _CLI],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )
    assert proc.returncode != 0


def test_additional_predecessor_flag_accumulates_via_append(tmp_path):
    """Review: coordinator:code-reviewer af8ffeae P2 finding 1 — repeated
    --additional-predecessor flags accumulate and EVERY accumulated rung reaches the
    cascade's comparison.

    Negative-spec: this must exercise the real CLI, never a locally-reconstructed
    argparse parser — a parser rebuilt in the test body asserts that argparse's
    `append` action works (it does) and would keep passing if the flag were deleted
    from the CLI outright. Three mutually-divergent rungs are used so the raise has to
    enumerate all of them: a CLI that dropped every flag but the last would still exit
    5, and only the all-three-ids assertion distinguishes accumulation from that."""
    plan = tmp_path / "plan.md"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    _write_frontmatter(plan, deliverable_id="dlv-accum-plan-777")
    _write_frontmatter(first, deliverable_id="dlv-accum-first-888")
    _write_frontmatter(second, deliverable_id="dlv-accum-second-999")

    proc = subprocess.run(
        [
            sys.executable,
            _CLI,
            "resolve",
            "--plan-file",
            str(plan),
            "--additional-predecessor",
            str(first),
            "--additional-predecessor",
            str(second),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 5
    assert "dlv-accum-plan-777" in proc.stderr
    assert "dlv-accum-first-888" in proc.stderr
    assert "dlv-accum-second-999" in proc.stderr
    assert proc.stdout == ""


def test_additional_predecessor_flag_absent_defaults_to_empty_list():
    """Flag omitted -> args.additional_predecessor defaults to []."""
    proc = subprocess.run(
        [sys.executable, _CLI, "resolve"],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )
    today = datetime.date.today().strftime("%Y%m%d")
    assert proc.returncode == 0, proc.stderr
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines[0].startswith(f"DLVR_ID=dlv-{today}-handoff-")
    assert stdout_lines[1] == "INITIATIVE_ID="


def test_additional_predecessor_reaches_cascade_kwarg_and_flags_divergence(tmp_path):
    """Pass-through from the CLI flag to resolve_deliverable_and_initiative's
    additional_predecessors kwarg, including a divergence case that exits non-zero
    (mirrors the plan/predecessor divergent-join CLI test above, but sourced from
    --additional-predecessor instead of --predecessor)."""
    plan = tmp_path / "plan.md"
    extra = tmp_path / "extra.md"
    _write_frontmatter(plan, deliverable_id="dlv-plan-side-333")
    _write_frontmatter(extra, deliverable_id="dlv-extra-side-444")

    proc = subprocess.run(
        [
            sys.executable,
            _CLI,
            "resolve",
            "--plan-file",
            str(plan),
            "--additional-predecessor",
            str(extra),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 5
    assert "divergent deliverable_id join" in proc.stderr
    assert "dlv-plan-side-333" in proc.stderr
    assert "dlv-extra-side-444" in proc.stderr
    assert proc.stdout == ""


def test_additional_predecessor_reaches_cascade_kwarg_when_agreeing(tmp_path):
    """Same pass-through, agreeing case: no divergence, carry proceeds normally,
    confirming the additional-predecessor value actually reached the kwarg (a plan
    value alone would also exit 0, so this pins that the extra rung was read and
    compared, not merely accepted and ignored)."""
    plan = tmp_path / "plan.md"
    extra = tmp_path / "extra.md"
    _write_frontmatter(plan, deliverable_id="dlv-agree-555")
    _write_frontmatter(extra, deliverable_id="dlv-agree-555")

    proc = subprocess.run(
        [
            sys.executable,
            _CLI,
            "resolve",
            "--plan-file",
            str(plan),
            "--additional-predecessor",
            str(extra),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, proc.stderr
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines == ["DLVR_ID=dlv-agree-555", "INITIATIVE_ID="]
