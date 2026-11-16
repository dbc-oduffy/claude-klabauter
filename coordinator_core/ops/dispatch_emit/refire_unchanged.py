"""Refusal for a criterion_not_met refire whose row briefs and footprints equal the halted fire's.

A row fingerprint is its sorted forward-slash `writes` plus the sha256 of its brief text with the
run id normalised. Reads the prior run's `manifest.json`, its briefs and its halt marker only; no git,
no subprocess. The new side arrives in memory because `ask_stage.stage` writes the new manifest after
the briefs leg.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_compose import HALT_CRITERION_NOT_MET

Fingerprint = tuple[tuple[str, ...], str]

_RUN_PLACEHOLDER = "<run>"


def fingerprint_row(writes: Sequence[str], brief_text: str, run_id: str) -> Fingerprint:
    """`(sorted forward-slash writes, sha256 of brief_text with every `run_id` replaced by "<run>")`."""
    norm = brief_text.replace(run_id, _RUN_PLACEHOLDER) if run_id else brief_text
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return tuple(sorted(str(w).replace("\\", "/") for w in writes)), digest


def _brief_text(candidates: Sequence[Path], brief_path: str) -> Optional[str]:
    for base in candidates:
        try:
            target = base / brief_path
            if target.is_file():
                return target.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
    return None


def _manifest(run_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def fingerprint(run_dir: Path, root: Optional[Path] = None) -> dict[str, Fingerprint]:
    """Row id -> fingerprint of the run's `manifest.json` rows and their briefs; {} when the manifest is
    absent or unreadable. A row whose brief cannot be read is omitted. `brief_path` is repo-relative:
    it resolves against `root`, else against each ancestor of `run_dir`."""
    run_dir = Path(run_dir)
    manifest = _manifest(run_dir)
    rows = manifest.get("rows") if manifest else None
    if not isinstance(rows, list):
        return {}
    bases = [Path(root)] if root is not None else []
    bases.extend(run_dir.resolve().parents)
    out: dict[str, Fingerprint] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        brief_path = row.get("brief_path")
        writes = row.get("writes")
        if not isinstance(brief_path, str) or not isinstance(writes, list):
            continue
        text = _brief_text(bases, brief_path)
        if text is None:
            continue
        out[row["id"]] = fingerprint_row([w for w in writes if isinstance(w, str)], text, run_dir.name)
    return out


def check(
    root: Path,
    prior_run_dir: Path,
    new_run_id: str,
    new_rows: Mapping[str, tuple[Sequence[str], str]],
    prior: Optional[Mapping[str, Fingerprint]] = None,
) -> Optional[str]:
    """The refusal text when every `new_rows` entry (row id -> (writes, brief text)) equals the prior
    run's fingerprint of that row and the prior halt is `criterion_not_met`; None otherwise (an added
    row, a changed row, an empty `new_rows`, another halt reason, an unreadable marker or manifest).

    `prior` is `fingerprint(prior_run_dir, root)` taken before the new briefs are written. A rebound
    refire restages under the halted run's own id, so its briefs overwrite the prior's on disk; read
    afterwards, the prior side equals the new side and every such refire refuses."""
    prior_run_dir = Path(prior_run_dir)
    if not new_rows:
        return None
    try:
        marker = halted_run.read_strict(prior_run_dir)
    except ValueError:
        return None
    if marker is None or marker.get("halted") != HALT_CRITERION_NOT_MET:
        return None
    if prior is None:
        prior = fingerprint(prior_run_dir, root)
    paths: list[str] = []
    for row_id, (writes, brief) in new_rows.items():
        if prior.get(row_id) != fingerprint_row(writes, brief, new_run_id):
            return None
        paths.append(_brief_rel(prior_run_dir, row_id))
    return (
        f"refire unchanged from halted run {prior_run_dir.name}: brief(s) {', '.join(paths)} and footprint "
        "equal on criterion_not_met; send workflow.revise with a note or add_paths naming the gap first."
    )


def _brief_rel(prior_run_dir: Path, row_id: str) -> str:
    """The prior manifest's `brief_path` for the row; the new run's brief shares its `briefs/<row>.md` tail."""
    for row in (_manifest(prior_run_dir) or {}).get("rows") or ():
        if isinstance(row, dict) and row.get("id") == row_id and isinstance(row.get("brief_path"), str):
            return row["brief_path"]
    return f"briefs/{row_id}.md"
