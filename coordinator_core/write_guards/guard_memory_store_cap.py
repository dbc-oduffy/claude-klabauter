"""coordinator_core.write_guards.guard_memory_store_cap — hard-deny guard.

Discharges AC14 of DoE-claude's
``docs/plans/2026-07-30-boot-doctrine-cut-and-refill-gate.md`` (chunk C12).
``MEMORY.md`` is the one always-on boot surface previously governed by pure
prose (``coordinator/commands/update-docs.md``'s "a pointer index, not a
state mirror" hygiene note) with no enforcing artifact — the discharge test
failing in the open. This guard is that artifact.

Scope: ``<home>/.claude/projects/<any-slug>/memory/**`` — Claude Code's
native per-project auto-memory store. ``<any-slug>`` is a wildcard segment
(one auto-memory directory per project the harness has ever rooted a session
in), which is why this guard cannot reuse
``coordinator_core.ops._path_guard.contained_path`` against a fixed root list
the way its nearest twin does; containment is checked against the coarser
``<home>/.claude/projects`` root and the wildcard segment is then matched by
relative-path shape (exactly three parts: ``<slug>/memory/<filename>``) —
this bounds the guard to direct children of a project's ``memory/`` dir,
never a deeper nesting, matching AC14's two-file-shape universe (the index
file and its sibling body files).

Four limits (AC14 owns the numbers; restated here because this module
implements against them): ``MEMORY.md`` <= 2,000 B; <= 20 index rows (lines
starting ``"- "``); <= 100 chars per index row; each sibling ``<slug>.md``
body file <= 1,500 B. Row-count and byte-count are both needed — a byte cap
alone admits 20 bloated rows, a row cap alone admits 51 terse ones.

Home resolution: copied from
``coordinator_core.write_guards.block_home_dir_memo_delivery`` (fidelity
rule 8 / the C12 body's explicit instruction to reuse that guard's idiom
rather than inventing one) — union of ``USERPROFILE``, ``HOME``,
``Path.home()``, plus ``CLAUDE_HOME`` treated as a direct ``.claude`` root,
never a first-wins chain (a Git-Bash ``HOME``/``USERPROFILE`` divergence on
Windows must never open a hole). Also copies that guard's case-fold fix:
``os.path.normcase`` is a no-op on POSIX (macOS included), so the roots and
candidate are additionally casefolded via the shared
``coordinator_core.write_guards._case_fold_path.casefold_path`` helper
before ``contained_path``'s (case-sensitive) ``relative_to`` comparison —
without it, a case-varied write (e.g. ``Memory/MEMORY.md`` instead of
``memory/MEMORY.md``) lands inside the same real guarded directory on
case-insensitive-but-case-preserving APFS while the string match misses it.
(Review: code-reviewer — case bypass in this guard, 2026-07-31.)

Byte measurement: copied from
``coordinator_core.write_guards.check_claude_md_size`` — simulate the FULL
post-edit content (Write -> content; Edit/MultiEdit -> read-then-replace)
and measure ``len(content.encode("utf-8"))`` on the simulated result, never
the diff. A simulation failure (unreadable existing file, decode error, any
exception while reconstructing content) ALLOWs, matching that guard's own
documented fail-open choice on this leg.

NO AUTO-TRIM, ever. The deny IS the curation act — a guard that silently
evicts to make room destroys the scarcity the cap exists to create and makes
a deletion nobody authorized. Per global design-as-offers doctrine, the deny
message leads with the better move: it states the current row count, says
plainly that admitting this memory requires evicting one, and states the
routing test the agent should apply to find the weakest candidate itself
(truth-expiring claims first, then rows restating doctrine already loaded
elsewhere, then the oldest-unreferenced row) — the guard has no semantic
access to a row's meaning, so it can name the test, not the verdict.

Shrink-toward-compliance carve-out: an edit that makes the target file
STRICTLY SMALLER (in bytes) than its current on-disk size is always
PERMITTED, regardless of which cap the resulting content still violates.
Without this, a file already over any cap (e.g. seeded before this guard
armed, or by another writer) would be permanently un-editable by any tool
this guard sees — trapping the very over-cap state the guard exists to walk
back. This is deliberately a single flat byte-based signal (not a
per-limit one) — the simplest rule that provably cannot trap the file, and
the one both the C12 spec's own worked example (the boot-payload plan's C8
survivor rewrite landing before this guard arms) and its own body ask for
by name ("a shrinking edit to an over-cap file is PERMITTED").

Hard-deny amendment (DR-345, amending DR-277): this guard was advisory from
C12 through DR-277's 2026-08-06 census. Advisory was read and ignored —
Claude-klabauter's own store reached 213 body files against the 20-file cap
this module now enforces, and a 26,718 B ``MEMORY.md`` against the 2,000 B
cap, 13x over, silently truncated at load. That is DR-277's own **fourth
structural rule** (a per-invocation gate whose harm scales with invocation
count is not weakened by an advisory, it is erased by one) plus **carve-out
1** (``MEMORY.md`` is doctrine loaded before review is possible — it
auto-loads into every session's boot context). The deny envelope
(``permissionDecision: "deny"`` / ``permissionDecisionReason``) is copied
verbatim in shape from ``block_home_dir_memo_delivery.py`` — no new envelope
shape invented.

Fifth limit, added with the hard-deny flip: ``MAX_MEMORY_FILES = 20`` body
files (excluding ``MEMORY.md`` itself) per project ``memory/`` dir, 1:1 with
``MAX_MEMORY_MD_ROWS`` so the index and the store cannot disagree about how
many memories exist. It fires ONLY on creation of a NEW body file (the
resolved target does not yet exist on disk) when the directory already
holds ``>= MAX_MEMORY_FILES`` such files — an edit/rewrite of an EXISTING
body file always passes this cap, even at or over the limit, or a full store
becomes permanently uneditable and the shrink-toward-compliance carve-out
below can no longer walk it back. The deny names the two routes the PM
ruled for: evict the oldest body file (by mtime — the cheapest correct
candidate), or if it's worth keeping, write it to ``state/lessons/`` first,
then delete it.

What memory is FOR, and why the deny says so (PM ruling 2026-08-21). The
agent reaching for memory is almost always obeying a durability instinct —
*"that sounded important to the human, it must not be forgotten."* Durability
is the one thing memory does not uniquely provide: ``state/lessons/`` is the
durable, findable store, swept and routed by other processes, and it is
AVAILABLE when someone reaches for it. What memory uniquely does is far
narrower and strictly more dangerous — it is FORCE-READ, unbidden, by every EM
at birth, and every one of them treats it as TRUE until somebody purges it.
A wrong or expired memory is therefore not inert; it is actively believed,
fleet-wide, for as long as it sits there.

So the routing test the deny states is deliberately NOT "is this important."
Everything an agent wants to save is important — that test admits everything,
which is how this store reached 213 entries. The test is "must every EM
believe this before it does anything at all," and almost nothing clears it.
*"The percolate button runs the code it is meant to replace, we should fix
that"* is a lesson: real, worth keeping, findable when reached for, and
catastrophic as a boot-time assertion once it is fixed. *"This repo's commits
refuse unless CLAUDE_KLABAUTER_ROOT is pinned"* is a memory: every EM hits it, before
anything else works.

Spec backlink: DoE-claude
  docs/plans/2026-07-30-boot-doctrine-cut-and-refill-gate.md § C12, AC14.
  claude-klabauter ``state/tasks/2026-08-21-memory-cap-hard-deny-and-count-cap.md``,
  ``docs/decisions/DR-345-memory-cap-is-hard-deny-with-a-file-count-cap.md``.
Sibling guards this module's idiom is copied from (DoE-claude repo, no
  ``.sh``/``.py`` reference hook of its own — C12 is a net-new guard, not a
  port):
  ``coordinator_core/write_guards/block_home_dir_memo_delivery.py`` (home
  resolution + containment, and now the deny-envelope shape too),
  ``coordinator_core/write_guards/check_claude_md_size.py`` (post-edit
  content simulation + byte measurement).

Negative-spec:
  - Does NOT extend ``guard_doctrine_surface_edits.py``'s protected-path
    list. Memory is not a doctrine surface and is not subject to the PM
    sentinel (explicit instruction, C12 body).
  - Does NOT auto-trim, auto-evict, or otherwise mutate the target on deny.
    The deny is the entire mechanism; a "helpful" silent rewrite would be an
    unauthorized deletion.
  - Does NOT attempt semantic classification of which row is weakest (no
    truth-expiry detector, no doctrine-overlap detector). The deny message
    states the routing TEST as instructions for the agent to apply, it does
    not compute a verdict the guard has no evidentiary basis for.
  - Does NOT govern anything below ``<slug>/memory/`` at more than one path
    segment of depth, and does NOT govern the ``memory/`` directory entry
    itself (mkdir is not a ``Write``/``Edit``/``MultiEdit`` concern).
  - Never raises: any unexpected input shape or internal error is treated as
    ALLOW (fail-open), matching every guard this idiom is copied from.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.write_guards._case_fold_path import (
    casefold_path,
    strip_extended_length_prefix,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 136  # hard-deny band; next free slot after bump_out_of_repo_tool_write (135)

#: AC14's four numbers, verbatim.
MAX_MEMORY_MD_BYTES = 2000
MAX_MEMORY_MD_ROWS = 20
MAX_ROW_CHARS = 100
MAX_BODY_FILE_BYTES = 1500

#: DR-345 — 1:1 with MAX_MEMORY_MD_ROWS so index and store cannot disagree
#: about how many memories exist. Fires only on NEW body-file creation.
MAX_MEMORY_FILES = 20

_CLAUDE_DIRNAME = ".claude"
_PROJECTS_DIRNAME = "projects"
_MEMORY_DIRNAME = "memory"
_MEMORY_MD_FILENAME = "MEMORY.md"

#: tool_input keys that can carry the target path.
_PATH_KEYS = ("file_path",)

_INTERCEPTED_TOOLS = {"Write", "Edit", "MultiEdit"}


def _guarded_project_roots() -> "List[Path]":
    """``<home>/.claude/projects`` roots to guard, union of USERPROFILE /
    HOME / Path.home(), plus CLAUDE_HOME treated as a direct ``.claude``
    root. Idiom copied verbatim in shape from
    ``block_home_dir_memo_delivery._guarded_roots`` -- only the trailing
    path segment differs (``projects``, not ``cross-repo``).

    Returns PLAIN (non-casefolded) roots -- unlike
    ``block_home_dir_memo_delivery._guarded_roots``, this guard's caller
    (``_memory_target``) needs the ORIGINAL casing to resolve real
    filesystem paths for content simulation (``_simulate``/
    ``os.path.getsize``); casefolding lives only in the containment
    DECISION inside ``_memory_target``, not baked into the roots
    themselves.
    """
    homes: "List[str]" = []
    for env_key in ("USERPROFILE", "HOME"):
        val = os.environ.get(env_key, "")
        if val and val.strip():
            homes.append(val.strip())
    try:
        homes.append(str(Path.home()))
    except Exception:
        pass

    roots: "List[Path]" = []
    for home in homes:
        try:
            roots.append(Path(home) / _CLAUDE_DIRNAME / _PROJECTS_DIRNAME)
        except Exception:
            continue

    claude_home = os.environ.get("CLAUDE_HOME", "")
    if claude_home and claude_home.strip():
        try:
            roots.append(Path(claude_home.strip()) / _PROJECTS_DIRNAME)
        except Exception:
            pass

    seen: "set[Path]" = set()
    out: "List[Path]" = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _memory_target(target_raw: str) -> "Optional[Tuple[Path, str, str]]":
    """If ``target_raw`` resolves under
    ``<home>/.claude/projects/<slug>/memory/<filename>`` for some project
    slug, return ``(resolved_path, filename, display)`` where ``display`` is
    the ``<slug>/memory/<filename>`` relative form (Review:
    coordinatorcode-reviewer-54284751 Finding 4 -- the guard's scope is a
    wildcard per-project directory, so a bare ``filename`` alone cannot
    disambiguate which project's store tripped the cap when multiple slugs
    each hold a same-named ``MEMORY.md``/body file). Otherwise ``None``.
    Only DIRECT children of a project's ``memory/`` dir are governed -- not
    the dir itself, not anything nested deeper.

    Containment is decided on RESOLVED-then-CASEFOLDED copies of the
    candidate and every guarded root -- ``resolve()`` runs first on the raw
    path, THEN ``casefold_path`` (closing both the case-insensitive-but-
    case-preserving APFS gap, ``os.path.normcase`` being a no-op on POSIX,
    and the Windows extended-length-prefix desync a resolve-before-casefold
    ordering closes -- see ``coordinator_core.write_guards._case_fold_path``
    module docstring for both). Casefolding a raw string BEFORE resolving it
    (the inverse order) was the guard's earlier defect: a candidate that
    reaches ``.resolve()`` picks up a form (e.g. Windows' extended-length
    prefix) that casefolding-then-resolving never captures, desyncing the
    two operands of the SAME comparison from one another
    (``state/handoffs/2026-08-03-windows-extended-length-prefix-
    desync.md``). Resolving both operands once, then casefolding the
    resolved strings, is the order used for the coarse containment gate.

    Everything AFTER the containment decision -- the relative-parts
    extraction and the returned ``filename`` -- uses the ORIGINAL
    (non-casefolded) resolved path, but with the extended-length prefix
    separately stripped via ``strip_extended_length_prefix`` (NOT
    casefolded) before the length-based suffix slice: the two operands of
    that slice (``resolved`` and ``root_resolved``) must come from the same
    prefix-stripped space, or a candidate whose ``.resolve()`` grows the
    prefix while the root's does not desyncs the slice by exactly the
    prefix's width (Review: code-reviewer -- coordinatorcode-reviewer-
    d278904f Finding 1, 2026-08-03). Case must NOT be folded on this leg: a
    case-sensitive filesystem (Linux) has no bypass to guard against here,
    and casefolding this leg would break the real file I/O the caller
    performs against ``resolved`` (``_simulate``/``os.path.getsize``) on
    such a filesystem. The dir-name (``memory``) and returned filename are
    matched/consumed case-insensitively downstream (see ``check()``'s
    ``.casefold()`` comparison against ``_MEMORY_MD_FILENAME``) so a
    case-varied write is still recognized as governed.
    """
    roots = _guarded_project_roots()
    if not roots:
        return None

    try:
        resolved = Path(target_raw).resolve()
    except OSError:
        return None
    resolved_cf = casefold_path(str(resolved))

    roots_resolved: "List[Tuple[Path, Path]]" = []
    for root in roots:
        try:
            roots_resolved.append((root, root.resolve()))
        except OSError:
            continue
    if not roots_resolved:
        return None

    candidate_cf = Path(resolved_cf)
    roots_cf = [Path(casefold_path(str(rr))) for (_r, rr) in roots_resolved]
    if contained_path(candidate_cf, roots_cf) is None:
        return None

    resolved_stripped = strip_extended_length_prefix(str(resolved))
    for _root, root_resolved in roots_resolved:
        root_cf = casefold_path(str(root_resolved))
        if resolved_cf != root_cf and not resolved_cf.startswith(root_cf.rstrip("/") + "/"):
            continue
        root_stripped = strip_extended_length_prefix(str(root_resolved))
        suffix = resolved_stripped[len(root_stripped):].strip("/\\")
        parts = suffix.replace("\\", "/").split("/") if suffix else []
        if len(parts) == 3 and parts[1].casefold() == _MEMORY_DIRNAME.casefold():
            display = f"{parts[0]}/{parts[1]}/{parts[2]}"
            return resolved, parts[2], display
    return None


def _simulate(tool_name: str, tool_input: Dict[str, Any], abs_file_path: str) -> Optional[str]:
    """Reconstruct the FULL post-edit file content. Mirrors
    ``check_claude_md_size._simulate`` byte-for-byte (same replace-once-vs-
    replace-all branch, same read-before-replace shape for Edit/MultiEdit).
    A not-yet-existing target under Edit/MultiEdit reads as empty content --
    this only matters for the shrink carve-out (old_size is separately
    computed as 0 for a not-yet-existing file), the simulate result itself
    is not reachable on a genuinely missing file in normal harness use
    (Edit/MultiEdit require an existing file), kept here only so a caller
    error surfaces as fail-open, not a raised exception.
    """
    if tool_name == "Write":
        return tool_input.get("content", "") or ""

    if tool_name == "Edit":
        try:
            with open(abs_file_path, "r", encoding="utf-8") as f:
                cur = f.read()
        except FileNotFoundError:
            cur = ""
        old_s = tool_input.get("old_string", "")
        new_s = tool_input.get("new_string", "")
        if tool_input.get("replace_all"):
            return cur.replace(old_s, new_s)
        return cur.replace(old_s, new_s, 1)

    if tool_name == "MultiEdit":
        try:
            with open(abs_file_path, "r", encoding="utf-8") as f:
                buf = f.read()
        except FileNotFoundError:
            buf = ""
        for edit in tool_input.get("edits", []) or []:
            old_s = edit.get("old_string", "")
            new_s = edit.get("new_string", "")
            if edit.get("replace_all"):
                buf = buf.replace(old_s, new_s)
            else:
                buf = buf.replace(old_s, new_s, 1)
        return buf

    return None


def _index_rows(content: str) -> "List[str]":
    return [line for line in content.splitlines() if line.startswith("- ")]


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


#: The eviction-priority order, condensed to fit the 220-byte prose cap --
#: full rationale (why this order, not a semantic verdict) is the module
#: docstring's job, not the deny message's.
_EVICT_ORDER = "stale fact, then doctrine dup, then oldest"

#: The memory-vs-lessons routing test, in the deny because that is the moment
#: the wrong choice is being made (PM ruling 2026-08-21). An agent reaching for
#: memory is usually acting on "this matters, it must not be forgotten" -- a
#: durability instinct. Durability is what LESSONS give. What memory gives is
#: something else, and strictly more dangerous: force-read, unbidden, by every
#: EM at birth, and believed TRUE until someone purges it. So the test is not
#: "is this important" (everything an EM wants to save is important) -- it is
#: "must every EM believe this before it does anything." Almost nothing clears
#: that bar. Kept to one clause per route so it survives the 220-byte cap.
_ROUTING = "memory is force-read as TRUE every boot; a lesson is found when reached for"


def _deny_reason_bytes(display: str, new_size: int, rows: "List[str]") -> str:
    return (
        f"[memory cap] {display}: {new_size}B > {MAX_MEMORY_MD_BYTES}B cap "
        f"({len(rows)} rows). No auto-trim -- evict a row yourself ({_EVICT_ORDER}), "
        "then retry."
    )


def _deny_reason_rows(display: str, rows: "List[str]") -> str:
    return (
        f"[memory cap] {display}: {len(rows)} rows > {MAX_MEMORY_MD_ROWS} cap. "
        f"{_ROUTING}. Not boot-critical -> state/lessons/. "
        "Else evict a row."
    )


def _deny_reason_row_length(display: str, overlong_rows: "List[str]") -> str:
    return (
        f"[memory cap] {display}: {len(overlong_rows)} row(s) over "
        f"{MAX_ROW_CHARS} chars. Shorten the flagged row(s), then retry."
    )


def _deny_reason_body(display: str, new_size: int) -> str:
    return (
        f"[memory cap] {display}: {new_size}B > {MAX_BODY_FILE_BYTES}B cap "
        "(body file). Trim it, then retry."
    )


def _check_memory_md(new_content: str, new_size: int, display: str) -> Optional[Dict[str, Any]]:
    rows = _index_rows(new_content)
    if new_size > MAX_MEMORY_MD_BYTES:
        return _deny(_deny_reason_bytes(display, new_size, rows))
    if len(rows) > MAX_MEMORY_MD_ROWS:
        return _deny(_deny_reason_rows(display, rows))
    overlong = [r for r in rows if len(r) > MAX_ROW_CHARS]
    if overlong:
        return _deny(_deny_reason_row_length(display, overlong))
    return None


def _check_body_file(new_size: int, display: str) -> Optional[Dict[str, Any]]:
    if new_size > MAX_BODY_FILE_BYTES:
        return _deny(_deny_reason_body(display, new_size))
    return None


def _body_file_count_and_oldest(mem_dir: Path) -> "Tuple[int, Optional[Path]]":
    """Body files (excluding MEMORY.md) directly in ``mem_dir``, plus the
    oldest by mtime -- the cheapest correct eviction candidate, per the PM
    ruling this cap implements. A listing failure (dir vanished, permission
    error) reads as zero, matching this module's fail-open posture -- it
    only ever widens what passes, never narrows it."""
    try:
        files = [
            p
            for p in mem_dir.iterdir()
            if p.is_file() and p.name.casefold() != _MEMORY_MD_FILENAME.casefold()
        ]
    except OSError:
        return 0, None
    if not files:
        return 0, None
    oldest = min(files, key=lambda p: p.stat().st_mtime)
    return len(files), oldest


def _deny_reason_file_count(dir_display: str, count: int, oldest_name: str) -> str:
    return (
        f"[memory cap] {dir_display}: {count}/{MAX_MEMORY_FILES} memories. "
        f"{_ROUTING}. Not boot-critical -> state/lessons/. "
        f"Else evict oldest: {oldest_name}."
    )


def _check_new_body_file_count(resolved: Path, display: str) -> Optional[Dict[str, Any]]:
    """DR-345's fifth limit. Fires ONLY when the write would CREATE a new
    body file -- an edit/rewrite of an EXISTING body file must always pass,
    even at or over the cap, or a full store becomes permanently
    uneditable and the shrink-toward-compliance carve-out can no longer
    walk it back."""
    if resolved.exists():
        return None
    count, oldest = _body_file_count_and_oldest(resolved.parent)
    if count < MAX_MEMORY_FILES:
        return None
    dir_display = display.rsplit("/", 1)[0] + "/"
    oldest_name = oldest.name if oldest is not None else "?"
    return _deny(_deny_reason_file_count(dir_display, count, oldest_name))


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in _INTERCEPTED_TOOLS:
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

        abs_file_path = os.path.abspath(target_raw)
        match = _memory_target(abs_file_path)
        if match is None:
            return None
        resolved, filename, display = match

        if filename.casefold() != _MEMORY_MD_FILENAME.casefold():
            count_deny = _check_new_body_file_count(resolved, display)
            if count_deny is not None:
                return count_deny

        try:
            new_content = _simulate(tool_name, tool_input, str(resolved))
        except Exception:
            return None
        if new_content is None:
            return None

        try:
            old_size = os.path.getsize(resolved) if resolved.exists() else 0
        except OSError:
            old_size = 0
        new_size = len(new_content.encode("utf-8"))

        # Shrink-toward-compliance carve-out: never deny an edit that makes
        # the file strictly smaller than it is on disk today, regardless of
        # which cap the (still possibly over-cap) result violates.
        if new_size < old_size:
            return None

        if filename.casefold() == _MEMORY_MD_FILENAME.casefold():
            return _check_memory_md(new_content, new_size, display)
        return _check_body_file(new_size, display)
    except Exception:
        # Fail-open on any unexpected error, matching every guard this
        # idiom is copied from.
        return None
