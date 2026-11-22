"""coordinator_core.install.pruner_runner -- one scheduled pruning tick across the registered repos.

Run as ``python -m coordinator_core.install.pruner_runner`` (cwd = the install's repo root) by the
OS job ``pruner_scheduler`` registers. Per registered repo it calls ``run_hygiene`` (apply) and
``prune_emitted_output`` in-process: no per-repo interpreter or git spawn from this module, each
repo bounded by ``DEFAULT_BUDGET_SECS`` of hygiene work (the cursor resumes on the next tick).

Negative-spec: never raises out of ``run_once`` -- one repo's failure is recorded and the next
repo still runs; defines no pruning policy of its own.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from coordinator_core.ops.fleet.scratch_hygiene import DEFAULT_BUDGET_SECS, run_hygiene

# `distill` is the cadence that applies the purge without the scratch-hold nag.
HYGIENE_CADENCE = "distill"


def registered_repo_roots() -> list[Path]:
    """The machine-local `repos.*` checkouts that exist on this box."""
    from coordinator_core.install.rag_fleet_registration import fleet_repo_roots

    return list(fleet_repo_roots().values())


def prune_repo(root: Path) -> dict[str, Any]:
    """One repo's tick: scratch hygiene (apply) then emitted-output pruning. Each leg is
    isolated, so a failed hygiene pass does not skip the emitted-output prune."""
    out: dict[str, Any] = {"repo": str(root)}
    try:
        res = run_hygiene(
            {
                "repo_root": str(root),
                "cadence": HYGIENE_CADENCE,
                "apply": True,
                "budget_secs": DEFAULT_BUDGET_SECS,
            }
        )
        out["hygiene"] = res.get("error") or res.get("walk", {})
        out["deleted"] = [
            {"path": r["path"], "bytes": r.get("bytes", 0)}
            for r in res.get("records", [])
            if r.get("kind") == "purge" and r.get("action") == "deleted" and r.get("reason") != "already-gone"
        ]
    except Exception as exc:  # noqa: BLE001 -- a repo's failure never stops the tick
        out["hygiene"] = f"{type(exc).__name__}: {exc}"
    try:
        from coordinator_core.lifecycle import git_common_dir
        from coordinator_core.ops.fleet.prune_emitted import prune_emitted_output

        res = prune_emitted_output(root, git_common_dir(root), dry_run=False)
        out["emitted_pruned"] = len(res["pruned"])
    except Exception as exc:  # noqa: BLE001
        out["emitted_pruned"] = f"{type(exc).__name__}: {exc}"
    return out


def run_once(roots: list[Path] | None = None) -> list[dict[str, Any]]:
    """Tick every repo in ``roots`` (default: the registered repos); one result dict per repo."""
    try:
        targets = registered_repo_roots() if roots is None else roots
    except Exception:  # noqa: BLE001 -- an unreadable registry means nothing to prune this tick
        return []
    return [prune_repo(Path(r)) for r in targets if Path(r).is_dir()]


def main() -> int:
    for line in run_once():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
