"""
bin/shell-init-guard.py — pure stdout emitter for the interactive-shell resource-cap guard.

Purpose: Prints two bash guard lines (`ulimit -S -f <blocks>` + `shopt -s failglob`) that
the calling shell evaluates directly. Invoked from the ~/.bashrc eval seam (DoE-owned):

    eval "$(python3 <path>/bin/shell-init-guard.py 2>/dev/null)"

DR-047 cross-repo split: DoE owns the ~/.bashrc seam and the eval invocation; claude-klabauter owns
this pure emitter engine. The two sides communicate only through stdout (bash to eval) and
one env var, `COORDINATOR_OVERRIDE_FSIZE_CAP`, read by this script to size the cap.

Cap resolution (see docs/plans and cross-repo spec below for full contract):
  - unset / empty            -> default cap, 8 GiB (8388608 blocks)
  - "unlimited" (any case/ws) -> ulimit -S -f unlimited
  - positive integer N >= 1  -> N GiB, emit ulimit -S -f <N * 1048576>
  - anything else (invalid)  -> default cap on stdout + a `# coordinator: ignored
                                 invalid COORDINATOR_OVERRIDE_FSIZE_CAP=<value>` comment
                                 on stderr (discarded by the seam's `2>/dev/null`)

`ulimit -f`'s unit is 1024-byte (1 KiB) blocks on both Linux and macOS; 1 GiB = 1048576
blocks.

Negative-spec:
  - stdlib-only. No third-party imports — the calling seam already requires bash>=4 +
    BSD coreutils (DR-148); this script itself has no such dependency beyond python3.
  - Pure emitter: no file writes, no other side effects. stdout is the only channel that
    matters to the caller.
  - Uses the SOFT limit only (`ulimit -S -f`), never the hard limit — a guardrail an
    operator can still raise within the hard ceiling, not a wall.
  - stdout is eval-only: nothing may reach stdout except the two guard lines. A stray
    traceback or stray text on stdout would brick every terminal that evals this output.
  - "Fail-open" here means "never wedge the session" — it does NOT mean "emit unlimited
    on error". Any internal error still emits the safe default (8 GiB cap + failglob) to
    stdout and exits 0.

Spec: cross-repo/inbox/2026-07-11-claude-central-em-shell-init-guard-emitter-script.md (DR-047 split)
"""

from __future__ import annotations

import sys

_DEFAULT_CAP_GIB = 8
_BLOCKS_PER_GIB = 1048576
_DEFAULT_CAP_BLOCKS = _DEFAULT_CAP_GIB * _BLOCKS_PER_GIB
_ENV_VAR = "COORDINATOR_OVERRIDE_FSIZE_CAP"


def _resolve_ulimit_line(raw_value: str | None) -> tuple[str, str | None]:
    """Resolve COORDINATOR_OVERRIDE_FSIZE_CAP into a ulimit line + optional stderr note.

    Returns (ulimit_line, stderr_comment_or_None). Never raises — any unparseable input
    falls through to the safe default cap, with the offending value echoed to the second
    return slot for the caller to route to stderr.
    """
    if raw_value is None:
        return f"ulimit -S -f {_DEFAULT_CAP_BLOCKS}", None

    trimmed = raw_value.strip()
    if trimmed == "":
        return f"ulimit -S -f {_DEFAULT_CAP_BLOCKS}", None

    if trimmed.lower() == "unlimited":
        return "ulimit -S -f unlimited", None

    # Review: code-reviewer (Finding 1) — reject sign-prefixed ("+8"), underscore-separated
    # ("1_000"), and non-ASCII-digit (e.g. Arabic-indic) forms before int() would otherwise
    # silently accept them; a leading-zero form like "008" still passes (isdigit() is True)
    # and that's intentional.
    if not (trimmed.isascii() and trimmed.isdigit()):
        comment = f"# coordinator: ignored invalid {_ENV_VAR}={raw_value}"
        return f"ulimit -S -f {_DEFAULT_CAP_BLOCKS}", comment

    try:
        n = int(trimmed)
    except ValueError:
        comment = f"# coordinator: ignored invalid {_ENV_VAR}={raw_value}"
        return f"ulimit -S -f {_DEFAULT_CAP_BLOCKS}", comment

    if n < 1:
        comment = f"# coordinator: ignored invalid {_ENV_VAR}={raw_value}"
        return f"ulimit -S -f {_DEFAULT_CAP_BLOCKS}", comment

    return f"ulimit -S -f {n * _BLOCKS_PER_GIB}", None


def main() -> int:
    """Emit the two guard lines to stdout; never let an exception reach the caller."""
    try:
        import os

        raw_value = os.environ.get(_ENV_VAR)
        ulimit_line, stderr_comment = _resolve_ulimit_line(raw_value)
    except Exception:
        ulimit_line = f"ulimit -S -f {_DEFAULT_CAP_BLOCKS}"
        stderr_comment = None

    try:
        print(ulimit_line)
        print("shopt -s failglob")
        if stderr_comment:
            print(stderr_comment, file=sys.stderr)
    except Exception:
        # The print() calls themselves failed (e.g. broken stdout pipe) --
        # there is no lower-level channel left to report through, and this
        # script's whole contract is best-effort stdout eval'd by the
        # shell, so silence is the only option. main() still returns 0
        # below so the ~/.bashrc eval seam never sees a nonzero exit.
        pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            print(f"ulimit -S -f {_DEFAULT_CAP_BLOCKS}")
            print("shopt -s failglob")
        except Exception:
            # Same last-resort case as main()'s inner guard: if even this
            # fallback print() fails, there is nowhere left to report it --
            # fall through to the unconditional sys.exit(0) below so the
            # eval seam is never left without its two guard lines silently
            # aborting the whole shell init.
            pass
        sys.exit(0)
