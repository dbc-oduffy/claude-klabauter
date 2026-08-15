"""
coordinator_core.orientation.regenerate_cache — orientation-cache producer.

Purpose: Derives the
per-repo ``state/orientation_cache.md`` file read at every session start
(coordinator/CLAUDE.md § Session Orientation). This module is the PRODUCER; the
CONSUMER (session boot, a plain file read) is unaffected by this port.

Cold caller (ceremony-invoked only: /workday-start, /update-docs,
/workstream-complete, /handoff) — no hot-path spawn-tax concern (constraint 7
does not apply to this writer).

WHAT THIS CACHE IS FOR (doctrine, PM-ratified 2026-07-30 — read before adding a
section).

**This cache is a ROUTER, not an answer sheet.** Its job is to let an EM who has
just been handed an ask reach the right artifact without hand-deriving where
that artifact is. It is not here to pre-answer the ask, and it cannot: the ask
is not known when this file is written.

The admission test for any candidate fact is **route-vs-answer**, not
precomputability and not size:

  - A ROUTING fact says what exists and where — an index, a name, a path, a
    citation. It is admissible however dynamic it looks.
  - An ANSWERING fact resolves a question. It is inadmissible however static
    its subject looks, because the question is always ask-shaped and this file
    predates the question.

Corollaries, each one earned rather than asserted:

  - **Pointers outperform payload.** Cite the artifact; do not inline it. A
    reader who needs the contents can fetch them cheaply — but a reader who
    does not know the artifact exists cannot discover it at any price. Fetching
    is lazy; discovery is not.
  - **An aggregate that destroys the identities it summarises is not a router.**
    A count is the canonical failure: ``Spinoffs ready: 24`` occupies exactly
    the slot an index belongs in and carries none of its information. Prefer
    names, capped and ordered, with the remainder stated.
  - **Every addition must displace, not accrete.** Session boot is already
    oversubscribed and demonstrably under-read; a section that is skimmed and
    discarded costs the reader more than it costs to omit. Adding here without
    cutting is a regression even when the added fact is true and cheap.

Evidence, so a future editor knows this is a finding and not a preference:
three cold EM sessions were booted into this repo on 2026-07-30, each handed a
different unrelated ask, then asked to classify every command they ran against
their own transcript. Each was explicitly invited to conclude that boot
injection saves nothing. All three independently reached the statements above.
One ran ``ls state/handoffs/`` on the very directory the counters summarise and
reported that the filenames would have answered its ask before it ran a
command. Full evidence:
``state/improvement-queue/2026-07-30-orientation-targets-work-finding-but-ems-d7e494b501a2.yaml``;
the surfaces outside this cache are spun off at
``state/handoffs/2026-07-30-boot-context-bloat-non-orientation-surfaces.md``.

Negative-spec (general): if a fact only makes sense once you know the ask, it
does not belong in this file. The working-tree-state ban stated at ``## Recent
commits`` below is the specific, already-ratified instance of this general rule
— it was originally justified on staleness grounds (a cached "the tree is
dirty" claim rots within one tool call), and it is independently correct here
because "what is dirty" is only meaningful relative to what you were about to
touch, which is ask-shaped by construction.

PURPOSE-MAP REWRITE (C6, 2026-07-30, superseding the paragraph this replaces):
this cache no longer carries a ``## Project`` identity line (it only ever
re-quoted CLAUDE.md's own first line — redundant with what every agent has
already read), a ``## Counters`` census, or a ``## Priorities`` handoff-derived
view. All three were ANSWERING facts (a count, a re-quote) or handoff/spinoff
state a skill already recomputes at the moment it matters (plan/baton
collision at planning; the memo blitz at ``/workday-start``) — see the
route-vs-answer test above. In their place this module emits ROUTING facts:
``## Wiki`` (repo wiki, if one exists at the conventional path), ``##
Architecture atlas`` (per-system pages, enumerated the same way
``coordinator_core.ops.check_atlas_watch_drift`` does — see
``emit_architecture_atlas``), ``## Capabilities`` (repo-declared capability
pointers, via ``resolve_capability_pointers`` reading ``coordinator.local.md``'s
``capability_pointers:`` key — the same declare-once-in-config seam
``fast_test_cmd:`` established; see ``emit_capability_pointers``), ``## Fast
test`` (the resolved fast-test invocation, via ``cs_resolve_fast_test_cmd`` —
the same resolver ``/validate`` uses, not reimplemented), and ``## Audits &
censuses`` (a directory pointer naming which investigation-record buckets
exist under ``state/``, answering "has someone already looked at this?" —
never a bare count, and never a sample of what was in them most recently).

``## Capabilities`` admission test (2026-08-14, per
cross-repo/inbox/2026-08-14-doe-claude-em-orientation-cache-capability-pointers.md):
a pointer earns its place in THIS section when a newborn agent would not
otherwise know the thing exists — discoverability, not usefulness. This is a
narrower test than route-vs-answer above: plenty of routing-shaped facts fail
it because the agent trips over them anyway (a tracked file, a directory
listing); the concrete first case is example-retrieval-repo query tools in a repo where
Example-retrieval-repo is available but nothing else in a cold session mentions it.

Recency-sample ruling (2026-08-14, per
cross-repo/inbox/2026-08-14-doe-claude-em-cache-recency-samples-are-not-pointers.md):
generalizing beyond ``## Capabilities``, this governs any section in this
module, present or future. The discriminator: does an enumeration tell the
reader WHAT EXISTS (a pointer set — bounded by the shape of the space,
changes only when that shape changes) or merely WHAT WAS TOUCHED LAST (a
recency sample — a moment's snapshot that implies a significance the sample
does not carry, and rots between boots)? The former earns a place here; the
latter does not, regardless of how useful it looks in isolation.

Behavior contract (byte-for-byte with the bash oracle for the sections both
still emit, except the one named parity-plus below):
  - Same tier resolution (ceremony vs mid-session), same section-omission-when-
    empty rules, same *.uproject detection (including the intentional git-vs-find
    exclusion-list DIVERGENCE between the two branches of detect_uproject — the
    bash oracle's ELSE branch's ``find`` has no node_modules/.git exclusions;
    this is preserved, not "fixed", per parity discipline).
  - PARITY-PLUS (explicitly authorized upgrade, not a silent deviation): the
    bash oracle writes via a direct ``printf > "$CACHE_FILE"`` overwrite (NOT
    atomic). This port writes via mkstemp-in-same-dir + os.replace (atomic
    rename) instead. Output CONTENT is identical either way — this upgrade is
    not observable except under a torn-write race, which it eliminates.

state-root resolution (``_state_root``): a MINIMAL, LOCAL reimplementation of
``coordinator-state-root.sh``'s Rule 5 (no ``--central`` flag) — meta-repo
(``~/.claude``) routes to claude-klabauter's ``state/``; any other (sibling) repo uses its
own ``<repo_root>/state``. This duplicates the same small pattern already
inlined in ``coordinator_core.ops.queue_append._claude_klabauter_root`` /
``_claude_home`` / ``_same_path`` (that module carries its own
"de-dup into a shared module" TODO — see queue_append.py:547) rather than
importing those private names cross-module. A canonical
``coordinator_core.state_root.coordinator_state_root()`` seam does not exist
yet on disk as of this port (grepped at build time) — if/when one lands, this
function should be replaced with a call to it rather than kept as a third
duplicate.

Housekeeping surfacing (C17b, 2026-07-23): every FULL regen (`build_cache`, i.e. every tier
except the ``--pinboard-only`` fast path added by C18) reads the shared
``state/housekeeping-failures.log`` (`coordinator_core.ops.ceremony.detached_spawn`,
non-destructive peek) and the housekeeping-liveness stamps
(`coordinator_core.ops.ceremony.housekeeping_liveness`), and — if either has anything to
report — renders a ``## Housekeeping`` section, inserted immediately before the (always-last)
``## Pinboard`` section so `patch_pinboard_only`'s "Pinboard is the LAST section" invariant is
undisturbed. This is the retargeted AC5 surfacing occasion: session boot's own SessionStart
hook stdout is NOT context-injected (DoE hooks.json), but the orientation-cache FILE this
module writes IS read and injected at every session start by the retained ``async: false``
injector (hooks.json:13) — so a failure recorded during session N's ceremony work reaches the
EM at the START of session N+1, satisfying "within one session boundary." The failures log is
cleared (`clear_failures_log`) by the WRITE call site immediately after a successful
non-``--check`` write that has already embedded its content — never inside `build_cache` itself,
so ``--check`` stays a true dry run with no side effects. There are THREE such write call sites,
all honouring the identical contract: the CLI trampoline and the RPC handler below (both call
`build_cache` + `write_cache` then `clear_failures_log` as a separate step), and
`patch_pinboard_only` itself (fixed 2026-07-28 — see that function's own docstring; it is a
third write path that re-derives and embeds Housekeeping content and had silently never called
`clear_failures_log`, so every record it delivered repeated at every subsequent regen forever —
cross-repo/inbox/2026-07-28-example-retrieval-repo-em-orientation-cache-housekeeping-flood.md Defect 1).
The ``--pinboard-only`` fast path (C18) DOES re-derive this ONE section (fixed 2026-07-23, see
negative-spec below) — every other section still stays byte-identical, zero re-derive, exactly
as C18 shipped it. Housekeeping is the sole named exception because DoE adopted
``--pinboard-only`` at BOTH mid-session call sites (`/workstream-complete`, `/handoff`) — the two
hottest mid-session cache-touch occasions — which meant a failure recorded between two FULL
regens (potentially a day, `/workday-start` to `/workday-start`) would not reach the EM until
long past AC5's "within one session boundary" promise. Re-deriving housekeeping is a peek of two
small files (`read_failures_log` + `check_stale_detailed`), not the full-regen section sweep
(no git spawn, no ``*.uproject`` find, no ``tasks/`` glob) — so C18's fast-path *purpose*
(avoid the expensive re-derives) survives untouched.

Negative-spec: ``--pinboard-only`` still does NOT re-derive any OTHER section —
Workstreams/Rechecks/Branch/Recent-commits/Auto-push-health/Wiki/Architecture
atlas/Capabilities/Fast test/Audits & censuses stay exactly what they were
before the patch: byte-identical, zero re-derive. Only Housekeeping and
Pinboard may change under ``--pinboard-only``. It DOES now clear the failures log itself,
on a real (non-``--check``) write that actually embedded non-empty
housekeeping content — see the "THREE such write call sites" paragraph
above; this negative-spec previously (incorrectly) exempted it, which was
Defect 1, not an intended design line.

Source: `cross-repo/inbox/2026-07-23-claude-central-em-wsc-tail-preflight-doe-reply.md`
§ "New — an AC5 hole your own ask 4 opens".

Registered as ``orientation.regenerate_cache`` (JSON-RPC op, MUTATING — writes
the per-repo orientation cache). NOT yet wired into ipc.py::_OP_KEY_SCOPE /
ops/__init__.py / ops/_registry_map.py / authz/classification.py (central-registry
files deferred to the EM per the build-wave's shared-tree concurrency-safety
rule) — the CLI trampoline (``coordinator/bin/regenerate-orientation-cache``)
calls ``build_cache``/``write_cache`` via a DIRECT in-process import instead of
``cc_invoke.route()`` RPC dispatch, matching the recipe's explicit disposition
("same shape as normalize-snippet") and sidestepping the not-yet-wired op-key-scope
dependency. The op registration exists for future daemon-RPC callers, not as
the CLI's own invocation path.

Spec backlink: scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 2
Port of: regenerate-orientation-cache.sh (DoE 60489ea9, 2026-07-16)
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.repo_root import is_inside_work_tree, show_toplevel
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.detached_spawn import clear_failures_log, read_failures_log
from coordinator_core.win_portability import same_path
from coordinator_core.ops.ceremony.housekeeping_liveness import (
    ARCHIVE_SWEEPS,
    REMEDY_COMMANDS,
    check_stale_detailed,
)
from coordinator_core.resolve_validation_cmd import (
    _extract_frontmatter as _cs_extract_frontmatter,
    _strip_wrapping_quotes as _cs_strip_wrapping_quotes,
    cs_resolve_fast_test_cmd,
)

# Generator-provenance declaration (C2, generator_provenance.py's AST reader
# — see that module for the discovery/coverage mechanism this feeds). `sources`
# names this module's own path: unlike the bin/ CLI trampolines that delegate
# derivation elsewhere, this module IS the whole derive-and-render
# implementation (build_cache/_render_cache/write_cache all live here), so
# its own movement is what actually changes state/orientation_cache.md's
# content -- there is no deeper locus to point at.
GENERATES = [
    {
        "artifact": "state/orientation_cache.md",
        "stamp_key": "generated_at",
        "sources": ["coordinator_core/orientation/regenerate_cache.py"],
    },
]

# ---------------------------------------------------------------------------
# state-root resolution (local minimal Rule-5 port — see module docstring)
# ---------------------------------------------------------------------------

_CLAUDE_HOME_ENV = "CLAUDE_HOME"
_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


def _machine_local_impl() -> str:
    """Return the path to _machine_local.py (best-effort; None-safe callers),
    settings-home first.

    Settings-home-first per DR-210 Amendment 2026-07-24 ("coordinator resolves
    nothing through ``~/.claude/bin``); the retired compat mirror stays as a
    last-resort rung only. Negative-spec: does NOT stop consulting the mirror —
    a machine whose settings-home copy is absent must still resolve.
    """
    override = os.environ.get("MACHINE_LOCAL_IMPL")
    if override:
        return override
    settings_home_impl = os.path.join(str(settings_home()), "bin", "_machine_local.py")
    if os.path.exists(settings_home_impl):
        return settings_home_impl
    return os.path.join(_claude_home(), "bin", "_machine_local.py")


def _machine_local_get(key: str) -> Optional[str]:
    """Call ``machine-local get <key>`` and return the value, or None on failure."""
    impl = _machine_local_impl()
    if not os.path.exists(impl):
        return None
    try:
        import sys

        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _claude_klabauter_root() -> Optional[str]:
    """Resolve the claude-klabauter repo root: CLAUDE_KLABAUTER_ROOT env, else machine-local registry."""
    override = os.environ.get(_CLAUDE_KLABAUTER_ROOT_ENV, "").strip()
    if override:
        return override
    val = _machine_local_get("repos.claude_klabauter")
    return val if val else None


def _same_path(a: str, b: str) -> bool:
    """Thin alias onto ``coordinator_core.win_portability.same_path`` -- the
    consolidated primitive (state/sizings/2026-08-07-path-equality-
    consolidates-onto-one-prim.yaml). Promoted from realpath-only to
    samefile-then-fallback semantics: broader (junction-aware) equality is
    correct here since this call site only checks "is repo_root the meta-repo
    home", where a junction-aliased home must compare equal."""
    return same_path(a, b)


def _state_root(repo_root: Path) -> Path:
    """Resolve the per-repo state root (coordinator-state-root.sh Rule 5, no --central).

    meta-repo (~/.claude) -> claude-klabauter/state; sibling repo -> repo_root/state.
    Fails loud (RuntimeError) when repo_root is the meta-repo and CLAUDE_KLABAUTER_ROOT is
    unresolvable — mirrors the bash seam's ``|| return 1`` fail-loud posture.
    """
    if _same_path(str(repo_root), _claude_home()):
        claude_klabauter_root = _claude_klabauter_root()
        if claude_klabauter_root is None:
            raise RuntimeError(
                "coordinator_state_root: repo_root is the meta-repo but CLAUDE_KLABAUTER_ROOT is "
                "unresolvable (no CLAUDE_KLABAUTER_ROOT env, no repos.claude_klabauter machine-local entry)"
            )
        return Path(claude_klabauter_root) / "state"
    return repo_root / "state"


# ---------------------------------------------------------------------------
# git helpers (one-shot cold calls — no spawn-tax concern, this whole script
# IS the cold path per the recipe)
# ---------------------------------------------------------------------------


def _git(args: List[str], repo_root: Path) -> Optional[str]:
    """Run ``git -C repo_root <args>`` and return stripped stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def resolve_repo_root(cwd: Optional[Path] = None) -> Path:
    """``git rev-parse --show-toplevel`` from *cwd*, falling back to *cwd* itself."""
    cwd = cwd or Path.cwd()
    out = show_toplevel(str(cwd))
    if out:
        return Path(out)
    return cwd


# ---------------------------------------------------------------------------
# *.uproject detection (bash:68-81) — preserves the intentional git-vs-find
# exclusion-list divergence between the two branches.
# ---------------------------------------------------------------------------


def detect_uproject(repo_root: Path) -> str:
    inside_repo = is_inside_work_tree(str(repo_root))
    if inside_repo:
        out = _git(["ls-files", "*.uproject"], repo_root) or ""
        first = out.splitlines()[0] if out else ""
        if first:
            return first
        # find fallback WITH exclusions (bash:74-76)
        found = _find_uproject(repo_root, exclude_node_modules_and_git=True)
        if found:
            return _strip_repo_prefix(found, repo_root)
        return ""
    # else-branch find fallback WITHOUT exclusions (bash:79-80 — intentional divergence)
    found = _find_uproject(repo_root, exclude_node_modules_and_git=False)
    if found:
        return _strip_repo_prefix(found, repo_root)
    return ""


def _find_uproject(repo_root: Path, exclude_node_modules_and_git: bool) -> str:
    args = ["find", str(repo_root), "-maxdepth", "6", "-name", "*.uproject"]
    if exclude_node_modules_and_git:
        args += ["-not", "-path", "*/node_modules/*", "-not", "-path", "*/.git/*"]
    try:
        from coordinator_core.win_portability import no_console_creationflags

        result = subprocess.run(
            args, capture_output=True, text=True, check=False,
            **no_console_creationflags(),
        )
    except OSError:
        return ""
    out = result.stdout.strip()
    if not out:
        return ""
    return out.splitlines()[0]


def _strip_repo_prefix(path: str, repo_root: Path) -> str:
    prefix = str(repo_root).rstrip("/") + "/"
    if path.startswith(prefix):
        return path[len(prefix):]
    return path


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Purpose-map probes (C6, 2026-07-30) -- computed ROUTING pointers to what
# exists in this repo and where, replacing the census/answer-shaped sections
# (## Project, ## Counters, ## Priorities) this module used to emit. See the
# module docstring's "PURPOSE-MAP REWRITE" paragraph for the admission test
# and the negative-spec (no rules, no counts) each probe below must honour.
# Modeled on `emit_rechecks`' glob-and-render shape, already used three times
# in this module before this section existed.
# ---------------------------------------------------------------------------

ATLAS_INDEX_MAX = 10
"""Cap on the number of architecture-atlas page names named in one ``##
Architecture atlas`` bullet -- an ellipsis, never a number, marks a longer
list (see the section's own negative-spec: no counts). The section is
elastic (`_CACHE_ELASTIC_SECTIONS`), so a repo with many more pages is
trimmed by `_enforce_cache_budget`, not by this cap alone.

Survives the recency-sample test (2026-08-14, per
cross-repo/inbox/2026-08-14-doe-claude-em-cache-recency-samples-are-not-pointers.md)
that the former ``## Audits & censuses`` filename tail failed: this
enumeration is a stable taxonomy, bounded by the subsystems that exist
rather than by when a page was last touched. It changes only when the
architecture does, and reading it teaches a newborn agent what subsystems
this repo has -- what exists, not what was touched last."""


def emit_wiki_pointer(repo_root: Path) -> List[str]:
    """Point at the repo wiki, if one exists at the conventional
    ``docs/wiki/`` path -- existence and location only, never its contents."""
    wiki_dir = repo_root / "docs" / "wiki"
    if not wiki_dir.is_dir():
        return []
    guide = wiki_dir / "DIRECTORY_GUIDE.md"
    if guide.is_file():
        return ["- `docs/wiki/DIRECTORY_GUIDE.md` — doctrine/reference index; start there."]
    return ["- `docs/wiki/` — doctrine/reference material; browse before assuming absence."]


def emit_architecture_atlas(repo_root: Path) -> List[str]:
    """Point at the per-system architecture atlas, enumerated the same way
    `coordinator_core.ops.check_atlas_watch_drift` does (same
    ``docs/architecture/systems/*.md`` path + glob) -- reused rather than
    reinvented, per this chunk's own instruction. Unlike that module, this
    probe never runs a ``.watch.py`` freshness check: a purpose map is a
    pointer, not an answer, and a per-page subprocess spawn would buy nothing
    a session boot needs."""
    systems_dir = repo_root / "docs" / "architecture" / "systems"
    if not systems_dir.is_dir():
        return []
    pages = sorted(p.stem for p in systems_dir.glob("*.md"))
    if not pages:
        return []
    shown = pages[:ATLAS_INDEX_MAX]
    tail = "…" if len(pages) > ATLAS_INDEX_MAX else ""
    return [
        "- `docs/architecture/systems/` — per-subsystem architecture pages: "
        + ", ".join(shown)
        + tail
    ]


CAPABILITY_POINTERS_MAX = 8
"""Cap on the number of ``## Capabilities`` bullets rendered -- same
ellipsis-not-count discipline as ATLAS_INDEX_MAX. The section is elastic
(`_CACHE_ELASTIC_SECTIONS`), so a repo declaring more entries than this is
trimmed by `_enforce_cache_budget` on top of this cap, not by this cap
alone."""

_CAPABILITY_POINTERS_HEADER_RE = re.compile(r"^capability_pointers:\s*$")
_CAPABILITY_POINTERS_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")


def _extract_capability_pointers(frontmatter: str) -> List[str]:
    """Extract the ``capability_pointers:`` YAML block-list from a
    ``coordinator.local.md`` frontmatter block.

    A small block-list reader for ONE key, not a YAML parser -- mirrors
    resolve_validation_cmd.py's own flat-scalar ``_extract_fast_test_cmd``
    (the sibling precedent this generalizes from a single scalar to a list).
    Each ``  - "..."`` item is unquoted via `_cs_strip_wrapping_quotes`
    (reused from resolve_validation_cmd.py rather than reimplemented -- same
    declare-once-in-config parsing discipline as `cs_resolve_fast_test_cmd`).
    Stops at the first line, once inside the block, that is neither blank nor
    a list item (the next frontmatter key). Returns [] when the key is absent.
    """
    lines = frontmatter.split("\n")
    out: List[str] = []
    in_block = False
    for line in lines:
        if _CAPABILITY_POINTERS_HEADER_RE.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        if not line.strip():
            continue
        m = _CAPABILITY_POINTERS_ITEM_RE.match(line)
        if not m:
            break
        item = _cs_strip_wrapping_quotes(m.group(1).strip())
        if item:
            out.append(item)
    return out


def resolve_capability_pointers(repo_root: str) -> List[str]:
    """Resolve the ``capability_pointers:`` list from *repo_root*'s
    ``coordinator.local.md`` frontmatter -- the SAME repo-local declaration
    seam `cs_resolve_fast_test_cmd` reads (``fast_test_cmd:``), applied to a
    second flat-top-level key. Deliberately no env-var escape hatch (unlike
    `cs_resolve_fast_test_cmd`'s COORDINATOR_FAST_TEST_CMD): a discoverability
    pointer is repo-local by construction, not a per-invocation override.
    Returns [] when the file or key is absent/empty -- never raises.
    """
    local_md = Path(repo_root) / "coordinator.local.md"
    if not local_md.is_file():
        return []
    fm = _cs_extract_frontmatter(local_md)
    return _extract_capability_pointers(fm)


def emit_capability_pointers(repo_root: Path) -> List[str]:
    """Point at repo-declared capabilities a newborn agent would not
    otherwise discover, fed from `resolve_capability_pointers` -- the same
    declare-once-in-config -> render-a-thin-pointer mechanism `emit_fast_test`
    already established via `cs_resolve_fast_test_cmd`, applied to a second
    repo-local key rather than a new mechanism.

    Admission test (memo-stated, discoverability not usefulness -- see module
    docstring's ``## Capabilities`` paragraph): a pointer earns its place here
    only when a newborn agent would not otherwise know the thing exists. The
    concrete first case is example-retrieval-repo's query surface in a repo where it is
    available but nothing else in a cold session boot mentions it.

    Elastic, not protected (`_CACHE_ELASTIC_SECTIONS`): a pointer trimmed by
    the byte budget costs an agent one lookup, not a wrong belief, so
    trimmability is correct rather than a defect to engineer around -- see
    cross-repo/inbox/2026-08-14-doe-claude-em-orientation-cache-capability-pointers.md.
    Omitted (not rendered as "none configured") when the key is absent/empty,
    matching every other omit-when-empty section in this module.
    """
    pointers = resolve_capability_pointers(str(repo_root))
    if not pointers:
        return []
    shown = pointers[:CAPABILITY_POINTERS_MAX]
    out = [f"- {p}" for p in shown]
    if len(pointers) > CAPABILITY_POINTERS_MAX:
        out.append("- …")
    return out


def emit_fast_test(repo_root: Path) -> List[str]:
    """Point at the resolved fast-test invocation via `cs_resolve_fast_test_cmd`
    -- the exact resolver ``/validate`` already uses -- rather than
    re-deriving ``coordinator.local.md``'s ``fast_test_cmd:`` key a second
    time. Omitted (not rendered as "unconfigured") when nothing resolves,
    matching every other omit-when-empty section in this module."""
    resolved = cs_resolve_fast_test_cmd(str(repo_root), _quiet=True)
    if resolved.exit_code != 0 or not resolved.cmd:
        return []
    return [f"- fast test: `{resolved.cmd}`"]


_AUDITS_SUBDIRS = ("audits", "censuses")


def emit_audits_index(state_root: Path) -> List[str]:
    """Point at existing investigation records under ``state/audits/`` and
    ``state/censuses/`` (whichever exist) -- answering "has someone already
    looked at this?" without pre-answering it.

    Directory pointer only, never a filename tail (2026-08-14 ruling, per
    cross-repo/inbox/2026-08-14-doe-claude-em-cache-recency-samples-are-not-
    pointers.md): a "recent: <names>" enumeration failed this section's own
    admission test. It samples a directory at one moment rather than naming
    what exists, implies a significance the sample does not carry, goes
    stale between every boot, and costs bytes in an elastic section
    (`_CACHE_ELASTIC_SECTIONS`) that is trimmed first when the cache is over
    budget. The directory pointer alone -- the fact the corpus exists at
    this path -- is the whole value; the bucket is omitted entirely (not
    rendered as "none yet") when it does not exist or is empty, since THAT
    existence signal is what a newborn agent needs.

    Negative-spec: do not reintroduce a filename enumeration here, "recent"
    or otherwise -- see the ruling above."""
    out: List[str] = []
    for sub in _AUDITS_SUBDIRS:
        d = state_root / sub
        if not d.is_dir():
            continue
        if not any(d.glob("*.md")):
            continue
        out.append(f"- `state/{sub}/` — existing investigation records")
    return out


# ---------------------------------------------------------------------------
# Active workstreams (bash:166-196) — awk transliteration, rule order preserved
# ---------------------------------------------------------------------------

_ACTIVE_WS_HEADER_RE = re.compile(r"^## active +workstreams", re.IGNORECASE)
_H3_ENTRY_RE = re.compile(r"^### (\d+)\. ")
_PLAIN_ENTRY_RE = re.compile(r"^(\d+)\. ")
_PAREN_TAIL_RE = re.compile(r" *\([^)]*\) *$")

WORKSTREAM_MAX = 10
"""Hard cap on the number of ``## Active workstreams`` entries this module
ever emits — enforced below, in `emit_workstreams`'s own ``count <
WORKSTREAM_MAX`` loop guard. Single source of truth shared with
`verify_orientation_cache_sync`, which imports this value instead of
redeclaring its own — the two were independently declared (coincidentally
both ``10``) before the 2026-07-28 bounds reconciliation that also unified
`CACHE_BUDGET_BYTES` / `LINE_CEILING` below; the coincidence is exactly the
kind of drift risk that reconciliation closes.
"""

WORKSTREAM_BODY_CAP = 84
"""Advisory bound on one ``## Active workstreams`` entry's rendered line
length. Checked ONLY by `verify_orientation_cache_sync`, post-hoc — this
module does NOT truncate tracker-derived workstream titles to this length at
write time (see `CACHE_BUDGET_BYTES`' docstring: this is the one documented
mechanism by which the byte budget can pass while the line ceiling fails,
"nothing on the write path caps a single line's length"). Declared here
rather than in the verifier so the NUMBER cannot drift between the two
modules even though write-time truncation to it remains open work, not
landed by this reconciliation.
"""


def emit_workstreams(repo_root: Path) -> List[str]:
    tracker: Optional[Path] = None
    for cand in ("tasks/project-tracker.md", "tasks/tracker.md"):
        p = repo_root / cand
        if p.is_file():
            tracker = p
            break
    if tracker is None:
        return []

    lines = _read_text_or_empty(tracker).splitlines()
    in_section = False
    saw_h3 = False
    count = 0
    out: List[str] = []

    for line in lines:
        if line.startswith("## "):
            if _ACTIVE_WS_HEADER_RE.match(line):
                in_section = True
                continue
            elif in_section:
                break  # awk `exit` — stop processing the whole file
            # else: fall through (mirrors awk fallthrough to the in_section rules,
            # which are no-ops here since in_section is False)

        if in_section and count < WORKSTREAM_MAX and _H3_ENTRY_RE.match(line):
            text = re.sub(r"^### ", "", line)
            text = _PAREN_TAIL_RE.sub("", text)
            out.append(text)
            count += 1
            saw_h3 = True
            continue

        if in_section and not saw_h3 and count < WORKSTREAM_MAX and _PLAIN_ENTRY_RE.match(line):
            text = _PAREN_TAIL_RE.sub("", line)
            out.append(text)
            count += 1

    return out


# ---------------------------------------------------------------------------
# Rechecks due <=7 days (bash:198-221)
# ---------------------------------------------------------------------------

_RECHECK_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def emit_rechecks(repo_root: Path) -> List[str]:
    tasks_dir = repo_root / "tasks"
    if not tasks_dir.is_dir():
        return []
    today_epoch = int(datetime.now(timezone.utc).timestamp())
    threshold_epoch = today_epoch + 7 * 86400
    out: List[str] = []
    for f in sorted(tasks_dir.glob("*-recheck-due-*.md")):
        if not f.is_file():
            continue
        base = f.name
        m = _RECHECK_DATE_RE.search(base)
        if not m:
            continue
        date_str = m.group(0)
        try:
            date_epoch = int(
                datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).timestamp()
            )
        except ValueError:
            continue  # malformed date in filename — not a real recheck marker, skip
        if date_epoch <= threshold_epoch:
            out.append(f"- `tasks/{base}` (due {date_str})")
    return out


# ---------------------------------------------------------------------------
# Branch line (bash:223-234) -- same spawn-amortization buy as ``## Recent
# commits`` below, and it survives on that ground alone: this is git state an
# agent would otherwise shell out for at boot. Read the rationale block below
# before applying the thin-pointer test to this section.
# ---------------------------------------------------------------------------


def emit_branch_line(repo_root: Path) -> str:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root) or "unknown"
    if _git(["rev-parse", "--verify", "origin/main"], repo_root) is not None:
        ahead = _git(["rev-list", "--count", "origin/main..HEAD"], repo_root) or "0"
        behind = _git(["rev-list", "--count", "HEAD..origin/main"], repo_root) or "0"
        return f"`{branch}` — {ahead}/{behind} vs origin/main"
    return f"`{branch}` (no origin/main reference)"


# ---------------------------------------------------------------------------
# Recent commits (leg 3, 2026-07-29) -- this section exists SOLELY as
# spawn-amortization, not because recent commit subjects are interesting
# reading. Every agent that boots and shells out to `git log` to answer "what
# just happened here" pays a process-spawn fork/exec penalty -- modest on
# POSIX, "brutal on Windows" per this repo's own P0 doctrine (see CLAUDE.md's
# "Windows is first-class" runtime convention: a spawn on a hot path is
# break-class, reimplemented rather than tolerated). 89% of fleet sessions
# were independently re-deriving this fact at boot via their own git log call
# (state/audits/2026-07-29-orientation-cache-boot-facts.md, DoE-claude) before
# this section existed. One cache-time git spawn here amortizes that cost
# across every one of them: this adds ONE git spawn to build_cache -- an
# explicitly cold, ceremony/machine-invoked path (~10 spawns already) with no
# hot-path spawn-tax concern (constraint 7 does not apply here; see module
# docstring's own "no hot-path spawn-tax concern" framing for build_cache as a
# whole). It is NOT on the sync boot path -- boot only ever reads the cache
# FILE this module writes.
#
# Negative-spec: this section is git-log recency ONLY. Working-tree state
# (``git status``, dirty/staged files) must NEVER enter the cache -- ``state/``
# is a shared multi-agent bus and "the tree is dirty" has no meaningful
# lifetime past a single tool call; a cached snapshot would manufacture
# confidently-wrong state for whichever session boots next and reads a stale
# dirty/clean claim. Do not add a working-tree field here or anywhere else in
# this module.
#
# WARNING: this section fails the thin-pointer/route-vs-answer admission test
# (see module docstring, and the recency-sample ruling applied to ``## Audits
# & censuses`` above) if you don't know it's a spawn-tax buy. Read in
# isolation, "- <sha> <subject>" lines look exactly like the recency samples
# that ruling cut. They are not content; they are pre-paid spawn avoidance.
# Applying that test to this section without this context will correctly
# conclude it is content and wrongly cut it.
# ---------------------------------------------------------------------------

RECENT_COMMITS_MAX = 5
"""Number of ``git log --oneline`` entries rendered in ``## Recent commits``
-- DERIVED from the section's spawn-amortization reason above, not a taste
default. 5 is what the boot-time `git log --oneline -5` call this section
amortizes would itself have returned (state/audits/2026-07-29-orientation-
cache-boot-facts.md) -- exactly enough to discharge the spawn the section is
buying out, no more. Resizing this is answered by "what does the agent no
longer need to spawn for", nothing else -- not by how many commit subjects
look nice in the cache."""

_COMMIT_SUBJECT_TRUNCATE_CHARS = 72
"""Max rendered length of one commit subject line, in CODE POINTS -- a plain
Python string slice, not a byte-count bound. A subject dense in multi-byte
UTF-8 (CJK, emoji) can still render up to ~4x this many bytes on one line.
This is not a byte-budget guarantee on its own; the real backstop against an
essay-length or non-ASCII-heavy commit message blowing the cache's byte/line
budget is `_enforce_cache_budget`'s elastic trim on the (elastic) ``## Recent
commits`` section (see CACHE_BUDGET_BYTES / LINE_CEILING below). Truncating
by code point rather than by byte is deliberate -- a byte-based cut risks
splitting a multi-byte character mid-sequence for no real budget gain, since
the elastic trim already catches a genuine flood."""


def emit_recent_commits(repo_root: Path) -> List[str]:
    """Render the ``## Recent commits`` section's bullet lines from ``git log
    --oneline -5`` -- ``- <short sha> <subject>`` per line, subject truncated to
    ``_COMMIT_SUBJECT_TRUNCATE_CHARS``. Degrades to ``[]`` on any git failure
    (detached HEAD with no commits, not a git repo, etc.) -- never raises;
    matches every other emit_* helper's fail-open posture in this module."""
    out = _git(["log", f"-{RECENT_COMMITS_MAX}", "--oneline"], repo_root)
    if not out:
        return []
    lines: List[str] = []
    for raw in out.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        sha, _, subject = raw.partition(" ")
        if len(subject) > _COMMIT_SUBJECT_TRUNCATE_CHARS:
            subject = subject[:_COMMIT_SUBJECT_TRUNCATE_CHARS] + "…"
        lines.append(f"- {sha} {subject}")
    return lines


# ---------------------------------------------------------------------------
# Auto-push health (bash:236-275) -- third section carried on the
# spawn-amortization buy documented above ``## Recent commits``, alongside
# ``## Branch``. All three are git state an agent would otherwise spawn for at
# boot; none of them earn their place as interesting reading, and the
# thin-pointer test applied without that context will wrongly cut them.
# ---------------------------------------------------------------------------

_LASTCLASS_RE = re.compile(r"\((direct push|powershell ssh push)/([a-z-]+) after")

_ACTION_MAP = {
    "non-fast-forward": "branch diverged — reconcile (/workday-start Step 0.4.5) then push",
    "gh-transient": "transient — retry: git push",
    "network": "transient — retry: git push",
    "auth": "auth failure — check credentials, then push",
    "gh-push-protection": "secret blocked — scrub, then push",
    "gh-size-limit": "see .git/push-failures.log; resolve, then push",
    "gh-lfs-quota": "see .git/push-failures.log; resolve, then push",
}


def emit_auto_push_health(repo_root: Path) -> str:
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root) or "unknown"
    if not branch.startswith("work/"):
        return ""
    upstream = f"origin/{branch}"
    if _git(["rev-parse", "--verify", upstream], repo_root) is None:
        return ""
    unpushed_raw = _git(["rev-list", "--count", f"{upstream}..HEAD"], repo_root) or "0"
    unpushed = int(unpushed_raw) if unpushed_raw.isdigit() else 0
    if unpushed == 0:
        return ""

    lastclass = ""
    logf = resolve_git_common_dir(repo_root) / "push-failures.log"
    if logf.is_file() and logf.stat().st_size > 0:
        lines = _read_text_or_empty(logf).splitlines()
        matches = [line for line in lines if f"on {branch} (" in line]
        if matches:
            m = _LASTCLASS_RE.search(matches[-1])
            if m:
                lastclass = m.group(2)

    action = _ACTION_MAP.get(lastclass, f"run: git push origin {branch} (see .git/push-failures.log)")
    cls_label = f" last failure: {lastclass};" if lastclass else ""
    return f"- ⚠ {unpushed} unpushed commit(s) on `{branch}` — auto-push lagging;{cls_label} {action}"


# ---------------------------------------------------------------------------
# Housekeeping surfacing (C17b, 2026-07-23) -- see module docstring
#
# Dedup + per-record cap (fixed 2026-07-28 -- see
# cross-repo/inbox/2026-07-28-example-retrieval-repo-em-orientation-cache-housekeeping-flood.md
# Defect 2): the original renderer showed the log's last N raw lines verbatim, no
# dedup, no per-record cap -- a single flapping item (same script, same error class,
# repeating every regen) rendered as N ~900-byte raw lines, ~18 KB into every session
# boot's injected context. This renders EXACTLY ONCE, here, inside _emit_housekeeping
# -- both write paths (build_cache, patch_pinboard_only) and the fast path all funnel
# through this one function, so there is no second copy of this logic anywhere (see
# module docstring's negative-spec).
# ---------------------------------------------------------------------------

_HOUSEKEEPING_DETAIL_TRUNCATE_CHARS = 200
_HOUSEKEEPING_MAX_RENDERED_GROUPS = 20

# Parses a housekeeping-failures.log record (format owned by
# coordinator_core.ops.ceremony.detached_spawn -- see that module's docstring):
#   [<ISO-8601 UTC timestamp>] <CHILD FAILED|SPAWN FAILED> script=<path> [args=<repr>] :: <detail>
# `detail` may itself contain literal ``\n`` (the writer escapes real newlines to the
# two-character sequence before appending -- see detached_spawn._log_spawn_failure /
# record_child_failure), which this regex treats as ordinary text, not a line break.
_FAILURE_RECORD_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+(?P<verb>CHILD FAILED|SPAWN FAILED)\s+script=(?P<script>\S+)"
    r"(?:\s+args=.*?)?\s*::\s*(?P<detail>.*)$"
)
_ERROR_CLASS_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):")


# Basenames of the archive_sweeps remedy CLIs, derived from REMEDY_COMMANDS so the two
# lists can never drift -- used only to DETECT that a failure record names one of these
# scripts, never to claim it was the cause (see module negative-spec in
# housekeeping_liveness.py: a remedy map is a list of commands, not a call-site registry).
_ARCHIVE_SWEEP_SCRIPT_NAMES = tuple(
    cmd.rsplit("/", 1)[-1] for cmd in REMEDY_COMMANDS.get(ARCHIVE_SWEEPS, ())
)


def _remedy_sub_bullets(class_key: str) -> List[str]:
    """Return indented sub-bullet lines for `class_key`'s on-demand remedy commands, or []
    when that class has no on-demand CLI on record (see REMEDY_COMMANDS negative-spec)."""
    return [f"  - `{cmd}`" for cmd in REMEDY_COMMANDS.get(class_key, ())]


def _parse_failure_line(line: str) -> Optional[dict]:
    """Best-effort parse of one housekeeping-failures.log record. Returns None for a
    line that does not match the expected shape -- the caller renders that line
    degraded (truncated verbatim) instead of dropping it or raising; this log's
    on-disk format is a cross-consumer contract this module does not own alone
    (coordinator/bin/test_sweep_boot.py reads the raw log independently)."""
    m = _FAILURE_RECORD_RE.match(line)
    if not m:
        return None
    detail = m.group("detail")
    script = m.group("script").replace("\\", "/").rsplit("/", 1)[-1]
    class_match = _ERROR_CLASS_RE.match(detail)
    error_class = class_match.group(1) if class_match else detail[:60]
    return {
        "timestamp": m.group("ts"),
        "verb": m.group("verb"),
        "script": script,
        "error_class": error_class,
        "detail": detail,
    }


def _dedup_failure_lines(failure_lines: List[str]) -> Dict[tuple, dict]:
    """Collapse *failure_lines* into groups keyed by (script, error_class) --
    identical-modulo-timestamp/UUID records collapse to one group with a count and a
    first/last-seen timestamp range. A line that fails to parse becomes its own group,
    keyed on its own (truncated) text, so byte-identical malformed lines still dedup
    against each other without ever being silently dropped. Insertion-ordered (dict
    preserves first-seen order in Python 3.7+) so rendering below can cap deterministically
    without re-sorting.
    """
    groups: Dict[tuple, dict] = {}
    for ln in failure_lines:
        parsed = _parse_failure_line(ln)
        if parsed is None:
            key = ("<unparsed>", ln[:120])
        else:
            # verb is part of the key (not displayed -- see _render_failure_group) so a
            # "never even started" SPAWN FAILED and a "ran and failed" CHILD FAILED for
            # the same script/error-class are never silently conflated into one count.
            key = (parsed["script"], parsed["verb"], parsed["error_class"])
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "parsed": parsed is not None,
                "script": parsed["script"] if parsed else "<unparsed>",
                "error_class": parsed["error_class"] if parsed else None,
                "detail": parsed["detail"] if parsed else ln,
                "first_ts": parsed["timestamp"] if parsed else "",
                "last_ts": parsed["timestamp"] if parsed else "",
                "count": 1,
            }
        else:
            group["count"] += 1
            if parsed:
                group["last_ts"] = parsed["timestamp"]
    return groups


def _render_failure_group(group: dict) -> str:
    """Render one dedup group to a single length-capped display line."""
    detail = group["detail"]
    if len(detail) > _HOUSEKEEPING_DETAIL_TRUNCATE_CHARS:
        detail = detail[:_HOUSEKEEPING_DETAIL_TRUNCATE_CHARS] + "…"
    if not group["parsed"]:
        return detail  # degraded: unparseable line, rendered truncated, not dropped
    if group["count"] > 1:
        return (
            f"{group['count']}x {group['script']} {group['error_class']} "
            f"since {group['first_ts']} (latest {group['last_ts']}): {detail}"
        )
    return f"[{group['first_ts']}] {group['script']} {group['error_class']}: {detail}"


def _emit_housekeeping(repo_root: Path) -> List[str]:
    """Peek the shared housekeeping-failures log + liveness stamps and render the
    ``## Housekeeping`` section's bullet lines. Non-destructive -- does NOT clear the
    failures log (see module docstring; clearing is the write call site's job, after a
    successful non-``--check`` write). Returns [] when there is nothing to report (the
    caller omits the whole section, matching every other omit-when-empty section here).

    Renders deduped-by-(script, error class) groups with a count and timestamp range,
    each capped at ``_HOUSEKEEPING_DETAIL_TRUNCATE_CHARS`` -- see module comment above
    ``_HOUSEKEEPING_MAX_RENDERED_GROUPS`` for why (this is the one and only rendering
    site; both write paths and the fast path funnel through this function).
    """
    lines: List[str] = []

    failures_content = read_failures_log(str(repo_root))
    failure_lines = [ln for ln in failures_content.splitlines() if ln.strip()]
    if failure_lines:
        groups = _dedup_failure_lines(failure_lines)
        group_keys = list(groups.keys())
        shown_keys = group_keys[:_HOUSEKEEPING_MAX_RENDERED_GROUPS]
        omitted = len(group_keys) - len(shown_keys)

        if omitted > 0:
            lines.append(
                f"- ⚠ {len(failure_lines)} housekeeping failure(s) recorded across "
                f"{len(group_keys)} distinct issue(s) (showing {len(shown_keys)}):"
            )
        else:
            lines.append(
                f"- ⚠ {len(failure_lines)} housekeeping failure(s) recorded "
                f"({len(group_keys)} distinct issue(s)):"
            )
        lines.extend(f"  - `{_render_failure_group(groups[k])}`" for k in shown_keys)
        if omitted > 0:
            lines.append(f"  - …and {omitted} more distinct issue(s) not shown")

        shown_scripts = {groups[k]["script"] for k in shown_keys}
        if shown_scripts & set(_ARCHIVE_SWEEP_SCRIPT_NAMES):
            lines.extend(_remedy_sub_bullets(ARCHIVE_SWEEPS))

    for class_key, msg in check_stale_detailed(str(repo_root)):
        lines.append(f"- ⚠ stale housekeeping: {msg}")
        lines.extend(_remedy_sub_bullets(class_key))

    return lines


# ---------------------------------------------------------------------------
# Pinboard preservation (bash:277-294)
# ---------------------------------------------------------------------------


def read_existing_pinboard(cache_file: Path) -> str:
    if not cache_file.is_file():
        return ""
    lines = _read_text_or_empty(cache_file).splitlines()
    in_pinboard = False
    for line in lines:
        if line == "## Pinboard":
            in_pinboard = True
            continue
        if in_pinboard and line.startswith("## "):
            break
        if in_pinboard and line.startswith("- "):
            return line[2:]
    return ""


def _first_line(text: str) -> str:
    lines = text.splitlines()
    return lines[0] if lines else ""


# ---------------------------------------------------------------------------
# Emit (bash:296-362)
# ---------------------------------------------------------------------------

_TRUST_CAVEAT_TMPL = (
    "\n## Trust caveats\n"
    "- Unreal Engine project detected (`{uproject}`) — do NOT trust your training data "
    "on UE5 APIs, classes, or Blueprint semantics. Verify every claim via "
    "`mcp__example_retrieval_repo__*` tools or dispatch `game-dev:staff-game-dev` (the Game Dev Reviewer). This "
    "applies to your delegates — restate it in every UE dispatch brief.\n"
)


def _render_cache(
    invoker: str,
    iso_now: str,
    git_head: str,
    uproject_path: str,
    workstreams: List[str],
    rechecks: List[str],
    branch_line: str,
    recent_commits: List[str],
    push_health: str,
    wiki_lines: List[str],
    atlas_lines: List[str],
    capability_pointers_lines: List[str],
    fast_test_lines: List[str],
    audits_lines: List[str],
    housekeeping_lines: List[str],
    pinboard_final: str,
) -> str:
    parts: List[str] = []

    parts.append(
        f"---\ngenerated_by: {invoker}\ngenerated_at: {iso_now}\n"
        f"git_head_at_generation: {git_head}\n---\n\n# Orientation Cache\n"
    )

    if uproject_path:
        parts.append(_TRUST_CAVEAT_TMPL.format(uproject=uproject_path))

    if workstreams:
        parts.append("\n## Active workstreams\n" + "\n".join(workstreams) + "\n")

    if rechecks:
        parts.append("\n## Rechecks due ≤7 days\n" + "\n".join(rechecks) + "\n")

    parts.append(f"\n## Branch\n{branch_line}\n")

    if recent_commits:
        parts.append("\n## Recent commits\n" + "\n".join(recent_commits) + "\n")

    if push_health:
        parts.append(f"\n## Auto-push health\n{push_health}\n")

    if wiki_lines:
        parts.append("\n## Wiki\n" + "\n".join(wiki_lines) + "\n")

    if atlas_lines:
        parts.append("\n## Architecture atlas\n" + "\n".join(atlas_lines) + "\n")

    if capability_pointers_lines:
        parts.append("\n## Capabilities\n" + "\n".join(capability_pointers_lines) + "\n")

    if fast_test_lines:
        parts.append("\n## Fast test\n" + "\n".join(fast_test_lines) + "\n")

    if audits_lines:
        parts.append("\n## Audits & censuses\n" + "\n".join(audits_lines) + "\n")

    if housekeeping_lines:
        parts.append("\n## Housekeeping\n" + "\n".join(housekeeping_lines) + "\n")

    if pinboard_final:
        parts.append(f"\n## Pinboard\n- {pinboard_final}\n")

    output = "".join(parts)
    # Mirrors the bash `$(emit_cache)` command-substitution, which strips ALL
    # trailing newlines, followed by `printf '%s\n' "$OUTPUT"` re-adding exactly one.
    return output.rstrip("\n") + "\n"


def _tier_for_invoker(invoker: str) -> str:
    """Resolve *invoker* to its regen tier.

    ``sweep-boot`` is a MACHINE invoker, not a ceremony -- it fires from the async
    ``sweep-boot`` SessionStart hook when the cache is detected stale, so freshness
    stops depending on a human remembering to run a ceremony. It shares the
    mid-session tier (full re-derive of every disk-sourced section, pinboard
    preserved) with ``workstream-complete``/``handoff`` -- there is no reason for a
    background self-heal to clear the pinboard the way a ceremony invoker does.

    ``quick-wrap`` is mid-session for the same reason ``workstream-complete`` and
    ``handoff`` are: it closes a SESSION, never the working day. The pinboard is a
    day-scoped surface, and quick-wrap's own skill body defers to
    ``/workday-complete`` as the day-level close that supersedes it -- so clearing
    the pinboard here would drop notes the day has not finished with. Added
    2026-07-31: the skill's step 4 told the operator to run this CLI while the
    invoker allowlist had no value for it, so the prescribed step could not be
    performed at all.
    """
    if invoker in ("workday-start", "update-docs"):
        return "ceremony"
    if invoker in ("workstream-complete", "handoff", "quick-wrap", "sweep-boot"):
        return "mid-session"
    raise ValueError(
        f"unknown invoker: {invoker} (expected "
        "workday-start|update-docs|workstream-complete|handoff|quick-wrap|sweep-boot)"
    )


def build_cache(
    invoker: str,
    repo_root: Path,
    pinboard: Optional[str] = None,
    pinboard_set: bool = False,
) -> dict:
    """Compute the orientation-cache output for *repo_root*, without writing it.

    Returns a dict with either ``{"skipped": True, "reason": str}`` (no ``tasks/``
    directory — bash:56-59 early-exit) or ``{"skipped": False, "tier", "output",
    "cache_file", "state_root"}``.

    Raises ValueError for an unrecognised *invoker* (bash:48-52 case-block failure).
    """
    tier = _tier_for_invoker(invoker)

    if not (repo_root / "tasks").is_dir():
        return {
            "skipped": True,
            "reason": f"regenerate-orientation-cache: no tasks/ directory at {repo_root} — skipping (per schema spec)",
        }

    state_root = _state_root(repo_root)
    cache_file = state_root / "orientation_cache.md"

    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_head = _git(["rev-parse", "--short", "HEAD"], repo_root) or "unknown"
    uproject_path = detect_uproject(repo_root)

    workstreams = emit_workstreams(repo_root)
    rechecks = emit_rechecks(repo_root)
    branch_line = emit_branch_line(repo_root)
    recent_commits = emit_recent_commits(repo_root)
    push_health = emit_auto_push_health(repo_root)
    wiki_lines = emit_wiki_pointer(repo_root)
    atlas_lines = emit_architecture_atlas(repo_root)
    capability_pointers_lines = emit_capability_pointers(repo_root)
    fast_test_lines = emit_fast_test(repo_root)
    audits_lines = emit_audits_index(state_root)
    housekeeping_lines = _emit_housekeeping(repo_root)

    pinboard_final = ""
    if tier == "mid-session":
        if pinboard_set:
            pinboard_final = pinboard or ""
        elif cache_file.is_file():
            pinboard_final = read_existing_pinboard(cache_file)
    pinboard_final = _first_line(pinboard_final)

    output = _render_cache(
        invoker,
        iso_now,
        git_head,
        uproject_path,
        workstreams,
        rechecks,
        branch_line,
        recent_commits,
        push_health,
        wiki_lines,
        atlas_lines,
        capability_pointers_lines,
        fast_test_lines,
        audits_lines,
        housekeeping_lines,
        pinboard_final,
    )

    return {
        "skipped": False,
        "tier": tier,
        "output": output,
        "cache_file": cache_file,
        "state_root": state_root,
    }


_LOCK_DIR_SUFFIX = ".orientation_cache.lock"
_LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.02


class _OrientationCacheLock:
    """Cross-repo-process mutex guarding orientation_cache.md's write step.

    Acquire primitive is ``os.mkdir`` on a sibling lock directory — atomic
    identically on POSIX and Windows (unlike ``fcntl``/``msvcrt``, which are
    platform-split), so this needs no platform branch to stay Windows-first-class.

    Both the full-regen writer (``write_cache``) and the fast-path pinboard
    patcher (``patch_pinboard_only``) acquire this same lock around their
    read-then-atomic-replace critical section, so the two writers serialize
    against each other rather than racing on the shared cache_file: whichever
    acquires second reads the FIRST writer's already-committed output, never a
    pre-first-writer snapshot. See ``patch_pinboard_only``'s docstring for the
    residual (pre-existing, not newly introduced) race this does not close.
    """

    def __init__(self, state_root: Path, timeout: Optional[float] = None, poll_interval: Optional[float] = None):
        # Module globals read at INSTANTIATION time (not bound as arg defaults
        # at def-time) so tests can monkeypatch _LOCK_TIMEOUT_S/_LOCK_POLL_S.
        self._dir = state_root / _LOCK_DIR_SUFFIX
        self._timeout = _LOCK_TIMEOUT_S if timeout is None else timeout
        self._poll_interval = _LOCK_POLL_S if poll_interval is None else poll_interval
        self._acquired = False

    def __enter__(self) -> "_OrientationCacheLock":
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                os.mkdir(self._dir)
                self._acquired = True
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"orientation_cache lock timed out after {self._timeout}s: {self._dir}"
                    )
                time.sleep(self._poll_interval)

    def __exit__(self, *exc_info) -> None:
        if self._acquired:
            try:
                os.rmdir(self._dir)
            except OSError:
                pass  # best-effort release; a stale lock dir self-heals on the next acquire's timeout-free path


# ---------------------------------------------------------------------------
# Cache size budget (2026-07-28, reconciled 2026-07-28) -- structural cap
# enforced at the single byte-write chokepoint (_atomic_replace below), so
# every write path -- the two that exist today (write_cache,
# patch_pinboard_only) and any future one -- is bounded regardless of which
# section misbehaves. This generalizes the 2026-07-28 Housekeeping dedup fix
# (see that section's own module comment above _emit_housekeeping): that fix
# bounds ONE section semantically; this bounds the WHOLE artifact
# structurally, so the next misbehaving section cannot reproduce the same
# flood in a new shape.
#
# Negative-spec: there is deliberately no separate "_enforce_cache_budget
# call site" contract to state -- _atomic_replace is the ONLY function that
# ever writes cache bytes to disk (verified: its two callers, write_cache and
# patch_pinboard_only, are its only callers repo-wide), so enforcing here
# instead of at each call site makes a bypass structurally impossible rather
# than merely disciplined-by-convention.
#
# Two independent axes, enforced together, in one trimming pass: bytes
# (CACHE_BUDGET_BYTES) and lines (LINE_CEILING below). They are NOT
# equivalent and neither subsumes the other -- WORKSTREAM_BODY_CAP's
# docstring above names the one documented mechanism (uncapped single-line
# length) by which a cache can pass one axis and fail the other. Both
# constants live here, on the writer, as the single source of truth;
# `verify_orientation_cache_sync` imports both instead of redeclaring its own
# values -- the prior state (each module independently declaring its own
# number, silently able to drift, and already disagreeing on LINE_CEILING
# specifically: this writer had no line-count opinion at all until this
# reconciliation) is the failure shape this section closes.
# ---------------------------------------------------------------------------

CACHE_BUDGET_BYTES = 8 * 1024
"""Hard ceiling on orientation_cache.md's total byte size, enforced inside
_enforce_cache_budget (called from the single byte-write chokepoint,
_atomic_replace). A healthy cache observed locally runs ~2.5 KB; the flooded
cache that prompted this cap (example-retrieval-repo, 2026-07-28, one Housekeeping
record repeating raw and undeduped every regen) was 27.4 KB. 8 KB leaves a
healthy cache >3x headroom while cutting a pathological one by ~70%. Nothing
else hardcodes this number -- retune here.

See `LINE_CEILING` for the sibling bound on the same artifact, enforced in
the same pass -- the two axes can still disagree even though they now share
one enforcement function and (where applicable) one set of shared
per-section constants; see that constant's docstring for the measured
evidence and for why disagreement is expected, not a bug.
"""

LINE_CEILING = 60
"""Hard ceiling on orientation_cache.md's total line count (``wc -l``
semantics -- counts newline bytes, matching `_cache_line_count` below and
`verify_orientation_cache_sync.verify`'s own ``line_count``), enforced inside
`_enforce_cache_budget` in the SAME pass as `CACHE_BUDGET_BYTES`.

Retired predecessor: `verify_orientation_cache_sync` used to independently
declare ``_LINE_CEILING = 35`` and check it ONLY post-hoc, at /update-docs
Phase 11b -- never on the write path, and never reconciled against this
module's own byte budget. That module now imports this constant instead of
redeclaring its own value.

35 was wrong, not merely stale: it predates the 2026-07-23 ``## Housekeeping``
section (C17b) and the 2026-07-27 ``## Priorities`` section (C8), and never
accounted for either. Measured 2026-07-28
(`coordinator_core/orientation/test_regenerate_cache.py`'s line-budget
fixtures, via `_render_cache` with plausible healthy inputs):

- A modest cache -- 3 active workstreams, one non-zero counter, a branch
  line, a 1-failure Housekeeping section, a pinboard note -- renders 29
  lines. Already close to 35.
- A realistically maximal HEALTHY cache -- `WORKSTREAM_MAX` (10) active
  workstreams, a Trust-caveats line, two rechecks, an Auto-push-health line,
  three Priorities entries, a 2-failure Housekeeping section, and a pinboard
  note, i.e. every section the emitter can produce, simultaneously populated
  -- renders 53 lines. 35 fails this outright; it is not a disciplined
  ceiling being tripped by abuse, it is a ceiling reality has already outrun.

Both figures above are the 2026-07-28 measurement and have drifted since, in
one known increment not re-measured: ``## Recent commits`` (leg 3, 2026-07-29)
adds up to ``RECENT_COMMITS_MAX`` + 2 lines. The 2026-07-30 C6 purpose-map
rewrite retired ``## Project``/``## Counters``/``## Priorities`` (and the
counters→index change that briefly lived on ``## Counters``) in favour of
``## Wiki``/``## Architecture atlas``/``## Fast test``/``## Audits &
censuses`` — a net-neutral-to-negative line-count trade, not re-measured
either. Treat 53/29 as lower bounds on today's shapes, not current readings;
re-measure before retuning this constant on them.

60 leaves ~13% headroom over the measured 53-line maximal-healthy case (and
>2x over the 29-line modest case) while still catching a genuine flood --
the housekeeping-flood incident that prompted `CACHE_BUDGET_BYTES` rendered
records with no per-record line cap at all before the same-day dedup fix, so
even a single flapping item pushed well past this ceiling. Retune here if
measurement says otherwise; do not hand-pick a round number by feel (see this
constant's own reconciliation history for why that produced the wrong value
once already).
"""

# Never truncated -- small by construction, load-bearing for orientation.
_CACHE_PROTECTED_SECTIONS = frozenset({
    "Branch", "Rechecks due ≤7 days", "Pinboard",
})
# Trimmed under pressure, largest-first, before anything protected is touched.
_CACHE_ELASTIC_SECTIONS = frozenset({
    "Housekeeping", "Active workstreams", "Auto-push health",
    "Recent commits", "Wiki", "Architecture atlas", "Capabilities",
    "Fast test", "Audits & censuses",
})
# "Capabilities" is elastic, not protected, by deliberate ruling (memo
# cross-repo/inbox/2026-08-14-doe-claude-em-orientation-cache-capability-pointers.md):
# a pointer trimmed by the byte budget costs an agent one lookup, not a wrong
# belief, so protecting it would defend against a cost this section does not
# actually impose.

_CACHE_SECTION_RE = re.compile(r"\n## ([^\n]+)\n")
_CACHE_BUDGET_EXCEEDED_SENTINEL = "orientation cache exceeded its"


def _cache_trim_marker(body_line_count: int) -> str:
    noun = "line" if body_line_count == 1 else "lines"
    return f"- [{body_line_count} {noun} trimmed to fit cache budget]"


def _parse_cache_sections(text: str) -> Optional[tuple]:
    """Split *text* at each ``\\n## Name\\n`` heading boundary into
    ``(preamble, [{"name", "raw", "trimmed"}, ...])``. ``preamble`` carries the
    frontmatter and the ``# Orientation Cache`` title untouched -- neither is
    ever a heading match, so both stay outside every returned section. Each
    section's ``raw`` spans from its own heading through (but not including)
    the next heading, so ``preamble + "".join(s["raw"] for s in sections)``
    always reconstructs *text* exactly.

    Returns ``None`` when *text* has no ``## `` heading at all (nothing this
    parser can act on) -- the caller's contract is to leave such text
    unchanged rather than guess at its shape.
    """
    matches = list(_CACHE_SECTION_RE.finditer(text))
    if not matches:
        return None
    preamble = text[: matches[0].start()]
    sections = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append({"name": m.group(1), "raw": text[start:end], "trimmed": False})
    if preamble + "".join(s["raw"] for s in sections) != text:
        return None  # round-trip mismatch -- structure isn't what this parser assumes
    return preamble, sections


def _cache_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _cache_line_count(text: str) -> int:
    """Count newline bytes -- the exact ``wc -l``-parity rule
    `verify_orientation_cache_sync.verify`'s own ``line_count`` uses (see
    that function). Sharing the counting rule, not just the constant, is
    what guarantees a cache this module writes under `LINE_CEILING` also
    passes the verifier's post-hoc re-check of the same file.
    """
    return text.count("\n")


def _within_cache_budget(text: str) -> bool:
    return _cache_size(text) <= CACHE_BUDGET_BYTES and _cache_line_count(text) <= LINE_CEILING


def _enforce_cache_budget(text: str) -> str:
    """Bound *text* (a fully-rendered orientation-cache body) to BOTH
    ``CACHE_BUDGET_BYTES`` (bytes) and ``LINE_CEILING`` (lines), in one pass.

    Policy (PM-directed, 2026-07-28; unified 2026-07-28 to cover both axes):
      1. Under both budgets: return *text* unchanged -- no gratuitous
         rewriting.
      2. Over either budget: trim ``_CACHE_ELASTIC_SECTIONS`` members
         largest-first to a single ``[N lines trimmed to fit cache budget]``
         marker each, until BOTH budgets are satisfied. Every trim leaves
         that marker -- nothing is ever silently dropped, because a silent
         cap would reproduce the exact failure mode (a hidden flood) this
         cap exists to end. A single trimming pass serves both axes rather
         than running two competing passes that could re-inflate each
         other's result -- trimming a section can only ever shrink both its
         byte size and its line count, so the two objectives never conflict.
      3. A ``## Heading`` this module does not recognise (neither protected nor
         named-elastic -- e.g. a ceremony-supplied section like ``Week
         priorities`` that ``_render_cache`` itself never emits but that does
         appear in live caches) is treated as elastic for trimming purposes,
         but is likewise never fully zeroed -- its heading plus the trim
         marker always survive, so the section's mere existence stays visible.
      4. ``_CACHE_PROTECTED_SECTIONS`` members are never trimmed. If the cache
         is still over EITHER budget after every elastic/unrecognized section
         has been reduced to its marker, protected sections are kept whole
         and a single loud marker naming which budget(s) are still exceeded
         is inserted at the top instead -- that is a bug to surface, not hide.

    Best-effort and defensive: a cache whose structure ``_parse_cache_sections``
    cannot make sense of is returned UNCHANGED. Capping a cache this function
    cannot parse risks corrupting it, which is worse than leaving it oversized
    for one cycle.

    Idempotent: re-applying this to its own output is a no-op. Once trimmed,
    the text is under both budgets, so the very first check short-circuits
    before any section is touched again -- load-bearing because
    ``patch_pinboard_only`` re-reads and re-writes the cache on its fast path,
    so this WILL run repeatedly against the same content in practice.
    """
    if _within_cache_budget(text):
        return text

    parsed = _parse_cache_sections(text)
    if parsed is None:
        return text
    preamble, sections = parsed

    trimmable = [s for s in sections if s["name"] not in _CACHE_PROTECTED_SECTIONS]
    trimmable.sort(key=lambda s: len(s["raw"]), reverse=True)

    for target in trimmable:
        header = f"\n## {target['name']}\n"
        body = target["raw"][len(header):]
        body_line_count = len([ln for ln in body.split("\n") if ln != ""])
        target["raw"] = f"{header}{_cache_trim_marker(body_line_count)}\n"
        target["trimmed"] = True
        rebuilt = preamble + "".join(s["raw"] for s in sections)
        if _within_cache_budget(rebuilt):
            return rebuilt

    rebuilt = preamble + "".join(s["raw"] for s in sections)
    if _CACHE_BUDGET_EXCEEDED_SENTINEL in preamble:
        return rebuilt  # already carries the loud marker -- idempotent, no accumulation
    exceeded = []
    if _cache_size(rebuilt) > CACHE_BUDGET_BYTES:
        exceeded.append(f"{CACHE_BUDGET_BYTES}-byte budget")
    if _cache_line_count(rebuilt) > LINE_CEILING:
        exceeded.append(f"{LINE_CEILING}-line ceiling")
    marker = (
        f"> ⚠ {_CACHE_BUDGET_EXCEEDED_SENTINEL} {' and '.join(exceeded)} even "
        "after trimming every elastic section -- protected sections kept whole. See "
        "coordinator_core.orientation.regenerate_cache.CACHE_BUDGET_BYTES / LINE_CEILING.\n"
    )
    return preamble + marker + "".join(s["raw"] for s in sections)


def _atomic_replace(cache_file: Path, output: str) -> None:
    """Write *output* to *cache_file* atomically (mkstemp + os.replace — PARITY-PLUS).

    shell-doc-ok: quotes the bash oracle's actual redirect, which is the
    non-atomic behavior this port deliberately diverges from.

    Bash oracle used a direct ``printf > "$CACHE_FILE"`` overwrite (non-atomic);
    this upgrade is content-neutral (see module docstring).

    Permission-bit parity: ``mkstemp`` creates its temp file at mode 0600
    (Python default, tighter than the bash oracle's ``>`` redirect, which
    creates at the umask-respecting 0666 default — typically 0644). Explicitly
    chmod the temp file to the umask-respecting default before the atomic
    rename so an existing world/group-readable cache file's permission bits
    are preserved across a regeneration, matching the bash oracle exactly
    (caught by a golden-diff permission-bit check during this port).

    Size budget: *output* is passed through ``_enforce_cache_budget`` here,
    unconditionally, before any byte is written -- see that function and the
    "Cache size budget" section comment above it. This is the ONLY place the
    budget is enforced; both callers (``write_cache``, ``patch_pinboard_only``)
    inherit it automatically, and any future write path gets it for free by
    virtue of having to route through this same chokepoint to write at all.
    """
    output = _enforce_cache_budget(output)
    fd, tmp = tempfile.mkstemp(dir=str(cache_file.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(output)
        current_umask = os.umask(0)
        os.umask(current_umask)
        os.chmod(tmp, 0o666 & ~current_umask)
        os.replace(tmp, cache_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort tmp cleanup on an already-failing path; original exception re-raises below
        raise


def write_cache(cache_file: Path, output: str) -> None:
    """Write *output* to *cache_file* atomically, serialized against a concurrent
    ``patch_pinboard_only`` (or another ``write_cache``) via ``_OrientationCacheLock``.

    See ``_atomic_replace`` for the write mechanics; behavior/output is
    unchanged from the pre-lock implementation for any non-concurrent caller.
    """
    with _OrientationCacheLock(cache_file.parent):
        _atomic_replace(cache_file, output)


def resolve_cache_file(repo_root: Path) -> Path:
    """Return the ``orientation_cache.md`` path for *repo_root* (no I/O beyond
    ``_state_root``'s own). Public seam for callers (the CLI trampoline) that
    need the path without running ``build_cache``'s full derivation."""
    return _state_root(repo_root) / "orientation_cache.md"


_PINBOARD_SECTION_RE = re.compile(r"\n## Pinboard\n- [^\n]*")

# Matches the ``## Housekeeping`` section ONLY when it is the last thing in the string --
# true here because patch_pinboard_only always strips ``## Pinboard`` (always-last per
# _render_cache) FIRST, which leaves Housekeeping (always immediately before Pinboard) as
# the new last section. re.DOTALL so the pattern spans the section's multi-line bullets
# (failure lines + indented remedy sub-bullets), and \Z anchors to the true string end so
# it can never accidentally swallow a later section (there is none, by construction).
_HOUSEKEEPING_SECTION_RE = re.compile(r"\n## Housekeeping\n.*\Z", re.DOTALL)


def patch_pinboard_only(
    cache_file: Path,
    pinboard: str,
    check: bool = False,
    repo_root: Optional[Path] = None,
) -> str:
    """Fast-path write: patch the ``## Pinboard`` line AND re-derive the
    ``## Housekeeping`` section of an EXISTING cache file, with no re-derive of any
    OTHER section (no git spawns, no ``*.uproject`` find, no ``tasks/`` globs).

    Housekeeping re-derive (2026-07-23, AC5 fix -- see module docstring's
    "Housekeeping surfacing" section for the full rationale) is the one deliberate
    exception to this fast path's "zero re-derive" contract: it is a peek of two small
    files (``read_failures_log`` + ``check_stale_detailed``), not a section sweep, so
    the cost this fast path exists to avoid is untouched. *repo_root* resolves the
    housekeeping log/liveness paths (``<repo_root>/state/...``, matching
    ``build_cache``'s own call shape); when omitted it is derived as
    ``cache_file.parent.parent`` (state_root's parent), which is exactly right for the
    non-meta-repo case (``state_root == repo_root / "state"``) that is every real caller
    of this fast path today -- the CLI trampoline never passes a meta-repo cache_file
    into ``--pinboard-only`` (mid-session invokers only; see the CLI's own
    ``--pinboard-only`` invoker gate).

    Tier: a THIRD tier alongside ceremony/mid-session
    (workday-start-internals.md § 5.5) — narrower than mid-session, which
    still re-derives every disk-sourced section and only special-cases the
    pinboard slot. This path re-derives NOTHING except Housekeeping (see above);
    it locates the existing ``## Pinboard`` section (always the LAST section per
    ``_render_cache``'s emit order) and the ``## Housekeeping`` section (always the
    second-to-last, immediately before Pinboard, when present) in the file currently
    on disk and replaces/inserts/removes only those bytes, so every OTHER section is
    preserved byte-identical.

    Concurrency: acquires the same ``_OrientationCacheLock`` as ``write_cache``
    around its read-patch-replace critical section, so a pinboard-only write
    can only ever land strictly before or strictly after a concurrent full
    regen's write — never interleaved with it (no torn file, no
    last-writer-wins on a HALF-written full regen). Residual (pre-existing)
    race NOT closed by this: ``build_cache``'s own read of the existing
    pinboard (mid-session tier, when ``--pinboard`` is not supplied) happens
    OUTSIDE this lock, before ``write_cache`` is called — so a full mid-session
    regen whose ``build_cache`` ran just before a pinboard-only patch's lock
    acquisition can still land (via its own later ``write_cache`` call) with a
    stale pre-patch pinboard value. This race exists identically today between
    two concurrent full regens (build_cache-then-write_cache is not itself
    atomic) and is not introduced or widened by this addition — it is
    NARROWED, since every pinboard-only write removes one full-regen call
    (and its read-derive window) from the two terminator paths that used to
    require one just to write a pinboard line.

    Raises FileNotFoundError if *cache_file* does not exist — nothing to patch;
    caller should fall back to a full ``build_cache`` + ``write_cache``.

    Deliver-exactly-once clearing (fixed 2026-07-28 -- this fast path is a THIRD
    cache-write path and had silently skipped the clear step the other two write
    call sites (CLI trampoline, RPC handler) both perform, so every record this
    path re-derived and embedded via ``_emit_housekeeping`` was delivered and then
    delivered again at every subsequent regen forever, see
    ``cross-repo/inbox/2026-07-28-example-retrieval-repo-em-orientation-cache-housekeeping-flood.md``
    Defect 1): when this call actually re-derived non-empty housekeeping content
    AND actually wrote (``check`` is False), it clears the failures log itself
    afterward -- the same "clear only after a successful, non-check write that has
    already embedded the log's content" contract ``clear_failures_log``'s own
    docstring states, now honoured by all three write paths, not two. A ``check``
    dry run never clears (matches ``build_cache``'s own peek-only posture); an
    empty ``housekeeping_lines`` means nothing was embedded (the log was already
    empty or unreadable), so no clear is issued -- clearing an empty log would be a
    silent no-op but the guard keeps intent legible. The clear happens AFTER
    ``_OrientationCacheLock`` is released, deliberately -- it guards the shared
    cache_file against a concurrent writer, not the (separate) housekeeping-failures
    log, and both other write call sites already clear with that lock not held (the
    CLI/RPC handler call ``write_cache`` -- which acquires and releases the lock
    internally -- and only then call ``clear_failures_log`` as a separate, unlocked
    step). Matching that timing keeps this path's read(_emit_housekeeping)-to-clear
    window the SAME PRE-EXISTING shape as the other two sites (a record appended by
    a detached child between the read and the clear is not covered by this lock at
    any of the three sites and would be silently dropped without ever being
    delivered) -- not newly introduced, not widened, and this path's window is if
    anything narrower since there is no git-spawn / section-sweep work between the
    read and the clear the way a full regen has. Closing that window itself is out
    of scope here (see the residual-race note above for the analogous, already-
    accepted pinboard race) and is left as a known residual, not silently accepted:
    a future fix could clear only the exact failure-log bytes ``_emit_housekeeping``
    actually read (rather than a blanket truncate), which would eliminate it.
    """
    line = _first_line(pinboard)[:400]
    resolved_repo_root = repo_root if repo_root is not None else cache_file.parent.parent
    with _OrientationCacheLock(cache_file.parent):
        if not cache_file.is_file():
            raise FileNotFoundError(
                f"patch_pinboard_only: {cache_file} does not exist yet — run a full regen first"
            )
        content = _read_text_or_empty(cache_file)
        body = content.rstrip("\n")
        # Strip Pinboard FIRST (always-last section) so Housekeeping -- always the
        # section immediately before it -- becomes the new last section, matching
        # _HOUSEKEEPING_SECTION_RE's "last thing in the string" anchor.
        body = _PINBOARD_SECTION_RE.sub("", body, count=1).rstrip("\n")
        body = _HOUSEKEEPING_SECTION_RE.sub("", body, count=1).rstrip("\n")

        housekeeping_lines = _emit_housekeeping(resolved_repo_root)

        # Every OTHER section is emitted by _render_cache as "\n## X\n...\n",
        # concatenated onto a prior section's own trailing "\n" — i.e. a blank
        # line always separates sections. `body` above has no trailing
        # newline at all (just stripped), so re-add the full "\n\n## X"
        # separator here to reproduce that same blank-line spacing exactly,
        # whether this is a fresh insert, a replace, or a removal — for BOTH
        # the (re-derived) Housekeeping section and the Pinboard line, in the
        # same order _render_cache emits them (Housekeeping, then Pinboard).
        tail = ""
        if housekeeping_lines:
            tail += "\n\n## Housekeeping\n" + "\n".join(housekeeping_lines)
        if line:
            tail += f"\n\n## Pinboard\n- {line}"
        new_content = f"{body}{tail}\n"
        if not check:
            _atomic_replace(cache_file, new_content)
    if not check and housekeeping_lines:
        clear_failures_log(str(resolved_repo_root))
    return new_content


# ---------------------------------------------------------------------------
# JSON-RPC handler (future daemon-RPC callers; NOT the CLI trampoline's path —
# see module docstring). Central-registry wiring (ops/__init__.py,
# ops/_registry_map.py, ipc.py::_OP_KEY_SCOPE, authz/classification.py) is
# deferred to the EM per the build-wave's shared-tree concurrency-safety rule.
# ---------------------------------------------------------------------------


@register_op("orientation.regenerate_cache")
async def _orientation_regenerate_cache(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'orientation.regenerate_cache' handler.

    MUTATING (writes the per-repo state/orientation_cache.md).

    Required params:
        invoker (str) — one of workday-start | update-docs | workstream-complete | handoff.

    Optional params:
        check         (bool) — if True, compute and return output WITHOUT writing.
        pinboard      (str)  — mid-session-tier pinboard note override.
        pinboard_set  (bool) — True if `pinboard` was explicitly supplied (distinguishes
                                "" clear-pinboard from "unset, preserve existing").

    repo_root is the per-request resolved repo root (injected by ipc.dispatch_message
    from the _origin_worktree envelope field). Fails loud when None — no silent
    fallback to a process-cwd guess for a MUTATING per-repo write.
    """
    if repo_root is None:
        raise ValueError(
            "orientation.regenerate_cache requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None."
        )
    invoker = params.get("invoker", "")
    result = build_cache(
        invoker=invoker,
        repo_root=Path(repo_root),
        pinboard=params.get("pinboard"),
        pinboard_set=bool(params.get("pinboard_set", False)),
    )
    if result["skipped"]:
        return result
    if not params.get("check", False):
        write_cache(result["cache_file"], result["output"])
        clear_failures_log(str(repo_root))
    return {
        "skipped": False,
        "tier": result["tier"],
        "cache_file": str(result["cache_file"]),
        "output": result["output"],
    }
