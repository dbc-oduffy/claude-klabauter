"""coordinator_core.benchmarks.percolate_round_ab -- round-level A/B harness
for a full percolate round, per docs/plans/2026-08-23-rebuild-the-percolate-
round-as-six-steps.md, chunk C1.

Purpose: AC1 and AC2/AC3 of that plan need a repeatable, on-disk figure for
"how much does a full 9-row percolate round cost" that gates on the two axes
DR-344 actually gates on -- PROCESS TIME (whole process tree, including every
spawned child) and SPAWN COUNT -- never wall clock, which on this box (50-70
concurrent LLM sessions is the design condition) measures peer load and
nothing about this round's own cost.

Two measurements, kept structurally separate because they answer different
questions and must never be confused:

  1. `measure_round_gate` -- the ONLY figure this module gates on. Runs the
     round through `coordinator_core.benchmarks.process_time ::
     batched_process_time_ms`, the sole shared process-time/spawn-count
     primitive (this module builds no second one, per the C1 brief). Its
     `process_time_ms` includes every child process the round spawns (a
     Windows job object rollup); its `procs_per_call` is the exact spawn
     count. `wall_ms` comes back too but is reported as CONTEXT ONLY, per
     that primitive's own annotation -- never gated on here either.

  2. `capture_phase_breakdown` -- reads production's OWN per-phase timing
     output (`coordinator/bin/publish.py :: _time_phase` /
     `_print_round_timing_summary`, landed by an earlier plan,
     docs/plans/2026-08-16-percolate-round-timing-and-changed-only.md C1).
     This function does not measure anything itself; it runs the round once
     more and parses the `[timing]` lines production already prints. Those
     figures are DRIVER-ONLY (`time.process_time()` on the driver process,
     explicitly excluding subprocess CPU -- see that function's own
     docstring) and exist for step-level context, never as a gate.

The round this module measures does NOT decompose cleanly into the six named
steps the rebuild plan describes (clone, transform, compare, move-and-
replace, commit, delete debris). This module does not invent a mapping.
`capture_phase_breakdown` reports the ~20 phase labels production actually
emits, verbatim; state/audits/2026-08-23-percolate-round-ab-baseline.md
records which of the six named steps have no per-phase oracle at all in the
CURRENT implementation (commit: skipped by this harness's own --no-commit
--dry-run invocation, which never mutates the destination; delete-debris:
runs every row via `_sweep_stale_publish_staging_dirs` but is not wrapped in
`_time_phase`, so its cost is invisible in the per-phase breakdown and folds
into "unattributed").

NEVER MUTATES THE PUBLISH DESTINATION: `round_command` hardcodes `--dry-run`
and `--no-commit` with no flag to disable either. A round that cannot be
measured without publishing is measured with `--dry-run` and the skipped
legs are named, not estimated (state/audits/2026-08-23-percolate-round-
process-time-census.md already did this for post_rsync/pre_ci; this module
does not re-derive that finding, only reuses the same invocation shape).

REAL-ROUND MODE (`measure_real_round_gate` / `capture_real_round_phase_breakdown`
/ `run_real_round_ab`, below): closes AC2 by running an actual, non-dry-run
`coordinator/bin/percolate-round.py` round -- but NEVER against the live
mirror. `provision_disposable_clone` resolves the live mirror path via the
REAL `machine-local` (never the shim below, to avoid recursing into itself),
then makes a bare local clone (the disposable "origin") and a working clone
of THAT bare clone -- two hops, so nothing this harness runs ever has the
live mirror as a git remote at all, not even one it merely doesn't push to.
A generated `MACHINE_LOCAL_BIN` shim (`_write_machine_local_shim`) redirects
exactly the two registry keys (`repos.claude_klabauter`,
`publish.mirrors.claude_klabauter.path`) that resolve the publish
destination to the disposable working clone; every other key passes through
the real `machine-local dump` unchanged. `_assert_dest_is_scratch` re-resolves
the destination through that same shim BEFORE the round runs and raises if it
is not under the scratch clone -- unconditionally, no flag disables it. The
round itself runs with `--no-publish` (stops before `git push`) on top of
that belt: even if something forced a push, it could only reach the
disposable bare "origin" clone, never the live mirror `machine-local`
resolves `repos.claude_klabauter` to. The clone tree is deleted in a
`finally` block regardless of outcome.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Iterator, List, Optional

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.git.run import GitResult, run_git
from coordinator_core.benchmarks.timer import SUBPROCESS_CREATIONFLAGS

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_PHASE_CAPTURE_TIMEOUT_S = 300
"""Bounded so a hung round fails loud rather than wedging the harness --
NOT `timer.SUBPROCESS_TIMEOUT_S` (60s): a round dry-run alone measures
~50-73s wall on this box (state/audits/2026-08-23-percolate-round-process-
time-census.md), already over that package-wide default."""
_PUBLISH_PY = _REPO_ROOT / "coordinator" / "bin" / "publish.py"

_DEFAULT_TARGET = "claude-klabauter-bin"
"""Matches the invocation state/audits/2026-08-23-percolate-round-process-
time-census.md already used and confirmed drives a full 9-row round
(procs_per_call: 120.0 measured there) -- reused rather than re-derived."""

_TIMING_PHASE_RE = re.compile(
    r"^\s*\[timing\]\s+([^:]+):\s+(.+?):\s+([\d.]+)s wall / ([\d.]+)s cpu"
)
_TIMING_ROUND_WALL_RE = re.compile(
    r"^\s*\[timing\] round wall time:\s*([\d.]+)s;\s*attributed:\s*([\d.]+)s;"
    r"\s*unattributed:\s*([\d.]+)s"
)
_TIMING_ROUND_CPU_RE = re.compile(
    r"^\s*\[timing\] round driver cpu:\s*([\d.]+)s;\s*attributed to phases:\s*([\d.]+)s"
)


def round_command(target: str) -> List[str]:
    """The one invocation shape this module ever runs. `--dry-run` and
    `--no-commit` are not parameters -- a caller who wants to measure a
    real, destination-mutating round needs a different, explicitly-
    authorized tool, not a flag on this one (§ module docstring)."""
    return [sys.executable, str(_PUBLISH_PY), target, "--dry-run", "--no-commit"]


def measure_round_gate(target: str = _DEFAULT_TARGET, *, k: int = 1) -> dict:
    """AC1/AC2/AC3's gating figures. `k` defaults to 1: a percolate round
    dry-run costs ~20s of process time / ~50-70s wall per invocation on this
    box (state/audits/2026-08-23-percolate-round-process-time-census.md).
    `batched_process_time_ms` amortises over k to recover sub-tick precision
    near a Windows job object's ~15.6ms quantisation -- irrelevant at a
    20,000ms+ scale, so k=1 avoids multiplying a round-scale cost across a
    shared box for precision this figure does not need. A caller after
    tighter confidence can pass a larger k explicitly.

    Returns `batched_process_time_ms`'s own dict unmodified: process_time_ms,
    wall_ms (context only), procs_per_call, rc, k.
    """
    return batched_process_time_ms(round_command(target), k=k, cwd=str(_REPO_ROOT))


def capture_phase_breakdown(target: str = _DEFAULT_TARGET) -> dict:
    """Runs the round ONCE more, directly (not through the job-object
    primitive, which sends stdout to DEVNULL), to parse production's own
    already-printed `[timing]` lines. This is not a second timing surface --
    it parses process_time/perf_counter data production code already
    computed and printed; this function contributes no new measurement
    mechanism. Figures returned here are driver-only (exclude subprocess
    CPU) and are reported as context, never as a gate.
    """
    proc = subprocess.run(
        round_command(target),
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_PHASE_CAPTURE_TIMEOUT_S,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    phases: List[dict] = []
    round_wall_s = round_attributed_s = round_unattributed_s = None
    round_driver_cpu_s = round_driver_cpu_attributed_s = None
    for line in proc.stdout.splitlines():
        m = _TIMING_PHASE_RE.match(line)
        if m:
            row_label, phase_label, wall_s, cpu_s = m.groups()
            phases.append(
                {
                    "row": row_label,
                    "phase": phase_label,
                    "wall_s": float(wall_s),
                    "cpu_s": float(cpu_s),
                }
            )
            continue
        m = _TIMING_ROUND_WALL_RE.match(line)
        if m:
            round_wall_s, round_attributed_s, round_unattributed_s = (
                float(x) for x in m.groups()
            )
            continue
        m = _TIMING_ROUND_CPU_RE.match(line)
        if m:
            round_driver_cpu_s, round_driver_cpu_attributed_s = (
                float(x) for x in m.groups()
            )
    return {
        "rc": proc.returncode,
        "phases": phases,
        "round_wall_s": round_wall_s,
        "round_attributed_s": round_attributed_s,
        "round_unattributed_s": round_unattributed_s,
        "round_driver_cpu_s": round_driver_cpu_s,
        "round_driver_cpu_attributed_s": round_driver_cpu_attributed_s,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def run_ab(target: str = _DEFAULT_TARGET, *, k: int = 1) -> dict:
    """Convenience entry point combining both measurements for one target.
    Used by both the CLI below and by C5 (old-vs-new verdict), which reruns
    this against the rebuilt round and records the after-figures beside
    this chunk's baseline."""
    return {
        "target": target,
        "command": round_command(target),
        "gate": measure_round_gate(target, k=k),
        "breakdown": capture_phase_breakdown(target),
    }



# ---------------------------------------------------------------------------
# Real-round mode -- a REAL (non-dry-run) round against a disposable clone
# ---------------------------------------------------------------------------

_REAL_ROUND_PY = _REPO_ROOT / "coordinator" / "bin" / "percolate-round.py"
_PERCOLATE_GATE_PY = _REPO_ROOT / "coordinator" / "bin" / "percolate-gate.py"

_REAL_ROUND_TIMEOUT_S = 1800
"""A real round does staging, transform, compare, swap, and commit -- not
just the dry-run subset. Generous but bounded: a hang must fail loud, not
wedge the harness or the box indefinitely."""

_MACHINE_LOCAL_KEYS_TO_REDIRECT = ("repos.claude_klabauter", "publish.mirrors.claude_klabauter.path")
"""The exact two `machine-local dump` keys that resolve the klabauter
publish destination (verified against a real `machine-local dump` on this
box before this module was written -- both keys carried the identical live
path). Every other key the shim sees passes through unmodified."""

_MACHINE_LOCAL_DUMP_TIMEOUT_S = 30


def _real_machine_local_exe() -> str:
    """Resolves the REAL `machine-local`, deliberately bypassing
    `MACHINE_LOCAL_BIN` (stripped from the env this looks up in) so this
    never recurses into the shim `_write_machine_local_shim` generates --
    the shim's own job is to call exactly this function once and delegate."""
    env = dict(os.environ)
    env.pop("MACHINE_LOCAL_BIN", None)
    exe = shutil.which("machine-local", path=env.get("PATH"))
    if exe is None:
        raise RuntimeError(
            "percolate_round_ab: real-round mode could not locate the real "
            "machine-local on PATH (with MACHINE_LOCAL_BIN stripped)."
        )
    return exe


def _real_machine_local_dump() -> dict:
    exe = _real_machine_local_exe()
    proc = subprocess.run(
        [exe, "dump"],
        capture_output=True,
        text=True,
        timeout=_MACHINE_LOCAL_DUMP_TIMEOUT_S,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"percolate_round_ab: 'machine-local dump' failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def resolve_live_mirror_path() -> Path:
    """Resolves the live klabauter mirror -- READ ONLY, never the
    destination of anything this module runs. `repos.claude_klabauter` is
    the exact key the C5 dispatch brief named."""
    dump = _real_machine_local_dump()
    raw = dump.get("repos.claude_klabauter")
    if not raw:
        raise RuntimeError(
            "percolate_round_ab: machine-local key 'repos.claude_klabauter' is unset."
        )
    return Path(raw)


_SHIM_SOURCE = '''"""Auto-generated by coordinator_core.benchmarks.percolate_round_ab --
real-round mode. NOT committed, NOT hand-maintained: regenerated fresh for
every disposable-clone round and deleted with the rest of the scratch tree.

Delegates to the REAL machine-local's own `dump`, then overrides exactly the
klabauter-mirror keys this file was generated with, so a REAL round writes
into a disposable clone instead of the live mirror. Every other key passes
through unmodified. Supports only the `dump` subcommand -- the one verified
call shape `coordinator/lib/percolate/resolve_target.py::_dump_registry`
actually uses."""
import json
import subprocess
import sys

REAL_BIN = {real_bin!r}
OVERRIDES = {overrides!r}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "dump":
        sys.stderr.write("machine-local-shim: only the 'dump' subcommand is supported\\n")
        return 1
    proc = subprocess.run([REAL_BIN, "dump"], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode
    values = json.loads(proc.stdout)
    values.update(OVERRIDES)
    sys.stdout.write(json.dumps(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_SHIM_SH_TEMPLATE = "#!/bin/sh\nexec {python_q} {script_q} \"$@\"\n"


def _write_machine_local_shim(overrides: dict, shim_dir: Path) -> Path:
    """Writes the shim script plus its directly-executable sibling
    (`.cmd` on Windows -- `CreateProcess` cannot exec an extensionless
    Python shebang script directly, same WinError 193 constraint
    `resolve_target.py::resolve_machine_local_bin` documents; a POSIX `.sh`
    sibling for parity elsewhere) and returns the path to set
    `MACHINE_LOCAL_BIN` to."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    script_path = shim_dir / "machine_local_shim.py"
    script_path.write_text(
        _SHIM_SOURCE.format(real_bin=_real_machine_local_exe(), overrides=overrides),
        encoding="utf-8", newline="\n",
    )
    if os.name == "nt":
        cmd_path = shim_dir / "machine-local-shim.cmd"
        cmd_path.write_text(
            f'@"{sys.executable}" "{script_path}" %*\r\n',
            encoding="utf-8", newline="\n",
        )
        return cmd_path
    sh_path = shim_dir / "machine-local-shim.sh"
    sh_path.write_text(
        _SHIM_SH_TEMPLATE.format(
            python_q=repr(sys.executable), script_q=repr(str(script_path))
        ),
        encoding="utf-8", newline="\n",
    )
    sh_path.chmod(sh_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return sh_path


def _git(args: List[str], *, cwd: Optional[Path] = None, remote: bool = False) -> GitResult:
    """This harness's git seam, a thin alias for the shared runner.

    The `timeout: int = 300` parameter it used to carry is gone, and so are
    the `timeout=600` values its two `clone` call sites passed. Neither was
    measured: the mirror those clones copy is 40MB and both hops are
    local-path `--no-hardlinks` clones, so `REMOTE_BUDGET_SECS` (which
    `git/run.py` names `clone` as a legitimate consumer of) is ample where
    600 was folklore.
    """
    return run_git(args, cwd=str(cwd) if cwd is not None else None, remote=remote)


def _onerror_clear_readonly(func, path, exc_info):
    """`shutil.rmtree` handler: a `.git` object file cloned from a
    read-only-flagged source can carry the read-only attribute on Windows,
    which blocks unlink outright. Clear it and retry once."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


@contextlib.contextmanager
def _disposable_clone(scratch_root: Path) -> Iterator[dict]:
    """Provisions a disposable TWO-HOP clone of the live klabauter mirror
    under `scratch_root` and deletes it unconditionally on exit.

    Two hops (bare "origin" clone, then a working clone OF that bare
    clone), not one: this way nothing this harness runs ever has the live
    mirror as a git remote at all -- belt-and-braces against a push
    reaching it, on top of `--no-publish` stopping the round before any
    push runs at all. Both clones are local (`file://`-equivalent path
    clones), so provisioning never touches the network and never touches
    the live mirror beyond a read.
    """
    live_mirror = resolve_live_mirror_path()
    if not live_mirror.is_dir():
        raise RuntimeError(
            f"percolate_round_ab: resolved live mirror {live_mirror} does not exist "
            "or is not a directory -- refusing to proceed."
        )
    tmp_root = scratch_root / f"percolate-round-real-{uuid.uuid4().hex[:12]}"
    origin_dir = tmp_root / "origin.git"
    work_dir = tmp_root / "work"
    tmp_root.mkdir(parents=True, exist_ok=False)
    try:
        bare_clone = _git(
            ["clone", "--bare", "--no-hardlinks", str(live_mirror), str(origin_dir)],
            remote=True,
        )
        if bare_clone.returncode != 0:
            raise RuntimeError(
                f"percolate_round_ab: bare clone of live mirror failed: {bare_clone.stderr}"
            )
        work_clone = _git(
            ["clone", "--no-hardlinks", str(origin_dir), str(work_dir)],
            remote=True,
        )
        if work_clone.returncode != 0:
            raise RuntimeError(
                f"percolate_round_ab: working clone of disposable origin failed: {work_clone.stderr}"
            )
        yield {"tmp_root": tmp_root, "origin_dir": origin_dir, "work_dir": work_dir}
    finally:
        shutil.rmtree(str(tmp_root), onerror=_onerror_clear_readonly)


def _assert_dest_is_scratch(target: str, env: dict, work_dir: Path) -> str:
    """Re-resolves the round's destination through the SAME env (i.e. the
    same `MACHINE_LOCAL_BIN` shim) the round itself will use, and refuses
    loudly if it is not under `work_dir`. Unconditional -- there is no flag
    anywhere in this module that skips or overrides this check."""
    proc = subprocess.run(
        [
            sys.executable, str(_PERCOLATE_GATE_PY), "list-targets",
            "--percolate-root", str(_REPO_ROOT), "--target", target,
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=_MACHINE_LOCAL_DUMP_TIMEOUT_S,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    dest = proc.stdout.strip()
    if proc.returncode != 0 or not dest:
        raise RuntimeError(
            f"percolate_round_ab: could not resolve dest for target {target!r} "
            f"through the scratch shim (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    resolved_dest = Path(dest).resolve()
    resolved_scratch = work_dir.resolve()
    try:
        resolved_dest.relative_to(resolved_scratch)
    except ValueError:
        raise RuntimeError(
            "percolate_round_ab: REFUSING TO RUN -- resolved destination "
            f"{resolved_dest} is NOT under the disposable clone {resolved_scratch}. "
            "This assertion has no override flag; it exists to make it structurally "
            "impossible for this module to run a real round against the live mirror."
        ) from None
    return dest


def real_round_command(target: str) -> List[str]:
    """`--no-publish`: stops before `git push` (still runs commit + CI
    smoke against the disposable clone -- see module docstring). `--yes`:
    non-interactive, skips the Step 3 confirmation prompt this harness has
    no TTY to answer."""
    return [sys.executable, str(_REAL_ROUND_PY), target, "--no-publish", "--yes"]


def round_module_command(target: str, *, no_commit: bool = False) -> List[str]:
    """`python -m coordinator_core.percolate.round <target>` -- the C3
    in-process six-step driver's own CLI (`round.py :: _cli_main`), never
    `percolate-round.py`/`publish.py`. No `--no-publish` flag exists because
    this driver never pushes at all (§ round.py module docstring, `commit`
    is the only git-touching step); `--no-commit` is the nearest analogue,
    for a caller that wants the write without the commit."""
    cmd = [sys.executable, "-m", "coordinator_core.percolate.round", target]
    if no_commit:
        cmd.append("--no-commit")
    return cmd


def measure_round_module_gate(
    target: str = _DEFAULT_TARGET,
    *,
    k: int = 1,
    scratch_root: Optional[Path] = None,
    no_commit: bool = False,
) -> dict:
    """Same disposable-clone provisioning and same `_assert_dest_is_scratch`
    safety check `measure_real_round_gate` uses (reused unchanged, not
    duplicated) -- only the measured COMMAND differs: `round_module_command`
    (round.py's own CLI) instead of `real_round_command`
    (`percolate-round.py`). This is what lets `--engine round` measure the
    C3 driver as a genuinely live, standalone path -- never through
    `publish.py` for the sync, never against the live mirror."""
    root = scratch_root or _default_scratch_root()
    with _disposable_clone(root) as clone:
        overrides = {key: str(clone["work_dir"]) for key in _MACHINE_LOCAL_KEYS_TO_REDIRECT}
        shim = _write_machine_local_shim(overrides, clone["tmp_root"] / "ml-shim")
        env = dict(os.environ)
        env["MACHINE_LOCAL_BIN"] = str(shim)
        _assert_dest_is_scratch(target, env, clone["work_dir"])
        result = batched_process_time_ms(
            round_module_command(target, no_commit=no_commit), k=k, env=env, cwd=str(_REPO_ROOT)
        )
        result["dest"] = str(clone["work_dir"])
        return result


def _default_scratch_root() -> Path:
    env_scratch = os.environ.get("CLAUDE_SCRATCHPAD")
    if env_scratch:
        return Path(env_scratch)
    return Path(tempfile.gettempdir())


def measure_real_round_gate(
    target: str = _DEFAULT_TARGET, *, k: int = 1, scratch_root: Optional[Path] = None
) -> dict:
    """AC2's real-round gate figure. Provisions one fresh disposable clone,
    runs the round through it exactly once via `batched_process_time_ms`
    (the same shared primitive `measure_round_gate` uses -- no second timing
    surface), and tears the clone down unconditionally. `k` defaults to 1:
    a real round is strictly more expensive than the dry-run subset, and
    each additional `k` provisions no new clone (the SAME clone would be
    reused across k invocations inside `batched_process_time_ms`, which
    `--delta` would then read as "already published" on invocation 2+ and
    skip -- so k>1 here would silently measure something cheaper than a
    real round, not a more precise one). Callers wanting a second real
    sample should call this function again, which provisions its own fresh
    clone.
    """
    root = scratch_root or _default_scratch_root()
    with _disposable_clone(root) as clone:
        overrides = {key: str(clone["work_dir"]) for key in _MACHINE_LOCAL_KEYS_TO_REDIRECT}
        shim = _write_machine_local_shim(overrides, clone["tmp_root"] / "ml-shim")
        env = dict(os.environ)
        env["MACHINE_LOCAL_BIN"] = str(shim)
        _assert_dest_is_scratch(target, env, clone["work_dir"])
        result = batched_process_time_ms(
            real_round_command(target), k=k, env=env, cwd=str(_REPO_ROOT)
        )
        result["dest"] = str(clone["work_dir"])
        return result


def capture_real_round_phase_breakdown(
    target: str = _DEFAULT_TARGET, *, scratch_root: Optional[Path] = None
) -> dict:
    """Same phase-breakdown parse as `capture_phase_breakdown`, against a
    SEPARATE fresh disposable clone (never the one `measure_real_round_gate`
    just consumed -- reusing one would hit `--delta`'s already-published
    skip on the second run and misreport a skipped round as a measured
    one). `percolate-round.py`'s own Step 1 `print(real.stdout)` (this
    module's own reading of that source, not modified here) means
    `publish.py`'s per-row `[timing]` lines surface on percolate-round.py's
    OWN stdout unchanged, so the same regexes this module already has
    apply here too.
    """
    root = scratch_root or _default_scratch_root()
    with _disposable_clone(root) as clone:
        overrides = {key: str(clone["work_dir"]) for key in _MACHINE_LOCAL_KEYS_TO_REDIRECT}
        shim = _write_machine_local_shim(overrides, clone["tmp_root"] / "ml-shim")
        env = dict(os.environ)
        env["MACHINE_LOCAL_BIN"] = str(shim)
        _assert_dest_is_scratch(target, env, clone["work_dir"])
        proc = subprocess.run(
            real_round_command(target),
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            timeout=_REAL_ROUND_TIMEOUT_S,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
        phases: List[dict] = []
        round_wall_s = round_attributed_s = round_unattributed_s = None
        round_driver_cpu_s = round_driver_cpu_attributed_s = None
        for line in proc.stdout.splitlines():
            m = _TIMING_PHASE_RE.match(line)
            if m:
                row_label, phase_label, wall_s, cpu_s = m.groups()
                phases.append(
                    {
                        "row": row_label,
                        "phase": phase_label,
                        "wall_s": float(wall_s),
                        "cpu_s": float(cpu_s),
                    }
                )
                continue
            m = _TIMING_ROUND_WALL_RE.match(line)
            if m:
                round_wall_s, round_attributed_s, round_unattributed_s = (
                    float(x) for x in m.groups()
                )
                continue
            m = _TIMING_ROUND_CPU_RE.match(line)
            if m:
                round_driver_cpu_s, round_driver_cpu_attributed_s = (
                    float(x) for x in m.groups()
                )
        refused = (
            "Rows FAILED:" in proc.stderr
            or "STATUS: PARTIAL" in proc.stderr
            or "percolate-round" in proc.stdout and "FAIL" in proc.stdout
        )
        return {
            "rc": proc.returncode,
            "refused_or_failed": bool(refused),
            "phases": phases,
            "round_wall_s": round_wall_s,
            "round_attributed_s": round_attributed_s,
            "round_unattributed_s": round_unattributed_s,
            "round_driver_cpu_s": round_driver_cpu_s,
            "round_driver_cpu_attributed_s": round_driver_cpu_attributed_s,
            "dest": str(clone["work_dir"]),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }


def run_real_round_ab(
    target: str = _DEFAULT_TARGET, *, k: int = 1, scratch_root: Optional[Path] = None
) -> dict:
    """Combines a real-round gate measurement and a real-round phase-
    breakdown capture, EACH against its own fresh disposable clone (see
    both functions' docstrings for why sharing one clone across the two
    would corrupt the second measurement via `--delta`)."""
    root = scratch_root or _default_scratch_root()
    return {
        "target": target,
        "command": real_round_command(target),
        "gate": measure_real_round_gate(target, k=k, scratch_root=root),
        "breakdown": capture_real_round_phase_breakdown(target, scratch_root=root),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Round-level A/B harness: process time + spawn count for a full "
            "percolate round (--dry-run --no-commit; never mutates the "
            "destination), plus production's own per-phase timing context."
        )
    )
    parser.add_argument(
        "--target",
        default=_DEFAULT_TARGET,
        help=f"publish.py target argument (default: {_DEFAULT_TARGET!r}).",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=1,
        help="batched_process_time_ms sample count (default: 1; see measure_round_gate).",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="Optional path to also write the full result as JSON.",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help=(
            "Real-round mode (AC2): runs an actual, non-dry-run "
            "percolate-round.py round against a disposable clone of the "
            "live mirror, never the mirror itself. See module docstring."
        ),
    )
    parser.add_argument(
        "--scratch-root",
        default=None,
        help="--real only: base directory for the disposable clone (default: $CLAUDE_SCRATCHPAD or the system temp dir).",
    )
    parser.add_argument(
        "--engine",
        choices=("publish", "round"),
        default="publish",
        help=(
            "--real only. 'publish' (default): the existing percolate-round.py/"
            "publish.py path. 'round': coordinator_core.percolate.round's own "
            "in-process six-step driver (C3), no publish.py sync involved."
        ),
    )
    args = parser.parse_args(argv)

    if args.engine == "round" and not args.real:
        parser.error("--engine round requires --real (there is no dry-run mode for round.py)")

    if args.engine == "round":
        scratch_root = Path(args.scratch_root) if args.scratch_root else None
        gate = measure_round_module_gate(args.target, k=args.k, scratch_root=scratch_root)
        print(f"target: {args.target}")
        print(f"command: {' '.join(round_module_command(args.target))}")
        print("--- gate (batched_process_time_ms; process time + spawn count GATE here) ---")
        print(f"  process_time_ms: {gate['process_time_ms']}")
        print(f"  procs_per_call:  {gate['procs_per_call']}")
        print(f"  wall_ms:         {gate['wall_ms']}  (context only -- never gate on this)")
        print(f"  rc:              {gate['rc']}  k: {gate['k']}")
        print(f"  dest (disposable clone): {gate['dest']}")
        if args.json:
            Path(args.json).write_text(json.dumps({"target": args.target, "gate": gate}, indent=2), newline="\n")
            print(f"wrote {args.json}")
        return 0

    if args.real:
        scratch_root = Path(args.scratch_root) if args.scratch_root else None
        result = run_real_round_ab(args.target, k=args.k, scratch_root=scratch_root)
    else:
        result = run_ab(args.target, k=args.k)

    gate = result["gate"]
    breakdown = result["breakdown"]
    print(f"target: {result['target']}")
    print(f"command: {' '.join(result['command'])}")
    print("--- gate (batched_process_time_ms; process time + spawn count GATE here) ---")
    print(f"  process_time_ms: {gate['process_time_ms']}")
    print(f"  procs_per_call:  {gate['procs_per_call']}")
    print(f"  wall_ms:         {gate['wall_ms']}  (context only -- never gate on this)")
    print(f"  rc:              {gate['rc']}  k: {gate['k']}")
    print("--- phase breakdown (production's own [timing] lines; context only) ---")
    print(f"  capture run rc: {breakdown['rc']}")
    print(f"  phases captured: {len(breakdown['phases'])}")
    for p in breakdown["phases"]:
        print(f"    {p['row']}: {p['phase']}: {p['wall_s']:.3f}s wall / {p['cpu_s']:.3f}s cpu")
    print(
        f"  round wall (driver's own view): {breakdown['round_wall_s']}; "
        f"attributed: {breakdown['round_attributed_s']}; "
        f"unattributed: {breakdown['round_unattributed_s']}"
    )
    print(
        f"  round driver cpu: {breakdown['round_driver_cpu_s']}; "
        f"attributed to phases: {breakdown['round_driver_cpu_attributed_s']}"
    )

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), newline="\n")
        print(f"wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
