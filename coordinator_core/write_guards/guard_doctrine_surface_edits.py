"""coordinator_core.write_guards.guard_doctrine_surface_edits — advisory on edits
to always-loaded doctrine surfaces and privileged configuration.

An edit to a protected surface lands. The guard returns `additionalContext`
naming the review checks the run answers for itself (always-loaded byte
budget; no unprompted rewrite). It never denies and reads no sentinel file,
transcript turn or other human-created unlock.

Two classes of file are protected, for two distinct reasons; the advisory
text is per-class because a class-1 reason attached to coordinator.local.md
reads as false to anyone who has looked at the file.

CLASS 1 — always-loaded instruction prose. The global CLAUDE.md, the
EM-only operating-doctrine channel, and the repo-root project CLAUDE.md are
injected into every session and every dispatched agent's context. An edit
here changes everyone's instructions, fleet-wide.

CLASS 2 — privileged configuration. coordinator.local.md is NOT
always-loaded prose. Its frontmatter is the repo's privileged-execution and
authority surface:
  - `fast_test_cmd` / `full_test_cmd` and the `*_post_command` keys are
    command strings the ceremony machinery EXECUTES.
  - `fast_tier_unscoped_reason` / `fast_tier_shape` DISCHARGE the Tier-U
    caller-authority check for this repo's literal resolved `fast_test_cmd`.
An edit that narrows the gate command while keeping the declaration that
authorizes it is the one the review looks for.

Negative spec — do NOT "split" this guard by advising on coordinator.local.md's
prose body while skipping its frontmatter keys: the frontmatter is the
executed/authority half.

Protected surfaces (resolved, symlink-free, absolute real paths; matched
exactly — never by substring or basename, so a nested CLAUDE.md deeper in
the tree, e.g. coordinator/tests/CLAUDE.md, is NOT protected):
  - <content-root>/global-doctrine/CLAUDE.md              (class 1)
  - $HOME/.claude/CLAUDE.md              (class 1, the derived live copy)
  - <content-root>/coordinator/snippets/em-operating-doctrine.md  (class 1)
  - <repo-root>/CLAUDE.md          (class 1, repo-root project instructions)
  - <repo-root>/coordinator.local.md                      (class 2)

PLUS, generically (`_foreign_repo_root_surface`): ANY other repo's own
`<its-root>/CLAUDE.md` or `<its-root>/coordinator.local.md`, when the edit
targets it directly regardless of the session's own repo_root; and a DoE
doctrine path edited from a session anchored outside DoE.

Gate
----
`check()` returns None as its first step only after `machine-local set
coordinator.feature.doctrine_edit_gate off` has been run
(`machine_profile.feature_enabled("doctrine_edit_gate")`).

Weakening writes
----------------
A Write/Edit to a machine-local registry file or a Claude settings file that
turns the gate off, lowers a guard level, or turns a coordinator feature off
lands with an advisory naming what it weakens.

A main session `gem_admitted` reports as the verified live holder edits the
`_gem_doctrine_carve_out` surfaces with only the rot-token advisory.
`edit_admitted` is the pure verdict for the bash guard's Edit route: an Edit
is offered for every target except the derived live copy.

Unparseable payload (missing tool_input, no path key, tool not in the guarded
set, or a path outside the protected surfaces) allows silently.

Repo-identity recording
-----------------------
`check()` also calls `coordinator_core.pickup_assemble.compute_repo_identity_gate`
and logs its verdict (MATCH/MISMATCH/UNRESOLVED) via
`_write_repo_identity_advisory_log`, best-effort, to
`<repo-root>/.git/coordinator-sessions/<session_id>/repo-identity-gate.log`.
**READ-ONLY and ADVISORY ONLY — see DR-277
(`docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md`).** The
verdict is never consulted by the decision below.

Spec backlink: coordinator-content-repo
  coordinator/hooks/scripts/guard-doctrine-surface-edits.py
Spec backlink (repo identity): pln-a-ceremony-must-not-be-able-to-5e9421
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core import machine_profile, machine_resolver
from coordinator_core.content_root import read_content_root
from coordinator_core.machine_profile import feature_enabled
from coordinator_core.repo_identity_gate import compute_repo_identity_gate
from coordinator_core.bash_guards import _write_bump_applicability
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._gem_doctrine_carve_out import (
    doctrine_root_of,
    gem_admitted,
)
from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.write_guards._sentinel_write_guard import extract_target_path

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 127

GENERATES = []

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

_GATE_FEATURE = "doctrine_edit_gate"

# Rot-token advisory (B06) -- fires on an edit that ADDS one of three token
# shapes the old text lacked. Shapes and rationale: see plan
# docs/plans/2026-09-26-inbox-blitz-part-b-engine-defects.md, B06 body and
# "Design decisions". A bare numeral ("count-shaped") is deliberately NOT a
# shape here -- it cannot be told apart from ordinary prose numbers at write
# time. Return shape mirrors `wiki_changelog_prose_advisory` (additionalContext
# only; never denies).
_ROT_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ROT_VERSION_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")
_ROT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _rot_sha_candidate_ok(token: str) -> bool:
    has_digit = any(ch.isdigit() for ch in token)
    has_letter = any(ch in "abcdef" for ch in token)
    return has_digit and has_letter


def _rot_tokens(text: str) -> "set[str]":
    if not text:
        return set()
    tokens: "set[str]" = set()
    tokens.update(_ROT_DATE_RE.findall(text))
    tokens.update(_ROT_VERSION_RE.findall(text))
    for match in _ROT_SHA_RE.findall(text):
        if _rot_sha_candidate_ok(match):
            tokens.add(match)
    return tokens


def _rot_old_text(tool_name: str, tool_input: Dict[str, Any], target: str) -> str:
    """The "old side" for the B06 advisory diff.

    Edit -> `old_string`. MultiEdit -> concatenation of each edit's
    `old_string`. Write -> the file's CURRENT on-disk bytes (one read;
    absent/unreadable file treated as empty old text -- the whole `content`
    is then "net-added", which is the correct read for a brand-new file).
    """
    if tool_name == "Edit":
        old_s = tool_input.get("old_string")
        return old_s if isinstance(old_s, str) else ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return ""
        parts = []
        for edit in edits:
            if isinstance(edit, dict):
                old_s = edit.get("old_string")
                if isinstance(old_s, str):
                    parts.append(old_s)
        return "\n".join(parts)
    if tool_name == "Write":
        try:
            with open(target, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""
        except Exception:
            return ""
    return ""


def _rot_new_text(tool_name: str, tool_input: Dict[str, Any]) -> str:
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else ""
    if tool_name == "Edit":
        new_s = tool_input.get("new_string")
        return new_s if isinstance(new_s, str) else ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return ""
        parts = []
        for edit in edits:
            if isinstance(edit, dict):
                new_s = edit.get("new_string")
                if isinstance(new_s, str):
                    parts.append(new_s)
        return "\n".join(parts)
    return ""


def _rot_token_advisory(
    tool_name: str, tool_input: Dict[str, Any], target: str
) -> "Optional[Dict[str, Any]]":
    try:
        new_text = _rot_new_text(tool_name, tool_input)
        if not new_text:
            return None
        old_text = _rot_old_text(tool_name, tool_input, target)
        added = _rot_tokens(new_text) - _rot_tokens(old_text)
        if not added:
            return None
        names = ", ".join(sorted(added))
        reason = (
            "[doctrine-surface guard] advisory: this edit adds a date-, "
            f"version-, or SHA-shaped token ({names}) to a protected "
            "doctrine surface. A pointer to the file that holds the value "
            "does not go stale -- consider linking to it instead of "
            "restating it here."
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        return None


def _norm(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _git_root() -> "str | None":
    try:
        return resolve_repo_root() or None
    except Exception:
        return None


def _home_claude_md() -> "str | None":
    try:
        home = os.path.expanduser("~")
        if not home or home == "~":
            return None
        return _norm(os.path.join(home, ".claude", "CLAUDE.md"))
    except Exception:
        return None


_CONTENT_ONLY_SURFACES: "tuple[tuple[str, ...], ...]" = (
    ("global-doctrine", "CLAUDE.md"),
    ("coordinator", "snippets", "em-operating-doctrine.md"),
)

_PER_REPO_SURFACES: "tuple[tuple[str, ...], ...]" = (
    ("CLAUDE.md",),
    ("coordinator.local.md",),
)

_content_root_memo: "list[str | None]" = []


def _content_root() -> "str | None":
    if not _content_root_memo:
        try:
            resolved = read_content_root() or None
        except Exception:
            resolved = None
        _content_root_memo.append(_norm(resolved) if resolved else None)
    return _content_root_memo[0]


def _protected_entries(repo_root: "str | None") -> "list[tuple[str, str | None]]":
    """Protected paths paired with the root that owns each (None = the session's repo root).

    TWO SURFACE FAMILIES, AND THEY ANCHOR DIFFERENTLY. `global-doctrine/CLAUDE.md`
    and `coordinator/snippets/em-operating-doctrine.md` exist ONLY in
    coordinator-content-repo, so composing them against the session's own repo root resolves
    to nonexistent paths while the real DoE files sit outside the protected set
    (measured 2026-09-17 from a claude-klabauter session).

      - DoE-ONLY surfaces anchor on `_content_root()`, never on the session's
        repo. One clone of coordinator-content-repo serves the whole box.
      - PER-REPO surfaces (`CLAUDE.md`, `coordinator.local.md`) stay
        anchored on the session's own repo root: every repo has its own copy,
        each governing that repo's sessions.

    An unresolvable content-root pointer drops the DoE-anchored entries and
    lands on the per-repo set rather than on an error.
    """
    entries: "list[tuple[str, str | None]]" = []
    seen: "set[str]" = set()

    def _add(path: str, owning_root: "str | None") -> None:
        if path not in seen:
            seen.add(path)
            entries.append((path, owning_root))

    home_claude_md = _home_claude_md()
    if home_claude_md:
        _add(home_claude_md, None)

    content_root = _content_root()
    if content_root:
        for parts in _CONTENT_ONLY_SURFACES:
            try:
                _add(_norm(os.path.join(content_root, *parts)), content_root)
            except Exception:
                pass

    if repo_root:
        for parts in _CONTENT_ONLY_SURFACES + _PER_REPO_SURFACES:
            try:
                _add(_norm(os.path.join(repo_root, *parts)), None)
            except Exception:
                pass

    return entries


def _protected_paths(repo_root: "str | None") -> "list[str]":
    return [path for path, _ in _protected_entries(repo_root)]


_FOREIGN_ROOT_BASENAMES = ("CLAUDE.md", "coordinator.local.md")


def _foreign_repo_root_surface(target: str) -> "str | None":
    basename = os.path.basename(target)
    if basename not in _FOREIGN_ROOT_BASENAMES:
        return None
    try:
        target_root = resolve_repo_root(cwd=os.path.dirname(target)) or None
    except Exception:
        return None
    if not target_root:
        return None
    try:
        if target != _norm(os.path.join(target_root, basename)):
            return None
    except Exception:
        return None
    return _norm(target_root)


def _local_config_path(repo_root: "str | None") -> "str | None":
    if not repo_root:
        return None
    try:
        return _norm(os.path.join(repo_root, "coordinator.local.md"))
    except Exception:
        return None


def _why_protected(is_local_config: bool) -> str:
    if is_local_config:
        return (
            "is this repo's privileged configuration: its frontmatter holds "
            "the command strings the ceremony machinery executes "
            "(fast_test_cmd, full_test_cmd, *_post_command) and the "
            "declarations that discharge the Tier-U authority check for them "
            "(fast_tier_unscoped_reason, fast_tier_shape)"
        )
    return (
        "is always-loaded doctrine: it reaches every session and every "
        "dispatched agent in the fleet"
    )


_REVIEW_CHECKS_DOCTRINE = (
    "The edit lands. The run's review answers two checks: the always-loaded "
    "byte budget still holds, and the change was asked for (no unprompted rewrite)."
)
_REVIEW_CHECKS_CONFIG = (
    "The edit lands. The run's review answers one check: a narrowed gate "
    "command is not paired with the declaration that authorizes it."
)


def _surface_advisory(target: str, is_local_config: bool) -> str:
    checks = _REVIEW_CHECKS_CONFIG if is_local_config else _REVIEW_CHECKS_DOCTRINE
    return (
        f"[doctrine-surface guard] advisory: {target} "
        f"{_why_protected(is_local_config)}. {checks}"
    )


def _advisory_envelope(*texts: "Optional[str]") -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n".join(t for t in texts if t),
        }
    }


def _rot_text(advisory: "Optional[Dict[str, Any]]") -> "Optional[str]":
    return advisory["hookSpecificOutput"]["additionalContext"] if advisory else None


def _write_repo_identity_advisory_log(
    repo_root: "str | None", session_id: str, gate_result: Dict[str, Any]
) -> None:
    """Best-effort, non-blocking record of the repo-identity gate verdict
    (plan `docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md`
    § C4).

    **DR-277 (`docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md`):
    the verdict computed here is recorded ONLY — it is NEVER consulted by
    `check()`, on MISMATCH or on any other verdict.** A hard block on repo
    identity needs its own named DR-277 carve-out, not a quiet upgrade of this
    log call.

    Wrapped so any failure (including no resolvable repo root or session id)
    can NEVER change the decision.
    """
    if not repo_root or not session_id:
        return
    try:
        log_dir = Path(repo_root) / ".git" / "coordinator-sessions" / session_id
        # NEVER mkdir here: an advisory log line minting a session directory
        # for whatever `session_id` it is handed manufactured phantom sessions
        # into the corpus `liveness.live_session_ids` reads (nine such dirs by
        # 2026-08-19). A real session's directory is created by `core.init`;
        # absent it, there is no session to annotate. Pinned by
        # `test_advisory_log_never_creates_a_session_dir`.
        if not log_dir.is_dir():
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_dir / "repo-identity-gate.log", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                f"{ts} | gates.repo_identity | verdict={gate_result['verdict']} | "
                f"{gate_result['message']}\n"
            )
    except OSError as exc:
        print(
            "guard_doctrine_surface_edits: repo-identity advisory-log write "
            f"failed (decision unaffected): {exc}",
            file=sys.stderr,
        )

_PRIVILEGED_KEY_RE = re.compile(
    r"^[ \t]*[\w-]*(?:_cmd|_reason|_post_command|_shape)[ \t]*:", re.MULTILINE
)


def _edit_texts(tool_name: str, tool_input: Dict[str, Any], target: str) -> "tuple[str, str]":
    return (
        _rot_old_text(tool_name, tool_input, target),
        _rot_new_text(tool_name, tool_input),
    )


def _is_unprivileged_config_edit(
    tool_name: str, tool_input: Dict[str, Any], target: str
) -> bool:
    """`coordinator.local.md` edit that writes `project_type` and neither
    reads nor writes a `*_cmd`, `*_reason`, `*_post_command`, or `*_shape`
    key: no execution or authority surface moves."""
    old, new = _edit_texts(tool_name, tool_input, target)
    if "project_type" not in new:
        return False
    return not (_PRIVILEGED_KEY_RE.search(old) or _PRIVILEGED_KEY_RE.search(new))


def _is_first_time_create(tool_name: str, tool_input: Dict[str, Any], target: str) -> bool:
    """A Write that creates a missing CLAUDE.md, or a missing
    `coordinator.local.md` with no privileged keys (onboarding renders)."""
    if tool_name != "Write" or os.path.lexists(target):
        return False
    base = os.path.basename(target)
    if base == "CLAUDE.md":
        return True
    return base == "coordinator.local.md" and _is_unprivileged_config_edit(
        tool_name, tool_input, target
    )


_GATE_KEY = "coordinator.feature." + _GATE_FEATURE


def _gate_value(text: str) -> "str | None":
    parsed = machine_resolver._parse_toml_text(text)
    value = machine_resolver._flatten(parsed).get(_GATE_KEY)
    if value is None:
        return None
    return str(value).strip().lower()


def _registry_text_after(tool_name: str, tool_input: Dict[str, Any], before: str) -> str:
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else before
    edits = [tool_input] if tool_name == "Edit" else (tool_input.get("edits") or [])
    text = before
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str) or old not in text:
            continue
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


_OFF_WORDS = frozenset({"off", "false", "0", "no"})
_LEVEL_RANK = {name: len(machine_profile.LEVELS) - i for i, name in enumerate(machine_profile.LEVELS)}
_SETTINGS_NAMES = frozenset({"settings.json", "settings.local.json"})
_ENV_FEATURE_PREFIX = "MACHINE_LOCAL_COORDINATOR_FEATURE_"
_ENV_LEVEL_PREFIX = "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL"


def _level_of(flat: Dict[str, Any], name: str) -> str:
    """Effective level of guard `name` ('' = global) in a flattened registry."""
    def valid(key: str) -> "str | None":
        value = str(flat.get(key, "")).strip().lower()
        return value if value in _LEVEL_RANK else None

    glob = valid(machine_profile.LEVEL_KEY) or "warn"
    if not name:
        return glob
    return (
        valid(machine_profile.LEVEL_KEY + "." + name)
        or machine_profile.GUARD_DEFAULT_LEVEL.get(name)
        or glob
    )


def _registry_weakening(before: str, after: str) -> "str | None":
    flat_b = machine_resolver._flatten(machine_resolver._parse_toml_text(before))
    flat_a = machine_resolver._flatten(machine_resolver._parse_toml_text(after))
    if _gate_value(after) == "off" and _gate_value(before) != "off":
        return "turns off the doctrine-edit gate"
    prefix = machine_profile.LEVEL_KEY + "."
    names = {""} | {
        k[len(prefix):] for k in (*flat_a, *flat_b) if k.startswith(prefix)
    }
    for name in names:
        if _LEVEL_RANK[_level_of(flat_a, name)] < _LEVEL_RANK[_level_of(flat_b, name)]:
            return "lowers the guard level"
    return None


def _settings_dirs() -> "set[str]":
    home = os.path.expanduser("~")
    dirs = {os.path.join(home, ".claude"), os.path.join(home, ".coordinator-claude-settings")}
    for var in ("COORDINATOR_SETTINGS_HOME", "CLAUDE_HOME", "CLAUDE_CONFIG_DIR"):
        if os.environ.get(var):
            dirs.add(os.environ[var])
    return {_norm(d) for d in dirs}


def _is_settings_file(target: str) -> bool:
    if os.path.basename(target).lower() not in _SETTINGS_NAMES:
        return False
    parent = os.path.dirname(target)
    return os.path.basename(parent).lower() == ".claude" or parent in _settings_dirs()


def _env_block(text: str) -> Dict[str, Any]:
    try:
        env = json.loads(text).get("env")
    except (ValueError, AttributeError):
        return {}
    return env if isinstance(env, dict) else {}


def _settings_weakening(before: str, after: str) -> "str | None":
    env_b, env_a = _env_block(before), _env_block(after)
    for key, value in env_a.items():
        key_u = str(key).upper()
        new = str(value).strip().lower()
        old = str(env_b.get(key, "")).strip().lower()
        if key_u.startswith(_ENV_FEATURE_PREFIX) and new in _OFF_WORDS and old not in _OFF_WORDS:
            return "turns off a coordinator feature"
        if key_u.startswith(_ENV_LEVEL_PREFIX) and new in _LEVEL_RANK:
            suffix = str(key)[len(_ENV_LEVEL_PREFIX):].lstrip("_").lower().replace("_", "-")
            base = old if old in _LEVEL_RANK else machine_profile.guard_level(suffix) if suffix else (
                machine_profile._explicit(machine_profile.LEVEL_KEY, machine_profile.LEVELS) or "warn"
            )
            if _LEVEL_RANK[new] < _LEVEL_RANK[base]:
                return "lowers the guard level"
    return None


def _weakening_write(tool_name: str, tool_input: Dict[str, Any], target: str) -> "str | None":
    """What a Write/Edit/MultiEdit to a machine-local registry file or a Claude
    settings file newly weakens (gate off, guard level lowered, feature env
    off), else None. Judged on the simulated result against the file as it is."""
    try:
        reg_dir = machine_resolver.registry_dir()
        is_registry = target in {_norm(str(reg_dir / n)) for n in ("registry.local.toml", "registry.toml")}
    except Exception:
        is_registry = False
    if not is_registry and not _is_settings_file(target):
        return None
    try:
        with open(target, "r", encoding="utf-8") as fh:
            before = fh.read()
    except OSError:
        before = ""
    after = _registry_text_after(tool_name, tool_input, before)
    try:
        if is_registry:
            return _registry_weakening(before, after)
        return _settings_weakening(before, after)
    except Exception:
        return None


def _weakening_advisory(what: str) -> str:
    return (
        f"[doctrine-surface guard] advisory: this edit {what}. The edit lands. "
        "The run's review answers one check: the change was asked for."
    )


def _anchor_inside(payload: Dict[str, Any], root: str) -> "bool | None":
    """Whether the session's launch anchor is at or under ``root``; None when no anchor resolves."""
    anchor = _write_bump_applicability.resolve_launch_anchor(
        payload.get("session_id") or "", cwd=payload.get("cwd")
    )
    if not anchor:
        return None
    folded = casefold_path(os.path.realpath(anchor)).rstrip("/")
    folded_root = casefold_path(os.path.realpath(root)).rstrip("/")
    return folded == folded_root or folded.startswith(folded_root + "/")


def _match_protected(
    target: str, repo_root: "str | None", payload: Dict[str, Any]
) -> "tuple[bool, str | None]":
    """(matched, owning root): a protected entry, a foreign repo-root surface,
    or a DoE doctrine path edited from a session anchored outside DoE."""
    for path, entry_root in _protected_entries(repo_root):
        if target == path:
            return True, entry_root
    foreign_root = _foreign_repo_root_surface(target)
    if foreign_root is not None:
        return True, foreign_root
    try:
        doc_root = doctrine_root_of(target)
        if doc_root and _anchor_inside(payload, doc_root) is False:
            return True, _norm(doc_root)
    except Exception:
        pass
    return False, None


def edit_admitted(payload: Dict[str, Any], target: str) -> bool:
    """Whether an Edit on ``target`` is open to this caller: every path except the derived live copy.

    Pure: reads nothing but the derived-copy check; writes and unlinks nothing.
    """
    try:
        from coordinator_core.write_guards import block_derived_global_doctrine_write as derived

        return not derived._is_derived_live_copy(target)
    except Exception:
        return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not feature_enabled(_GATE_FEATURE):
        return None

    tool_name = payload.get("tool_name", "")
    if tool_name not in _GUARDED_TOOLS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return None

    try:
        target = _norm(target_raw)
    except Exception:
        return None

    repo_root = _git_root()

    # Advisory `gates.repo_identity` fact; the verdict never reaches the decision (DR-277).
    session_id = payload.get("session_id") or ""
    if repo_root:
        repo_identity_gate = compute_repo_identity_gate(Path(repo_root), session_id or None)
        _write_repo_identity_advisory_log(repo_root, session_id, repo_identity_gate)

    weakened = _weakening_write(tool_name, tool_input, target)
    if weakened:
        return _advisory_envelope(_weakening_advisory(weakened))

    if gem_admitted(payload, target):
        return _rot_token_advisory(tool_name, tool_input, target)

    matched, _owning_root = _match_protected(target, repo_root, payload)
    if not matched:
        return None

    rot = _rot_token_advisory(tool_name, tool_input, target)

    is_local_config = os.path.basename(target) == "coordinator.local.md"
    if _is_first_time_create(tool_name, tool_input, target):
        return None
    if is_local_config and _is_unprivileged_config_edit(tool_name, tool_input, target):
        return None

    return _advisory_envelope(_surface_advisory(target_raw, is_local_config), _rot_text(rot))
