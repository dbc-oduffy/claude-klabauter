"""
coordinator_core.ops.run_commenting_sweep — JSON-RPC "ci.run_commenting_sweep"
operation.

Reports comment shapes the commenting standard bans (changelog, attribution,
task-ref, narration, grep-bait). Detection is
`coordinator_core.commenting.scan_text`; exemption is that module's own
reused `attribution.is_exempt_path`, never duplicated here.

Contract:
    ci.run_commenting_sweep
    params:   {repo_root: str | None, paths: list[str] | None, base: str | None}
    response: {findings: list[dict], files_checked: int}

`findings` items: {file (str, repo-relative, POSIX-style), line (int,
1-indexed), family (str — one of patterns.PATTERNS' keys), text (str — the
matched comment substring)}.

Never blocking: this op reports only. No hook, no pre-commit leg, no
`_GATE_REGISTRY` entry — the commenting standard is enforced as linting and
health-scan reporting, never at commit time.

Scope: `paths` given (a non-empty list, POSIX-relative) scans exactly those
files — a caller wanting a whole-repo pass supplies the full tracked-file
list itself (`git ls-files`, or `tracked_source_files` below). No `paths`
scans only files changed against `base` (default: the merge-base between
HEAD and the remote/local default branch) plus untracked files — one `git
diff --name-only` and one `git ls-files --others`. This is the default
because a repo-wide regex pass over every tracked file does not fit inside
a single-process budget; a diff-scoped pass over what one session actually
touched does.

Every candidate, from either scope, passes through the same filter before a
byte is read: `attribution.is_exempt_path`, plus `.json`/`.ndjson` (no
comments to find) and any path under `.structural-index/` (a generated
index, not source — and, on this repo, alone larger than every other
tracked file combined).

No subprocess spawns the detector itself — `scan_text` is a pure-Python
regex pass, so there is no external-tool budget to stamp here; only the
read-only `git` calls this module makes are bounded, same as the shellcheck
sweep's own `_run_git`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.attribution import is_exempt_path
from coordinator_core.commenting import scan_text
from coordinator_core.ipc import register_op
from coordinator_core.win_portability import no_console_creationflags

_GIT_TIMEOUT_SECONDS = 15

#: Extensions that carry no comments to find — skipped before a byte is
#: read, same reasoning as `.json` (the original AC6 exemption): the
#: detector answers a comment-shape question, and these formats have none.
_SKIP_SUFFIXES = (".json", ".ndjson")

#: Repo-root-anchored generated-content prefixes, never source. On this
#: repo `.structural-index/` alone (a generated symbol index) outweighed
#: every other tracked file's scan time combined.
_SKIP_PREFIXES = (".structural-index/",)

#: Remote/local default-branch candidates tried, in order, when `base` is
#: not supplied and `git symbolic-ref refs/remotes/origin/HEAD` fails (no
#: remote configured — a fresh clone-less throwaway repo, for instance).
_DEFAULT_BRANCH_CANDIDATES = ("origin/main", "origin/master", "main", "master")


def _run_git(repo_root: Path, args: List[str]) -> Optional[str]:
    """Run a read-only `git` subprocess in `repo_root`; return stdout on
    success, None on any failure (not a git repo, `git` missing, timeout,
    or a non-zero exit — e.g. no such ref).
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(
            f"skip: _run_git: proc = subprocess.run(...) failed: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _is_scannable(path: str) -> bool:
    if not path or is_exempt_path(path):
        return False
    if path.endswith(_SKIP_SUFFIXES):
        return False
    if any(path.startswith(prefix) for prefix in _SKIP_PREFIXES):
        return False
    return True


def tracked_source_files(repo_root: Path) -> List[str]:
    """List every git-tracked path under `repo_root` worth scanning
    (repo-relative, forward-slash, sorted ascending). Pass this to `paths`
    for the whole-repo (audit-cadence) form. Empty when `repo_root` is not
    a git worktree.
    """
    stdout = _run_git(repo_root, ["ls-files"])
    if stdout is None:
        return []
    return sorted(p for p in stdout.splitlines() if _is_scannable(p))


def _default_branch_ref(repo_root: Path) -> Optional[str]:
    """The ref `changed_files` diffs against when no `base` is supplied.
    Prefers the remote's own notion of default branch; falls back to a
    fixed candidate list for a repo with no remote configured at all.
    """
    stdout = _run_git(repo_root, ["symbolic-ref", "refs/remotes/origin/HEAD"])
    if stdout:
        ref = stdout.strip()
        if ref:
            return ref
    for candidate in _DEFAULT_BRANCH_CANDIDATES:
        if _run_git(repo_root, ["rev-parse", "--verify", "--quiet", candidate]) is not None:
            return candidate
    return None


def _merge_base(repo_root: Path, ref: str) -> Optional[str]:
    stdout = _run_git(repo_root, ["merge-base", "HEAD", ref])
    if stdout is None:
        return None
    sha = stdout.strip()
    return sha or None


def changed_files(repo_root: Path, base: Optional[str] = None) -> List[str]:
    """Files changed relative to `base` (committed and uncommitted), plus
    every untracked file — the default scope. `base` defaults to the
    merge-base between HEAD and the resolved default branch; when neither
    is resolvable (no remote, no `main`/`master`, a single-commit repo with
    no divergent history), the diff leg contributes nothing and only
    untracked files are reported — never an error, since "nothing to diff
    against" is a legitimate state, not a failure.
    """
    base_ref = base
    if base_ref is None:
        default_ref = _default_branch_ref(repo_root)
        if default_ref is not None:
            base_ref = _merge_base(repo_root, default_ref)

    changed: set[str] = set()
    if base_ref:
        stdout = _run_git(repo_root, ["diff", "--name-only", base_ref, "--"])
        if stdout:
            changed.update(p for p in stdout.splitlines() if p)

    untracked = _run_git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    if untracked:
        changed.update(p for p in untracked.splitlines() if p)

    return sorted(changed)


def _line_number(text: str, offset: int) -> int:
    """1-indexed line number of `offset` within `text`. CRLF-agnostic: only
    `\\n` counts as a break, so a `\\r\\n` file's line count matches an `\\n`
    file's for the same content.
    """
    return text.count("\n", 0, offset) + 1


def _scan_file(repo_root: Path, rel_path: str) -> List[dict]:
    """Read `rel_path` and report every `scan_text` match as a finding.
    Returns [] for an unreadable or non-UTF-8-decodable file rather than
    raising — one unreadable file is skipped, not fatal to the sweep.
    """
    abs_path = repo_root / rel_path
    try:
        raw = abs_path.read_bytes()
    except OSError:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    findings: List[dict] = []
    for match in scan_text(text, rel_path):
        findings.append(
            {
                "file": rel_path,
                "line": _line_number(text, match.span[0]),
                "family": match.pattern,
                "text": match.text,
            }
        )
    return findings


def run_commenting_sweep(
    repo_root: Path,
    paths: Optional[List[str]] = None,
    base: Optional[str] = None,
) -> dict:
    """Scan `paths` if given (whole-repo form: pass `tracked_source_files`'s
    own return), else `changed_files(repo_root, base)` (the default,
    diff-scoped form). See module docstring for the full contract.
    """
    candidates = list(paths) if paths else changed_files(repo_root, base=base)
    files = sorted({p for p in candidates if _is_scannable(p)})
    findings: List[dict] = []
    for rel_path in files:
        findings.extend(_scan_file(repo_root, rel_path))
    return {"findings": findings, "files_checked": len(files)}


@register_op("ci.run_commenting_sweep")
def _run_commenting_sweep(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ci.run_commenting_sweep" handler.

    `params["repo_root"]` takes priority when caller-supplied; falls back to
    the dispatch-supplied `repo_root` kwarg, then `Path.cwd()`.
    """
    param_repo_root = params.get("repo_root")
    if param_repo_root:
        root = Path(param_repo_root)
    elif repo_root is not None:
        root = Path(repo_root)
    else:
        root = Path.cwd()

    paths = params.get("paths") or None
    base = params.get("base") or None
    return run_commenting_sweep(root, paths=paths, base=base)
