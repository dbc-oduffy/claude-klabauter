"""coordinator_core.write_guards.block_oss_mirror_memo_delivery — hard-deny guard.

Closes the direct-``Write`` leg of the OSS-mirror memo hole. Modelled closely
on ``block_home_dir_memo_delivery.py`` — same structure, same fail-open
discipline, same use of ``coordinator_core.ops._path_guard.contained_path``.

Purpose: doctrine (example-doctrine-repo ``CLAUDE.md``) is unambiguous that
``coordinator/`` percolates ONE-WAY, example-doctrine-repo→OSS mirror only — the OSS
``coordinator-claude`` repo (and any sibling publish-target mirror) is a
publish DESTINATION, never a working tree, and it must NEVER receive a
cross-repo memo: a memo delivered there sits unactioned, invisible to every
``/pickup`` and every inbox sweep, because no EM lives there to read it.

The sanctioned ``cross-repo-memo`` CLI (``claude-klabauter
coordinator/bin/cross-repo-memo``) already closes this for the CLI path —
``_is_publish_target_em()`` / ``_publish_target_owner()`` /
``_publish_target_rejection_msg()`` / ``_redirect_kind()``, backed by
machine-local ``publish.mirrors.<key>.owner`` enumeration plus the
code-pinned ``REDIRECT_ALIASES`` fallback (``coordinator/bin/lib/
coordinator_registry.py``). That machinery is NOT rebuilt here.

The remaining hole is the HAND-WRITTEN delivery: an agent that skips the CLI
and issues a ``Write``/``Edit``/``MultiEdit``/``NotebookEdit`` straight at
``<oss-mirror-clone>/cross-repo/inbox/<file>.md`` is unguarded — the
home-dir guard's scope (``<home>/.claude/cross-repo/**``) never sees this
path, and no other guard vets an arbitrary sibling-clone path.

Block predicate: DENY when the tool is a file-mutating tool AND its resolved
target path is equal to, or a descendant of, ``<mirror-root>/cross-repo`` for
ANY publish-mirror root this machine's registry currently resolves — where
``<mirror-root>`` comes from ``publish.mirrors.<key>.path`` (the SAME
registry table the ``cross-repo-memo`` CLI already trusts for publish-target
detection, read here via ``coordinator_core.ops.fleet._memo_resolver.
read_publish_mirrors()`` rather than re-parsing TOML a second time).

Scope is deliberately ``<mirror-root>/cross-repo/**`` and nothing else:
ordinary percolation writes elsewhere under a mirror (e.g.
``<mirror>/coordinator/skills/foo/SKILL.md``) MUST pass through silently —
percolation is the mirror's LEGITIMATE purpose; breaking that would be the
primary regression this guard could cause.

Maintainer override: ``COORDINATOR_OVERRIDE_OSS_MIRROR_MEMO_GUARD=1``
disables this guard entirely. Advertised in the deny text via the shared
``operator_override_note`` helper, per this package's general convention
(unlike ``block_home_dir_memo_delivery``'s deliberately-unadvertised
override — that guard's scope has no legitimate use at all, whereas an OSS
mirror clone IS a legitimate write target for everything except its
``cross-repo/`` subtree, so advertising the escape hatch here matches every
other guard in this tree rather than that one exception).

Registry resolution failure (no ``registry.toml``/``registry.local.toml``,
unparseable file, no mirror declares a ``.path``) is FAIL-OPEN (allow) —
mirrors ``block_home_dir_memo_delivery``'s posture on every unresolvable
input. A guard that hard-denies when it cannot resolve config would break
ordinary work on a fresh clone with no registry provisioned yet.

Negative-spec:
  - Does NOT duplicate the ``cross-repo-memo`` CLI's publish-target
    ownership/redirect logic (``_is_publish_target_em`` et al.) — this guard
    is a narrower, independent safety net over the hand-written-``Write``
    leg only; the CLI path is already closed.
  - Does NOT hand-roll TOML parsing — mirror paths come from
    ``coordinator_core.ops.fleet._memo_resolver.read_publish_mirrors()``,
    the ONE TOML-merge authority the CLI's own claude-klabauter-side registry reader
    already uses (``registry.toml`` baseline + ``registry.local.toml``
    per-machine override, ``[publish.mirrors.<key>]`` nested tables).
  - Does NOT hand-roll the symlink/``..``-resolution — containment
    delegates directly to ``coordinator_core.ops._path_guard.contained_path``
    (fidelity rule 8: reuse shared helpers, don't reinvent).
  - Does NOT filter on ``tool_name`` beyond the explicit membership check —
    MATCHERS-based filtering in the engine already restricts invocation to
    the same four tools; the in-body check is kept for defense-in-depth,
    matching every sibling guard's own redundancy.
  - Never raises: any unexpected input shape, registry-read failure, or
    internal error is treated as ALLOW (fail-open).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._memo_resolver import read_publish_mirrors
from coordinator_core.write_guards._case_fold_path import casefold_path as _casefold_path

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 132

#: Advertised maintainer/test escape hatch — see module docstring for why
#: this guard advertises it (unlike block_home_dir_memo_delivery's
#: deliberately-unadvertised twin).
OVERRIDE_ENV = "COORDINATOR_OVERRIDE_OSS_MIRROR_MEMO_GUARD"

#: Tools whose tool_input names a file this guard must vet.
_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

#: tool_input keys that can carry the target path, in probe order.
#: NotebookEdit uses notebook_path; the rest use file_path.
_PATH_KEYS = ("file_path", "notebook_path", "path")

_CROSS_REPO_DIRNAME = "cross-repo"


def _guarded_roots() -> "list[Path]":
    """``<mirror-root>/cross-repo`` for every publish-mirror this machine's
    registry currently resolves to a ``.path``.

    Reads via ``read_publish_mirrors()`` (the shared TOML-merge authority the
    ``cross-repo-memo`` CLI's claude-klabauter-side resolver already uses) rather than
    a second hand-rolled registry read. A mirror declared with no ``.path``
    (not yet provisioned on this machine) contributes no root — nothing to
    guard until the operator provisions it. Never raises: any read/parse
    failure inside ``read_publish_mirrors()`` already degrades to ``{}``.
    """
    roots: "list[Path]" = []
    seen: "set[Path]" = set()
    try:
        mirrors = read_publish_mirrors()
    except Exception:
        return []
    for entry in mirrors.values():
        path = entry.get("path") if isinstance(entry, dict) else None
        if not path or not isinstance(path, str):
            continue
        try:
            root = Path(_casefold_path(str(Path(path) / _CROSS_REPO_DIRNAME)))
        except Exception:
            continue
        if root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def _deny_reason(target: str, payload: Optional[Dict[str, Any]] = None) -> str:
    _note = operator_override_note(OVERRIDE_ENV, payload=payload)
    return (
        f"[oss-mirror-memo] DENY: {target} — publish-only mirror; memo "
        "unactioned, no EM reads it.\n"
        "Use instead:\n"
        "  bin/cross-repo-memo --to <real-receiver-em> --topic <slug> "
        '--title "<one line>"'
        + ("\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if os.environ.get(OVERRIDE_ENV, "") == "1":
        return None

    try:
        if payload.get("tool_name", "") not in _GUARDED_TOOLS:
            return None

        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None

        target_raw = ""
        for key in _PATH_KEYS:
            val = tool_input.get(key, "")
            if isinstance(val, str) and val.strip():
                target_raw = val.strip()
                break
        if not target_raw:
            return None

        roots = _guarded_roots()
        if not roots:
            return None

        candidate = Path(_casefold_path(target_raw))
        if contained_path(candidate, roots) is None:
            return None

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _deny_reason(target_raw, payload),
            }
        }
    except Exception:
        # Fail-open on any unexpected error, matching every documented
        # fail-open branch in the reference guard.
        return None
