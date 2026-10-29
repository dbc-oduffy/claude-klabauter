"""After-the-fact twin of the peer-hunk gate: list each landing hunk outside its run's declared writes.

Landing rule: a commit is an engine landing when its message carries a ``Lands-*`` or ``Checkpoint-*``
trailer, or names a run id (``ask-YYYYMMDDTHHMMSS-xxxxxx`` / ``warp-<slug>-YYYYMMDDTHHMMSS-xxxxxx``).
Its run id is the first such id ranked: subject, then an ``Inline-Review: applies <id>`` line, then the
rest of the body in order; ids on ``Row-Unlanded`` lines name a withholding peer and are never the owner.
A landing whose id has no ``state/warp/<id>/manifest.json`` carries ``run_id: None`` and every path foreign.

Declared set: the manifest's ``rows[].writes`` plus ``doc_writes`` plus ``run_dir``, plus the run's own
``state/warp/<id>`` record files. Touch records (``session.touch_record.project_live_claims``) are
session-keyed live claims with no run association, so they yield nothing per run; manifest writes alone
define "declared". Coverage is ``run_ownership._covers``.

Owner: another run among the window's landings whose declared set covers the path, nearest earlier landing
first (git log order is newest first, so later list positions are earlier), then newer ones; else "unknown".

One ``git log -p -U0`` spawn covers the window; nothing is written.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.run_ownership import _covers

_SENT = "\x01COMMIT\x01"
_END = "\x01BODYEND\x01"
_RUN_ID = re.compile(r"\b(?:ask|warp)-[A-Za-z0-9-]*?\d{8}T\d{6}-[0-9a-f]{6}\b")
_MARKER = re.compile(r"^(?:Lands-[A-Za-z]+|Checkpoint-[A-Za-z-]+):", re.M)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF = re.compile(r"^diff --git a/.* b/(.*)$")


@dataclass
class Landing:
    commit: str
    run_id: str | None
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)


def _pick_run_id(subject: str, body: str) -> str | None:
    cands = _RUN_ID.findall(subject)
    for line in body.splitlines():
        if line.startswith("Inline-Review:") and " applies " in line:
            cands += _RUN_ID.findall(line.split(" applies ", 1)[1])
    for line in body.splitlines():
        if not line.startswith("Row-Unlanded"):
            cands += _RUN_ID.findall(line)
    return cands[0] if cands else None


def _parse_diff(lines: list[str]) -> dict[str, list[tuple[int, int]]]:
    hunks: dict[str, list[tuple[int, int]]] = {}
    cur: str | None = None
    for ln in lines:
        m = _DIFF.match(ln)
        if m:
            cur = m.group(1)
            hunks.setdefault(cur, [])
            continue
        m = _HUNK.match(ln)
        if m and cur is not None:
            hunks[cur].append((int(m.group(1)), 1 if m.group(2) is None else int(m.group(2))))
    return hunks


def landings(repo: Path, since: str) -> list[Landing]:
    """Engine landings since `since`, newest first, from one `git log -p -U0` spawn."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        ["git", "-c", "core.quotepath=off", "log", f"--since={since}", "-p", "-U0", "--no-renames",
         f"--format={_SENT}%H%n%s%n%b{_END}"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, creationflags=flags,
    ).stdout
    result: list[Landing] = []
    for chunk in out.split(_SENT)[1:]:
        head, _, diff = chunk.partition(_END)
        sha, _, rest = head.partition("\n")
        subject, _, body = rest.partition("\n")
        run_id = _pick_run_id(subject, body)
        if run_id is None and not _MARKER.search(body):
            continue
        if run_id is not None and not _manifest_path(Path(repo), run_id):
            run_id = None
            if not _MARKER.search(body):
                continue
        result.append(Landing(sha, run_id, _parse_diff(diff.splitlines())))
    return result


def _record_dir(repo: Path, run_id: str) -> Path | None:
    """The run's record dir via the shared seam; None for an unsafe run id."""
    try:
        return record_dir(repo, run_id)
    except ValueError:
        return None


def _manifest_path(repo: Path, run_id: str) -> Path | None:
    d = _record_dir(repo, run_id)
    p = d / "manifest.json" if d is not None else None
    return p if p is not None and p.is_file() else None


def _declared(repo: Path, run_id: str) -> set[str]:
    d = set(run_manifest_writes(repo, run_id))
    rd = _record_dir(repo, run_id)
    if rd is not None:
        try:
            d.add(rd.relative_to(repo).as_posix())
        except ValueError:
            pass
    return d


def run_manifest_writes(repo: Path, run_id: str) -> set[str]:
    """`rows[].writes` plus `doc_writes` (plus `run_dir`) of one run's manifest; empty when it is absent."""
    p = _manifest_path(Path(repo), run_id)
    try:
        m = json.loads(p.read_text(encoding="utf-8")) if p is not None else {}
    except (OSError, ValueError):
        return set()
    w: set[str] = set()
    for row in m.get("rows") or []:
        w.update(x for x in (row.get("writes") or []) if isinstance(x, str))
    w.update(x for x in (m.get("doc_writes") or []) if isinstance(x, str))
    if isinstance(m.get("run_dir"), str):
        w.add(m["run_dir"])
    return w


def audit(repo: Path, since: str) -> dict:
    """{"since", "landings": [{commit, run_id, foreign: [{path, hunks, likely_owner}]}]}, newest first."""
    repo = Path(repo)
    lands = landings(repo, since)
    declared: dict[str, set[str]] = {}
    order: list[str] = []
    for ld in lands:
        if ld.run_id and ld.run_id not in declared:
            declared[ld.run_id] = _declared(repo, ld.run_id)
            order.append(ld.run_id)
    entries = []
    for i, ld in enumerate(lands):
        mine = declared.get(ld.run_id, set()) if ld.run_id else set()
        # order is newest->oldest; this run's position splits older (nearest first) from newer.
        if ld.run_id in order:
            pos = order.index(ld.run_id)
            older, newer = order[pos + 1:], list(reversed(order[:pos]))
        else:
            older, newer = [], order
        foreign = []
        for path, hk in ld.hunks.items():
            if any(_covers(e, path) for e in mine):
                continue
            owner = next((r for r in older + newer if any(_covers(e, path) for e in declared[r])), "unknown")
            foreign.append({"path": path, "hunks": [list(h) for h in hk], "likely_owner": owner})
        entries.append({"commit": ld.commit, "run_id": ld.run_id, "foreign": foreign})
    return {"since": since, "landings": entries}
