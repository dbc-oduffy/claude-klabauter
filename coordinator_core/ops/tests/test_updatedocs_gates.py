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

Spec backlink: cross-repo/inbox/2026-08-06-doe-claude-em-updatedocs-gates-
  structured-verdicts.md
"""

from __future__ import annotations

import asyncio
import stat
import subprocess
import sys
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
# (l2) 11i-queue-prune-sweep — the default-callee legacy-markdown leg batches
# every existing queue into ONE spawn of prune-resolved-queue-entries.py
# (amplification-gate fix: the callee's old one-positional arity was
# self-imposed, not a real per-item constraint).
# ---------------------------------------------------------------------------


def test_queue_prune_sweep_batches_default_cli_into_one_spawn(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "improvement-queue.md").write_text("## Open\n- a\n")
    (tmp_path / "state" / "bug-backlog.md").write_text("## Open\n- b\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_cli = Path(__file__).resolve().parents[1] / "prune_resolved_queue_entries.py"
    (bin_dir / "prune-resolved-queue-entries.py").write_text(
        real_cli.read_text(encoding="utf-8"), encoding="utf-8"
    )

    calls: list[list[str]] = []
    orig_run = udg._run

    def _spy_run(cli_path, args, **kwargs):
        calls.append(args)
        return orig_run(cli_path, args, **kwargs)

    monkeypatch.setattr(udg, "_run", _spy_run)

    # No overrides["prune_cli"] — this exercises the DEFAULT-callee branch,
    # which is the one that must collapse to a single spawn.
    result = udg._gate_queue_prune_sweep(tmp_path, tmp_path, {})

    assert len(calls) == 1
    assert len(calls[0]) == 2
    assert result.verdict in (udg.GateVerdict.CLEAN, udg.GateVerdict.FINDING)


def test_queue_prune_sweep_override_cli_keeps_one_spawn_per_queue(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "improvement-queue.md").write_text("## Open\n- a\n")
    (tmp_path / "state" / "bug-backlog.md").write_text("## Open\n- b\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "single-arg-prune.py"
    fake_cli.write_text(
        "import sys\n"
        "assert len(sys.argv) == 2, sys.argv\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    result = udg._gate_queue_prune_sweep(tmp_path, tmp_path, {"prune_cli": str(fake_cli)})

    assert result.verdict == udg.GateVerdict.CLEAN


def test_queue_prune_sweep_default_cli_reports_which_queue_failed(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "improvement-queue.md").write_text("## Open\n- a\n")
    (tmp_path / "state" / "bug-backlog.md").write_text("## Open\n- b\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "prune-resolved-queue-entries.py"
    fake_cli.write_text(
        "import sys\n"
        "for p in sys.argv[1:]:\n"
        "    if p.endswith('bug-backlog.md'):\n"
        "        sys.stderr.write(f'ERROR: file not found: {p}\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    result = udg._gate_queue_prune_sweep(tmp_path, tmp_path, {})

    assert result.verdict == udg.GateVerdict.FINDING
    assert result.severity == udg.Severity.BLOCKING
    assert any("bug-backlog.md" in line for line in result.detail["lines"])


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


# ---------------------------------------------------------------------------
# (n) Windows extensionless-CLI exec resolution — `_windows_exec_argv`
# precedence (P1: CreateProcess does not honour a shebang; the fix mirrors
# the .cmd/.ps1/.py launcher-shim shape gen-launcher-shim.py already
# generates for every bin/ entrypoint). Monkeypatches `udg.sys.platform` so
# the branch is exercised deterministically regardless of the host OS this
# suite happens to run on.
# ---------------------------------------------------------------------------


def test_windows_exec_argv_cmd_sibling_wins_over_bare_extensionless_file(tmp_path):
    cli = tmp_path / "verify-something"
    _write_fake_cli(cli, 0)  # bare file with a python shebang — must lose to .cmd
    cmd_sibling = tmp_path / "verify-something.cmd"
    cmd_sibling.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    argv = udg._windows_exec_argv(cli, ["--flag"])
    assert argv == [str(cmd_sibling), "--flag"]


def test_windows_exec_argv_ps1_sibling_used_when_no_cmd_sibling(tmp_path):
    cli = tmp_path / "verify-something"
    ps1_sibling = tmp_path / "verify-something.ps1"
    ps1_sibling.write_text("exit 0\n", encoding="utf-8")
    argv = udg._windows_exec_argv(cli, ["x"])
    if argv is None:
        pytest.skip("no powershell/pwsh on PATH to resolve the .ps1 sibling")
    assert argv[-2:] == [str(ps1_sibling), "x"]


def test_windows_exec_argv_py_sibling_used_when_no_cmd_or_ps1(tmp_path):
    cli = tmp_path / "verify-something"
    py_sibling = tmp_path / "verify-something.py"
    py_sibling.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    argv = udg._windows_exec_argv(cli, [])
    assert argv == [sys.executable, str(py_sibling)]


def test_windows_exec_argv_python_shebang_falls_back_to_sys_executable(tmp_path):
    cli = tmp_path / "verify-something"
    _write_fake_cli(cli, 0)  # no .cmd/.ps1/.py sibling — only the shebang read remains
    argv = udg._windows_exec_argv(cli, [])
    assert argv == [sys.executable, str(cli)]


def test_windows_exec_argv_non_python_shebang_stays_unavailable(tmp_path):
    cli = tmp_path / "verify-something"
    cli.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    argv = udg._windows_exec_argv(cli, [])
    assert argv is None


def test_run_non_python_shebang_extensionless_cli_is_unavailable_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(udg.sys, "platform", "win32")
    cli = tmp_path / "verify-something"
    cli.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    proc = udg._run(cli, [])
    assert proc is None


def test_run_posix_argv_unchanged_by_the_windows_branch(tmp_path, monkeypatch):
    """POSIX behaviour must stay byte-identical: the extensionless CLI is
    still handed to subprocess.run() directly, with no launcher-shim
    resolution in between — regardless of which OS actually runs this test."""
    monkeypatch.setattr(udg.sys, "platform", "linux")
    cli = tmp_path / "verify-something"
    _write_fake_cli(cli, 0, stdout="posix-direct")
    captured: dict[str, list[str]] = {}
    orig_subprocess_run = udg.subprocess.run

    def _spy(argv, **kwargs):
        captured["argv"] = argv
        if sys.platform != "win32":
            return orig_subprocess_run(argv, **kwargs)
        # The real host is Windows and cannot direct-exec a shebang file —
        # fabricate a result so this asserts argv shape only, not execution.
        import subprocess as _subprocess

        return _subprocess.CompletedProcess(argv, 0, "posix-direct", "")

    monkeypatch.setattr(udg.subprocess, "run", _spy)
    udg._run(cli, ["--x"])
    assert captured["argv"] == [str(cli), "--x"]


# ---------------------------------------------------------------------------
# (j) The four bucket-2 doc-index / prune gates
#
# One UNAVAILABLE test per gate, each asserting explicitly that the verdict is
# not CLEAN. The 2026-09-02 boundary audit found "engine unreachable"
# indistinguishable from "found nothing" at nine of ten fallback sites in the
# ceremony this file serves; these four are where a tenth would be added, so
# the distinction is pinned per gate rather than once for the group.
# ---------------------------------------------------------------------------

_BUCKET2_GATES = (
    "docs-readme-index-drift",
    "directory-md-staleness",
    "plans-prune-candidates",
    "archive-memo-prune-candidates",
)


@pytest.mark.parametrize("gate_id", _BUCKET2_GATES)
def test_bucket2_gate_is_unavailable_not_clean_when_target_absent(gate_id, tmp_path):
    """An absent corpus is UNAVAILABLE, never CLEAN.

    tmp_path is an empty directory: every one of these gates' targets is
    missing. A gate that reported CLEAN here would be telling the ceremony
    "nothing to do" about a corpus it never looked at.
    """
    result = udg._GATES[gate_id](tmp_path, tmp_path, {})
    assert result.verdict is udg.GateVerdict.UNAVAILABLE
    assert result.verdict is not udg.GateVerdict.CLEAN
    assert result.severity is None
    assert "missing_path" in result.detail


@pytest.mark.parametrize("gate_id", _BUCKET2_GATES)
def test_bucket2_gate_is_registered(gate_id):
    assert gate_id in udg._GATES


# ---------------------------------------------------------------------------
# directory-md-staleness takes its index path from the repo, not from claude-klabauter.
#
# The gate hardcoded `coordinator_core/DIRECTORY.md`, so it read UNAVAILABLE
# forever on every consumer of this ceremony except claude-klabauter — including
# DoE-claude, whose index is `./DIRECTORY.md`. Reported by doe-claude-3f,
# state/bug-backlog/2026-09-02-directory-md-staleness-hardcodes-claude_klabauters-index-path.yaml.
# ---------------------------------------------------------------------------

_INDEX_BODY = "# DIRECTORY\n\nLast refreshed: 2026-09-02\n"


def _run_directory_md_gate(root, overrides=None):
    return udg._GATES["directory-md-staleness"](root, root, overrides or {})


def test_directory_md_gate_finds_a_root_level_index(tmp_path):
    """DoE-claude's shape: the index is `./DIRECTORY.md`."""
    (tmp_path / "DIRECTORY.md").write_text(_INDEX_BODY, encoding="utf-8")

    result = _run_directory_md_gate(tmp_path)

    assert result.verdict is not udg.GateVerdict.UNAVAILABLE
    assert result.detail["directory_md"] == "DIRECTORY.md"


def test_directory_md_gate_finds_a_package_level_index(tmp_path):
    """claude-klabauter's shape: the index is under a top-level package dir."""
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir()
    (pkg / "DIRECTORY.md").write_text(_INDEX_BODY, encoding="utf-8")

    result = _run_directory_md_gate(tmp_path)

    assert result.verdict is not udg.GateVerdict.UNAVAILABLE
    assert result.detail["directory_md"] == "coordinator_core/DIRECTORY.md"


def test_directory_md_gate_prefers_the_root_index_over_a_package_one(tmp_path):
    (tmp_path / "DIRECTORY.md").write_text(_INDEX_BODY, encoding="utf-8")
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir()
    (pkg / "DIRECTORY.md").write_text(_INDEX_BODY, encoding="utf-8")

    assert _run_directory_md_gate(tmp_path).detail["directory_md"] == "DIRECTORY.md"


def test_directory_md_gate_honours_an_explicit_override(tmp_path):
    (tmp_path / "DIRECTORY.md").write_text(_INDEX_BODY, encoding="utf-8")
    nested = tmp_path / "engine"
    nested.mkdir()
    (nested / "DIRECTORY.md").write_text(_INDEX_BODY, encoding="utf-8")

    result = _run_directory_md_gate(tmp_path, {"directory_md": "engine/DIRECTORY.md"})

    assert result.detail["directory_md"] == "engine/DIRECTORY.md"


def test_directory_md_discovery_does_not_recurse(tmp_path):
    """Two levels down is not discovery — it is a walk this gate must not do."""
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "DIRECTORY.md").write_text(_INDEX_BODY, encoding="utf-8")

    assert udg._discover_directory_md(tmp_path) is None


def test_bucket2_gates_are_informational_never_blocking(tmp_path):
    """None of the four may halt /update-docs.

    Doc-index drift is not a reason to stop the ceremony. This is the concrete
    guard against repeating Phase 11h2's unconditional halt, whose blocking
    severity was attached to the phase rather than to the finding.
    """
    docs = tmp_path / "docs"
    (docs / "plans").mkdir(parents=True)
    (docs / "README.md").write_text("# Index\n\n## Plans\n", encoding="utf-8")
    (tmp_path / "cross-repo" / "archive").mkdir(parents=True)
    (tmp_path / "coordinator_core").mkdir()
    (tmp_path / "coordinator_core" / "DIRECTORY.md").write_text("# map\n", encoding="utf-8")
    (docs / "plans" / "p.md").write_text("---\nstatus: implemented\n---\n", encoding="utf-8")

    results = [udg._GATES[g](tmp_path, tmp_path, {}) for g in _BUCKET2_GATES]
    for result in results:
        assert result.severity is not udg.Severity.BLOCKING

    rolled = udg.rollup(results)
    assert rolled["halt"] is False
    assert rolled["blocking"] == []


def test_bucket2_gate_ids_do_not_bypass_the_unknown_id_guard():
    """Adding four gates must not weaken the no-silent-skip contract."""
    with pytest.raises(ValueError):
        udg._updatedocs_gates({"gates": ["docs-readme-index-drift", "not-a-real-gate"]})


def test_memo_prune_gate_survives_an_actual_prune_candidate(tmp_path):
    """Regression: the gate raised AttributeError the moment a memo qualified.

    It iterated `c.path` over MemoPruneResult's `list[str]`, copied from the
    plan gate above it whose lists hold candidate objects. Eighty-seven tests
    and a correctness review missed it because no test ever drove a PRUNABLE
    memo through the gate — the live corpus yields zero, so every run took the
    empty path.
    """
    import os
    import time

    archive = tmp_path / "cross-repo" / "archive"
    archive.mkdir(parents=True)
    memo = archive / "old.md"
    memo.write_text("---\nstatus: actioned\n---\n", encoding="utf-8")
    old = time.time() - 200 * 86400
    os.utime(memo, (old, old))

    result = udg._GATES["archive-memo-prune-candidates"](tmp_path, tmp_path, {})

    assert result.verdict is udg.GateVerdict.FINDING
    assert result.detail["prunable"] == ["cross-repo/archive/old.md"]


def test_gates_resolve_corpora_under_the_worktree_not_the_git_dir(tmp_path):
    """A corpus must never be joined onto the injected common dir.

    Regression for the defect DoE-claude found on 2026-09-02: `updatedocs.gates`
    is keyed "common_dir", so the injected `repo_root` is `<worktree>/.git`.
    Every corpus join landed under `.git`, the walks counted zero, and
    `distill-threshold` reported CLEAN over a populated tree.

    This asserts on a corpus that EXISTS in the worktree. The per-gate
    UNAVAILABLE tests cannot catch this: they assert on an absent path, and the
    failure mode here is a path that resolves somewhere real and wrong.
    """
    worktree = tmp_path / "repo"
    (worktree / ".git").mkdir(parents=True)
    plans = worktree / "docs" / "plans"
    plans.mkdir(parents=True)
    for i in range(3):
        (plans / f"plan-{i}.md").write_text("# plan\n", encoding="utf-8")

    # Dispatch injects the COMMON DIR, exactly as the live engine does.
    out = udg._updatedocs_gates({"gates": ["distill-threshold"]}, repo_root=worktree / ".git")
    gate = out["gates"][0]

    assert gate["detail"]["total"] == 3, (
        f"gate counted {gate['detail']['total']} of 3 files — corpus resolved off the worktree"
    )
    assert gate["verdict"] != "clean" or gate["detail"]["total"] > 0


# ---------------------------------------------------------------------------
# (l) CLI resolution is platform-aware
#
# The deployed coordinator settings tree publishes each bin/ entrypoint as a
# native forwarder (`verify-skill-anchor-links.exe`) while this module names
# them the way the authoring repo writes them (extensionless, or `.py`). An
# exact-name resolver reported "CLI not found" for four gates whose binaries
# were present, which reads UNAVAILABLE — the verdict reserved for "could not
# look" — from a box where the gate could have run. These pin resolution
# against a platform-blind resolver, in both directions: Windows must find the
# PATHEXT form, POSIX must never invent one.
# ---------------------------------------------------------------------------


def test_resolve_cli_finds_the_pathext_executable_when_the_bare_name_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(udg.sys, "platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    forwarder = tmp_path / "verify-skill-anchor-links.exe"
    forwarder.write_bytes(b"MZ")
    assert udg._resolve_cli(tmp_path, "verify-skill-anchor-links") == forwarder


def test_resolve_cli_prefers_the_executable_over_a_co_located_extensionless_script(tmp_path, monkeypatch):
    """On Windows an extensionless file cannot be exec'd at all.

    A settings tree that carries both forms for one entrypoint must resolve to
    the forwarder — resolving to the script would spawn nothing and report
    UNAVAILABLE with the executable sitting beside it.
    """
    monkeypatch.setattr(udg.sys, "platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    (tmp_path / "sync-plugin-wiki").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    forwarder = tmp_path / "sync-plugin-wiki.exe"
    forwarder.write_bytes(b"MZ")
    assert udg._resolve_cli(tmp_path, "sync-plugin-wiki") == forwarder


def test_resolve_cli_maps_a_py_name_onto_the_forwarder_that_replaced_it(tmp_path, monkeypatch):
    monkeypatch.setattr(udg.sys, "platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    forwarder = tmp_path / "check-rag-state.exe"
    forwarder.write_bytes(b"MZ")
    assert udg._resolve_cli(tmp_path, "check-rag-state.py") == forwarder


def test_resolve_cli_never_invents_a_windows_extension_on_posix(tmp_path, monkeypatch):
    """`.exe` beside a POSIX CLI is not that CLI."""
    monkeypatch.setattr(udg.sys, "platform", "linux")
    (tmp_path / "sync-plugin-wiki.exe").write_bytes(b"MZ")
    assert udg._resolve_cli(tmp_path, "sync-plugin-wiki") == tmp_path / "sync-plugin-wiki"


def test_resolve_cli_keeps_the_name_as_written_when_it_exists_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(udg.sys, "platform", "linux")
    cli = tmp_path / "sync-plugin-wiki"
    cli.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert udg._resolve_cli(tmp_path, "sync-plugin-wiki") == cli


def test_resolve_cli_returns_the_canonical_path_when_nothing_resolves(tmp_path):
    """UNAVAILABLE must still name the path the gate looked for."""
    assert udg._resolve_cli(tmp_path, "sync-plugin-wiki") == tmp_path / "sync-plugin-wiki"


def test_windows_exec_argv_execs_a_pathext_binary_directly(tmp_path, monkeypatch):
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    forwarder = tmp_path / "reap-stale-subagent-sidecars.exe"
    forwarder.write_bytes(b"MZ")
    assert udg._windows_exec_argv(forwarder, ["--repo-root", "x"]) == [str(forwarder), "--repo-root", "x"]


def test_reap_sidecars_gate_runs_against_the_forwarder_instead_of_reporting_cli_not_found(tmp_path, monkeypatch):
    """End-to-end for the four gates the platform-blind resolver silenced.

    `_run` is stubbed because a fabricated `.exe` cannot be spawned; what this
    asserts is that the gate reached a spawn at all, with the forwarder's path,
    rather than short-circuiting to UNAVAILABLE on an exact-name miss.
    """
    monkeypatch.setattr(udg.sys, "platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    bin_dir = tmp_path / "settings" / "bin"
    bin_dir.mkdir(parents=True)
    forwarder = bin_dir / "reap-stale-subagent-sidecars.exe"
    forwarder.write_bytes(b"MZ")

    spawned: dict[str, Path] = {}

    def _fake_run(cli_path, args, **kwargs):
        spawned["cli"] = cli_path
        return subprocess.CompletedProcess([str(cli_path), *args], 0, "reaped 0 sidecars", "")

    monkeypatch.setattr(udg, "_run", _fake_run)
    result = udg._gate_reap_stale_sidecars(tmp_path, tmp_path / "settings", {})

    assert spawned["cli"] == forwarder
    assert result.verdict is not udg.GateVerdict.UNAVAILABLE
