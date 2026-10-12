"""Release contract for workweek-complete: rule ids, states, results, miss reasons.

Plain data, dataclasses and two ledger-row readers, no I/O. Rule modules return `RuleResult`; a reason outside
`MISS_REASONS[rule]` is refused at construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Sequence, Tuple

RULE_IDS: Tuple[str, ...] = (
    "changelog-confirm",
    "version-bump",
    "release-notes",
    "merge-evidence",
    "publish-evidence",
)

RELEASE_STATES: Tuple[str, ...] = (
    "unstamped",
    "stamped",
    "merged-unpublished",
    "published-unarchived",
    "nothing-to-release",
)

RELEASE_MODES: Tuple[str, ...] = ("gh-release", "git-tag", "none")

# Keys read from the first frontmatter block of coordinator.local.md. `tag_prefix` is parsed
# as `merge-recovery-and-tag-cut.py :: resolve_tag_prefix` does: first block only, first
# line wins, inline `#` comment stripped, a quoted value is an authoring error.
MODE_CONFIG_KEYS: Dict[str, Tuple[str, ...]] = {
    "gh-release": ("tag_anchor", "source_is_live", "tag_prefix"),
    "git-tag": ("tag_anchor", "tag_prefix"),
    "none": (),
}

# A daily block under state/week-changelog/, group 1 its date. A pending-release file matches
# the pattern too and is never a daily block: test the suffix first.
DAILY_BLOCK_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-.+)?\.md$")
PENDING_RELEASE_SUFFIX = "-pending-release.md"


def row_field(row: object, key: str) -> object:
    """One field of a ledger row, which is a mapping or an object."""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def last_ledger_row(ledger_rows: Iterable[object], ids: Sequence[str]) -> Optional[object]:
    """The last row whose `id` is one of `ids`, or None."""
    rows = [r for r in ledger_rows if row_field(r, "id") in ids]
    return rows[-1] if rows else None

MISS_REASONS: Dict[str, Tuple[str, ...]] = {
    "changelog-confirm": (
        "uncovered-commits",
        "validation-failed",
        "block-outside-week",
        "backfill-not-run",
    ),
    "version-bump": (
        "no-recommendation",
        "not-an-increase",
        "tag-exists",
    ),
    "release-notes": (
        "notes-missing",
        "notes-empty",
        "entry-not-cited",
        "placeholder-version",
        "lint-failed",
    ),
    "merge-evidence": (
        "review-blocked",
        "review-missing",
        "unreviewed-tail",
        "review-uncovered",
        "gate-tests-missing",
        "gate-tests-failed",
        "gate-tests-other-commit",
        "ledger-not-clean",
        "not-stamped",
        "head-moved",
        "tree-modified",
        "main-conflict",
        "project-checks-not-composed",
    ),
    "publish-evidence": (
        "merge-not-on-main",
        "tag-on-other-commit",
        "notes-changed",
    ),
}

# Argv tokens no release act may carry. Beyond the tokens: no refspec starts with `+`
# and none names `main` as its destination.
FORBIDDEN_ARGV_TOKENS: Tuple[str, ...] = (
    "--force",
    "-f",
    "--force-with-lease",
    "--admin",
    "--auto",
    "--squash",
    "--rebase",
    "--delete-branch",
    "revert",
    "reset",
)


@dataclass(frozen=True)
class RuleResult:
    """Outcome of one rule. `reason` is empty on a pass, else a member of `MISS_REASONS[rule]`.

    `evidence` is a flat dict of what the rule read (shas, paths, counts, hashes), small
    enough for one Decision row.
    """

    rule: str
    passed: bool
    reason: str = ""
    evidence: Dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rule not in RULE_IDS:
            raise ValueError(f"unknown rule {self.rule!r}")
        if self.passed:
            if self.reason:
                raise ValueError(f"rule {self.rule!r} passed but carries reason {self.reason!r}")
        elif self.reason not in MISS_REASONS[self.rule]:
            raise ValueError(f"reason {self.reason!r} is not a miss reason of rule {self.rule!r}")


@dataclass(frozen=True)
class ReleaseState:
    """Where a release stands; `state` is a member of `RELEASE_STATES`."""

    state: str
    version: str = ""
    tag: str = ""
    merge_sha: str = ""

    def __post_init__(self) -> None:
        if self.state not in RELEASE_STATES:
            raise ValueError(f"unknown release state {self.state!r}")


@dataclass(frozen=True)
class StampResult:
    """Outcome of stamping the version surfaces and notes; `changed` is False on a re-run."""

    changed: bool
    notes_path: str = ""


@dataclass(frozen=True)
class MergeResult:
    """Outcome of the merge act; `merged_now` is False when main already held the merge."""

    merge_sha: str
    pr_number: int
    merged_now: bool
