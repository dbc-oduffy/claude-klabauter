"""
coordinator_core.ops.doctor — the WS-9 out-of-harness health/repair command.

WHY THIS EXISTS
    Five occurrences of the same incident class in two days (2026-07-28/29): a
    hook registration or a synced config file goes bad, and the tools needed to
    repair it (Write, Edit, Bash) are exactly the tools the break disables. A
    guard that runs through the tool it guards can detect a break but never
    repair it — see `docs/plans/2026-07-29-windows-viability-stop-the-spawn-
    storms.md` row WS-9 and DoE-claude `state/2026-07-29-deleted-hook-scripts-
    bricked-every-write.md`. This module's PRIMARY invocation is a plain
    terminal (`python3 <this-repo>/coordinator/bin/doctor.py`, or the paired
    `doctor.cmd` on Windows) with NO Claude Code process involved, so a session
    with every tool call dead can still be diagnosed and partially repaired.

AUDIENCE
    A nervous operator, often on a machine they have force-shut-down before.
    Output is quiet on a clean layer (`## Layers` below never print a line for
    a layer with nothing to report) and loud, with a plain remediation, on a
    broken one. This is not a wall of diagnostics — see `render_report()`.

LAYERS CHECKED (each is one `Layer` in `run_doctor()`'s return list)
    1. Sibling resolution     — can this machine resolve claude-klabauter's own
       root and DoE-claude's root via the settings-home ladder? (the same
       resolution every coordinator bin/ trampoline depends on.)
    2. Hook registration      — every `coordinator/hooks/hooks.json` command
       (DoE-claude side) resolves to a script that exists on disk, and reports
       whether it routes through the fail-open launcher
       (`coordinator/hooks/fail_open_launcher.py`, DoE 7e5b546a9) or is a bare
       command that would brick every tool call if its target ever goes
       missing. Also inspects a `hooks` block inside `~/.claude/settings.json`
       itself, if one is present — WS-12 has not yet decided whether the
       settings.json-generated copy survives, so this checks both surfaces
       rather than assuming one.
    3. Foreign-platform paths — `~/.claude/settings.json` /
       `settings.local.json` for a path or env-var-reference shaped for the
       OTHER platform (the 2026-07-28 four-brick incident). Delegates to the
       already-built, already-tested
       `coordinator_core.ops.session.guard_foreign_platform_paths` module —
       this file does not re-derive that detector.
    4. Hooks kill-switch      — reports whether
       `~/.claude/.coordinator-hooks-disabled` is armed. Detect-only: WS-12 is
       an open design fork over what this marker should mean going forward
       ("DO NOT RESOLVE THE FORK BY ASSUMPTION") and the marker's own file is
       gitignored per-machine state, not this doctor's to toggle on a guess.

REPAIR POSTURE — what this command fixes vs. only reports, and why
    Exactly one layer is auto-repairable, and only when `--fix` is passed
    (never on a bare read): layer 2's BARE (non-fail-open-wrapped) hook
    command entries in DoE-claude's `coordinator/hooks/hooks.json`. This is
    safe because (a) `hooks.json` is not the bidirectionally-synced file —
    `~/.claude/settings.json` is, and this doctor never writes that one — and
    (b) the wrap is idempotent and already covered by the fail_open_launcher
    module's own tests; `--fix` only ever adds the wrapper, never removes or
    reorders anything. Every other layer is DETECT-ONLY BY CONSTRUCTION:
      - foreign-platform paths and the settings.json hooks block live in a
        file synced across machines via git; a same-host "repair" can
        localize the file to this machine's own form and clobber the other
        machine's next pull. Hand-fix only.
      - the kill-switch marker's correct state is an open design decision
        (WS-12), not a fact this command can derive.
    Absence of a check must not read as the check passing: a layer that could
    not be evaluated (unresolvable sibling root, unreadable hooks.json, etc.)
    reports itself as UNKNOWN, distinct from OK.

Spec backlink: DoE-claude:pln-windows-viability-stop-the-spa-b969d9 WS-9 / AC-28
"""

from __future__ import annotations

import re
import json
import os
import shlex
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from coordinator_core.session.declared_writes import declare_write

_HOOK_SEAM_MARKER = "COORDINATOR HOOK SEAM"
_PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"
_HOOKS_DISABLED_MARKER_NAME = ".coordinator-hooks-disabled"

# Non-"command" `type` values this layer knows have no script path to audit.
# Anything else present-and-non-"command" (a typo like "Command"/"cmd") is an
# unrecognized shape, not a legitimate skip — see _check_one_hooks_doc.
_KNOWN_NON_COMMAND_HOOK_TYPES = {"http"}

# Generator-provenance: the only auto-repair write (--fix, layer 2) targets
# DoE-claude's coordinator/hooks/hooks.json -- a sibling repo's tree, never
# claude-klabauter's own.
GENERATES = []


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One concrete, actionable line item within a layer."""

    severity: str  # "broken" | "info" | "repaired"
    message: str


@dataclass
class Layer:
    name: str
    status: str  # "ok" | "broken" | "unknown"
    findings: List[Finding] = field(default_factory=list)


@dataclass
class DoctorReport:
    layers: List[Layer] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(layer.status == "ok" for layer in self.layers)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    """Mirrors `guard_settings_json_write._config_dir()` — the live
    `~/.claude`-analog directory, CLAUDE_CONFIG_DIR-overridable."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw)
    from coordinator_core._settings_home import home_dir

    return home_dir() / ".claude"


def _hook_argv(hook: dict) -> Optional[List[str]]:
    """Return a hook registration's full argv, across both encodings the fleet
    ships.

    Legacy string form puts the whole invocation in `command`
    (`python3 -c '<bootstrap>' <script> [args...]`, or bare
    `python3 <script> [args...]`). Exec form — what `fail_open_launcher
    .wrap_command_exec` emits and what every registration in DoE-claude's
    hooks.json now uses — puts the bare interpreter in `command` and the real
    argv in `args`, so no shell is ever in the path.

    NEGATIVE SPEC: never `shlex.split` an exec-form `args` entry. Its elements
    are passed to the child verbatim; splitting the `-c` payload on whitespace
    would fabricate tokens that no process ever sees.

    NEGATIVE SPEC: never POSIX-split the legacy form on Windows. POSIX mode
    treats a backslash as an escape, so a native path of the form
    `<drive>:\\hooks\\scripts\\dispatch.py` splits to `<drive>:hooksscripts
    dispatch.py` — a path that stats absent, making the layer report a false
    "registered script missing on disk" for a registration that is in fact
    fine. `posix=False` keeps the separators and, because it also keeps
    surrounding quotes on quoted tokens, those are stripped back off here.
    """
    command = hook.get("command")
    if not isinstance(command, str) or not command:
        return None
    args = hook.get("args")
    if isinstance(args, list) and args:
        return [command] + [a for a in args if isinstance(a, str)]
    posix = os.name != "nt"
    try:
        tokens = shlex.split(command, posix=posix)
    except ValueError:
        return None
    if posix:
        return tokens
    return [_strip_enclosing_quotes(t) for t in tokens]


def _strip_enclosing_quotes(token: str) -> str:
    """Drop one matched pair of surrounding quotes from a non-POSIX shlex token.

    `shlex.split(posix=False)` is the only split that survives Windows path
    separators, but it hands back quoted tokens with their quotes still
    attached — so a quoted path containing a space would otherwise stat as a
    path whose first character is a quote."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _extract_script_path(argv: List[str]) -> Optional[str]:
    """Return the underlying script path a hook's argv invokes — the target
    script, not the bootstrap trampoline or the injector appended after it.

    Both encodings reduce to the same argv shape once `_hook_argv` has
    normalised them, so one rule covers both: skip the interpreter and any
    `-c` payload, then take the first `.py`-suffixed entry. In exec form that
    is `args[2]`, ahead of the injector and bootstrap paths the loader pops off
    the tail; in the legacy wrapped form it is the token after the quoted
    bootstrap; bare, it is the token right after the interpreter.

    The interpreter is matched on its BASENAME, case-folded and stripped of an
    `.exe` suffix, never on the whole token. Windows is first-class and an
    ABSOLUTE interpreter path is its ordinary registration shape; a bare
    `python3` found on `$PATH` is the POSIX case, not the general one. Matching
    the full token reported "command shape not understood" for
    `C:\\...\\Python313\\python3.EXE <script>.py` — a live hook firing on every
    tool call — and a checker that calls a working registration broken is worse
    than one that stays quiet: it is the finding a reader learns to scroll past
    before a true one arrives.
    """
    if not argv:
        return None
    interpreter = os.path.basename(argv[0].replace("\\", "/")).lower()
    if interpreter.endswith(".exe"):
        interpreter = interpreter[: -len(".exe")]
    if interpreter not in ("python3", "python"):
        return None
    rest = argv[1:]
    if rest and rest[0] == "-c":
        rest = rest[2:]
    for token in rest:
        if token.endswith(".py"):
            return token
    return None


def _resolve_plugin_root_token(path: str, doe_root: Optional[str]) -> str:
    if _PLUGIN_ROOT_TOKEN in path and doe_root:
        return path.replace(_PLUGIN_ROOT_TOKEN, f"{doe_root}/coordinator")
    return path


def _iter_hook_commands(hooks_doc: Any):
    """Yield every (event, matcher_index, hook_index, argv, hook_type) tuple in
    a hooks.json-shaped or settings.json `hooks`-block-shaped document.

    `hook_type` is the entry's own `type` field, defaulted to `"command"` when
    absent (the historical shape). It rides along so the caller can tell "a
    command registration I could not read" from "not a command registration at
    all" — a distinction with teeth: the live PreToolUse fan-in is a
    `type: "http"` entry pointing at the warm engine, it has no script path by
    construction, and reporting it as an unreadable command called the warm
    entrypoint broken on a healthy box.

    `argv` is the normalised full invocation from `_hook_argv`, so callers see
    one shape whether the registration is legacy-string or exec-form. Yielding
    the raw `command` string instead is what made this layer vacuous against
    exec form: `command` is then just `python3`.

    NEGATIVE SPEC: a dict-shaped hook entry is always yielded, even when
    `_hook_argv` cannot parse it (`argv is None`). Skipping unparseable
    entries here — rather than handing the caller a `None` argv to turn into
    a Finding — is exactly the "OK on zero parsed registrations" pathology
    this module's status derivation exists to close, moved one call frame
    earlier: a doc where every hook's `command` is missing/non-string/empty
    would parse zero entries and report `ok` on a check it never performed.
    """
    hooks_block = hooks_doc.get("hooks") if isinstance(hooks_doc, dict) else None
    if not isinstance(hooks_block, dict):
        return
    for event, matcher_blocks in hooks_block.items():
        if not isinstance(matcher_blocks, list):
            continue
        for m_idx, block in enumerate(matcher_blocks):
            if not isinstance(block, dict):
                continue
            for h_idx, hook in enumerate(block.get("hooks", []) or []):
                if not isinstance(hook, dict):
                    continue
                hook_type = hook.get("type")
                if not isinstance(hook_type, str) or not hook_type:
                    hook_type = "command"
                yield event, m_idx, h_idx, _hook_argv(hook), hook_type


# ---------------------------------------------------------------------------
# Layer 1 — sibling resolution
# ---------------------------------------------------------------------------


def _check_sibling_resolution() -> Layer:
    findings: List[Finding] = []
    status = "ok"

    from coordinator_core.engine_root import coordinator_engine_root

    try:
        claude_klabauter_root = coordinator_engine_root()
    except RuntimeError as exc:
        status = "broken"
        findings.append(Finding("broken", f"claude-klabauter root did not resolve: {exc}"))
        claude_klabauter_root = None
    else:
        if not os.path.isdir(os.path.join(claude_klabauter_root, "coordinator_core")):
            status = "broken"
            findings.append(
                Finding(
                    "broken",
                    f"resolved claude-klabauter root '{claude_klabauter_root}' has no "
                    "coordinator_core/ — wrong path or partial checkout.",
                )
            )

    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    doe_root = coordinator_doe_root()
    if not doe_root:
        status = "broken"
        findings.append(
            Finding(
                "broken",
                "DoE-claude root did not resolve — set it via "
                "'machine-local set repos.doe_claude <path>', or repair the "
                "'.doe-root' pointer under the settings-home machine-local dir.",
            )
        )
    elif not os.path.isfile(os.path.join(doe_root, "coordinator", "hooks", "hooks.json")):
        status = "broken"
        findings.append(
            Finding(
                "broken",
                f"resolved DoE-claude root '{doe_root}' has no "
                "coordinator/hooks/hooks.json — wrong path or partial checkout.",
            )
        )

    return Layer("Sibling repo resolution (claude-klabauter + DoE-claude)", status, findings)


# ---------------------------------------------------------------------------
# Layer 2 — hook registration health
# ---------------------------------------------------------------------------


def _check_one_hooks_doc(
    doc_path: Path, doe_root: Optional[str], label: str
) -> "tuple[str, List[Finding], bool]":
    """Returns (status, findings, present). `present` is False only for the
    "file does not exist at all" case — the expected, silent, clean state for
    `~/.claude/settings.json` right now (plugin-side delivery from hooks.json
    is live; see module docstring). A present-but-unreadable/unparsable file
    is a real finding, not silence."""
    if not doc_path.is_file():
        return "unknown", [], False

    try:
        with doc_path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return "unknown", [Finding("broken", f"{label}: unreadable/unparsable: {exc}")], True

    findings: List[Finding] = []
    missing = 0
    bare = 0
    total = 0
    non_command = 0

    for event, m_idx, h_idx, argv, hook_type in _iter_hook_commands(doc):
        if hook_type != "command":
            if hook_type in _KNOWN_NON_COMMAND_HOOK_TYPES:
                # Not a command registration, so it has no script path to audit
                # and nothing this layer can say about it. Counted, never
                # asserted on: the live PreToolUse fan-in is `type: "http"`
                # into the warm engine, and calling that "command shape not
                # understood" reported the healthy warm entrypoint as broken.
                non_command += 1
                continue
            # A present type that is neither "command" nor a known
            # non-command shape (e.g. a hand-edit typo like "Command" or
            # "cmd") is not "nothing to audit" — it's a registration this
            # layer cannot read. Review: coordinator:code-reviewer Finding 3
            # — silently folding it into non_command made a mistyped type
            # indistinguishable from a legitimate http entry. `broken` (not
            # `info`) because an unreadable registration is closer to "this
            # entry cannot be verified at all" than to "nothing to see" —
            # the same posture the module already takes for unparsable argv
            # shapes just below.
            findings.append(
                Finding(
                    "broken",
                    f"{label} [{event}/{m_idx}/{h_idx}]: unrecognized hook "
                    f"type {hook_type!r} — not 'command' and not a known "
                    f"non-command type ({sorted(_KNOWN_NON_COMMAND_HOOK_TYPES)!r}).",
                )
            )
            continue
        total += 1
        if argv is None:
            findings.append(
                Finding("broken", f"{label} [{event}/{m_idx}/{h_idx}]: command shape not understood.")
            )
            continue
        wrapped = any(_HOOK_SEAM_MARKER in token for token in argv)
        script = _extract_script_path(argv)
        if script is None:
            findings.append(
                Finding("broken", f"{label} [{event}/{m_idx}/{h_idx}]: command shape not understood.")
            )
            continue
        resolved = _resolve_plugin_root_token(script, doe_root)
        if not os.path.isfile(resolved):
            missing += 1
            findings.append(
                Finding(
                    "broken",
                    f"{label} [{event}/{m_idx}/{h_idx}]: registered script missing on disk: "
                    f"{resolved}"
                    + ("" if wrapped else " (NOT fail-open-wrapped — this would brick every tool call)"),
                )
            )
        if not wrapped:
            bare += 1

    if total == 0:
        # An unrecognized-type finding may already be sitting in `findings`
        # even though no command was ever counted — that's a real `broken`,
        # not the quiet "nothing to audit" case below, so it takes priority.
        if any(f.severity == "broken" for f in findings):
            return "broken", findings, True
        if non_command:
            # Says WHY it is quiet. "ok with nothing audited" and "ok, all
            # registrations pass" are the same word, and this module's whole
            # posture is that absence of a check must never be indistinguishable
            # from the check passing.
            return "ok", findings + [
                Finding(
                    "info",
                    f"{label}: no command registrations to audit "
                    f"({non_command} non-command hook(s), e.g. type: http).",
                )
            ], True
        return "ok", findings, True

    if bare:
        findings.append(
            Finding(
                "info",
                f"{label}: {bare}/{total} hook command(s) are bare (not routed through "
                "fail_open_launcher) — run with --fix to wrap them.",
            )
        )

    # NEGATIVE SPEC: never derive this from the `missing` counter. `missing`
    # only counts registrations that parsed and then failed the stat, so a doc
    # where nothing parses at all — every entry a `broken` finding — reported
    # `ok`, and the layer went green on a check it had not performed. Status
    # follows the findings the layer itself emitted, so any future encoding
    # change fails loud instead of vacuously passing.
    status = "broken" if any(f.severity == "broken" for f in findings) else "ok"
    return status, findings, True


def _check_hook_registration() -> Layer:
    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    doe_root = coordinator_doe_root()
    findings: List[Finding] = []
    statuses: List[str] = []

    if doe_root:
        hooks_json = Path(doe_root) / "coordinator" / "hooks" / "hooks.json"
        status, doc_findings, present = _check_one_hooks_doc(hooks_json, doe_root, "hooks.json")
        if present:
            statuses.append(status)
        findings.extend(doc_findings)
    else:
        statuses.append("unknown")
        findings.append(Finding("broken", "hooks.json: cannot check — DoE-claude root unresolved."))

    settings_path = _config_dir() / "settings.json"
    status, doc_findings, present = _check_one_hooks_doc(
        settings_path, doe_root, "settings.json hooks block"
    )
    # A present-and-empty (no hooks key at all) or genuinely-absent
    # settings.json is the expected clean state right now (plugin-side
    # delivery from hooks.json is live) — deliberately silent, not folded
    # into the overall status. Only a present-but-broken settings.json
    # (unreadable, or entries that resolve to a missing script) surfaces.
    if doc_findings:
        statuses.append(status)
        findings.extend(doc_findings)

    if "broken" in statuses:
        overall = "broken"
    elif all(s == "unknown" for s in statuses):
        overall = "unknown"
    else:
        overall = "ok"

    return Layer("Hook registration (hooks.json + settings.json)", overall, findings)


#: Process-scope lock + refcount table guarding the `sys.path` push/pop
#: below (C10, staff-eng review finding 8). The naive shape — a per-call
#: `inserted = dir not in sys.path` check, insert, then unconditional
#: `sys.path.remove(dir)` in a `finally` — is a first-match-by-VALUE
#: removal: under two interleaved warm dispatches both needing the SAME
#: `hooks_lib_dir` (the common case — most sessions target one doe-claude
#: root), the FIRST caller to finish can pop the entry while the SECOND
#: caller's still-in-flight `import fail_open_launcher` is relying on it
#: being present, stripping a peer's still-needed path entry mid-import.
#: Refcounting fixes this: the directory is only ever actually removed from
#: `sys.path` once every concurrent holder has released it.
_SYS_PATH_LOCK = threading.Lock()
_SYS_PATH_REFCOUNTS: dict = {}


# Review: coordinator:code-reviewer 9b8765ad finding 1 — tracks, per
# dir_path, whether THIS module's own `_sys_path_push` was the call that
# actually inserted the entry, separately from the borrow count. Without
# this, a `dir_path` already present in `sys.path` for a reason outside this
# refcounting (PYTHONPATH, another module, a future second call site) would
# be treated as "count==0 -> we must insert" on first touch, inserting a
# duplicate, and the matching pop would then genuinely `sys.path.remove` an
# entry this module never owned once the count reached 0 — the exact
# "strip a peer's still-needed path entry" bug class C10 exists to close,
# just relocated from a race to a pre-populated path.
_SYS_PATH_OWNED: set = set()


def _sys_path_push(dir_path: str) -> None:
    """Add *dir_path* to `sys.path` (front) if not already held, and record
    one more holder. Pair with `_sys_path_pop` in a `try`/`finally`."""
    with _SYS_PATH_LOCK:
        count = _SYS_PATH_REFCOUNTS.get(dir_path, 0)
        if count == 0 and dir_path not in sys.path:
            sys.path.insert(0, dir_path)
            _SYS_PATH_OWNED.add(dir_path)
        _SYS_PATH_REFCOUNTS[dir_path] = count + 1


def _sys_path_pop(dir_path: str) -> None:
    """Release one holder of *dir_path*; only actually removed from
    `sys.path` once the last concurrent holder releases it, and only if this
    module's own `_sys_path_push` was the one that inserted it."""
    with _SYS_PATH_LOCK:
        count = _SYS_PATH_REFCOUNTS.get(dir_path, 0)
        if count <= 1:
            _SYS_PATH_REFCOUNTS.pop(dir_path, None)
            if dir_path in _SYS_PATH_OWNED:
                _SYS_PATH_OWNED.discard(dir_path)
                try:
                    sys.path.remove(dir_path)
                except ValueError:
                    pass  # already absent — nothing to release
        else:
            _SYS_PATH_REFCOUNTS[dir_path] = count - 1


def _fix_bare_hook_commands(fix_report: List[str]) -> None:
    """--fix action: wrap every bare (unwrapped) command in DoE-claude's
    hooks.json via the real, already-tested fail_open_launcher.wrap_command —
    imported from its actual source location rather than re-derived here, so
    this command shares the exact same wrapping logic the rest of the fleet
    already relies on. Idempotent: is_wrapped() entries are left untouched.
    Does NOT touch ~/.claude/settings.json — that file is bidirectionally
    synced and out of scope for auto-repair (see module docstring)."""
    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    doe_root = coordinator_doe_root()
    if not doe_root:
        fix_report.append("--fix: skipped hooks.json wrap — DoE-claude root unresolved.")
        return

    hooks_json_path = Path(doe_root) / "coordinator" / "hooks" / "hooks.json"
    if not hooks_json_path.is_file():
        fix_report.append(f"--fix: skipped hooks.json wrap — not found at {hooks_json_path}.")
        return

    hooks_lib_dir = str(Path(doe_root) / "coordinator" / "hooks")
    _sys_path_push(hooks_lib_dir)
    try:
        import fail_open_launcher
    except ImportError as exc:
        fix_report.append(f"--fix: skipped hooks.json wrap — fail_open_launcher unimportable: {exc}")
        return
    finally:
        _sys_path_pop(hooks_lib_dir)

    try:
        with hooks_json_path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        fix_report.append(f"--fix: skipped hooks.json wrap — unreadable: {exc}")
        return

    wrapped_count = 0
    hooks_block = doc.get("hooks") if isinstance(doc, dict) else None
    if isinstance(hooks_block, dict):
        for matcher_blocks in hooks_block.values():
            if not isinstance(matcher_blocks, list):
                continue
            for block in matcher_blocks:
                if not isinstance(block, dict):
                    continue
                for hook in block.get("hooks", []) or []:
                    if not isinstance(hook, dict):
                        continue
                    command = hook.get("command")
                    if not isinstance(command, str) or not command:
                        continue
                    # `is_wrapped` is dict-aware: hand it the whole hook when
                    # the registration is exec-form (marker lives in args[1]),
                    # the command string when it is the legacy single-string
                    # form (marker is a substring of the command).
                    probe = hook if isinstance(hook.get("args"), list) else command
                    if fail_open_launcher.is_wrapped(probe):
                        continue
                    try:
                        wrapped = fail_open_launcher.wrap_command_exec(command)
                    except (ValueError, AttributeError):
                        continue
                    hook["command"] = wrapped["command"]
                    hook["args"] = wrapped["args"]
                    wrapped_count += 1

    if wrapped_count == 0:
        fix_report.append("--fix: hooks.json — nothing to wrap (all entries already fail-open).")
        return

    with hooks_json_path.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(hooks_json_path)
    fix_report.append(
        f"--fix: wrapped {wrapped_count} bare hook command(s) in {hooks_json_path} "
        "(left in the working tree — this command never commits)."
    )


# ---------------------------------------------------------------------------
# Layer 3 — foreign-platform paths
# ---------------------------------------------------------------------------


def _check_foreign_platform_paths() -> Layer:
    from coordinator_core.ops.session.guard_foreign_platform_paths import (
        evaluate_foreign_platform_paths,
    )

    config_dir = _config_dir()
    findings: List[Finding] = []
    for name in ("settings.json", "settings.local.json"):
        path = config_dir / name
        try:
            banner = evaluate_foreign_platform_paths(path, config_dir=config_dir)
        except Exception as exc:  # defensive: this layer must never crash the run
            findings.append(Finding("broken", f"{name}: foreign-path check raised {exc!r}."))
            continue
        if banner:
            findings.append(Finding("broken", f"{name}: foreign-platform path(s) detected.\n{banner}"))

    status = "broken" if findings else "ok"
    return Layer("Foreign-platform paths in settings.json", status, findings)


# ---------------------------------------------------------------------------
# Layer 4 — hooks kill-switch marker
# ---------------------------------------------------------------------------


def _check_kill_switch_marker() -> Layer:
    marker = _config_dir() / _HOOKS_DISABLED_MARKER_NAME
    if not marker.is_file():
        return Layer("Hooks generation kill-switch", "ok", [])
    findings = [
        Finding(
            "info",
            f"ARMED: {marker} is present — hook-generation regeneration is suppressed "
            "on this machine (detect-only: whether this marker should still exist is an "
            "open design decision, WS-12 — this command does not toggle it).",
        )
    ]
    return Layer("Hooks generation kill-switch", "ok", findings)


# ---------------------------------------------------------------------------
# Git-hook generation currency
# ---------------------------------------------------------------------------

_GIT_HOOK_GEN_RE = re.compile(r"coordinator-hook-gen: (\d+)")

#: The two hooks `git_hook_install` emits, paired with the installer entrypoint
#: that regenerates each. Order is the order they are repaired in.
_GEN_HOOKS = ("prepare-commit-msg", "post-commit")


def _git_hook_install():
    """Import the emitter, which lives in `coordinator/bin/lib`, not on sys.path.

    Returns None when it cannot be found — a doctor check must degrade to
    `unknown` rather than raise, since it runs on machines whose layout is the
    thing being diagnosed.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "coordinator" / "bin" / "lib"
        if (cand / "git_hook_install.py").is_file():
            _sys_path_push(str(cand))
            try:
                import git_hook_install  # noqa: PLC0415

                return git_hook_install
            except Exception:
                return None
            finally:
                _sys_path_pop(str(cand))
    return None


def _stale_generation_hooks(ghi) -> tuple[List[tuple], Optional[int]]:
    """Every registered clone whose installed hook predates the emitter's stamp.

    Reads only — the repair is `_fix_stale_hook_generations`. Returns
    (rows, current_stamp), each row (repo_name, repo_path, hook_name, found_gen).
    """
    try:
        from coordinator_core.machine_resolver import merged_flat_registry
    except Exception:
        return [], None
    current = getattr(ghi, "_HOOK_GEN_STAMP", None)
    if current is None:
        return [], None

    stale: List[tuple] = []
    for key, path in sorted(merged_flat_registry().items()):
        if not key.startswith("repos.") or not path:
            continue
        name = key.split(".", 1)[1]
        for hook in _GEN_HOOKS:
            f = Path(path) / ".git" / "hooks" / hook
            if not f.is_file():
                continue  # absent is not stale — never install where nothing was
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _GIT_HOOK_GEN_RE.search(body)
            if not m:
                continue  # unstamped: a foreign or hand-written hook, not ours to touch
            if int(m.group(1)) != current:
                stale.append((name, path, hook, int(m.group(1))))
    return stale, current


def _check_hook_generation_currency() -> Layer:
    """Do the INSTALLED hook bodies match the generator?

    Exists because `_HOOK_GEN_STAMP` made currency decidable and nothing
    consumed it. Measured 2026-08-25: 15 of 16 registered clones sat at gen 4
    against an emitter at 8, so every guard emitted into those bodies was live
    in the generator and dead on every box. The only non-test caller of the
    installer entrypoints ran at registration time, so a stamp bump reached
    only repos someone happened to re-register.

    That gap is why this check reports the INSTALLED artifact rather than the
    generator: a session that reads the emitter concludes the guard is present
    and is right about the generator and wrong about the machine. Two sessions
    reached wrong conclusions from exactly that on 2026-08-25.

    NEGATIVE SPEC: an absent hook is not stale, and an unstamped hook is not
    ours -- neither is repaired. Installing where nothing was installed, or
    rewriting a body carrying no `coordinator-hook-gen` line, would make this
    check a fleet-wide hook installer wearing a diagnostic's costume.
    """
    name = "Git-hook generation currency"
    ghi = _git_hook_install()
    if ghi is None:
        return Layer(name, "unknown", [Finding("info", "git_hook_install not importable — cannot compare generations")])

    stale, current = _stale_generation_hooks(ghi)
    if current is None:
        return Layer(name, "unknown", [Finding("info", "no machine registry or no _HOOK_GEN_STAMP — nothing to compare")])
    if not stale:
        return Layer(name, "ok", [])

    repos = sorted({row[0] for row in stale})
    findings = [
        Finding(
            "broken",
            f"{len(stale)} installed hook(s) across {len(repos)} repo(s) predate the emitter "
            f"(gen {current}) — the guards in those bodies are not live on this machine. "
            "Re-run with --fix. Repos: " + ", ".join(repos),
        )
    ]
    return Layer(name, "broken", findings)


def _fix_stale_hook_generations(fix_report: List[str]) -> None:
    """Regenerate every stale installed hook to the emitter's current stamp.

    Idempotent by construction: the installer entrypoints return
    `already-current` when a body is at the stamp, so re-running is free. Only
    `.git/hooks/` is written, which is untracked and per-clone -- nothing
    tracked is mutated in any sibling repo.
    """
    ghi = _git_hook_install()
    if ghi is None:
        return
    stale, current = _stale_generation_hooks(ghi)
    if not stale or current is None:
        return

    bin_dir = None
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "coordinator" / "bin" / "lib" / "git_hook_install.py").is_file():
            bin_dir = str(parent / "coordinator" / "bin")
            break
    if bin_dir is None:
        return

    entry = {
        "prepare-commit-msg": getattr(ghi, "ensure_prepare_commit_msg_hook", None),
        "post-commit": getattr(ghi, "ensure_post_commit_hook", None),
    }
    # Hold the emitter's own directory on sys.path for the WHOLE sweep. The
    # installer lazy-imports siblings out of `coordinator/bin/lib` while it
    # runs, and `_git_hook_install` pops the path before returning -- so
    # repairing with the path already popped makes those imports fail inside a
    # function contractually forbidden from raising, which turns a failed
    # rewrite into a silent no-op that reports success.
    lib_dir = str(Path(bin_dir) / "lib")
    _sys_path_push(lib_dir)
    try:
        _repair_each(entry, stale, bin_dir, fix_report, current)
    finally:
        _sys_path_pop(lib_dir)


def _repair_each(entry, stale, bin_dir: str, fix_report: List[str], current: int) -> None:
    """One installer call per stale hook, each isolated from the others."""
    for repo_name, repo_path, hook, found in stale:
        fn = entry.get(hook)
        if fn is None:
            continue
        outcome: List[str] = []
        try:
            fn(bin_dir, root=repo_path, outcome=outcome)
        except Exception as exc:  # never let one clone stop the sweep
            fix_report.append(f"{repo_name}/{hook}: FAILED ({type(exc).__name__}: {exc})")
            continue
        f = Path(repo_path) / ".git" / "hooks" / hook
        m = _GIT_HOOK_GEN_RE.search(f.read_text(encoding="utf-8", errors="replace")) if f.is_file() else None
        now = int(m.group(1)) if m else None
        if now == current:
            fix_report.append(f"{repo_name}/{hook}: gen {found} -> {current} ({','.join(outcome) or 'rewritten'})")
        else:
            fix_report.append(f"{repo_name}/{hook}: STILL gen {now} after repair — investigate")


# ---------------------------------------------------------------------------
# Orchestration + rendering
# ---------------------------------------------------------------------------


def run_doctor(fix: bool = False) -> tuple[DoctorReport, List[str]]:
    report = DoctorReport()
    report.layers.append(_check_sibling_resolution())
    report.layers.append(_check_hook_registration())
    report.layers.append(_check_foreign_platform_paths())
    report.layers.append(_check_kill_switch_marker())
    report.layers.append(_check_hook_generation_currency())

    fix_report: List[str] = []
    if fix:
        _fix_bare_hook_commands(fix_report)
        _fix_stale_hook_generations(fix_report)

    return report, fix_report


def render_report(report: DoctorReport, fix_report: List[str]) -> str:
    lines: List[str] = []
    lines.append("coordinator doctor — layer health")
    lines.append("=" * 34)

    any_broken = False
    any_unknown = False
    for layer in report.layers:
        marker = {"ok": "OK", "broken": "BROKEN", "unknown": "UNKNOWN"}[layer.status]
        lines.append(f"[{marker:7}] {layer.name}")
        if layer.status == "broken":
            any_broken = True
        if layer.status == "unknown":
            any_unknown = True
        for finding in layer.findings:
            prefix = {"broken": "  ! ", "info": "  - ", "repaired": "  + "}[finding.severity]
            for sub_line in finding.message.splitlines():
                lines.append(prefix + sub_line)

    if fix_report:
        lines.append("")
        lines.append("Repairs attempted (--fix):")
        for line in fix_report:
            lines.append(f"  + {line}")

    lines.append("")
    if not any_broken and not any_unknown:
        lines.append("All layers healthy. Nothing to fix.")
    elif any_broken:
        lines.append("BROKEN layer(s) found above — hand-fix per the remediation line, "
                      "or re-run with --fix for the one auto-repairable class (bare hook commands).")
    else:
        lines.append("Some layer(s) could not be evaluated (UNKNOWN) — see above; "
                      "absence of a check is not the same as a clean result.")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    fix = "--fix" in argv

    report, fix_report = run_doctor(fix=fix)
    print(render_report(report, fix_report))

    return 1 if not report.all_ok else 0


if __name__ == "__main__":
    sys.exit(main())
