"""coordinator_core.ops.fleet.klabauter_freshness — `fleet.klabauter_freshness` op and the
shared `run_freshness` entry the SessionStart async leg and the G-EM watch tick both call.

One call fetches, compares the klabauter checkout to its upstream, fast-forwards only a
behind-clean-no-live-run tree, re-installs stale copies and surfaces a dirty or diverged
tree once. Mutates the checkout and installed copies, so it is not COMPUTE_ONLY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

SURFACED_ADVISORY = (
    "[klabauter-freshness] {hostname}: checkout {state} (local {local} vs origin {origin}); "
    "not auto-updated."
)


def resolve_checkout() -> Path:
    """The klabauter checkout: the registered engine source tree, else this module's repo."""
    from coordinator_core.engine_root import engine_source_root

    source = engine_source_root()
    return Path(source) if source else Path(__file__).resolve().parents[3]


def run_freshness(do_fetch: bool = True, checkout: Optional[Path] = None) -> dict:
    """Run one freshness pass; returns `FreshnessResult.to_dict()`."""
    from coordinator_core._settings_home import settings_home
    from coordinator_core.engine_root import coordinator_engine_root
    from coordinator_core.install.klabauter_freshness_apply import apply_freshness

    try:
        engine = Path(coordinator_engine_root())
    except RuntimeError:
        engine = None
    result = apply_freshness(
        checkout or resolve_checkout(),
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
