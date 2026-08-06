"""
coordinator_core.install.gen_settings_hooks — settings.json hooks-block generator.

Port of: ``coordinator/bin/gen-settings-hooks.sh`` (example-doctrine-repo a2078a9b, 2026-07-22)
[example-doctrine-repo repo]. Purpose (unchanged from bash): read ``coordinator/hooks/hooks.json``
and emit/merge a settings.json ``hooks`` block where:

  - ONLY ``type=='command'`` entries WITH ``${CLAUDE_PLUGIN_ROOT}`` in their
    command string are emitted.
  - ``type=='mcp_tool'`` entries are SKIPPED (in-process coordinator_core
    ops, not settings.json rails).
  - ``type=='command'`` entries WITHOUT ``${CLAUDE_PLUGIN_ROOT}`` are SKIPPED
    (no bake needed, not plugin hooks).
  - every ``${CLAUDE_PLUGIN_ROOT}`` is rewritten to a zero-subprocess-spawn
    reference to
    :data:`coordinator_core.install._shared.COORDINATOR_CONTENT_ROOT_ENV_KEY`
    (via :func:`coordinator_core.install._shared.hook_root_env_expr`) — a
    plain env-var expansion evaluated at hook-FIRE time by whichever machine
    executes the command — NOT to a machine-absolute path baked at
    generation time, and NOT to a command-substitution (rejected: spawns a
    second process per hook fire, see below). ``generate()`` writes the
    actual value ONCE into settings.json's top-level ``env`` block
    (``env[COORDINATOR_CONTENT_ROOT] = coordinator_root``) rather than
    repeating it into all ~37 hook commands. **Changed 2026-07-28** (see
    negative-spec below): the original bash oracle and this port's own first
    Python translation baked a registry-resolved absolute path directly into
    every command; that is the defect a 2026-07-28 incident exposed — a
    POSIX host's settings.json silently overwritten with a Windows peer's
    baked ``X:/...`` paths by a cross-machine sync of the file, killing
    every coordinator hook on that host with no error surfaced anywhere. A
    same-day intermediate revision of this fix used a
    ``$(python3 .../resolve-coordinator-clone --content-root)`` command
    substitution instead — portable, but it doubles per-hook process spawns
    (a second `python3` per fire, across ~37 hooks), which is exactly the
    Windows spawn-tax cost class ``~/.claude/.coordinator-hooks-disabled``
    was armed for; REJECTED before landing, replaced by this env-var form.
    Baking SOME value is still unavoidable — CPR is unset for
    settings.json-registered hooks (bug #38699/#24529; see the Mechanism
    spec-backlink below) — but the value is now baked exactly ONCE, in one
    named place, not repeated per-command.
  - non-generated hooks already in settings.json (identity: path NOT under
    ``coordinator/hooks/``, checked against BOTH the current portable form
    and the legacy baked-absolute-path form so a pre-fix settings.json still
    strips/regenerates cleanly) are PRESERVED.
  - **Changed 2026-07-28 (exit-code hygiene):** every emitted command is
    additionally passed through
    :func:`coordinator_core.install._shared.wrap_hook_command_guarded`,
    which guards the resolved ``$COORDINATOR_CONTENT_ROOT``/
    ``$env:COORDINATOR_CONTENT_ROOT`` reference so an EMPTY or UNSET env
    var, or a script path that does not resolve on disk, exits **127**
    instead of falling through to the interpreter's own file-not-found exit
    (measured as **2** on this harness) — 2 is the PreToolUse hook
    contract's BLOCKING-DENY sentinel, so an unresolvable hook previously
    denied every tool call in lock-step with every OTHER hook wired the
    same way, including Bash/Write/Edit, bricking the tools needed to
    repair the settings.json that caused it (the incident this leg fixes).
    Identity classification (`_group_is_generated`/`_stray_check`, both via
    `_cmd_path`) understands the guarded shape alongside the legacy and
    bare-portable ones, so a regeneration over an already-guarded
    settings.json still classifies those groups as generated rather than
    "preserved" (which would silently duplicate every hook — see that
    function's own docstring).

Identity key: a hook group is "generated" iff at least one of its command
hooks has a resolved path starting with ``<coordinator_root>/hooks/``
(legacy) or with ``hook_root_env_expr(...) + "/hooks/"`` (current). All
other groups are preserved verbatim. This mirrors the identity key already
ported at :func:`coordinator_core.install._shared._group_is_generated` (the
uninstall inverse-strip leg's counterpart) — REUSED here, not re-derived, per
that module's own "single source of truth" framing (the bash siblings
gen-settings-hooks.sh / settings-hook-identity.sh were the same
single-source-of-truth pairing pre-port).

Requirements (unchanged from bash): deterministic, idempotent (byte-identical
output on re-run); ``--out`` param; fail-loud on generator business errors.

Port backlink: docs/plans/2026-07-16-clean-slate-residual-migration.md
    (BIG_PORT Wave B, item gen-settings-hooks).
Spec backlink: docs/plans/2026-07-04-doe-maximalist-execution-plugin-dir.md § M1
Mechanism: coordinator/docs/wiki/external-plugin-live-resolution.md
    § Hook-delivery — SOLVED via settings.json [example-doctrine-repo repo]

Double-fire refusal (added 2026-07-29, example-doctrine-repo dispatch
state/subagent-share/78b683cd-1b62-4a25-904d-954cb3c69412/
coordinatorexecutor-ba51c36f.md): ``hooks.json`` is the sole input to BOTH
delivery surfaces this generator can produce — everything this generator can
emit, plugin-side delivery already delivers when it is live. Before doing any
work, ``generate()`` now asks
:func:`coordinator_core.ops.session.guard_settings_integrity.
detect_hook_delivery_duplication` whether plugin-side delivery is VERIFIED
live and resolvable on THIS machine (hooks.json resolves via the canonical
content-root resolver AND every declared script path exists on disk) and, if
so, refuses to generate — every hook this generator would emit is already
being delivered, so emitting it too would fire it twice per event. This is a
POSITIVE-evidence-only refusal: absence of evidence (unresolvable content
root, missing hooks.json, an unresolved script) is NOT evidence of anything
and falls straight through to the pre-existing marker-gated generation path,
unchanged — getting this backwards (refusing on absence-of-evidence) would
kill the hook layer entirely on a machine that actually needs generation,
which is strictly worse than the double-fire this closes. The generator
never mutates settings.json on this path — it returns before
``_load_current_settings``/``_atomic_write_json`` are ever reached, so an
already-present generated ``hooks`` block is left byte-identical (report the
condition via the printed skip banner; the detector's own remediation is
where a stale block gets cleaned up, not this generator). Reuses
``detect_hook_delivery_duplication`` (the same detector
``guard_settings_integrity._is_healthy`` and the SessionStart double-fire
banner already use) rather than adding a second hook-path resolver — see
that function's own docstring for why comparison is by resolved script path,
not raw command text.

Negative-spec (faithful bash-oracle reproduction, NOT a fix):
  - The oracle's ``is_cpr_command`` ASSUMPTION (documented inline in the bash
    jq program, "F6") is preserved: ``${CLAUDE_PLUGIN_ROOT}`` need only
    appear ANYWHERE in the command string to count as CPR — a non-prefix
    occurrence still passes ``is_cpr_command`` here but would fail a
    ``cmd_path``-based startswith re-check on a later run, silently
    duplicating the hook. All hooks.json entries are expected to place
    ``${CLAUDE_PLUGIN_ROOT}`` immediately after the interpreter, exactly as
    the bash oracle assumed.
  - The bash oracle's jq dependency is REPLACED by the stdlib ``json``
    module (per porter-brief guidance: "In Python replace jq with json
    module but preserve the EXACT merge semantics byte-for-byte") — this is
    an implementation-substrate change, not a behavior change; the bash
    oracle's ``jq``-absent fail-loud path has no Python analogue (there is
    no longer a jq dependency to be absent), so that specific negative case
    does not carry forward.
  - **Flagged behavioral broadening (not a silent fix):** coordinator-root
    resolution (when ``--coordinator-root`` is not given) delegates to
    :func:`coordinator_core.install._shared.resolve_coordinator_root`,
    which is a strict SUPERSET of the bash oracle's own resolution order.
    The bash oracle only tried ``machine-local get repos.example_doctrine_repo`` then
    ``$REPO_EXAMPLE_DOCTRINE_REPO`` before failing loud; the shared helper additionally
    tries an explicit ``$COORDINATOR_ROOT`` env var first and a
    ``${CLAUDE_HOME:-$HOME}/.doe-root`` pointer file as a final fallback
    before failing loud. Every input that resolved under the bash oracle
    still resolves identically here (win-only — this can only turn a prior
    failure into a success, never the reverse); the shared helper's own
    docstring frames this as the deliberate DR-047-aligned consolidation
    ("the SAME seam the settings.json hook generator uses" — written when
    that helper was authored, anticipating this exact port) rather than a
    footgun to reproduce per-caller. Documented here per addendum rule 7
    ("flag, don't silently fix") since it IS a resolution-order difference
    from the literal bash source, even though it is behavior-compatible.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.install._shared import (
    COORDINATOR_CONTENT_ROOT_ENV_KEY,
    COORDINATOR_PYTHON_BIN_ENV_KEY,
    _cmd_path,
    _group_is_generated,
    hook_root_env_expr,
    resolve_coordinator_root,
    wrap_hook_command_guarded,
)
from coordinator_core.install.substrate import resolve_hook_python_bin
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.ops.session.guard_settings_integrity import (
    detect_hook_delivery_duplication,
)

# ---------------------------------------------------------------------------
# helpers — CPR (${CLAUDE_PLUGIN_ROOT}) filter/rewrite
# ---------------------------------------------------------------------------

_CPR = "${CLAUDE_PLUGIN_ROOT}"

_SETTINGS_DIR_NAME = ".claude"
"""The directory segment under `$HOME` this generator's output (and its two
consent markers) live in — extracted so `resolve_settings_out_path`,
`_kill_switch_marker`/`_positive_marker_path`, and `WRITE_SURFACE` read one
spelling rather than three independently-typed literals."""

_SETTINGS_FILE_NAME = "settings.json"
"""The generated-into file's leaf name, read by `resolve_settings_out_path`
and `WRITE_SURFACE` alike."""

_POSITIVE_MARKER_NAME = ".coordinator-hooks-enabled"
"""Leaf name of the positive per-machine consent marker this generator
creates (`_create_positive_marker`) — extracted so `_positive_marker_path`
and `WRITE_SURFACE` agree on one spelling."""

# A resolved coordinator_root never legitimately contains these — a Windows
# drive letter surviving into an emitted command means the portability
# rewrite below was bypassed (e.g. a future edit re-introducing a baked
# absolute path). Checked by `_assert_portable_command` as a structural
# backstop, not merely documentation.
_DRIVE_LETTER_RE = re.compile(r"[A-Za-z]:[\\/]")


_HOOKS_MERGE_CLAUSE_INDEX = 0
"""Index of `WRITE_SURFACE`'s sole SHAPED clause (the `hooks.<event>`
structured-file-key merge) — the only clause `generate()` journals against;
the other three clauses are `StaticClause`s and need no resolution."""


def _record_resolution(clause_index: int, entries) -> None:
    """Deferred-import wrapper over `resolution_journal.record_resolution`
    — see `clone_sibling_repo._record_resolution`'s docstring for why a
    module-level import of `resolution_journal` is not used here (this
    module is transitively reachable from `coordinator_core.ops`'s eager
    op-registration walk via its own downstream import graph, and this
    module already imports `substrate` at module level)."""
    from coordinator_core.install import resolution_journal

    resolution_journal.record_resolution("gen-settings-hooks", clause_index, entries)


class GenSettingsHooksError(RuntimeError):
    """Fail-loud generator business error — CLI entry converts to exit 1.
    Mirrors the bash ``die()`` helper's single undifferentiated exit-1
    contract (all business errors — bad arg, missing coordinator root,
    missing hooks.json, stray hook detected — share rc=1 in the oracle)."""


def _is_cpr_command(hook: Dict[str, Any]) -> bool:
    """Mirrors the jq ``is_cpr_command`` def: a command hook whose command
    string contains the literal ``${CLAUDE_PLUGIN_ROOT}`` substring anywhere
    (see module negative-spec re: non-prefix-position hazard, faithfully
    reproduced, not fixed)."""
    return hook.get("type") == "command" and _CPR in hook.get("command", "")


def _rewrite_cpr(command: str, coordinator_root: str) -> str:
    """Rewrite every ``${CLAUDE_PLUGIN_ROOT}`` occurrence to this machine's
    :func:`~coordinator_core.install._shared.hook_root_env_expr` — a
    zero-subprocess-spawn env-var reference evaluated at hook-FIRE time —
    rather than to a machine-absolute path baked at GENERATION time, and
    rather than to a command-substitution (which would spawn a second
    process on every hook fire; see the module-level rejection note above
    `_shared.COORDINATOR_CONTENT_ROOT_ENV_KEY`).

    ``coordinator_root`` is accepted (not used in the substitution itself —
    the actual VALUE is written once into the settings.json ``env`` block by
    :func:`generate`, not repeated per-command) only so this stays a
    drop-in replacement for the prior baked-path signature; the value still
    matters elsewhere in this module (locating THIS machine's own
    ``hooks.json`` to read, and being the value written to ``env``)."""
    return command.replace(_CPR, hook_root_env_expr(windows=(os.name == "nt")))


def _finalize_command(
    raw_command: str, coordinator_root: str, *, event: str, python_bin_resolved: bool = False
) -> str:
    """CPR-rewrite, portability-assert, then exit-code-hygiene-guard a raw
    ``hooks.json`` command — the ONE function both `_build_new_generated`
    (what gets emitted) and `_build_will_emit_set` (what the stray-check
    derives its identity set from) call, so the two paths cannot drift and
    produce different final strings for the same input within a SINGLE
    `generate()` call — see `_stray_check`'s own docstring for why identity
    across DIFFERENT calls is instead compared by `_cmd_path`, not text
    equality.

    ``python_bin_resolved`` is resolved ONCE per `generate()` run (never
    per-command — see `wrap_hook_command_guarded`'s docstring) and threaded
    through unchanged to both call sites, so they stay in lockstep."""
    rewritten = _rewrite_cpr(raw_command, coordinator_root)
    _assert_portable_command(rewritten, event=event)
    guarded = wrap_hook_command_guarded(
        rewritten, windows=(os.name == "nt"), python_bin_resolved=python_bin_resolved
    )
    # Belt-and-suspenders: cannot fire given `wrap_hook_command_guarded`'s
    # current implementation (it only quotes/wraps an already-validated
    # `rewritten` string, so it cannot reintroduce a residual
    # `${CLAUDE_PLUGIN_ROOT}` token or a drive-letter path) — kept as
    # defense-in-depth against a future change to that function, not because
    # this path is reachable today (code-reviewer F3, 2026-07-28).
    _assert_portable_command(guarded, event=event)
    return guarded


def _assert_portable_command(command: str, *, event: str) -> None:
    """Fail loud rather than silently emit a bad path: raise if an emitted
    hook command still carries a residual ``${CLAUDE_PLUGIN_ROOT}`` token (a
    rewrite that didn't fire) or a Windows drive-letter / otherwise
    non-portable absolute-path shape. Never falls back to baking an
    absolute path — there is no fallback branch here at all, by design;
    a command that fails this check is a generator bug, not a degraded
    output to ship anyway."""
    if _CPR in command:
        raise GenSettingsHooksError(
            f"gen-settings-hooks: internal error — unrewritten {_CPR} survived "
            f"into an emitted command (event={event}): {command!r}"
        )
    if _DRIVE_LETTER_RE.search(command):
        raise GenSettingsHooksError(
            "gen-settings-hooks: refusing to emit a hook command containing a "
            f"Windows drive-letter absolute path (event={event}): {command!r}\n"
            "  Generated commands must be machine-portable — see "
            "hook_root_env_expr() in coordinator_core.install._shared."
        )


# ---------------------------------------------------------------------------
# unit 1 — arg parse, kill-switch, hooks.json locate, stray-check
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen-settings-hooks",
        description="Generate settings.json hooks block from coordinator hooks.json",
        add_help=False,
    )
    parser.add_argument("--out", dest="out_path", default=None)
    parser.add_argument("--hooks-json", dest="hooks_json_override", default=None)
    parser.add_argument("--coordinator-root", dest="coordinator_root_override", default=None)
    parser.add_argument(
        "--check-only", dest="check_only", action="store_true",
        help="Report what would be seeded; mutate nothing.",
    )
    parser.add_argument("-h", "--help", action="store_true", dest="help")
    return parser


def _usage_text() -> str:
    return (
        "Usage: gen-settings-hooks [OPTIONS]\n"
        "\n"
        "Options:\n"
        "  --out <path>              Output path (default: ~/.claude/settings.json)\n"
        "  --hooks-json <path>       Override hooks.json path (for tests)\n"
        "  --coordinator-root <path> Override coordinator root (for tests; bypasses registry resolution)\n"
        "  --check-only              Report what would be seeded; mutate nothing.\n"
        "  -h, --help                Show this help\n"
        "\n"
        "Environment:\n"
        "  REPO_EXAMPLE_DOCTRINE_REPO           Fallback if machine-local get repos.example_doctrine_repo fails\n"
        "\n"
        "Exit codes:\n"
        "  0  success (including operator-kill-switch no-op)\n"
        "  1  generator business error (bad arg, missing coordinator root,\n"
        "     missing hooks.json, stray hand-authored hook detected,\n"
        "     malformed JSON in hooks.json or an existing settings.json)\n"
    )


def resolve_settings_out_path(out_path: Optional[str] = None) -> str:
    """Same resolution `generate()` applies to its own ``out_path`` param —
    exposed publicly so a caller (e.g. the maximalist orchestrator) can
    report the operator kill-switch marker path WITHOUT re-deriving this
    resolution itself and risking drift from `generate()`'s own logic.

    ``os.environ.get("HOME", default)`` only applies ``default`` when HOME is
    ABSENT — an exported-empty ``HOME=""`` still wins and yields a relative
    ``.claude/settings.json`` anchored at cwd (review: code-reviewer P3,
    2026-07-28). ``or`` treats empty-string the same as unset, matching
    ``Path.home()``'s own USERPROFILE-then-expanduser resolution on Windows."""
    return out_path or str(
        Path(os.environ.get("HOME") or Path.home()) / _SETTINGS_DIR_NAME / _SETTINGS_FILE_NAME
    )


def kill_switch_marker_path(out_path: Optional[str] = None) -> Path:
    """Public wrapper over :func:`_kill_switch_marker` for callers that need
    to report the marker's path (not just whether it fired) — e.g. an
    installer orchestrator surfacing "delete this file to re-enable" to the
    operator without duplicating the kill-switch's own naming convention."""
    return _kill_switch_marker(resolve_settings_out_path(out_path))


def _kill_switch_marker(out_path: str) -> Path:
    return Path(os.path.dirname(out_path) or ".") / ".coordinator-hooks-disabled"


def positive_marker_path(out_path: Optional[str] = None) -> Path:
    """Public wrapper over :func:`_positive_marker_path`, mirroring
    :func:`kill_switch_marker_path` — intended for a caller (installer, the
    post-merge/post-checkout resync gate) that needs to report/probe the
    POSITIVE per-machine consent marker's path without re-deriving its
    naming convention. No caller uses this wrapper yet (the resync gate
    currently gets its marker path back from `ensure_positive_marker`'s own
    return value instead) — this is forward-looking infrastructure, not a
    presently-wired helper."""
    return _positive_marker_path(resolve_settings_out_path(out_path))


def _positive_marker_path(out_path: str) -> Path:
    """The positive, per-machine, gitignored consent marker — its PRESENCE
    (not absence) means "this machine has opted into coordinator hook
    generation." See the module docstring's 2026-07-28 polarity-inversion
    history for why absence, not presence, must be the fail-safe default:
    the negative marker (`_kill_switch_marker`) was fail-OPEN (absence ==
    "generate") — untracking it on one machine and syncing that deletion to
    a peer silently RE-ENABLED generation there. This marker inverts that:
    absence means "do nothing," on any machine, including one that has
    never seen this code before. The negative marker (checked first, in
    `generate()`) still wins when present — two independent ways to be OFF,
    only one way to be ON."""
    return Path(os.path.dirname(out_path) or ".") / _POSITIVE_MARKER_NAME


def _has_local_generation_evidence(current_settings: Dict[str, Any]) -> bool:
    """True iff `current_settings` carries first-party evidence that THIS
    machine already generated hooks before the positive marker existed —
    the migration discriminator for an already-consenting machine (see
    `_ensure_positive_marker` and the module docstring).

    Deliberately keyed on `env[COORDINATOR_CONTENT_ROOT_ENV_KEY]`, not on
    the presence of a `hooks` block: that env key is written ONLY by this
    generator's own `_merge_env` (never by hand, never by a template), and
    `settings.json` itself is untracked/gitignored on this fleet's dev
    topology (see `_shared.py`'s own docstring on why baked hook paths and a
    synced settings.json don't mix) — so this value can only ever reflect
    THIS machine's own prior local runs, never something that arrived over
    git from a peer. That is the "cannot be spoofed over git" property the
    migration discriminator is required to have."""
    env = current_settings.get("env")
    if not isinstance(env, dict):
        return False
    return bool(env.get(COORDINATOR_CONTENT_ROOT_ENV_KEY))


def _create_positive_marker(marker: Path, *, reason: str) -> None:
    """Create the positive consent marker. Content is a human-breadcrumb
    only (never parsed) — existence, not content, is the signal."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        f"# coordinator hook generation is ENABLED on this machine.\n"
        f"# Created: {reason}\n"
        f"# Delete this file to disable generation (or create "
        f"{marker.parent / '.coordinator-hooks-disabled'} for the same effect).\n",
        encoding="utf-8",
    )


def ensure_positive_marker(
    out_path: Optional[str] = None,
) -> Tuple[Path, bool, bool]:
    """Idempotent migration/probe entry point, reusable by both `generate()`
    itself and any OTHER caller that needs the same decision (e.g. the
    post-merge/post-checkout marker-resync gate, so the two never drift on
    what "should this machine be enabled" means).

    Returns ``(marker_path, is_enabled, migrated)``:
      - ``is_enabled`` — True if the marker exists (already, or just-now via
        migration). False means "leave generation off."
      - ``migrated`` — True iff THIS call just created the marker via the
        local-evidence migration path (never true for a pre-existing marker
        or a pre-existing absence with no evidence).

    Never mutates when the marker already exists (read-only probe in that
    case) and never mutates on a from-scratch machine with no local
    evidence — see `_has_local_generation_evidence` for what counts as
    evidence and why it cannot arrive over git."""
    resolved_out = resolve_settings_out_path(out_path)
    marker = _positive_marker_path(resolved_out)
    if marker.is_file():
        return marker, True, False

    try:
        current_settings = _load_current_settings(resolved_out)
    except GenSettingsHooksError:
        # Malformed settings.json on the not-yet-enabled path degrades to
        # "not enabled" rather than raising — matches the pre-2026-07-28
        # ordering, where `_load_current_settings` ran AFTER coordinator-root
        # resolution and a clone-absent machine never reached this parse at
        # all (soft "skipped (clone absent)"). This call now runs BEFORE
        # that resolution (`ensure_positive_marker` must decide enablement
        # before `generate()` even attempts to resolve a coordinator root),
        # so a first-run machine with both a malformed settings.json AND an
        # unresolvable clone must still get the graceful skip it got before,
        # not a hard failure on a file this path was never trying to parse
        # for its own sake — see review finding F3 (2026-07-28 s2 review).
        return marker, False, False

    if _has_local_generation_evidence(current_settings):
        _create_positive_marker(
            marker,
            reason=(
                f"migrated — {resolved_out} already carried "
                f"env.{COORDINATOR_CONTENT_ROOT_ENV_KEY} from a prior run on this machine."
            ),
        )
        return marker, True, True

    return marker, False, False


def _load_current_settings(out_path: str) -> Dict[str, Any]:
    if os.path.isfile(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError as exc:
                raise GenSettingsHooksError(
                    f"malformed JSON in existing settings file: {out_path}\n  {exc}"
                ) from exc
    return {}


def _build_will_emit_set(
    hooks_json: Dict[str, Any], coordinator_root: str, python_bin_resolved: bool = False
) -> set:
    """Build the set of commands (already CPR-rewritten) this generator WILL
    emit — used by the stray-check to detect a hand-authored hook living
    under the generator-owned dir that would be silently clobbered on
    regeneration."""
    will_emit: set = set()
    for event, groups in (hooks_json.get("hooks") or {}).items():
        for group in groups:
            for hook in group.get("hooks", []) or []:
                if _is_cpr_command(hook):
                    will_emit.add(
                        _finalize_command(
                            hook["command"],
                            coordinator_root,
                            event=event,
                            python_bin_resolved=python_bin_resolved,
                        )
                    )
    return will_emit


def _stray_check(
    current_settings: Dict[str, Any],
    hooks_json: Dict[str, Any],
    coordinator_root: str,
    generated_hooks_dir: str,
    python_bin_resolved: bool = False,
) -> List[Tuple[str, str]]:
    """Find any existing hook whose ``cmd_path`` is under
    ``generated_hooks_dir`` but is NOT among the scripts this run WILL emit —
    such a hook would be silently overwritten on the next regeneration (the
    group containing it is classified "generated" by ``_group_is_generated``
    and its ENTIRE group gets replaced). Returns a list of ``(event,
    command)`` stray pairs — empty means clean.

    Compares by `_cmd_path` IDENTITY, not raw-command text equality. Command
    TEXT varies across emission eras (bare-portable vs the plan-C2
    resolved-interpreter guarded shape, per `_cmd_path`'s own docstring) —
    the same interpreter-resolution flag threaded through `generate()` can
    differ between the run that WROTE an existing hook and the run doing
    THIS check (e.g. a venv rebuild regresses `resolve_hook_python_bin()`
    from a resolved path back to ``""``). A text-equality comparison would
    then flag every one of this generator's own prior-era hooks as a
    hand-authored stray and abort `generate()` on its own repair path (the
    exact scenario `[ -x ... ]` self-healing exists for — see
    `wrap_hook_command_guarded`'s F7 framing). `_finalize_command` still
    guarantees `_build_new_generated` and `_build_will_emit_set` never
    diverge for a SINGLE call's `python_bin_resolved` value; what changed is
    that two different calls (across regenerations) no longer need to agree
    on that value for their outputs to identify as "the same generated
    script" here."""
    will_emit = _build_will_emit_set(hooks_json, coordinator_root, python_bin_resolved)
    will_emit_paths = {_cmd_path(cmd) for cmd in will_emit}
    legacy_prefix = generated_hooks_dir.rstrip("/") + "/"
    portable_prefix = hook_root_env_expr(windows=(os.name == "nt")) + "/hooks/"
    strays: List[Tuple[str, str]] = []
    for event, groups in (current_settings.get("hooks") or {}).items():
        for group in groups:
            for hook in group.get("hooks", []) or []:
                if hook.get("type") != "command":
                    continue
                command = hook.get("command", "")
                cmd_path = _cmd_path(command)
                if not (cmd_path.startswith(legacy_prefix) or cmd_path.startswith(portable_prefix)):
                    continue
                if cmd_path not in will_emit_paths:
                    strays.append((event, command))
    return strays


# ---------------------------------------------------------------------------
# unit 2 — build new-generated, extract preserved, merge, atomic write
# ---------------------------------------------------------------------------


def _build_new_generated(
    hooks_json: Dict[str, Any], coordinator_root: str, python_bin_resolved: bool = False
) -> Dict[str, List[Dict[str, Any]]]:
    """For each event/group in hooks.json: filter hooks to CPR commands only,
    rewrite their ``.command``, drop the group if it filters to empty, strip
    ``_comment`` (not meaningful in settings.json), forward all other
    group-level fields (matcher, etc.) and all other per-hook fields
    (timeout, async, ...) untouched."""
    new_generated: Dict[str, List[Dict[str, Any]]] = {}
    for event, groups in (hooks_json.get("hooks") or {}).items():
        emitted_groups: List[Dict[str, Any]] = []
        for group in groups:
            filtered_hooks = []
            for hook in group.get("hooks", []) or []:
                if _is_cpr_command(hook):
                    new_hook = dict(hook)
                    new_hook["command"] = _finalize_command(
                        hook["command"],
                        coordinator_root,
                        event=event,
                        python_bin_resolved=python_bin_resolved,
                    )
                    filtered_hooks.append(new_hook)
            if not filtered_hooks:
                continue
            new_group = {k: v for k, v in group.items() if k != "_comment"}
            new_group["hooks"] = filtered_hooks
            emitted_groups.append(new_group)
        if emitted_groups:
            new_generated[event] = emitted_groups
    return new_generated


def _extract_preserved(current_settings: Dict[str, Any], generated_hooks_dir: str) -> Dict[str, List[Dict[str, Any]]]:
    """Preserved: groups where NO command hook has a path under
    ``<coordinator_root>/hooks/``."""
    preserved: Dict[str, List[Dict[str, Any]]] = {}
    for event, groups in (current_settings.get("hooks") or {}).items():
        kept = [g for g in groups if not _group_is_generated(g, generated_hooks_dir)]
        if kept:
            preserved[event] = kept
    return preserved


def _merge_hooks(
    preserved: Dict[str, List[Dict[str, Any]]],
    new_generated: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Merge: preserved_groups + new_generated_groups per event, events
    sorted alphabetically for deterministic/idempotent output."""
    all_events = sorted(set(preserved.keys()) | set(new_generated.keys()))
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for event in all_events:
        combined = list(preserved.get(event, [])) + list(new_generated.get(event, []))
        if combined:
            merged[event] = combined
    return merged


def _merge_env(
    current_settings: Dict[str, Any], coordinator_root: str, python_bin: str = ""
) -> Dict[str, Any]:
    """Set/overwrite ``env[COORDINATOR_CONTENT_ROOT]`` to ``coordinator_root``
    — the ONE place this machine's coordinator location gets baked, instead
    of into every generated hook command (see the module docstring's
    2026-07-28 history). Every OTHER existing ``env`` key (e.g. an
    operator-set ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS``) is preserved
    untouched — this key is generator-owned, the rest is not.

    ``python_bin`` (plan C2) is
    ``coordinator_core.install.substrate.resolve_hook_python_bin()``'s
    result, resolved once by the caller. Written to
    ``env[COORDINATOR_PYTHON_BIN_ENV_KEY]`` only when non-empty — an empty
    string (nothing resolved, AC3's degradation path) writes NO key at all,
    never an empty-string value, so this env block and
    `wrap_hook_command_guarded`'s bare-token fallback agree on what
    "unresolved" looks like. Symmetrically, this key is fully generator-owned
    (like ``COORDINATOR_CONTENT_ROOT``): when ``python_bin`` is empty this run,
    any stale value from a PRIOR successful run is popped, not left behind —
    the env block always reflects this run's own resolution outcome, never a
    prior one (review: code-reviewer F2, 2026-08-03)."""
    env = dict(current_settings.get("env") or {})
    env[COORDINATOR_CONTENT_ROOT_ENV_KEY] = coordinator_root
    if python_bin:
        env[COORDINATOR_PYTHON_BIN_ENV_KEY] = python_bin
    else:
        env.pop(COORDINATOR_PYTHON_BIN_ENV_KEY, None)
    return env


def _atomic_write_json(target: str, data: Dict[str, Any]) -> None:
    """Atomic write via tempfile-in-same-dir + os.replace. Preserves the
    target's prior permission bits on the replacement file (addendum A5) —
    ``os.replace`` does not carry mode bits forward from a freshly
    ``mkstemp``-ed file, so an existing settings.json's permissions
    (operator-set, e.g. 0600) must be explicitly re-applied rather than
    silently reset to the tempfile default."""
    out_dir = os.path.dirname(target) or "."
    os.makedirs(out_dir, exist_ok=True)
    prior_mode = None
    if os.path.isfile(target):
        prior_mode = os.stat(target).st_mode
    fd, tmp_name = tempfile.mkstemp(prefix=".gen-settings-hooks.", dir=out_dir)
    try:
        # newline="" disables universal-newline translation — the emitted
        # settings.json is a byte-contract; without this, Windows text mode
        # silently rewrites every embedded "\n" (from json.dump(indent=2))
        # to "\r\n", breaking byte-identity across platforms/re-runs.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        if prior_mode is not None:
            os.chmod(tmp_name, prior_mode)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass  # best-effort cleanup on an already-failing path; original exception re-raises below
        raise


def generate(
    out_path: Optional[str] = None,
    hooks_json_override: Optional[str] = None,
    coordinator_root_override: Optional[str] = None,
    check_only: bool = False,
) -> str:
    """Core generator entry — raises :class:`GenSettingsHooksError` on any
    business failure (never partially writes ``out_path`` on error). Kill
    switch and success both return normally (no exception).

    Returns one of the install.md contract's status tokens (§ 3.5c):
    ``"seeded"``, ``"skipped (check-only)"``, ``"skipped (clone absent)"``,
    ``"skipped (disabled by operator marker)"`` (negative kill-switch — an
    additive fifth token beyond the four documented in install.md, since
    the kill-switch case was previously silent-return-only), or
    ``"skipped (no positive marker)"`` (2026-07-28 polarity inversion — the
    NEW default-off case, distinguishable in logs from the operator
    kill-switch case above: that one means "an operator explicitly turned
    generation off"; this one means "no one has ever turned it on"), or
    ``"skipped (plugin delivery already live)"`` (2026-07-29 double-fire
    refusal — plugin-side delivery is verified live+resolvable on this
    machine, so generating would double-fire every hook; see the module
    docstring's "Double-fire refusal" section). See `ensure_positive_marker`
    for the migration path that lets an already-consenting machine convert
    without a flag day.

    ``"skipped (clone absent)"`` is a SOFT skip (no exception, generate()
    returns normally, exit 0) — this mirrors the retired bash trampoline's
    own ``else`` branch ("example-doctrine-repo clone not resolved — complete step 3.5a
    first"), which never set a non-zero rc either. It applies ONLY when NO
    explicit ``coordinator_root_override`` was given and the registry/env/
    pointer-file resolution chain (``resolve_coordinator_root()``) itself
    comes up empty — i.e. the moral equivalent of the unset ``DOE_CLONE``
    env var. An explicit ``coordinator_root_override`` that fails its
    ``isdir`` check remains a HARD failure (raises ``GenSettingsHooksError``,
    unchanged from pre-existing behavior) — the caller asked for a specific
    path and got one that doesn't exist; that is a business error, not an
    absent-clone soft skip."""
    resolved_out = resolve_settings_out_path(out_path)

    marker = _kill_switch_marker(resolved_out)
    if marker.is_file():
        print(
            f"gen-settings-hooks: DISABLED by operator marker ({marker}) — leaving settings.json untouched.",
            file=sys.stderr,
        )
        print("  Delete that file to re-enable coordinator hook generation.", file=sys.stderr)
        # Operator kill-switch refused this run's generation outright — a
        # genuine "resolved to nothing" fact, not "we never got there".
        _record_resolution(_HOOKS_MERGE_CLAUSE_INDEX, ())
        return "skipped (disabled by operator marker)"

    if check_only:
        # install.md contract: check-only never resolves or mutates —
        # matches the retired bash trampoline's own check-only branch,
        # which short-circuited before even checking DOE_CLONE. Also never
        # touches the positive marker (`ensure_positive_marker` is not
        # called on this path) — check-only must not create/mutate ANY
        # marker, per the 2026-07-28 polarity-inversion requirements.
        return "skipped (check-only)"

    # Double-fire refusal (see module docstring): only skip on POSITIVE
    # evidence that plugin-side delivery is already live and fully
    # resolvable -- never on absence of evidence. Checked ahead of the
    # marker-gated path below because it is a stronger, orthogonal signal:
    # if plugin delivery already covers every hook this generator would
    # emit, generating is wrong regardless of whether this machine has
    # opted into the (legacy) marker-gated generation path at all. Never
    # touches settings.json on this branch -- returns before
    # `_load_current_settings`/`_atomic_write_json` are reached.
    delivery_report = detect_hook_delivery_duplication(
        config_dir=Path(os.path.dirname(resolved_out) or ".")
    )
    if delivery_report.plugin_present and delivery_report.plugin_resolvable:
        print(
            "gen-settings-hooks: SKIPPING generation -- plugin-side hook delivery "
            "is already live and fully resolvable on this machine "
            "(coordinator/hooks/hooks.json resolves via the canonical content-root "
            "resolver and every declared script path exists on disk). This "
            "generator's ONLY input is that same hooks.json, so every hook it "
            "could emit is already being delivered by the plugin path -- "
            "generating on top would fire every hook TWICE per event. This is "
            "deliberate, not a bug: settings.json is left untouched.",
            file=sys.stderr,
        )
        # Positive-evidence refusal: this run deliberately generates
        # nothing because plugin-side delivery already covers it — a
        # genuine "resolved to nothing" fact for this clause this run.
        _record_resolution(_HOOKS_MERGE_CLAUSE_INDEX, ())
        return "skipped (plugin delivery already live)"

    positive_marker, is_enabled, migrated = ensure_positive_marker(resolved_out)
    if not is_enabled:
        print(
            f"gen-settings-hooks: no positive marker ({positive_marker}) and no local "
            "evidence this machine has generated before — leaving settings.json untouched.",
            file=sys.stderr,
        )
        print(
            "  Create that file (empty is fine) to enable coordinator hook generation on this machine.",
            file=sys.stderr,
        )
        # This machine has never consented to generation — resolved to
        # nothing for this run, same reasoning as the kill-switch branch.
        _record_resolution(_HOOKS_MERGE_CLAUSE_INDEX, ())
        return "skipped (no positive marker)"
    if migrated:
        print(
            f"gen-settings-hooks: migrated — created positive marker {positive_marker} "
            "from local evidence of prior generation on this machine.",
            file=sys.stderr,
        )

    if coordinator_root_override:
        coordinator_root = coordinator_root_override.rstrip("/")
    else:
        try:
            coordinator_root = resolve_coordinator_root()
        except RuntimeError:
            # Discovery (the coordinator-root resolver) came up empty —
            # resolved to nothing this run.
            _record_resolution(_HOOKS_MERGE_CLAUSE_INDEX, ())
            return "skipped (clone absent)"

    # Windows portability: normalise drive-letter backslash to forward slash
    # (mirrors bash's belt-and-suspenders normalisation — see bash comment
    # block on the equivalent line; canonical fix lives at the
    # machine-local cmd_get emission point, this is defense-in-depth).
    coordinator_root = coordinator_root.replace("\\", "/")

    if not os.path.isdir(coordinator_root):
        raise GenSettingsHooksError(
            f"Coordinator root does not exist: {coordinator_root}\n"
            "Remediation: ensure the example-doctrine-repo repo is cloned and has a coordinator/ subdirectory.\n"
            f"  Expected: {coordinator_root}"
        )

    hooks_json_path = hooks_json_override or f"{coordinator_root}/hooks/hooks.json"
    if not os.path.isfile(hooks_json_path):
        raise GenSettingsHooksError(f"hooks.json not found: {hooks_json_path}")

    with open(hooks_json_path, "r", encoding="utf-8") as f:
        try:
            hooks_json = json.load(f)
        except json.JSONDecodeError as exc:
            raise GenSettingsHooksError(
                f"malformed JSON in hooks.json: {hooks_json_path}\n  {exc}"
            ) from exc

    # Windows portability: build with an explicit forward-slash join, not
    # os.path.join — os.path.join uses os.sep (backslash on Windows)
    # regardless of the separator style already present in `coordinator_root`
    # (already forward-slash-normalised above), which would silently produce
    # a mixed-separator prefix that never startswith()-matches the
    # all-forward-slash command paths emitted by `_rewrite_cpr` (both the
    # stray-check and `_group_is_generated` compare against this prefix).
    generated_hooks_dir = f"{coordinator_root}/hooks"

    current_settings = _load_current_settings(resolved_out)

    # Resolved ONCE per run (plan C2) — never per-command — and the same
    # value threaded into `_stray_check` (via `_build_will_emit_set`),
    # `_build_new_generated`, and `_merge_env`, so all three agree on
    # whether an interpreter was resolved for this generation pass.
    python_bin = resolve_hook_python_bin()
    python_bin_resolved = bool(python_bin)

    strays = _stray_check(
        current_settings, hooks_json, coordinator_root, generated_hooks_dir, python_bin_resolved
    )
    if strays:
        lines = [
            "Hand-authored hook detected under the generator-owned coordinator/hooks/ dir.",
            "This hook is not in hooks.json and would be silently OVERWRITTEN on regeneration:",
        ]
        for event, command in strays:
            lines.append(f"  event={event} command={command}")
        lines.append("")
        lines.append(f"Remediation: This hook lives under the generator-owned {generated_hooks_dir}/ dir.")
        lines.append(
            "             Move it elsewhere (e.g. <settings-home>/bin/ or ~/.claude/bin/ during"
        )
        lines.append("             the compat window) and update the command path,")
        lines.append("             or add it to hooks.json so the generator manages it.")
        raise GenSettingsHooksError("\n".join(lines))

    new_generated = _build_new_generated(hooks_json, coordinator_root, python_bin_resolved)
    preserved = _extract_preserved(current_settings, generated_hooks_dir)
    merged_hooks = _merge_hooks(preserved, new_generated)

    final_settings = dict(current_settings)
    final_settings["hooks"] = merged_hooks
    final_settings["env"] = _merge_env(current_settings, coordinator_root, python_bin)

    _atomic_write_json(resolved_out, final_settings)
    print(f"gen-settings-hooks: hooks block written to {resolved_out}", file=sys.stderr)

    # Journal the concrete `hooks.<event>` keys THIS run actually merged in
    # via `_merge_hooks` (`new_generated`'s own event keys) — not every key
    # in `merged_hooks`, which also includes preserved (non-generator-owned)
    # groups this run left untouched.
    resolved_entries = tuple(
        WriteSurfaceEntry(
            kind="structured-file-key",
            key=f"hooks.{event}",
            path=resolved_out,
            reason="_build_new_generated groups merged in; preserved (non-generator) groups left untouched",
        )
        for event in sorted(new_generated)
    )
    _record_resolution(_HOOKS_MERGE_CLAUSE_INDEX, resolved_entries)

    return "seeded"


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="gen-settings-hooks",
    source_module="coordinator_core.install.gen_settings_hooks",
    clauses=(
        # Clause 1 — `_merge_hooks`/`generate`: the `hooks` top-level key of
        # an existing settings.json is REPLACED with the merge of preserved
        # (non-generator-owned) groups plus this run's newly generated
        # groups. SHAPED: the set of `hooks.<event>` keys touched is
        # whatever `hooks.json` declares, not enumerable in source — a
        # structured-file-key merge (every other top-level key untouched),
        # never a whole-file overwrite.
        ShapedClause(
            discovered_by="_merge_hooks (per-event group merge, keyed by hooks.json's own event names)",
            entry_template=WriteSurfaceEntry(
                kind="structured-file-key",
                key="hooks.<event>",
                path=f"<home>/{_SETTINGS_DIR_NAME}/{_SETTINGS_FILE_NAME}",
                reason="_build_new_generated groups merged in; preserved (non-generator) groups left untouched",
            ),
        ),
        # Clause 2 — `_merge_env`: `env[COORDINATOR_CONTENT_ROOT_ENV_KEY]` is
        # set/overwritten every successful run, the ONE place this
        # machine's coordinator location is baked (see module docstring's
        # 2026-07-28 history).
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="structured-file-key",
                    key=COORDINATOR_CONTENT_ROOT_ENV_KEY,
                    path=f"<home>/{_SETTINGS_DIR_NAME}/{_SETTINGS_FILE_NAME}",
                    reason="_merge_env sets env[COORDINATOR_CONTENT_ROOT] every run",
                ),
            ),
        ),
        # Clause 3 — `_merge_env`: `env[COORDINATOR_PYTHON_BIN_ENV_KEY]` is
        # written when `resolve_hook_python_bin()` resolves a value this
        # run, and POPPED (an explicit delete, not left stale) when it does
        # not — see `_merge_env`'s own docstring (code-reviewer F2,
        # 2026-08-03).
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="structured-file-key",
                    key=COORDINATOR_PYTHON_BIN_ENV_KEY,
                    path=f"<home>/{_SETTINGS_DIR_NAME}/{_SETTINGS_FILE_NAME}",
                    reason="_merge_env writes env[COORDINATOR_PYTHON_BIN] when resolve_hook_python_bin() succeeds",
                ),
                WriteSurfaceEntry(
                    kind="structured-file-key",
                    key=COORDINATOR_PYTHON_BIN_ENV_KEY,
                    path=f"<home>/{_SETTINGS_DIR_NAME}/{_SETTINGS_FILE_NAME}",
                    effect="delete",
                    reason="_merge_env pops env[COORDINATOR_PYTHON_BIN] when this run resolves no interpreter",
                ),
            ),
        ),
        # Clause 4 — `_create_positive_marker` (via `ensure_positive_marker`'s
        # migration path): the positive per-machine consent-marker file, a
        # human-breadcrumb-only file-path write, next to settings.json.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<home>/{_SETTINGS_DIR_NAME}/{_POSITIVE_MARKER_NAME}",
                    reason="_create_positive_marker seeds the positive consent marker on migration",
                ),
            ),
        ),
    ),
)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit:
        return 1
    if args.help:
        print(_usage_text(), file=sys.stderr)
        return 0
    if unknown:
        print(f"ERROR: Unknown argument: {unknown[0]}", file=sys.stderr)
        print("Run with --help for usage.", file=sys.stderr)
        return 1

    try:
        status = generate(
            out_path=args.out_path,
            hooks_json_override=args.hooks_json_override,
            coordinator_root_override=args.coordinator_root_override,
            check_only=args.check_only,
        )
    except GenSettingsHooksError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("settings_hooks_seed: failed")
        return 1
    print(f"settings_hooks_seed: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
