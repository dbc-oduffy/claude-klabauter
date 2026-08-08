"""coordinator_core.write_guards.block_dev_side_mirror_wiki — advisory guard.

Originally a Python engine-ification of example-doctrine-repo's retired
``coordinator/hooks/scripts/block-dev-side-mirror-wiki.sh`` PreToolUse
(Write|Edit|NotebookEdit) hook (deleted 2026-07-16, example-doctrine-repo ``2f8b8450``), per
the naked-Python hook migration (write_guards/INTERFACE.md).

Purpose (ported verbatim from the reference hook, deny/flag condition
unchanged): under Option B (2026-05-15), plugin-doctrine wikis live ONLY in
the bundled plugin tree. A write to the dev-side path
(``~/.claude/docs/wiki/<name>.md``) re-introduces the write-direction trap —
two copies, drift is the default. This hook intercepts a write to the
dev-side path and, if a bundled copy of the same filename already exists at
the plugin's ``docs/wiki/<name>.md``, flags the write with a message naming
the bundled path.

CLASS = "advisory" (2026-08-06 write-guard classification pass,
docs/plans/2026-08-06-... B5): reclassified from hard-deny. A dev-side wiki
mirror is a plain extra file, not a silent or total loss — the drift it
risks is caught the moment anyone diffs or reads the dev-side copy against
the bundled one, and the write that created it remains fully correctable
(delete the mirror, or edit the bundled copy instead). That is well short of
the irreversible-harm bar this family's hard-deny band is reserved for (see
this package's classification test).

This is otherwise a faithful port: it preserves the reference hook's
PLUGIN_ROOT resolution (``CLAUDE_PLUGIN_ROOT`` env var, else
``$(cat "${CLAUDE_HOME:-$HOME}/.claude/.doe-root")/coordinator``), trust-checked
via the canonical ``coordinator_core.trusted_root_guard.is_trusted`` (fail-open
call-site shape — see that module for the full anchor list), the
escape hatch, the ``~`` expansion and backslash normalization, the dev-wiki
prefix match (against plain ``$HOME``, deliberately NOT ``CLAUDE_HOME`` — see
negative-spec), and the reason text verbatim — only the envelope shape
(advisory, not deny) and the lead-in sentence changed.

Ported from the retired example-doctrine-repo bash guard ``block-dev-side-mirror-wiki.sh``
  (deleted 2026-07-16, example-doctrine-repo ``2f8b8450``).

Negative-spec:
  - Does NOT vary the dev-wiki prefix by ``CLAUDE_HOME`` — the reference hook
    computes ``DEV_WIKI_ABS`` from plain ``${HOME}``, distinct from the
    ``${CLAUDE_HOME:-$HOME}`` used for PLUGIN_ROOT/trust-core resolution; this
    module preserves that exact asymmetry rather than "fixing" it to be
    consistent.
  - Does NOT add an explicit ``additionalContext`` advisory when no bundled
    copy exists — the reference hook's "Allow but warn" comment at that
    branch is a code comment only; the actual behavior is a silent
    ``exit 0`` with no output, ported here as returning ``None``.
  - Does NOT filter on ``tool_name`` inside ``check()`` — the reference hook
    has no such explicit guard for this hook (unlike the other two ported
    guards); MATCHERS-based filtering in the engine is the only tool-name
    gate, matching the reference exactly.
  - Does NOT read stdin — the engine passes ``payload`` directly.
  - Does NOT deny — advisory only; the write always lands, with the bundled
    path surfaced via ``additionalContext``.
  - Never raises: any unexpected input shape or internal error is treated as
    ALLOW/no-op (fail-open on error), matching the reference hook's
    ``set -uo pipefail`` fail-open discipline (including the reference's own
    fail-open-on-unresolvable-PLUGIN_ROOT branch, ``[ ! -d "$PLUGIN_ROOT" ]``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core._settings_home import machine_local_dir
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.trusted_root_guard import is_trusted as _is_trusted_root
from coordinator_core.write_guards._case_fold_path import casefold_path

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "NotebookEdit"]
PRIORITY = 172  # advisory band; next slot after block_completion_monolith_write (171)

#: Escape hatch.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_WIKI_MIRROR"

#: Sanctioned --plugin-dir developer opt-out for the trust-core.
_PLUGIN_ROOT_TRUSTED_ENV_VAR = "COORDINATOR_PLUGIN_ROOT_TRUSTED"


def _home() -> str:
    """``$HOME`` — falls back to ``os.path.expanduser("~")`` if unset
    (bash's bare ``$HOME`` has no such fallback, but an unset HOME is a
    degenerate environment this module must not crash on; this is a
    defensive addition, not a behavior port)."""
    return (
        os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )


def _claude_home() -> str:
    """``${CLAUDE_HOME:-$HOME}`` — reference hook lines 33, and the
    trust-core's line 143/147."""
    return os.environ.get("CLAUDE_HOME") or _home()


def _resolve_default_plugin_root(claude_home: str) -> str:
    """``<resolved .doe-root>/coordinator`` (reference hook line 33). A
    missing/unreadable ``.doe-root`` yields an empty resolution, so the default
    degrades to the literal string ``"/coordinator"`` — that degrade is ported
    as-is, not repaired.

    Read order is durable-first (DR-071/DR-072), matching every other reader:
    ``<settings-home>/machine-local/.doe-root`` then the legacy
    ``${CLAUDE_HOME:-$HOME}/.claude/.doe-root``. The durable rung was added
    2026-07-28 when the generator stopped writing the legacy target — without it
    this guard silently degrades to ``"/coordinator"``, fails its
    ``_is_trusted_root`` check, and fail-opens ALLOW on every dev-side mirror
    write.

    Kept as a local read rather than routed through
    ``coordinator_core.doe_root_pointer.read_doe_root_pointer_file`` (which the
    other five relocated readers now share): this guard ports a reference hook
    line-for-line and takes ``claude_home`` as an argument, and its documented
    degrade-to-``"/coordinator"`` behavior on an absent pointer is load-bearing
    parity, not an accident to normalize away.
    """
    for candidate in (
        machine_local_dir() / ".doe-root",
        Path(claude_home) / ".claude" / ".doe-root",
    ):
        try:
            content = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            return content + "/coordinator"
    return "/coordinator"


def _resolve_plugin_root() -> str:
    """Port of the reference hook's PLUGIN_ROOT resolution + trust-guard
    fail-open application (lines 33-43). Returns "" if untrusted or
    unresolvable — the caller then treats that as fail-open ALLOW (row
    ``[ ! -d "$PLUGIN_ROOT" ]``)."""
    claude_home = _claude_home()
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or _resolve_default_plugin_root(claude_home)

    if not _is_trusted_root(plugin_root):
        return ""

    return plugin_root


def _expand_tilde(file_path: str, home: str) -> str:
    """Port of bash's ``"${FILE_PATH/#\\~/$HOME}"`` — replaces a LEADING
    literal ``~`` only (not ``~user``), leaving the rest of the string
    untouched (reference hook line 71)."""
    if file_path == "~":
        return home
    if file_path.startswith("~/"):
        return home + file_path[1:]
    return file_path


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        plugin_root = _resolve_plugin_root()
        if not plugin_root or not os.path.isdir(plugin_root):
            # Fail-open: cannot locate bundled wiki tree, so cannot evaluate
            # the write-direction trap (reference hook lines 41-43).
            return None
        bundled_wiki = plugin_root + "/docs/wiki"

        # Honor escape hatch (reference hook lines 53-58).
        if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name == "NotebookEdit":
            file_path = tool_input.get("notebook_path") or ""
        else:
            file_path = tool_input.get("file_path") or ""

        if not file_path:
            return None

        home = _home()
        file_path_expanded = _expand_tilde(file_path, home)
        # Windows-path backslash normalization (reference hook line 73).
        file_path_expanded = file_path_expanded.replace("\\", "/")

        # DEV_WIKI_ABS is deliberately plain $HOME, not $CLAUDE_HOME (see
        # module negative-spec) — reference hook lines 77-79.
        dev_wiki_abs = (home + "/.claude/docs/wiki").replace("\\", "/")
        dev_wiki_norm = dev_wiki_abs.rstrip("/")

        # Comparison-only fold: `file_path_expanded` itself stays original-case
        # below (it feeds `wiki_filename` -> `bundled_path` -> `os.path.isfile`,
        # a real disk lookup that must not be lowercased on a case-sensitive
        # filesystem). Only these local copies are casefolded, and both sides
        # of the comparison must be folded together or the bypass reopens.
        if not casefold_path(file_path_expanded).startswith(
            casefold_path(dev_wiki_norm) + "/"
        ):
            return None

        wiki_filename = file_path_expanded.rsplit("/", 1)[-1]

        bundled_path = bundled_wiki + "/" + wiki_filename
        if not os.path.isfile(bundled_path):
            # No bundled copy — new project-level wiki, or a new
            # plugin-doctrine wiki being created for the first time in the
            # wrong place. Allow (silently — reference hook's "Allow but
            # warn" is a comment only, see negative-spec).
            return None

        reason = (
            f"{wiki_filename} mirrors a bundled wiki. Use instead:\n"
            f"  {bundled_path}\n\n"
            + operator_override_note(_OVERRIDE_ENV_VAR)
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-open on any unexpected error — mirrors the reference hook's
        # fail-open-on-error discipline (never fail-closed on a hard guard's
        # own internal error).
        return None
