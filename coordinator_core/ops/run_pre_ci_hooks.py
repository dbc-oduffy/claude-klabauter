"""
coordinator_core.ops.run_pre_ci_hooks — JSON-RPC "percolate.run_pre_ci_hooks"
operation: discover and invoke registered pre-CI publish hooks for a percolate
target, Python-only hook contract.

Purpose: replaces the percolate Step 5a fence (example-doctrine-repo skills/percolate/SKILL.md
§ Step 5a), which iterated ``$PERCOLATE_ROOT/setup/percolate-hooks/<target>/
pre-ci/*.sh`` invoking each via ``bash "$hook" "<dest>"``. That shape has no
Windows-portable equivalent (assumes bash on PATH, assumes the hooks are
bash), and the naked-Python mandate makes new shell hooks illegal anyway.

Settled hook contract (Wave-3 settlement B3 — SETTLES, hook contract + scope):
    - Iterate ``<hooks_root>/pre-ci/*.py`` in lexical (name-sorted) order.
    - Invoke each hook as ``[sys.executable, <hook>, <dest>]`` — direct
      list-argv subprocess of the running interpreter (CC-1: no shell
      interpreter anywhere in the invocation chain).
    - Any non-zero exit aborts the sweep: ``aborted_at`` names that hook,
      ``exit_code`` carries its return code, later hooks do not run.
    - A ``.sh`` file encountered in the pre-ci dir is a structured error
      naming the file and the migration requirement — fail loud, per the
      no-degradation-story PM ruling: NO bash fallback, NO silent skip.
      The check runs BEFORE any hook executes, so a mixed .py/.sh dir runs
      nothing at all (deterministic: the dir is either fully migrated or
      the sweep refuses it).
    - Missing or empty ``pre-ci`` dir is a clean skip (the fence's nullglob
      guard: "If no pre-ci directory exists or it's empty, skip silently").

Contract (op-classification.tsv row run-registered-pre-ci-hooks):
    params: {hooks_root: str, dest: str}
        hooks_root — the percolate hooks root for the target (the fence's
                     ``setup/percolate-hooks/<target>``); the op appends
                     ``pre-ci`` itself.
        dest       — the publish destination path, handed to each hook as
                     its sole argument (the fence's ``$1``).
    -> {hooks_run: list[str], aborted_at: str | None, exit_code: int}
        hooks_run  — basenames of hooks that completed successfully, in
                     execution (lexical) order.
        aborted_at — basename of the hook whose non-zero exit aborted the
                     sweep, or None if all hooks passed (or none existed).
        exit_code  — 0 on full success / clean skip; the aborting hook's
                     return code otherwise.

Keying scope: none — ``hooks_root`` and ``dest`` are EXPLICIT caller-supplied
params mirroring percolate.run's ``target_root`` precedent; no repo state is
read via an injected worktree, and ``repo_root`` is unused. Settlement B3
records that the ``none`` verdict holds ONLY because of this: resolving
``setup/percolate-hooks/`` off a caller's worktree implicitly would leave the
settlement (the verdict would have to flip to show_top) — do not add it.

Idempotency (DEC-7 note, per settlement B3): the sweep itself is
read-iterate-invoke — rerunning it re-discovers the same lexical hook list
and re-invokes each hook once, exactly as the first run did. Whether the
SECOND run of a given hook is a safe no-op is that hook's own contract:
per-hook idempotency is CALLER-HOOK-DEPENDENT and is documented here rather
than closed here (the op cannot and does not enforce it).

Self-registration: importing this module calls
register_op("percolate.run_pre_ci_hooks", ...) as a side-effect. Registration
across the remaining three surfaces (_EAGER_OP_MODULES / _OP_KEY_SCOPE /
_registry_map.py) lands in the separate EM-serial registration pass (CC-3).

Spec backlink: docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B3
Port source: example-doctrine-repo coordinator/skills/percolate/SKILL.md § Step 5a (bash fence)

Negative-spec (hard-won):
  - Does NOT execute ``.sh`` hooks via bash, sh, or any shell — there is no
    Windows-portable way to run arbitrary shell hooks, and the PM ruling is
    explicit: no degradation story, fail loud instead.
  - Does NOT silently skip a ``.sh`` file (that would hide a hook the fence
    used to run — silent behavior change is worse than a loud error).
  - Does NOT resolve ``setup/percolate-hooks/`` off any worktree or repo
    root — hooks_root is caller-supplied by settlement (scope-none anchor).
  - Does NOT rely on shebang dispatch — hooks run under ``sys.executable``
    regardless of their shebang line (shebangs are inert on Windows).
  - Does NOT continue past a failing hook (the fence's ``|| exit 1``).
"""

from __future__ import annotations

import logging
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op

logger = logging.getLogger(__name__)

#: Subdirectory of hooks_root that holds the pre-CI hook files (fence layout).
_PRE_CI_SUBDIR = "pre-ci"

#: Max characters of a failing hook's stderr replayed into the log.
_STDERR_LOG_TAIL = 2000


@register_op("percolate.run_pre_ci_hooks")
def _run_pre_ci_hooks(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "percolate.run_pre_ci_hooks" handler (sync — offloaded to a
    worker thread by dispatch, so the blocking subprocess.run calls here never
    park the event loop).

    See module docstring for the full contract. Raises ValueError (structured
    fail-loud) on a missing param or on any ``.sh`` file in the pre-ci dir.
    """
    hooks_root_raw = (params.get("hooks_root") or "").strip()
    if not hooks_root_raw:
        raise ValueError(
            "percolate.run_pre_ci_hooks: missing required param 'hooks_root' "
            "(explicit caller-supplied percolate hooks root — the op never "
            "resolves it off a worktree)"
        )
    dest = (params.get("dest") or "").strip()
    if not dest:
        raise ValueError(
            "percolate.run_pre_ci_hooks: missing required param 'dest' "
            "(publish destination path, handed to each hook as its sole argument)"
        )

    hooks_dir = Path(hooks_root_raw) / _PRE_CI_SUBDIR

    if not hooks_dir.is_dir():
        # Fence parity: "If no pre-ci directory exists or it's empty, skip
        # silently" — absence of hooks is a valid, healthy state.
        return {"hooks_run": [], "aborted_at": None, "exit_code": 0}

    # Python-only contract enforcement — BEFORE any hook runs, so a mixed
    # dir executes nothing (settlement B3 fail-loud, no-degradation-story).
    sh_files = sorted(p.name for p in hooks_dir.glob("*.sh") if p.is_file())
    if sh_files:
        raise ValueError(
            "percolate.run_pre_ci_hooks: shell hook(s) present under "
            f"{hooks_dir}: {', '.join(sh_files)} — the pre-ci hook contract "
            "is Python-only (Wave-3 settlement B3; naked-Python mandate). "
            "Re-author each listed hook as a .py file invoked as "
            "[sys.executable, hook, dest]. There is no bash fallback and no "
            "silent skip; no hooks were run."
        )

    hooks: List[Path] = sorted(
        (p for p in hooks_dir.glob("*.py") if p.is_file()),
        key=lambda p: p.name,
    )

    hooks_run: List[str] = []
    for hook in hooks:
        logger.debug("percolate.run_pre_ci_hooks: invoking %s", hook.name)
        proc = subprocess.run(
            [sys.executable, str(hook), dest],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            **no_console_creationflags(),
        )
        if proc.returncode != 0:
            logger.error(
                "percolate.run_pre_ci_hooks: hook %s exited %d; stderr tail: %s",
                hook.name,
                proc.returncode,
                (proc.stderr or "")[-_STDERR_LOG_TAIL:],
            )
            return {
                "hooks_run": hooks_run,
                "aborted_at": hook.name,
                "exit_code": proc.returncode,
            }
        hooks_run.append(hook.name)

    return {"hooks_run": hooks_run, "aborted_at": None, "exit_code": 0}
