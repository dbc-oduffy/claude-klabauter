"""Judge a plan's optional `wiring:` list: which declared parts are not delivered.

IO: one `Path.is_file()` per part's `built_path`, and at most one `git log` per distinct
evidence sha that is not the delivered sha. An absent `wiring:` key is legacy and never
refused. A `runs_evidence` entry is a `{path, sha, dirty}` mapping (`evidence_entry`) and
counts only when its tree was clean and its sha is the delivered sha or an ancestor of it
with no change under the part's `built_path` or `consumes` paths since.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit.run_truth_contract import STATUS_UNWIRED

STATUS_WIRED = "wired"
STATUS_NOT_BUILT = "not_built"
STATUS_BLOCKED = "blocked"
STATUS_OFF_ROUTE = "off_route"

REASON_BUILT_PATH_MISSING = "built-path-missing"
REASON_STUB_ONLY = "stub-only-evidence"
REASON_DECLARED = "declared"
REASON_EVIDENCE_SHA_MISMATCH = "evidence-sha-mismatch"
REASON_EVIDENCE_DIRTY = "evidence-dirty-tree"
REASON_EVIDENCE_SHA_MISSING = "evidence-sha-missing"
REASON_UNFED_CONSUMES = "unfed-consumes"
REASON_NOT_ANCESTOR = "not-an-ancestor-of-head"
REASON_COVERED_CHANGED = "covered-paths-changed"

_SHA_RE = re.compile(r"[0-9a-fA-F]{4,64}")

SEAM_KEYS = ("consumes", "produces", "consumes_external")

STUB_SEGMENTS = frozenset(
    {"fixtures", "fixture", "stubs", "stub", "fakes", "fake", "mocks", "mock"}
)
STUB_BASENAME_EXACT = frozenset({"conftest.py"})
STUB_BASENAME_PREFIXES = ("stub_", "fake_", "mock_")


def is_stub_evidence(path: str) -> bool:
    """True when a runs-evidence path binds a fixture or stub (pure string test)."""
    segments = [s for s in str(path).replace("\\", "/").split("/") if s]
    if not segments:
        return True
    if any(s in STUB_SEGMENTS for s in segments):
        return True
    base = segments[-1]
    return base in STUB_BASENAME_EXACT or base.startswith(STUB_BASENAME_PREFIXES)


def _seam_list(part: Mapping[str, Any], key: str) -> Optional[list[str]]:
    """The part's `key` as a list of strings; `[]` when absent, None when not a list of strings."""
    value = part.get(key, [])
    if isinstance(value, list) and all(isinstance(e, str) for e in value):
        return value
    return None


def _seam_parts(fm: Mapping[str, Any]) -> list[tuple[str, list[str], list[str], list[str]]]:
    """`(id, consumes, produces, consumes_external)` per well-formed mapping part."""
    wiring = fm.get("wiring") if isinstance(fm, Mapping) else None
    if not isinstance(wiring, list):
        return []
    rows = []
    for p in wiring:
        if not isinstance(p, Mapping):
            continue
        if p.get("status") == STATUS_OFF_ROUTE:
            continue
        lists = [_seam_list(p, k) for k in SEAM_KEYS]
        if any(lst is None for lst in lists):
            continue
        rows.append((str(p.get("id", "?")), *lists))
    return rows


def unfed_consumes(fm: Mapping[str, Any]) -> list[tuple[str, str]]:
    """`(part id, entry)` for each `consumes` entry that no other part in the list `produces`.

    `consumes_external` entries are exempt. A part feeding itself does not count as fed.
    """
    rows = _seam_parts(fm)
    out = []
    for i, (pid, consumes, _produces, external) in enumerate(rows):
        fed = {e for j, r in enumerate(rows) if j != i for e in r[2]}
        out.extend((pid, e) for e in consumes if e not in fed and e not in external)
    return out


def unconsumed_produces(fm: Mapping[str, Any]) -> list[tuple[str, str]]:
    """`(part id, entry)` for each `produces` entry no other part `consumes`; report-only, never refused."""
    rows = _seam_parts(fm)
    out = []
    for i, (pid, _consumes, produces, _external) in enumerate(rows):
        used = {e for j, r in enumerate(rows) if j != i for e in r[1]}
        out.extend((pid, e) for e in produces if e not in used)
    return out


def _verdict(
    part: Any,
    repo_root: Path,
    delivered_sha: Optional[str],
    ancestry: Optional[dict] = None,
) -> dict:
    if not isinstance(part, Mapping):
        return {"id": "?", "status": "malformed", "reason": "part-not-a-mapping"}
    pid = str(part.get("id", "?"))
    for key in SEAM_KEYS:
        if key in part and _seam_list(part, key) is None:
            return {"id": pid, "status": "malformed", "reason": f"{key}-not-a-list-of-strings"}
    declared = part.get("status")
    if declared == STATUS_OFF_ROUTE:
        why = part.get("off_route_reason")
        if not isinstance(why, str) or not why.strip():
            return {"id": pid, "status": "malformed", "reason": "off-route-reason-missing"}
        return {"id": pid, "status": STATUS_OFF_ROUTE, "reason": why}
    if declared in (STATUS_NOT_BUILT, STATUS_BLOCKED):
        reason = REASON_DECLARED
        if declared == STATUS_BLOCKED and part.get("blocked_on"):
            reason = str(part["blocked_on"])
        return {"id": pid, "status": declared, "reason": reason}
    if declared != STATUS_WIRED:
        return {"id": pid, "status": "malformed", "reason": f"unknown-status:{declared}"}
    built = part.get("built_path")
    if not isinstance(built, str) or not built or not (repo_root / built).is_file():
        return {"id": pid, "status": STATUS_UNWIRED, "reason": REASON_BUILT_PATH_MISSING}
    evidence = part.get("runs_evidence")
    if not isinstance(evidence, list) or not evidence:
        return {"id": pid, "status": STATUS_UNWIRED, "reason": REASON_STUB_ONLY}
    covered = [built, *(_seam_list(part, "consumes") or [])]
    reasons = [_entry_reason(e, delivered_sha, repo_root, covered, ancestry) for e in evidence]
    if "" in reasons:
        return {"id": pid, "status": STATUS_WIRED, "reason": ""}
    # First non-stub failure names the cause; all-stub stays the stub reason.
    reason = next((r for r in reasons if r != REASON_STUB_ONLY), REASON_STUB_ONLY)
    return {"id": pid, "status": STATUS_UNWIRED, "reason": reason}


def evidence_entry(path: str, sha: str, dirty: bool) -> dict:
    """The one `runs_evidence` entry shape writers use: the path plus the tree it ran from."""
    return {"path": path, "sha": sha, "dirty": dirty}


def _changed_since(
    repo_root: Path, sha: str, head: str, cache: dict
) -> Optional[tuple[bool, list[str]]]:
    """`(is_ancestor, paths changed sha..head)` from one `git log`, memoised per sha in `cache`.

    None when git cannot answer (unknown sha, not a repo). One spawn covers every part's
    covered paths; the caller filters the returned names.
    """
    if sha in cache:
        return cache[sha]
    result = None
    if _SHA_RE.fullmatch(sha):
        from coordinator_core.ops.ceremony import git_native

        r = git_native._git(
            [
                "-c", "core.quotepath=false", "log", "--left-right", "--no-renames",
                "--name-only", "--format=%x01%m", f"{sha}...{head}", "--",
            ],
            cwd=repo_root,
        )
        if r.ok:
            ancestor, changed, side = True, [], ""
            for line in r.stdout.splitlines():
                if line.startswith("\x01"):
                    side = line[1:]
                    ancestor = ancestor and side != "<"
                elif line and side == ">":
                    changed.append(line)
            result = (ancestor, changed)
    cache[sha] = result
    return result


def _covered_changes(covered: list[str], changed: list[str]) -> list[str]:
    """The changed paths equal to or under a covered path, sorted and de-duplicated."""
    roots = [c.replace("\\", "/").strip().rstrip("/") for c in covered if isinstance(c, str)]
    roots = [r for r in roots if r]
    return sorted({n for n in changed if any(n == r or n.startswith(r + "/") for r in roots)})


def _entry_reason(
    entry: Any,
    delivered_sha: Optional[str],
    repo_root: Optional[Path] = None,
    covered: Sequence[str] = (),
    ancestry: Optional[dict] = None,
) -> str:
    """'' when the entry counts, else the reason it does not.

    A sha other than `delivered_sha` counts when it is an ancestor of it and no covered path
    changed between them; sha == delivered_sha costs no git spawn.
    """
    if not isinstance(entry, Mapping):
        if isinstance(entry, str) and not is_stub_evidence(entry):
            return REASON_EVIDENCE_SHA_MISSING
        return REASON_STUB_ONLY
    path = entry.get("path")
    if not isinstance(path, str) or is_stub_evidence(path):
        return REASON_STUB_ONLY
    sha = entry.get("sha")
    if not isinstance(sha, str) or not sha:
        return REASON_EVIDENCE_SHA_MISSING
    if delivered_sha is not None and sha != delivered_sha:
        answer = (
            _changed_since(repo_root, sha, delivered_sha, ancestry if ancestry is not None else {})
            if repo_root is not None
            else None
        )
        if answer is None:
            return REASON_EVIDENCE_SHA_MISMATCH
        is_ancestor, changed = answer
        if not is_ancestor:
            return f"{REASON_EVIDENCE_SHA_MISMATCH} ({REASON_NOT_ANCESTOR})"
        hits = _covered_changes(list(covered), changed)
        if hits:
            return f"{REASON_COVERED_CHANGED}: {', '.join(hits)}"
    if entry.get("dirty") is not False:
        return REASON_EVIDENCE_DIRTY
    return ""


def part_verdicts(
    fm: Mapping[str, Any], repo_root: Path, delivered_sha: Optional[str] = None
) -> list[dict]:
    """Per-part `{id, status, reason}`; `[]` when `wiring:` is absent or not a list.

    `delivered_sha=None` skips only the sha-equality test (a caller that cannot name the
    delivered sha); a missing sha and a dirty tree are refused regardless.
    """
    wiring = fm.get("wiring") if isinstance(fm, Mapping) else None
    if not isinstance(wiring, list):
        return []
    root = Path(repo_root)
    ancestry: dict = {}
    return [_verdict(p, root, delivered_sha, ancestry) for p in wiring]


def refusal(
    fm: Mapping[str, Any], repo_root: Path, delivered_sha: Optional[str] = None
) -> Optional[str]:
    """One sentence naming every non-wired part, or None when delivery may proceed."""
    if not isinstance(fm, Mapping) or "wiring" not in fm:
        return None
    if not isinstance(fm["wiring"], list):
        return "wiring: malformed"
    ok = (STATUS_WIRED, STATUS_OFF_ROUTE)
    bad = [v for v in part_verdicts(fm, repo_root, delivered_sha) if v["status"] not in ok]
    sentences = [f"wiring: {v['id']} {v['status']} ({v['reason']})" for v in bad]
    sentences += [
        f"wiring: {pid} {STATUS_NOT_BUILT} ({REASON_UNFED_CONSUMES}: {entry})"
        for pid, entry in unfed_consumes(fm)
    ]
    return "; ".join(sentences) or None


REASON_UNCLAIMED = "unclaimed"
REASON_WRITES_ABSENT = "writes-absent"


def register_coverage_refusal(
    spine_rows: Any,
    register_rows: Any,
    repo_root: Path,
    exclude: Any = (),
) -> Optional[str]:
    """One sentence naming each register row no live spine row claims (`unclaimed`) or whose claiming
    rows declare no write that exists under `repo_root` (`writes-absent`; a row with `writes: []`
    claims nothing built), or None when every row is covered. Rows in `exclude` never refuse."""
    from coordinator_core.ops import requirement_register as rr

    spine = [r for r in spine_rows if isinstance(r, Mapping)] if isinstance(spine_rows, list) else []
    claimants = rr.spine_claimants(spine)
    by_id = {str(r.get("id", "")): r for r in spine}
    skip = {str(i) for i in exclude}
    parts: list[str] = []
    for rid in rr.unclaimed_ids(register_rows, (), exclude=skip):
        owners = claimants.get(rid)
        if not owners:
            parts.append(f"register: {rid} not_built ({REASON_UNCLAIMED})")
            continue
        writes = [
            str(w).replace("\\", "/")
            for o in owners
            for w in (by_id.get(o, {}).get("writes") or [])
            if isinstance(w, str) and w
        ]
        if not any((Path(repo_root) / w).is_file() for w in writes):
            parts.append(f"register: {rid} not_built ({REASON_WRITES_ABSENT})")
    return "; ".join(parts) or None
