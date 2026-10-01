"""The LFS pre-push gate: `replace` the stock git-lfs shim or an older rendering
of the coordinator gate with the current gate body, in engine clones only.

Classification and the body come from `install_lfs_pre_push_hook`; a foreign
hook is never matched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.git.hook_dispositions import HookDisposition, Match
from coordinator_core.ops.install_lfs_pre_push_hook import classify_existing, hook_body

ENTRY_ID = "lfs-pre-push-gate"


def _is_engine_clone(repo_root: Path) -> bool:
    return (repo_root / "coordinator_core/ops/install_lfs_pre_push_hook.py").is_file()


def _identify(text: str) -> Optional[Match]:
    if classify_existing(text) in ("stock-lfs", "ours") and text != hook_body():
        return Match(0, len(text))
    return None


ENTRY = HookDisposition(
    id=ENTRY_ID,
    hook_name="pre-push",
    action="replace",
    identify=_identify,
    replacement=hook_body,
    applies_to=_is_engine_clone,
)
