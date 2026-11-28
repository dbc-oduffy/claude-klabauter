"""coordinator_core.install.pruner_runner -- one scheduled pruning tick across the registered repos.

Run as ``python -m coordinator_core.install.pruner_runner`` (cwd = the install's repo root) by the
OS job ``pruner_scheduler`` registers. Per registered repo it calls ``run_hygiene`` (apply) and
``prune_emitted_output`` in-process: no per-repo interpreter or git spawn from this module, each
repo bounded by ``DEFAULT_BUDGET_SECS`` of hygiene work (the cursor resumes on the next tick).

Negative-spec: never raises out of ``run_once`` -- one repo's failure is recorded and the next
repo still runs; defines no pruning policy of its own.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coordinator_core.atomic_append import append_line
from coordinator_core.ops.fleet.scratch_hygiene import DEFAULT_BUDGET_SECS, run_hygiene

# `distill` is the cadence that applies the purge without the scratch-hold nag.
HYGIENE_CADENCE = "distill"
# One rotation generation (`pruner.log.1`) bounds the log; stdout/stderr append to the same file.
MAX_LOG_BYTES = 1_000_000


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
        out["emitted_deleted"] = [{"path": p, "bytes": None} for p in res["pruned"]]
    except Exception as exc:  # noqa: BLE001
        out["emitted_pruned"] = f"{type(exc).__name__}: {exc}"
    return out


def pruner_log_path() -> Path:
    """The one pruner log under the settings home; the scheduler points launchd/systemd stdout
    and stderr at the same file."""
    from coordinator_core._settings_home import settings_home

    return settings_home() / "logs" / "pruner.log"


def log_deletions(results: list[dict[str, Any]], log_path: Path | None = None) -> None:
    """Append one JSON line per deleted path (``ts``, ``repo``, ``path``, ``bytes``) to the pruner
    log. Never raises: an unwritable log must not stop a prune tick."""
    try:
        lines = [
            json.dumps(
                {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "repo": res["repo"],
                 "path": rec["path"], "bytes": rec.get("bytes")}
            )
            for res in results
            for rec in (*res.get("deleted", ()), *res.get("emitted_deleted", ()))
        ]
        if not lines:
            return
        path = Path(log_path) if log_path is not None else pruner_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        append_line(path, ("\n".join(lines) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001
        pass


def run_once(roots: list[Path] | None = None, log_path: Path | None = None) -> list[dict[str, Any]]:
    """Tick every repo in ``roots`` (default: the registered repos); one result dict per repo.
    Every deleted path is appended to the pruner log (``log_path``, default ``pruner_log_path()``)."""
    try:
        targets = registered_repo_roots() if roots is None else roots
    except Exception:  # noqa: BLE001 -- an unreadable registry means nothing to prune this tick
        return []
    results = []
    for r in targets:
        if not Path(r).is_dir():
            continue
        res = prune_repo(Path(r))
        log_deletions([res], log_path)  # per repo: a kill mid-tick still leaves the record
        results.append(res)
    return results


def main() -> int:
    for line in run_once():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
