"""The four cockpit columns (``status``, ``deployment_state``, ``predecessor``,
``shipped_in``), callable without an ``EmitContext`` or an emit envelope.

Extracted out of ``sections/handoffs.py`` (C1, plan
``docs/plans/2026-08-11-pull-surface-for-cockpit-the-four-columns-and-the-archive.md``) so a
query-side caller (the C3 ``handoff.columns`` op) can compute the same four values ``handoffs.py``
emits, without constructing an envelope. This is a PURE MOVE — the moved logic is unchanged, only
the ``EmitContext`` argument some of it took is narrowed to the one thing it actually needed (a
repo-root path).

Only two of the four columns are genuinely computed here: ``deployment_state`` (old-vocabulary
coercion via ``_coerce_legacy_abandoned``) and ``shipped_in`` ({sha, date} git enrichment via
``_resolve_shipped_in_dates``). ``status`` is a raw frontmatter passthrough and ``predecessor``'s
only "computation" is its ``"none"`` default when the field is absent — see the plan's Problem
section for the correction this module's docstring exists to not overstate again.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

# DR-084 P4 transitional ingest tolerance (see sections/handoffs.py's module docstring for the
# full history and named exit condition) — the union of old-and-new legal `deployment_state`
# values. A value outside this union is neither, and is per-record quarantined by the caller.
_DEPLOYMENT_RECOGNIZED = {
    "in_flight", "shipped", "awaiting_gate", "ready_to_fire", "abandoned",
    "continued", "closed",
}

# `predecessor`'s only computation: the default projected when frontmatter omits the field.
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
    """Resolve distinct raw ``shipped_in`` SHAs to commit dates via ONE git log.

    ``git log --no-walk=unsorted --ignore-missing --format='%H %ad' --date=format:%Y-%m-%d``
    over the SHA batch; unresolvable SHAs are silently dropped (--ignore-missing → exit 0).
    Each output ``%H`` is prefix-matched back to the first unmatched raw SHA (shipped_in.sha
    must be the raw frontmatter value, not the 40-char expansion). Offline / git failure →
    empty map (all shipped_in resolve to null; caller never aborts).
    """
    if not raw_shas:
        return {}
    # jq `unique` sorts ascending — replicate so the prefix-match tiebreak order matches bash.
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
    """Mirror jq ``//`` — return ``default`` when ``value`` is null or false, else ``value``.

    Local copy of ``sections/handoffs.py``'s helper of the same name — kept private and
    duplicated (not imported) so this module has no dependency edge onto ``handoffs.py``, per the
    plan's anti-scope ("the arrow points from both callers into it, never between them").
    """
    if value is None or value is False:
        return default
    return value


def _compute_non_git_columns(fm: dict) -> tuple[Any, Any, Any, Optional[str]]:
    """Compute the three non-git columns plus the raw (unresolved) ``shipped_in`` SHA.

    Shared innards of both ``compute_handoff_columns`` (single-record) and
    ``compute_handoff_columns_batch`` (many-record, one ``git log``) — factored out so the
    ``status``/``deployment_state``/``predecessor`` derivation and the ``shipped_in`` raw-value
    extraction live in exactly one place, and only the git-resolution step (single-SHA vs.
    batched) differs between the two callers.

    Returns ``(status, deployment_state, predecessor, shipped_sha_raw)`` — ``shipped_sha_raw`` is
    ``None`` when the record has no ``shipped_in`` value.
    """
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
    """Batch-shaped sibling of ``compute_handoff_columns`` — ONE ``git log`` for N records.

    Added for the C3 ``handoff.columns`` query op (2026-08-11 pull-surface-four-columns plan),
    whose corpus is 133 live plus 284 archived handoffs in this repo alone — calling
    ``compute_handoff_columns`` in a loop would spawn one ``git log`` subprocess per record
    (400+ spawns), a serious regression on a machine running 50-70 concurrent LLM sessions as
    normal load (see this repo's CLAUDE.md § Load norm) and precisely the read-path cost DR-287
    halted the emit cadence over.

    Collects every record's raw ``shipped_in`` SHA first, resolves the whole batch via ONE
    ``_resolve_shipped_in_dates(repo_root, all_shas)`` call, then joins the resolved dates back
    per record. Returns one ``{"status", "deployment_state", "predecessor", "shipped_in"}`` dict
    per input frontmatter, same order as ``frontmatters``, same per-record shape as
    ``compute_handoff_columns``'s return value.
    """
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
