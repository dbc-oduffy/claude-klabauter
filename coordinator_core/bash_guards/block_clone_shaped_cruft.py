"""coordinator_core.bash_guards.block_clone_shaped_cruft -- PreToolUse(Bash, PowerShell) guard:
a command that copies a repo tree by hand (`git clone` of a local repo, `cp -r`/`robocopy`/`xcopy`/
`Copy-Item -Recurse` of a repo root, `git archive` piped into a `tar`/`unzip` extract).

A regular subagent is denied; the EM and a named reviewer agent get an advisory. Identity comes from
the destructive-action guard's own subagent test and the shared audience resolver, never a second
derivation here. An engine op that owns its delete (`baseline-export`, `landing_export`) is no copy
shape at command position and is never matched.

Zero spawns: token parsing and `.git` existence probes.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    normalize_executable_basename,
    resolve_command_positions,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards.block_topic_branch import _git_argv

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 45

GUARD_NAME = "block-clone-shaped-cruft"

_PRE_FILTER_RE = re.compile(r"\b(?:clone|cp|copy|copy-item|ci|robocopy|xcopy|archive)\b", re.I)
_CLONE_VALUE_FLAGS = frozenset(
    {
        "-b", "--branch", "--depth", "-o", "--origin", "--reference", "--reference-if-able", "-c",
        "--config", "--template", "-j", "--jobs", "--separate-git-dir", "--filter", "-u",
        "--upload-pack", "--shallow-since", "--shallow-exclude", "--server-option", "--bundle-uri",
    }
)
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_RE = re.compile(r"^(?:[^/\\:@]+@)?[^/\\:@]{2,}:(?!\\|/)")
_EXTRACT_VERBS = frozenset({"tar", "bsdtar", "unzip", "7z", "7za"})


def _is_repo_root(path: str, cwd: Optional[str]) -> bool:
    """True when `path` (resolved against `cwd`) is a work tree root or a bare repo directory."""
    if not path or path.startswith("-"):
        return False
    base = cwd or os.getcwd()
    full = path if os.path.isabs(path) else os.path.join(base, path)
    full = os.path.normpath(full)
    if not os.path.isdir(full):
        return False
    if os.path.exists(os.path.join(full, ".git")):
        return True
    return os.path.isfile(os.path.join(full, "HEAD")) and os.path.isdir(os.path.join(full, "objects"))


def _clone_shape(args: List[str], cwd: Optional[str], c_dirs: List[str]) -> Optional[str]:
    positional: List[str] = []
    skip = False
    for tok in args:
        if skip:
            skip = False
        elif tok in _CLONE_VALUE_FLAGS:
            skip = True
        elif not tok.startswith("-"):
            positional.append(tok)
    if not positional:
        return None
    src = positional[0]
    if _URL_RE.match(src) and not src.lower().startswith("file://"):
        return None
    if _SCP_RE.match(src):
        return None
    if src.lower().startswith("file://"):
        src = src[len("file://"):].lstrip("/") if os.name == "nt" else src[len("file://"):]
    base = cwd
    for d in c_dirs:
        base = d if os.path.isabs(d) else os.path.join(base or os.getcwd(), d)
    return "git clone of a local repo" if _is_repo_root(src, base) else None


def _cp_shape(tokens: List[str], cwd: Optional[str]) -> Optional[str]:
    recursive = False
    operands: List[str] = []
    target_dir_flag = False
    skip = False
    for tok in tokens[1:]:
        if skip:
            skip = False
        elif tok == "--":
            continue
        elif tok in ("-t", "--target-directory"):
            target_dir_flag = True
            skip = True
        elif tok.startswith("--"):
            recursive = recursive or tok in ("--recursive", "--archive")
        elif tok.startswith("-") and len(tok) > 1:
            recursive = recursive or any(c in tok[1:] for c in "rRa")
        else:
            operands.append(tok)
    sources = operands if target_dir_flag else operands[:-1]
    if recursive and any(_is_repo_root(s, cwd) for s in sources):
        return "cp -r of a repo tree"
    return None


def _robocopy_shape(tokens: List[str], cwd: Optional[str], verb: str) -> Optional[str]:
    if any(t.lower() in ("/l", "-l") for t in tokens[1:]):
        return None
    operands = [t for t in tokens[1:] if not t.startswith("/")]
    if len(operands) >= 2 and _is_repo_root(operands[0], cwd):
        return f"{verb} of a repo tree"
    return None


def _copy_item_shape(tokens: List[str], cwd: Optional[str]) -> Optional[str]:
    lowered = [t.lower() for t in tokens]
    if not any(len(t) >= 2 and "-recurse".startswith(t) for t in lowered[1:]):
        return None
    source: Optional[str] = None
    positional: List[str] = []
    i = 1
    while i < len(tokens):
        low = lowered[i]
        if low in ("-path", "-literalpath", "-lp", "-p") and i + 1 < len(tokens):
            source = tokens[i + 1]
            i += 2
        elif low.startswith("-"):
            i += 2 if low in ("-destination", "-d", "-filter", "-include", "-exclude") else 1
        else:
            positional.append(tokens[i])
            i += 1
    source = source or (positional[0] if positional else None)
    if source and _is_repo_root(source, cwd):
        return "Copy-Item -Recurse of a repo tree"
    return None


def _tar_extracts(tokens: List[str]) -> bool:
    verb = normalize_executable_basename(tokens[0])
    if verb in ("unzip", "7z", "7za"):
        return True
    for tok in tokens[1:]:
        if tok == "--extract":
            return True
        if tok.startswith("-") and not tok.startswith("--") and "x" in tok:
            return True
        if not tok.startswith("-") and re.fullmatch(r"[a-zA-Z]+", tok) and "x" in tok:
            return True
    return False


def _shape_of(command: str, cwd: Optional[str]) -> Optional[str]:
    archive = False
    extract = False
    for resolved in resolve_command_positions(command, preserve_windows_backslashes=True):
        tokens = resolved.tokens
        if not tokens:
            continue
        verb = normalize_executable_basename(tokens[0])
        shape: Optional[str] = None
        if verb == "git":
            parsed = _git_argv(tokens)
            if parsed is not None and parsed[0] == "clone":
                shape = _clone_shape(parsed[1], cwd, parsed[2])
            elif parsed is not None and parsed[0] == "archive":
                archive = True
        elif verb == "cp":
            shape = _cp_shape(tokens, cwd)
        elif verb in ("robocopy", "xcopy"):
            shape = _robocopy_shape(tokens, cwd, verb)
        elif verb in ("copy-item", "ci", "cpi"):
            shape = _copy_item_shape(tokens, cwd)
        elif verb in _EXTRACT_VERBS and _tar_extracts(tokens):
            extract = True
        if shape:
            return shape
    return "git archive extracted to a directory" if archive and extract else None


def _warn_only(payload: Dict[str, Any]) -> bool:
    """True for the EM (main thread) and a named reviewer agent; False for a regular subagent."""
    from coordinator_core.bash_guards._audience import resolve_audience
    from coordinator_core.bash_guards._helpers import is_confined_findings_agent
    from coordinator_core.bash_guards.block_subagent_destructive_action import _payload_names_a_subagent
    from coordinator_core.guard_messages.alternative_record import Audience

    if "agent_id" not in payload and not _payload_names_a_subagent(payload):
        return True
    audience, caller_class = resolve_audience(payload)
    kind = payload.get("agent_type") or payload.get("subagent_type") or caller_class or ""
    return audience is Audience.EM or is_confined_findings_agent(kind)


def _record(shape: str, deny: bool) -> Any:
    from coordinator_core.guard_messages.alternative_record import (
        AdaptableOffer,
        AlternativeRecord,
        EscalationRoute,
    )

    summary = f"{shape} makes a tree copy with no owner or lifetime."
    offer = "`baseline-export --sha <sha> -- <runner argv>`, or work in the shared tree"
    members = [AdaptableOffer(template=offer, what_to_adapt="the sha and the runner argv")]
    if deny:
        members.append(
            EscalationRoute(
                report_what="the run that needs a tree copy",
                authorize_what="an engine export",
            )
        )
    return AlternativeRecord(
        verdict_kind="deny" if deny else "advisory",
        guard_name=GUARD_NAME,
        summary=summary,
        em_text=f"{summary} Alternative: {offer}.",
        members=tuple(members),
    )


def check(payload: Dict[str, Any]) -> Optional[Any]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    shape = _shape_of(cmd.replace("\r", ""), payload.get("cwd"))
    if shape is None:
        return None
    return _record(shape, deny=not _warn_only(payload))
