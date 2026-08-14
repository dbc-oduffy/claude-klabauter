"""
coordinator_core.ops.install_doe_claude_precommit_hook — pre-commit gate-chain
installer for DoE-claude, dispatched from claude-klabauter.

Purpose: installs (or appends onto an existing custom hook) DoE-claude's
`.git/hooks/pre-commit` gate chain. Idempotent, conditional: only installs
when the resolved target repo root IS DoE-claude itself. Consumer/sibling
repos (and any other git repo) are skipped cleanly.

Modelled closely on `coordinator_core.ops.install_claude_klabauter_precommit_hook` —
same registry-driven shape, same repo-root-relative gate resolution, same
exit-code clamping, same append-not-clobber behavior on a foreign existing
hook. Kept as an independent module for the same reason that one gives for
staying independent of `install_meta_repo_precommit_hook`: this installer's
gate-execution contract is deliberately duplicated rather than grafted onto
shared private plumbing, so a narrower contract does not risk the wider one
regressing back in on a future edit to shared code. Only `py_probe_sh` is
shared, and only because that module documents why.

Identity guard — the ONE load-bearing divergence from the claude-klabauter installer's
shape. `install_claude_klabauter_precommit_hook` resolves "is this the target repo"
from its OWN file location (`_self_repo_root()`), because that module ships
INSIDE claude-klabauter. This module does not ship inside DoE-claude — it
lives in claude-klabauter and targets a PEER repo — so there is no self-
relative anchor to read. Identity instead resolves through the canonical
DoE-root resolver, `coordinator_core.doe_root_pointer.read_doe_root_pointer()`
(registry-first four-tier chain over `repos.doe_claude`, DR-071). That
resolver returns `""` on an unresolved key and never raises (see its own
docstring's negative-spec) — this module treats an unresolved DoE root as a
CLEAN SKIP, exit 0, with a named advisory on stderr, never a traceback and
never a block: DoE-claude may legitimately not be registered on a given
machine, and that is not this installer's problem to fail on.

The resolution is exposed as its own module-level function
(`_resolve_doe_root()`), never inlined, so tests can monkeypatch it to point
at a throwaway `tmp_path` repo — the same seam `_self_repo_root()` exposes
in the claude-klabauter installer.

Gate registry (`_GATE_REGISTRY` below) starts with exactly one gate — DoE-
claude's doctrine-weight guard (`guard-doctrine-surface-ratio.py`, under
DoE-claude's own `coordinator/hooks/scripts/`, NOT `coordinator/bin/` — that
subdir holds DoE's *other* CLI surface, not its pre-commit gate scripts).
That script is DoE-claude's own deliverable and is NOT expected to exist in
THIS tree (claude-klabauter). Its absence at install time fires the ordinary
`_warn_if_gate_script_missing` ADVISORY on every run against this repo —
that is correct, not a defect to silence. Adding a future gate is a registry
entry, not a new code path.

`_BIN_SUBDIR` below is, exactly as in the claude-klabauter installer, the single
source of truth for BOTH the install-time existence check (an absolute path,
resolved against whatever repo root the caller supplies) AND the path
EMITTED into the hook body (repo-root-relative) — the two can never drift
apart. Unlike the claude-klabauter installer's `_bin_dir()` (self-relative, no
arguments, because that module's own location IS the target repo), this
module's bin-dir helper is parameterised on the ALREADY-RESOLVED target repo
root, because this module has no self-relative anchor into DoE-claude's tree
to read.

Exit-code clamping: identical contract and identical rationale to
`install_claude_klabauter_precommit_hook`'s own "Exit-code clamping" section — a
pre-commit hook exiting anything other than 0 or 1 is read by the Claude
Code harness as a blocking DENY that kills Bash/Write/Edit together,
INCLUDING the tools needed to repair the hook (bricked the primary macOS box
four times on 2026-07-28). Every gate block here captures `$?` into a
variable and re-derives the branch from it explicitly: exactly 0 continues,
anything else collapses uniformly to a CANNOT-PROCEED / BLOCKED exit 1 —
never a propagated raw code.

Negative-spec:
    - Never emits a hook body that can exit anything other than 0 or 1 — see
      "Exit-code clamping" above and this module's own tests
      (`test_gate_script_exit_2_is_clamped_to_1`,
      `test_hook_body_never_contains_a_bare_exit_dollar_question`).
    - No `bash` anywhere in the emitted hook body — the shim is `#!/bin/sh`
      and execs `$_py` directly, per claude-klabauter's CLAUDE.md § Runtime
      conventions.
    - Does not raise, and does not block, on an unresolved `repos.doe_claude`
      — that is a clean advisory skip (AC3), never a traceback.
    - Does not author or require `guard-doctrine-surface-ratio.py` — that
      script is DoE-claude's own deliverable, not this module's to write.
    - Does not install into DoE-claude's real, live tree, and does not
      install into claude-klabauter's own live repo either — this module and
      its CLI are built and tested against `tmp_path` throwaway repos only;
      installing/verifying the live artifact is an EM/PM-gated external
      action left to the invoking operator.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from coordinator_core import py_probe_sh as _py_probe_sh
from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.session.declared_writes import declare_write
# Cross-package import of the SSOT doc-pointer display string (same
# precedent write_guards already uses for operator_override_note itself) --
# emitted hook-body remediation text points readers at the doc that
# enumerates these keys, never names a key inline (B6/B8, see
# docs/wiki/guard-messaging.md § Register). Repo-qualified ("claude-klabauter
# <path>"), so it stays fleet-addressable when this hook fires inside
# DoE-claude's own tree, not just claude-klabauter's.
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY

GENERATES = []  # writes only DoE-claude's own .git/hooks/pre-commit, never a tracked path

_PROG = "install-doe-claude-precommit-hook"


@dataclass(frozen=True)
class _Gate:
    marker: str        # presence key: substring searched for in the hook body to find this gate's region
    filename: str       # bin-dir filename (relative to the resolved gate-script dir)
    label: str          # human label for the BLOCKED banner
    override_env: str   # env var that bypasses a CANNOT-RUN/CANNOT-PROCEED block


_GATE_REGISTRY: List[_Gate] = [
    _Gate(
        marker="guard-doctrine-surface-ratio",
        filename="guard-doctrine-surface-ratio.py",
        label="doctrine-surface-ratio",
        override_env="COORDINATOR_OVERRIDE_PRECOMMIT_DOCTRINE_SURFACE_RATIO",
    ),
]


#: Gate-script directory as repo-root-relative POSIX segments, inside
#: DoE-claude's own tree. Single source of truth for BOTH the install-time
#: existence check (resolved absolute against the target repo root) and the
#: path EMITTED into the hook body (relative, see `_gate_block`) — so the two
#: can never drift apart. NOT `coordinator/bin/` — that is DoE-claude's
#: general CLI surface; its pre-commit gate scripts live under
#: `coordinator/hooks/scripts/`.
_BIN_SUBDIR = ("coordinator", "hooks", "scripts")


def _bin_dir(repo_root: str) -> Path:
    """The directory holding DoE-claude's pre-commit gate scripts, resolved
    against the ALREADY-RESOLVED `repo_root` (DoE-claude's own root, per
    `_resolve_doe_claude_target`). Unlike the claude-klabauter installer's `_bin_dir()`
    — which is self-relative because that module ships inside its own
    target — this module ships in claude-klabauter and has no self-relative
    anchor into DoE-claude's tree, so the target repo root is threaded in
    explicitly rather than derived from `__file__`.

    Used for the INSTALL-TIME existence check only — never for the path
    written into the hook. See `_gate_block` for why the emitted path is
    relative instead."""
    return Path(repo_root).joinpath(*_BIN_SUBDIR)


def _resolve_doe_root() -> str:
    """The DoE-claude repo root, per the canonical registry-first resolver —
    the identity anchor for "is the target DoE-claude". Exposed as its own
    function (rather than called inline) so tests can monkeypatch it to
    point at a throwaway `tmp_path` repo, the same way `_self_repo_root()`
    is independently monkeypatchable in the claude-klabauter installer. Returns `""`
    on an unresolved `repos.doe_claude` — never raises (see
    `coordinator_core.doe_root_pointer`'s own negative-spec) — and that is
    treated as a clean advisory skip by the caller, not an error here."""
    return read_doe_root_pointer()


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
    try:
        from coordinator_core.win_portability import no_console_creationflags

        result = subprocess.run(
            ["git", "-C", target, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _git_toplevel: subprocess.run failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    toplevel = result.stdout.strip()
    return toplevel or None


def _atomic_write(path: str, content: str) -> None:
    """Write `content` to `path` atomically: write to a `.tmp.<pid>` sibling,
    then rename, so a concurrent git-commit never reads a torn hook. Chmods
    0o755 after on POSIX, where a git hook must be executable to fire at
    all — the POSIX exec bit is meaningless on Windows (git for Windows
    invokes the hook via its own shebang-aware shell layer regardless), so
    the chmod is skipped there rather than issued as a harmless no-op."""
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
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


def _py_resolve_line() -> str:
    """POSIX `sh` interpreter probe, resolved in-order (python3, python, py)
    and skipping any hit resolved under `WindowsApps` — shared with the
    other two installers via `coordinator_core.py_probe_sh` rather than
    re-hand-rolled here; see that module's docstring for the "why one shared
    implementation" reasoning, and this module's own docstring for why only
    THIS probe is shared while the surrounding gate-block machinery stays
    independently duplicated."""
    return _py_probe_sh.python_probe_lines("_py")


def _gate_block(gate: _Gate) -> List[str]:
    """Emit the runtime lines for one gate.

    Three CANNOT-PROCEED cases, each honoring `gate.override_env` and each
    ending in exactly `exit 1` (never a propagated raw code — see module
    docstring's "Exit-code clamping"):
      1. missing script
      2. missing interpreter
      3. the gate script itself exited nonzero (a real finding, OR its own
         transport-layer failure — both collapse to the same BLOCKED/exit-1
         shape here; the gate script's own stderr, already flushed to the
         terminal by the time this branch runs, carries the distinguishing
         detail)

    A clean gate run (`$_gate_rc` = 0) falls through with no output.

    The emitted gate path is REPO-ROOT-RELATIVE, not the absolute path
    `_bin_dir()` resolves. git runs `pre-commit` with cwd at the top level of
    the working tree, so a relative path resolves correctly AND survives the
    clone being moved or renamed. An absolute literal baked at install time
    does not: relocate the checkout and every commit BLOCKs on a missing gate
    script until the installer is re-run. That is the same defect class that
    silently disarmed all four meta-repo gates when the executable surface
    moved at `b644d5a9`, so it is not repeated here — see
    `install_claude_klabauter_precommit_hook._gate_block`'s docstring for the fuller
    account; this module inherits the same property for the same reason.
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
        "no python interpreter found (python3/python/py) on PATH", "install Python, then retry"
    )
    lines += [
        "else",
        '  "$_py" "$_gate_script"',
        "  _gate_rc=$?",
        '  if [ "$_gate_rc" -ne 0 ]; then',
    ]
    lines += _cannot_proceed_branch(
        'gate reported a problem (exit code $_gate_rc) -- see output above',
        "fix the flagged issue",
    )
    lines += ["  fi", "fi"]
    return lines


#: Opening lines of a body THIS module wrote. Used as the authorship test in
#: `_install_or_append_hook`: a hook starting with this is ours end to end and
#: may be rewritten wholesale when stale; anything else is treated as a custom
#: hook and only ever appended to.
_BODY_HEADER = (
    "#!/bin/sh\n"
    "# DoE-claude pre-commit gates — fire before doctrine drift can land.\n"
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
        "# Registry-driven (coordinator_core.ops.install_doe_claude_precommit_hook);",
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


def _warn_if_gate_script_missing(repo_root: str, gates: List[_Gate]) -> None:
    """Best-effort INSTALL-TIME advisory: if a registered gate's script is
    not actually present at the resolved bin dir right now, say so loudly at
    install time rather than only discovering it at the next commit's
    runtime `[ -f ]` check (see `_gate_block`, which is the actual
    enforcement point — this is a heads-up, not a gate). `guard-doctrine-
    surface-ratio.py` is DoE-claude's own deliverable and is not expected to
    exist in claude-klabauter's tree; when this installer is exercised against
    THIS repo (never done in a real run — see module docstring), this
    ADVISORY fires on every call, which is correct behavior, not a defect."""
    bin_dir = _bin_dir(repo_root)
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
    _warn_if_gate_script_missing(repo_root, gates)

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

    # Marker-presence alone is NOT "up to date" — see
    # `install_claude_klabauter_precommit_hook._install_or_append_hook`'s own comment
    # here (2026-07-28) for the empirical incident this guards against.
    # A hook this installer wrote is compared against what it WOULD write
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


def _resolve_doe_claude_target(target: str) -> Optional[str]:
    """Identity guard: resolve `target` to DoE-claude's own repo root, or
    None if `target` is not a git repo / not DoE-claude / DoE-claude's root
    cannot presently be resolved at all (AC3 — a clean advisory skip, never a
    block)."""
    toplevel = _git_toplevel(target)
    if toplevel is None:
        print(f"{_PROG}: {target} not in a git repo — skipping.", file=sys.stderr)
        return None
    if not toplevel:
        print(f"{_PROG}: empty repo root — skipping.", file=sys.stderr)
        return None

    doe_root = _resolve_doe_root()
    if not doe_root:
        print(
            f"{_PROG}: ADVISORY: repos.doe_claude is unresolved (registry key and both "
            "pointer-file rungs are empty) — skipping install; DoE-claude may legitimately "
            "not be registered on this machine, this is a clean no-op, not a failure.",
            file=sys.stderr,
        )
        return None

    canon_doe_root = _canon(doe_root)
    if not canon_doe_root:
        print(
            f"{_PROG}: ADVISORY: repos.doe_claude resolves to {doe_root!r}, which does not "
            "exist on disk — skipping install; the registry pointer is stale, re-run the "
            "coordinator installer to refresh it.",
            file=sys.stderr,
        )
        return None

    if _canon(toplevel) != canon_doe_root:
        print(f"{_PROG}: not DoE-claude ({toplevel}) — skipping.", file=sys.stderr)
        return None
    return toplevel


def main(argv: List[str]) -> int:
    target = argv[0] if argv else "."
    repo_root = _resolve_doe_claude_target(target)
    if repo_root is None:
        return 0
    return _install_or_append_hook(repo_root, _GATE_REGISTRY)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
