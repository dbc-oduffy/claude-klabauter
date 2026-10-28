"""Write-guard census: one fate per ``coordinator_core/write_guards`` module.

No write guard is in ``deny_registry`` (every registered entry is a bash-plane act), so no row is
``block``. Every module is ``guide``: its deny resolves through ``machine_profile.apply_guard_level``
into one logged advisory and never refuses, and each message names the sanctioned path
(``coordinator-doc-new``, the engine op, the owning session), which is the value kept. The engine
discovers modules by directory scan, so a write guard has no wiring to unpick; a body whose message
names no path is deleted with its tests and pins in the delete pass.
"""

from __future__ import annotations

from coordinator_core.guard_census import GUIDE, Row, enumerate_write_guards

ROWS = [Row(name, GUIDE) for name in enumerate_write_guards()]
