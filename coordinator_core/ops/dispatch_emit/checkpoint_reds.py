"""``dispatch.checkpoint_reds``: classify a checkpoint's red tests and mint fix rows for the introduced ones.

Params: ``{"run_id": str, "failing": [str], "base_red": [str]}``.
Reply: ``{ok, classified: [{id, class}], fix_ids, fix_paths, commit_block}``. ``class`` is
``red_at_base`` for an id in ``base_red`` and ``introduced`` otherwise. Each introduced id gets one
``state/bug-backlog/<date>-introduced-red-<slug>-<id12>.yaml`` row (``introduced_red: true``,
``status: open``); an id that already has an open row reuses it. ``fix_paths`` are the repo-relative
rows the caller adds to its commit pathspec. ``open_introduced_fix_rows`` is the publish gate's one
listing. Stdlib plus lazy yaml; no git, no spawn.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit.bug_close import BUG_HOME

OP_NAME = "dispatch.checkpoint_reds"
RED_AT_BASE = "red_at_base"
INTRODUCED = "introduced"
_NAME_MARK = "-introduced-red-"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _norm(test_id: object) -> str:
    """Posix-normalised id, matching baseline_reds' `_baselineNorm` membership rule."""
    return str(test_id if test_id is not None else "").replace("\\", "/")


def _ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [_norm(v) for v in value if isinstance(v, str) and v]


def classify(failing: Iterable[str], base_red: Iterable[str]) -> list[dict]:
    """One ``{id, class}`` per distinct failing id, in first-seen order."""
    red = {_norm(b) for b in base_red}
    seen: dict[str, dict] = {}
    for raw in failing:
        tid = _norm(raw)
        if tid and tid not in seen:
            seen[tid] = {"id": tid, "class": RED_AT_BASE if tid in red else INTRODUCED}
    return list(seen.values())


def _row_id(run_id: str, test_id: str) -> str:
    return hashlib.sha1(f"{run_id}\0{test_id}".encode("utf-8")).hexdigest()[:12]


def _slug(test_id: str) -> str:
    return _SLUG_RE.sub("-", test_id.lower()).strip("-")[-40:].strip("-") or "test"


def open_introduced_fix_rows(repo_root) -> list[dict]:
    """Open introduced-red rows as ``[{id, path, test_id, run_id}]``, sorted by path.

    Only names carrying the mint marker are parsed; a row that does not parse or is not
    ``status: open`` with ``introduced_red: true`` is skipped.
    """
    import yaml

    root = Path(repo_root)
    try:
        with os.scandir(root / BUG_HOME) as it:
            names = sorted(e.name for e in it if _NAME_MARK in e.name and e.name.endswith(".yaml") and not e.is_dir())
    except OSError:
        return []
    rows = []
    for name in names:
        try:
            data = yaml.safe_load((root / BUG_HOME / name).read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or data.get("introduced_red") is not True or data.get("status") != "open":
            continue
        rows.append(
            {
                "id": name[: -len(".yaml")].rsplit("-", 1)[-1],
                "path": f"{BUG_HOME}/{name}",
                "test_id": str(data.get("test_id", "")),
                "run_id": str(data.get("run_id", "")),
            }
        )
    return rows


def mint_fix_rows(repo_root, run_id: str, introduced: Iterable[str]) -> list[dict]:
    """Write (or reuse) one open fix row per introduced test id; ``[{id, path, test_id}]`` in input order."""
    import yaml

    root = Path(repo_root)
    existing = {r["test_id"]: r for r in open_introduced_fix_rows(root)}
    out: list[dict] = []
    for raw in introduced:
        tid = _norm(raw)
        if not tid or any(o["test_id"] == tid for o in out):
            continue
        hit = existing.get(tid)
        if hit is not None:
            out.append({"id": hit["id"], "path": hit["path"], "test_id": tid})
            continue
        row_id = _row_id(run_id, tid)
        today = datetime.date.today().isoformat()
        rel = f"{BUG_HOME}/{today}{_NAME_MARK}{_slug(tid)}-{row_id}.yaml"
        data = {
            "created": today,
            "title": f"introduced red: {tid}",
            "body": (
                f"Test {tid} failed at a checkpoint or halt commit of run {run_id} and is not red at the run base. "
                "Fix it or prove it a base red; the publish gate refuses while this row is open."
            ),
            "status": "open",
            "surface": tid,
            "severity": "P1",
            "introduced_red": True,
            "test_id": tid,
            "run_id": run_id,
        }
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
        out.append({"id": row_id, "path": rel, "test_id": tid})
    return out


def commit_lines(classified: list[dict], fix_ids: list[str]) -> str:
    """Commit-message block: one ``red: <id> (<class>)`` line per red, then the fix rows; empty when no reds."""
    if not classified:
        return ""
    lines = ["Reds at this commit:"]
    lines += [f"  red: {c['id']} ({c['class']})" for c in classified]
    if fix_ids:
        lines.append("Fix rows: " + ", ".join(fix_ids))
    return "\n".join(lines)


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.checkpoint_reds": classify reds, mint fix rows for introduced ones, render the commit block."""
    if repo_root is None:
        return {"ok": False, "error": f"{OP_NAME} requires repo_root"}
    run_id = params.get("run_id") if isinstance(params, dict) else None
    if not isinstance(run_id, str) or not run_id:
        return {"ok": False, "error": 'run_id must be a non-empty string; pass {"run_id": "<id>", "failing": [...], "base_red": [...]}'}
    classified = classify(_ids(params.get("failing")), _ids(params.get("base_red")))
    try:
        rows = mint_fix_rows(repo_root, run_id, [c["id"] for c in classified if c["class"] == INTRODUCED])
    except Exception as exc:  # noqa: BLE001 -- a mint fault must not block the commit; surfaced in the reply
        return {"ok": False, "error": f"fix-row mint failed: {exc!r}", "classified": classified}
    fix_ids = [r["id"] for r in rows]
    return {
        "ok": True,
        "classified": classified,
        "fix_ids": fix_ids,
        "fix_paths": [r["path"] for r in rows],
        "commit_block": commit_lines(classified, fix_ids),
    }
