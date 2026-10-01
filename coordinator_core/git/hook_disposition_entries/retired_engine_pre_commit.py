"""The retired claude-klabauter `pre-commit` gate chain: whole-file `remove` for a
`pre-commit` carrying the registry banner the deleted
`install_claude_klabauter_precommit_hook` stamped into every hook it wrote.

Identification is the banner alone. Hooks written by the coordinator-content-repo and
meta-repo installers carry their own installer's name in the same sentence and
are not matched.
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.git.hook_dispositions import HookDisposition, Match

ENTRY_ID = "retired-claude-klabauter-pre-commit"

# Must equal `_BANNER` in coordinator/bin/remove-claude-klabauter-precommit-hook.py.
_BANNER = "Registry-driven (coordinator_core.ops.install_claude_klabauter_precommit_hook)"


def _identify(text: str) -> Optional[Match]:
    return Match(0, len(text)) if _BANNER in text else None


ENTRY = HookDisposition(
    id=ENTRY_ID,
    hook_name="pre-commit",
    action="remove",
    identify=_identify,
)
