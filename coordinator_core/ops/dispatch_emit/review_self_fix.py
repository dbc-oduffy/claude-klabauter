"""Decision module for a reviewer that applied its own fixes: re-verify them, then give the
verdict on the fixed plan. Re-author is reached only through findings left unfixed.

Pure: no I/O. The composed review leg supplies the findings and the re-verify callable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit.run_truth_contract import ReviewVerdict

VERDICT_OK = "ok"
VERDICT_BLOCKED = "blocked"
_SELF_FIXABLE = frozenset({"blocked", "warn"})
STATUS_APPLIED = "applied"


@dataclass(frozen=True)
class FinalVerdict:
    """`verdict` is the reviewer's verdict on the fixed plan; `unfixed` names the findings that
    still need a re-author, and `reauthor` is true only when `unfixed` is non-empty."""

    verdict: ReviewVerdict
    unfixed: tuple = ()

    @property
    def reauthor(self) -> bool:
        return bool(self.unfixed)


def _fid(finding: Mapping[str, Any], index: int) -> str:
    return str(finding.get("id") or f"finding-{index + 1}")


def final_verdict(
    verdict: str,
    findings: Sequence[Mapping[str, Any]],
    reverify: Optional[Callable[[Mapping[str, Any]], bool]] = None,
) -> FinalVerdict:
    """The verdict on the plan as fixed.

    A blocked or warn verdict whose every finding is `applied` is re-verified finding by finding
    (`verified: true` on the finding counts as done; otherwise `reverify` must return true).
    All re-verified: the verdict is `ok` on the fixed plan. A finding not applied, or whose
    re-verify fails or cannot run, stays unfixed and the verdict stays `blocked`. A blocked
    verdict with no findings is unfixed by definition. Any other verdict passes through."""
    label = str(verdict or "").strip().lower()
    if label not in _SELF_FIXABLE:
        return FinalVerdict(ReviewVerdict(verdict=label, reverified=False))
    if not findings:
        return FinalVerdict(ReviewVerdict(verdict=VERDICT_BLOCKED), ("no-findings-recorded",))
    applied: list = []
    unfixed: list = []
    for index, finding in enumerate(findings):
        fid = _fid(finding, index)
        if str(finding.get("status") or "").strip().lower() != STATUS_APPLIED:
            unfixed.append(fid)
            continue
        if finding.get("verified") is True or (reverify is not None and reverify(finding)):
            applied.append(fid)
        else:
            unfixed.append(fid)
    if unfixed:
        return FinalVerdict(
            ReviewVerdict(verdict=VERDICT_BLOCKED, fixes_applied=tuple(applied), reverified=False),
            tuple(unfixed),
        )
    return FinalVerdict(ReviewVerdict(verdict=VERDICT_OK, fixes_applied=tuple(applied), reverified=True))
