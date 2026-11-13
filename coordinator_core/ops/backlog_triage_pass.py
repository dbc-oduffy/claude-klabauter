"""coordinator_core.ops.backlog_triage_pass — JSON-RPC "backlog.triage_pass" operation.

Purpose: the model-free changelog-first pass over a repo's backlog. Walks every ISO week of
the source repo's changelogs (`backlog.changelog_walk`), resolves each flagged change's cited
artifacts through example-retrieval-repo's read contract (`backlog.rag_reads`, falling back to the
walker's own parse and recording a gap), opens only artifacts a flagged change cites,
reduces each to a compact record (`backlog.extract`), and clusters the records into one
digest per topic (`backlog.cluster`). Nothing is archived or deleted; fates are recorded only.

Writes, under the INVOKING repo's ``state/scratch/backlog-pass/<run-id>/`` and never under
``source_repo``:
  digests.json        — the per-topic digests a model reads, plus coverage counts.
  triage-ledger.json  — every walked week, every flagged change and its fate, rag gaps.

MUTATING scratch-tier writer, same class as distill.scope (DR-228 § D6): write-confined to its
own run directory, create-or-full-rewrite only, no delete, no commit, schema_version pinned.
``source_repo`` is read-only input.

Repo-root contract: ``repo_root`` is the injected git common dir (op scope "common_dir");
None fails loud, no silent meta-repo fallback.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from coordinator_core.backlog import rag_reads
from coordinator_core.backlog.changelog_walk import walk_changelogs
from coordinator_core.backlog.cluster import cluster_records
from coordinator_core.backlog.extract import extract_record
from coordinator_core.backlog.record_schema import ArtifactRecord
from coordinator_core.backlog.record_schema import FlaggedChange
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import claimed_write
from coordinator_core.wire_paths import rel_id

OUT_DIR_REL = "state/scratch/backlog-pass"
SCHEMA_VERSION = 1
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _under(root: Path, rel: str) -> Path | None:
    """The file `rel` names under `root`, or None when it escapes `root` or is not a file."""
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


def _gap(gaps: dict[str, dict[str, str]], found: tuple[dict[str, str], ...]) -> None:
    for g in found:
        gaps.setdefault(g["form"], g)


def _resolve_changes(entries, gaps: dict[str, dict[str, str]], loader):
    """Each week's changes with rag-resolved paths merged, plus the outlines rag served.

    The first read-contract fallback is recorded and ends further changelog_resolve calls: a
    form that is missing for one entry is missing for all."""
    outlines: dict[str, tuple[str, ...]] = {}
    served = True
    resolved: list[tuple[str, FlaggedChange]] = []
    for week in entries:
        for change in week.flagged:
            if served:
                read = rag_reads.changelog_resolve(change, loader=loader)
                if read.fallback:
                    served = False
                    _gap(gaps, read.gaps)
                else:
                    change = read.change or change
                    outlines.update(read.outlines or {})
            resolved.append((week.week, change))
    return resolved, outlines


_DOC_SUFFIXES = (".md", ".markdown", ".txt")


def run_pass(source_root: Path, loader=None) -> dict[str, Any]:
    """Compute the pass over `source_root`: {digests, ledger} as JSON-ready dicts. Pure of writes."""
    entries, coverage = walk_changelogs(source_root)
    gaps: dict[str, dict[str, str]] = {}
    resolved, outlines = _resolve_changes(entries, gaps, loader)

    cited = sorted({p for _, c in resolved for p in c.cited_paths})
    present = {p: _under(source_root, p) for p in cited}
    unserved = [p for p in cited if present[p] is not None and p not in outlines]
    if unserved:
        read = rag_reads.doc_outline(unserved, loader=loader)
        if read.fallback:
            _gap(gaps, read.gaps)
        else:
            outlines.update(read.outlines or {})

    records = []
    how: dict[str, str] = {}
    for path in cited:
        file = present[path]
        if file is None:
            how[path] = "missing"
        elif not path.lower().endswith(_DOC_SUFFIXES):
            # Trap: changelogs cite binaries and logs (a 63MB graph.db, a 7MB jsonl); reading
            # them as prose was 97% of the pass's input bytes and broke the 500ms bar.
            records.append(ArtifactRecord(path=path))
            how[path] = "path_only"
        elif path in outlines and outlines[path]:
            records.append(extract_record(path, outline="\n".join(outlines[path])))
            how[path] = "outline"
        else:
            records.append(extract_record(path, text=file.read_bytes().decode("utf-8", errors="replace")))
            how[path] = "opened"

    digests = cluster_records(records)
    changes = []
    for week, change in resolved:
        states = [how[p] for p in change.cited_paths]
        if not states:
            fate = "log_only"
        elif all(s != "missing" for s in states):
            fate = "opened"
        elif any(s != "missing" for s in states):
            fate = "partial"
        else:
            fate = "missing"
        changes.append({"week": week, "day": change.day, "title": change.title,
                        "commits": list(change.commits), "cited_paths": list(change.cited_paths),
                        "fate": fate})

    opened = sum(1 for s in how.values() if s in ("opened", "outline"))
    path_only = sum(1 for s in how.values() if s == "path_only")
    return {
        "digests": {
            "schema_version": SCHEMA_VERSION,
            "weeks_total": coverage["weeks_total"],
            "weeks_walked": coverage["weeks_walked"],
            "artifacts_opened": opened,
            "artifacts_path_only": path_only,
            "artifacts_cited": len(cited),
            "total_tokens": sum(d.tokens for d in digests),
            "topics": [{"topic_key": d.topic_key, "tokens": d.tokens, "text": d.text,
                        "paths": [r.path for r in d.records]} for d in digests],
        },
        "ledger": {
            "schema_version": SCHEMA_VERSION,
            "coverage": coverage,
            "weeks": [{"week": e.week, "summary_path": e.summary_path, "flagged": len(e.flagged)}
                      for e in entries],
            "changes": changes,
            "artifacts": [{"path": p, "how": how[p]} for p in cited],
            "rag_gaps": sorted(gaps.values(), key=lambda g: g["form"]),
        },
    }


def _dump(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


@register_op("backlog.triage_pass")
def _handler(params: dict, repo_root: Path | None = None) -> dict:
    """backlog.triage_pass handler: changelog-first, model-free triage of a repo's backlog.

    Params:
        run_id (str, REQUIRED) — names the output directory; a plain token, never a path.
        source_repo (str, optional) — the checkout whose changelogs are walked, read-only;
            default the invoking repo. Relative paths resolve against the invoking repo.

    Writes state/scratch/backlog-pass/<run_id>/digests.json and triage-ledger.json under the
    invoking repo only. repo_root (injected): git common dir of the originating worktree.
    """
    if repo_root is None:
        raise ValueError(
            "backlog.triage_pass requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None — op scope must be 'common_dir'. No silent meta-repo fallback."
        )
    run_id = params.get("run_id")
    if not run_id or not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id) or ".." in run_id:
        raise ValueError("backlog.triage_pass requires run_id: a plain token of letters, digits, '.', '_', '-'.")

    worktree_root = main_worktree_root(repo_root)
    raw = params.get("source_repo")
    source_root = Path(raw).expanduser() if raw else worktree_root
    if not source_root.is_absolute():
        source_root = worktree_root / source_root
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise ValueError(f"backlog.triage_pass source_repo is not a directory: {source_root}")

    out = run_pass(source_root)
    run_dir = worktree_root / OUT_DIR_REL / run_id
    digests_path = claimed_write.replace_bytes(run_dir / "digests.json", _dump(out["digests"]))
    ledger_path = claimed_write.replace_bytes(run_dir / "triage-ledger.json", _dump(out["ledger"]))

    d = out["digests"]
    gaps = out["ledger"]["rag_gaps"]
    return {
        "run_id": run_id,
        "digests_path": rel_id(digests_path, worktree_root),
        "ledger_path": rel_id(ledger_path, worktree_root),
        "weeks_total": d["weeks_total"],
        "weeks_walked": d["weeks_walked"],
        "topics": len(d["topics"]),
        "total_tokens": d["total_tokens"],
        "artifacts_opened": d["artifacts_opened"],
        "artifacts_path_only": d["artifacts_path_only"],
        "artifacts_cited": d["artifacts_cited"],
        "rag_gaps": gaps,
        "summary": (
            f"backlog.triage_pass {run_id}: {d['weeks_walked']}/{d['weeks_total']} weeks walked, "
            f"{d['artifacts_opened']}/{d['artifacts_cited']} cited artifacts opened, "
            f"{len(d['topics'])} topics, {d['total_tokens']} tokens, {len(gaps)} rag gap(s)"
        ),
    }
