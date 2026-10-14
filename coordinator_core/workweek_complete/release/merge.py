"""The pinned merge: push the candidate, open the pull request once, merge only that head.

The only module in the release package whose children write to a remote, so `ARGV` is the closed
table of every child it can start. No retry, no wait loop, no other merge strategy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, NamedTuple, Optional, Tuple

from coordinator_core.workweek_complete.release.contract import MergeResult

ARGV: Dict[str, Tuple[str, ...]] = {
    "push": ("git", "-C", "{repo_root}", "push", "origin", "{candidate}:refs/heads/{branch}"),
    "pr-list": (
        "gh", "pr", "list", "--head", "{branch}", "--state", "all",
        "--json", "number,state,headRefOid,mergeCommit",
    ),
    "pr-create": (
        "gh", "pr", "create", "--base", "{base}", "--head", "{branch}",
        "--title", "{title}", "--body-file", "{body_file}",
    ),
    "pr-merge": ("gh", "pr", "merge", "{number}", "--merge", "--match-head-commit", "{candidate}"),
    "pr-view": ("gh", "pr", "view", "{number}", "--json", "state,headRefOid,mergeCommit"),
}


class MergeRefused(Exception):
    """The merge act stopped; `reason` is the first token, `detail` the child's own words."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class Child(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


Run = Callable[[Tuple[str, ...], str], Child]


def _default_run(argv: Tuple[str, ...], cwd: str) -> Child:
    if argv[0] == "git":
        from coordinator_core.git.run import run_git

        res = run_git(list(argv[1:]), cwd=cwd, remote=True)
        return Child(res.returncode, res.stdout, res.stderr)
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    try:
        proc = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True, check=False, timeout=120,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MergeRefused("merge-refused", f"{argv[0]} did not run to completion: {exc}") from None
    return Child(proc.returncode, proc.stdout or "", proc.stderr or "")


def _post_coverage_status(repo_root: str, candidate: str, base: str) -> None:
    from coordinator_core.ops import post_coverage_status
    from coordinator_core.ops.ceremony import push

    owner_repo = push._resolve_github_owner_repo(Path(repo_root))
    if owner_repo is None:
        raise MergeRefused("status-not-posted", "owner/repo not resolvable from origin")
    result = post_coverage_status.post_coverage_status(
        owner_repo[0], owner_repo[1], candidate, f"origin/{base}..{candidate}", repo_root=repo_root
    )
    if not (result.posted and result.state == "success"):
        raise MergeRefused("status-not-posted", str(getattr(result, "reason", "") or result.state))


def _argv(name: str, **fields: str) -> Tuple[str, ...]:
    return tuple(part.format(**fields) for part in ARGV[name])


def _json(child: Child, what: str):
    try:
        return json.loads(child.stdout or "null")
    except ValueError:
        raise MergeRefused("merge-not-confirmed", f"{what} printed unparseable JSON") from None


def _merge_oid(pr: dict) -> str:
    commit = pr.get("mergeCommit") or {}
    return str(commit.get("oid") or "")


def merge_pinned(
    repo_root: str,
    *,
    candidate: str,
    branch: str,
    title: str,
    body: str,
    run_dir: str,
    base: str = "main",
    post_status: Optional[Callable[[str, str, str], None]] = _post_coverage_status,
    run: Run = _default_run,
) -> MergeResult:
    """Merge the pull request for `branch` only if its head is `candidate`; raise `MergeRefused` otherwise.

    `run_dir` receives the pull request body file. `post_status=None` skips the coverage status
    leg for a repo without the ruleset.
    """
    root = str(repo_root)
    listed = run(_argv("pr-list", branch=branch), root)
    if listed.returncode != 0:
        raise MergeRefused("merge-not-confirmed", f"gh pr list failed: {listed.stderr.strip()}")
    prs = [p for p in (_json(listed, "gh pr list") or []) if isinstance(p, dict)]
    for pr in prs:
        if pr.get("headRefOid") == candidate and pr.get("state") == "MERGED" and _merge_oid(pr):
            return MergeResult(_merge_oid(pr), int(pr["number"]), False)

    pushed = run(_argv("push", repo_root=root, candidate=candidate, branch=branch), root)
    if pushed.returncode != 0:
        raise MergeRefused("push-rejected", pushed.stderr.strip())

    open_prs = [p for p in prs if p.get("state") == "OPEN"]
    if open_prs:
        number = int(open_prs[0]["number"])
    else:
        body_file = Path(run_dir) / f"pr-body-{candidate[:12]}.md"
        body_file.parent.mkdir(parents=True, exist_ok=True)
        body_file.write_text(body, encoding="utf-8")
        created = run(
            _argv("pr-create", base=base, branch=branch, title=title, body_file=str(body_file)), root
        )
        if created.returncode != 0:
            raise MergeRefused("merge-refused", created.stderr.strip())
        tail = created.stdout.strip().rsplit("/", 1)[-1]
        if not tail.isdigit():
            raise MergeRefused("merge-refused", f"no pull request number in {created.stdout.strip()!r}")
        number = int(tail)

    if post_status is not None:
        post_status(root, candidate, base)

    merged = run(_argv("pr-merge", number=str(number), candidate=candidate), root)
    if merged.returncode != 0:
        raise MergeRefused("merge-refused", merged.stderr.strip())

    viewed = run(_argv("pr-view", number=str(number)), root)
    if viewed.returncode != 0:
        raise MergeRefused("merge-not-confirmed", viewed.stderr.strip())
    pr = _json(viewed, "gh pr view")
    if not (
        isinstance(pr, dict)
        and pr.get("state") == "MERGED"
        and pr.get("headRefOid") == candidate
        and _merge_oid(pr)
    ):
        raise MergeRefused("merge-not-confirmed", f"pull request {number} is not merged at {candidate}")
    return MergeResult(_merge_oid(pr), number, True)
