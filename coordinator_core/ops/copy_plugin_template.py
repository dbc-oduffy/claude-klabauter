"""
coordinator_core.ops.copy_plugin_template — JSON-RPC
"repo_setup.copy_console_subprocess_tripwire" operation.

Purpose: content-idempotent install of the coordinator-standard
console-subprocess tripwire test into a consuming repo, then a direct pytest
verification run — the native port of the repo-setup fence
(DoE coordinator/skills/repo-setup/SKILL.md:1103 "Install steps": trusted-root
bash preamble + `cp` + `pytest`). The bash oracle's `cp` overwrites
unconditionally, clobbering hand-customized PREFIXES/EXACT_FILES allowlist
edits on rerun — the exact hazard the C0a manifest names. Settlement A6
(docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A6,
RATIFIES) closes it with a content-idempotent copy:

    dest absent                      → copy (`shutil.copy2`), `copied: true`
    dest byte-identical to template  → skip, `copied: false`
    dest differs from template       → SKIP-AND-REPORT (`skipped_existing:
                                       true`), NEVER clobber — differing bytes
                                       are presumed hand-customized allowlist
                                       edits.

Then the tripwire test runs in every branch via
`[sys.executable, "-m", "pytest", <dest>]` — a direct pytest subprocess with
no bash preamble (CC-1: `sys.executable` is a named binary, no shell
interpreter anywhere in the invocation chain).

Idempotency (AC7, DEC-7 note): achieved BY DESIGN via the content-idempotent
copy — a second invocation with identical inputs finds the destination
byte-identical to the template and performs zero writes, returning
`{copied: false, skipped_existing: false}` plus a fresh (read-only)
verification run. The double-invocation test asserts exactly this no-op
shape and that the destination bytes are untouched.

Cross-repo resolution: the template lives in the DoE / coordinator-claude
tree and is resolved EXCLUSIVELY via the mandated resolver
`coordinator_core.ops.coordinator_doe_root.coordinator_doe_root()` (parent
plan § Mandated resolvers) — never a literal path, never a `parents[n]` walk
across the repo boundary. Unresolvable DoE root → structured error, fail
loud (CC-7).

Fail-loud (CC-7): missing target repo root, unresolvable DoE root, missing
template file, and a destination that exists but is not a regular file each
raise a structured TripwireCopyError naming the offending path — the op
never picks a side silently.

Manual steps stay human (oracle scope note, preserved): customizing the
PREFIXES/EXACT_FILES allowlist and placing suppression markers are NOT this
op's job; it covers mkdir+copy+verify only.

Scope: `show_top` (ratified per-row in op-classification.tsv — both the copy
destination and the pytest run land in the specific worktree's checked-out
files; a common_dir key would let a copy destined for one linked worktree
register against a different worktree's key). Registration on the three
shared surfaces (`_EAGER_OP_MODULES`, `_OP_KEY_SCOPE`, `_registry_map.py`)
lands in the EM-serial registration pass per CC-3; this module carries only
its own `register_op`.

Contract: params {target_repo_root: str}
          -> {copied: bool, skipped_existing: bool, test_passed: bool}
Spec backlink: pln-wave-3-design-settlements-15-d-76fdbd § A6
Parent plan:   docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Mandated resolvers

Negative-spec:
    - NEVER overwrites an existing destination, byte-identical or not — the
      only write path is dest-absent → copy. There is no --force flag and
      none may be added without a new settlement.
    - Does NOT reimplement the bash oracle's trusted-root preamble
      (`.doe-root` cat + prefix trust check) — root resolution delegates
      entirely to the mandated resolver, which owns that discipline.
    - Does NOT parse or edit the template's allowlist; human customization
      is out of scope by the oracle's own note.
"""

from __future__ import annotations

import shutil
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._pytest_child_env import pytest_child_env
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

_TEMPLATE_REL = Path("coordinator") / "tests" / "templates" / "test_no_bare_console_subprocess.py"

_DEST_REL = Path("tests") / "test_no_bare_console_subprocess.py"


class TripwireCopyError(RuntimeError):
    pass


def _resolve_template_path() -> Path:
    """Resolve the tripwire template via the mandated DoE-root resolver.

    Raises TripwireCopyError when the resolver returns None (rung-4 hard
    failure) — remediation is the resolver's own documented story
    (re-run coordinator:install / set REPO_DOE_CLAUDE).
    """
    doe_root = coordinator_doe_root()
    if not doe_root:
        raise TripwireCopyError(
            "repo_setup.copy_console_subprocess_tripwire: DoE / coordinator-claude "
            "root unresolvable via coordinator_doe_root() — cannot locate the "
            f"tripwire template ({_TEMPLATE_REL.as_posix()}). Re-run "
            "coordinator:install or set REPO_DOE_CLAUDE."
        )
    return Path(doe_root) / _TEMPLATE_REL


def _run_pytest(dest: Path, cwd: Path) -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(dest)],
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=pytest_child_env(),
        **no_console_creationflags(),
    )
    return proc.returncode == 0


def copy_console_subprocess_tripwire(
    target_repo_root: str,
    template_path: Optional[Path] = None,
) -> dict:
    target = Path(target_repo_root)
    if not target.is_dir():
        raise TripwireCopyError(
            "repo_setup.copy_console_subprocess_tripwire: target_repo_root "
            f"{str(target)!r} does not exist or is not a directory"
        )

    template = _resolve_template_path() if template_path is None else Path(template_path)
    if not template.is_file():
        raise TripwireCopyError(
            "repo_setup.copy_console_subprocess_tripwire: tripwire template "
            f"{str(template)!r} does not exist or is not a regular file"
        )

    dest = target / _DEST_REL
    copied = False
    skipped_existing = False

    if dest.exists():
        if not dest.is_file():
            raise TripwireCopyError(
                "repo_setup.copy_console_subprocess_tripwire: destination "
                f"{str(dest)!r} exists but is not a regular file (template: "
                f"{str(template)!r}) — refusing to proceed"
            )
        if dest.read_bytes() == template.read_bytes():
            pass
        else:
            skipped_existing = True
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, dest)
        copied = True

    test_passed = _run_pytest(dest, cwd=target)
    return {
        "copied": copied,
        "skipped_existing": skipped_existing,
        "test_passed": test_passed,
    }


@register_op("repo_setup.copy_console_subprocess_tripwire")
def _copy_console_subprocess_tripwire(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'repo_setup.copy_console_subprocess_tripwire' handler — sync.

    Params: target_repo_root (str, required) — the consuming repo's worktree
    root. repo_root (ipc-injected, scope show_top) is not consumed: the
    contract carries the target explicitly, matching the fence's shape where
    repo-setup acts ON a named consuming repo rather than the caller's own.
    """
    target_repo_root = params.get("target_repo_root") or ""
    if not target_repo_root:
        raise TripwireCopyError(
            "repo_setup.copy_console_subprocess_tripwire: required param "
            "'target_repo_root' is missing or empty"
        )
    return copy_console_subprocess_tripwire(target_repo_root)
