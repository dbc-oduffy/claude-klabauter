r"""
coordinator_core.tests.test_no_machine_absolute_path_resolution — corpus gate:
no machine-absolute path literal in Python source may reach a host-touching
API.

PM ruling (2026-08-26): **machine-absolute locations must not be hardcoded in
our code at all** — not the Windows box's root, not the Mac's. Every location
has a resolution ladder (flag -> env -> registry -> discovery); a hardcoded
literal bypasses all of it.

What provoked this gate
------------------------
`coordinator_core/ops/session/tests/test_warm_start_import_cycle.py` passed
`cwd="X:/claude-klabauter"` to `subprocess.run`. `X:\claude-klabauter` is the
Windows box's REAL engine root, so the literal was correct on the machine that
wrote it and `FileNotFoundError`d on every POSIX box. The tests carrying it are
marked `spawns_process` + `cadence`, so no routine run ever hit it. Same
incident shape as 2026-07-28 (a POSIX host's `settings.json` rewritten with
Windows drive-letter hook paths, bricking every hook with zero signal) in a
surface no guard covered: Python source.

Why this is a NEW gate and not a widened old one
-------------------------------------------------
Detection of the *shape* already exists and is reused here, not copied:
`_path_shape_regexes.WIN_DRIVE_RE` (whose lookbehind keeps `https://` from
reading as drive `s:`). `guard_concrete_path_citations` already fires on both
provoking literals — but its write-time leg is ADVISORY and delta-scoped
(`new_violations`), so it never sees a literal that is already in the tree, and
`guard_foreign_platform_paths` scans JSON configs and CLAUDE.md prose only.
What was missing is not a detector; it is (a) a standing gate over the existing
corpus and (b) the executable/prose discriminator that lets such a gate survive.

The discriminator: RESOLUTION, not resemblance
-----------------------------------------------
A drive-letter literal appears ~1000 times in this repo's `.py` files and
almost none of them are defects. Firing on resemblance would fire on
legitimate work, and a guard that fires on legitimate work is switched off
within a day (the exemption lesson that decided the session-hub litter guard).
So this gate fires on exactly one structural property, established by AST, not
by pattern-guessing at intent:

    the literal is passed to an API that touches THIS HOST's filesystem or
    interpreter state.

`subprocess.run(cwd=...)`, `os.chdir`, `sys.path.insert`, `open`,
`Path(...).is_dir()` — those resolve against the machine the process is
running on, so a foreign-machine literal there is broken by construction.
Everything else stays silent, and does so for a stated reason rather than by
being listed:

  - **Guard-fixture literals** — `test_no_machine_absolute_path_in_guard_
    messages.py`, `test_write_bump_path_translation.py`, `guard_foreign_
    platform_paths`'s own tests deliberately construct a foreign path to test
    the detector. That path is an INPUT to a pure function and never resolved,
    so it never reaches a sink. It keeps its literal with no allowlist entry.
  - **Docstring and comment prose** — `#`-comments are not AST constants at
    all, and a docstring `Expr` is not an argument to anything.
  - **Synthetic registry/config values** (`test_engine_root_census.py`) —
    written into a `tmp_path` fixture, compared as strings; never resolved.
  - **Message-rendering fixtures** (`test_write_bump_message.py`'s
    `_LONG_RESOLVED_NATIVE_PATH`) — fed to a formatter and asserted in its
    output.

The exemption model is an EXACT-SITE allowlist (`_ALLOWED_SITES`), never a
directory or filename pattern: a `/fixtures/` style match is what let this
fleet's `.sh`/`.bats` exclusion silently grow before. It is empty at
introduction — every site this gate found was fixed rather than listed, which
is the state a named allowlist should start in.

Scope: live code roots
-----------------------
`coordinator_core/`, `coordinator/`, `scripts/`, `tasks/` — the code that runs
on a host. `state/` is a records corpus (frozen crash scratchpads, one-shot
audit probes, subagent-share transcripts) whose exemption policy is
`guard_concrete_path_citations`' to set, not this gate's to invent; its
residue is reported to the owning EM rather than silently swallowed here.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytest

from coordinator_core.ops.session._path_shape_regexes import WIN_DRIVE_RE
from coordinator_core.ops.session.guard_concrete_path_citations import (
    _is_placeholder_segment,
)
from coordinator_core.win_portability import leaf_spawn_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Roots holding code that RUNS on a host. See the module docstring's
#: "Scope" section for why `state/` is out.
_LIVE_ROOTS: Tuple[str, ...] = (
    "coordinator_core/",
    "coordinator/",
    "scripts/",
    "tasks/",
)

#: A concrete POSIX home root — `/Users/<name>/` or `/home/<name>/`. Named
#: as machine-absolute for the same reason a drive letter is: it is one
#: operator's box. `/Users/` alone is universal and never matches, and the
#: account segment is run through the sibling guard's placeholder word list
#: (`alice`, `me`, a bracketed user placeholder, a USER env-var reference,
#: ...) so a worked-example home is not a
#: machine. Reusing that list rather than re-deriving one keeps the two
#: guards from disagreeing about what counts as a real operator name.
_POSIX_HOME_RE = re.compile(r"/(?:Users|home)/([A-Za-z0-9_.$<>-]+)/")

#: Sinks: dotted call name -> positional indices whose literal is resolved
#: against this host. Matched on the trailing attribute too (`chdir` for a
#: `from os import chdir`), so an import style cannot dodge the gate.
_SINK_POSITIONS: Dict[str, Tuple[int, ...]] = {
    "open": (0,),
    "os.chdir": (0,),
    "chdir": (0,),
    "os.stat": (0,),
    "os.listdir": (0,),
    "os.makedirs": (0,),
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.unlink": (0,),
    "os.rename": (0, 1),
    "os.walk": (0,),
    "shutil.rmtree": (0,),
    "shutil.copy": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "sys.path.insert": (1,),
    "sys.path.append": (0,),
}

#: Spawn entrypoints whose `cwd=` is resolved against this host. The argv
#: itself is not scanned: a foreign path inside argv is a payload as often
#: as a target, and `cwd=` is the shape that produced the incident.
_SPAWN_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "run",
        "Popen",
        "check_call",
        "check_output",
    }
)

#: `Path(...)`/`PurePath(...)` construction is NOT a sink — it is pure value
#: construction, which is exactly what a detector fixture does. A
#: filesystem-touching METHOD on that object is the sink.
_PATH_CONSTRUCTORS = frozenset({"Path", "pathlib.Path", "PosixPath", "WindowsPath"})
_PATH_FS_METHODS = frozenset(
    {
        "exists",
        "is_dir",
        "is_file",
        "is_symlink",
        "stat",
        "lstat",
        "iterdir",
        "glob",
        "rglob",
        "mkdir",
        "rmdir",
        "unlink",
        "touch",
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "samefile",
        "chmod",
        "rename",
        "replace",
    }
)

#: EXACT sites exempted from this gate, as `"<repo-relative path>::<line-anchor
#: symbol or call>"`. Never a directory or filename pattern — see the module
#: docstring's "The discriminator" section. Empty at introduction: every site
#: this gate found was fixed, not listed. An addition here needs a named
#: reason on its own line and a reviewer, exactly like a `SUSPENSION` entry.
_ALLOWED_SITES: frozenset = frozenset()


@dataclass(frozen=True)
class ResolutionFinding:
    """One machine-absolute literal reaching a host-touching API."""

    file: str
    line: int
    sink: str
    literal: str

    @property
    def site(self) -> str:
        return f"{self.file}::{self.sink}"


def _is_machine_absolute(value: object) -> bool:
    """True iff `value` is a string carrying a machine-absolute root — a
    Windows drive letter or a concrete POSIX home. Substring match, not
    anchored: a literal is just as broken embedded in a longer string."""
    if not isinstance(value, str):
        return False
    if WIN_DRIVE_RE.search(value):
        return True
    return any(
        not _is_placeholder_segment(m.group(1)) for m in _POSIX_HOME_RE.finditer(value)
    )


def _dotted_name(node: ast.expr) -> str:
    parts: List[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _module_level_path_constants(tree: ast.Module) -> Dict[str, ast.Constant]:
    """Module- and class-level names bound to a machine-absolute string
    literal. Only a BARE path (no whitespace, no newline, no quote) counts:
    a multi-line blob assigned to a module name is fixture text — a TOML
    document, a PowerShell script, an expected message body — not a
    location this code will resolve."""
    bound: Dict[str, ast.Constant] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef)):
            continue
        for stmt in node.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            value = stmt.value
            if not isinstance(value, ast.Constant) or not _is_machine_absolute(value.value):
                continue
            text = value.value
            if any(ch in text for ch in " \t\n\r\"'"):
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = value
    return bound


def _literal_behind(
    node: ast.expr, constants: Dict[str, ast.Constant]
) -> Optional[ast.Constant]:
    """The machine-absolute literal `node` carries — directly, or via a
    module-level constant name — or None."""
    if isinstance(node, ast.Constant) and _is_machine_absolute(node.value):
        return node
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def detect_resolution_sites(text: str, filename: str = "") -> List[ResolutionFinding]:
    """Every machine-absolute literal in `text` that reaches a host-touching
    API. Pure and filesystem-free. A file that does not parse yields no
    findings — a syntax error is another gate's business, and guessing at
    broken source is how a guard earns false positives."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    constants = _module_level_path_constants(tree)
    findings: List[ResolutionFinding] = []
    seen: Set[Tuple[int, str, str]] = set()

    def _record(node: ast.Constant, sink: str, lineno: int) -> None:
        key = (lineno, sink, node.value)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            ResolutionFinding(file=filename, line=lineno, sink=sink, literal=node.value)
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted_name(node.func)
        leaf = name.rsplit(".", 1)[-1] if name else ""

        for index in _SINK_POSITIONS.get(name, ()) or _SINK_POSITIONS.get(leaf, ()):
            if index < len(node.args):
                literal = _literal_behind(node.args[index], constants)
                if literal is not None:
                    _record(literal, name or leaf, node.lineno)

        if name in _SPAWN_CALLS or leaf in _SPAWN_CALLS:
            for keyword in node.keywords:
                if keyword.arg != "cwd":
                    continue
                literal = _literal_behind(keyword.value, constants)
                if literal is not None:
                    _record(literal, f"{name or leaf}(cwd=)", node.lineno)

        # `Path(<literal>).<fs-method>()` — the constructor is a propagator,
        # the method is the sink.
        if isinstance(node.func, ast.Attribute) and node.func.attr in _PATH_FS_METHODS:
            inner = node.func.value
            if isinstance(inner, ast.Call) and _dotted_name(inner.func) in _PATH_CONSTRUCTORS:
                for arg in inner.args:
                    literal = _literal_behind(arg, constants)
                    if literal is not None:
                        _record(
                            literal,
                            f"{_dotted_name(inner.func)}().{node.func.attr}",
                            node.lineno,
                        )

    return findings


def _tracked_python_files(root: Path) -> Tuple[str, ...]:
    """One `git ls-files` spawn for the whole scan — the amplification gate's
    batched shape, never a spawn per file."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        **leaf_spawn_creationflags(),
    ).stdout
    return tuple(p for p in out.split("\0") if p and p.startswith(_LIVE_ROOTS))


def scan_live_roots(root: Path) -> List[ResolutionFinding]:
    findings: List[ResolutionFinding] = []
    for rel in _tracked_python_files(root):
        full = root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not (WIN_DRIVE_RE.search(text) or _POSIX_HOME_RE.search(text)):
            continue
        findings.extend(detect_resolution_sites(text, rel))
    return [f for f in findings if f.site not in _ALLOWED_SITES]


def _format(findings: List[ResolutionFinding]) -> str:
    lines = [
        "machine-absolute path literal reaching a host-touching API:",
        "",
    ]
    for f in findings:
        lines.append(f"  {f.file}:{f.line}")
        lines.append(f"    sink:    {f.sink}")
        lines.append(f"    literal: {f.literal}")
    lines.append("")
    lines.append(
        "Resolve through the ladder instead: `Path(__file__).resolve().parents[N]` "
        "for this repo's own root, `read_doe_root_pointer()` for the DoE sibling, "
        "`machine_resolver` for a registered repo."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_machine_absolute_literal_reaches_host_resolution() -> None:
    findings = scan_live_roots(_REPO_ROOT)
    assert not findings, _format(findings)


# ---------------------------------------------------------------------------
# The gate's own proofs: it fires on a planted executable literal, and stays
# silent on the three legitimate categories it must not disturb.
# ---------------------------------------------------------------------------

_PLANTED_SUBPROCESS_CWD = '''
import subprocess
subprocess.run(["git", "status"], cwd="X:/claude-klabauter")
'''

_PLANTED_MODULE_CONSTANT = r'''
import sys
ENGINE_ROOT = r"X:\claude-klabauter"
sys.path.insert(0, ENGINE_ROOT)
'''

_PLANTED_PATH_PROBE = '''
from pathlib import Path
_LIVE_DOE_ROOT = "X:/DoE-claude"
if Path(_LIVE_DOE_ROOT).is_dir():
    pass
'''

_PLANTED_POSIX_HOME = '''
def load():
    with open("/Users/example-operator/X/claude-klabauter/state/x.json") as fh:
        return fh.read()
'''

_FIXTURE_DETECTOR_INPUT = '''
from guard import detect
def test_detector_fires_on_a_foreign_path():
    assert detect("X:/DoE-claude/coordinator/hooks/x.py")
    assert detect({"path": r"C:\\Users\\devbox\\project"})
'''

_FIXTURE_CONSTRUCTED_NOT_RESOLVED = '''
from pathlib import Path
def test_translation():
    assert translate(Path("X:/claude-klabauter/docs/plans/p.md")) == "docs/plans/p.md"
'''

_FIXTURE_EXPECTED_MESSAGE = r'''
_RESOLVED_NATIVE_PATH = "C:\\Users\\example-operator\\foreign\\scratch.txt"
def test_message_shows_resolved_path():
    assert _RESOLVED_NATIVE_PATH in render(_RESOLVED_NATIVE_PATH)
'''

_PROSE_DOCSTRING = '''
"""Run: cd X:/claude-klabauter && python -m pytest coordinator_core/tests

Historic note: the 2026-07-28 incident rewrote hooks to C:\\Users\\pm\\...
"""
def f():
    """Usage: python X:/claude-klabauter/tasks/probe.py"""
'''

_PROSE_COMMENT = '''
import subprocess
# On the Windows box this used to read cwd="X:/claude-klabauter"; it now
# derives the root from __file__. See C:/Users/pm for the old scratch tree.
subprocess.run(["git", "status"], cwd=str(_REPO_ROOT))
'''


@pytest.mark.parametrize(
    "label,source,expected_sink",
    [
        ("subprocess cwd", _PLANTED_SUBPROCESS_CWD, "subprocess.run(cwd=)"),
        ("module constant", _PLANTED_MODULE_CONSTANT, "sys.path.insert"),
        ("path probe", _PLANTED_PATH_PROBE, "Path().is_dir"),
        ("posix home open", _PLANTED_POSIX_HOME, "open"),
    ],
)
def test_gate_fires_on_planted_executable_literal(
    label: str, source: str, expected_sink: str
) -> None:
    findings = detect_resolution_sites(source, f"planted/{label}.py")
    assert findings, f"{label}: planted executable literal was not detected"
    assert findings[0].sink == expected_sink, findings


@pytest.mark.parametrize(
    "label,source",
    [
        ("detector fixture input", _FIXTURE_DETECTOR_INPUT),
        ("constructed but never resolved", _FIXTURE_CONSTRUCTED_NOT_RESOLVED),
        ("expected message fixture", _FIXTURE_EXPECTED_MESSAGE),
        ("docstring prose", _PROSE_DOCSTRING),
        ("comment prose", _PROSE_COMMENT),
    ],
)
def test_gate_is_silent_on_legitimate_literals(label: str, source: str) -> None:
    findings = detect_resolution_sites(source, f"legit/{label}.py")
    assert not findings, f"{label}: {_format(findings)}"


def test_real_guard_fixture_files_stay_silent() -> None:
    """The named fixture files the PM called out by name — they MUST keep
    their literals, and must do so without an allowlist entry."""
    for rel in (
        "coordinator_core/bash_guards/tests/test_no_machine_absolute_path_in_guard_messages.py",
        "coordinator_core/ops/session/tests/test_guard_foreign_platform_paths.py",
    ):
        full = _REPO_ROOT / rel
        if not full.is_file():
            continue
        findings = detect_resolution_sites(full.read_text(encoding="utf-8"), rel)
        assert not findings, _format(findings)


def test_unparsable_source_yields_no_findings() -> None:
    assert detect_resolution_sites("def f(:\n", "broken.py") == []
