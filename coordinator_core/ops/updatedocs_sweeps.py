"""
coordinator_core.ops.updatedocs_sweeps — JSON-RPC "updatedocs.sweeps" operation.

Purpose: apply the two `/update-docs` sweeps that WRITE — 11i (prune resolved-state
bloat from the queues) and 11j (reap stale subagent-share sidecars). They left
`updatedocs.gates` so that op stays a read-only verdict battery; this op is the
mutating half and returns the same `{gates, rollup}` shape.

Repo-root resolution: keyed "common_dir" in coordinator_core/op_scopes.py, so the
injected `repo_root` is the git COMMON DIR, not a worktree. The handler derives the
worktree via `main_worktree_root()` exactly as `updatedocs.gates` does.

NEGATIVE SPEC — do not "simplify" this back to using the injected root directly.
Joining a corpus onto `<worktree>/.git` yields a real-but-wrong directory: a sweep
over it reports CLEAN over an empty walk. See `updatedocs_gates` for the incident.
"""

from __future__ import annotations

import io
import re
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import record_homes
from coordinator_core.ops.updatedocs_gates import (
    _DETAIL_TAIL_CHARS,
    GateResult,
    GateVerdict,
    Severity,
    _GateFn,
    _resolve_cli,
    _run,
    _run_in_process,
    _settings_home,
    rollup,
)

# ---------------------------------------------------------------------------
# Sweep 11i: Prune resolved-state bloat from queues
# ---------------------------------------------------------------------------

_DEFAULT_QUEUES = (
    "state/coordinator-improvement-queue.md",
    "state/improvement-queue.md",
    "state/bug-backlog.md",
)

_YAML_QUEUE_FAMILIES = (
    ("bug-backlog", "prune-closed-bugs.py"),
    ("improvement-queue", "prune-closed-improvements.py"),
)


def _gate_queue_prune_sweep(repo_root: Path, settings_home: Path, overrides: dict) -> GateResult:
    bin_dir = settings_home / "bin"
    override_cli = overrides.get("prune_cli")
    prune_cli = Path(override_cli) if override_cli else None
    queues = overrides.get("queues") or list(_DEFAULT_QUEUES)

    overall_fail = False
    total_pruned = 0
    lines: list[str] = []

    existing_queues = [q for q in queues if (repo_root / q).is_file()]
    any_legacy_present = bool(existing_queues)
    before_counts = {
        q: sum(1 for _ in (repo_root / q).open("r", encoding="utf-8", errors="replace"))
        for q in existing_queues
    }

    if existing_queues:
        if override_cli:
            # A caller-substituted CLI (overrides["prune_cli"]) is not
            # guaranteed to accept multiple positionals the way our own
            # prune-resolved-queue-entries.py now does — keep the original
            # one-spawn-per-queue contract for this seam rather than
            # assuming multi-arg support it may not have.
            for queue in existing_queues:
                proc = _run(prune_cli, [str(repo_root / queue)])
                if proc is None or proc.returncode != 0:
                    overall_fail = True
                    lines.append(f"prune FAILED for {queue}")
        else:
            # The default callee prunes N queue files independently in one call
            # and reports each failure on stderr, so the sweep is one in-process
            # call; only an explicit prune_cli override spawns.
            from coordinator_core.cli_entry import run_op_main

            paths = [str(repo_root / q) for q in existing_queues]
            proc = _run_in_process(
                lambda: run_op_main("coordinator_core.ops.prune_resolved_queue_entries", paths, cwd=str(repo_root))
            )
            if proc.returncode != 0:
                overall_fail = True
                stderr_lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
                lines.extend(stderr_lines or ["prune FAILED (nonzero exit, no stderr detail)"])

    for queue in existing_queues:
        queue_path = repo_root / queue
        after = sum(1 for _ in queue_path.open("r", encoding="utf-8", errors="replace"))
        pruned = before_counts[queue] - after
        total_pruned += pruned
        if pruned:
            lines.append(f"pruned {pruned} lines from {queue}")

    any_yaml_present = False
    for kind, cli_name in _YAML_QUEUE_FAMILIES:
        family_dir = f"state/{kind}"
        dir_path = Path(record_homes.home_dir(str(repo_root), kind))
        if not dir_path.is_dir():
            continue
        any_yaml_present = True
        cli_path = _resolve_cli(bin_dir, cli_name)
        if not cli_path.is_file():
            overall_fail = True
            lines.append(f"YAML prune CLI missing for {family_dir}: {cli_path}")
            continue
        # A nonzero exit (partial-archive failure) must not collapse into CLEAN:
        # fold it into overall_fail like the legacy markdown leg above.
        yaml_proc = _run(cli_path, ["--repo-root", str(repo_root)])
        if yaml_proc is None or yaml_proc.returncode != 0:
            overall_fail = True
            exit_desc = "could not be spawned" if yaml_proc is None else f"exit {yaml_proc.returncode}"
            lines.append(f"YAML prune FAILED for {family_dir} ({exit_desc})")

    if not any_legacy_present and not any_yaml_present:
        return GateResult(
            "11i-queue-prune-sweep",
            GateVerdict.UNAVAILABLE,
            f"no legacy markdown queues and no YAML queue families found under {repo_root} — verify repo_root",
        )

    if overall_fail:
        return GateResult(
            "11i-queue-prune-sweep",
            GateVerdict.FINDING,
            "queue prune failed loud on unexpected structure — must not be bypassed",
            Severity.BLOCKING,
            detail={"lines": lines},
        )
    if total_pruned:
        return GateResult(
            "11i-queue-prune-sweep",
            GateVerdict.FINDING,
            f"pruned {total_pruned} resolved-state line(s)",
            Severity.INFORMATIONAL,
            detail={"lines": lines, "total_pruned": total_pruned},
        )
    return GateResult("11i-queue-prune-sweep", GateVerdict.CLEAN, "queue prune clean (no resolved bloat found)")


# ---------------------------------------------------------------------------
# Sweep 11j: Reap stale subagent-share sidecars
# ---------------------------------------------------------------------------

# The reap's own count line reads "N stale subagent-share sidecar(s) reaped"; the
# leading "reaped N" form is the older shape and is still accepted.
_REAPED_RE = re.compile(r"reaped\s+(\d+)|(\d+)\s+stale subagent-share sidecar\(s\) reaped", re.IGNORECASE)


def _gate_reap_stale_sidecars(repo_root: Path, settings_home: Path, overrides: dict) -> GateResult:
    if overrides.get("cli"):
        cli = Path(overrides["cli"])
        if not cli.exists():
            return GateResult("11j-reap-stale-sidecars", GateVerdict.UNAVAILABLE, f"CLI not found at {cli}")
        proc = _run(cli, ["--repo-root", str(repo_root)])
        if proc is None:
            return GateResult("11j-reap-stale-sidecars", GateVerdict.UNAVAILABLE, "CLI could not be spawned")
    else:
        from coordinator_core.session.liveness import session_live
        from coordinator_core.sidecar_reap import reap_stale_sidecars

        out, err = io.StringIO(), io.StringIO()
        try:
            code = reap_stale_sidecars(str(repo_root), session_live, out=out, err=err)
        except Exception as exc:  # noqa: BLE001 — a crashed reap is a failed gate, not a crashed op
            code = 1
            err.write(f"{type(exc).__name__}: {exc}\n")
        proc = subprocess.CompletedProcess([], code, out.getvalue(), err.getvalue())
    if proc.returncode != 0:
        return GateResult(
            "11j-reap-stale-sidecars",
            GateVerdict.FINDING,
            f"reap failed (exit {proc.returncode}) — surface, do not skip",
            Severity.BLOCKING,
            detail={"stderr": proc.stderr[-_DETAIL_TAIL_CHARS:]},
        )
    reaped_match = _REAPED_RE.search(proc.stdout or "")
    reaped = int(reaped_match.group(1) or reaped_match.group(2)) if reaped_match else 0
    if reaped:
        return GateResult(
            "11j-reap-stale-sidecars",
            GateVerdict.FINDING,
            f"reaped {reaped} stale sidecar(s)",
            Severity.INFORMATIONAL,
            detail={"reaped": reaped},
        )
    return GateResult("11j-reap-stale-sidecars", GateVerdict.CLEAN, "subagent-share sidecar reap clean (nothing stale)")


_SWEEPS: dict[str, _GateFn] = {
    "11i-queue-prune-sweep": _gate_queue_prune_sweep,
    "11j-reap-stale-sidecars": _gate_reap_stale_sidecars,
}


@register_op("updatedocs.sweeps")
def _updatedocs_sweeps(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "updatedocs.sweeps" handler — applies the queue prune and the
    sidecar reap and returns {"gates": [...], "rollup": {...}}.

    Params (all optional):
        repo_root      (str) — worktree root; default: the injected common dir
                         translated through main_worktree_root(), else cwd.
        settings_home  (str) — override for $COORDINATOR_SETTINGS_HOME resolution.
        overrides      (dict[str, dict]) — per-sweep keyword overrides keyed by
                         sweep id, for test/diagnostic use.
    """
    explicit = params.get("repo_root")
    if explicit:
        root = Path(explicit)
    elif repo_root is not None:
        root = main_worktree_root(Path(repo_root))
    else:
        root = Path.cwd()
    settings_home = _settings_home(params.get("settings_home"))
    overrides_by_gate: dict[str, dict] = params.get("overrides") or {}
    results = [fn(root, settings_home, overrides_by_gate.get(sweep_id, {})) for sweep_id, fn in _SWEEPS.items()]
    return {
        "gates": [r.to_dict() for r in results],
        "rollup": rollup(results),
    }
