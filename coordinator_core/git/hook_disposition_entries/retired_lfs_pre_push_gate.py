"""The retired coordinator LFS pre-push gate: whole-file `remove` of a hook body
carrying the installer-stamped `# coordinator-lfs-gate` banner line.

A stock `git lfs pre-push` shim and any foreign hook are never matched. The
entry applies only to clones that declare no `filter=lfs` attribute, so a repo
that really tracks LFS keeps its delegating hook.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.hook_dispositions import HookDisposition, Match

ENTRY_ID = "retired-lfs-pre-push-gate"

_BANNER = "# coordinator-lfs-gate"


def _identify(text: str) -> Optional[Match]:
    if any(line.strip().startswith(_BANNER) for line in text.splitlines()):
        return Match(0, len(text))
    return None


def _declares_lfs(path: Path) -> bool:
    try:
        return "filter=lfs" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _tracks_no_lfs(repo_root: Path) -> bool:
    return not (
        _declares_lfs(repo_root / ".gitattributes")
        or _declares_lfs(resolve_git_common_dir(repo_root) / "info" / "attributes")
    )


ENTRY = HookDisposition(
    id=ENTRY_ID,
    hook_name="pre-push",
    action="remove",
    identify=_identify,
    applies_to=_tracks_no_lfs,
)
