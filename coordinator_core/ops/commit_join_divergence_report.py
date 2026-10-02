"""
coordinator_core.ops.commit_join_divergence_report -- JSON-RPC
"commit_ledger.join_divergence_report" operation.

Purpose: read the claim-derived ledger join (sha -> handoff -> deliverable) and
the Deliverable-Id trailer join over the last N commits and hand both to
`commit_ledger.join_divergence.compare`.

Negative spec: read-only, never gates. Exactly ONE `run_git` call per dispatch
(the trailer walk); the ledger side is file reads only. The payload carries no
exit code, no ok/refused flag and no verdict. An absent or empty ledger is a
normal result, reported in `basis`, never an error. A ledger sha is matched by
its full 40-hex form only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional, Set

from coordinator_core.commit_ledger import join_divergence, store
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.reconcile.handoff_corpus import _collect_all_handoffs_for_gate_index

DEFAULT_N = 200
MAX_N = 1000
_RS = "\x1e"
_US = "\x1f"
_DELIVERABLE_PREFIX = "dlv-"


def _clamp_n(raw: object) -> int:
    try:
        n = int(raw) if raw is not None else DEFAULT_N
    except (TypeError, ValueError):
        n = DEFAULT_N
    return max(1, min(MAX_N, n))


def _read_trailer_window(n: int, cwd: Optional[str]):
    """One git spawn: returns (shas in log order, trailer_by_sha, error)."""
    result = run_git(
        ["log", f"-{n}", f"--format=%H%x1f%(trailers:key=Deliverable-Id,valueonly)%x1e"],
        cwd=cwd,
    )
    if result.returncode != 0:
        return [], {}, (result.stderr or "").strip() or f"git exited {result.returncode}"
    shas: List[str] = []
    trailers: Dict[str, str] = {}
    for record in result.stdout.split(_RS):
        record = record.strip("\r\n")
        if not record or _US not in record:
            continue
        sha, value = record.split(_US, 1)
        sha = sha.strip()
        if not sha:
            continue
        shas.append(sha)
        for token in value.split():
            if token.startswith(_DELIVERABLE_PREFIX):
                trailers[sha] = token
                break
    return shas, trailers, None


def _read_ledger_shas(ldir: Optional[Path], window: Set[str]):
    """Every `*.jsonl` read once: returns (sha -> {handoff_id} within `window`, files_seen)."""
    sha_handoffs: Dict[str, Set[str]] = {}
    if ldir is None or not ldir.is_dir():
        return sha_handoffs, 0
    files_seen = 0
    for path in sorted(ldir.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        files_seen += 1
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            sha = data.get("sha")
            if isinstance(sha, str) and sha in window:
                sha_handoffs.setdefault(sha, set()).add(path.stem)
    return sha_handoffs, files_seen


def _deliverables_by_handoff(worktree_root: Path, wanted: Set[str]) -> Dict[str, str]:
    handoffs, _scan_errors = _collect_all_handoffs_for_gate_index(worktree_root)
    out: Dict[str, str] = {}
    for meta in handoffs:
        hid = meta.get("handoff_id")
        did = meta.get("deliverable_id")
        if isinstance(hid, str) and hid in wanted and isinstance(did, str) and did.strip():
            out[hid] = did.strip()
    return out


@register_op("commit_ledger.join_divergence_report")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    # Body spawns git and reads the filesystem; off-loop so dispatch's wait_for can fire.
    return await asyncio.to_thread(_run, params, repo_root)


def _run(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "commit_ledger.join_divergence_report" handler.

    Params: `n` (int, default 200, clamped to 1..1000), optional `cwd`.

    Returns `DivergenceReport._asdict()` (counts, disagreements, commits_examined,
    basis) plus `ledger_files_seen`, `ledger_shas_unresolved`,
    `ledger_shas_ambiguous`, and `git_error` (None unless the log read failed).
    """
    params = params or {}
    n = _clamp_n(params.get("n"))
    cwd = params.get("cwd") or (str(repo_root) if repo_root is not None else None)

    shas, trailer_by_sha, git_error = _read_trailer_window(n, cwd)

    ldir = store.ledger_dir(cwd)
    sha_handoffs, files_seen = _read_ledger_shas(ldir, set(shas))

    ledger_by_sha: Dict[str, str] = {}
    unresolved = 0
    ambiguous = 0
    if sha_handoffs:
        root = main_worktree_root(Path(cwd)) if cwd else main_worktree_root(repo_root)
        wanted = {hid for hids in sha_handoffs.values() for hid in hids}
        deliverable_of = _deliverables_by_handoff(root, wanted)
        for sha, hids in sha_handoffs.items():
            resolved = {deliverable_of[h] for h in hids if h in deliverable_of}
            if not resolved:
                unresolved += 1
            elif len(resolved) > 1:
                ambiguous += 1
            else:
                ledger_by_sha[sha] = next(iter(resolved))

    report = join_divergence.compare(shas, ledger_by_sha, trailer_by_sha)
    payload = report._asdict()
    payload["disagreements"] = [row._asdict() for row in report.disagreements]
    if files_seen == 0:
        payload["basis"] += " The ledger side saw 0 files in this clone."
    payload["ledger_files_seen"] = files_seen
    payload["ledger_shas_unresolved"] = unresolved
    payload["ledger_shas_ambiguous"] = ambiguous
    payload["git_error"] = git_error
    return payload
