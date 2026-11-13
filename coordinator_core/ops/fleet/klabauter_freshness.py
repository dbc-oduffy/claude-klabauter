"""coordinator_core.ops.fleet.klabauter_freshness — `fleet.klabauter_freshness` op and the
shared `run_freshness` entry the SessionStart async leg and the G-EM watch tick both call.

One call fetches, compares the klabauter checkout to its upstream, fast-forwards only a
behind-clean-no-live-run tree, re-installs stale copies and surfaces a dirty or diverged
tree once. Mutates the checkout and installed copies, so it is not COMPUTE_ONLY.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

SURFACED_ADVISORY = (
    "[klabauter-freshness] {hostname}: checkout {state} (local {local} vs origin {origin}); "
    "not auto-updated."
)


def resolve_checkout() -> tuple[Optional[Path], str]:
    """The box's klabauter clone and the remediation to quote when there is none.

    Never falls back to this module's own tree: that is the claude-klabauter checkout, not klabauter.
    """
    from coordinator_core.install.engine_root_for_install import resolve_engine_root_for_install

    resolved = resolve_engine_root_for_install()
    if resolved.kind == "none" or resolved.root is None:
        return None, resolved.remediation or ""
    return Path(resolved.root), ""


def run_freshness(do_fetch: bool = True, checkout: Optional[Path] = None) -> dict:
    """Run one freshness pass; returns `FreshnessResult.to_dict()`.

    With no `checkout` and no resolvable klabauter clone, returns a `current` result whose
    `reason` names `repos.claude_klabauter` and carries the remediation; nothing is probed.
    """
    from coordinator_core._settings_home import settings_home
    from coordinator_core.engine_root import coordinator_engine_root
    from coordinator_core.install.klabauter_freshness_apply import (
        FreshnessResult,
        apply_freshness,
    )

    if checkout is None:
        checkout, remediation = resolve_checkout()
        if checkout is None:
            return FreshnessResult(
                hostname=socket.gethostname(),
                reason=f"repos.claude_klabauter is unresolved: {remediation}",
            ).to_dict()

    try:
        engine = Path(coordinator_engine_root())
    except RuntimeError:
        engine = None
    result = apply_freshness(
        checkout,
        settings_home(),
        do_fetch=do_fetch,
        engine_root=engine,
    )
    return result.to_dict()


def surfaced_advisory(result: dict) -> Optional[str]:
    """One-line advisory when this pass newly surfaced a dirty or diverged tree, else None."""
    if not result.get("surfaced"):
        return None
    state = str(result.get("action", "")).replace("surface_", "")
    return SURFACED_ADVISORY.format(
        hostname=result.get("hostname", ""),
        state=state,
        local=str(result.get("local_sha", ""))[:10],
        origin=str(result.get("origin_sha", ""))[:10],
    )


@register_op("fleet.klabauter_freshness")
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "fleet.klabauter_freshness" handler.

    Params:
        fetch (bool, optional): run `git fetch` first. Default True.

    Returns the `FreshnessResult` dict: action, reason, hostname, local_sha, origin_sha,
    moved, changed_paths, installers_run, installer_errors, stale_installs, surfaced,
    fetch_error.
    """
    fetch = (params or {}).get("fetch", True)
    if not isinstance(fetch, bool):
        raise ValueError("fetch must be a boolean")
    return run_freshness(do_fetch=fetch)
