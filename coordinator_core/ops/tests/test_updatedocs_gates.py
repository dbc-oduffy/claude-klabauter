"""
coordinator_core.ops.tests.test_updatedocs_gates

Tests for the "updatedocs.gates" op and its gate battery
(coordinator_core.ops.updatedocs_gates).

Import guard: coordinator_core.ops.updatedocs_gates MUST be imported at module
load time so @register_op("updatedocs.gates") fires and populates _REGISTRY.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) GateVerdict.UNAVAILABLE fires (not CLEAN) when an external CLI is absent
      — the core Pattern-A distinction this op exists to preserve
  (c) 11g-plugin-wiki: exit 0 with a nonzero "missing-bundled" count in stdout
      produces CONTRADICTION, never CLEAN — the memo's own worked example
  (d) 11h-skill-anchor-links: exit 1 (dead anchors) vs exit 2 (could not
      check) never collapse into the same verdict
  (e) rollup() is a pure function — severity determines "halt", not gate id
  (f) fresh-scaffold-probe fires on the 3-axis-AND fresh repo, not otherwise
  (g) distill-threshold fire/no-fire arithmetic preserved from the ported
      update-docs-probes.py logic
  (h) end-to-end: the "updatedocs.gates" op dispatch returns a verdict array
      + rollup for a `params["gates"]` subset
  (i) unknown gate id in params["gates"] raises ValueError (no silent skip)

Spec backlink: cross-repo/inbox/2026-08-06-example-doctrine-repo-em-updatedocs-gates-
  structured-verdicts.md
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("updatedocs.gates") as a side-effect.
# ---------------------------------------------------------------------------
import coordinator_core.ops.updatedocs_gates as udg  # noqa: F401
from coordinator_core.ipc import _REGISTRY


def _run(coro):
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


def _write_fake_cli(path: Path, exit_code: int, stdout: str = "", stderr: str = "") -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# (a) registry
# ---------------------------------------------------------------------------


def test_op_registered():
    assert "updatedocs.gates" in _REGISTRY


# ---------------------------------------------------------------------------
# (b) UNAVAILABLE vs CLEAN — a missing CLI must never read as "clean"
# ---------------------------------------------------------------------------


def test_lens_orthogonality_unavailable_when_cli_missing(tmp_path):
    result = udg._gate_lens_orthogonality(tmp_path, tmp_path / "no-such-settings-home", {})
    assert result.verdict == udg.GateVerdict.UNAVAILABLE
    assert result.severity is None


def test_lens_orthogonality_clean_when_cli_exits_zero(tmp_path):
    cli = tmp_path / "verify-parallel-review-lens-orthogonality"
    _write_fake_cli(cli, 0)
    result = udg._gate_lens_orthogonality(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.CLEAN


def test_lens_orthogonality_finding_when_cli_exits_nonzero(tmp_path):
    cli = tmp_path / "verify-parallel-review-lens-orthogonality"
    _write_fake_cli(cli, 1)
    result = udg._gate_lens_orthogonality(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.INFORMATIONAL


# ---------------------------------------------------------------------------
# (c) 11g-plugin-wiki — CONTRADICTION, not CLEAN, on exit 0 + nonzero warnings
# ---------------------------------------------------------------------------


def test_plugin_wiki_contradiction_on_clean_exit_with_warnings(tmp_path):
    cli = tmp_path / "sync-plugin-wiki"
    _write_fake_cli(cli, 0, stdout="clean (161 validated, 31 missing-bundled warnings)\n")
    result = udg._gate_plugin_wiki(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.CONTRADICTION
    assert result.detail["missing_bundled"] == 31
    assert result.detail["validated"] == 161


def test_plugin_wiki_clean_when_no_warnings(tmp_path):
    cli = tmp_path / "sync-plugin-wiki"
    _write_fake_cli(cli, 0, stdout="clean (161 validated)\n")
    result = udg._gate_plugin_wiki(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.CLEAN


def test_plugin_wiki_dev_mirror_exit_5_is_blocking_finding(tmp_path):
    cli = tmp_path / "sync-plugin-wiki"
    _write_fake_cli(cli, 5, stdout="dev mirror found\n")
    result = udg._gate_plugin_wiki(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.BLOCKING


# ---------------------------------------------------------------------------
# (d) 11h-skill-anchor-links — exit 1 vs exit 2 never collapse
# ---------------------------------------------------------------------------


def test_skill_anchor_links_exit1_is_blocking_finding_not_unavailable(tmp_path):
    cli = tmp_path / "verify-skill-anchor-links"
    _write_fake_cli(cli, 1, stdout="dead anchor found\n")
    result = udg._gate_skill_anchor_links(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.BLOCKING


def test_skill_anchor_links_exit2_is_unavailable_not_clean(tmp_path):
    cli = tmp_path / "verify-skill-anchor-links"
    _write_fake_cli(cli, 2, stderr="doctrine-surfaces.json unresolvable\n")
    result = udg._gate_skill_anchor_links(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.UNAVAILABLE
    assert result.severity is None


def test_skill_anchor_links_exit0_with_unresolved_is_finding_not_clean(tmp_path):
    cli = tmp_path / "verify-skill-anchor-links"
    _write_fake_cli(cli, 0, stdout="clean (40 total, 38 qualified, 2 unresolved)\n")
    result = udg._gate_skill_anchor_links(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.INFORMATIONAL


def test_skill_anchor_links_exit0_clean(tmp_path):
    cli = tmp_path / "verify-skill-anchor-links"
    _write_fake_cli(cli, 0, stdout="clean (40 total, 40 qualified, 0 unresolved)\n")
    result = udg._gate_skill_anchor_links(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.CLEAN


# ---------------------------------------------------------------------------
# (e) rollup() — pure function over the verdict array
# ---------------------------------------------------------------------------


def test_rollup_halts_on_any_blocking_finding():
    results = [
        udg.GateResult("g1", udg.GateVerdict.CLEAN, "clean"),
        udg.GateResult("g2", udg.GateVerdict.FINDING, "bad", severity=udg.Severity.BLOCKING),
        udg.GateResult("g3", udg.GateVerdict.FINDING, "meh", severity=udg.Severity.INFORMATIONAL),
    ]
    r = udg.rollup(results)
    assert r["halt"] is True
    assert r["blocking"] == ["g2"]
    assert r["informational"] == ["g3"]
    assert r["clean"] == ["g1"]
    assert r["gate_count"] == 3


def test_rollup_no_halt_when_only_informational():
    results = [
        udg.GateResult("g1", udg.GateVerdict.FINDING, "meh", severity=udg.Severity.INFORMATIONAL),
    ]
    r = udg.rollup(results)
    assert r["halt"] is False


def test_rollup_tracks_unavailable_and_contradiction_separately():
    results = [
        udg.GateResult("g1", udg.GateVerdict.UNAVAILABLE, "no cli"),
        udg.GateResult("g2", udg.GateVerdict.CONTRADICTION, "clean but warnings", severity=udg.Severity.INFORMATIONAL),
    ]
    r = udg.rollup(results)
    assert r["unavailable"] == ["g1"]
    assert r["contradiction"] == ["g2"]
    assert r["halt"] is False


# ---------------------------------------------------------------------------
# (f) fresh-scaffold-probe — 3-axis AND
# ---------------------------------------------------------------------------


def test_fresh_scaffold_probe_fires_on_all_three_axes(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x")
    result = udg._gate_fresh_scaffold_probe(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.INFORMATIONAL


def test_fresh_scaffold_probe_clean_when_content_present(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x")
    (tmp_path / "DIRECTORY.md").write_text("x")
    result = udg._gate_fresh_scaffold_probe(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.CLEAN


def test_fresh_scaffold_probe_unavailable_outside_repo_root(tmp_path):
    result = udg._gate_fresh_scaffold_probe(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.UNAVAILABLE


# ---------------------------------------------------------------------------
# (g) distill-threshold arithmetic
# ---------------------------------------------------------------------------


def test_distill_threshold_fires_at_50_plus(tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    for i in range(50):
        (plans / f"p{i}.md").write_text("x")
    result = udg._gate_distill_threshold(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.detail["total"] == 50


def test_distill_threshold_clean_below_threshold(tmp_path):
    result = udg._gate_distill_threshold(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.CLEAN


# ---------------------------------------------------------------------------
# (h) end-to-end op dispatch
# ---------------------------------------------------------------------------


def test_op_end_to_end_runs_requested_subset(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x")
    (tmp_path / "DIRECTORY.md").write_text("x")
    reply = _run(
        udg._updatedocs_gates(
            {"repo_root": str(tmp_path), "gates": ["fresh-scaffold-probe", "distill-threshold"]},
            repo_root=None,
        )
    )
    gate_ids = [g["gate_id"] for g in reply["gates"]]
    assert gate_ids == ["fresh-scaffold-probe", "distill-threshold"]
    assert reply["rollup"]["gate_count"] == 2
    assert all(g["verdict"] == "clean" for g in reply["gates"])


def test_op_unknown_gate_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        _run(udg._updatedocs_gates({"repo_root": str(tmp_path), "gates": ["not-a-real-gate"]}, repo_root=None))


# ---------------------------------------------------------------------------
# (j) _run interpreter selection — regression guard for the P1 os.access(X_OK)
# defect: a .py CLI MUST always get the explicit sys.executable prefix,
# regardless of its own executable bit (a checked-out .py never needs +x —
# os.access(X_OK) on Windows would report True for ANY file and mask this).
# ---------------------------------------------------------------------------


def test_run_execs_py_cli_via_sys_executable_even_without_exec_bit(tmp_path):
    cli = tmp_path / "check-rag-state.py"
    cli.write_text("import sys\nsys.stdout.write('ran')\nsys.exit(0)\n", encoding="utf-8")
    # Deliberately no chmod +x — a shell-exec-direct branch would fail to
    # spawn this at all on POSIX; the sys.executable branch does not care.
    proc = udg._run(cli, [])
    assert proc is not None
    assert proc.returncode == 0
    assert proc.stdout == "ran"


def test_run_non_py_cli_execs_directly_not_via_interpreter(tmp_path):
    cli = tmp_path / "verify-something"
    _write_fake_cli(cli, 0, stdout="ok")
    proc = udg._run(cli, [])
    assert proc is not None
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# (k) _run timeout — a hung CLI must surface as UNAVAILABLE, never CLEAN,
# and must never raise TimeoutExpired out of _run.
# ---------------------------------------------------------------------------


def test_run_timeout_returns_none_not_raise(tmp_path):
    cli = tmp_path / "hangs.py"
    cli.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    proc = udg._run(cli, [], timeout=0.05)
    assert proc is None


def test_lens_orthogonality_unavailable_not_clean_on_timeout(tmp_path):
    cli = tmp_path / "verify-parallel-review-lens-orthogonality"
    cli.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n", encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    orig_timeout = udg._DEFAULT_CLI_TIMEOUT_SECONDS
    udg._DEFAULT_CLI_TIMEOUT_SECONDS = 0.05
    try:
        result = udg._gate_lens_orthogonality(tmp_path, tmp_path, {"cli": str(cli)})
    finally:
        udg._DEFAULT_CLI_TIMEOUT_SECONDS = orig_timeout
    assert result.verdict == udg.GateVerdict.UNAVAILABLE
    assert result.verdict != udg.GateVerdict.CLEAN


# ---------------------------------------------------------------------------
# (l) 11i-queue-prune-sweep — YAML leg exit code MUST be consumed, not
# discarded (P1 fix regression guard: the F9 fix's whole purpose was a
# caller being able to branch on prune-closed-*.py's non-zero exit).
# ---------------------------------------------------------------------------


def test_queue_prune_sweep_yaml_leg_failure_is_blocking_not_clean(tmp_path):
    (tmp_path / "state" / "improvement-queue").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir / "prune-closed-improvements.py", 1, stderr="cannot lock ref 'HEAD'\n")
    result = udg._gate_queue_prune_sweep(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.BLOCKING
    assert any("YAML prune FAILED" in line for line in result.detail["lines"])


def test_queue_prune_sweep_yaml_leg_clean_exit_is_not_blocking(tmp_path):
    (tmp_path / "state" / "improvement-queue").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir / "prune-closed-improvements.py", 0)
    result = udg._gate_queue_prune_sweep(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.CLEAN


# ---------------------------------------------------------------------------
# (m) regex-canary — a parse-shape drift on the sole CONTRADICTION-detection
# mechanism must surface as UNAVAILABLE, never silently default to CLEAN.
# ---------------------------------------------------------------------------


def test_plugin_wiki_unavailable_when_validated_count_does_not_parse(tmp_path):
    cli = tmp_path / "sync-plugin-wiki"
    _write_fake_cli(cli, 0, stdout="all good, nothing to report\n")
    result = udg._gate_plugin_wiki(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.UNAVAILABLE
    assert result.verdict != udg.GateVerdict.CLEAN


def test_skill_anchor_links_unavailable_when_unresolved_word_present_but_uncounted(tmp_path):
    cli = tmp_path / "verify-skill-anchor-links"
    _write_fake_cli(cli, 0, stdout="clean, some unresolved somewhere\n")
    result = udg._gate_skill_anchor_links(tmp_path, tmp_path, {"cli": str(cli)})
    assert result.verdict == udg.GateVerdict.UNAVAILABLE
    assert result.verdict != udg.GateVerdict.CLEAN
