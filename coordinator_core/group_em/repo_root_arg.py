r"""One validator for the `--repo-root` every Group EM runnable takes from argv.

WHY THIS EXISTS. `watch.py` accepted a `--repo-root` that did not exist and
armed normally, printing `ARMED peer_count=0` -- no error, no warning, exit 0.
A watcher reported "armed and standing by" four times across fifty minutes
while watching nothing, through nine live peer transitions
(cross-repo/inbox/2026-09-01-example-game-repo-em-group-em-watch-accepts-bad-repo-root-silently.md).
An unreadable repo root and a genuinely quiet repo emitted the identical line.

THE MANGLING IS ORDINARY, WHICH IS WHY A TYPE CHECK IS NOT ENOUGH. The spelling
a Windows-hosted agent reaches for is `X:\example-game-workbench-repo`. Through the
Bash tool, `\c` and `\u` are eaten before the module ever sees the argument,
leaving `X:example-game-workbench-repo` -- which is not a typo Python can see. It is a
DRIVE-RELATIVE path, a real Windows path shape that resolves against the current
directory on drive X:, so it silently binds the run to wherever the process
happened to be standing. In the measured incident that was a sibling checkout,
and the run went on to MINT a repo-shaped directory there: the heartbeat writer
created `<mangled>/state/group-em-watch.json` inside a publish mirror, where the
stray tree failed a publish row's content check and blocked the round for the
whole fleet (claude-klabauter-c0, 2026-09-01).

So the check is two separate refusals, not one:
  - NOT ABSOLUTE -- catches the mangled form even when the directory it would
    resolve to happens to exist. A repo root that depends on cwd is not a repo
    root; every caller here runs under a harness tool whose working directory is
    not ours, which is why these CLIs take the root as an argument at all.
  - NOT AN EXISTING DIRECTORY -- catches every other bad spelling, and is the
    cheap whole fix for the case that bit us.

The refusal names the RESOLVED path, never the raw argument. The display name
surviving the mangling (`example-game-workbench-repo` was still printed, correctly,
beside a path that did not exist) is what made the broken arm look healthy for
fifty minutes.

Negative-spec:
    - Does NOT create, repair, or normalise-away a bad root. A runnable that
      fixes up its own arguments is how a mangled path becomes a plausible one.
    - Does NOT fall back to cwd. That fallback IS the defect.
    - No I/O beyond one `isdir`.
"""

from __future__ import annotations

import os


class RepoRootArgError(ValueError):
    """A `--repo-root` this process must refuse rather than work around."""


def resolve_repo_root_arg(value: object) -> str:
    """Validate a `--repo-root` argument; return it absolute and normalised.

    Raises `RepoRootArgError` with an operator-facing message naming the
    resolved path. Callers exit non-zero on it -- never warn and continue: the
    whole failure this guards is a run that looked successful.
    """
    if not isinstance(value, str) or not value.strip():
        raise RepoRootArgError("--repo-root is empty")

    raw = value.strip()
    if not os.path.isabs(raw):
        resolved = os.path.abspath(raw)
        raise RepoRootArgError(
            f"--repo-root {raw!r} is not an absolute path; it resolves against this "
            f"process's working directory to {resolved!r}. On Windows a backslash "
            f"path through a shell loses its separators, and what is left is "
            f"drive-relative rather than absolute. Pass forward slashes: X:/name"
        )

    resolved = os.path.abspath(os.path.normpath(raw))
    if not os.path.isdir(resolved):
        raise RepoRootArgError(
            f"--repo-root {raw!r} resolves to {resolved!r}, which is not an existing "
            f"directory. Refusing rather than arming over nothing: a watch on an "
            f"unreadable root and a watch on a quiet repo print the same line."
        )
    return resolved
