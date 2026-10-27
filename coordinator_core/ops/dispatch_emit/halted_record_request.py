"""Halted-record request and manifest reads: the call predicate, the finished-chunk set, the run
request an emitted script promises, and the run manifest's baseline, ``kept_from`` and coded rows.
Lowest layer of the halted-record split: imports no sibling ``halted_record_*`` module."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit import run_artifacts
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PLAN_FIELD,
    HALT_REASON_FIELDS,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
)


def is_halted_record_call(params: object) -> bool:
    """True when ``params[HALTED_RECORD_PARAM]`` is a dict with a non-empty str ``halted``."""
    halted = params.get(HALTED_RECORD_PARAM) if isinstance(params, dict) else None
    return isinstance(halted, dict) and isinstance(halted.get("halted"), str) and bool(halted["halted"])


def within_handoffs_home(worktree_root: Path, artifacts: run_artifacts.RunArtifacts) -> run_artifacts.RunArtifacts:
    """``artifacts`` without any ``baton`` candidate outside the handoffs home: a sizing's
    ``baton:`` naming another directory is never landed."""
    from coordinator_core.session.record_homes import home_dir

    home = [Path(home_dir(str(worktree_root), "handoffs"))]
    kept = tuple(
        p for p in artifacts.by_class.get("baton", ()) if contained_path(worktree_root / p, home) is not None
    )
    if kept == artifacts.by_class.get("baton", ()):
        return artifacts
    return run_artifacts.RunArtifacts(
        run_id=artifacts.run_id,
        by_class={**artifacts.by_class, "baton": kept},
        record_dirs=artifacts.record_dirs,
        root=artifacts.root,
    )


def _run_request(worktree_root: Path, script_path: object):
    """The commit request an emitted script (or its ask-run manifest) promises, plus the ids the
    manifest withheld: ``(request, withheld_ids)``. ``(None, set())`` when nothing is readable."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, StageManifest
    from coordinator_core.ops.dispatch_emit.commit_request import (
        MalformedCommitRequestError,
        parse_manifest_marker,
        parse_marker,
    )

    if not isinstance(script_path, str) or not script_path:
        return None, set()
    script = contained_path(worktree_root / script_path, [worktree_root])
    if script is None:
        return None, set()
    try:
        text = script.read_text(encoding="utf-8")
        manifest_rel = parse_manifest_marker(text)
        if manifest_rel is None:
            return parse_marker(text), set()
        manifest_abs = contained_path(
            worktree_root / manifest_rel, [worktree_root / RECORD_DIR_ROOT, worktree_root / RUN_DIR_ROOT]
        )
        if manifest_abs is None:
            return None, set()
        manifest = StageManifest.from_json(json.loads(manifest_abs.read_text(encoding="utf-8")))
        marker = contained_path(worktree_root / manifest.marker_path, [worktree_root / RUN_DIR_ROOT])
        if marker is None:
            return None, set()
        withheld = {g.id for g in manifest.gated} | {o.id for o in manifest.owed}
        return parse_marker(marker.read_text(encoding="utf-8")), withheld
    except (OSError, ValueError, KeyError, TypeError, MalformedCommitRequestError):
        return None, set()


def done_chunks(worktree_root: Path, params: dict) -> list:
    """The chunks a halted run finished. With ``params.incomplete_chunks`` the rest are done, as in
    the DAG mode; without it a chunk is done only when its own report reads DONE and not
    PARTIAL/BLOCKED. A manifest-withheld chunk and an unreadable request are never done."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _read_rel
    from coordinator_core.ops.dispatch_emit.terminal_commit_ledger import _DELIVERED_REPORT_RE, _UNDELIVERED_REPORT_RE

    request, withheld = _run_request(worktree_root, params.get("script_path"))
    if request is None:
        return []
    listed = params.get("incomplete_chunks")
    incomplete = set(listed) if isinstance(listed, list) else None
    out: list = []
    for chunk in request.chunks:
        if chunk.id in withheld or (incomplete is not None and chunk.id in incomplete):
            continue
        if incomplete is None:
            text = _read_rel(worktree_root, chunk.report) if chunk.report else None
            if text is None or not _DELIVERED_REPORT_RE.search(text) or _UNDELIVERED_REPORT_RE.search(text):
                continue
        out.append(chunk)
    return out


def _held_reason(halted: dict) -> Optional[str]:
    """The halt's first non-empty reason field, whitespace collapsed; None when it carries none."""
    for field in HALT_REASON_FIELDS:
        text = " ".join(str(halted.get(field) or "").split())
        if text:
            return text
    return None


def _manifest_doc(record_dir: Path) -> dict:
    """The run manifest as a dict; empty when absent or unreadable."""
    import json

    try:
        data = json.loads((record_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_baseline(record_dir: Path, doc: Optional[dict] = None) -> Optional[dict]:
    """The run manifest's ``write_baseline`` (read from ``doc`` when given); None when the manifest or the key is absent or unreadable."""
    base = (_manifest_doc(record_dir) if doc is None else doc).get("write_baseline")
    return base if isinstance(base, dict) and base else None


def _coded_rows(worktree_root: Path, halted: dict) -> tuple:
    """``(row ids the halt's plan marks coded, error)``. The set is empty when the plan field is
    absent (error None) or the plan cannot be read or decoded (error names it): an unreadable plan
    credits no row, and the caller surfaces that."""
    from coordinator_core.ops.dispatch_emit import delivery_credit

    plan = halted.get(HALT_PLAN_FIELD)
    if not isinstance(plan, str) or not plan:
        return set(), None
    try:
        by_ref = delivery_credit.coded_row_refs(str(worktree_root / plan))
    except (OSError, ValueError) as exc:
        return set(), f"{plan}: {exc}"
    return {i for ids in by_ref.values() for i in ids}, None


def _manifest_kept_from(record_dir: Path) -> dict:
    """``{row id: prior run id}`` for each row the run manifest stages ``kept_from``; empty when unreadable."""
    import json

    try:
        data = json.loads((record_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = data.get("rows") if isinstance(data, dict) else None
    return {
        r["id"]: r["kept_from"]
        for r in rows or ()
        if isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("kept_from"), str) and r["kept_from"]
    }
