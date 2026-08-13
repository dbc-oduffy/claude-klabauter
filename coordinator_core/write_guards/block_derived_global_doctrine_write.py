"""coordinator_core.write_guards.block_derived_global_doctrine_write — hard-deny
guard for a direct write to the DERIVED live global-doctrine copy
(``~/.claude/CLAUDE.md``).

Purpose (the failure this closes): of the four ``CLAUDE.md``-class surfaces,
global doctrine is AUTHORED at ``global-doctrine/CLAUDE.md`` in the example-doctrine-repo
repo; ``coordinator/hooks/scripts/derive-global-doctrine-live-copy.py`` (a
PostToolUse hook, example-doctrine-repo repo) re-derives ``~/.claude/CLAUDE.md`` from it
on every matching write to the authoring copy. There is no ``--plugin-dir``-
style live resolution for the derived copy the way there is for
``skills/``/``hooks/``/``bin/``/``lib/`` — it is a plain file, kept in sync
only by that one hook firing.

So an agent that edits ``~/.claude/CLAUDE.md`` directly gets NO error, sees
the edit land, and has it silently overwritten the next time the derivation
hook fires — the work vanishes with no signal at the moment of loss. This is
a distinct failure from authority (``block_unauthorized_claude_md_write``, a
DIFFERENT guard governing WHICH agent may write a CLAUDE.md-class surface)
and from size (``check_claude_md_size``): this guard is about SURFACE
ROUTING — which of the four copies is the authoring one — and fires
regardless of who is writing or how large the edit is.

Deny, not advisory (evidence, not a guess): the loss this guard prevents is
TOTAL (the whole edit vanishes) and SILENT (no error at write time, no error
at the moment of loss either) — the same total-and-silent shape the example-doctrine-repo
``invisible-doctrine.md`` FLOOR classification exists for. An advisory would
let the edit land and vanish anyway; only a deny actually prevents the loss.
The one concern a deny would need to clear — blocking the re-derivation hook
itself — does not apply: the hook (see its own docstring, "Portability")
writes via ``shutil.copyfile`` directly from Python, invoked as a PostToolUse
hook subprocess. It never issues a ``Write``/``Edit``/``MultiEdit``/
``NotebookEdit`` tool call, so this PreToolUse guard never observes that
write at all — a hard deny here cannot cause the self-inflicted outage a
deny-the-deriver guard would risk.

Exact target set (do NOT widen): the ONE derived surface,
``<home>/.claude/CLAUDE.md``, for every plausible home root this host's
``Path.home()``/``~`` could resolve to (``HOME``, ``USERPROFILE``,
``os.path.expanduser("~")``), plus ``CLAUDE_HOME`` honored as a direct
``.claude`` root (that env var points AT ``.claude``, not at ``$HOME`` — the
same asymmetry ``block_dev_side_mirror_wiki`` and
``block_home_dir_memo_delivery`` both preserve). Deliberately NOT
``coordinator_core.claude_md_budget.is_claude_md_class``/
``is_governed_claude_md`` — both of those predicates ALSO match
``global-doctrine/CLAUDE.md`` (the authoring surface, where writes are
correct), example-doctrine-repo's ``coordinator/CLAUDE.md`` (dev-repo-sentinel gated, also an
authoring surface), the two ``coordinator/snippets/*.md`` surfaces, and any
repo-root project ``CLAUDE.md`` — none of those are derived, and firing on
them would block legitimate authoring, which is worse than the defect this
guard closes. This module intentionally does NOT reuse either predicate;
widening it to do so would reopen exactly that over-fire.

Matching is pure STRING normalization (backslash -> forward-slash,
Unicode casefold), never ``Path.resolve()``: a ``Path(...).resolve()``
comparison interprets a Windows-separator string literally on a POSIX
interpreter (a single odd path segment relative to cwd, not a drive-rooted
path), which would make a Windows-shaped payload UNMATCHABLE on this
guard's own macOS/Linux CI. String normalization first — mirroring
``block_dev_side_mirror_wiki``'s own ``_expand_tilde``/backslash-normalize
discipline, not ``block_home_dir_memo_delivery``'s
``contained_path``/``Path.resolve()`` discipline — is what keeps a
Windows-separator-form payload testable from a POSIX interpreter (see this
module's own test file for a same-host Windows-separator assertion, no
``WindowsPath`` construction required).

No ``agent_id`` gate: unlike ``block_unauthorized_claude_md_write`` (an
authority question, scoped to the dispatched-subagent path DR-104's evidence
is about), this is a ROUTING question that applies identically whether the
EM or a dispatched subagent performs the write — an EM unaware of the
re-derivation hook loses the edit exactly the same way a subagent does.

Deny text: leads with the alternative (edit the authoring copy in the
Example-doctrine-repo repo, path resolved via ``coordinator_core.machine_resolver.
registry_get("repos.example_doctrine_repo")`` at message-render time, never a literal),
then states the consequence (silent overwrite, no error, no signal) without
overstating severity — this guard defends against an EAGER edit landing in
the wrong place, not a hostile one.

PRIORITY reasoning (all three CLAUDE.md-class guards can match
``~/.claude/CLAUDE.md``; in the hard-deny phase the first non-None ``check``
wins): this guard is PRIORITY 10, ahead of ``check_claude_md_size`` (15) and
``block_unauthorized_claude_md_write`` (45). The routing defect this guard
names is true unconditionally — regardless of the edit's size or the
writer's authorization — and is the MORE FUNDAMENTAL problem: an edit to the
wrong file is going to vanish whether or not it is oversized, and whether or
not its author holds a grant. Telling the agent "you are editing a copy that
gets silently overwritten" first is strictly more useful than either "this
edit is too big" or "you lack a grant" for an edit that is going to be lost
either way; those two guards' own conditions become moot once this one has
already redirected the agent to the authoring surface.

Override env var: ``COORDINATOR_OVERRIDE_DERIVED_GLOBAL_DOCTRINE_WRITE``,
following the same rare-use escape-hatch convention every sibling
write_guards module carries (see ``INTERFACE.md`` fidelity rule 3).

Spec backlink: example-doctrine-repo coordinator/hooks/scripts/
  derive-global-doctrine-live-copy.py (the derivation this guard protects);
  example-doctrine-repo CLAUDE.md "Four CLAUDE.md-class surfaces, one trap" (the failure
  this guard names).
Precedent (module shape — derived-vs-authoring routing, not authority):
  coordinator_core/write_guards/block_dev_side_mirror_wiki.py.
Distinct from (do not conflate): coordinator_core/write_guards/
  block_unauthorized_claude_md_write.py (authority, not routing).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.machine_resolver import registry_get

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 10

_INTERCEPTED_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

#: Rare-use escape hatch — read the module docstring before invoking.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_DERIVED_GLOBAL_DOCTRINE_WRITE"

#: NEGATIVE-SPEC: there is deliberately no literal fallback root here. A
#: message that fabricates a path for an unregistered root hands the reader
#: somewhere that does not exist — and a codename literal in that position
#: publishes as a redaction placeholder naming nothing at all. When the
#: registry lookup fails, `_deny_reason` names the registry key the operator
#: sets instead of inventing a location.


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """``file_path``, falling back to ``notebook_path`` for NotebookEdit."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _normalize(p: str) -> str:
    """Backslash -> forward-slash, Unicode casefold — matches this module's
    own string-first matching discipline (see module docstring)."""
    return p.replace("\\", "/").casefold()


def _home_candidates() -> List[str]:
    """Every plausible operator-home root this host's ``Path.home()``/``~``
    could resolve to — union of ``HOME``, ``USERPROFILE``, and
    ``os.path.expanduser("~")`` — mirroring
    ``block_home_dir_memo_delivery._guarded_roots``'s own union discipline,
    since the derivation hook's own live-copy path
    (``derive-global-doctrine-live-copy.py``'s ``_live_path``,
    ``Path.home() / ".claude" / "CLAUDE.md"``) is itself ``Path.home()``-
    anchored, and ``Path.home()`` consults these same variables depending on
    platform."""
    homes: List[str] = []
    for env_key in ("HOME", "USERPROFILE"):
        val = os.environ.get(env_key, "")
        if val and val.strip():
            homes.append(val.strip())
    try:
        expanded = os.path.expanduser("~")
        if expanded and expanded != "~":
            homes.append(expanded)
    except Exception:
        pass

    seen = set()
    out: List[str] = []
    for h in homes:
        key = _normalize(h)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out


def _expand_tilde(file_path: str, home: str) -> str:
    """Port of the ``block_dev_side_mirror_wiki`` tilde-expansion shape —
    replaces a LEADING literal ``~`` only (not ``~user``)."""
    if file_path in ("~", "~/", "~\\"):
        return home
    if file_path.startswith("~/") or file_path.startswith("~\\"):
        return home.rstrip("/\\") + "/" + file_path[2:]
    return file_path


def _derived_live_target_suffixes() -> List[str]:
    """Normalized candidate strings for the ONE derived surface this guard
    governs — ``<home>/.claude/CLAUDE.md`` for every home root
    ``_home_candidates`` resolves, plus ``CLAUDE_HOME`` honored as a direct
    ``.claude`` root (see module docstring)."""
    targets: List[str] = []
    for home in _home_candidates():
        targets.append(_normalize(home.rstrip("/\\") + "/.claude/CLAUDE.md"))

    claude_home = os.environ.get("CLAUDE_HOME", "")
    if claude_home and claude_home.strip():
        targets.append(_normalize(claude_home.strip().rstrip("/\\") + "/CLAUDE.md"))

    seen = set()
    out: List[str] = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _is_derived_live_copy(file_path: str) -> bool:
    if not file_path:
        return False

    expanded = file_path
    if file_path.startswith("~"):
        homes = _home_candidates()
        if homes:
            expanded = _expand_tilde(file_path, homes[0])

    normalized = _normalize(expanded)
    return normalized in _derived_live_target_suffixes()


def _authoring_path() -> Optional[str]:
    """The authoring surface's path, resolved from the registry at
    message-render time — never a literal (module docstring "Deny text").

    Returns None when the root is unregistered on this machine, so the
    caller can name the registry key rather than render a fabricated path.
    """
    root = registry_get("repos.example_doctrine_repo")
    if not root:
        return None
    return root.replace("\\", "/").rstrip("/") + "/global-doctrine/CLAUDE.md"


def _deny_reason(file_path: str, payload: Optional[Dict[str, Any]] = None) -> str:
    authoring = _authoring_path()
    target = (
        f"edit instead: `{authoring}`"
        if authoring
        else (
            "the authoring root is unregistered here — "
            "`machine-local set repos.example_doctrine_repo <path>` names it"
        )
    )
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        f"BLOCKED: not the authoring source — {target} (not `{file_path}`); "
        "a re-derivation hook leaves edits made here silently overwritten, "
        "with no error and no signal at the moment of loss."
        + ("\n\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the derived-global-doctrine-copy routing guard against a
    PreToolUse payload. Returns ``None`` (allow) or the nested hard-deny
    envelope."""
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    if not _is_derived_live_copy(file_path):
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(file_path, payload),
        }
    }
