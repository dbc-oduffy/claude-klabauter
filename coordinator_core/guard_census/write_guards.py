"""Write-guard census: one fate per ``coordinator_core/write_guards`` module.

A module registered in ``deny_registry`` (``SUBAGENT_DENY_CLASS``) is ``block``, naming its redirect;
every other module is ``guide``: its deny resolves through ``machine_profile.apply_guard_level``
into one logged advisory and never refuses, and each message names the sanctioned path
(``coordinator-doc-new``, the engine op, the owning session), which is the value kept. The engine
discovers modules by directory scan, so a write guard has no wiring to unpick; a body whose message
names no path is deleted with its tests and pins in the delete pass.
"""

from __future__ import annotations

from coordinator_core import deny_registry
from coordinator_core.guard_census import BLOCK, GUIDE, Row, enumerate_write_guards

_REDIRECT = {
    "guard-doctrine-surface-edits": "route the CLAUDE.md change to the EM, who edits it or sends the owning repo a memo",
}

_REGISTERED = {**deny_registry.GUARD_DENY_CLASS, **deny_registry.SUBAGENT_DENY_CLASS}

ROWS = [
    Row(name, BLOCK, _REDIRECT[name]) if name in _REGISTERED else Row(name, GUIDE)
    for name in enumerate_write_guards()
]
