
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

_DEPLOYMENT_RECOGNIZED = {
    "in_flight", "shipped", "awaiting_gate", "ready_to_fire", "abandoned",
    "continued", "closed", "record",
}

PREDECESSOR_DEFAULT = "none"


def _coerce_legacy_abandoned(fm: dict) -> tuple[str, Optional[str], Optional[str]]:
    """Split a legacy ``deployment_state: abandoned`` record into the new terminal it earns.

    ``abandoned`` collapsed two epistemically distinct cases (contract ``DeploymentState``
    docstring): a dead-holder node WITH a successor, and a deliberate stop WITHOUT one. Claude-klabauter's
    own live corpus was already migrated at C5+C8 (``e2cf1a08`` — zero ``abandoned`` records
    remain under this repo's ``state/handoffs/``, ``archive/handoffs/``, or
    ``state/handoffs/.archive/``), but this path is NOT legacy tolerance for rare stragglers —
    consumer repos this section also ingests from (example-retrieval-repo, example-cockpit-repo) carry
    un-migrated ``abandoned`` records as their normal corpus state (verified 2026-07-23), so this
    is a hot path there until the exit condition in ``sections/handoffs.py``'s module docstring
    is met.

    A ``continued`` verdict REQUIRES a positive successor proof — never guesses at succession via
    a cross-record join. It only honors a successor the record ITSELF already names: a
    ``continued_into`` value already present in this record's own frontmatter (the same field
    C5's writer cutover stamps on positive succession proof). Any other legacy ``abandoned``
    record — the overwhelming majority, with no successor reference of its own — maps to
    ``closed`` + ``closed_reason: stale``, the same mapping C8's mechanical archive migration
    used for successor-less records (DoE-blessed, plan § C8).

    Returns ``(deployment_state, continued_into, closed_reason)``.
    """
    continued_into = fm.get("continued_into")
    if isinstance(continued_into, str) and continued_into:
        return "continued", continued_into, None
    return "closed", None, "stale"


def _resolve_shipped_in_dates(repo_root: Path, raw_shas: list[str]) -> dict[str, str]:
    if not raw_shas:
        return {}
    ordered = sorted(set(raw_shas))
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            [
                "git", "-C", str(repo_root), "log",
                "--no-walk=unsorted", "--ignore-missing",
                "--format=%H %ad", "--date=format:%Y-%m-%d",
                *ordered,
            ],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, ValueError):
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}

    sha_date: dict[str, str] = {}
    matched: set[str] = set()
    for line in proc.stdout.replace("\r", "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        full, date = parts[0], parts[1]
        for raw in ordered:
            if raw not in matched and full[: len(raw)] == raw:
                sha_date[raw] = date
                matched.add(raw)
                break
    return sha_date


def _jq_or(value: Any, default: Any) -> Any:
    if value is None or value is False:
        return default
    return value


def _compute_non_git_columns(fm: dict) -> tuple[Any, Any, Any, Optional[str]]:
    status = fm.get("status")

    deployment_state = fm.get("deployment_state")
    if deployment_state == "abandoned":
        deployment_state, _continued_into, _closed_reason = _coerce_legacy_abandoned(fm)

    predecessor = _jq_or(fm.get("predecessor"), PREDECESSOR_DEFAULT)

    raw_shipped = _jq_or(fm.get("shipped_in"), None)
    shipped_sha_raw = None if raw_shipped is None else str(raw_shipped)

    return status, deployment_state, predecessor, shipped_sha_raw


def compute_handoff_columns(fm: dict, repo_root: Path) -> dict:
    """Compute the four cockpit columns for ONE record's parsed frontmatter.

    Callable without an ``EmitContext`` or an emit envelope — ``repo_root`` is the only thing the
    ``shipped_in`` git resolution needs. Returns exactly
    ``{"status", "deployment_state", "predecessor", "shipped_in"}``.

    ``status`` and ``predecessor`` are raw frontmatter passthroughs (``predecessor`` defaulting to
    ``PREDECESSOR_DEFAULT`` when absent) — see the module docstring's correction on which columns
    are genuinely computed. ``deployment_state`` runs through ``_coerce_legacy_abandoned`` when
    the raw value is the legacy ``"abandoned"`` token. ``shipped_in`` is ``{sha, date}`` (or
    ``None``) via a single-SHA call into ``_resolve_shipped_in_dates`` — callers processing many
    records in one pass should call ``compute_handoff_columns_batch`` instead (each call here
    spawns its own ``git log``).
    """
    status, deployment_state, predecessor, shipped_sha_raw = _compute_non_git_columns(fm)

    shipped_in: Optional[dict]
    if shipped_sha_raw is None:
        shipped_in = None
    else:
        sha_dates = _resolve_shipped_in_dates(repo_root, [shipped_sha_raw])
        date = sha_dates.get(shipped_sha_raw)
        shipped_in = {"sha": shipped_sha_raw, "date": date} if isinstance(date, str) else None

    return {
        "status": status,
        "deployment_state": deployment_state,
        "predecessor": predecessor,
        "shipped_in": shipped_in,
    }


def compute_handoff_columns_batch(frontmatters: list[dict], repo_root: Path) -> list[dict]:
    parsed = [_compute_non_git_columns(fm) for fm in frontmatters]

    all_shas = sorted({s for (_, _, _, s) in parsed if s is not None})
    sha_dates = _resolve_shipped_in_dates(repo_root, all_shas)

    rows: list[dict] = []
    for status, deployment_state, predecessor, shipped_sha_raw in parsed:
        shipped_in: Optional[dict]
        if shipped_sha_raw is None:
            shipped_in = None
        else:
            date = sha_dates.get(shipped_sha_raw)
            shipped_in = {"sha": shipped_sha_raw, "date": date} if isinstance(date, str) else None

        rows.append({
            "status": status,
            "deployment_state": deployment_state,
            "predecessor": predecessor,
            "shipped_in": shipped_in,
        })
    return rows
