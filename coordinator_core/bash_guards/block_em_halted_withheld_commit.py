"""Deny an EM `git commit` that would land a still-halted run's withheld paths.

Main-loop only: any subagent-shaped caller (executor, `coordinator:git-commit-agent`) is left to
`block_subagent_commit`, and engine terminal commits never pass through a Bash hook. The withheld
set comes from `halted_withheld_index` (one small read per repo; no `state/warp` scan). An explicit
pathspec, or a `git add` earlier in the same chain, is matched without spawning git; a bare or `-a`
commit falls to `git diff --cached --name-only` (plus `git diff --name-only` for `-a`) and only
while the index is non-empty. Commit detection reuses `block_subagent_commit`'s tokenizer seams.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    token_matches_binary,
    tokenize_full_command,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    expand_start_process_invocations,
    tokenize_command,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
# Private seams of block_subagent_commit, shared on purpose so both guards tokenize commits alike.
from coordinator_core.bash_guards.block_subagent_commit import (
    _GIT_GLOBAL_OPTS_WITH_SEP_ARG,
    _normalize_windows_argv0_head_path_with_spaces,
    _normalize_windows_git_argv0,
    _pathspec_tokens_before_redirection,
    _peeled_effective_tokens,
    _segments_from_tokens,
    _strip_heredoc_bodies,
    _wrapped_shell_c_payload_legs,
)

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

GUARD_NAME = "block-em-halted-withheld-commit"

_MAX_UNWRAP_DEPTH = 3

_COMMIT_VALUE_FLAGS = frozenset(
    {
        "-m", "-F", "-C", "-c", "-t", "--message", "--file", "--reuse-message",
        "--reedit-message", "--template", "--author", "--date", "--cleanup",
        "--fixup", "--squash", "--trailer",
    }
)
_COMMIT_VALUE_LETTERS = "mFCct"
_ADD_SWEEP_FLAGS = frozenset({"-A", "--all", "-u", "--update"})
_GLOB_CHARS = frozenset("*?[")


@dataclass
class _Scan:
    """Per-check state: index and git-spawn budget memoised by repo root."""

    cwd: str
    indexes: Dict[str, Dict[str, str]]
    hit: Optional[Tuple[str, str]] = None


def _index_for(scan: _Scan, root: str) -> Dict[str, str]:
    if root not in scan.indexes:
        from coordinator_core.ops.dispatch_emit.halted_withheld_index import withheld_by_path

        try:
            scan.indexes[root] = withheld_by_path(root)
        except Exception as exc:
            print(f"{GUARD_NAME}: withheld index unreadable ({exc!r}); guard inactive", file=sys.stderr)
            scan.indexes[root] = {}
    return scan.indexes[root]


def _repo_root(cwd: str) -> Optional[str]:
    from coordinator_core.subagent_sandbox import resolve_git_root_cheap

    return resolve_git_root_cheap(cwd)


def _rel_to_root(token: str, cwd: str, root: str) -> Optional[str]:
    """Repo-relative posix form of a pathspec token, or None when it points outside the repo."""
    text = token.replace("\\", "/")
    if not text or text.startswith(":"):
        return None
    joined = text if os.path.isabs(text) else os.path.join(cwd, text)
    try:
        rel = os.path.relpath(os.path.normpath(joined), root).replace("\\", "/")
    except ValueError:
        return None
    if rel == ".." or rel.startswith("../"):
        return None
    return rel


def _spec_hits(rel: str, indexed: Dict[str, str], root: str) -> List[str]:
    """Indexed paths a pathspec names: exact, or (when it is `.`, a glob or a directory) the
    indexed paths under it; a withheld deletion is matched the same as a withheld edit."""
    if rel == ".":
        found = list(indexed)
    elif any(c in _GLOB_CHARS for c in rel):
        found = [p for p in indexed if fnmatch.fnmatchcase(p, rel)]
    elif rel in indexed:
        return [rel]
    else:
        prefix = rel.rstrip("/") + "/"
        found = [p for p in indexed if p.startswith(prefix)]
    return sorted(found)


def _git_names(root: str, *args: str) -> List[str]:
    from coordinator_core.git.run import run_git

    proc = run_git(["-C", root, *args])
    if proc.returncode != 0:
        return []
    return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]


def _merge_in_progress(root: str) -> bool:
    dot_git = os.path.join(root, ".git")
    if os.path.isfile(dot_git):
        try:
            with open(dot_git, encoding="utf-8") as fh:
                line = fh.readline().strip()
        except OSError:
            return False
        if not line.startswith("gitdir:"):
            return False
        gitdir = line[len("gitdir:"):].strip()
        dot_git = gitdir if os.path.isabs(gitdir) else os.path.join(root, gitdir)
    return os.path.exists(os.path.join(dot_git, "MERGE_HEAD"))


def _drop_landed_merge_paths(root: str, hits: Set[str]) -> Set[str]:
    """Hits whose staged blob matches neither MERGE_HEAD's nor HEAD's blob (one batched cat-file).
    An unreadable answer keeps every hit."""
    from coordinator_core.git.run import run_git

    ordered = sorted(hits)
    specs = [f"{prefix}{p}" for p in ordered for prefix in (":", "MERGE_HEAD:", "HEAD:")]
    proc = run_git(
        ["-C", root, "cat-file", "--batch-check"],
        input=("\n".join(specs) + "\n").encode("utf-8"),
    )
    lines = proc.stdout.splitlines() if proc.returncode == 0 else []
    if len(lines) != len(specs):
        return hits

    def blob(line: str) -> Optional[str]:
        parts = line.split()
        return parts[0] if len(parts) == 3 and parts[1] == "blob" else None

    kept: Set[str] = set()
    for i, path in enumerate(ordered):
        staged, merge, head = (blob(lines[3 * i + k]) for k in range(3))
        if staged != merge and staged != head:
            kept.add(path)
    return kept


def _git_subcommand(tokens: Sequence[str]) -> Optional[Tuple[str, Optional[str], List[str]]]:
    """`(subcommand, -C dir, args after it)` for a segment whose head is `git`, else None."""
    peeled = _peeled_effective_tokens(list(tokens))
    if not peeled or not token_matches_binary(peeled[0], "git"):
        return None
    rest = peeled[1:]
    dash_c: Optional[str] = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not tok.startswith("-"):
            return tok, dash_c, rest[i + 1 :]
        if tok in _GIT_GLOBAL_OPTS_WITH_SEP_ARG:
            if tok == "-C" and i + 1 < len(rest):
                dash_c = rest[i + 1]
            i += 2
            continue
        i += 1
    return None


def _parse_commit(args: List[str]) -> Tuple[List[str], bool]:
    """`(pathspec tokens, commits -a)` from the tokens after `git commit`."""
    args = _pathspec_tokens_before_redirection(args)
    sep = args.index("--") if "--" in args else len(args)
    head, tail = args[:sep], args[sep + 1 :]
    pathspecs: List[str] = []
    sweeping = False
    i = 0
    while i < len(head):
        tok = head[i]
        i += 1
        if tok in _COMMIT_VALUE_FLAGS:
            i += 1
        elif tok == "--all":
            sweeping = True
        elif tok.startswith("--"):
            continue
        elif tok.startswith("-") and len(tok) > 1:
            for pos, ch in enumerate(tok[1:], start=1):
                if ch == "a":
                    sweeping = True
                if ch in _COMMIT_VALUE_LETTERS:
                    if pos == len(tok) - 1:
                        i += 1
                    break
        else:
            pathspecs.append(tok)
    return pathspecs + tail, sweeping


def _add_pathspecs(args: List[str]) -> List[str]:
    """Pathspecs a `git add` stages; a sweeping flag with none names `.`."""
    args = _pathspec_tokens_before_redirection(args)
    sep = args.index("--") if "--" in args else len(args)
    paths = [t for t in args[:sep] if not t.startswith("-")] + args[sep + 1 :]
    if not paths and any(t in _ADD_SWEEP_FLAGS for t in args[:sep]):
        return ["."]
    return paths


def _segment_cwd(scan: _Scan, tokens: Sequence[str]) -> None:
    peeled = _peeled_effective_tokens(list(tokens))
    if len(peeled) == 2 and peeled[0] == "cd":
        target = peeled[1].replace("\\", "/")
        scan.cwd = target if os.path.isabs(target) else os.path.join(scan.cwd, target)


def _scan_text(cmd: str, scan: _Scan, depth: int) -> None:
    normalized = _normalize_windows_git_argv0(cmd)
    tokens = tokenize_full_command(normalized, preserve_windows_backslashes=True)
    if tokens is None:
        return
    staged: Dict[str, Set[str]] = {}
    for seg in _segments_from_tokens(tokens):
        if scan.hit is not None:
            return
        if not seg:
            continue
        _segment_cwd(scan, seg)
        sub = _git_subcommand(seg)
        if sub is None:
            continue
        name, dash_c, args = sub
        if name not in ("add", "commit"):
            continue
        cwd = scan.cwd
        if dash_c:
            cwd = dash_c if os.path.isabs(dash_c) else os.path.join(cwd, dash_c)
        root = _repo_root(cwd)
        if root is None:
            continue
        indexed = _index_for(scan, root)
        if not indexed:
            continue
        if name == "add":
            for spec in _add_pathspecs(args):
                rel = _rel_to_root(spec, cwd, root)
                if rel is not None:
                    staged.setdefault(root, set()).update(
                        _spec_hits(rel, indexed, root)
                    )
            continue
        specs, sweeping = _parse_commit(args)
        hits: Set[str] = set()
        if specs:
            for spec in specs:
                rel = _rel_to_root(spec, cwd, root)
                if rel is not None:
                    hits.update(_spec_hits(rel, indexed, root))
        else:
            hits.update(staged.get(root, set()))
            if not hits:
                hits.update(p for p in _git_names(root, "diff", "--cached", "--name-only") if p in indexed)
            if not hits and sweeping:
                hits.update(p for p in _git_names(root, "diff", "--name-only") if p in indexed)
        if hits and _merge_in_progress(root):
            hits = _drop_landed_merge_paths(root, hits)
        if hits:
            first = sorted(hits)[0]
            scan.hit = (indexed[first], first if len(hits) == 1 else f"{first} (+{len(hits) - 1} more)")
            return
    if depth >= _MAX_UNWRAP_DEPTH:
        return
    for payload, _leg in _wrapped_shell_c_payload_legs(normalized):
        _scan_text(payload, scan, depth + 1)
        if scan.hit is not None:
            return


def _deny_record(run_id: str, path: str) -> Any:
    from coordinator_core.bash_guards._rewrite_support import _deny_record as _build
    from coordinator_core.bash_guards._rewrite_support import _offer

    text = (
        f"BLOCKED: halted run {run_id} withholds {path}; a hand commit would land it. "
        f"Alternative: re-fire the run, or workflow.revise {run_id}."
    )
    return _build(
        GUARD_NAME,
        text,
        (_offer(f"workflow.revise {run_id}", "the revision text"),),
        em_text=text,
    )


def check(payload: Dict[str, Any]) -> Any:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")
    cmd_for_scan = _normalize_windows_argv0_head_path_with_spaces(_strip_heredoc_bodies(cmd))
    if "commit" not in cmd_for_scan:
        return None
    dialect = dialect_from_tool_name(payload.get("tool_name"))
    if dialect is Dialect.POWERSHELL:
        ps_tokens = tokenize_command(cmd_for_scan, dialect, guard_name=GUARD_NAME)
        if ps_tokens is not None:
            cmd_for_scan = " ".join(expand_start_process_invocations(ps_tokens))

    from coordinator_core.bash_guards.check_test_suite_invocation import _caller_is_subagent

    if _caller_is_subagent(payload):
        return None

    cwd = payload.get("cwd")
    scan = _Scan(cwd=cwd if isinstance(cwd, str) and cwd else os.getcwd(), indexes={})
    _scan_text(cmd_for_scan, scan, 0)
    if scan.hit is None:
        return None
    return _deny_record(*scan.hit)
