"""Types and the frontmatter reader shared by the release_stamp package (stdlib only)."""

from __future__ import annotations

from dataclasses import dataclass

ENTRY_ROOT = "archive/completed"
PENDING = "pending-release"
RELEASED = "released"
STAMP_FIELDS = ("status", "released_in", "released_at", "released_sha")


@dataclass(frozen=True)
class ReleaseTag:
    """One version tag, resolved to the commit it names.

    `version` is the (major, minor, patch) triple the ordering uses; `date` is the
    commit's author date as YYYY-MM-DD. A stamp is a function of these fields alone.
    """

    name: str
    version: tuple[int, int, int]
    commit_sha: str
    tree_sha: str
    date: str

    def record(self, path: str) -> dict[str, str]:
        """The stamped-entry record for `path`: its path and the three `released_*` values."""
        return {
            "path": path,
            "released_in": self.name,
            "released_at": self.date,
            "released_sha": self.commit_sha,
        }


@dataclass(frozen=True)
class Attribution:
    """`stamps` maps a repo-relative POSIX entry path to the earliest ReleaseTag whose
    tree held it; `unreleased` is the sorted paths no tag's tree held."""

    stamps: dict[str, ReleaseTag]
    unreleased: tuple[str, ...]


class StampError(Exception):
    """A refusal. `reason` is `bad-params`, `git-failed` or `tree-unreadable`; the run
    stamps nothing on it."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def frontmatter_value(text: str, key: str) -> str | None:
    """`key`'s value in the first frontmatter block, unquoted; None when absent."""
    n = 0
    prefix = f"{key}:"
    for line in text.splitlines():
        if line == "---":
            n += 1
            if n == 2:
                return None
            continue
        if n == 1 and line.startswith(prefix):
            return line[len(prefix):].strip().strip("'\"")
    return None
