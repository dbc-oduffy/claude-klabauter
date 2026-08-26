"""coordinator/bin/tests/test_installed_hook_gate_scripts_resolve.py —
proves the LOCALLY-INSTALLED `.git/hooks/pre-commit` (if any) actually names
scripts that still exist on disk.

Purpose: AC8 of `docs/plans/2026-08-25-the-staged-rollback-gate-dies-
without-blocking-a-commit.md` requires that no clone is left with an
installed hook pointing at a deleted gate script. "The C3 commit exists" is
NOT proof of that — `--no-verify`, the mass-deletion override, or a
ceremony-path bypass can all land a commit while leaving a stale hook
installed on THIS box. This test is the mechanism that actually discharges
the rule: it reads whatever `.git/hooks/pre-commit` is really installed
(git-common-dir-resolved, worktree/submodule-safe) and, if it carries the
registry banner `install_claude_klabauter_precommit_hook` stamps into every hook it
writes, asserts every `_gate_script="..."` path the hook body names resolves
on disk relative to the repo root.

Fails RED on exactly the state AC8 exists to prevent: an installed hook
whose gate script(s) were deleted out from under it. Survives this plan past
this session because it inspects the actual installed artifact, not a
static in-repo fixture.

Negative-spec:
    - Skips (does not fail) when no hook is installed, or the installed hook
      does not carry the registry banner — this test proves nothing about a
      hook that was never installed by `install_claude_klabauter_precommit_hook`, and
      an absent/foreign hook is a legitimate, expected state on most boxes.
    - Does not itself install, remove, or modify the hook — read-only probe.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BANNER = "Registry-driven (coordinator_core.ops.install_claude_klabauter_precommit_hook)"
_GATE_SCRIPT_RE = re.compile(r'_gate_script="([^"]+)"')


def _no_console_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git_common_dir(target: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(target),
            capture_output=True,
            text=True,
            check=False,
            creationflags=_no_console_creationflags(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = target / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def test_installed_hook_gate_scripts_resolve_on_disk():
    common_dir = _git_common_dir(_REPO_ROOT)
    if common_dir is None:
        pytest.skip("not resolvable as a git repo from this checkout")

    hook_path = common_dir / "hooks" / "pre-commit"
    if not hook_path.is_file():
        pytest.skip("no .git/hooks/pre-commit installed on this box")

    text = hook_path.read_text(encoding="utf-8")
    if _BANNER not in text:
        pytest.skip("installed hook does not carry the registry banner — not ours")

    gate_scripts = _GATE_SCRIPT_RE.findall(text)
    assert gate_scripts, (
        f"{hook_path} carries the registry banner but no _gate_script=\"...\" "
        "assignment was found — parse regressed or the hook body shape changed"
    )

    missing = [
        script
        for script in gate_scripts
        if not (_REPO_ROOT / script).is_file()
    ]
    assert not missing, (
        f"{hook_path} is installed and carries the registry banner, but names "
        f"gate script(s) that no longer exist on disk: {missing} — this is the "
        "exact state AC8 exists to prevent. Re-run "
        "coordinator/bin/remove-claude-klabauter-precommit-hook.py (or the installer) "
        "to repair it."
    )
