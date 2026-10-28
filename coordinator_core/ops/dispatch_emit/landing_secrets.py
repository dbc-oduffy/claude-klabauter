"""Landing refusal scanners for the two refusal kinds: a credential added by the landing
(`secret-in-diff`) and a history-destroying git act (`destructive-git`). Both run in-process, with
no git spawn: HEAD blobs are read through the object reader."""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

SECRET_IN_DIFF = "secret-in-diff"
DESTRUCTIVE_GIT = "destructive-git"

_MAX_SCAN_BYTES = 2_000_000

_PATTERNS: tuple = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
)


class SecretFinding(NamedTuple):
    path: str
    line: int
    label: str


def _head_text(root: Path, rel: str) -> Optional[str]:
    """HEAD's text for `rel`, or None when the path is absent from HEAD or unreadable."""
    from coordinator_core.git import git_state
    from coordinator_core.git.git_objects import read_object

    try:
        spine = git_state.read_tree_spine(root, [rel])
        if spine is None:
            return None
        parts = rel.split("/")
        entry = (spine.get("/".join(parts[:-1])) or {}).get(parts[-1])
        if entry is None:
            return None
        stored = read_object(git_state.resolve_git_common_dir(root), entry[1])
    except (OSError, ValueError, KeyError):
        return None
    if stored is None or stored[0] != "blob":
        return None
    return stored[1].decode("utf-8", errors="replace")


def scan(root: Path, paths: Sequence[str]) -> list:
    """`SecretFinding`s for lines the landing adds to `paths`: worktree lines absent from HEAD's
    copy that match a credential pattern. A binary or oversize file, and a line HEAD already
    holds, is not judged."""
    found: list = []
    for rel in paths:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        if len(data) > _MAX_SCAN_BYTES or b"\0" in data:
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        if not any(p.search(ln) for ln in lines for _label, p in _PATTERNS):
            continue
        head = _head_text(root, rel)
        existing = set(head.splitlines()) if head is not None else set()
        for number, line in enumerate(lines, 1):
            if line in existing:
                continue
            for label, pattern in _PATTERNS:
                if pattern.search(line):
                    found.append(SecretFinding(rel, number, label))
                    break
    return found


def refusal(findings: Sequence[SecretFinding]) -> Optional[str]:
    """The `secret-in-diff` refusal text for the first finding, or None when there are none."""
    if not findings:
        return None
    first = findings[0]
    return (
        f"landing refused {SECRET_IN_DIFF}: {first.path}:{first.line} adds a {first.label}"
        f" ({len(findings)} finding(s)); remove the credential from the file and re-land"
    )


_DESTRUCTIVE_SUBCOMMANDS = frozenset({"filter-branch", "filter-repo", "replace"})


def destructive_git_act(argv: Sequence[str]) -> Optional[str]:
    """The reason `argv` (a git argv, `git` first) rewrites or deletes history, or None. The
    landing path issues no such act today, so this kind is reserved: it refuses a constructed
    argv through this function and has no production caller."""
    args = [a for a in argv[1:] if a]
    while len(args) >= 2 and args[0] in ("-C", "-c"):
        args = args[2:]
    if not args:
        return None
    sub = args[0]
    rest = args[1:]
    if sub in _DESTRUCTIVE_SUBCOMMANDS:
        return sub
    if sub == "reflog" and rest[:1] in (["expire"], ["delete"]):
        return "reflog " + rest[0]
    if sub == "reset" and "--hard" in rest:
        return "reset --hard"
    if sub == "push" and any(a in ("-f", "--force", "--force-with-lease", "--delete", "-d") or a.startswith("+") for a in rest):
        return "push force/delete"
    if sub == "branch" and any(a in ("-D", "-d", "--delete") for a in rest):
        return "branch delete"
    if sub == "update-ref" and "-d" in rest:
        return "update-ref -d"
    if sub == "clean" and any(a.startswith("-") and "f" in a for a in rest):
        return "clean -f"
    if sub == "commit" and "--amend" in rest:
        return "commit --amend"
    return None


def destructive_refusal(argv: Sequence[str]) -> Optional[str]:
    """The `destructive-git` refusal text for `argv`, or None when it is not destructive."""
    why = destructive_git_act(argv)
    if why is None:
        return None
    return f"landing refused {DESTRUCTIVE_GIT}: {why}; land through ceremony.commit_v2 instead"
