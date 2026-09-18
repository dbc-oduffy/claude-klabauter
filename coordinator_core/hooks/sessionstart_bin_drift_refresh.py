"""coordinator_core.hooks.sessionstart_bin_drift_refresh — SessionStart
(startup-only) op: keeps `<settings-home>/bin/` from silently lagging its
`templates/bin/` source.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude
`coordinator/hooks/scripts/sessionstart-bin-drift-refresh.py`. The mechanics
(refresh-only, the baked-`__PYTHON_BIN__` exemption, the once-a-day cadence)
already live in `coordinator_core.hooks.support.bin_impl_drift.check_and_refresh`
(W4-C4) — this module is the entrypoint and nothing else, matching the source
script's own "this file is the SessionStart entrypoint and nothing else"
contract.

Op contract: `params` is unused. Calls `check_and_refresh(settings_home() /
"bin")` directly (no sibling-checkout resolution needed — both the caller and
`bin_impl_drift` are same-repo modules). Returns `context_only("SessionStart",
banner)` when a refresh banner is produced, `no_advisory()` otherwise or on
any failure. Fails open, always.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

from coordinator_core._settings_home import settings_home
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.support.bin_impl_drift import check_and_refresh
from coordinator_core.ipc import register_op


@register_op("hooks.sessionstart_bin_drift_refresh")
async def _handler(params: dict, repo_root=None) -> dict:
    try:
        banner = check_and_refresh(settings_home() / "bin")
    except Exception:
        return no_advisory()
    if banner:
        return context_only("SessionStart", banner)
    return no_advisory()
