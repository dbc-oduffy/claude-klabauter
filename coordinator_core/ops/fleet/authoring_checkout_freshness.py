"""coordinator_core.ops.fleet.authoring_checkout_freshness — `fleet.authoring_checkout_freshness`
op and the `run` entry the SessionStart async leg calls.

One call reports, per registered authoring checkout, its lag behind `origin/main` and
fast-forwards a behind, clean, no-live-run day branch. Every unsafe state is an advisory
row, never an error. Mutates checkouts, so it is not COMPUTE_ONLY.
"""

from __future__ import annotations

import socket

from coordinator_core.ipc import register_op


def run(do_fetch: bool = False) -> dict:
    """Rows and advisories for every authoring checkout in the machine registry.

    An unreadable registry yields no rows and one advisory naming the registry; nothing
    is probed and nothing raises.
    """
    from coordinator_core.install.authoring_checkout_freshness_apply import apply_all
    from coordinator_core.ops.fleet._memo_resolver import RegistryReadError, read_registry_repos

    try:
        registry = read_registry_repos()
    except RegistryReadError as exc:
        reason = f"machine registry unreadable: {exc.reason}"
        hostname = socket.gethostname()
        return {
            "hostname": hostname,
            "rows": [],
            "advisories": [f"[authoring-checkout-freshness] {hostname}: {reason}"],
            "reason": reason,
        }
    return apply_all(registry, do_fetch=do_fetch)


@register_op("fleet.authoring_checkout_freshness")
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "fleet.authoring_checkout_freshness" handler.

    Params:
        fetch (bool, optional): run `git fetch origin main` per checkout first. Default False.

    Returns {hostname, rows, advisories}: each row carries key, root, branch, head_sha,
    behind, ahead, action, reason, moved and, when set, fetch_error, merge_clean,
    conflict_paths, remediation.
    """
    fetch = (params or {}).get("fetch", False)
    if not isinstance(fetch, bool):
        raise ValueError("fetch must be a boolean")
    return run(do_fetch=fetch)
