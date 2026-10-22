"""coordinator_core.bash_guards._dc_commit_named_stage -- pure carrier parse and staged-vs-named set logic for a bare `git commit` that names its staged set.

The carrier is a separate-token `git -c coordinator.staged=<path> commit ...` operand. No
subprocess and no filesystem read: callers hand in tokens and the probe's staged paths.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

from coordinator_core.bash_guards._rewrite_support import (
    _GIT_GLOBAL_OPT_WITH_ARG,
    _bt_git_subcommand_start_index,
    _bt_peel_wrapper_prefix,
)

NAMED_STAGE_CONFIG_KEY = "coordinator.staged"

_CARRIER_PREFIX = NAMED_STAGE_CONFIG_KEY + "="
_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def named_stage_paths(seg_tokens: List[str]) -> Optional[Tuple[str, ...]]:
    """Every value of a separate-token `-c coordinator.staged=<v>` in the git
    global-option run ahead of the subcommand, in order. `None` when the
    subcommand does not resolve or no such operand exists; glued
    `-ccoordinator.staged=` and `--config-env` spellings never count."""
    end = _bt_git_subcommand_start_index(seg_tokens)
    if end is None:
        return None
    offset = len(seg_tokens) - len(_bt_peel_wrapper_prefix(seg_tokens))
    values: List[str] = []
    i = offset + 1
    stop = end - 1
    while i < stop:
        tok = seg_tokens[i]
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            if tok == "-c" and i + 1 < stop:
                operand = seg_tokens[i + 1]
                if operand.startswith(_CARRIER_PREFIX):
                    values.append(operand[len(_CARRIER_PREFIX):])
            i += 2
        else:
            i += 1
    return tuple(values) if values else None


def named_set_refusal(named: Tuple[str, ...]) -> Optional[str]:
    """One-fact reason the named set is invalid (empty value, absolute path,
    `..` segment), or `None` when it is valid."""
    for path in named:
        if path == "":
            return "coordinator.staged names an empty path"
        norm = path.replace("\\", "/")
        if norm.startswith("/") or _DRIVE_ABS_RE.match(path):
            return f"coordinator.staged path is absolute: {path}"
        if ".." in norm.split("/"):
            return f"coordinator.staged path has a '..' segment: {path}"
    return None


def _normalise(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def unnamed_staged_paths(staged: Iterable[str], named: Iterable[str]) -> Tuple[str, ...]:
    """Sorted `staged - named` after `\\`->`/` normalisation and leading `./` stripping."""
    named_set = {_normalise(p) for p in named}
    return tuple(sorted({_normalise(p) for p in staged} - named_set))
