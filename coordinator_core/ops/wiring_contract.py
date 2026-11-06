"""Judge a plan's optional `wiring:` list: which declared parts are not delivered.

Pure functions; the only IO is one `Path.is_file()` per part's `built_path`.
An absent `wiring:` key is legacy and never refused. A `runs_evidence` entry is a
`{path, sha, dirty}` mapping (`evidence_entry`) and counts only when its sha is the
delivered sha and its tree was clean.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.ops.dispatch_emit.run_truth_contract import STATUS_UNWIRED

STATUS_WIRED = "wired"
STATUS_NOT_BUILT = "not_built"
STATUS_BLOCKED = "blocked"

REASON_BUILT_PATH_MISSING = "built-path-missing"
REASON_STUB_ONLY = "stub-only-evidence"
REASON_DECLARED = "declared"
REASON_EVIDENCE_SHA_MISMATCH = "evidence-sha-mismatch"
REASON_EVIDENCE_DIRTY = "evidence-dirty-tree"
REASON_EVIDENCE_SHA_MISSING = "evidence-sha-missing"

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


def _verdict(part: Any, repo_root: Path, delivered_sha: Optional[str]) -> dict:
    if not isinstance(part, Mapping):
        return {"id": "?", "status": "malformed", "reason": "part-not-a-mapping"}
    pid = str(part.get("id", "?"))
    declared = part.get("status")
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
    reasons = [_entry_reason(e, delivered_sha) for e in evidence]
    if "" in reasons:
        return {"id": pid, "status": STATUS_WIRED, "reason": ""}
    # First non-stub failure names the cause; all-stub stays the stub reason.
    reason = next((r for r in reasons if r != REASON_STUB_ONLY), REASON_STUB_ONLY)
    return {"id": pid, "status": STATUS_UNWIRED, "reason": reason}


def evidence_entry(path: str, sha: str, dirty: bool) -> dict:
    """The one `runs_evidence` entry shape writers use: the path plus the tree it ran from."""
    return {"path": path, "sha": sha, "dirty": dirty}


def _entry_reason(entry: Any, delivered_sha: Optional[str]) -> str:
    """'' when the entry counts, else the reason it does not."""
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
        return REASON_EVIDENCE_SHA_MISMATCH
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
    return [_verdict(p, root, delivered_sha) for p in wiring]


def refusal(
    fm: Mapping[str, Any], repo_root: Path, delivered_sha: Optional[str] = None
) -> Optional[str]:
    """One sentence naming every non-wired part, or None when delivery may proceed."""
    if not isinstance(fm, Mapping) or "wiring" not in fm:
        return None
    if not isinstance(fm["wiring"], list):
        return "wiring: malformed"
    bad = [v for v in part_verdicts(fm, repo_root, delivered_sha) if v["status"] != STATUS_WIRED]
    if not bad:
        return None
    return "; ".join(
        f"wiring: {v['id']} {v['status']} ({v['reason']})" for v in bad
    )
