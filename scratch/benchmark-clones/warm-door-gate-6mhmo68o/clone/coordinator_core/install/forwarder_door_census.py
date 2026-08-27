"""
coordinator_core.install.forwarder_door_census — the corrected door-eligibility census.

Purpose: classify every generator-known `coordinator/bin/` CLI (the same
installed-name -> on-disk-target map `_derive_agent_helper_target_map`
already derives for forwarder generation) on the TWO independent axes
DR-365's re-review named, recording per row which axis fails rather than
collapsing to a single pass/fail:

    (a) op-equivalent   — is this CLI's own logic reachable through the
                           warm engine's *existing op surface*, or does it
                           do real client-side computation (filesystem
                           stat/walk, subprocess spawning) the engine does
                           not already expose as an op? The canonical
                           (a)-failure is `cross-repo-memo list`, whose own
                           module comment says the op "deliberately does
                           not provide" mtime-based age/stale/sort — the
                           CLI reproduces it itself with a stat pass
                           (`coordinator/bin/cross-repo-memo.py`, the
                           `mtime = os.stat(path).st_mtime` block).
    (b) warm-loadable    — does the module load and run cleanly inside the
                           warm server process? Import-time side effects,
                           module-level I/O, interpreter-global mutation,
                           and a hard `sys.exit`/`raise SystemExit` reached
                           at MODULE-EXEC time (not inside a function body —
                           `invoke_from_argv._run_entrypoint` already
                           contains a `SystemExit` raised *from inside*
                           `main()`, see that module's OP-BOUNDARY
                           CONTAINMENT docstring; what it cannot contain is
                           one reached while `exec_module` is still running
                           the file's own top-level statements) are all
                           (b)-failures.

Both axes are evaluated by a static, single-file AST scan of the resolved
`coordinator/bin/<target>` script — no import, no execution, no transitive
scan of what that file itself imports (a real but named limitation: see
`_WARM_UNSAFE_TOP_LEVEL_LIMITATION` below).

Four buckets, per DR-365 (`docs/decisions/DR-365-ruling-2-governs-every-
managed-launcher-class.md`) and the C2 dispatch brief:

    door-eligible       — (a) and (b) both pass.
    needs-op-extension  — (a) fails: the engine could return what the
                           client computes, but does not yet.
    needs-warm-safety   — (b) fails: fixable, but per-CLI work.
    engine-unreachable  — neither passes.

BUCKETS ARE A PERFORMANCE AXIS, NOT A COVERAGE AXIS. Every one of the
generator-known names gets a native launcher image under chunk C0's
name-aware cold leg regardless of bucket (AC16/AC18) — the bucket decides
only whether a call is served warm (door-eligible, loaded into
`invoke_from_argv`'s allowlist) or degrades to that name's own Python CLI
on a warm miss. `engine-unreachable` is NOT "exempt" from anything; it
still gets a native launcher, it just cannot get warm service by
dispatching an op.

Negative-spec (RAG-bait):
    This module does NOT count files installed under any settings-home
    `bin/` — that is the exact blindness the `.ps1` split census exposed (a
    file-count census reads that leg as zero when it in fact emits 393
    entries and then unlinks them behind a RED policy gate; see
    `coordinator_core/install/substrate.py :: _write_ps1_policy_status` and
    `_PS1_POLICY_STATUS_FILENAME`, written to `bin_dst.parent` — the
    settings-home ROOT, not `bin/` — as
    `ps1-policy-gate-status.json`). This census reads exclusively from
    GENERATOR STATE — `_derive_agent_helper_target_map(coordinator/bin)` —
    and each forwarder's on-disk TARGET script, never from an install
    output directory. If a caller wants the `.ps1` leg's own status, the
    positive evidence source is `ps1-policy-gate-status.json` at the
    settings-home root; an absent `bin_dst` is "emitted-then-rolled-back",
    never "never built" — `render_table` surfaces this as a footer note
    when that file is reachable, and says nothing when it is not (this
    module is not itself an install-time reader and never fails for a
    missing settings-home).

    This module does NOT gate `SystemExit`/exception containment at the op
    boundary — that already exists in `invoke_from_argv._run_entrypoint`.
    A (b)-failure recorded here is about module-EXEC-time safety, not
    about whether a later call into `main()` can raise safely (it can;
    that is contained already).

    This module does NOT decide which launcher class a name gets — C0/C1
    already build the name-aware native launcher for every name regardless
    of bucket. This module only measures the warm-eligibility axis C1's
    allowlist gates on.

Spec backlink: state/dispatch-briefs/2026-08-26-every-forwarder-that-can-reach-the-door-does/C2.md
Spec backlink: docs/decisions/DR-365-ruling-2-governs-every-managed-launcher-class.md
"""

from __future__ import annotations

import ast
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Sequence
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BIN_DIR = _REPO_ROOT / "coordinator" / "bin"
_ALLOWLIST_PATH = _REPO_ROOT / "coordinator_core" / "ops" / "warm_entrypoint_allowlist.json"
_PS1_POLICY_STATUS_FILENAME = "ps1-policy-gate-status.json"

#: The bare name every caller types. Resolution of THIS name is what decides
#: whether a call reaches the ~2.34ms native relay or an interpreter start.
_DOOR_STEM = "coordinator-invoke"

#: PowerShell consults a same-directory `.ps1` BEFORE PATHEXT's own order --
#: it is not in PATHEXT and `shutil.which` will never report it. It is the
#: sibling `install_warm_door :: claim_bare_name` exists to strip, so the
#: resolver below must model it or it cannot see the original hazard.
#:
#: VERIFIED (2026-08-27) on this PowerShell host, not assumed: a directory
#: containing ONLY `zzprobe.ps1` and `zzprobe.cmd`, prepended to PATH.
#:   `$env:PATHEXT -split ';' -contains '.PS1'`  ->  False   (.ps1 is NOT in PATHEXT)
#:   bare `zzprobe`                              ->  WINNER=ps1
#:   `Get-Command zzprobe -All | % Source`        ->  ...\zzprobe.ps1
#:                                                     ...\zzprobe.cmd
#: A non-PATHEXT-listed `.ps1` beats a PATHEXT-listed `.cmd`, confirming the
#: claim below empirically rather than by citation of `about_Command_Precedence`
#: alone.
_POWERSHELL_FIRST_EXT = ".ps1"

#: A bare-name winner with any of these suffixes -- or one sitting under a
#: Python `Scripts/` directory (a pip console-script shim) -- starts an
#: interpreter per call rather than relaying natively.
_INTERPRETER_START_SUFFIXES = (".py", ".ps1", ".cmd", ".bat")


def _settings_home_root() -> Path:
    """`bin_dst.parent` in `substrate.py :: _ps1_policy_status_path` terms --
    the settings-home ROOT, one level above its `bin/`. Read the same
    `COORDINATOR_SETTINGS_HOME` env var the rest of this codebase's
    forwarders honor, falling back to the documented default. This module
    never fails for the settings home being absent -- see `render_table`,
    which only consults this path opportunistically."""
    env = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if env:
        return Path(env)
    return Path.home() / ".coordinator-claude-settings"

#: Client-side-computation markers for axis (a). Any of these dotted-call
#: heads found ANYWHERE in the script's body (module scope or inside a
#: function) mark the CLI as doing real client-side work the warm engine's
#: existing op surface does not already expose — filesystem stat/walk and
#: subprocess spawning are exactly the shapes DR-365's cited example
#: (`cross-repo-memo list`'s own mtime pass) does. This is a proxy, not a
#: registry cross-reference against the live op set: a CLI that merely
#: calls an EXISTING op to get this same data would still be flagged here,
#: which is the conservative (needs-op-extension, never a false
#: door-eligible) direction to err in.
_CLIENT_SIDE_WORK_MARKERS = (
    "os.walk",
    "os.listdir",
    "os.scandir",
    "os.stat",
    "os.path.getmtime",
    "os.path.getctime",
    "os.path.getatime",
    ".rglob",
    ".iterdir",
    ".glob",
    ".stat",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
)

#: Dotted-call heads that mark a hard, module-EXEC-time side effect for
#: axis (b) when reached OUTSIDE the `if __name__ == "__main__":` guard
#: (which never executes under `importlib.util.spec_from_file_location` +
#: `exec_module` — see `invoke_from_argv._load_entrypoint_main`'s
#: docstring). Bare top-level `Expr(Call(...))` statements (a call whose
#: return value is discarded) are flagged unconditionally alongside these,
#: since a call made purely for its side effect is definitionally one.
#: Module-exec-time shapes `invoke_from_argv._run_entrypoint` CONTAINS, and
#: which therefore do not disqualify a CLI from being warm-served. Each entry
#: names the containment that neutralises it, because an entry added here
#: without one is how a real hazard gets waved through:
#:
#:   sys.path.insert / *_engine_on_path()  -- `sys.path` is snapshotted before
#:       the module exec and restored in the `finally`, so the shared server's
#:       import path is identical before and after.
#:   sys.exit / exit / raise SystemExit    -- caught as `SystemExit` at the op
#:       boundary and converted to an ordinary `exit_code`.
#:   print / sys.stdout / sys.stderr writes / traceback.print_exc
#:                                         -- both streams are redirected into
#:       the response buffers; a warm worker's real streams are never written.
#:
#: NOT contained, and deliberately absent: `os.chdir` (the caller's cwd is
#: already restored per call, but a chdir DURING exec races other requests in
#: the same process), `logging.basicConfig`, `warnings.filterwarnings`,
#: `random.seed` -- each mutates interpreter-global state no `finally` here
#: puts back.
_CONTAINED_EXEC_TIME_HAZARD_SUBSTRINGS = (
    "sys.path.insert",
    "sys.path.append",
    "_engine_on_path",
    "ensure_engine_on_path",
    "sys.exit",
    "raise SystemExit",
    "print()",
    "print(",
    "sys.stdout.write",
    "sys.stderr.write",
    "traceback.print_exc",
)


def _is_contained_hazard(hazard: str) -> bool:
    """True when `hazard` (one `_scan_top_level_exec_hazards` string) names a
    shape the op boundary neutralises -- see
    `_CONTAINED_EXEC_TIME_HAZARD_SUBSTRINGS`, which records the containment
    per shape rather than listing names to wave through."""
    return any(s in hazard for s in _CONTAINED_EXEC_TIME_HAZARD_SUBSTRINGS)


_HARD_EXEC_TIME_MARKERS = (
    "sys.exit",
    "exit",
    "os.chdir",
    "logging.basicConfig",
    "warnings.filterwarnings",
    "random.seed",
)

#: Named limitation: this scan reads only the resolved script's own AST. A
#: script that is safe at its own top level but imports a sibling module
#: with import-time side effects is not detected here — that would require
#: a transitive import graph walk across ~390 files' own dependency trees,
#: which this census does not attempt. Recorded per DR-365's own caution
#: against a census that implies more certainty than it has.
_WARM_UNSAFE_TOP_LEVEL_LIMITATION = (
    "single-file AST scan only — transitive import-time side effects in a "
    "script's own imports are not detected"
)


@dataclass(frozen=True)
class ForwarderVerdict:
    name: str
    target: str
    op_equivalent: bool
    warm_loadable: bool
    bucket: str
    op_equivalent_evidence: "tuple[str, ...]" = field(default_factory=tuple)
    warm_loadable_evidence: "tuple[str, ...]" = field(default_factory=tuple)
    scan_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target": self.target,
            "op_equivalent": self.op_equivalent,
            "warm_loadable": self.warm_loadable,
            "bucket": self.bucket,
            "op_equivalent_evidence": list(self.op_equivalent_evidence),
            "warm_loadable_evidence": list(self.warm_loadable_evidence),
            "scan_error": self.scan_error,
        }


def _is_main_guard(node: ast.stmt) -> bool:
    """True for `if __name__ == "__main__":` (either operand order) --
    the one top-level conditional whose body never executes under
    `exec_module`, so statements inside it are not exec-time hazards."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    operands = [test.left, test.comparators[0]]
    names = [o for o in operands if isinstance(o, ast.Name) and o.id == "__name__"]
    consts = [o for o in operands if isinstance(o, ast.Constant) and o.value == "__main__"]
    return bool(names) and bool(consts)


def _dotted_call_head(node: ast.expr) -> Optional[str]:
    """Reconstructs a dotted call-target string (`"os.stat"`, `"exit"`) from
    a `Call.func` node, or `None` for a shape this doesn't recognize
    (e.g. a call through a subscript or a computed attribute)."""
    parts: "list[str]" = []
    cur = node
    while isinstance(cur, (ast.Attribute, ast.Call)):
        if isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        else:
            cur = cur.func
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    if parts:
        # Chain rooted in something other than a bare Name (e.g.
        # `Path('.').rglob(...)`, rooted in a Call) -- still return the
        # trailing attribute chain so an `endswith` marker match (".rglob")
        # can fire; a leading-anchored match ("os.stat") will not.
        return ".".join(reversed(parts))
    return None


def _scan_client_side_work(tree: ast.Module) -> "tuple[str, ...]":
    hits: "list[str]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        head = _dotted_call_head(node.func)
        if head is None:
            continue
        for marker in _CLIENT_SIDE_WORK_MARKERS:
            if head == marker or head.endswith(marker):
                hits.append(f"{marker} @ line {node.lineno}")
                break
    return tuple(dict.fromkeys(hits))


def _scan_top_level_exec_hazards(tree: ast.Module) -> "tuple[str, ...]":
    """Walks ONLY module-scope statements (never descending into
    `FunctionDef`/`AsyncFunctionDef`/`ClassDef` bodies -- those run at
    CALL time, not at `exec_module` time), recursing into `Try` and
    non-guard `If` bodies since those DO run unconditionally or
    conditionally at exec time. Returns hazard descriptions, empty when
    none found."""
    hazards: "list[str]" = []

    def walk_top_level(stmts: "list[ast.stmt]") -> None:
        for node in stmts:
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # docstring
            if _is_main_guard(node):
                continue  # never executes under exec_module
            if isinstance(node, ast.If):
                walk_top_level(node.body)
                walk_top_level(node.orelse)
                continue
            if isinstance(node, ast.Try):
                walk_top_level(node.body)
                for handler in node.handlers:
                    walk_top_level(handler.body)
                walk_top_level(node.orelse)
                walk_top_level(node.finalbody)
                continue
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                hazards.append(f"interpreter-global mutation ({', '.join(node.names)}) @ line {node.lineno}")
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                head = _dotted_call_head(node.value.func) or "<call>"
                hazards.append(f"module-exec-time call {head}() @ line {node.lineno}")
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    head = _dotted_call_head(sub.func)
                    if head is None:
                        continue
                    for marker in _HARD_EXEC_TIME_MARKERS:
                        if head == marker or head.endswith(marker):
                            hazards.append(f"{marker}(...) reached at module-exec time @ line {sub.lineno}")
                            break
                if isinstance(sub, ast.Raise) and isinstance(sub.exc, (ast.Call, ast.Name)):
                    exc_name = sub.exc.func.id if isinstance(sub.exc, ast.Call) and isinstance(sub.exc.func, ast.Name) else getattr(sub.exc, "id", None)
                    if exc_name == "SystemExit":
                        hazards.append(f"raise SystemExit reached at module-exec time @ line {sub.lineno}")

    walk_top_level(tree.body)
    return tuple(dict.fromkeys(hazards))


def classify_one(name: str, target: str, bin_dir: Optional[Path] = None) -> ForwarderVerdict:
    """Classifies a single generator-known forwarder. `target` is the
    on-disk filename `_derive_agent_helper_target_map` resolved (relative
    to `bin_dir`, default `coordinator/bin/`), never the installed NAME
    (they differ for a `.py`-suffixed on-disk file)."""
    resolved_bin_dir = bin_dir if bin_dir is not None else _BIN_DIR
    script = resolved_bin_dir / target
    try:
        source = script.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(script))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return ForwarderVerdict(
            name=name,
            target=target,
            op_equivalent=False,
            warm_loadable=False,
            bucket="engine-unreachable",
            scan_error=f"{type(exc).__name__}: {exc}",
        )

    op_evidence = _scan_client_side_work(tree)
    warm_evidence = _scan_top_level_exec_hazards(tree)
    op_equivalent = not op_evidence

    # A hazard the op boundary CONTAINS is not a reason to exclude a CLI --
    # it is a reason the containment exists. `_run_entrypoint` snapshots and
    # restores `sys.path` around the module exec, catches `SystemExit` and
    # every `Exception`, and redirects stdout/stderr into the response, so
    # the shapes in `_CONTAINED_EXEC_TIME_HAZARDS` cannot reach the shared
    # warm server. Classifying on the raw scan instead condemned 315 of 384
    # CLIs on one shared bootstrap line and reported 3% coverage as if it
    # were the ceiling.
    #
    # The scan output is still recorded per row: containment is a property of
    # the loader, so if that containment is ever narrowed, this evidence is
    # what re-derives which names were relying on it.
    uncontained = tuple(h for h in warm_evidence if not _is_contained_hazard(h))
    warm_loadable = not uncontained

    # DOOR-ELIGIBILITY IS AXIS (b) ALONE. Axis (a) -- op-equivalence -- asks
    # whether a CLI's whole behaviour is reachable by dispatching an OP, and
    # that is not the mechanism the door uses. `invoke.from_argv` runs the
    # CLI's OWN `main(argv)` inside the warm process, so a CLI that does real
    # client-side work does that work warm, in Python, exactly as it does
    # cold -- it simply stops paying an interpreter start to get there.
    # Gating warm service on (a) condemned 104 CLIs for doing legitimate
    # local work the warm process is perfectly able to do.
    #
    # (a) is still SCANNED and recorded per row, because it answers a real
    # and different question: which CLIs could one day be replaced by a
    # server-side op rather than merely hosted warm. It is a roadmap axis,
    # never a coverage one.
    if warm_loadable:
        bucket = "door-eligible"
    else:
        bucket = "needs-warm-safety"

    return ForwarderVerdict(
        name=name,
        target=target,
        op_equivalent=op_equivalent,
        warm_loadable=warm_loadable,
        bucket=bucket,
        op_equivalent_evidence=op_evidence,
        warm_loadable_evidence=warm_evidence,
    )


def run_census(bin_dir: Optional[Path] = None) -> "list[ForwarderVerdict]":
    """Classifies every name `_derive_agent_helper_target_map` derives from
    `bin_dir` (default `coordinator/bin/`, i.e. GENERATOR STATE -- never an
    installed settings-home `bin/`). Returns verdicts sorted by name."""
    from coordinator_core.install.substrate import _derive_agent_helper_target_map

    resolved_bin_dir = bin_dir if bin_dir is not None else _BIN_DIR
    target_map = _derive_agent_helper_target_map(resolved_bin_dir)

    return [
        classify_one(name, target_map[name], bin_dir=resolved_bin_dir)
        for name in sorted(target_map)
    ]


def bucket_counts(verdicts: "list[ForwarderVerdict]") -> "dict[str, int]":
    counts = {"door-eligible": 0, "needs-op-extension": 0, "needs-warm-safety": 0, "engine-unreachable": 0}
    for v in verdicts:
        counts[v.bucket] += 1
    return counts


def door_eligible_names(verdicts: "list[ForwarderVerdict]") -> "tuple[str, ...]":
    return tuple(sorted(v.name for v in verdicts if v.bucket == "door-eligible"))


def to_json(verdicts: "list[ForwarderVerdict]") -> str:
    payload = {
        "$comment": (
            "Re-runnable door-eligibility census (chunk C2). Classified from "
            "generator state (coordinator/bin/) and each forwarder's own "
            "on-disk target, never from an installed settings-home bin/ "
            "listing. BUCKETS ARE A PERFORMANCE AXIS, NOT A COVERAGE AXIS -- "
            "every name still gets a native launcher image under C0's "
            "name-aware cold leg regardless of bucket; the bucket decides "
            "only whether a call is served warm or degrades to that name's "
            "own Python CLI."
        ),
        "counts": bucket_counts(verdicts),
        "rows": [v.to_dict() for v in verdicts],
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def resolve_bare_name(stem: str, path_dirs: Sequence[str], pathext: str) -> "list[Path]":
    """Every file `stem` resolves to across `path_dirs`, in the order Windows
    would try them: PATH directories outermost, and within each directory
    PowerShell's `.ps1` first, then PATHEXT's own order, then the extensionless
    file (which cmd tries only after PATHEXT).

    PURE over its arguments -- takes PATH and PATHEXT rather than reading the
    environment -- so the ordering logic is testable on a machine that has no
    door installed. `bare_name_door_report` is the impure caller that supplies
    the live environment.

    `shutil.which` is deliberately not used: it answers for the CURRENT
    process's rules, and the callers that matter are a PowerShell host and a
    `CreateProcess` from the engine -- one of which prefers an extension
    PATHEXT does not list at all.

    The `.ps1`-first claim is verified, not assumed -- see `_POWERSHELL_FIRST_EXT`'s
    comment for the captured `Get-Command`/PATHEXT trace."""
    exts = [_POWERSHELL_FIRST_EXT]
    exts += [e for e in pathext.split(os.pathsep) if e]
    exts.append("")

    hits: "list[Path]" = []
    for raw_dir in path_dirs:
        if not raw_dir:
            continue
        for ext in exts:
            candidate = Path(raw_dir) / f"{stem}{ext}"
            if candidate.is_file() and candidate not in hits:
                hits.append(candidate)
    return hits


def bare_name_starts_an_interpreter(winner: Path) -> bool:
    """True when `winner` costs an interpreter (or shell) start per call.

    Its own predicate rather than folded into the report, so two findings stay
    distinct: a bare name resolving to the WRONG door is one problem; one
    resolving to something that starts an interpreter ahead of warmth is a
    worse and more specific one -- break-class outright, per CLAUDE.md
    § The brightline."""
    if winner.suffix.lower() in _INTERPRETER_START_SUFFIXES:
        return True
    return winner.parent.name.lower() == "scripts"


def bare_name_door_report() -> "list[str]":
    """Report lines for what `coordinator-invoke`, typed bare, actually selects
    on THIS machine's PATH.

    WHY THIS IS A CENSUS LINE AND NOT A TEST (2026-08-27). The property is
    about the real environment, and `coordinator_core`'s suite quarantines
    `Path.home()` to a temp dir by design -- a pytest test of it can only ever
    SKIP, which reads as coverage and is not. `resolve_bare_name` carries the
    logic and is unit-tested; this carries the machine, and reports rather than
    asserts.

    THE LIVE HAZARD it exists to surface: a pip-installed console-script shim
    (`<python>/Scripts/coordinator-invoke.exe`) is present by construction --
    this package declares the console entry point. It loses to the settings-home
    door on PATH ORDERING ALONE. A `pip install --user`, a venv activation, or
    an installer that prepends flips it with no error and no runtime signal; the
    only symptom is ~94ms of interpreter start plus engine import per call
    against the door's ~2.34ms relay. `test_door_bare_name_ordering.py` pins the
    installer's own `.ps1` sequencing; nothing pinned this.

    NAMED GAP -- this function is a REPORT, not a GATE (2026-08-27). Nothing
    invokes it on a cadence that would catch a PATH-ordering flip before it
    ships silently to a user; it fires only on a manual `forwarder_door_census.main()`
    run. `coordinator_core`'s suite quarantines `Path.home()`, so a hermetic
    pytest gate cannot see this property -- but that only rules out a pytest
    gate, not every gate. An install-time check in `scripts/setup.py` (fail the
    install when this function's BROKEN/BREAK-CLASS lines fire against the
    just-installed environment) or a `doctor`-style probe run on a cadence
    outside pytest's quarantine would close this; neither exists yet. Until one
    does, this module documents the hazard on request -- it does not guard it."""
    door = _settings_home_root() / "bin" / f"{_DOOR_STEM}.exe"
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    hits = resolve_bare_name(
        _DOOR_STEM,
        path_dirs,
        os.environ.get("PATHEXT", ""),
    )

    lines = ["", "## Bare-name resolution", ""]
    if not door.is_file():
        lines.append(f"Door not installed at `{door}` -- nothing to resolve against.")
        return lines
    if not hits:
        lines.append(
            f"**BROKEN**: bare `{_DOOR_STEM}` resolves to NOTHING on this PATH, yet "
            f"the door is installed at `{door}`. Its `bin/` is not on PATH, so no "
            f"bare-name caller reaches the warm relay."
        )
        return lines

    winner = hits[0]
    lines.append(f"Winner: `{winner}`")
    if winner != door:
        lines.append(
            f"**BROKEN**: expected the settings-home door at `{door}`. Everything "
            f"typing the bare name reaches `{winner.name}` instead. Fix PATH "
            f"ORDERING -- repointing callers at an absolute path hides the same "
            f"breakage from every other caller."
        )
    if bare_name_starts_an_interpreter(winner):
        lines.append(
            f"**BREAK-CLASS**: `{winner}` starts an interpreter per call rather than "
            f"relaying natively (CLAUDE.md § The brightline)."
        )
    if winner == door and not bare_name_starts_an_interpreter(winner):
        lines.append("OK -- the bare name reaches the native door.")

    shadowed = [h for h in hits[1:] if bare_name_starts_an_interpreter(h)]
    if shadowed:
        lines.append("")
        lines.append("Interpreter-starting candidates LATER on PATH (ordering alone defuses them):")
        lines += [f"- `{h}`" for h in shadowed]

    sibling = door.with_suffix(_POWERSHELL_FIRST_EXT)
    door_bin_on_path = any(
        raw_dir and Path(raw_dir) == door.parent for raw_dir in path_dirs
    )
    if sibling.is_file() and door_bin_on_path and sibling not in hits:
        lines.append("")
        lines.append(
            f"**BROKEN**: `{sibling}` exists beside the door. PowerShell prefers a "
            f"same-directory `.ps1` over an `.exe` regardless of PATHEXT, so every "
            f"PowerShell caller is off the native relay. `install_warm_door :: "
            f"claim_bare_name` removes this -- it either did not run or did not take."
        )
    return lines


def render_table(verdicts: "list[ForwarderVerdict]") -> str:
    counts = bucket_counts(verdicts)
    lines = [
        "# Forwarder door-eligibility census",
        "",
        "BUCKETS ARE A PERFORMANCE AXIS, NOT A COVERAGE AXIS. Every one of the names below gets a",
        "native launcher image under C0's name-aware cold leg regardless of bucket -- the bucket",
        "decides only whether a call is served warm or degrades to that name's own Python CLI.",
        "",
        f"Total classified: {len(verdicts)}",
        "",
        "| bucket | count |",
        "|---|---|",
        f"| door-eligible | {counts['door-eligible']} |",
        f"| needs-op-extension | {counts['needs-op-extension']} |",
        f"| needs-warm-safety | {counts['needs-warm-safety']} |",
        f"| engine-unreachable | {counts['engine-unreachable']} |",
        "",
        "| name | target | (a) op-equivalent | (b) warm-loadable | bucket |",
        "|---|---|---|---|---|",
    ]
    for v in verdicts:
        lines.append(
            f"| {v.name} | {v.target} | {'PASS' if v.op_equivalent else 'FAIL'} | "
            f"{'PASS' if v.warm_loadable else 'FAIL'} | {v.bucket} |"
        )

    lines += bare_name_door_report()

    ps1_status = _settings_home_root() / _PS1_POLICY_STATUS_FILENAME
    if ps1_status.is_file():
        lines += [
            "",
            f"Note: `.ps1` leg policy-gate status found at `{ps1_status}` -- "
            "consult it directly for that leg's emitted-then-rolled-back "
            "state; this census does not read or summarize it.",
        ]

    return "\n".join(lines) + "\n"


def _write_allowlist(verdicts: "list[ForwarderVerdict]", allowlist_path: Path = _ALLOWLIST_PATH) -> "tuple[str, ...]":
    """Populates the committed warm-load allowlist from the door-eligible
    bucket, UNION'd with whatever names are already present (C1 seeds
    `cross-repo-memo`, its own proving CLI, unconditionally -- this
    function never removes an already-present name, it only adds
    door-eligible ones). Returns the resulting sorted entrypoint tuple."""
    existing: "tuple[str, ...]" = ()
    if allowlist_path.is_file():
        data = json.loads(allowlist_path.read_text(encoding="utf-8"))
        existing = tuple(data.get("entrypoints", ()))

    merged = tuple(sorted(set(existing) | set(door_eligible_names(verdicts))))
    payload = {
        "$comment": (
            "Committed warm-load allowlist for invoke.from_argv's "
            "params.entrypoint (coordinator_core/ops/invoke_from_argv.py). "
            "A name absent from this list refuses (fail closed) rather than "
            "warm-loading an unvetted CLI's module body into the shared "
            "server process ~50 concurrent sessions share. Populated "
            "(chunk C2) from forwarder_door_census.py's 'door-eligible' "
            "bucket, unioned with C1's seeded proving CLI -- see "
            "docs/research/spike-verdicts/2026-08-27-multi-name-native-"
            "invocation-surface.md, chunks C0-C2."
        ),
        "entrypoints": list(merged),
    }
    allowlist_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return merged


def main(argv: Optional["list[str]"] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    write_allowlist = "--write-allowlist" in argv
    fmt = "table"
    if "--json" in argv:
        fmt = "json"

    verdicts = run_census()

    if write_allowlist:
        merged = _write_allowlist(verdicts)
        print(f"[forwarder-door-census] allowlist populated with {len(merged)} entrypoint(s)", file=sys.stderr)

    print(to_json(verdicts) if fmt == "json" else render_table(verdicts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
