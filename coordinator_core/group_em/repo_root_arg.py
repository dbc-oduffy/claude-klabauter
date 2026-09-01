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
    # Review: coordinator:code-reviewer.a1574022171f8f1cc (P2, accepted) --
    # `os.path.isabs` (ntpath) treats a driveless rooted path (`/foo/bar`) as
    # absolute, then `abspath` resolves it against the PROCESS'S CURRENT
    # DRIVE -- the identical "binds to wherever the process happens to be
    # standing" hazard this module exists to close, just swapping drive for
    # directory. `splitdrive` is the one extra check that closes it: a path
    # this refusal accepts must name both a drive and a root.
    #
    # Review: coordinator:code-reviewer.a89481390696514f7 (P1, accepted) --
    # the drive requirement is a Windows-only hazard (`ntpath.splitdrive`).
    # On POSIX, `os.path` is `posixpath`, whose `splitdrive` always returns
    # an empty drive -- applying this check unconditionally refused every
    # legitimate POSIX absolute root (fleet floor includes a MacBook Pro).
    # Gate the drive requirement on the platform that motivates it; a UNC
    # path (`\\server\share\...`) and a mapped drive both still produce a
    # non-empty `drive` from `ntpath.splitdrive` on Windows, so this does
    # not weaken the original catch.
    drive, _tail = os.path.splitdrive(raw)
    require_drive = os.name == "nt"
    if not os.path.isabs(raw) or (require_drive and not drive):
        resolved = os.path.abspath(raw)
        # Review: coordinator:code-reviewer.a1574022171f8f1cc (P3, accepted) --
        # this message carried a causal explanation after the fact; the
        # register wants one fact plus a terse alternative, WHY stays in this
        # docstring, not the operator-facing line.
        raise RepoRootArgError(
            f"--repo-root {raw!r} is not an absolute, drive-anchored path; it "
            f"resolves to {resolved!r}. Pass forward slashes: X:/name"
        )

    resolved = os.path.abspath(os.path.normpath(raw))
    if not os.path.isdir(resolved):
        raise RepoRootArgError(
            f"--repo-root {raw!r} resolves to {resolved!r}, which is not an existing "
            f"directory. Pass an existing repo root."
        )
    return resolved
