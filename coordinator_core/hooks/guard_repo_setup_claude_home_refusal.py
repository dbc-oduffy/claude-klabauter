"""coordinator_core.hooks.guard_repo_setup_claude_home_refusal — PreToolUse
(Bash|PowerShell) hard-deny op: makes the repo-setup precondition "never
target ~/.claude" an executable refusal, not prose.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-repo-setup-claude-home-
refusal.py`. That script ran folded into `preuse-bash-dispatch.py`'s own
`_BASH_GUARD_REGISTRY`, a second, doctrine-plane-resident dispatch table.
None of that applies here: this lands as its own `hooks.<name>` op per this
row's body, called directly by whatever door dials
`hooks.guard_repo_setup_claude_home_refusal`.

THE DEFECT THIS CLOSES (unchanged from the source): the repo-setup skill
already names `~/.claude` (Claude Central) as a non-target root in prose —
but prose can be misapplied, and `~/.claude` carried a stray repo-setup
scaffold as a result. This guard makes that precondition executable: deny
the Bash/PowerShell command BEFORE the engine-side scaffold CLI (or its
target-root resolver) ever runs.

DETECTION STRATEGY (unchanged): fires only when the command text names one
of the scaffold-mechanism CLIs (`repo-setup-args-and-register` or
`coordinator_core.install.scaffold_structure` / `scaffold_structure`), in
an INVOKED-program-ish text position (start of command, after `-m`, or
preceded by a quote/path separator) — never a bare substring match, which
would deny a command that merely mentions the marker (a `grep`, a
`--grep` value). The candidate target root is an explicit `--root`/
`--target` flag value if present (resolved against the payload's `cwd` if
relative), else a leading `cd`/`Set-Location` prefix's target, else the
payload's own `cwd`. Both the candidate and Claude Home are resolved to
real, canonical paths via `Path.resolve()` before comparison — never a
string compare.

NEGATIVE-SPEC — `--dry-run` is exempt (a no-write exemption, never a
bypass: drop the flag and the write is denied again). NEGATIVE-SPEC —
`--batch` mode is deliberately UNHANDLED (see source docstring: this hook
cannot see the per-repo list without executing the very command it is
trying to gate). NEGATIVE-SPEC — only ONE leading `cd`/`Set-Location` is
honored; a second `cd` later in the command is not walked.

CLAUDE HOME RESOLUTION — never `os.path.expanduser` naively (ignores a
monkeypatched `HOME`). Order: `CLAUDE_CONFIG_DIR` (if set, IS Claude Home)
-> `HOME` (POSIX) -> `USERPROFILE` (Windows), each joined with `.claude`
for the latter two. No fallback to `Path.home()` — an unresolvable Claude
Home fails OPEN (allow).

Fail-open (returns `no_advisory()`), in order: `tool_name` not in
{Bash, PowerShell}; no string `tool_input.command`; command does not name
the scaffold mechanism; `--dry-run` present; unresolvable Claude Home; no
candidate root to compare; unresolvable candidate path; candidate does not
resolve to Claude Home. Any exception while resolving paths also fails
open — this op must never brick an ordinary Bash/PowerShell call.

Deny message: no wiki anchor — see source docstring's own note that the
obvious anchor is a fleet-private page outside the seed allowlist and
would 404 for the caller this op actually reaches (a sibling repo's
checkout or an OSS install). The prose carries the whole diagnosis inline.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

import os
import re

from coordinator_core._hook_envelope import deny, no_advisory
from coordinator_core.bash_guards.guard_repo_setup_claude_home_refusal import (
    _canonical,
    _join_onto_cwd,
)
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.ipc import register_op

_COMMAND_TOOL_NAMES = ("Bash", "PowerShell")

#: Identifiers naming the engine-plane scaffold mechanism. A bare substring
#: test would deny a command that merely MENTIONS one of these strings; see
#: `_names_scaffold_mechanism` below.
_SCAFFOLD_MECHANISM_MARKERS = (
    "repo-setup-args-and-register",
    "coordinator_core.install.scaffold_structure",
    "scaffold_structure",
)

#: ``--root <val>`` / ``--target <val>`` (also ``--root=val``), tolerating a
#: single- or double-quoted value.
_ROOT_FLAG_RE = re.compile(r"--(?:root|target)(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)")

#: ``--dry-run`` is the scaffold CLI's own no-write mode. NEGATIVE-SPEC:
#: this is a no-write exemption, never a bypass.
_DRY_RUN_RE = re.compile(r"(?:^|\s)--dry-run(?:[=\s]|$)")

#: A leading ``cd <path> &&``/``cd <path> ;`` or PowerShell
#: ``Set-Location``/``sl`` (optionally ``-Path``) prefix. Must anchor the
#: START of the command; only ONE such prefix is recognized.
_LEADING_CD_RE = re.compile(
    r"""^\s*(?:cd|Set-Location|sl)\s+(?:-Path\s+)?
        ("[^"]*"|'[^']*'|\S+)
        \s*(?:&&|;)""",
    re.IGNORECASE | re.VERBOSE,
)


def _resolve_claude_home(env: "dict[str, str]") -> "str | None":
    """Canonical, resolved path to Claude Home, or ``None`` if
    unresolvable. Never ``os.path.expanduser``."""
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        try:
            return _canonical(config_dir)
        except OSError:
            pass
    for key in ("HOME", "USERPROFILE"):
        val = env.get(key)
        if not val:
            continue
        try:
            return _canonical(_join_onto_cwd(".claude", val))
        except OSError:
            continue
    return None


def _expand_home_shorthand(raw: str, env: "dict[str, str]") -> str:
    """Expand a literal leading ``~`` or ``$HOME``/``${HOME}``/
    ``%USERPROFILE%`` token in ``raw`` against ``env``."""
    home = env.get("HOME") or env.get("USERPROFILE")
    if raw.startswith("~") and home:
        return home + raw[1:]
    for token in ("${HOME}", "$HOME", "%USERPROFILE%", "$env:USERPROFILE"):
        if raw.startswith(token) and home:
            return home + raw[len(token) :]
    return raw


def _names_scaffold_mechanism(cmd: str) -> bool:
    """True iff ``cmd`` invokes (not merely mentions) one of
    ``_SCAFFOLD_MECHANISM_MARKERS``. A marker occurrence counts as
    "invoked" when it sits at the very start of the command, immediately
    follows a ``python3 -m``/``python -m`` module flag, or is preceded by a
    quote or a path separator."""
    for marker in _SCAFFOLD_MECHANISM_MARKERS:
        start = 0
        while True:
            idx = cmd.find(marker, start)
            if idx == -1:
                break
            if idx == 0:
                return True
            prefix = cmd[:idx]
            if prefix[-1] in "\"'/\\":
                return True
            if re.search(r"-m\s+$", prefix):
                return True
            start = idx + 1
    return False


def _leading_cd_target(cmd: str, cwd: "str | None", env: "dict[str, str]") -> "str | None":
    """The effective cwd after a leading ``cd``/``Set-Location`` prefix, or
    ``None`` if ``cmd`` doesn't open with one."""
    match = _LEADING_CD_RE.match(cmd)
    if not match:
        return None
    raw = _expand_home_shorthand(match.group(1).strip("'\""), env)
    return _join_onto_cwd(raw, cwd)


def _extract_candidate_root(cmd: str, cwd: "str | None", env: "dict[str, str]") -> "str | None":
    """The path this command would resolve ``$_TARGET_ROOT`` to: an explicit
    ``--root``/``--target`` value (resolved against ``cwd`` if relative);
    else a leading ``cd``/``Set-Location`` prefix's target; else ``cwd``
    itself when neither is present."""
    match = _ROOT_FLAG_RE.search(cmd)
    if match:
        raw = _expand_home_shorthand(match.group(1).strip("'\""), env)
        return _join_onto_cwd(raw, cwd)
    cd_target = _leading_cd_target(cmd, cwd, env)
    if cd_target is not None:
        return cd_target
    return cwd


def is_denied_repo_setup_claude_home(
    cmd: str, cwd: "str | None", env: "dict[str, str]"
) -> bool:
    """The whole predicate, isolated from payload/envelope plumbing so it
    is directly unit-testable. Returns True (deny) iff ``cmd`` invokes the
    scaffold mechanism AND its resolved candidate target root is Claude
    Home."""
    if not _names_scaffold_mechanism(cmd):
        return False

    if _DRY_RUN_RE.search(cmd):
        return False  # no-write mode -- nothing to refuse

    claude_home = _resolve_claude_home(env)
    if not claude_home:
        return False  # cannot resolve what to compare against -- fail open

    candidate = _extract_candidate_root(cmd, cwd, env)
    if not candidate:
        return False  # no cwd and no explicit flag -- nothing to compare

    try:
        resolved_candidate = _canonical(candidate)
    except OSError:
        return False  # unresolvable candidate path -- fail open

    return resolved_candidate == claude_home


def _deny_reason() -> str:
    return (
        "BLOCKED: repo-setup's scaffold cannot target ~/.claude -- it is not "
        "a working tree. Run repo-setup against the project clone you mean to "
        "set up: /repo-setup --root <path-to-that-clone>."
    )


@register_op("hooks.guard_repo_setup_claude_home_refusal")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Bash|PowerShell) op: deny a command that scaffolds
    repo-setup against ~/.claude."""
    if params.get("tool_name") not in _COMMAND_TOOL_NAMES:
        return no_advisory()

    tool_input = params.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str) or not cmd:
        return no_advisory()

    cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None

    try:
        denied = is_denied_repo_setup_claude_home(cmd, cwd, dict(os.environ))
    except Exception:
        return no_advisory()  # any resolution failure -- fail open

    if not denied:
        return no_advisory()

    reason = render(compose(_deny_reason()))
    return deny("PreToolUse", reason)
