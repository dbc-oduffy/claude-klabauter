"""
coordinator_core.install.shell_rc_guard — the fleet's ONE sentinel-guarded
POSIX shell-rc block writer.

Registered op: ``install.write_shell_rc_guard_block``
Contract (state/audits/2026-07-22-command-payload-inventory/op-classification.tsv,
row ``install-shell-rc-guard-block``): ``params: {check_only: bool} ->
{rc_path: str, already_present: bool, modified: bool}`` for the legacy
single-file/CLAUDE_KLABAUTER_CLONE call shape (see § Two calling shapes below).

Purpose: appends a sentinel-delimited block to one or more POSIX shell rc /
login-profile files, idempotently, no-op on native Windows. Originally a
single-purpose ``CLAUDE_KLABAUTER_CLONE``-export writer; generalized
(`docs/plans/2026-07-25-posix-bareword-path-provisioning.md` C1) into the
fleet's one rc-mutation surface so a future PATH-provisioning consumer never
has to hand-roll a second, independently-buggy sentinel-block implementation
(the Director of Engineering F2). Every consumer — the ``CLAUDE_KLABAUTER_CLONE`` export, the claude-CLI PATH
block, and the settings-home ``bin/`` PATH block — now routes through
``write_shell_rc_guard_block`` / ``write_path_entry_guard_blocks``, and all
per-file read/strip/write logic lives exactly once, in
``_write_block_to_file``.

§ Two calling shapes:
  1. **Legacy single-file** (``rc_files`` omitted): resolves exactly one rc
     file via ``$SHELL`` (mirrors the original ``CLAUDE_KLABAUTER_CLONE``-only writer;
     see ``_resolve_rc_path``) and returns the flat
     ``{rc_path, already_present, modified}`` dict the
     ``install.write_shell_rc_guard_block`` op contract and the existing
     test suite (``test_shell_rc_guard.py``) already depend on.
  2. **Multi-file** (``rc_files`` given explicitly, or via the
     ``write_path_entry_guard_blocks`` convenience wrapper): writes the same
     block, independently sentinel-guarded, to EVERY file in ``rc_files``,
     and returns an aggregate ``{results, modified, already_present}`` dict
     keyed by path. This is the shape ``substrate.py``'s claude-CLI and
     ``bin_dst`` PATH legs use — see ``applicable_rc_files``.

Bakes the resolved value into the written block at install time rather than
re-resolving it every shell start — a deliberate, oracle-preserved design
choice for the ``CLAUDE_KLABAUTER_CLONE`` case (`distinct-ops-new.tsv` row
`install-shell-rc-guard-block`: "Bakes CLAUDE_KLABAUTER_CLONE path into the written
block rather than re-resolving at eval time — preserve that design choice
when porting"). A stale bake is recovered the same way a relocated PATH
entry is (see § Relocation self-heal below): rerun the writer.

Rc-path resolution (legacy single-file shape) mirrors the pre-generalization
``substrate.py`` PATH-block writer, with one platform correction: `MSYSTEM`
(Git-Bash/MSYS) forces `.bashrc`, else `$SHELL` selects `.zshrc` / `.bashrc`
by an `.exe`-tolerant suffix match, defaulting to `.zshrc`. The bare
basename-equality form it replaced silently missed Git-Bash's `bash.exe` —
see ``_resolve_rc_path``'s own negative-spec. The multi-file shape does NOT have this
single-pick limitation — ``applicable_rc_files`` returns every applicable
login-profile (`.zprofile`, `.bash_profile`, `.profile`) and interactive-rc
(`.zshrc`, `.bashrc`) file independently, fixing the trap where `$SHELL`
picks `.bash_profile` while the interactive default shell is zsh.

§ Relocation self-heal (AC12): idempotency is checked against the exact
rendered block text, not merely sentinel-marker presence. If a sentinel is
present but its body has drifted (e.g. ``COORDINATOR_SETTINGS_HOME``
changed and the installer re-ran), the stale block is stripped and a fresh
one appended in the same write — a re-run never leaves two blocks, and
never leaves a stale PATH entry behind.

§ CI limit (the Director of Engineering F3 — stated explicitly, not implied): rc-file writes do
NOT cover CI or other non-interactive/non-login invocations. `.zshrc` is
interactive-zsh only; `.bashrc` is interactive-non-login-bash only;
`.zprofile`/`.bash_profile`/`.profile` are login-shell only; `sh -c`/
`bash -c` CI runners source none of the five files this module writes. The
explicit-path form remains the only reachable contract in those contexts —
nothing in this module changes that.

DEC-7 idempotency/platform note (op-classification.tsv rates this row
idempotency-hazard "none" and platform-hazard "medium" — AC8 requires a
recorded note for medium-or-high in EITHER column):
  - Idempotency (none, i.e. already satisfied): see § Relocation self-heal
    above — a second invocation with identical inputs finds the exact
    rendered block already present and returns `modified: False` without
    touching the file. `check_only` never mutates regardless of presence.
  - Platform (medium, closed per the oracle's own remediation): POSIX shell
    rc files have no native Windows equivalent outside Git Bash/WSL, and
    guessing one under native Windows would silently write to a file no
    native shell reads. Closed via an explicit `os.name == "nt"` branch to
    a documented no-op — never guessing a POSIX path or trusting `$SHELL`
    (frequently unset/irrelevant on native Windows).

Mandated resolver used (legacy CLAUDE_KLABAUTER_CLONE default body only):
`coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root()` (this op's ONE
cross-repo-shaped fact — the claude-klabauter clone's own root — per the plan's
§ Mandated resolvers; never a `parents[n]` guess).

§ Shared internal surface (Review: code-reviewer, Finding 4, nit): three
names below keep their leading underscore despite being imported by
`uninstall_legs.py` — `_resolve_rc_path`, `_sentinel_markers`, and
`_strip_block_text`. This is a deliberate choice, not an oversight: the
underscore still signals "not part of the public op/wrapper API"
(`write_shell_rc_guard_block`, `write_path_entry_guard_blocks`,
`applicable_rc_files`), while this docstring section is the explicit,
searchable marker that these three are the module's sanctioned
internal-but-cross-module-shared surface — distinct from a truly private
helper like `_write_block_to_file`, which no other module may import. Kept
underscore-prefixed (rather than renamed public) to match this module's
existing convention of underscore-by-default, and to avoid widening the
rename's blast radius across `uninstall_legs.py` and its test coverage for
a naming-only nit.

Negative-spec:
  - Does NOT support `$SHELL` values other than zsh/bash in the LEGACY
    single-file resolver (`_resolve_rc_path`) — the oracle's own rc-path
    selection is exactly this two-way branch; a wider shell family is out
    of scope for a mechanical port. The multi-file shape sidesteps this
    entirely by writing every applicable file rather than picking one.
  - Does NOT compare `$SHELL`'s basename for EQUALITY against `"bash"` /
    `"zsh"` — Git-Bash/MSYS `$SHELL` values carry a `.exe` suffix, and an
    exact match silently resolves them to `.zshrc`, a file that shell
    never sources. Suffix-match, and honour `MSYSTEM` first.
  - Does NOT strip or fully remove a previously-written block on its own —
    full removal (uninstall) is `coordinator_core.install.uninstall_legs`'s
    job. This module's own strip-and-rewrite only fires on a body-content
    mismatch (relocation self-heal), and always re-adds a fresh block in
    the same operation — it never leaves a file with no block at all.
  - Does NOT claim rc-file provisioning covers CI or non-interactive/
    non-login invocations — see § CI limit above.
  - The sentinel-guarded read/strip/write logic exists exactly ONCE in this
    module (`_write_block_to_file`) — no consumer, in this module or in
    `substrate.py`, may hand-roll a second copy of it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root_with_class
from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

# Generator-provenance declaration (generator_provenance.py). Appends a
# sentinel block to the operator's POSIX shell rc/login-profile files
# (module docstring: "the fleet's ONE sentinel-guarded POSIX shell-rc block
# writer") -- the operator's home shell config, outside the tracked repo.
GENERATES = []

_RC_BLOCK_CLAUSE_INDEX = 0
"""Index of this writer's sole (SHAPED) clause within `WRITE_SURFACE.clauses`
below — the clause `write_shell_rc_guard_block` journals against."""


def _sentinel_markers(sentinel_id: str) -> Tuple[str, str]:
    """Derive the BEGIN/END sentinel pair for `sentinel_id`. For
    `sentinel_id="CLAUDE_KLABAUTER_CLONE"` this reproduces the pre-generalization
    literal exactly, byte-for-byte — see the module-level `SENTINEL_BEGIN`/
    `SENTINEL_END` constants below, which are DERIVED from this function
    rather than hand-duplicated, so the two can never drift apart."""
    return (
        f"# --- coordinator claude-klabauter {sentinel_id} shell-init guard [generated] ---",
        f"# --- end coordinator claude-klabauter {sentinel_id} shell-init guard ---",
    )


SENTINEL_BEGIN, SENTINEL_END = _sentinel_markers("CLAUDE_KLABAUTER_CLONE")


def _resolve_rc_path() -> Path:
    """Pick the rc file to guard for the LEGACY single-file calling shape:
    `MSYSTEM` (Git-Bash/MSYS) forces `.bashrc`, otherwise `$SHELL` selects
    `.zshrc` / `.bashrc`, defaulting to `.zshrc` when `$SHELL` is unset or
    names neither. Retained for the `install.write_shell_rc_guard_block` op
    contract — the multi-file shape (`applicable_rc_files`) is the fix for
    this resolver's single-pick limitation, not a replacement of it.

    Negative-spec — never compare `$SHELL`'s basename for EQUALITY with
    `"bash"`. Under Git-Bash/MSYS on Windows `$SHELL` basenames to
    `bash.exe`, an exact-match miss that silently returned `.zshrc` — a
    file that shell never sources, so the guard block landed where it
    could not take effect and `uninstall_legs`' CLAUDE_KLABAUTER_CLONE strip leg
    (the resolver's other live caller) looked for it in the wrong file.
    The `.exe`-tolerant suffix match plus the `MSYSTEM` precedence below
    mirror `ops/gen_claude_doe_shim.py`'s resolver, which got this right
    first; two resolvers, one platform assumption."""
    home = Path(os.environ.get("HOME") or str(Path.home()))
    if os.environ.get("MSYSTEM"):
        return home / ".bashrc"
    shell = os.environ.get("SHELL", "zsh")
    if shell.endswith(("bash", "bash.exe")):
        return home / ".bashrc"
    return home / ".zshrc"


def applicable_rc_files(home: Optional[Path] = None) -> List[Path]:
    """Every login-profile file (`.zprofile`, `.bash_profile`, `.profile`)
    plus interactive-rc file (`.zshrc`, `.bashrc`) a POSIX PATH/export
    guard block may need to reach — independent of `$SHELL`, so a block
    lands in the file the CURRENT interactive shell actually sources
    rather than whichever one `$SHELL`'s single guess happened to name
    (fixes the trap where `$SHELL` picks `.bash_profile` while the
    interactive default shell is zsh, and the trap where `.bash_profile`
    alone is not always a login shell)."""
    resolved_home = Path(home) if home is not None else Path(os.environ.get("HOME") or str(Path.home()))
    return [
        resolved_home / name
        for name in (".zprofile", ".bash_profile", ".profile", ".zshrc", ".bashrc")
    ]


def _shell_dquote_escape(value: str) -> str:
    """Escape `value` for safe interpolation inside a double-quoted POSIX
    shell string (and inside a `case` pattern, which undergoes the same
    parameter/command-substitution expansion as a double-quoted string).
    Backslash-escapes backslash, double-quote, backtick, and `$` — in that
    order, so escaping backslash first never double-escapes the characters
    escaped after it. Review: code-reviewer (Finding 2, P1) — every value
    this module interpolates into a written rc-file block (a PATH
    directory via `_build_path_entry_body`, and the legacy CLAUDE_KLABAUTER_CLONE
    default body) is operator/environment-controlled (e.g. `bin_dst`
    traces to `COORDINATOR_SETTINGS_HOME`) and was previously interpolated
    unescaped — an unescaped `$(...)` or backtick becomes command
    substitution that executes on every future login/interactive shell
    once the block is sourced: a persistent auto-executed payload, not a
    one-shot. Fixed once here so all three call sites are covered."""
    out = value.replace("\\", "\\\\")
    out = out.replace('"', '\\"')
    out = out.replace("`", "\\`")
    out = out.replace("$", "\\$")
    return out


def _build_path_entry_body(path_entry: str, position: str) -> str:
    """Render the runtime `case ":$PATH:" in ...` idempotency guard line
    for a single PATH directory, per `position`:
      - "prepend": the directory wins ties (used for the claude-CLI dir,
        whose ordering is unchanged from the pre-generalization writer).
      - "append": a colliding system binary always wins (AC5 — used for
        `bin_dst`, appended never prepended, deliberately).

    `path_entry` is escaped via `_shell_dquote_escape` before
    interpolation — see that function's docstring."""
    escaped = _shell_dquote_escape(path_entry)
    if position == "prepend":
        return f'case ":$PATH:" in *":{escaped}:"*) ;; *) export PATH="{escaped}:$PATH" ;; esac'
    return f'case ":$PATH:" in *":{escaped}:"*) ;; *) export PATH="$PATH:{escaped}" ;; esac'


def _desired_block(begin: str, body: str, end: str) -> str:
    return f"{begin}\n{body}\n{end}"


def _strip_block_text(text: str, begin_marker: str, end_marker: str) -> str:
    """In-memory strip of the block delimited by begin_marker..end_marker
    (inclusive) from `text`. This is the ONE sentinel-block strip transform
    in the tree: `uninstall_legs._strip_sentinel_block` delegates here for
    the transform and keeps only its own file I/O around it, so the two can
    never drift. The dependency edge runs `uninstall_legs` -> this module;
    do not reintroduce a second copy in either direction.

    No-op — returns `text` completely unchanged — if `begin_marker` is
    absent, AND ALSO if `begin_marker` is present but `end_marker` never
    follows it before end-of-text (a genuine no-op claim; it previously was
    not one). Review: code-reviewer (Finding 1, P1) — an unterminated block
    (a truncated write, a crash mid-install, or a hand-edit that removed
    the END line) used to set the internal skip-state True and never reset
    it, silently dropping every remaining line of the file — including the
    operator's own unrelated shell customizations below the orphaned
    marker. Both callers (`_write_block_to_file`'s relocation self-heal and
    `uninstall_legs._strip_sentinel_block`) write the result straight back
    to disk with no round-trip check, so that loss was committed, not just
    computed. Returning the original text unchanged instead lets the
    relocation self-heal fall back to appending a fresh block after the
    corrupted one (still a working install) rather than destroying
    content, and lets uninstall leave the malformed block in place rather
    than eating the rest of the file."""
    lines = text.split("\n")
    out: List[str] = []
    skip = False
    terminated = True
    for line in lines:
        if not skip and line == begin_marker:
            skip = True
            terminated = False
            continue
        if skip and line == end_marker:
            skip = False
            terminated = True
            continue
        if skip:
            continue
        out.append(line)
    if not terminated:
        print(
            f"[shell-rc-guard] WARNING: found {begin_marker!r} with no matching "
            f"{end_marker!r} before end of file — leaving the file unchanged "
            "rather than risk silently dropping unrelated content below the "
            "orphaned marker",
            file=sys.stderr,
        )
        return text
    return "\n".join(out)


def _write_block_to_file(rc_path: Path, begin: str, end: str, body: str, check_only: bool, label: str) -> dict:
    """The ONE place any consumer's guard-block file I/O happens (module
    negative-spec: this logic exists exactly once fleet-wide). Idempotent
    against the exact rendered block, self-heals a stale block on content
    mismatch (§ Relocation self-heal in the module docstring — AC12), never
    raises: I/O failures degrade to a stderr message and `modified: False`.
    """
    try:
        existing_text = rc_path.read_text(encoding="utf-8", errors="replace") if rc_path.is_file() else ""
    except OSError as exc:
        print(f"[shell-rc-guard] failed to read {rc_path}: {exc}", file=sys.stderr)
        existing_text = ""

    desired_block = _desired_block(begin, body, end)
    already_present = desired_block in existing_text
    stale_present = (not already_present) and (begin in existing_text)

    if already_present:
        return {"rc_path": str(rc_path), "already_present": True, "modified": False, "stale_present": False}

    if check_only:
        verb = "update (relocated)" if stale_present else "append"
        print(f"[shell-rc-guard] would: {verb} {label} guard block in {rc_path}")
        return {"rc_path": str(rc_path), "already_present": False, "modified": False, "stale_present": stale_present}

    # Review: coordinator:code-reviewer — this is the ONE place any consumer's
    # guard-block file I/O happens (per this function's own docstring), so
    # gating here is what makes every caller (write_shell_rc_guard_block,
    # write_path_entry_guard_blocks, and the `install.write_shell_rc_guard_block`
    # IPC handler) inherit COORDINATOR_DISABLE_MACHINE_MUTATION coverage
    # without needing its own call-site gate. Deferred import — see
    # substrate_migrate.py's identical pattern for why this can't be a
    # module-level import (substrate.py imports this module at module load
    # time, so a module-level back-import would deadlock the import graph).
    from coordinator_core.install import substrate as _substrate_mod

    blocked = _substrate_mod._refuse_machine_mutation(
        str(rc_path), what=f"write {label} guard block into {rc_path}", check_temp_path=False,
    )
    if blocked:
        print(f"[shell-rc-guard] REFUSED: {blocked}", file=sys.stderr)
        return {"rc_path": str(rc_path), "already_present": False, "modified": False, "stale_present": stale_present}

    if stale_present:
        existing_text = _strip_block_text(existing_text, begin, end)

    not_writable = (rc_path.exists() and not os.access(rc_path, os.W_OK)) or (
        not rc_path.exists() and not os.access(rc_path.parent, os.W_OK)
    )
    if not_writable:
        print(f"[shell-rc-guard] WARNING: {rc_path} not writable; add this to your shell profile manually:", file=sys.stderr)
        for line in body.split("\n"):
            print(f"[shell-rc-guard]   {line}", file=sys.stderr)
        return {"rc_path": str(rc_path), "already_present": False, "modified": False, "stale_present": stale_present}

    try:
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        separator = "\n" if existing_text and not existing_text.endswith("\n") else ""
        rc_path.write_text(f"{existing_text}{separator}{desired_block}\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[shell-rc-guard] ERROR: failed to write {label} guard block to {rc_path}: {exc}", file=sys.stderr)
        return {"rc_path": str(rc_path), "already_present": False, "modified": False, "stale_present": stale_present}

    print(f"[shell-rc-guard] {'updated' if stale_present else 'added'} {label} guard block in {rc_path}")
    return {"rc_path": str(rc_path), "already_present": False, "modified": True, "stale_present": stale_present}


def write_shell_rc_guard_block(
    body: Optional[str] = None,
    sentinel_id: str = "CLAUDE_KLABAUTER_CLONE",
    rc_files: Optional[Iterable[Path]] = None,
    position: str = "append",
    check_only: bool = False,
    *,
    path_entry: Optional[str] = None,
) -> dict:
    """Core op logic — see module docstring §§ Two calling shapes /
    Relocation self-heal / CI limit for the full contract. Never raises:
    I/O and resolver failures degrade to a stderr message and a
    `modified: False` result.

    Args:
        body: literal block content (excluding sentinels). Ignored if
            `path_entry` is given. If both `body` and `path_entry` are
            omitted and `sentinel_id == "CLAUDE_KLABAUTER_CLONE"`, the legacy
            default body (`export CLAUDE_KLABAUTER_CLONE="<resolved clone root>"`)
            is auto-resolved via `coordinator_claude_klabauter_root()`.
        sentinel_id: identity keying the derived BEGIN/END markers (see
            `_sentinel_markers`).
        rc_files: target files. Omitted (`None`) selects the LEGACY
            single-file calling shape (§ Two calling shapes shape 1);
            given explicitly (e.g. via `applicable_rc_files()` or the
            `write_path_entry_guard_blocks` convenience wrapper) selects
            the multi-file shape (shape 2).
        position: "append" | "prepend" — only consulted when `path_entry`
            is given (see `_build_path_entry_body`).
        check_only: never mutates; reports what would happen.
    """
    legacy_single_file = rc_files is None

    if os.name == "nt":
        print(
            "[shell-rc-guard] native Windows has no POSIX shell-rc equivalent "
            "outside Git Bash/WSL — skipping (see module docstring DEC-7 note)",
            file=sys.stderr,
        )
        # Platform branch never fires on native Windows — deliberately left
        # UNREPORTED here rather than journaled empty: this early return
        # predates any settings-home/journal-path resolution this run would
        # otherwise perform, and the design note explicitly permits either
        # choice for a non-firing platform branch ("journal the true
        # resolved set ... or do not journal that clause at all").
        if legacy_single_file:
            return {"rc_path": "", "already_present": False, "modified": False}
        return {"results": {}, "modified": False, "already_present": False}

    if path_entry is not None:
        body = _build_path_entry_body(path_entry, position)
    elif body is None:
        if sentinel_id != "CLAUDE_KLABAUTER_CLONE":
            raise ValueError(
                "write_shell_rc_guard_block: body or path_entry is required "
                f"for sentinel_id={sentinel_id!r}"
            )
        try:
            claude_klabauter_clone, _resolution_class = coordinator_claude_klabauter_root_with_class()
        except RuntimeError as exc:
            print(f"[shell-rc-guard] skip: could not resolve claude-klabauter clone root: {exc}", file=sys.stderr)
            # Discovery (the claude-klabauter clone root) came up empty — resolved
            # to nothing this run, same reasoning as the native-Windows
            # branch above.
            if not check_only:
                from coordinator_core.install import resolution_journal

                resolution_journal.record_resolution("shell-rc-guard", _RC_BLOCK_CLAUSE_INDEX, ())
            if legacy_single_file:
                return {"rc_path": str(_resolve_rc_path()), "already_present": False, "modified": False}
            return {"results": {}, "modified": False, "already_present": False}
        body = f'export CLAUDE_KLABAUTER_CLONE="{_shell_dquote_escape(claude_klabauter_clone)}"'

    begin, end = _sentinel_markers(sentinel_id)
    files = [_resolve_rc_path()] if legacy_single_file else list(rc_files)

    results = {
        str(f): _write_block_to_file(f, begin, end, body, check_only, sentinel_id)
        for f in files
    }

    # Journal the concrete, resolved rc-block entries: a file the guard
    # actually wrote this run (`modified`), or one already confirmed to
    # carry the exact rendered block (`already_present`, itself a genuine
    # on-disk fact `_write_block_to_file` checks unconditionally, before
    # its own check_only branch — see that function's docstring). A file
    # left untouched because the write was refused/failed/not-writable
    # produces no entry — there is nothing confirmed to journal for it.
    # `check_only` never journals at all: it is a preview mode, not a real
    # install run, and must never contaminate the receipt with an entry no
    # write (or confirmed-present read) was actually performed under.
    if not check_only:
        from coordinator_core.install import resolution_journal

        resolved_entries = tuple(
            WriteSurfaceEntry(
                kind="rc-block",
                path=path_str,
                begin_marker=begin,
                end_marker=end,
            )
            for path_str, result in results.items()
            if result.get("modified") or result.get("already_present")
        )
        resolution_journal.record_resolution("shell-rc-guard", _RC_BLOCK_CLAUSE_INDEX, resolved_entries)

    if legacy_single_file:
        return next(iter(results.values()))

    return {
        "results": results,
        "modified": any(r["modified"] for r in results.values()),
        "already_present": all(r["already_present"] for r in results.values()),
    }


def write_path_entry_guard_blocks(
    path_entry: str,
    sentinel_id: str,
    position: str,
    home: Optional[Path] = None,
    check_only: bool = False,
) -> dict:
    """Convenience wrapper for a PATH-directory guard block written to
    EVERY applicable POSIX profile/rc file (`applicable_rc_files`) in one
    call — the shape both of `substrate.py`'s new PATH consumers
    (claude-CLI dir, `position="prepend"`; `bin_dst`, `position="append"`)
    use. Returns the multi-file aggregate dict shape (§ Two calling shapes
    shape 2)."""
    return write_shell_rc_guard_block(
        sentinel_id=sentinel_id,
        rc_files=applicable_rc_files(home),
        position=position,
        check_only=check_only,
        path_entry=path_entry,
    )


@register_op("install.write_shell_rc_guard_block")
async def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'install.write_shell_rc_guard_block' handler.

    Writes to the operator's `$HOME` shell rc file, not the caller's repo —
    `repo_root` is accepted for handler-signature parity (per
    `coordinator_core.ipc.register_op`'s contract) but unused, matching the
    op-classification manifest's scope-verdict ("writes to the operator's
    $HOME shell rc file, not the caller's repo"). Always the legacy
    single-file CLAUDE_KLABAUTER_CLONE shape — the op contract is unchanged by this
    module's generalization.
    """
    del repo_root
    check_only = bool(params.get("check_only", False))
    return write_shell_rc_guard_block(check_only=check_only)


_PLACEHOLDER_BEGIN, _PLACEHOLDER_END = _sentinel_markers("<sentinel_id>")
"""BEGIN/END marker TEMPLATE, derived from `_sentinel_markers` itself (never
hand-retyped) with the literal placeholder `"<sentinel_id>"` standing in for
whatever `sentinel_id` a given call site passes. Recomputing from
`_sentinel_markers` at import time — rather than pasting the rendered
string — is what keeps `WRITE_SURFACE` from silently drifting the moment
`_sentinel_markers`'s format changes; the test module re-derives the same
pair independently and asserts equality against this constant."""


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="shell-rc-guard",
    source_module="coordinator_core.install.shell_rc_guard",
    clauses=(
        # This writer's surface is SHAPED, not enumerable in source, on two
        # independent axes:
        #   - WHICH files: `rc_files` is a caller-supplied parameter. The
        #     legacy single-file shape resolves exactly one of
        #     `.zshrc`/`.bashrc` via `_resolve_rc_path` ($SHELL-branched);
        #     the multi-file shape (`write_path_entry_guard_blocks`) targets
        #     every file `applicable_rc_files` names — currently a fixed
        #     five (`.zprofile`, `.bash_profile`, `.profile`, `.zshrc`,
        #     `.bashrc`), but that tuple lives in `applicable_rc_files`, not
        #     here, so a future edit to it is not a silent declaration
        #     drift.
        #   - WHICH sentinel/marker pair: `sentinel_id` (and `position`) are
        #     parameters of `write_shell_rc_guard_block` /
        #     `write_path_entry_guard_blocks`, not a closed enum owned by
        #     this module — today's two known callers pass "CLAUDE_KLABAUTER_CLONE"
        #     (`substrate.py`'s predecessor / the legacy default body) and
        #     "SETTINGS_HOME_BIN" / "CLAUDE_CLI_PATH" (`substrate.py`'s two
        #     PATH legs), but a third caller passing a third `sentinel_id`
        #     tomorrow is in-contract, not a violation — the shaped
        #     declaration below covers it without a code change here.
        ShapedClause(
            discovered_by=(
                "applicable_rc_files (target file set) + the sentinel_id "
                "parameter of write_shell_rc_guard_block / "
                "write_path_entry_guard_blocks, rendered through "
                "_sentinel_markers into the begin/end marker pair"
            ),
            entry_template=WriteSurfaceEntry(
                kind="rc-block",
                path="$HOME/<rc-file: .zprofile|.bash_profile|.profile|.zshrc|.bashrc>",
                begin_marker=_PLACEHOLDER_BEGIN,
                end_marker=_PLACEHOLDER_END,
            ),
        ),
    ),
)
"""This writer always writes a BEGIN and a matching END marker together in
the same operation (`_write_block_to_file`'s single write call renders
`_desired_block(begin, body, end)` as one unit) — there is no code path in
this module that leaves a begin-only block on a freshly-written file, so
`ABSENT_ON_LEGACY_INSTALLS` does not apply to entries this writer produces.
(An unterminated block can still exist on disk — see `_strip_block_text`'s
corruption-recovery branch — but that is pre-existing damage this writer
detects and refuses to touch, never something it itself wrote.)"""
