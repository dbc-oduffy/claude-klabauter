"""
coordinator_core.ops.install_claude_klabauter_precommit_hook — claude-klabauter's own
pre-commit git-hook gate installer.

Purpose: installs (or appends onto an existing custom hook) claude-klabauter's
`.git/hooks/pre-commit` gate chain. Idempotent, conditional: only installs
when the resolved target repo root IS claude-klabauter itself. Consumer/sibling
repos (and any other git repo) are skipped cleanly.

Modelled closely on `coordinator_core.ops.install_meta_repo_precommit_hook`
(the meta-repo's own pre-commit installer) — same registry-driven shape,
same self-relative gate resolution, same fail-loud discipline, same
append-not-clobber behavior on a foreign existing hook. Kept as an
independent module (not an import of that module's private helpers) because
this installer's gate-execution contract differs from that module's in one
load-bearing way — see "Exit-code clamping" below — and grafting a
narrower contract onto shared private plumbing risks the wider one
regressing back in on a future edit to the shared module.

Identity guard — no `$HOME` literal, no env var: this module resolves "is
this claude-klabauter" from its OWN file location (`_self_repo_root()`), the
same self-relative principle `_bin_dir()` already applies to gate-script
lookup. This module ships INSIDE claude-klabauter, so wherever it is actually
running from IS the claude-klabauter repo root — there is no separate identity to look
up. This differs from the meta-repo installer (which targets `$HOME/.claude`,
a DIFFERENT repo than the one that module ships in) only in what the
identity anchor points at; the shape (canonicalize target, canonicalize
anchor, compare) is the same.

Gate registry (`_GATE_REGISTRY` below) starts with exactly one gate — the
staged-rollback detector (`coordinator_core.ops.detect_staged_rollback` via
its `coordinator/bin/detect-staged-rollback.py` CLI). Adding a future gate is
a registry entry, not a new code path.

Exit-code clamping (the one place this module's gate execution deliberately
diverges from the meta-repo installer's `_gate_block`): that module's gates
run `"$_py" "$_gate_script" || exit $?`, propagating the gate script's raw
exit code verbatim. That is safe for THAT registry's four scripts, which
only ever exit 0 or 1. It is NOT safe here: `detect-staged-rollback.py`'s own
documented exit contract has THREE values (0 clean / 1 a mass-deletion
finding / 2 usage error — see that module's own "Exit-code contract"
docstring). A bare `|| exit $?` would let 2 escape straight out of this
hook. A pre-commit hook exiting anything other than 0 or 1 is read by the
Claude Code harness as a blocking DENY that kills Bash/Write/Edit together,
INCLUDING the tools needed to repair the hook — this bricked the primary
macOS box four times on 2026-07-28 (see the dispatch brief this module was
built from). So every gate block here captures `$?` into a variable and
explicitly re-derives the branch from it via `_finding_branch`'s `case`
statement: exactly 0 continues with no output, every other code (recognized
or not) ends in exactly `exit 1` on its BLOCKED leg — never the raw code.

2026-08-21 — CHECK 1 (exact-blob rollback) KILLED, PM ruling; see
`coordinator_core.ops.detect_staged_rollback`'s module docstring for the
full ruling. `_Gate.override_env` and `gate.finding_override_env` here are
now scoped to the ONE surviving check (mass deletion) and its ONE key,
`COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION` — the earlier
`COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK` key is dead everywhere in
this file (no gate script reads it any more) and MUST NOT be reintroduced
here on the theory that a future check might want it back; a new check gets
a new key with its own registry entry, as this one had. BREAK-CLASS bug
found and fixed in the same edit: `_Gate.override_env` used to be exactly
`COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK`, so a hook built from the
pre-2026-08-21 `_finding_branch` scoping (below) still let that stale key
blanket-suppress a mass-deletion BLOCK via the missing-script/missing-
interpreter CANNOT-PROCEED branches. Repointing `override_env` to the mass-
deletion key closes that.

`gate.finding_override_env` still maps each finding exit code to the
override key(s) THAT code alone honors (`_finding_branch`'s own docstring
has the full rationale); a code absent from that map — including
`detect-staged-rollback.py`'s own usage-error code, 2 — is never
overridable here, by construction, matching the meta-repo installer's
`_finding_branch` (which never honors ANY override on a real finding at
all — see that module's own docstring). The gate script's OWN override
handling (`detect_staged_rollback.main`'s internal env read) already makes
a deliberate override exit 0 before this wrapper ever sees a nonzero code —
since the hook subprocess inherits this wrapper's own environment
unchanged, this wrapper's per-code check can only ever confirm what the
script already resolved, never diverge from it; the wrapper-level check
exists for symmetry with the missing-script/missing-interpreter
CANNOT-PROCEED branches (`_cannot_proceed_branch`, unchanged by this fix)
and for the BLOCKED banner naming which finding fired, not because it is
reachable through any other path.

Hooks already installed on disk keep the pre-fix key/body until this
installer is RE-RUN — there is no separate migration step, because one
already exists: `_install_or_append_hook`'s "ours_wholesale" path does a
full byte-for-byte compare of the hook this installer itself wrote against
what it would write today (`_hook_body(gates) == existing_text`), and
rewrites on any difference (`test_stale_gate_body_is_refreshed_not_reported_
as_installed` pins this generically; this fix's own body change is picked up
by the same mechanism with no dedicated version stamp needed, unlike the
meta-repo installer's separate versioned-region system).

2026-08-21 — C17 (`docs/plans/2026-08-21-the-cli-bootstrap-tax-dies-at-the-
interpreter-floor.md`): the emitted hook body no longer walks `$PATH` in
shell to resolve an interpreter. `_py_resolve_line()` now bakes
`sys.executable` — the interpreter this installer is ALREADY running
under — at install time, forward-slash-normalized via
`coordinator_core.win_portability.split_path`, instead of emitting
`coordinator_core.py_probe_sh.python_probe_lines()`'s directory-by-directory
`$PATH` walk. That walk's two load-bearing behaviours (per-directory
candidate resolution, the case-insensitive `WindowsApps` exclusion) are
DISCHARGED BY CONSTRUCTION here, not reproduced: a Microsoft Store App
Execution Alias stub cannot be the process resolving this line, so there is
no candidate set to walk in the first place — see `_py_resolve_line`'s own
docstring for the measured cost this removes and the self-heal
(`[ -x "$_py" ] || _py=""`) that keeps a moved/upgraded interpreter from
silently wedging every future commit; a cleared `_py` still routes through
the SAME missing-interpreter CANNOT-PROCEED branch `_gate_block` already
had, exit-code-clamped like every other failure mode here. `py_probe_sh`
itself is untouched — it remains the shared primitive for the sites that
still need a real `$PATH` walk (see that module's own docstring for the
current, corrected consumer count). Carve-out paperwork:
`docs/reference/shell-out-carve-outs.md` § (b) now names this module's
emitted interpreter-invocation lines explicitly, scoped to that line alone
— not to candidate resolution, which lives in Python now.

Negative-spec:
    - Never emits a hook body that can exit anything other than 0 or 1 — see
      "Exit-code clamping" above and this module's own tests
      (`test_gate_script_exit_2_is_clamped_to_1`,
      `test_hook_body_never_contains_a_bare_exit_dollar_question`).
    - No `bash` anywhere in the emitted hook body — the shim is `#!/bin/sh`
      and execs `$_py` directly, per claude-klabauter's CLAUDE.md § Runtime
      conventions (b) (sanctioned ONLY as a git-hook shim body that execs
      Python directly, never general script execution).
    - Never re-introduces a `$PATH` walk into the emitted hook body — the
      interpreter is resolved exactly once, at install time in Python
      (`_baked_interpreter_path`), never at commit time in shell. A future
      gate needing a DIFFERENT interpreter than the installer's own is a
      new problem, not a reason to bring `python_probe_sh` back here.
    - Does not install into THIS session's own live repo — that is an
      explicit dispatch-brief carve-out; installing/verifying the live
      artifact is left to the invoking operator. This module and its CLI are
      built and tested against `tmp_path` throwaway repos only.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.git.repo_root import show_toplevel as _show_toplevel
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import split_path
# Cross-package import of the SSOT doc-pointer display string (same
# precedent write_guards already uses for operator_override_note itself) --
# emitted hook-body remediation text points readers at the doc that
# enumerates these keys, never names a key inline (B6/B8, see
# docs/wiki/guard-messaging.md § Register).
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY

GENERATES = []  # writes only own .git/hooks/pre-commit, never tracked

_PROG = "install-claude-klabauter-precommit-hook"


@dataclass(frozen=True)
class _Gate:
    marker: str        # presence key: substring searched for in the hook body to find this gate's region
    filename: str       # coordinator/bin/ filename (relative to the resolved bin dir)
    label: str          # human label for the BLOCKED banner
    override_env: str   # env var that bypasses a CANNOT-RUN/CANNOT-PROCEED block (missing script/interpreter)
    # Per-exit-code override mapping for the "gate script itself exited
    # nonzero" branch — see `_finding_branch`'s docstring. A code absent
    # from this mapping (including any code the gate script's own contract
    # names as a transport failure, e.g. `detect-staged-rollback.py`'s 2)
    # is NEVER overridable at this wrapper: it always BLOCKS. A gate with an
    # empty mapping (the default) therefore never offers a wrapper-level
    # bypass for ANY of its findings — the conservative, fail-loud default.
    finding_override_env: Dict[int, Tuple[str, ...]] = field(default_factory=dict)


_GATE_REGISTRY: List[_Gate] = [
    _Gate(
        marker="detect-staged-rollback",
        filename="detect-staged-rollback.py",
        label="staged-rollback",
        override_env="COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION",
        # Mirrors coordinator_core.ops.detect_staged_rollback's own EXIT_*
        # contract: exit 1 is the (sole surviving) mass-deletion finding.
        # Exit 2 (that module's own usage-error code) is deliberately ABSENT
        # from this mapping — see `_finding_branch`. Check 1 (exact-blob
        # rollback, exit codes 1/3/4 under the old contract) was KILLED
        # 2026-08-21 (PM ruling) — see detect_staged_rollback's module
        # docstring — so this mapping no longer carries a rollback-only or
        # both-findings arm.
        finding_override_env={
            1: ("COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION",),
        },
    ),
]


#: Gate-script directory as repo-root-relative POSIX segments. Single source
#: of truth for BOTH `_bin_dir()` (an absolute path, for the install-time
#: existence check) and the path EMITTED into the hook body (relative, see
#: `_gate_block`) — so the two can never drift apart.
_BIN_SUBDIR = ("coordinator", "bin")


def _bin_dir() -> Path:
    """The directory holding the pre-commit gate scripts, resolved SELF-
    RELATIVE to this module — this ops package (`coordinator_core/ops/`) and
    `coordinator/bin/` are siblings under the same repo root, so the engine
    that is actually running locates its own helpers. No `$HOME` literal, no
    env var, no cross-repo registry read.

    Used for the INSTALL-TIME existence check only — never for the path
    written into the hook. See `_gate_block` for why the emitted path is
    relative instead."""
    return Path(__file__).resolve().parents[2].joinpath(*_BIN_SUBDIR)


def _self_repo_root() -> str:
    """The repo root this module itself lives in — the identity anchor for
    "is the target claude-klabauter". This module ships INSIDE claude-klabauter,
    so wherever it is actually running from IS the claude-klabauter repo — there is no
    separate lookup. Exposed as its own function (rather than inlined) so
    tests can monkeypatch it to point at a throwaway `tmp_path` repo, the
    same way `_bin_dir()` is independently monkeypatchable for gate-script
    resolution."""
    return str(Path(__file__).resolve().parents[2])


def _canon(path: str) -> str:
    """Canonicalize a path: "" on any failure (non-existent/non-directory
    target, or an OSError resolving it) so a failed canon can only match
    another failed canon — never a false positive against a real path."""
    if not path:
        return ""
    try:
        if not os.path.isdir(path):
            return ""
        return os.path.realpath(path)
    except OSError:
        print(f"skip: _canon: os.path.isdir/realpath failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""


def _git_toplevel(target: str) -> Optional[str]:
    """Resolve the repo root for `target` via `git -C <target> rev-parse
    --show-toplevel`. Returns None on any git failure (not a git repo, git
    missing, etc.)."""
    return _show_toplevel(cwd=target)


def _atomic_write(path: str, content: str) -> None:
    """Write `content` to `path` atomically: write to a `.tmp.<pid>` sibling,
    then rename, so a concurrent git-commit never reads a torn hook. Chmods
    0o755 after on POSIX, where a git hook must be executable to fire at
    all — the POSIX exec bit is meaningless on Windows (git for Windows
    invokes the hook via its own shebang-aware shell layer regardless), so
    the chmod is skipped there rather than issued as a harmless no-op."""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.replace(tmp_path, path)
    if os.name != "nt":
        os.chmod(path, 0o755)
    # DR-276: declared AFTER the write lands, at the FINAL destination
    # (`path`), never the discarded `tmp_path` — this is the single real
    # write site every `_install_or_append_hook` call routes through.
    declare_write(path)


def _strip_trailing_exit0(text: str) -> str:
    """Strip a bare trailing `exit 0` line so gates appended after it are not
    dead code — a `sh` script that already returned never reaches anything
    appended past it. Every hook this installer writes re-adds exactly one
    trailing `exit 0` at the true end, so this strip-then-reappend keeps that
    invariant regardless of how many times gates get appended."""
    rstripped = text.rstrip("\n")
    lines = rstripped.split("\n") if rstripped else []
    if lines and lines[-1].strip() == "exit 0":
        lines = lines[:-1]
    result = "\n".join(lines)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


def _sh_double_quote_escape(value: str) -> str:
    """Escape `value` for embedding inside a POSIX-sh double-quoted string
    literal (`_py="<value>"`). Escapes only the four characters `sh`'s own
    double-quote grammar treats specially inside `"..."` — backslash,
    double-quote, `$`, and backtick — and escapes backslash FIRST, so a
    later substitution's own inserted backslash is never re-escaped by an
    earlier one. Callers here always pass a forward-slash-normalized path
    (see `_baked_interpreter_path`), so in practice this only ever needs to
    guard `"`/`$`/backtick; the backslash leg exists for correctness against
    an arbitrary string, not because a real baked path is expected to carry
    one."""
    out = value.replace("\\", "\\\\")
    for ch in ('"', "$", "`"):
        out = out.replace(ch, "\\" + ch)
    return out


def _baked_interpreter_path() -> str:
    """The interpreter this installer itself is running under
    (`sys.executable`), forward-slash-normalized via
    `coordinator_core.win_portability.split_path` — reused rather than a
    hand-rolled `.replace("\\\\", "/")`, per that module's own "one correct
    implementation" rationale. Forward-slash form sidesteps the one real
    hazard a native Windows path (backslashes, frequently a space from
    `Program Files`) poses when embedded in a `#!/bin/sh` body that
    git-for-Windows' bundled MSYS `sh` will `exec` — see module docstring,
    "The commit-hot-path PATH walk this replaces"."""
    return "/".join(split_path(sys.executable))


def _py_resolve_line() -> str:
    """Emit a BAKED interpreter assignment, not a `$PATH` walk.

    Was: `coordinator_core.py_probe_sh.python_probe_lines("_py")`, a
    directory-by-directory `$PATH` walk with a `WindowsApps` case-exclusion
    (see that module's own docstring for why THAT shape is load-bearing
    prior art for the sites that still need it). This installer does not
    need it: `sys.executable` IS the interpreter the running installer is
    already executing under, so there is no candidate SET to walk and no
    WindowsApps-stub-vs-real-interpreter name collision to disambiguate —
    a Microsoft Store App Execution Alias stub cannot be the process that is
    resolving this line. Both of `python_probe_sh`'s load-bearing behaviours
    (directory-by-directory walking, the WindowsApps exclusion) are
    discharged BY CONSTRUCTION, not reproduced, exactly because there is
    nothing left to filter.

    Measured cost of the walk this removes (`batched_process_time_ms`,
    k=20, this box, the pre-C17 `_py_resolve` body run standalone under
    git-for-Windows' `sh.exe`): 67.188ms/call process time at 3.0
    procs/call, against a 7.812ms/call `cmd /c exit` floor at 1.0
    procs/call on the same box — roughly 59ms of PATH-walk-specific
    process time paid on every one of the ~840 commits/day this fires
    before ("THE FIRST THING ON THE COMMIT PATH") this plan's own other
    savings can apply.

    Self-heal, not a silent skip: a baked absolute path goes stale the
    moment Python is upgraded or moved. `[ -x "$_py" ] || _py=""` re-tests
    executability at COMMIT time (once, not per-candidate) and clears `_py`
    on failure so the ALREADY-EXISTING missing-interpreter CANNOT-PROCEED
    branch in `_gate_block` fires — same exit-code-clamping shape (BLOCKED
    banner, `exit 1`, never a raw code) every other failure mode here
    already uses, not a new mechanism. See `_gate_block`'s own
    `elif [ -z "$_py" ]` arm for the (now baked-path-specific) remediation
    text.
    """
    escaped = _sh_double_quote_escape(_baked_interpreter_path())
    return "\n".join(
        [
            f'_py="{escaped}"',
            '[ -x "$_py" ] || _py=""',
        ]
    )


def _finding_branch(gate: _Gate) -> List[str]:
    """Emit the "the gate script itself exited nonzero" branch (case 3 of
    `_gate_block`'s three CANNOT-PROCEED/BLOCKED cases) as a `case
    "$_gate_rc" in` statement with exactly one arm per exit code
    `gate.finding_override_env` names, plus a `*)` default arm.

    Fixes the 2026-08-21 defect this module's own `_Gate` used to have: a
    SINGLE `override_env` string applied uniformly to ANY nonzero
    `$_gate_rc`, so `COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK` (spelled
    for check 1 of `detect-staged-rollback.py` only) silently bypassed a
    check-2 mass-deletion BLOCK it never inspected — an operator who exports
    that var for legitimate revert work could, without knowing it, suppress
    the guard built to catch the 2026-07-28 empty-tree incident. Each arm
    below tests ONLY the override key(s) `gate.finding_override_env` maps to
    THAT exit code (`&&`-joined when a code needs more than one, e.g. "both
    findings fired" needs both) — a key correct for a sibling code has no
    effect here, by construction, since it is never even tested in the
    wrong arm.

    The `*)` default arm covers every exit code `gate.finding_override_env`
    does NOT name — including `detect-staged-rollback.py`'s own transport-
    failure/usage-error code (2) and any future surprise value — and
    NEVER offers an override: an unrecognized code fails LOUD, unconditionally,
    rather than falling through to success. This also means a gate with an
    empty `finding_override_env` (the default) never offers a wrapper-level
    bypass for any of its findings at all — the conservative default for a
    future registry entry nobody has reasoned about per-code overrides for
    yet.

    Every arm still ends in exactly `exit 1` on the BLOCKED leg (never the
    raw `$_gate_rc` — see module docstring's "Exit-code clamping"); the
    override leg (when present and satisfied) SKIPS instead, exactly like
    the CANNOT-PROCEED branches' own override handling.
    """
    lines = [
        '  case "$_gate_rc" in',
        "    0)",
        "      :",  # clean run — no-op, matches the pre-existing fall-through-with-no-output shape
        "      ;;",
    ]
    for code in sorted(gate.finding_override_env):
        keys = gate.finding_override_env[code]
        override_test = " && ".join(f'[ "${k}" = "1" ]' for k in keys)
        lines += [
            f"    {code})",
            f"      if {override_test}; then",
            f'        echo "pre-commit: gate [{gate.label}] ({gate.marker}) SKIPPED -- reported a problem (exit code $_gate_rc) (override set)." >&2',
            "      else",
            f'        echo "pre-commit: BLOCKED -- gate [{gate.label}] ({gate.marker}) reported a problem (exit code $_gate_rc) -- see output above." >&2',
            f'        echo "pre-commit: remediation: fix the flagged issue. See {OVERRIDE_KEYS_DOC_DISPLAY} for override options." >&2',
            "        exit 1",
            "      fi",
            "      ;;",
        ]
    lines += [
        "    *)",
        f'      echo "pre-commit: BLOCKED -- gate [{gate.label}] ({gate.marker}) reported a problem (exit code $_gate_rc) -- see output above." >&2',
        f'      echo "pre-commit: remediation: fix the flagged issue. See {OVERRIDE_KEYS_DOC_DISPLAY} for override options." >&2',
        "      exit 1",
        "      ;;",
        "  esac",
    ]
    return lines


def _gate_block(gate: _Gate) -> List[str]:
    """Emit the runtime lines for one gate.

    Three CANNOT-PROCEED/BLOCKED cases, none of which ever propagates a raw
    exit code (see module docstring's "Exit-code clamping"):
      1. missing script — honors `gate.override_env` (`_cannot_proceed_branch`)
      2. missing interpreter — honors `gate.override_env` (`_cannot_proceed_branch`)
      3. the gate script itself exited nonzero (a real finding, OR its own
         transport-layer failure such as exit 2) — dispatched by exit CODE
         via `_finding_branch`, never by the single `gate.override_env`; see
         that function's own docstring for why cases 1-2 and case 3 use
         different override keys/shapes on purpose.

    A clean gate run (`$_gate_rc` = 0) falls through with no output.

    The emitted gate path is REPO-ROOT-RELATIVE, not the absolute path
    `_bin_dir()` resolves. git runs `pre-commit` with cwd at the top level of
    the working tree (a linked worktree's top level likewise contains
    `coordinator/`), so a relative path resolves correctly AND survives the
    clone being moved or renamed. An absolute literal baked at install time
    does not: relocate the checkout and every commit BLOCKs on a missing gate
    script until the installer is re-run. That is the same defect class that
    silently disarmed all four meta-repo gates when the executable surface
    moved at `b644d5a9`, so it is not repeated here.

    DELIBERATE DIVERGENCE from `install_meta_repo_precommit_hook.py`, which
    emits an absolute path — and correctly so. That installer writes a hook
    into `~/.claude` pointing at a gate in a DIFFERENT repo (claude-klabauter's
    `coordinator/bin/`), where nothing relative could resolve. This installer
    writes a hook into claude-klabauter pointing at a gate inside claude-klabauter itself. The two
    must differ; do not "harmonize" them.
    """
    script_path = "/".join([*_BIN_SUBDIR, gate.filename])
    override_test = f'[ "${gate.override_env}" = "1" ]'

    def _cannot_proceed_branch(reason: str, remediation: str) -> List[str]:
        return [
            f"  if {override_test}; then",
            f'    echo "pre-commit: gate [{gate.label}] ({gate.marker}) SKIPPED -- {reason} (override set)." >&2',
            "  else",
            f'    echo "pre-commit: BLOCKED -- gate [{gate.label}] ({gate.marker}) cannot proceed: {reason}." >&2',
            f'    echo "pre-commit: remediation: {remediation}. See {OVERRIDE_KEYS_DOC_DISPLAY} for override options." >&2',
            "    exit 1",
            "  fi",
        ]

    lines = [
        f"# --- Gate: {gate.label} ({gate.marker}) ---",
        f'_gate_script="{script_path}"',
        'if [ ! -f "$_gate_script" ]; then',
    ]
    lines += _cannot_proceed_branch(
        "missing script $_gate_script", "re-run the coordinator installer to restore it"
    )
    lines.append('elif [ -z "$_py" ]; then')
    lines += _cannot_proceed_branch(
        "the interpreter baked in at install time is missing or was moved",
        "re-run the coordinator installer (install-claude-klabauter-precommit-hook) to rebake it",
    )
    lines += [
        "else",
        '  "$_py" "$_gate_script"',
        "  _gate_rc=$?",
    ]
    lines += _finding_branch(gate)
    lines += ["fi"]
    return lines


#: Opening lines of a body THIS module wrote. Used as the authorship test in
#: `_install_or_append_hook`: a hook starting with this is ours end to end and
#: may be rewritten wholesale when stale; anything else is treated as a custom
#: hook and only ever appended to.
_BODY_HEADER = (
    "#!/bin/sh\n"
    "# claude-klabauter pre-commit gates — fire before drift can land.\n"
)


def _gate_region_is_current(existing_text: str, gate: _Gate) -> bool:
    """Whether `existing_text` contains this gate's block EXACTLY as it would
    be emitted today.

    The gate's marker being present proves only that some version of the gate
    is wired; it says nothing about whether that version is the current one.
    This compares the emitted lines themselves, so a changed script path,
    override name, or exit-code clamp reads as stale rather than as installed.
    """
    return "\n".join(_gate_block(gate)) in existing_text


def _hook_body(gates: List[_Gate]) -> str:
    """Assemble a full hook body for exactly `gates` (in registry order).

    Takes no bin dir: every emitted gate path is repo-root-relative (see
    `_gate_block`), so the body is independent of where this checkout lives."""
    lines = [
        *_BODY_HEADER.rstrip("\n").split("\n"),
        "# Registry-driven (coordinator_core.ops.install_claude_klabauter_precommit_hook);",
        "# every gate below fails LOUD (exit 1, named banner) on a missing script,",
        "# missing interpreter, or a nonzero gate exit code of ANY value -- this",
        "# hook body NEVER exits anything other than 0 or 1 (see that module's",
        "# docstring, \"Exit-code clamping\").",
        "",
        _py_resolve_line(),
        "",
    ]
    for gate in gates:
        lines.extend(_gate_block(gate))
        lines.append("")
    lines.append("exit 0")
    lines.append("")
    return "\n".join(lines)


def _warn_if_gate_script_missing(gates: List[_Gate]) -> None:
    """Best-effort INSTALL-TIME advisory: if a registered gate's script is
    not actually present at the resolved bin dir right now, say so loudly at
    install time rather than only discovering it at the next commit's
    runtime `[ -f ]` check (see `_gate_block`, which is the actual
    enforcement point — this is a heads-up, not a gate). This is the genuine
    remaining use for `_bin_dir()`'s absolute resolution: an existence check
    needs a real filesystem path to stat. The path EMITTED into the hook
    stays repo-root-relative regardless — see `_gate_block`'s own docstring."""
    bin_dir = _bin_dir()
    for gate in gates:
        if not (bin_dir / gate.filename).is_file():
            print(
                f"{_PROG}: ADVISORY: gate script not found at install time: "
                f"{bin_dir / gate.filename} (gate [{gate.label}] will BLOCK at "
                "commit time until this is resolved).",
                file=sys.stderr,
            )


def _install_or_append_hook(repo_root: str, gates: List[_Gate]) -> int:
    """Install/append `.git/hooks/pre-commit` in `repo_root` against
    `gates`. Fresh install writes the full body; an existing hook gets
    whatever gates are missing appended (after stripping a trailing bare
    `exit 0` so the appended gates are reachable); a hook that already
    carries every gate marker is a no-op."""
    hook_path = os.path.join(repo_root, ".git", "hooks", "pre-commit")
    _warn_if_gate_script_missing(gates)

    existing_text = ""
    hook_exists = os.path.isfile(hook_path)
    if hook_exists:
        try:
            with open(hook_path, "r", encoding="utf-8") as fh:
                existing_text = fh.read()
        except OSError as exc:
            print(f"skip: _install_or_append_hook: reading {hook_path} failed: {exc}", file=sys.stderr)
            existing_text = ""

    missing_gates = [g for g in gates if g.marker not in existing_text]

    if not hook_exists:
        content = _hook_body(gates)
        _atomic_write(hook_path, content)
        print(f"{_PROG}: installed {hook_path}.", file=sys.stderr)
        return 0

    # Marker-presence alone is NOT "up to date". A gate's marker survives any
    # change to the body that runs it — a corrected script path, a new
    # override, a fixed exit-code clamp — so keying idempotency on the marker
    # would leave an OUTDATED hook in place and report success. That is the
    # "looks installed while enforcing something else" failure this whole gate
    # chain exists to prevent, reproduced in the installer itself. Empirically
    # hit on 2026-07-28: the fix making the emitted gate path repo-root-
    # relative was a silent no-op on the box that already had the absolute-path
    # version, which reported "already installed" while still carrying the
    # stale path.
    #
    # So a hook this installer wrote is compared against what it WOULD write
    # now, and rewritten on any difference. A hook carrying foreign content is
    # never rewritten wholesale — appending is the only safe move there, and a
    # stale gate region inside a foreign hook is surfaced loudly rather than
    # silently rewritten around someone else's edits.
    ours_wholesale = existing_text.startswith(_BODY_HEADER)
    if ours_wholesale:
        desired = _hook_body(gates)
        if existing_text == desired:
            print(f"{_PROG}: gate already installed and current at {hook_path} — no-op.", file=sys.stderr)
            return 0
        _atomic_write(hook_path, desired)
        print(f"{_PROG}: refreshed stale gate body at {hook_path}.", file=sys.stderr)
        return 0

    if not missing_gates:
        stale = [g for g in gates if not _gate_region_is_current(existing_text, g)]
        if stale:
            print(
                f"{_PROG}: WARNING: {hook_path} is a custom hook whose gate region(s) "
                f"[{', '.join(g.marker for g in stale)}] are STALE — this installer will not "
                "rewrite around hand-edited content. Remove the gate block(s) and re-run to "
                "get the current version.",
                file=sys.stderr,
            )
            return 1
        print(f"{_PROG}: gate already installed and current at {hook_path} — no-op.", file=sys.stderr)
        return 0

    base = _strip_trailing_exit0(existing_text)
    addition_lines: List[str] = [""]
    if "_py=" not in existing_text:
        addition_lines.append(_py_resolve_line())
        addition_lines.append("")
    for gate in missing_gates:
        addition_lines.extend(_gate_block(gate))
        addition_lines.append("")

    new_content = base + "\n".join(addition_lines) + "\nexit 0\n"
    _atomic_write(hook_path, new_content)
    print(
        f"{_PROG}: appended gate(s) [{', '.join(g.marker for g in missing_gates)}] at {hook_path}.",
        file=sys.stderr,
    )
    return 0


def _resolve_claude_klabauter_target(target: str) -> Optional[str]:
    """Identity guard: resolve `target` to claude-klabauter's own repo root, or
    None if `target` is not a git repo / not claude-klabauter."""
    toplevel = _git_toplevel(target)
    if toplevel is None:
        print(f"{_PROG}: {target} not in a git repo — skipping.", file=sys.stderr)
        return None
    if not toplevel:
        print(f"{_PROG}: empty repo root — skipping.", file=sys.stderr)
        return None

    if _canon(toplevel) != _canon(_self_repo_root()):
        print(f"{_PROG}: not claude-klabauter ({toplevel}) — skipping.", file=sys.stderr)
        return None
    return toplevel


def main(argv: List[str]) -> int:
    target = argv[0] if argv else "."
    repo_root = _resolve_claude_klabauter_target(target)
    if repo_root is None:
        return 0
    return _install_or_append_hook(repo_root, _GATE_REGISTRY)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
