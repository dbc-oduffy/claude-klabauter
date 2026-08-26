"""coordinator_core.merge_assemble.tests.test_warm_ceremony_brightline --
C7 (docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md):
re-measures the converged merge ceremony against DR-344's 500ms brightline
on the WARM-SERVED path C6 registered (`merge_assemble.brief` /
`merge_assemble.apply` in `coordinator_core.merge_assemble.ops`), and
proves which path a real ceremony dispatch actually takes.

AC11 (brightline, on the correct path). C3 measured the converged handlers'
COLD-forwarder delta pre-C6 — necessary evidence, not this AC's claim:
measuring before C6 registered the ops would have measured the still-cold
forwarder path, a guaranteed miss unrelated to this plan's work (this
module's own dispatch brief). Every number below is labelled with the path
it was taken on; an unlabelled number is not evidence for this AC.

AC12 ("REACHED"). `TestNoInTreeCallerDispatchesTheRegisteredOp` is the
structural half: post-C6, `coordinator/bin/merge-assemble.py` is STILL a
thin shim over `entry_point_shim.py :: run_target`, which resolves
`merge-assemble` via `_ENGINE_ENTRIES` (`_simple_entry("merge-assemble",
"coordinator_core.merge_assemble")`) and runs the entry point IN-PROCESS,
in a COLD interpreter, per invocation — never through the warm engine's
UDS transport (`coordinator_core.merge_assemble.ops` module docstring).
Grepped at build time: no in-tree module outside `ops.py` itself (its
registration side-effect), `authz/classification.py` / `op_scopes.py` /
`ops/_registry_map.py` (registration bookkeeping, not call sites), and
this test module dispatches `"merge_assemble.brief"`/`"merge_assemble.
apply"` as an op id to any warm-dispatch mechanism. C6 registered two ops
nobody dispatches; the 137.5ms/5-process cold-forwarder leg of the C3
baseline is UNTOUCHED, exactly as this plan's own C6 chunk predicted.

METHODOLOGY. `apply()` MUTATES (cuts tags, runs `gh`, writes grant
records) so it cannot be safely re-run `k` times against a shared repo the
way `brief()` (COMPUTE_ONLY) can — this module never runs it against this
tree's own working copy. Instead:

  - `test_warm_op_path_brief_process_time` batches the WARM OP PATH's
    registered `merge_assemble.brief` handler (real git calls, read-only)
    against a disposable throwaway git repo built fresh per test.
  - `test_warm_op_path_ceremony_process_time` batches "brief plus apply"
    through the SAME warm op path, against the SAME disposable repo, with
    every `_CLI_DISPATCH` handler monkeypatched to a uniform stub. This
    isolates the OP PATH's own reach and directive-loop overhead (real git
    reads for brief's branch/tag state, the async dispatch adapters in
    `ops.py`, `apply_base.execute_directives`'s ordering/gating) — each
    individual directive handler's OWN execution cost (real tag cuts, PR
    creation, portability sweeps) is a separate, per-handler question
    C2/C3 and each handler's own module already own; duplicating it here
    would conflate two different things under one number. Verified by
    hand against this fixture before landing this file: with no judgment
    decisions supplied, `apply()` halts at `ship_verdict`/
    `version_bump_final` (exit_code 1, "directive_failed") without landing
    `d2`/`d4` — i.e. the stub table is exercised but the repo is never
    mutated, so re-running it `k` times measures the same job every time.
  - `test_cold_forwarder_brief_process_time` re-measures the COLD path
    (`coordinator/bin/merge-assemble.py brief`, C3's own instrument) on
    the SAME fixture, so the two paths are compared like-for-like rather
    than against C3's original (differently-provisioned) baseline.

NEGATIVE-SPEC:
    - Do NOT run `apply()` (real dispatch table, no stub) against this
      repo's own working tree — a prior hand-verification of this file's
      approach did exactly that from the wrong cwd and minted a real
      `tier-u-grant` write against this tree before the throwaway-repo
      fixture was added; every test in this module passes an explicit
      `repo_root` AND spawns with `cwd` pointed at the throwaway fixture,
      never this tree.
    - Do NOT roll a new timer — `benchhmarks/process_time.py ::
      batched_process_time_ms` is the one instrument, per that module's
      own docstring and every prior chunk's usage of it.
    - Do NOT re-assert C2/C3's static AST spawn-freedom guard here
      (`test_no_interpreter_spawn.py` already owns it) — this module's
      job is process TIME on the real dispatch path, not source shape.

Spec backlink: docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md, chunk C7
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 6
"""Matches the sizing/C3/C4 methodology (k>=6) already established for this
instrument elsewhere in this repo — not re-derived here."""

BRIGHTLINE_MS = 500.0
"""DR-344's own brightline (CLAUDE.md § The brightline) — the number every
assertion in this module is read against, never `SUSPENSION_BAR_MS`."""

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FORWARDER = _REPO_ROOT / "coordinator" / "bin" / "merge-assemble.py"

_OP_PATH_SWEEP_EXEMPT_FILES = (
    Path(__file__).resolve(),
    (_REPO_ROOT / "coordinator_core" / "merge_assemble" / "ops.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "merge_assemble" / "apply.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "merge_assemble" / "__init__.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "authz" / "classification.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "op_scopes.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "ops" / "_registry_map.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "ops" / "__init__.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "directive_cli_arity.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "session" / "grant_directive.py").resolve(),
    (_REPO_ROOT / "coordinator_core" / "workweek_complete" / "brief.py").resolve(),
)
"""Files that legitimately NAME the two op ids without DISPATCHING them,
verified individually at pin time (each occurrence read at its cited line
before exemption — no blanket "docstrings are fine" rule):

    - `ops.py` — defines the two handlers (`register_op` decorator).
    - `apply.py`, `merge_assemble/__init__.py` — a bare PYTHON MODULE PATH
      (`coordinator_core.merge_assemble.apply`), not the op-id STRING
      `"merge_assemble.apply"` a dispatch call would pass — the substring
      match is incidental to Python's own dotted-path spelling, confirmed
      by reading each cited line (module docstrings, an import statement,
      inline comments), none inside a call expression.
    - `authz/classification.py`, `op_scopes.py`, `ops/_registry_map.py`,
      `ops/__init__.py` — registration/classification bookkeeping (op-class
      table, scope table, module-path map, this package's own doc comment
      about C6) — none calls a warm-dispatch mechanism with either op id.
    - `directive_cli_arity.py`, `session/grant_directive.py`,
      `workweek_complete/brief.py` — prose citing the op/module by name in
      a docstring or comment, not a call site.

This module itself names both ids to assert their absence elsewhere, so it
is exempt from its own sweep too — matching the idiom
`test_assembler_brightline_conformance.py` already established for this
exact shape of check."""

_OP_IDS = ("merge_assemble.brief", "merge_assemble.apply")

_WARM_OP_PROBE_SOURCE = textwrap.dedent(
    '''\
    """Standalone probe (written to disk by test_warm_ceremony_brightline.py,
    never committed): calls the WARM OP PATH's own two registered handlers
    in-process, in a single spawned interpreter, so
    `batched_process_time_ms` can measure that interpreter's process time.

    argv[1] is the throwaway repo root; argv[2] ("stub" | "real") selects
    whether `_CLI_DISPATCH` is monkeypatched to a uniform no-op stub before
    calling `merge_assemble.apply` — "stub" isolates the op path's own
    reach/dispatch-loop overhead (this module's ceremony test); "real"
    never actually lands here (apply mutates and this probe is reused
    read-only-only by design), kept as an explicit branch so a future
    caller cannot silently drop the stub without noticing the branch.
    """
    import asyncio
    import sys
    from pathlib import Path

    from coordinator_core.merge_assemble import apply as _ma_apply
    from coordinator_core.merge_assemble import ops as _ma_ops


    def _stub_handler(args, repo_root):
        return {"ok": True, "args": args, "repo_root": str(repo_root)}


    def main() -> int:
        repo_root = Path(sys.argv[1])
        mode = sys.argv[2] if len(sys.argv) > 2 else "brief-only"

        brief_result = asyncio.run(_ma_ops._merge_assemble_brief({}, repo_root))
        if brief_result["exit_code"] != 0:
            print(f"brief failed: {brief_result}", file=sys.stderr)
            return 1
        if mode == "brief-only":
            return 0

        if mode == "stub":
            for cli_name in list(_ma_apply._CLI_DISPATCH):
                _ma_apply._CLI_DISPATCH[cli_name] = _stub_handler
        elif mode != "real":
            print(f"unrecognized mode {mode!r}", file=sys.stderr)
            return 2

        apply_result = asyncio.run(
            _ma_ops._merge_assemble_apply(
                {"session_id": "c7-warm-op-probe", "force": True}, repo_root
            )
        )
        # exit_code 1 ("directive_failed"/halted-at-judgment) is EXPECTED
        # and fine here — no decisions are supplied, so ship_verdict/
        # version_bump_final stay unresolved and d2/d4 never land (verified
        # by hand: the repo's own tag set is unchanged after this call).
        # Only a transport-level failure (exit_code not in {0, 1}) is an
        # error this probe should surface.
        if apply_result["exit_code"] not in (0, 1):
            print(f"apply transport-failed: {apply_result}", file=sys.stderr)
            return 1
        return 0


    if __name__ == "__main__":
        sys.exit(main())
    '''
)


def _require_windows_or_darwin() -> None:
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip(
            "batched_process_time_ms's spawn-count primitive is Windows/Darwin-only "
            "(coordinator_core.benchmarks.process_time module docstring)"
        )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_throwaway_repo(root: Path) -> None:
    """A minimal, disposable git repo with one commit and one tag — enough
    for `brief()`'s real (non-mocked) `rev-list`/`tag` calls to succeed,
    entirely isolated from this tree's own working copy."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "c7-brightline@example.invalid")
    _git(root, "config", "user.name", "c7-brightline")
    (root / "README.md").write_text("throwaway C7 brightline fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "tag", "v0.0.1")


@pytest.fixture()
def _throwaway_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "c7-throwaway-repo"
    repo.mkdir()
    _init_throwaway_repo(repo)
    return repo


@pytest.fixture()
def _warm_op_probe(tmp_path: Path) -> Path:
    probe = tmp_path / "warm_op_probe.py"
    probe.write_text(_WARM_OP_PROBE_SOURCE, encoding="utf-8")
    return probe


def _spawn_env() -> dict:
    """The probe imports `coordinator_core` — it is spawned with `cwd` set
    to the throwaway repo (never this tree), so `PYTHONPATH` is how it
    finds this repo's own package rather than relying on an accidental
    cwd-based `sys.path` entry."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_REPO_ROOT) + (os.pathsep + existing if existing else "")
    return env


class TestNoInTreeCallerDispatchesTheRegisteredOp:
    """AC12's structural half: post-C6, nothing in this tree dispatches
    `merge_assemble.brief`/`merge_assemble.apply` as a warm op — the
    forwarder (`coordinator/bin/merge-assemble.py`, still a thin shim over
    `entry_point_shim.run_target`) is the only path a real ceremony
    invocation takes."""

    def test_the_sweep_still_catches_a_production_caller(self) -> None:
        """The exemption list is worthless if it swallows a real offender.
        A planted reference in an ordinary module must still be caught."""
        content = 'client.dispatch("merge_assemble.apply", params={})\n'
        offenders = [op_id for op_id in _OP_IDS if op_id in content]
        assert offenders == ["merge_assemble.apply"]

    def test_no_in_tree_caller_dispatches_either_op_id(self) -> None:
        offenders: list[tuple[str, str]] = []
        for root, dirs, files in os.walk(_REPO_ROOT / "coordinator_core"):
            if "__pycache__" in root:
                continue
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = Path(root) / fname
                if path.resolve() in _OP_PATH_SWEEP_EXEMPT_FILES:
                    continue
                if fname.startswith("test_") or fname.endswith("_test.py"):
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for op_id in _OP_IDS:
                    if op_id in content:
                        offenders.append((str(path.relative_to(_REPO_ROOT)), op_id))
        assert offenders == [], (
            "an in-tree caller now references a merge_assemble op id outside "
            "ops.py's own registration/classification bookkeeping — if this "
            "is a new dispatch call site, the registered op has been "
            "REACHED and this module's AC12 finding (forwarder-only, cold) "
            f"needs re-measuring: {offenders!r}"
        )

    def test_forwarder_still_resolves_in_process_via_entry_point_shim(self) -> None:
        """`coordinator/bin/merge-assemble.py` is still the routing half:
        a thin shim importing `entry_point_shim.run_target`, never a warm
        client. This is the file every real `/merge-to-main` invocation
        actually runs."""
        content = _FORWARDER.read_text(encoding="utf-8")
        assert "run_target" in content
        assert "entry_point_shim" in content
        assert "try_warm_dispatch" not in content

        shim_content = (
            _REPO_ROOT / "coordinator" / "bin" / "lib" / "entry_point_shim.py"
        ).read_text(encoding="utf-8")
        assert '"merge-assemble"' in shim_content
        assert "_simple_entry" in shim_content


class TestWarmCeremonyBrightline:
    """AC11: the converged ceremony's process-time cost on the WARM OP
    PATH (C6's registered handlers, called in-process), against DR-344's
    500ms brightline. Every assertion below is labelled with which path
    produced its number."""

    def test_warm_op_path_brief_process_time(
        self, _throwaway_repo: Path, _warm_op_probe: Path
    ) -> None:
        """WARM OP PATH, `merge_assemble.brief` only — COMPUTE_ONLY, real
        (non-stubbed) git reads against the throwaway fixture, safely
        batched k times."""
        _require_windows_or_darwin()

        result = batched_process_time_ms(
            [sys.executable, str(_warm_op_probe), str(_throwaway_repo), "brief-only"],
            k=K_INVOCATIONS,
            cwd=str(_throwaway_repo),
            env=_spawn_env(),
        )
        assert result["rc"] == 0, result
        assert result["process_time_ms"] <= BRIGHTLINE_MS, (
            f"WARM OP PATH brief() missed the 500ms brightline: {result!r}"
        )

    def test_warm_op_path_ceremony_process_time(
        self, _throwaway_repo: Path, _warm_op_probe: Path
    ) -> None:
        """WARM OP PATH, brief() + apply() end to end, `_CLI_DISPATCH`
        stubbed (module docstring § METHODOLOGY) — the op path's own
        reach and directive-loop overhead, real git reads included, no
        per-handler execution cost folded in."""
        _require_windows_or_darwin()

        result = batched_process_time_ms(
            [sys.executable, str(_warm_op_probe), str(_throwaway_repo), "stub"],
            k=K_INVOCATIONS,
            cwd=str(_throwaway_repo),
            env=_spawn_env(),
        )
        assert result["rc"] == 0, result
        # Reported findings from this chunk's own verification run (this
        # box, 2026-08-26, k=6, Windows): 182.292ms process time / 9.0
        # procs per call (1 interpreter + 8 real `git` subprocesses — two
        # brief() computations, one standalone and one inside apply()'s
        # own re-brief, each issuing ~4 real git calls). Both figures are
        # reported, not just gated, so a reader does not have to re-derive
        # the spawn/interpreter split from a bare pass/fail.
        assert result["process_time_ms"] <= BRIGHTLINE_MS, (
            "WARM OP PATH brief()+apply() (dispatch table stubbed) missed "
            f"the 500ms brightline: {result!r} — dominant cost is real git "
            "subprocesses (brief() runs twice: once standalone, once "
            "inside apply()'s own re-brief), not the op-path adapter itself"
        )

    @pytest.mark.real_home
    def test_cold_forwarder_brief_process_time(self, _throwaway_repo: Path) -> None:
        """COLD FORWARDER PATH (`coordinator/bin/merge-assemble.py brief`,
        C3's own instrument), re-measured on this SAME throwaway fixture
        so the warm/cold comparison above is like-for-like rather than
        against C3's differently-provisioned baseline. Reported findings
        from this chunk's own verification run (this box, 2026-08-26,
        k=6, Windows): 114.583ms process time / 5.0 procs per call —
        consistent with C3's own pre-C6 baseline (137.5ms/5 procs), i.e.
        this leg is genuinely untouched by C6/C7 (AC12's own finding: no
        caller was repointed at the registered op).

        `@pytest.mark.real_home`: the forwarder resolves `CLAUDE_KLABAUTER_ROOT`
        through the machine-local registry — under this suite's own
        HOME-quarantine autouse fixture (`coordinator_core/conftest.py ::
        _quarantine_real_home`) that resolution fails outright
        ("CLAUDE_KLABAUTER_ROOT resolution failed... set it via 'machine-local set
        repos.claude_klabauter'"), which is a fixture-environment artifact,
        not a property of the path itself. This is exactly the documented
        opt-out case (a read-only oracle against the live tree's own
        registry), verified against this file's own throwaway repo before
        landing this test."""
        _require_windows_or_darwin()

        result = batched_process_time_ms(
            [sys.executable, str(_FORWARDER), "brief"],
            k=K_INVOCATIONS,
            cwd=str(_throwaway_repo),
        )
        assert result["rc"] == 0, result
        assert result["procs_per_call"] >= 2.0, (
            "the cold forwarder path is expected to still spawn multiple "
            f"processes (interpreter + real git calls) — got {result!r}, "
            "which would mean this leg silently changed shape"
        )
