"""
coordinator_core.ops.orphan_branch_sweep — read-only work/*|feature/* branch health sweep.

Purpose: enumerate user-owned `work/*` and `feature/*` branches in the current repo,
classify each as CRITICAL (commits added after its PR merged — real orphaned work),
WARNING (no PR yet, ahead of main, and old enough to flag), or OK. Emits one JSON
(or text) line per qualifying branch to stdout. Feeds `/workday-start` and
`merging-to-main`'s pre-merge advisory scan.

Negative-spec: this module never mutates branches, refs, or PRs. It is purely
diagnostic — no `git branch -d`, no `git push`, no PR state changes.

Faithfully-reproduced oracle bug (see `_strip_branch_list_line`): the bash oracle's
`sed 's|^[[:space:]]*||; ...'` strips only leading whitespace, NOT the `"* "`
current-branch marker `git branch --list` prints ahead of the checked-out branch.
A qualifying `work/*`/`feature/*` branch that happens to be the current HEAD is
therefore silently dropped from `seen_branches` (the residual `"* "` prefix fails
the `^(work|feature)/` match). This module reproduces that drop exactly rather than
"fixing" it mid-port — flagged here as a NEGATIVE-SPEC, not a design goal.

Branch-collection iteration order: the bash oracle iterates an unordered bash
associative array (`"${!seen_branches[@]}"`) whose order is bash-hash-defined, not a
documented contract — no caller depends on emission order (each JSON line is parsed
independently). This port iterates in sorted order for determinism; this is a
positive port-time improvement, not a behavior regression.

jq dependency: the bash oracle falls back from `--format json` to `--format text`
when `jq` is absent (with a stderr warning), because it shells out to `jq -cn` to
build each JSON line. This port uses Python's stdlib `json` module directly, so
JSON output no longer depends on an external binary — a positive port-time
simplification, not a silently-dropped behavior (the *feature* — "emit JSON" — is
preserved unconditionally; only the jq-specific degrade-to-text workaround, which
existed solely to route around missing jq, is now unreachable because its trigger
condition no longer exists).

Exit codes:
  0 — normal completion (including: not inside a git repo, `gh` unavailable, `--help`)
  1 — unrecognized CLI argument (usage error)

Port of: orphan-branch-sweep.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: archive/specs/2026-05-01-orphan-branch-prevention.md § 1.1
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

---

Registered JSON-RPC ops (C1c quartet, added 2026-07-22 — fence-inventory
buildout wave 1, chunk w1-orphan-branch-sweep): four read-only git-ancestry
primitives that feed the orphan/unmerged-branch family of callers
(`/workday-start`'s local-unpushed-commits check, `/workday-complete`'s
descendant-tip walk, workday-start-internals' unmerged-work-branch listing,
and `plan-delivery-audit`'s review-window verification). All four reuse this
module's existing `_git`/`_rev_list_count`/`_is_ancestor`/`_tip_sha_for`
primitives (now accepting an optional `cwd` so a caller-injected repo root
can be honored without an `os.chdir()` side effect) rather than
reimplementing git plumbing.

    git_branch.compute_descendant_tip     — walk candidate tips, find the one
                                             descendant of every other (or
                                             report divergence)
    git_branch.detect_unpushed_commits    — local HEAD vs. origin ahead-count
                                             for a work/*|feature/* branch
    git_branch.list_unmerged_work         — work/* branches not merged into
                                             main_ref, scoped to one machine
    git_branch.verify_commit_in_review_window — inclusive-upper/exclusive-
                                             lower ancestry check for a commit

Idempotency: all four are pure read-only `git` queries (`rev-list --count`,
`merge-base --is-ancestor`, `branch --list`, `rev-parse`) over immutable
committed history — no fetch, no ref mutation. Two calls with identical
inputs against an unchanged repo return the identical result by
construction; there is no state for a second invocation to collide with.
(None of these four op-names is rated medium-or-high idempotency/platform
hazard in `distinct-ops-new.tsv`, so a DEC-7 docstring block is not
mandated here — this paragraph is descriptive, not a required note.)

Negative-spec: none of these four handlers perform a `git fetch` — the
oracle prose for `detect-local-unpushed-commits` mentions fetching the
branch first, but the fetch is the CALLER's responsibility; this op only
ever reads refs already present in the local repo, which keeps it
network-free and safe to run inside a throwaway test repo with no
configured remote.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
import time
from pathlib import Path
from typing import Any, Optional, Union

from coordinator_core.ipc import register_op

_PathLike = Union[str, "Path", None]

_GIT_TIMEOUT = 15
_GH_TIMEOUT = 20

_SEVERITY_RANK = {"OK": 0, "WARNING": 1, "CRITICAL": 2}


def _severity_rank(sev: str) -> int:
    return _SEVERITY_RANK.get(sev, 0)


def _run(
    cmd: list[str], timeout: int = _GIT_TIMEOUT, cwd: _PathLike = None
) -> subprocess.CompletedProcess:
    """Run a subprocess with a hang cap and no stdin — mirrors every `git`/`gh` call site.

    All calls here are read-only (git plumbing / `gh pr list`), but `gh` is a
    network call and any child could otherwise block forever on an unattached
    stdin (e.g. an auth prompt) or a hung remote — both violate this script's
    documented "never blocks the caller" contract (`/workday-start` depends on it).

    `cwd` (added for the C1c JSON-RPC quartet, see module docstring): when
    None (every pre-existing call site), behavior is unchanged — the
    subprocess inherits this process's own cwd, exactly as before this
    parameter existed.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
        cwd=cwd,
    )


def _git(
    args: list[str], timeout: int = _GIT_TIMEOUT, cwd: _PathLike = None
) -> subprocess.CompletedProcess:
    return _run(["git", *args], timeout=timeout, cwd=cwd)


def _strip_branch_list_line(line: str) -> str:
    """Mirror `sed 's|^[[:space:]]*||; s|^origin/||; s|^remotes/origin/||'` exactly.

    Faithfully reproduces the oracle's bug: only leading whitespace is stripped,
    NOT git's `"* "` current-branch marker. See module negative-spec.
    """
    stripped = re.sub(r"^[ \t]*", "", line)
    stripped = re.sub(r"^origin/", "", stripped)
    stripped = re.sub(r"^remotes/origin/", "", stripped)
    return stripped


_QUALIFYING_RE = re.compile(r"^(work|feature)/", re.IGNORECASE)


def _collect_branches(raw_list: str, seen: set[str]) -> None:
    for line in raw_list.splitlines():
        branch = _strip_branch_list_line(line)
        if not branch:
            continue
        if branch == "HEAD":
            continue
        if _QUALIFYING_RE.match(branch):
            seen.add(branch)


def _ref_exists(ref: str, cwd: _PathLike = None) -> bool:
    return _git(["rev-parse", ref], cwd=cwd).returncode == 0


def _rev_parse(ref: str, cwd: _PathLike = None) -> str | None:
    res = _git(["rev-parse", ref], cwd=cwd)
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _rev_list_count(*args: str, cwd: _PathLike = None) -> int:
    """`git rev-list --count <args...>`. Callers pass either a single `a..b` range
    expression, or separate positional revs (e.g. `tip_sha`, `^main`) — git requires
    the latter as distinct argv entries; combining them into one string (e.g.
    `f"{tip_sha} ^{main_ref}"`) is a shell-quoting-shaped bug that raises
    'ambiguous argument' and was caught by this port's parity test."""
    res = _git(["rev-list", "--count", *args], cwd=cwd)
    if res.returncode != 0:
        return 0
    try:
        return int(res.stdout.strip())
    except ValueError:
        print(f"skip: _rev_list_count: return int(res.stdout.strip()) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0


def _is_ancestor(candidate_sha: str, of_sha: str, cwd: _PathLike = None) -> bool:
    return _git(["merge-base", "--is-ancestor", candidate_sha, of_sha], cwd=cwd).returncode == 0


def _tip_sha_for(branch: str, cwd: _PathLike = None) -> str | None:
    if _ref_exists(f"refs/heads/{branch}", cwd=cwd):
        return _rev_parse(f"refs/heads/{branch}", cwd=cwd)
    if _ref_exists(f"refs/remotes/origin/{branch}", cwd=cwd):
        return _rev_parse(f"refs/remotes/origin/{branch}", cwd=cwd)
    return None


def _main_ref() -> str | None:
    if _ref_exists("origin/main"):
        return "origin/main"
    if _ref_exists("main"):
        return "main"
    return None


def _emit_result(
    branch: str,
    severity: str,
    ahead: int,
    age_h: int,
    pr: dict[str, Any] | None,
    orphan_after_merge: int,
    *,
    fmt: str,
    min_rank: int,
    out: Any = None,
) -> None:
    # `out` is resolved HERE, not via a `sys.stdout`-valued default parameter —
    # a default bound at function-definition (import) time captures a stale
    # reference that survives `sys.stdout` being swapped later (e.g. by a
    # test's `capsys`/`capfd` fixture), silently routing output around the
    # capture. Caught by this port's own test suite when capsys.readouterr()
    # returned empty despite pytest's own capture panel showing output.
    if out is None:
        out = sys.stdout
    if _severity_rank(severity) < min_rank:
        return

    if fmt == "text":
        if severity == "CRITICAL":
            out.write(
                f"{severity} {branch} | ahead={ahead} age_h={age_h}h | "
                f"pr={json.dumps(pr) if pr is not None else 'null'} | "
                f"orphan_commits={orphan_after_merge}\n"
            )
        elif severity == "WARNING":
            out.write(f"{severity} {branch} | ahead={ahead} age_h={age_h}h | no_pr\n")
        else:
            out.write(f"OK {branch} | ahead={ahead} age_h={age_h}h\n")
    else:
        obj = {
            "branch": branch,
            "ahead": ahead,
            "age_h": age_h,
            "pr": pr,
            "orphan_after_merge": orphan_after_merge,
            "severity": severity,
        }
        out.write(json.dumps(obj, separators=(",", ":")) + "\n")


_HELP_TEXT = """Usage: orphan-branch-sweep.sh [OPTIONS]

Options:
  --format json|text          Output format (default: json)
  --severity-min ok|warning|critical  Minimum severity to emit (default: ok)
  --include-remote / --no-include-remote  Include origin/* branches (default: on)
  --max-age-days N            Ignore branches older than N days (default: 30)
  --help                      Show this help

Outputs one line per qualifying branch. Exits 0 always (even when gh unavailable).
"""


def main(argv: list[str]) -> int:
    fmt = "json"
    severity_min = "ok"
    include_remote = True
    max_age_days = 30

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--format":
            if i + 1 >= len(argv):
                print(f"Unknown argument: {arg}", file=sys.stderr)
                return 1
            fmt = argv[i + 1]
            i += 2
        elif arg == "--severity-min":
            if i + 1 >= len(argv):
                print(f"Unknown argument: {arg}", file=sys.stderr)
                return 1
            severity_min = argv[i + 1]
            i += 2
        elif arg == "--include-remote":
            include_remote = True
            i += 1
        elif arg == "--no-include-remote":
            include_remote = False
            i += 1
        elif arg == "--max-age-days":
            if i + 1 >= len(argv):
                print(f"Unknown argument: {arg}", file=sys.stderr)
                return 1
            try:
                max_age_days = int(argv[i + 1])
            except ValueError:
                print(f"Unknown argument: {arg}", file=sys.stderr)
                return 1
            i += 2
        elif arg in ("--help", "-h"):
            print(_HELP_TEXT, end="")
            return 0
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            return 1

    # Guard: must be inside a git repo.
    if _run(["git", "rev-parse", "--is-inside-work-tree"]).returncode != 0:
        return 0

    gh_available = shutil.which("gh") is not None

    min_rank = _severity_rank(severity_min.upper())

    user_email_res = _git(["config", "user.email"])
    user_email = user_email_res.stdout.strip() if user_email_res.returncode == 0 else ""

    seen_branches: set[str] = set()
    local_res = _git(["branch", "--list", "work/*", "feature/*"])
    if local_res.returncode == 0:
        _collect_branches(local_res.stdout, seen_branches)
    if include_remote:
        remote_res = _git(["branch", "--list", "-r", "origin/work/*", "origin/feature/*"])
        if remote_res.returncode == 0:
            _collect_branches(remote_res.stdout, seen_branches)

    now = int(time.time())
    max_age_secs = max_age_days * 86400
    main_ref = _main_ref()
    head_sha = _rev_parse("HEAD")

    for branch in sorted(seen_branches):
        tip_sha = _tip_sha_for(branch)
        if tip_sha is None:
            continue

        if user_email:
            author_res = _git(["log", "-1", "--format=%ae", tip_sha])
            tip_author = author_res.stdout.strip() if author_res.returncode == 0 else ""
            if tip_author != user_email:
                continue

        ct_res = _git(["log", "-1", "--format=%ct", tip_sha])
        try:
            tip_ct = int(ct_res.stdout.strip()) if ct_res.returncode == 0 else now
        except ValueError:
            tip_ct = now
        age_secs = now - tip_ct

        if age_secs > max_age_secs:
            continue

        age_h = age_secs // 3600

        ahead = 0
        if main_ref is not None:
            ahead = _rev_list_count(f"{main_ref}..{tip_sha}")

        pr_json: dict[str, Any] | None = None
        pr_state = ""
        pr_merged_at = ""
        orphan_after_merge = 0

        if gh_available:
            pr_res = _run(
                [
                    "gh", "pr", "list", "--head", branch, "--state", "all",
                    "--limit", "5", "--json", "number,state,mergedAt,mergeCommit",
                ],
                timeout=_GH_TIMEOUT,
            )
            pr_raw = pr_res.stdout.strip() if pr_res.returncode == 0 else ""
            if pr_raw and pr_raw != "[]":
                try:
                    prs = json.loads(pr_raw)
                except json.JSONDecodeError:
                    prs = []
                if prs:
                    p = prs[-1]
                    pr_number = p.get("number", "")
                    pr_state = p.get("state", "") or ""
                    pr_merged_at = p.get("mergedAt") or ""
                    pr_json = {
                        "number": pr_number if pr_number != "" else 0,
                        "state": pr_state,
                        "merged_at": pr_merged_at,
                    }

                    if pr_state == "MERGED" and pr_merged_at:
                        log_res = _git(
                            ["log", tip_sha, f"--after={pr_merged_at}", "--format=%H"]
                        )
                        if log_res.returncode == 0:
                            orphan_after_merge = len(
                                [ln for ln in log_res.stdout.splitlines() if ln.strip()]
                            )

        # ---------------------------------------------------------------
        # Classify severity
        # ---------------------------------------------------------------
        severity = "OK"

        if pr_state == "MERGED" and orphan_after_merge > 0:
            unmerged = 0
            if main_ref is not None:
                unmerged = _rev_list_count(tip_sha, f"^{main_ref}")

            if unmerged > 0:
                carried_forward = False
                if head_sha and head_sha != tip_sha:
                    if _is_ancestor(tip_sha, head_sha):
                        carried_forward = True
                if not carried_forward:
                    for other in seen_branches:
                        if other == branch:
                            continue
                        other_tip = _tip_sha_for(other)
                        if not other_tip or other_tip == tip_sha:
                            continue
                        if _is_ancestor(tip_sha, other_tip):
                            carried_forward = True
                            break
                severity = "OK" if carried_forward else "CRITICAL"
            else:
                severity = "OK"
        elif pr_state != "MERGED" and ahead > 0:
            branch_age_days = age_secs // 86400
            if branch_age_days >= 2 or age_h > 36:
                severity = "WARNING"

        _emit_result(
            branch, severity, ahead, age_h, pr_json, orphan_after_merge,
            fmt=fmt, min_rank=min_rank,
        )

    return 0


# ---------------------------------------------------------------------------
# C1c quartet — JSON-RPC ops (see module docstring "Registered JSON-RPC ops").
# ---------------------------------------------------------------------------


def _resolve_repo_dir(params: dict, repo_root: Optional[Path], key: str = "repo_root") -> _PathLike:
    """Resolve the directory git commands should run in for a handler call.

    Preference order: an explicit `params[key]` (part of this op's contract,
    where present) beats the IPC-injected `repo_root` (from `_OP_KEY_SCOPE`),
    which beats `None` (process cwd) — the injected value is a scope-derived
    default, not an override of an explicit caller-supplied path.
    """
    explicit = params.get(key)
    if explicit:
        return explicit
    if repo_root is not None:
        return repo_root
    return None


def compute_descendant_tip(candidate_tips: list[str], cwd: _PathLike = None) -> dict:
    """Walk *candidate_tips*, returning the one that is a descendant of (or
    equal to) every other candidate via `git merge-base --is-ancestor`.

    Mirrors `workday-complete.md:464`'s per-date actual_tips walk: when no
    single tip dominates every other (the branches diverged), there is no
    correct answer — report `diverged: true` with a null `descendant_tip`
    rather than guessing one.
    """
    tips = [t for t in candidate_tips if t]
    if not tips:
        return {"descendant_tip": None, "diverged": False}
    if len(tips) == 1:
        return {"descendant_tip": tips[0], "diverged": False}

    for candidate in tips:
        dominates_all = True
        for other in tips:
            if other == candidate:
                continue
            if not _is_ancestor(other, candidate, cwd=cwd):
                dominates_all = False
                break
        if dominates_all:
            return {"descendant_tip": candidate, "diverged": False}

    return {"descendant_tip": None, "diverged": True}


@register_op("git_branch.compute_descendant_tip")
async def _compute_descendant_tip_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `git_branch.compute_descendant_tip` handler.

    params: {candidate_tips: list[str]} -> {descendant_tip: str | null, diverged: bool}
    Read-only; no fetch, no ref mutation.
    """
    candidate_tips = params.get("candidate_tips") or []
    cwd = _resolve_repo_dir(params, repo_root)
    return await asyncio.to_thread(compute_descendant_tip, candidate_tips, cwd=cwd)


def _current_branch(cwd: _PathLike = None) -> Optional[str]:
    res = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if res.returncode != 0:
        return None
    branch = res.stdout.strip()
    return None if not branch or branch == "HEAD" else branch


_QUALIFYING_WORK_FEATURE_RE = re.compile(r"^(work|feature)/")


def detect_unpushed_commits(branch: Optional[str], cwd: _PathLike = None) -> dict:
    """Compare local HEAD (or *branch*) against its `origin/<branch>` ref.

    Branch-prefix gate mirrors `workday-start.md:979`: only `work/*` and
    `feature/*` branches are in scope for this check (the "current work
    session" branch families). A non-qualifying branch is reported as-is
    with `ahead_count: 0` rather than silently substituting a different
    branch.

    Does NOT `git fetch` — see module docstring negative-spec; this reads
    whatever `origin/<branch>` ref is already present locally.
    """
    resolved_branch = branch or _current_branch(cwd=cwd)
    if not resolved_branch:
        return {"branch": "", "ahead_count": 0, "has_origin": False}

    has_origin = _ref_exists(f"refs/remotes/origin/{resolved_branch}", cwd=cwd)

    if not _QUALIFYING_WORK_FEATURE_RE.match(resolved_branch):
        return {"branch": resolved_branch, "ahead_count": 0, "has_origin": has_origin}

    if has_origin:
        ahead = _rev_list_count(
            f"refs/remotes/origin/{resolved_branch}..refs/heads/{resolved_branch}", cwd=cwd
        )
    else:
        ahead = _rev_list_count(f"refs/heads/{resolved_branch}", cwd=cwd)

    return {"branch": resolved_branch, "ahead_count": ahead, "has_origin": has_origin}


@register_op("git_branch.detect_unpushed_commits")
async def _detect_unpushed_commits_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `git_branch.detect_unpushed_commits` handler.

    params: {repo_root: str, branch: str | null} -> {branch: str, ahead_count: int, has_origin: bool}
    `repo_root` in params is this op's own contractual target-directory
    override (show_top scope: the caller's checked-out worktree); it takes
    precedence over the IPC-injected `repo_root` per `_resolve_repo_dir`.
    """
    branch = params.get("branch") or None
    cwd = _resolve_repo_dir(params, repo_root)
    return await asyncio.to_thread(detect_unpushed_commits, branch, cwd=cwd)


_BRANCH_LIST_MARKER_RE = re.compile(r"^[* ]*")


def list_unmerged_work_branches(machine: str, main_ref: str, cwd: _PathLike = None) -> list[str]:
    """`git branch --list "work/*" --no-merged <main_ref>`, filtered to the
    branches scoped to *machine* (`work/<machine>/...`), case-insensitively.

    Native replacement for `workday-start-internals.md:197`'s
    `grep -i "^[* ]*work/${MACHINE}/"` — same case-fold + machine-scoping
    semantics via `str.lower()`, no `grep` dependency (that pipeline's own
    comment flags bash's `shopt nocasematch` as non-portable).
    """
    res = _git(["branch", "--list", "work/*", "--no-merged", main_ref], cwd=cwd)
    if res.returncode != 0:
        return []

    prefix = f"work/{machine}/".lower()
    matches: list[str] = []
    for line in res.stdout.splitlines():
        branch = _BRANCH_LIST_MARKER_RE.sub("", line).strip()
        if not branch:
            continue
        if branch.lower().startswith(prefix):
            matches.append(branch)
    return sorted(matches)


@register_op("git_branch.list_unmerged_work")
async def _list_unmerged_work_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `git_branch.list_unmerged_work` handler.

    params: {machine: str, main_ref: str} -> {branches: list[str]}
    """
    machine = params.get("machine") or ""
    main_ref = params.get("main_ref") or "main"
    cwd = _resolve_repo_dir(params, repo_root)
    branches = await asyncio.to_thread(list_unmerged_work_branches, machine, main_ref, cwd=cwd)
    return {"branches": branches}


def verify_commit_in_review_window(
    commit_sha: str, lower_bound_sha: str, upper_bound_sha: str, cwd: _PathLike = None
) -> dict:
    """Inclusive-upper / exclusive-lower ancestry check for *commit_sha*.

    A commit is "in window" iff it is strictly AFTER `lower_bound_sha`
    (exclusive — the lower bound itself does not count as covered) and AT OR
    BEFORE `upper_bound_sha` (inclusive — equal to the upper bound counts).
    Named, tested helper per the oracle rationale (`plan-delivery-audit/
    SKILL.md:93`) rather than re-deriving this boundary logic inline each
    audit run.
    """
    if commit_sha == lower_bound_sha:
        lower_ok = False
    else:
        lower_ok = _is_ancestor(lower_bound_sha, commit_sha, cwd=cwd)

    upper_ok = commit_sha == upper_bound_sha or _is_ancestor(commit_sha, upper_bound_sha, cwd=cwd)

    return {"in_window": bool(lower_ok and upper_ok)}


@register_op("git_branch.verify_commit_in_review_window")
async def _verify_commit_in_review_window_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `git_branch.verify_commit_in_review_window` handler.

    params: {commit_sha: str, lower_bound_sha: str, upper_bound_sha: str} -> {in_window: bool}
    """
    commit_sha = params.get("commit_sha") or ""
    lower_bound_sha = params.get("lower_bound_sha") or ""
    upper_bound_sha = params.get("upper_bound_sha") or ""
    cwd = _resolve_repo_dir(params, repo_root)
    return await asyncio.to_thread(
        verify_commit_in_review_window, commit_sha, lower_bound_sha, upper_bound_sha, cwd=cwd
    )
