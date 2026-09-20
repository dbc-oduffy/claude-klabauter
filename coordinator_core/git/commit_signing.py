"""Commit-signature resolution and the one signed-object write.

`commit.gpgsign` is porcelain-only: `git commit` reads it, `git commit-tree`
does not, and the native commit-object writers in this package read nothing
at all. Every seam that lands a commit without porcelain therefore has to
replay the decision itself, or a repo whose every `git commit` signs quietly
accumulates unsigned commits from the engine.

Two halves, deliberately split: resolving WHETHER to sign is spawn-free and
sits on the brightline path; producing the signature costs exactly one
`git commit-tree -S`, reached only when the answer is yes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.win_portability import no_console_creationflags


class SigningFailed(RuntimeError):
    """Kept importable for backward compatibility only -- nothing in this
    module raises it any more. DR-308 (`docs/decisions/DR-308-signed-
    commit-enforcement-is-declined-as.md`) declines a mechanism that can
    ever block a commit on a signing failure: `write_signed_commit_object`
    now returns a `(None, warning)` pair on ANY signing failure and the
    caller falls through to the unsigned write, matching `coordinator_
    core.git.commit._sign_commit_tree`'s contract exactly."""


_TRUE_SPELLINGS = frozenset({"true", "yes", "on", "1"})
_FALSE_SPELLINGS = frozenset({"false", "no", "off", "0", ""})


def _strip_inline_comment(line: str) -> str:
    """Drop a git-config inline comment (`#` or `;`) from `line`.

    Both characters are literal inside a quoted value, so quoting is tracked
    rather than assumed absent. Without this, `gpgsign = true # note` -- a
    form git itself accepts -- reads as the token `true # note`, which is in
    neither spelling set and would report a signing repo as unknown.
    """
    out = []
    in_quotes = False
    escaped = False
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
        elif ch in "#;" and not in_quotes:
            break
        out.append(ch)
    return "".join(out)


def _read_config_bool(config_path: Path, section: str, key: str) -> Optional[bool]:
    """Best-effort single boolean read from one git config file -- same
    line-scanning shape and same scope limits as `_read_config_user_section`
    (no include chains, no `includeIf`, no system config).

    A value in neither spelling set returns None rather than False: this
    answer decides whether a commit gets signed, and reporting an
    unrecognised value as "signing is off" is the one wrong answer that
    ships silently.
    """
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_section = False
    found: Optional[bool] = None
    for line in text.splitlines():
        stripped = _strip_inline_comment(line).strip()
        if stripped.startswith("["):
            in_section = stripped.lower().startswith(f"[{section}]")
            continue
        if not in_section or not stripped:
            continue
        raw_key, sep, value = stripped.partition("=")
        if raw_key.strip().lower() != key:
            continue
        if not sep:
            found = True
            continue
        normalized = value.strip().strip('"').lower()
        if normalized in _TRUE_SPELLINGS:
            found = True
        elif normalized in _FALSE_SPELLINGS:
            found = False
        else:
            found = None
    return found


def commit_signing_enabled(root: Path) -> bool:
    """Whether `commit.gpgsign` is on for `root` -- resolved spawn-free from
    the same two config files `_resolve_commit_identity` reads, in git's own
    local-over-global precedence.

    Negative spec -- this is DELIBERATELY narrower than git's own resolution
    and a miss returns False, which is exactly today's behaviour. It must
    never spawn to resolve: the seams it guards are on the brightline budget.
    """
    for config_path in (
        resolve_git_common_dir(root) / "config",
        Path.home() / ".gitconfig",
    ):
        value = _read_config_bool(config_path, "commit", "gpgsign")
        if value is not None:
            return value
    return False


def sign_flag_args(root: Path) -> List[str]:
    """`["-S"]` when `root` signs its commits, else `[]` -- the argument
    splice every `git commit-tree` call site uses to keep plumbing-landed
    commits signed in step with porcelain-landed ones."""
    return ["-S"] if commit_signing_enabled(root) else []


def write_signed_commit_object(
    repo: Union[str, Path],
    root_tree: str,
    old_head: Optional[str],
    message: Union[str, bytes],
    name: str,
    email: str,
    stamp: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Attempt a SIGNED commit object for an already-assembled tree, via one
    `git commit-tree -S` spawn.

    Why a spawn on a route whose whole point is not spawning: the signature
    is the one part of a commit object this module cannot assemble itself.
    `gpg.format`, `gpg.ssh.program`, `user.signingkey` and the openpgp/ssh
    payload rules are git's, and a hand-rolled `gpgsig` header that gets any
    of them wrong produces a commit that LOOKS signed and verifies as
    forged. `commit-tree` is plumbing -- it runs no hooks and reads no
    worktree -- so it costs exactly the signature and nothing else.

    Budget note: this spawn is reached ONLY when `commit.gpgsign` is on for
    the repo. An unsigned repo keeps the zero-spawn native write below.

    Identity is pinned through the environment rather than left to git's own
    resolution so a signed commit carries the byte-identical author and
    committer lines the native path would have stamped -- same name, same
    email, same timestamp.

    `message` is carried as BYTES to `commit-tree`'s stdin. The native writers
    this stands in for write their message bytes verbatim; decoding here and
    re-encoding would make a non-UTF-8 message body differ between a signed
    and an unsigned commit of the same input, which is a divergence nothing
    downstream would report.

    Returns `(sha, None)` on success. Returns `(None, warning)` on ANY
    failure (missing/unreadable signing key, wrong `gpg.format`, no agent
    available, non-zero exit, a timeout, git not runnable at all) -- NEVER
    raises. DR-308 (`docs/decisions/DR-308-signed-commit-enforcement-is-
    declined-as.md`) declines a mechanism that catches/blocks on an unsigned
    commit: signing stays, enforcement does not, so a broken signing setup
    must never stop a commit from landing. The caller is expected to fall
    through to the same zero-spawn unsigned write it would have used with
    `commit.gpgsign` off, carrying `warning` forward to report unsigned-but-
    landed to its own caller (mirrors `coordinator_core.git.commit._sign_
    commit_tree`'s contract, which this module previously duplicated with
    the opposite -- raising -- policy)."""
    env = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = name
        env[f"GIT_{role}_EMAIL"] = email
        env[f"GIT_{role}_DATE"] = stamp
    args = ["git", "-C", str(repo), "commit-tree", "-S", root_tree]
    if old_head:
        args += ["-p", old_head]
    args += ["-F", "-"]
    raw = message if isinstance(message, bytes) else message.encode("utf-8", "surrogateescape")
    body = raw if raw.endswith(b"\n") else raw + b"\n"
    try:
        proc = subprocess.run(
            args,
            input=body,
            capture_output=True,
            timeout=30,
            env=env,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, (
            f"commit.gpgsign is set but `git commit-tree -S` could not run: "
            f"{exc} -- committed unsigned instead"
        )
    if proc.returncode != 0:
        return None, (
            "commit.gpgsign is set but `git commit-tree -S` failed -- "
            f"committed unsigned instead: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    sha = proc.stdout.decode("utf-8", "replace").strip()
    if len(sha) < 40:
        return None, (
            "commit.gpgsign is set but `git commit-tree -S` returned "
            f"{sha!r}, not a commit sha -- committed unsigned instead"
        )
    return sha, None
