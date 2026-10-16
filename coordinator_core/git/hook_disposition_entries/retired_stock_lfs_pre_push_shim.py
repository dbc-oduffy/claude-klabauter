"""The stock `git lfs install` pre-push shim: whole-file `remove` in clones that
declare no `filter=lfs` attribute.

A clone with no LFS filter has nothing for the shim to upload, yet it runs on every
push. A hook with any line beyond the three the installer writes is foreign and never
matched; a clone that declares LFS keeps its shim.
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.git.hook_dispositions import HookDisposition, Match
from coordinator_core.git.lfs_attributes import clone_declares_lfs

ENTRY_ID = "retired-stock-lfs-pre-push-shim"

_SHEBANG = "#!/bin/sh"
_PRESENCE_PREFIX = "command -v git-lfs >/dev/null 2>&1 || {"
_PRESENCE_SUFFIX = "exit 2; }"
_DELEGATE = 'git lfs pre-push "$@"'


def _identify(text: str) -> Optional[Match]:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) != 3:
        return None
    shebang, presence, delegate = lines
    if shebang != _SHEBANG or delegate != _DELEGATE:
        return None
    if not (presence.startswith(_PRESENCE_PREFIX) and presence.endswith(_PRESENCE_SUFFIX)):
        return None
    return Match(0, len(text))


ENTRY = HookDisposition(
    id=ENTRY_ID,
    hook_name="pre-push",
    action="remove",
    identify=_identify,
    applies_to=lambda root: not clone_declares_lfs(root),
)
