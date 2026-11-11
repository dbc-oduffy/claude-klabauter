"""A `pre-commit` that is a byte copy of a `coordinator/bin` forwarder stub.

Copied into `.git/hooks` the stub has no `lib/` beside it, so every commit dies on
`from _engine_forward import forward`. Replaced whole with the self-resolving
corpus-guard hook body.
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.git.corpus_guard_hook import hook_body, is_forwarder_stub
from coordinator_core.git.hook_dispositions import HookDisposition, Match

ENTRY_ID = "forwarder-stub-pre-commit"


def _identify(text: str) -> Optional[Match]:
    return Match(0, len(text)) if is_forwarder_stub(text) else None


ENTRY = HookDisposition(
    id=ENTRY_ID,
    hook_name="pre-commit",
    action="replace",
    identify=_identify,
    replacement=hook_body,
)
