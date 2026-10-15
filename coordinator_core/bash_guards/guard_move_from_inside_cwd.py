"""coordinator_core.bash_guards.guard_move_from_inside_cwd --
``check_move_from_inside_cwd``: advisory (never a deny) when ``mv`` (Bash) or
``Move-Item``/``mv``/``move`` (PowerShell) moves a directory the shell's own cwd
is inside. Windows refuses that move while the persistent shell holds the
directory. Path arithmetic only: no spawn, no stat. Silent on any operand that
is not a literal path (glob, ``$VAR``, substitution).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards import _dialect
from coordinator_core.bash_guards._command_tokenizer import normalize_executable_basename
from coordinator_core.bash_guards._rewrite_support import _advisory
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES

_GUARD_NAME = "guard_move_from_inside_cwd"
_PREFILTER = re.compile(r"(?i)\b(mv|move|move-item|mi)\b")
_MOVE_HEADS = frozenset({"mv", "move", "move-item", "mi"})
_CD_HEADS = frozenset({"cd", "chdir", "set-location", "sl"})
_NON_LITERAL = re.compile(r"[*?\[\]$`(){}<>|;&,~]|^%")
_PS_PATH_FLAGS = frozenset({"-path", "-literalpath", "-lp", "-pspath"})
_PS_VALUE_FLAGS = frozenset(
    {"-destination", "-filter", "-include", "-exclude", "-credential", "-pathtype"}
)

_MESSAGE = (
    "MOVE-FROM-INSIDE-CWD: the shell cwd `{cwd}` is inside `{src}`; Windows refuses "
    "the move (Permission denied) while the shell holds it. `cd` out first."
)


def _literal(token: str, powershell: bool) -> Optional[str]:
    if powershell:
        token = _dialect._strip_ps_quotes(token)
    if not token or _NON_LITERAL.search(token):
        return None
    return token


def _resolve(base: str, operand: str) -> str:
    joined = operand if os.path.isabs(operand) else os.path.join(base, operand)
    return os.path.normpath(joined)


def _inside(cwd: str, src: str) -> bool:
    cwd, src = os.path.normcase(cwd), os.path.normcase(src)
    if cwd == src:
        return True
    prefix = src if src.endswith(os.sep) else src + os.sep
    return cwd.startswith(prefix)


def _bash_sources(args: List[str]) -> List[str]:
    positional: List[str] = []
    target_dir_flag = False
    skip_next = False
    ended = False
    for tok in args:
        if skip_next:
            skip_next = False
            continue
        if not ended and tok == "--":
            ended = True
        elif not ended and tok.startswith("-") and len(tok) > 1:
            if tok == "-t" or tok == "--target-directory":
                target_dir_flag = True
                skip_next = True
            elif tok.startswith("--target-directory=") or (
                tok.startswith("-t") and not tok.startswith("--")
            ):
                target_dir_flag = True
            elif tok in ("-S", "--suffix"):
                skip_next = True
        else:
            positional.append(tok)
    return positional if target_dir_flag else positional[:-1]


def _ps_sources(args: List[str]) -> List[str]:
    named: List[str] = []
    positional: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        low = tok.lower()
        if low in _PS_PATH_FLAGS and i + 1 < len(args):
            named.append(args[i + 1])
            i += 2
        elif low in _PS_VALUE_FLAGS:
            i += 2
        elif tok.startswith("-") and len(tok) > 1:
            i += 1
        else:
            positional.append(tok)
            i += 1
    if named:
        return named
    return positional[:1]


def check_move_from_inside_cwd(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    cwd: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not cmd or not _PREFILTER.search(cmd):
        return None
    payload = payload or {}
    cur = cwd or payload.get("cwd") or ""
    if not cur:
        return None
    dialect = _dialect.dialect_from_tool_name(payload.get("tool_name")) or _dialect.Dialect.BASH
    powershell = dialect is _dialect.Dialect.POWERSHELL
    segments = _dialect.resolve_segments_for_dialect(cmd, dialect, guard_name=_GUARD_NAME)
    if not segments:
        return None
    effective: Optional[str] = os.path.normpath(cur)
    for tokens, _pipe in segments:
        if not tokens:
            continue
        head = normalize_executable_basename(_dialect._strip_ps_quotes(tokens[0]) if powershell else tokens[0]).lower()
        args = tokens[1:]
        if head in _CD_HEADS:
            target = next((a for a in args if not a.startswith("-")), None)
            lit = _literal(target, powershell) if target else None
            effective = _resolve(effective, lit) if (lit and effective) else None
            continue
        if head not in _MOVE_HEADS or effective is None:
            continue
        sources = _ps_sources(args) if powershell else _bash_sources(args)
        for operand in sources:
            lit = _literal(operand, powershell)
            if lit is None:
                continue
            src = _resolve(effective, lit)
            if _inside(effective, src):
                return _advisory(_MESSAGE.format(cwd=effective, src=src))
    return None
