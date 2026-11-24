"""coordinator_core.ops.dispatch_emit.wired_check — re-run a judge-cited wired check.

A judge's `command` string is untrusted input: it runs only when `census.screen` admits it as a
read-only form AND it is a single stage argv can run without a shell. Anything else demotes with
reason `not-rerunnable-at-land` and is never spawned.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.roadmap import census
from coordinator_core.win_portability import no_console_creationflags

NOT_RERUNNABLE = "not-rerunnable-at-land"
DEFAULT_TIMEOUT_S = 0.3
_OUTPUT_CAP = 2000
# Tokens a shell would act on; argv without a shell would pass them as literal arguments.
_SHELL_PUNCT = frozenset("|&;()<>")


def _demoted(reason: str, output: str = "") -> dict:
    return {"passed": False, "reason": reason, "output": output}


def _single_stage_argv(command: str) -> Optional[list[str]]:
    """The argv of `command`, or None when it is a pipeline, has a redirect or does not tokenise."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    if not tokens or any(set(t) <= _SHELL_PUNCT for t in tokens):
        return None
    return tokens


def rerun_wired_check(
    repo_root: Path | str, command: str, timeout_s: float = DEFAULT_TIMEOUT_S
) -> dict:
    """Re-run `command` at `repo_root`; return {passed, reason, output}.

    Pass is exit code 0. An inadmissible command (not read-only per `census.screen`, a pipeline or
    redirect, `python -m pytest`, an unresolvable head) returns passed False with reason
    `not-rerunnable-at-land` and is never spawned; a timeout returns passed False, reason `timeout`.
    """
    root = Path(repo_root)
    command = str(command or "")
    if census.screen(command, root) is not None:
        return _demoted(NOT_RERUNNABLE)
    argv = _single_stage_argv(command)
    if argv is None or "pytest" in argv:
        return _demoted(NOT_RERUNNABLE)
    head = Path(argv[0]).name
    if head in census._PY_HEADS:
        argv[0] = sys.executable
    else:
        resolved = shutil.which(argv[0])
        if resolved is None:
            return _demoted(NOT_RERUNNABLE)
        argv[0] = resolved
    try:
        proc = subprocess.run(
            argv, cwd=str(root), capture_output=True, text=True, timeout=timeout_s,
            check=False, **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return _demoted("timeout")
    except OSError as exc:
        return _demoted(NOT_RERUNNABLE, str(exc))
    output = ((proc.stdout or "") + (proc.stderr or ""))[:_OUTPUT_CAP]
    if proc.returncode == 0:
        return {"passed": True, "reason": "", "output": output}
    return {"passed": False, "reason": f"exit {proc.returncode}", "output": output}
