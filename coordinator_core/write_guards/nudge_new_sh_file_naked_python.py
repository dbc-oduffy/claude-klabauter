"""coordinator_core.write_guards.nudge_new_sh_file_naked_python — advisory
guard.

Discharges example-doctrine-repo CLAUDE.md § Runtime conventions: "Multi-OS support is
P0 -- macOS, Windows, Linux all first-class. Process spawns are cheap on
some hosts and brutally expensive on others, so structural bash is a defect
to be ported to claude-klabauter naked Python -- author new logic as naked Python,
not new bash." Verified 2026-07-31: no guard anywhere keys on target-path
extension ``.sh`` -- ``nudge_windows_subprocess_popup`` classifies file
CONTENT for shell-token/``creationflags`` shapes, it never asks "are you
creating a new ``.sh`` file"; there is no near-miss to fold this into.

This is a NEW guard, not a port of a example-doctrine-repo reference ``.sh`` hook. It follows
the module-shape convention set by ``nudge_tasks_state_folder_split.py``
(advisory CLASS, path-gate-only, no content reconstruction needed) and the
NAMED-EXCEPTION TABLE shape from ``coordinator/hooks/scripts/
_oss_operative_strings.py`` / ``guard-oss-payload-locality.py`` (example-doctrine-repo
repo) -- a small, deliberately-edited table with the rationale carried
alongside each entry, rather than an inline suppression marker.

CLASS is "advisory" -- this must NEVER deny. Plenty of legitimate reasons
exist to author a new ``.sh`` file (the two irreducible legs below, a CI
entry point the harness itself requires, etc.); a hard block here would be
wrong on the merits and would earn a bypass, after which it protects
nothing. Per design-as-offers (global CLAUDE.md § Implementation Standards;
``docs/wiki/hook-best-practices.md`` § nag->action), the offer leads with
the alternative and names a concrete landing spot, not a bare "don't write
bash."

DETECTION: fires ONLY on the creation of a genuinely NEW ``.sh`` file --
``Write`` (never ``Edit``/``MultiEdit``, mid-edit nagging on an existing
script is exactly the friction design-as-offers rejects) whose target path
case-foldedly ends in ``.sh`` AND does not already exist on disk. Editing an
existing ``.sh`` stays silent unconditionally, regardless of any other
condition below -- porting bash to Python is a workstream, not something to
interrupt an in-flight edit over.

NAMED-EXCEPTION TABLE (``_IRREDUCIBLE_SH_BASENAMES``): the two bash legs
Example-doctrine-repo doctrine itself names as physics, not preference --
``invoking-shell-bash4-probe.sh`` (a child process cannot observe the
invoking shell's own version -- must itself be a shell script to introspect
the parent shell) and ``claude-machine-local.sh`` (must be ``source``d into
the *parent* shell -- a Python script cannot mutate the calling shell's own
environment). A guard that nags on doctrine's own named exceptions is
exactly the kind of nag that earns a bypass, so these are checked by
case-folded basename BEFORE anything else fires.

CARVE-OUTS: a target path with ``tests``, ``fixtures``, ``vendor``, or
``node_modules`` as a path segment (case-insensitive, anchored to a full
segment -- not a bare substring, so ``vendor-scripts/x.sh`` does not match)
is exempt. Vendored/fixture ``.sh`` content is not new authored logic this
project can port to Python; it is either someone else's tree or a test
double whose whole point is to exercise real shell behavior.

Negative-spec:
  - Does NOT deny/block anything -- CLASS is "advisory"; the envelope
    carries only ``additionalContext``, never ``permissionDecision``.
  - Does NOT fire on ``Edit``/``MultiEdit`` of any ``.sh`` file, existing or
    not -- only a ``Write`` of a path that does not yet exist on disk
    counts as "creating a new" file.
  - Does NOT fire on the two irreducible legs, by case-folded basename,
    regardless of which directory they are written under.
  - Does NOT fire on ``.bash``/``.zsh``/``.ps1``/``.py`` or any other
    extension -- ``.sh`` only.
  - Does NOT fire under a ``tests``/``fixtures``/``vendor``/``node_modules``
    path segment.
  - Never raises: any unexpected input shape, or an ``os.path.exists``
    failure, returns ``None`` (ALLOW/no-op) -- a guard that cannot resolve
    its own detection state has no basis to advise.

Spec backlink: example-doctrine-repo CLAUDE.md § Runtime conventions
Grep anchors: NEW-SH-FILE-NAKED-PYTHON
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import (
    is_trivial_reason as _is_trivial_reason,
    operator_override_note,
)

CLASS = "advisory"
MATCHERS = ["Write"]
PRIORITY = 160  # deny-offer band; next slot after 110/120/130/140/150 (see nudge_terminal_artifact_edit.py)

#: The two irreducible bash legs example-doctrine-repo doctrine names as physics, not
#: preference -- see module docstring NAMED-EXCEPTION TABLE. Case-folded
#: basename match, regardless of directory.
_IRREDUCIBLE_SH_BASENAMES = frozenset(
    {
        "invoking-shell-bash4-probe.sh",  # a child process cannot observe the invoking shell's own version
        "claude-machine-local.sh",  # must be `source`d into the parent shell; a Python script cannot do this
    }
)

#: Vendored/fixture path-segment carve-out -- case-insensitive, anchored to
#: a full path segment (see module docstring CARVE-OUTS).
_CARVEOUT_SEGMENTS = frozenset({"tests", "fixtures", "vendor", "node_modules"})

_ESCAPE_HATCH_ENV_VAR = "COORDINATOR_NEW_SH_PUNT"

_REASON_TEMPLATE = """New .sh: {file_path}. Write as a coordinator_core module instead -- spawns are costly cross-host. (invoking-shell-bash4-probe.sh/claude-machine-local.sh pre-approved.) Genuine third leg? proceed.{override_block}"""

_TRIVIAL_PUNT_REASON_TEMPLATE = """New .sh: {file_path}. Write as a coordinator_core module instead -- spawns are costly cross-host. Genuine third leg? proceed.

[hook] {env_var} set but trivial (< {min_len} chars) -- ignored.{override_block}"""


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    file_path = tool_input.get("file_path")
    return file_path if isinstance(file_path, str) else ""


def _is_carveout_path(normalized: str) -> bool:
    segments = [seg.lower() for seg in normalized.split("/") if seg]
    return any(seg in _CARVEOUT_SEGMENTS for seg in segments)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name != "Write":
            return None

        file_path = _extract_file_path(payload)
        if not file_path:
            return None

        normalized = file_path.replace("\\", "/")
        while "//" in normalized:
            normalized = normalized.replace("//", "/")

        if not normalized.lower().endswith(".sh"):
            return None

        basename = normalized.rsplit("/", 1)[-1]
        if basename.lower() in _IRREDUCIBLE_SH_BASENAMES:
            return None

        if _is_carveout_path(normalized):
            return None

        # Windows-style drive-absolute path (a drive letter followed by a
        # separator) even on a non-Windows host running this engine --
        # os.path.isabs alone would not recognize it on a POSIX host.
        resolved = file_path
        if not os.path.isabs(resolved) and not re.match(r"^[A-Za-z]:[\\/]", resolved):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                resolved = os.path.join(cwd, file_path)

        try:
            if os.path.exists(resolved):
                return None
        except Exception:
            return None

        _note = operator_override_note(
            _ESCAPE_HATCH_ENV_VAR,
            payload=payload,
            reason_placeholder="genuinely a third irreducible bash leg",
        )
        _override_block = "\n\n" + _note if _note else ""

        punt_reason = os.environ.get(_ESCAPE_HATCH_ENV_VAR, "") or ""
        if punt_reason:
            if not _is_trivial_reason(punt_reason):
                return None
            reason = _TRIVIAL_PUNT_REASON_TEMPLATE.format(
                file_path=file_path,
                env_var=_ESCAPE_HATCH_ENV_VAR,
                min_len=12,
                override_block=_override_block,
            )
        else:
            reason = _REASON_TEMPLATE.format(
                file_path=file_path,
                override_block=_override_block,
            )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard offers only on a
        # positive new-.sh-file match, never on an error.
        return None
