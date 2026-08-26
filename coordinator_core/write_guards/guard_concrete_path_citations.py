"""coordinator_core.write_guards.guard_concrete_path_citations — advisory
guard, tiered by target surface.

Mid-session leg of the concrete-path-citation defect
(`coordinator_core.ops.session.guard_concrete_path_citations`, see that
module's docstring for the full incident writeup and the four detection
rules). That module's `scan_repo` is the per-commit / audit-time leg over
the whole tracked corpus; this guard is the WRITE-TIME leg -- it inspects a
Write/Edit/MultiEdit/NotebookEdit's proposed content before it lands, so a
new concrete-path citation never reaches disk in the first place instead of
waiting to be caught at the next commit-time sweep.

Warn, never deny (PM ruling 2026-08-05, superseding the tiered-deny design)
----------------------------------------------------------------------------
This guard used to hard-deny on its high-stakes tier. It no longer denies
on ANY tier -- every firing is advisory `additionalContext` and the write
lands. Two things made the deny wrong, and both are structural rather than
a matter of taste:

  - A hardcoded path in a written body is a portability wart, not
    irreversible harm, and this repo's own north star reserves hard blocks
    for irreversible harm ("make the correct path cheaper rather than
    walling off the wrong one"). Same demotion, same reasoning, as
    `nudge_windows_subprocess_popup` under DR-077 part 2.
  - The deny was self-defeating on its own remedy. Its message names
    `fix-concrete-path-citations --apply`, a fixer that operates on a FILE
    -- but a denied write never reached disk, so at the moment of the deny
    there was no file to fix and the cheap correct path it pointed at was
    unreachable. The author's only exit was to re-emit the whole body by
    hand to change one path. Warning instead makes the offer real: the file
    exists, and `_fix_hint` names the exact one-line command against it.

The backstop is unchanged and is what makes warning safe: the commit-time
sweep (`coordinator_core.ops.session.guard_concrete_path_citations.
scan_repo`, consumed by DoE-claude's `test_no_absolute_path_literals.py`)
is still a hard failure across the whole tracked corpus. Nothing ships with
a concrete path because this guard stopped blocking; a citation waved
through here is caught before it lands in a commit.

New-violations-only, not a blanket warning
--------------------------------------------
The tracked corpus already carries a real number of pre-existing concrete-
path citations (the very debt this whole guard exists to work down). A
guard that re-flagged every one of them on any touch to an already-
offending file would be pure noise on nearly any edit to a governed
doctrine surface. Instead this guard fires only on
`new_violations(before, after)` (see
`guard_concrete_path_citations.new_violations`'s own docstring) -- a
multiset difference between the file's citations before and after THIS
write. A legacy citation sitting untouched anywhere else in the file
contributes equally to both sides of the diff and never surfaces here; only
a citation this write is the first to introduce (or duplicates an existing
one an additional time) does. Same detection design as
`coordinator/hooks/scripts/guard-prompt-surface-citations.py` (DoE-claude)
-- this guard reimplements that shape natively in claude-klabauter rather than
depending on a DoE-owned module, because (unlike that guard's SEED_WIKIS
data dependency) nothing here needs DoE-owned data at hook time.

Reconstructing before/after
-----------------------------
`before` is the current on-disk content (empty string for a not-yet-
existing file). `after` is reconstructed from `tool_input`:
  - Write: `tool_input["content"]` directly.
  - Edit: `before` with `old_string` replaced by `new_string` once (or every
    occurrence if `replace_all`), matching the Edit tool's own contract.
    `old_string == ""` is the tool's own "file did not exist / is empty"
    shape, so `after` is just `new_string` in that case.
  - MultiEdit: the same replacement applied sequentially over
    `tool_input["edits"]`.
  - NotebookEdit: no reliable `before` reconstruction is available for a
    single cell's prior source from a PreToolUse payload alone, so `before`
    is treated as empty and `after` is `new_source` directly -- every
    concrete-path finding in the proposed cell content is treated as new.
    This is conservative (a cell whose content is unchanged but re-supplied
    could in principle re-flag), not a missed-detection risk.
Any shape this guard cannot confidently reconstruct (missing/wrong-typed
field, `old_string` absent from `before`) fails OPEN -- a guard that cannot
compute its own input has no basis to speak.

Surface tiers -- LOUD vs QUIET
--------------------------------
Both tiers are advisory now; the tier decides only HOW OFTEN the advisory
repeats. `_LOUD_*`/`_QUIET_*` below are the tier table -- DATA, not a
buried conditional, so moving a directory between tiers is a one-line edit
to the table, not a control-flow change.

LOUD covers surfaces that reach an agent's always-loaded context or an OSS
install (percolating skill/agent/command/snippet/pipeline prompt surfaces,
global-doctrine, root CLAUDE.md and any *.local.md, the wiki, and
executable/config code that is actually read), plus `state/`, `archive/`,
and every unlisted in-repo surface. It fires on EVERY offending write, with
no session memoization -- the write lands either way, so the only cost of
repeating is a line of context, and silence after the first one would mean
the second citation ships unmentioned.

QUIET covers low-stakes scratch/ephemeral surfaces (scratch/, tmp/, tasks/,
and anything outside the repo entirely) and fires AT MOST ONCE PER SESSION:
a scratch tree full of citations would otherwise warn on every touch, which
is exactly the noise a low-stakes tier exists to avoid. The once-per-session
claim is a session-scoped sentinel file
(`coordinator-sessions/<session>/concrete-path-citation-warn.fired`) under
the repo's git common directory, mirroring
`coordinator_core.hooks.nudge_harness_directive_dispatch`'s established
fire-once pattern (atomic `open(..., "x")` claim).

NEGATIVE SPEC -- an out-of-repo target does NOT skip the cap. Out-of-repo
targets are the single largest slice of the QUIET tier (this tier's own
definition names them), so hanging the cap exclusively off a git common dir
made the cap unreachable for exactly the surface it was written for, and the
guard re-warned on every touch of a session scratch tree. When no git common
dir is resolvable the sentinel falls back to
`tempfile.gettempdir()/coordinator-sessions/<session>/`, keyed the same way.
Only a session with no id at all (`pid-<pid>` key) still degrades to "may
fire more than once", which remains strictly better than never firing.

Hint accuracy across tiers
----------------------------
The advisory's consequence clause is tier-specific, because the LOUD one is
FALSE on the QUIET tier: a path in a file that will never be committed --
session scratch, a tempdir target outside any repo -- cannot fail the
commit-time sweep, and a guard that asserts a consequence the reader can
verify is impossible teaches them to discount it. LOUD keeps the sweep
wording; QUIET names portability alone. Both tiers keep the full offer
(`machine-local get`, the fixer invocation, the `abs-path-ok:` marker) --
the guard leads with the alternative on every tier.

Fail-open guards (all exit None/no-op, in order): tool_input not a dict; no
target path; on-disk read failure for an existing file; ambiguous
before/after reconstruction; zero new violations; any internal exception.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.lifecycle import find_repo_root, git_common_dir
from coordinator_core.ops.session.guard_concrete_path_citations import (
    Finding,
    new_violations,
)
from coordinator_core.write_guards._sentinel_write_guard import extract_target_path

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
#: Top of the advisory/deny-offer band (110-180), not its old hard-deny slot
#: of 141. `engine.evaluate` lets only the FIRST advisory speak, so a guard
#: demoted out of the deny phase inherits a new failure mode: any advisory
#: with a lower PRIORITY can now swallow it entirely. 111 keeps this one
#: ahead of the frequently-firing structural nudges at 130-180 -- it fires
#: rarely (only on a NEW citation) and carries a correctness signal the
#: commit-time sweep will otherwise raise much later.
PRIORITY = 111

_FIX_COMMAND = (
    "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/"
    "fix-concrete-path-citations"
)

# ---------------------------------------------------------------------------
# Surface tiers -- LOUD (warn every write) vs QUIET (warn once per session).
# Neither denies. Amend THIS TABLE to move a directory between tiers; do not
# add a special case to check()/`_classify_tier`.
# ---------------------------------------------------------------------------

# LOUD: reaches an agent's always-loaded context (percolating prompt
# surfaces, global doctrine) or an OSS install, or is executable/config code
# actually read at runtime. A bad path here is load-bearing wrong, not just
# untidy -- worth saying every single time.
_LOUD_PATH_PREFIXES: Tuple[str, ...] = (
    "coordinator/skills/",
    "coordinator/agents/",
    "coordinator/commands/",
    "coordinator/snippets/",
    "coordinator/pipelines/",
    "coordinator/docs/wiki/",
    "global-doctrine/",
)
# Root CLAUDE.md specifically (an exact repo-relative match, not every
# nested CLAUDE.md) -- the always-on doctrine entrypoint.
_LOUD_EXACT_FILES = frozenset({"CLAUDE.md"})
# Any *.local.md, anywhere -- this fleet's convention for tracked
# collaboration doctrine that happens to use the `.local.md` naming
# (coordinator.local.md, CLAUDE.local.md), not personal/gitignored config.
_LOUD_BASENAME_SUFFIXES: Tuple[str, ...] = (".local.md",)
# Executable/config code that is actually read at runtime -- a repo-wide
# extension check (not path-scoped): a bad path in a `.py`/`.sh`/`.bats`
# script or a `.json` config is load-bearing wherever in the tree it sits.
_LOUD_EXTENSIONS = frozenset({".py", ".sh", ".bats", ".json"})

# QUIET: low-stakes scratch/ephemeral surfaces -- fires at most once per
# session (see module docstring). Also covers any target that resolves
# OUTSIDE the current repo entirely (no repo root resolvable, or the path
# escapes it) -- there's no tracked corpus to protect there either.
#
# `state/` and `archive/` are deliberately NOT here -- an earlier cut of
# this table lumped them in with genuine scratch, which was wrong: this
# fleet's own doctrine (DoE-claude CLAUDE.md's `state/ vs tasks/` section,
# and the sibling state-placement-law wiki) makes `state/` always-on
# load-bearing substrate -- `state/lessons/` and `state/handoffs/` are read
# and TRUSTED at session start, exactly the "an agent reads and trusts it as
# fact" hazard this guard exists to close. `archive/` is closed-out
# substrate, not scratch, either. Both fall through to the default LOUD
# tier below rather than being listed in `_LOUD_PATH_PREFIXES` explicitly
# -- an unlisted in-repo surface already defaults to "loud".
_QUIET_PATH_PREFIXES: Tuple[str, ...] = (
    "scratch/",
    "tmp/",
    "tasks/",
)

_SESSION_SENTINEL_NAME = "concrete-path-citation-warn.fired"


def _reconstruct_after(tool_name: str, tool_input: dict, before: str) -> "str | None":
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None

    if tool_name == "Edit":
        old_s = tool_input.get("old_string")
        new_s = tool_input.get("new_string")
        if not isinstance(old_s, str) or not isinstance(new_s, str):
            return None
        if old_s == "":
            return new_s
        if old_s not in before:
            return None
        return (
            before.replace(old_s, new_s)
            if tool_input.get("replace_all")
            else before.replace(old_s, new_s, 1)
        )

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        text = before
        for edit in edits:
            if not isinstance(edit, dict):
                return None
            old_s = edit.get("old_string")
            new_s = edit.get("new_string")
            if not isinstance(old_s, str) or not isinstance(new_s, str):
                return None
            if old_s == "":
                text = new_s
                continue
            if old_s not in text:
                return None
            text = (
                text.replace(old_s, new_s)
                if edit.get("replace_all")
                else text.replace(old_s, new_s, 1)
            )
        return text

    if tool_name == "NotebookEdit":
        source = tool_input.get("new_source")
        if not isinstance(source, str):
            source = tool_input.get("new_string")
        return source if isinstance(source, str) else None

    return None


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------


def _repo_relative(target: str) -> "tuple[Path | None, str | None]":
    """Return (repo_root, repo-relative-posix-path) for `target`, or
    (repo_root_or_None, None) when `target` cannot be placed inside a
    resolvable repo (no repo root, or the path escapes it -- both are the
    "outside the repo" WARN case)."""
    probe_dir = os.path.dirname(target) or "."
    # Walk up to the nearest existing ancestor -- a Write to a brand-new
    # nested directory means `probe_dir` itself doesn't exist yet, and
    # `git rev-parse`'s subprocess cwd must exist or the spawn itself raises
    # (not merely a non-repo RuntimeError).
    while probe_dir and not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            return None, None
        probe_dir = parent
    try:
        repo_root = find_repo_root(cwd=probe_dir)
    except (RuntimeError, OSError):
        return None, None
    try:
        rel = Path(target).resolve().relative_to(repo_root)
    except ValueError:
        return repo_root, None
    return repo_root, rel.as_posix()


def _classify_tier(rel_path: "str | None") -> str:
    """Return "loud" (warn on every offending write) or "quiet" (warn at
    most once per session) for a repo-relative POSIX path. `None` (target
    outside any resolvable repo) is always "quiet" -- there is no tracked
    corpus to protect there. An unlisted in-repo surface defaults to
    "loud"."""
    if rel_path is None:
        return "quiet"
    basename = rel_path.rsplit("/", 1)[-1]
    if rel_path in _LOUD_EXACT_FILES:
        return "loud"
    if any(basename.endswith(suf) for suf in _LOUD_BASENAME_SUFFIXES):
        return "loud"
    if any(rel_path.startswith(p) for p in _LOUD_PATH_PREFIXES):
        return "loud"
    # QUIET-tier directories are tested BEFORE the extension check, and the
    # order is the whole point. A `.py` under `scratch/` or `tasks/` is a
    # throwaway probe -- nothing imports it, ships it, or runs it but its
    # author -- so repeating an advisory about it every touch is pure noise,
    # which is the specific annoyance this tiering exists to remove. The
    # extension check still covers `.py`/`.sh`/`.bats`/`.json` everywhere
    # else in the tree, where they ARE load-bearing. Named-LOUD surfaces
    # above already won, so a percolating skill or a wiki page is unaffected
    # by this ordering.
    if any(rel_path.startswith(p) for p in _QUIET_PATH_PREFIXES):
        return "quiet"
    ext = "." + basename.rsplit(".", 1)[-1] if "." in basename else ""
    if ext in _LOUD_EXTENSIONS:
        return "loud"
    return "loud"


# ---------------------------------------------------------------------------
# Once-per-session WARN claim -- mirrors
# coordinator_core.hooks.nudge_harness_directive_dispatch's fire-once
# sentinel pattern (session-keyed file under the git common dir, atomic
# exclusive-create claim).
# ---------------------------------------------------------------------------


def _session_key(payload: dict) -> str:
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid.strip())
        if safe:
            return safe
    return f"pid-{os.getpid()}"


def _sentinel_base(repo_root: "Path | None") -> Path:
    """Return the directory the per-session claim tree hangs off.

    The git common dir when one is resolvable, else `tempfile.gettempdir()`
    -- the same per-user, process-independent fallback base used by the
    hook-side session sentinels (`coordinator_core.hooks` sentinel paths).
    An out-of-repo target has no git dir by construction and is the QUIET
    tier's most common shape, so it needs a base that exists rather than an
    early "always warn" exit (module docstring's negative spec)."""
    if repo_root is not None:
        try:
            return git_common_dir(repo_root)
        except (RuntimeError, OSError):
            pass
    import tempfile
    return Path(tempfile.gettempdir())


#: Mirrors the one entry of `liveness._NON_SESSION_DIR_NAMES` this module
#: needs. Deliberately NOT an import: this guard runs in a PreToolUse hook on
#: the write hot path, where pulling in the session package for one string is
#: cost the hook cannot justify -- the same reasoning
#: `bash_guards/_override_log_path.NO_SESSION_BUCKET` states for its own copy.
_NO_SESSION_BUCKET = "no-session"


def _claim_warn_once(repo_root: "Path | None", payload: dict) -> bool:
    """Atomically claim this session's one WARN slot; True iff this call
    won it (i.e. should actually emit the advisory). A session carrying no
    id at all is keyed per-process and so degrades to "always warn" --
    strictly better than never warning at all, matching the precedent this
    mirrors."""
    base = _sentinel_base(repo_root)
    # A warn-once sentinel is not a session and must never mint one. This used
    # to be `<hub>/<session_key>/` unconditionally, and when `repo_root`
    # resolves, that hub is the REAL one: `liveness.live_session_ids`
    # enumerates every non-denylisted child of it as a SESSION, so an advisory
    # sentinel manufactured a phantom, record-less session that claim
    # attribution and scope computation both read.
    # `session/core.py::ensure_session` is the one constructor.
    # Warn-once semantics are unchanged: the sentinel stays keyed on the
    # session, one level deeper, under the denylisted `no-session` bucket
    # (`liveness._NON_SESSION_DIR_NAMES`, the same fallback
    # `bash_guards/_override_log_path` uses) whenever the session's own
    # directory does not already exist.
    sessions_root = base / "coordinator-sessions"
    session_key = _session_key(payload)
    sentinel_dir = sessions_root / session_key
    if not sentinel_dir.is_dir():
        sentinel_dir = sessions_root / _NO_SESSION_BUCKET / session_key
    sentinel = sentinel_dir / _SESSION_SENTINEL_NAME
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        with open(sentinel, "x", encoding="utf-8", newline="\n") as fh:
            fh.write("1")
        return True
    except FileExistsError:
        return False
    except OSError:
        # A sentinel we cannot write means at worst a repeat warning next
        # touch -- never let it turn into a raised exception on a write path.
        return True


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _fix_hint(target: str, *, committable: bool) -> str:
    """The offer, with the just-written file's path already spliced into the
    fixer invocation.

    The path is not decoration. `fix-concrete-path-citations` acts on files,
    and naming the file turns the remedy into one runnable line instead of a
    tool the reader has to work out how to aim -- which is the whole reason
    this guard warns rather than denies (module docstring). The marker
    escape is named alongside it because a citation the fixer classifies
    MARKER or REPORT_ONLY (quoted incident evidence, a captured diff) has no
    mechanical rewrite at all, and without this line the reader is left
    thinking the fixer failed.

    NEGATIVE SPEC -- the commit-time-sweep consequence is claimed ONLY when
    `committable` (the LOUD tier: in-repo, sweepable surfaces). Asserting it
    for a scratch or out-of-repo target states something the reader can
    disprove in one step, and a guard caught being wrong about its own
    stakes is discounted on the citations that DO ship. The offer itself is
    identical on both tiers -- lead with the alternative regardless."""
    consequence = (
        "wrong on other hosts, and the commit-time sweep will fail on it."
        if committable
        else "wrong on other hosts if this file is ever kept or copied."
    )
    return (
        f"{consequence} Use instead: `machine-local get repos.<key>`, run "
        f"`{_FIX_COMMAND} --apply {target}`, or mark the line "
        "`abs-path-ok: <reason>`."
    )


def _first_citation(violations: List[Finding]) -> str:
    """Render the first matched citation as ``rule: matched`` -- names WHICH
    citation tripped the guard, not just which file, so the operator isn't
    forced to re-scan the whole diff (Review: coordinatorcode-reviewer-54284751
    Finding 1 -- this detail was dropped by the 220-byte prose compression
    while ``violations`` was still passed to both builders unread)."""
    if not violations:
        return ""
    v = violations[0]
    return f" [{v.rule}: {v.matched}]"


#: The target is deliberately NOT echoed as bare prose here, only inside the
#: backticked fixer invocation `_fix_hint` builds. An absolute path is easily
#: 80+ bytes and only exempt from `MESSAGE_PROSE_CAP_BYTES` inside a
#: cue-window backtick span, so echoing it twice spent a third of the cap
#: restating what the runnable command already says.
def _loud_reason(target: str, violations: List[Finding]) -> str:
    return (
        f"[concrete-path-citation guard] WARN: new hardcoded path"
        f"{_first_citation(violations)} -- {_fix_hint(target, committable=True)}"
    )


def _quiet_reason(target: str, violations: List[Finding]) -> str:
    return (
        f"[concrete-path-citation guard] WARN (scratch): new hardcoded path"
        f"{_first_citation(violations)} -- {_fix_hint(target, committable=False)}"
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None

        target = extract_target_path(tool_input)
        if not target:
            return None

        try:
            p = Path(target)
            before = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        except Exception:
            return None

        after = _reconstruct_after(tool_name, tool_input, before)
        if after is None:
            return None

        # Computed before `new_violations` (not after, as before this fix)
        # so the evidence-artifact exemption -- a reviewer/integrator
        # sidecar under `state/subagent-share/`, or a frozen diff under
        # `state/review-trail/diffs/` -- applies at detection time, not just
        # at tiering time. Without this, the guard denied a sidecar quoting
        # its own finding, which is the exact circularity the exemption
        # closes (see `guard_concrete_path_citations`'s module docstring).
        repo_root, rel_path = _repo_relative(target)

        new = new_violations(before, after, rel_path or "")
        if not new:
            return None

        tier = _classify_tier(rel_path)

        if tier == "loud":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _loud_reason(target, new),
                }
            }

        # tier == "quiet": same advisory, at most once per session.
        if not _claim_warn_once(repo_root, payload):
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": _quiet_reason(target, new),
            }
        }
    except Exception:
        return None
