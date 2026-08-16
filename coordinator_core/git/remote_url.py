"""coordinator_core.git.remote_url -- remote-URL derivation, the one git
fact `coordinator_core.git.repo_root` does not expose.

Scope (prior-art-check, Claim #3, verdict CONFLICT -- folded in): repo root
and git dir already ship the non-spawning, cwd-keyed-memoized, `.git`-FILE-
aware parent-walk this module would otherwise have had to rebuild
(`coordinator_core.git.repo_root`, `coordinator_core.git.git_dir`, landed by
docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md).
This module does not re-derive or re-expose either -- callers needing repo
root or git dir go straight to `coordinator_core.git.repo_root.show_toplevel`
/ `coordinator_core.git.git_dir.resolve_git_dir`. It follows the delegation
pattern `coordinator_core.write_guards._repo_root` already uses: adds zero
new caching logic, and the zero-new-subprocess-logic property that module
claims does NOT carry over here (see "Derivation method" below -- a remote
URL, unlike a toplevel/gitdir, has no cheap non-spawning walk equivalent).

Derivation method (the one decision this module exists to make): SPAWNS
`git remote get-url <remote>` once per resolution. Does NOT parse
`.git/config`. `.git/config`'s INI-like format is not `configparser`-
compatible (git allows subsection quoting configparser rejects, and
multivalued keys configparser silently drops all but the last of), and even
a correct hand-rolled parser would diverge from what `git remote get-url`
actually reports whenever `url.<base>.insteadOf` / `pushInsteadOf` rewrites
or `[include]`/`[includeIf]` directives are in play -- both rewrite the
effective URL past what a literal read of the `[remote "<name>"] url =` line
shows. A parser can be made to model straightforward cases, but "mostly
right" is worse than always-right here: a caller trusts this module's
answer to be the CLONE URL, and a config-reader that silently disagrees
with git on a rewritten remote is a correctness bug wearing a performance
optimization's clothes. This is an HONEST one-spawn-per-resolution module,
not a non-spawning one -- callers on a hot path should call it at most once
per remote per invocation, same discipline as any other subprocess call.

No memo (correction to what `repo_root`'s "session constant" framing would
otherwise suggest -- read as amended, not as originally scoped): unlike
repo root/git dir, remote URL is NOT a session constant.
`coordinator_core.ops.create_github_remote.create_and_push_remote`
configures a local remote entry, in-process, when one is absent -- an
absent-to-present transition within a single process for the exact fact
this module resolves. A cwd-keyed memo here would serve a stale "absent"
(`None`) answer to any call after that configure step, for the rest of the
process. Remote URL is also read approximately once per session and the
underlying spawn is a cheap, already-idempotent `git` invocation -- a cache
buys negligible spawn elimination while costing exactly the correctness
`repo_root`'s "Negative caching" section already warns against inheriting
uncritically. So: no memo dict, nothing for `repo_root.clear_memo()` to
reach, nothing to invalidate.

Negative-spec (cites, does not re-derive, `repo_root.py`'s own "Negative
caching" section): status, HEAD, and branch are NEVER served or cached here
-- this module resolves exactly one fact (a named remote's URL) and nothing
else. Peer sessions commit into this shared worktree mid-run; even if this
module DID cache, repo identity facts that can change under a live process
must never be memoized past the moment they were read, per the delegate's
own rule.
"""

from __future__ import annotations

import subprocess
from typing import Optional

_TIMEOUT_SECS = 2.0


def get_remote_url(remote: str = "origin", cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git remote get-url <remote>`: the configured URL for
    `remote` in the repo enclosing `cwd` (defaults to the process's current
    cwd), or `None` on any failure (no such remote, not a repo, git
    missing, timeout, non-zero exit).

    Always spawns -- see module docstring's "Derivation method". Never
    memoized -- see module docstring's "No memo".
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_TIMEOUT_SECS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        # A successful `git remote get-url` with empty stdout is not a
        # documented git behavior (unlike `--show-prefix` at the toplevel
        # in `repo_root.py`, which legitimately emits ""); treat it as a
        # failed resolution rather than inventing a "you have a remote
        # named '' " semantics no caller expects.
        return None
    return value
