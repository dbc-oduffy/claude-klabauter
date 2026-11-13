"""coordinator_core.post_run.p5_audit -- the distill post-shard audit run before `post_run.finish` commits.

`run(result, porcelain)` reads the distill run result JSON plus the `git status --porcelain` lines
the commit step already read (no git spawn here). It reports the shard's counts, lists the
unexplained tree changes, and writes two evidence files for the scoped commit: the sorted DELETE
`.md` list and one jsonl row per distillation_log_row (path, disposition, fate, run_id).

Unexplained = a porcelain path that is not a promoted file, not under `docs/decisions/`, and not
the artifact of a DELETE-disposition entry in the phase-3d deletion manifest.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from coordinator_core.session.record_homes import home_rel

__all__ = ["EVIDENCE_DIR", "LOG_PATTERN", "run"]

EVIDENCE_DIR = home_rel("post-run")
DECISIONS_PREFIX = "docs/decisions/"
LOG_PATTERN = re.compile(r"source-hash|promote|provenance|phase-3d|re-entry|BLOCKED|halt|degrad")
_LOG_CLIP = 220
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _porcelain_path(line: str) -> str:
    """Path of a porcelain line: the new side of a rename, unquoted."""
    path = line[3:] if len(line) > 3 else ""
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    path = path.strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1]
    return path.replace("\\", "/")


def _promoted_paths(promoted: list) -> set[str]:
    out: set[str] = set()
    for item in promoted:
        raw = item if isinstance(item, str) else _as_dict(item).get("target") or _as_dict(item).get("path")
        if isinstance(raw, str) and raw:
            out.add(raw.replace("\\", "/"))
    return out


def _scalars(mapping: dict) -> dict:
    return {k: v for k, v in mapping.items() if not isinstance(v, (list, dict))}


def _coverage(mapping: dict) -> dict:
    return {k: (len(v) if isinstance(v, list) else v) for k, v in mapping.items()}


def _evidence_rel(run_id: str, name: str) -> str:
    safe = re.sub(r"\.{2,}", ".", _SAFE.sub("-", run_id)).strip("-.")
    return f"{EVIDENCE_DIR}/{safe or 'run'}/{name}"


def run(
    result: dict,
    porcelain: list[str],
    *,
    repo_root: Optional[Path] = None,
) -> dict:
    """Audit one distill run result against the tree's porcelain lines.

    `result` is the run result JSON, bare or wrapped as `{"result": {...}, "logs": [...]}`.
    Returns a dict: `unexplained` (sorted porcelain paths nothing accounts for), `evidence`
    (repo-relative paths written under `state/post-run/<run_id>/`; empty when `result` carries no
    distillation_log_rows and no DELETE entries), `report` (counts and matched log lines).
    Evidence files are written under `repo_root` (default: the current directory); pass the
    originating worktree root.
    """
    wrapper = _as_dict(result)
    body = _as_dict(wrapper.get("result")) if isinstance(wrapper.get("result"), dict) else wrapper
    logs = [l for l in _as_list(wrapper.get("logs")) + _as_list(body.get("logs")) if isinstance(l, str)]

    promoted = _as_list(body.get("promoted_files"))
    log_rows = [r for r in _as_list(body.get("distillation_log_rows")) if isinstance(r, dict)]
    deletions = [d for d in _as_list(_as_dict(body.get("phase_3d_deletion_manifest")).get("deletions"))
                 if isinstance(d, dict)]
    provenance = _as_dict(body.get("provenance_completeness"))
    contradiction = _as_dict(body.get("contradiction_detection"))
    opus = _as_dict(body.get("opus_escalation"))
    run_id = str(body.get("run_id") or wrapper.get("run_id") or "")

    promoted_set = _promoted_paths(promoted)
    delete_paths = {str(d["artifact_path"]).replace("\\", "/") for d in deletions
                    if d.get("disposition") == "DELETE" and d.get("artifact_path")}

    unexplained = sorted({
        p for p in (_porcelain_path(line) for line in porcelain)
        if p and p not in promoted_set and not p.startswith(DECISIONS_PREFIX) and p not in delete_paths
    })

    delete_md = sorted(p for p in delete_paths if p.endswith(".md"))
    report = {
        "promoted_files": len(promoted),
        "distillation_log_rows": len(log_rows),
        "deletion_dispositions": dict(Counter(d.get("disposition") for d in deletions)),
        "provenance": {
            "complete": provenance.get("complete"),
            "files": provenance.get("files"),
            "missing_fields": len(_as_list(provenance.get("missing_fields"))),
        },
        "dr_minting": _scalars(_as_dict(body.get("dr_minting"))),
        "total_unresolvable_contradictions": contradiction.get("total_unresolvable_contradictions"),
        "opus_resolution": opus.get("resolution"),
        "coverage": _coverage(_as_dict(body.get("coverage"))),
        "log_lines": [l[:_LOG_CLIP] for l in logs if LOG_PATTERN.search(l)],
        "delete_md": len(delete_md),
        "delete_non_md": len(delete_paths) - len(delete_md),
        "unexplained": len(unexplained),
    }

    evidence: list[str] = []
    if log_rows or delete_paths:
        root = Path(repo_root) if repo_root is not None else Path.cwd()
        del_rel = _evidence_rel(run_id, "p5-delete-md.txt")
        rows_rel = _evidence_rel(run_id, "p5-distillation-log-rows.jsonl")
        (root / del_rel).parent.mkdir(parents=True, exist_ok=True)
        (root / del_rel).write_text("".join(p + "\n" for p in delete_md), encoding="utf-8", newline="\n")
        (root / rows_rel).write_text(
            "".join(
                json.dumps({"path": r.get("path"), "disposition": r.get("disposition"),
                            "fate": r.get("fate"), "run_id": r.get("run_id", run_id)}) + "\n"
                for r in log_rows),
            encoding="utf-8", newline="\n")
        evidence = [del_rel, rows_rel]

    return {"unexplained": unexplained, "evidence": evidence, "report": report}
