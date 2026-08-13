"""test_update_docs_probes — pytest coverage for update-docs-probes.py's 4 subcommands.

Spec backlink: coordinator/commands/update-docs.md (coordinator-claude) — Pre-flight probe,
  Phase 9b, Phase 11i, Phase 13 steps 1-2.

Coverage:
  fresh-scaffold-probe: cwd-guard fall-through, all-3-axes-fire no-op, single-axis
    miss (falls through to pipeline).
  repomap-gate: fresh-skip, absent-generate (no fallback note), stale-generate
    (fallback note), generator-script-missing (non-fatal skip).
  queue-prune-sweep: missing queue file skipped, successful prune reports delta,
    failing prune aggregates to non-zero exit.
  distill-threshold: under-threshold no-fire, count>=50 fire, no-log+count>=20 fire,
    stale-log(>14d) fire.

Retired 2026-07-29: snippet-sync-sweep coverage removed along with the
subcommand — see update-docs-probes.py's module docstring for the retirement
rationale (dead code, verifiers retired fleet-wide, claude-klabauter's last
unsanctioned bash dependency).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import updatedocs_gates as _gates_mod

# Declared, not excused: `test_cli_subcommand_smoke` and
# `test_retired_snippet_sync_sweep_verb_is_still_accepted` spawn a real
# `sys.executable` child running update-docs-probes.py because the property
# under test is the CLI's real argparse-wiring/exit-code contract at the
# process boundary (cwd-guard fall-through, the retired-verb no-op shim's
# stderr message) -- no in-process call observes that. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "update_docs_probes",
        _BIN_DIR / "update-docs-probes.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _ns(**kwargs):
    class _NS:
        pass

    ns = _NS()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# fresh-scaffold-probe
# ---------------------------------------------------------------------------


def test_fresh_scaffold_cwd_guard_falls_through(tmp_path):
    # No CLAUDE.md, no .git/HEAD — probe is skipped entirely, falls through.
    args = _ns(repo_root=str(tmp_path))
    assert _mod._cmd_fresh_scaffold_probe(args) == 1


def test_fresh_scaffold_all_axes_fire(tmp_path, capsys):
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    args = _ns(repo_root=str(tmp_path))
    rc = _mod._cmd_fresh_scaffold_probe(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "freshly-scaffolded" in out


def test_fresh_scaffold_one_axis_miss_falls_through(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    (tmp_path / "DIRECTORY.md").write_text("x", encoding="utf-8")  # axis1 no longer fires
    args = _ns(repo_root=str(tmp_path))
    assert _mod._cmd_fresh_scaffold_probe(args) == 1


def test_fresh_scaffold_completed_dir_with_content_misses_axis2(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    completed = tmp_path / "archive" / "completed"
    completed.mkdir(parents=True)
    (completed / "done.md").write_text("x", encoding="utf-8")
    args = _ns(repo_root=str(tmp_path))
    assert _mod._cmd_fresh_scaffold_probe(args) == 1


# ---------------------------------------------------------------------------
# repomap-gate
# ---------------------------------------------------------------------------


def test_repomap_gate_fresh_skips(tmp_path, capsys):
    args = _ns(
        repo_root=str(tmp_path),
        rag_state="fresh",
        check_rag_state_cli=None,
        generate_repomap_cli=None,
    )
    rc = _mod._cmd_repomap_gate(args)
    assert rc == 0
    assert "skipped (RAG present + fresh)" in capsys.readouterr().out


def test_repomap_gate_absent_generates_no_fallback_note(tmp_path, capsys):
    generator = tmp_path / "fake-generate-repomap.py"
    generator.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    args = _ns(
        repo_root=str(tmp_path),
        rag_state="absent",
        check_rag_state_cli=None,
        generate_repomap_cli=str(generator),
    )
    rc = _mod._cmd_repomap_gate(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "RAG-fallback" not in out


def test_repomap_gate_stale_generates_with_fallback_note(tmp_path, capsys):
    generator = tmp_path / "fake-generate-repomap.py"
    generator.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    args = _ns(
        repo_root=str(tmp_path),
        rag_state="stale",
        check_rag_state_cli=None,
        generate_repomap_cli=str(generator),
    )
    rc = _mod._cmd_repomap_gate(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "repomap generated (rag_state=stale, fallback)" in out


def test_repomap_gate_missing_generator_is_non_fatal(tmp_path, capsys):
    args = _ns(
        repo_root=str(tmp_path),
        rag_state="unknown",
        check_rag_state_cli=None,
        generate_repomap_cli=str(tmp_path / "does-not-exist.py"),
    )
    rc = _mod._cmd_repomap_gate(args)
    assert rc == 0
    assert "unresolvable" in capsys.readouterr().out


def test_repomap_gate_generator_failure_propagates(tmp_path):
    generator = tmp_path / "fake-generate-repomap.py"
    generator.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    args = _ns(
        repo_root=str(tmp_path),
        rag_state="absent",
        check_rag_state_cli=None,
        generate_repomap_cli=str(generator),
    )
    assert _mod._cmd_repomap_gate(args) == 1


# ---------------------------------------------------------------------------
# queue-prune-sweep
# ---------------------------------------------------------------------------


def _fake_prune_cli(tmp_path: Path, exit_code: int = 0, strip_lines: int = 1) -> Path:
    cli = tmp_path / "fake-prune.py"
    cli.write_text(
        "import sys\n"
        f"exit_code = {exit_code}\n"
        f"strip = {strip_lines}\n"
        "if exit_code == 0:\n"
        "    p = sys.argv[1]\n"
        "    with open(p) as f:\n"
        "        lines = f.readlines()\n"
        "    with open(p, 'w') as f:\n"
        "        f.writelines(lines[strip:] if strip <= len(lines) else [])\n"
        "sys.exit(exit_code)\n",
        encoding="utf-8",
    )
    return cli


def test_queue_prune_sweep_missing_file_skipped(tmp_path):
    args = _ns(repo_root=str(tmp_path), prune_cli=None, queue=None)
    assert _mod._cmd_queue_prune_sweep(args) == 0


def test_queue_prune_sweep_reports_delta(tmp_path, capsys):
    queue = tmp_path / "state" / "bug-backlog.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("line1\nline2\nline3\n", encoding="utf-8")
    cli = _fake_prune_cli(tmp_path, exit_code=0, strip_lines=1)
    args = _ns(repo_root=str(tmp_path), prune_cli=str(cli), queue=["state/bug-backlog.md"])
    rc = _mod._cmd_queue_prune_sweep(args)
    assert rc == 0
    assert "pruned 1 lines from state/bug-backlog.md" in capsys.readouterr().out


def test_queue_prune_sweep_failure_aggregates(tmp_path):
    queue = tmp_path / "state" / "bug-backlog.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("line1\n", encoding="utf-8")
    cli = _fake_prune_cli(tmp_path, exit_code=1)
    args = _ns(repo_root=str(tmp_path), prune_cli=str(cli), queue=["state/bug-backlog.md"])
    assert _mod._cmd_queue_prune_sweep(args) == 1


def test_queue_prune_sweep_neither_leg_present_is_loud(tmp_path, capsys):
    # A migrated-away-from AND never-YAML-adopted repo (or a plain wrong
    # --repo-root) -- neither the legacy markdown paths nor the YAML family
    # directories exist. This must be a loud, distinguishable line, never a
    # bare exit-0 silent no-op (the exact defect this repoint closes).
    args = _ns(repo_root=str(tmp_path), prune_cli=None, queue=None)
    rc = _mod._cmd_queue_prune_sweep(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no legacy markdown queues and no YAML queue families found under" in out
    assert "verify repo_root" in out


def test_queue_prune_sweep_yaml_leg_invokes_wrapper_when_family_present(tmp_path, capsys):
    # A migrated (YAML-only) repo: state/bug-backlog/ exists -> the YAML leg
    # must dispatch prune-closed-bugs.py rather than silently reporting a
    # vacuous legacy-only success.
    bug_dir = tmp_path / "state" / "bug-backlog"
    bug_dir.mkdir(parents=True)
    (bug_dir / "2026-01-01-x.yaml").write_text("status: closed\n", encoding="utf-8")

    fake_cli = _BIN_DIR / "prune-closed-bugs.py"
    assert fake_cli.is_file(), "prune-closed-bugs.py must exist for this to be a real wiring check"

    calls = []

    class _FakeSubprocess:
        # Rebinding _gates_mod.subprocess to this stand-in (rather than
        # mutating the real, process-shared `subprocess` module's `.run`
        # attribute) keeps the fake scoped to this test's own module
        # reference. The actual spawn now lives in
        # coordinator_core.ops.updatedocs_gates._run (2026-08-06
        # updatedocs-gates-structured-verdicts port), not in
        # update-docs-probes.py itself -- _mod carries no `subprocess`
        # attribute of its own to rebind.
        @staticmethod
        def run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.run([sys.executable, "-c", "print('fake prune-closed-bugs.py invoked')"], **kwargs)

    orig_subprocess = _gates_mod.subprocess
    _gates_mod.subprocess = _FakeSubprocess
    try:
        args = _ns(repo_root=str(tmp_path), prune_cli=None, queue=None)
        rc = _mod._cmd_queue_prune_sweep(args)
    finally:
        _gates_mod.subprocess = orig_subprocess

    assert rc == 0
    invoked = [c for c in calls if str(fake_cli) in c]
    assert invoked, f"prune-closed-bugs.py was never dispatched; calls={calls!r}"
    assert "--repo-root" in invoked[0] and str(tmp_path) in invoked[0]


def test_queue_prune_sweep_yaml_leg_missing_wrapper_cli_fails_loud(tmp_path):
    # A YAML family directory is present but its bin/ ceremony wrapper is
    # missing -- must aggregate to a non-zero exit, never a silent skip.
    improvement_dir = tmp_path / "state" / "improvement-queue"
    improvement_dir.mkdir(parents=True)
    (improvement_dir / "2026-01-01-x.yaml").write_text("status: closed\n", encoding="utf-8")

    real_bin_dir = _mod._BIN_DIR
    fake_bin_dir = tmp_path / "fake-bin-dir-no-wrappers"
    fake_bin_dir.mkdir()

    _mod._BIN_DIR = fake_bin_dir
    try:
        args = _ns(repo_root=str(tmp_path), prune_cli=None, queue=None)
        rc = _mod._cmd_queue_prune_sweep(args)
    finally:
        _mod._BIN_DIR = real_bin_dir

    assert rc == 1


# ---------------------------------------------------------------------------
# distill-threshold
# ---------------------------------------------------------------------------


def _touch_md_files(dir_path: Path, n: int) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (dir_path / f"f{i}.md").write_text("x", encoding="utf-8")


def test_distill_threshold_under_threshold_no_fire(tmp_path, capsys):
    _touch_md_files(tmp_path / "docs" / "plans", 3)
    args = _ns(repo_root=str(tmp_path), log_path=None)
    rc = _mod._cmd_distill_threshold(args)
    assert rc == 0
    assert "harvest threshold not met" in capsys.readouterr().out


def test_distill_threshold_count_fires(tmp_path, capsys):
    _touch_md_files(tmp_path / "docs" / "plans", 50)
    args = _ns(repo_root=str(tmp_path), log_path=None)
    rc = _mod._cmd_distill_threshold(args)
    assert rc == 1
    assert "harvest threshold met — total count 50 >= 50" in capsys.readouterr().out


def test_distill_threshold_no_log_count_20_fires(tmp_path):
    _touch_md_files(tmp_path / "docs" / "plans", 20)
    args = _ns(repo_root=str(tmp_path), log_path=None)
    assert _mod._cmd_distill_threshold(args) == 1


def test_distill_threshold_stale_log_fires(tmp_path):
    _touch_md_files(tmp_path / "docs" / "plans", 1)
    log = tmp_path / "state" / "distillation-log.md"
    log.parent.mkdir(parents=True)
    log.write_text("# Columns: run | path | disposition | fate\n\n## Run 2020-01-01 — old run\n", encoding="utf-8")
    args = _ns(repo_root=str(tmp_path), log_path=None)
    assert _mod._cmd_distill_threshold(args) == 1


def test_distill_threshold_recent_log_no_fire(tmp_path):
    from datetime import datetime, timezone

    _touch_md_files(tmp_path / "docs" / "plans", 1)
    log = tmp_path / "state" / "distillation-log.md"
    log.parent.mkdir(parents=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.write_text(f"# Columns: run | path | disposition | fate\n\n## Run {today} — recent\n", encoding="utf-8")
    args = _ns(repo_root=str(tmp_path), log_path=None)
    assert _mod._cmd_distill_threshold(args) == 0


def test_distill_threshold_legacy_pipe_table_log_is_not_never(tmp_path, capsys):
    """A legacy pipe-delimited log (no `## Run YYYY-MM-DD` header, just bare
    `| date | action | ... |` rows) must not be read as "no distillation date
    found" — 2026-08-06 example-market-data-repo-em Defect 3: a real, dated,
    47-row legacy log reported "never" because the reader only recognized the
    canonical header shape. Regression for `_LEGACY_ROW_DATE_RE`."""
    _touch_md_files(tmp_path / "docs" / "plans", 1)
    log = tmp_path / "state" / "distillation-log.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "| date | action | path | last_sha | belongs_to_spec | reason |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| 2020-01-01 | harvest | archive/specs/foo.md | abc123 | | some reason |\n"
        "| 2020-01-02 | delete | archive/specs/bar.md | def456 | | some reason |\n",
        encoding="utf-8",
    )
    args = _ns(repo_root=str(tmp_path), log_path=None)
    rc = _mod._cmd_distill_threshold(args)
    out = capsys.readouterr().out
    assert "never" not in out
    assert rc == 1  # last legacy date (2020-01-02) is > 14 days ago -> fires


# ---------------------------------------------------------------------------
# CLI smoke test — argparse wiring end-to-end
# ---------------------------------------------------------------------------


def test_cli_subcommand_smoke(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(_BIN_DIR / "update-docs-probes.py"), "fresh-scaffold-probe", "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )  # popup-intentional-last-resort — test-only subprocess, no headless-parent focus-steal concern
    assert proc.returncode == 1  # cwd guard falls through (no CLAUDE.md/.git)


# ---------------------------------------------------------------------------
# snippet-sync-sweep retirement shim
# ---------------------------------------------------------------------------


def test_retired_snippet_sync_sweep_verb_is_still_accepted(tmp_path):
    """The retired verb must keep exiting 0, not argparse's exit-2 "invalid
    choice".

    The caller lives in another repo (coordinator-claude `coordinator/commands/
    update-docs.md` Phase 11b), so dropping the verb outright would break
    `/update-docs` for every user until coordinator-claude lands a matching edit. This test
    is the contract that keeps the two repos independently deployable while
    that edit is outstanding — delete it in the same change that deletes the
    shim, once Phase 11b no longer names the verb.
    """
    proc = subprocess.run(
        [sys.executable, str(_BIN_DIR / "update-docs-probes.py"), "snippet-sync-sweep"],
        capture_output=True,
        text=True,
        check=False,
    )  # popup-intentional-last-resort — test-only subprocess, no headless-parent focus-steal concern
    assert proc.returncode == 0, (
        "the retired verb must no-op, not exit 2 — a coordinator-claude-side Phase 11b run "
        f"would break.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "retired" in proc.stderr.lower(), (
        "the no-op must say so on stderr; a silent pass reads as a probe that "
        f"ran and found nothing.\nstderr: {proc.stderr}"
    )


def test_retired_snippet_sync_sweep_spawns_no_shell():
    """No `bash`/`sh` spawn survives anywhere in this module.

    The sweep's Windows-side interpreter resolution was claude-klabauter's last
    unsanctioned bash dependency (CLAUDE.md § Runtime conventions enumerates
    the sanctioned sites; this was not among them). Pinned as a test because
    the retirement shim keeps the VERB alive, and a future reader restoring a
    body behind that verb is the plausible way the dependency comes back.
    """
    source = (_BIN_DIR / "update-docs-probes.py").read_text(encoding="utf-8")
    code_lines = [
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ]
    for needle in ('"bash"', "'bash'", 'which("sh")', "which('sh')", '"sh"'):
        offenders = [line for line in code_lines if needle in line]
        assert not offenders, f"shell spawn reintroduced ({needle}): {offenders}"
