"""coordinator_core.ops.check_native_door_interpreter_handoff -- RED-on-
existence census of every site that hands a settings-home ``bin/`` path to a
Python interpreter.

Purpose: the native-door cutover (2026-09-02) installs a COMPILED NATIVE
IMAGE at the settings-home bin entry -- ``<name>.exe`` on Windows and the
EXTENSIONLESS BARE NAME on POSIX, with no extension, no shebang and no other
surface tell. Four consumers still carried the standing assumption
"extensionless under settings-home therefore Python source" and prefixed an
interpreter onto such a path. One of them was the generated git hook, which
took ``git commit`` down in every repo on the box with ``SyntaxError:
Non-UTF-8 code starting with '\\xcf'`` -- python being handed a Mach-O
header. All four are fixed; this module exists so the population stays at
zero without anyone remembering the rule.

WHY THIS IS ITS OWN GUARD AND NOT A CLASS IN THE POSIX GUARD. The obvious
host was ``check_posix_exec_assumptions``, which already scans tracked source
for platform-execution defects. Measured before deciding, on this box:
that guard's own CLI runs at **36.8s user + 5.1s sys process time** over this
tree and exits 1 today on unrelated stale-baseline entries. A class added
there would inherit a 70x-over-brightline runtime and land its findings
inside an already-RED report -- the exact "report-only classes get skimmed
and ignored" failure that module's own docstring records. It is also a
different axis: that guard asks "does this POSIX assumption break Windows",
where this defect breaks POSIX and Windows identically, because the tell it
depends on (an extension) is absent on both.

WHAT IS SCANNED. Tracked source in one or more repo roots -- this repo by
default, plus the DoE-claude plane when it resolves (see ``resolve_roots``).
Candidate files are selected with seven ``git grep`` invocations per root
(four settings-home, two for the suffix arm, one for the shell arm);
nothing spawns a process per file.

MEASURED COST, STATED NOT HIDDEN. 11.0s process time over both roots
(6.6s user + 4.4s sys) on the normal-tier box under live load. The
suffix-dispatch arm accounts for 2.9s of that: the same run measured 8.1s
immediately before it landed, and the arm's selector is what the extra buys.
An earlier shape of that selector cost 6.8s and was cut by intersecting on
the arm's own preconditions -- see `suffix_arm_files`. The figure before the
third arm existed, kept because a later reader will want the delta: 3.9s user
+ 2.7s sys over this repo alone, 7.8s over both roots.
That is OVER the 500ms brightline and is not claimed otherwise. What it is:
a whole-tree census over 39k tracked files, run at CADENCE gates (the two
slow tests carry ``@pytest.mark.cadence``), never on a session, commit or
ceremony hot path; 6x cheaper than the nearest comparable guard
(``check_posix_exec_assumptions``, 41.9s); zero per-file process spawns, the
axis the amplification gate actually polices. The shape that would get this
under 500ms is an INCREMENTAL scan over the staged diff, not a faster
whole-tree walk -- build that if and when a hot-path caller needs it, and do
not mistake the whole-tree run for one.

THE THREE ARMS.

  - **Python arm** (``classify_python_source``): a light intra-repo taint.
    A *composition* is ``os.path.join(<settings-home>, "bin", ...)``, a
    ``<settings-home> / "bin" / ...`` pathlib chain, or a string literal
    naming ``${COORDINATOR_SETTINGS_HOME...}/bin/`` /
    ``.coordinator-claude-settings/bin/``. A function that RETURNS one is a
    *producer*; a call to a producer taints, and an argument passed tainted
    at any call site taints that parameter in the callee. The defect is a
    tainted value reaching an interpreter: ``[sys.executable, path]``,
    ``["python3", path]``, ``runpy.run_path``,
    ``spec_from_file_location``, ``exec``/``compile``, or a TEXT read
    (``open()`` in text mode, ``read_text()``) of a path that may be a
    binary image.
  - **Shell arm** (``classify_text_source``): the same defect in emitted
    shell -- a rung that resolves ``${COORDINATOR_SETTINGS_HOME:-...}/bin/
    <name>`` and then runs it as ``"$_PY" "$_T"``.
    shell-doc-ok: those two spellings ARE the defect this arm detects; a
    docstring that cannot name the shape cannot document the detector.
    Applied to the raw text
    of shell/launcher files AND of ``.py`` files, because the hook that
    broke every commit was a shell body built from Python string literals:
    an AST-only guard cannot see it.
  - **Suffix-dispatch arm** (``_suffix_dispatch_sites``): a scope that
    decides native-vs-script FROM THE PATH'S SUFFIX and prefixes an
    interpreter on the branch where a NATIVE-image suffix test came out
    FALSE. Needs no taint and no settings-home token, which is the point:
    both other arms select candidates by co-occurrence, so a helper that
    takes the bin DIRECTORY AS A PARAMETER is unselectable by them, and
    stage two cannot rescue it because a producer is only ever discovered
    inside a stage-one file. The DoE plane's ``_forwarder_resolve.py`` was
    exactly that -- 5th consumer of the class, composition in four callers,
    interpreter prefix in the callee, zero occurrences of either selector
    token -- and the census read GREEN on a reconstructed pre-fix tree until
    this arm existed. See the arm's own section comment for the asymmetry
    (``.py``-positive safe, ``.exe``-negative defective) that keeps it
    precise.

THE REMEDY, AND WHY IT IS ONE NAME. ``coordinator_core.launchable.
resolve_launchable`` already returns a bare path for a native image and
prefixes an interpreter only for a non-executable ``.py``. That is precisely
why roughly 370 other cut-over names survived the same cutover untouched.
Delegating is the fix; a second hand-rolled answer to the same question is
how this class recurs.

THE THREE SHAPES THAT ARE NOT DEFECTS, and are recognised as such:

  1. **Delegation** -- ``[*resolve_launchable(path), *args]``. Produces no
     finding structurally (the interpreter is never named at the call site).
  2. **Magic-byte refusal** -- the enclosing function reads the bytes and
     compares against ``door_install.NATIVE_IMAGE_MAGIC`` before deciding
     (``coordinator_core.ops.install_health_run``'s launch-chain leg). The
     shell arm's equivalent is a ``_native``-style header probe, which the
     generated hook body carries.
  3. **Exec-bit discrimination at call time** -- ``os.access(path, os.X_OK)``
     gating a bare-path return, with the interpreter as the fallback
     (``wsc-session-disposition._session_claim_cli_argv``,
     ``workday-complete-reconcile._cruft_sweep_argv``). Not the preferred
     shape -- it is a second answer to the question shape 1 already answers,
     and is drift-prone -- but it is CORRECT, it is what the two fixed sites
     landed, and REDing already-fixed code would train authors to route
     around this guard.

DOCTRINE ANCHOR (DoE plane): ``coordinator/docs/wiki/coordinator-tripwires/
an-extensionless-settings-home-bin-entry-is-not-python-source.md``. Cited by
name, never by line.

Negative-spec -- read this before changing anything here:

  - **"Extensionless under settings-home means Python source" is FALSE and
    must never be reintroduced as a standing assumption**, in code, in a
    comment, or as a premise in a test fixture. It was true only until
    2026-09-02. Since then the bare name is the door image's own home. Any
    new consumer must decide from the FILE'S BYTES or from
    ``resolve_launchable``, never from the absence of an extension, never
    from "the installer writes Python there", and never from a name pattern.
    A name-matching drift script is exactly how the DoE plane's twin of this
    hazard un-cutover'd a door.
  - **The absence of a finding is not the absence of the hazard.** This
    guard decides statically over a taint that stops at the first
    indirection it cannot follow -- a path routed through a dict, a class
    attribute, or a subprocess argv assembled elsewhere is invisible to it.
    It is a ratchet on the shapes that have actually occurred, not a proof.
    Widen it when a new shape occurs; do not read green as "impossible".
  - **This guard never blocks on a second root being absent.** A box with no
    DoE clone is a normal box, not a failure. It reports the skip and its
    reason and stays green on the roots it did scan.
  - **No baseline, ever.** The population is a handful of sites and was
    driven to zero before this file existed. A baseline here would be the
    grandfather slot that lets the next one in silently.
  - **No per-file subprocess.** Candidate selection is one ``git grep`` per
    stage per root. A loop of ``git`` calls over 39k tracked files is a
    brightline violation, not an implementation detail.

Spec backlink: the 2026-09-02 native-door consumer post-mortem.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from coordinator_core.git.run import run_git

REMEDY = (
    "route the path through coordinator_core.launchable.resolve_launchable "
    "-- it returns a bare path for a native image and prefixes an interpreter "
    "only for a non-executable .py, which is why ~370 other cut-over names "
    "survived. If this site cannot import the engine, refuse on the image's "
    "own magic bytes (door_install.NATIVE_IMAGE_MAGIC) instead. Never decide "
    "from the absence of a file extension. Doctrine anchor (DoE plane): "
    "coordinator/docs/wiki/coordinator-tripwires/"
    "an-extensionless-settings-home-bin-entry-is-not-python-source.md"
)

_SETTINGS_HOME_TOKEN = "settings_home"
_SHELL_HOME_TOKENS = ("COORDINATOR_SETTINGS_HOME", ".coordinator-claude-settings")

_INTERPRETER_LITERALS = frozenset(
    {"python", "python3", "python.exe", "python3.exe", "py", "pythonw", "pythonw.exe"}
)

# A file with none of these cannot contain an interpreter-mediated site, so
# it is never parsed. `git grep --all-match` applies them at file level.
_INTERPRETER_FILE_TOKENS = ("sys.executable", "python", "run_path", "read_text")

_SOURCE_EXEC_CALLS = frozenset({"run_path", "spec_from_file_location", "exec", "compile"})

_TEXT_READ_ATTRS = frozenset({"read_text"})

_MAGIC_REFUSAL_TOKENS = ("NATIVE_IMAGE_MAGIC", "native_image", "is_native_image")

# Suffixes a door image can actually occupy. A test that enumerates THESE and
# treats the complement as Python is the defect `_is_positive_suffix_test`
# describes and refuses to clear; `_suffix_dispatch_sites` is what finds it.
#
# `.py` is deliberately absent -- a positive `.py` test is the safe form.
# `.com` is deliberately absent too, and not as an oversight: the installer
# emits `<name>.exe` and nothing else, so `.com` would be speculative
# coverage, and it is an ACTIVELY BAD selector token because it matches every
# `github.com` in a tree (2316 files here against 1335 for `.exe` alone).
# Add a suffix here only when the installer actually emits one.
_NATIVE_IMAGE_SUFFIXES = frozenset({".exe"})

# Selector tokens for the suffix-dispatch arm. A file with no native-image
# suffix literal cannot carry this shape, so it is never parsed.
_SUFFIX_ARM_FILE_TOKENS = tuple(sorted(_NATIVE_IMAGE_SUFFIXES))

# The arm's other precondition: it only ever fires on a test that READS a
# suffix, so a file naming none of these cannot carry the shape. Kept in step
# with `_reads_a_suffix` and `_native_suffix_test`, which are what actually
# decide -- these are the file-level shadow of those two.
_SUFFIX_READ_FILE_TOKENS = (".suffix", "splitext", "endswith")

_SUFFIX_DISPATCH_SHAPE = (
    "interpreter prefixed on the branch where a native-image suffix test was FALSE"
)

_TEXT_ARM_SUFFIXES = frozenset({"", ".sh", ".bash", ".cmd", ".ps1", ".py"})

# Prose corpora. They quote hook bodies and remediation commands verbatim by
# the thousand; scanning them would report documentation of the defect as the
# defect. Code lives outside these.
_PROSE_DIR_PREFIXES = (
    "docs/",
    "state/",
    "archive/",
    "tasks/",
    "cross-repo/",
    ".structural-index/",
)

# The planted controls. `test_check_native_door_interpreter_handoff.py`
# requires this directory to hold BOTH a defective and a correct specimen and
# requires the guard to flag exactly the defective one -- a guard that can
# only ever print green is not evidence of anything. The specimens carry a
# `.py.txt` suffix, so no collector, importer or sibling guard reads them as
# source and this exclusion is belt-and-braces rather than the only thing
# holding them out. Putting real code here to silence a finding is the one
# abuse the exclusion admits, and the control test's membership assertion is
# what closes it.
CONTROL_FIXTURE_DIR = "coordinator_core/ops/tests/fixtures/native_door_handoff/"

_SHELL_BIN_RE = re.compile(
    r"(?:COORDINATOR_SETTINGS_HOME|coordinator-claude-settings)[^\n]{0,160}?/bin/"
)
# A shell assignment: `_T="..."`, `_fwd='...'`, `SCRIPT=...`.
_SHELL_ASSIGN_RE = re.compile(r"""(?m)^[^\n]*?\b([A-Za-z_][A-Za-z0-9_]*)=["']?([^"'\n]*)""")
# A Python assignment whose value names the settings home -- the emitted hook
# interpolates such a name into its own shell body, so the shell variable's
# provenance is only visible through it.
_PY_HOME_ASSIGN_RE = re.compile(
    # Spans a few lines on purpose: the live emitter writes
    # `settings_home_script = (\n    '${COORDINATOR_SETTINGS_HOME...}/bin/'`,
    # so a single-line pattern found no carrier and the shell arm went quiet.
    r"""(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[\s\S]{0,300}?"""
    r"""(?:COORDINATOR_SETTINGS_HOME|coordinator-claude-settings)"""
)
# `"$_PY" "$_T"`, `python3 "$_T"`, `%_py% "%_T%"` -- an interpreter running a
# variable, with the variable captured.
_SHELL_RUN_RES = (
    re.compile(r"""["']?\$\{?_?[Pp][Yy][A-Za-z0-9_]*\}?["']?\s+["']?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"""),
    re.compile(r"""\bpython3?\b\s+["']?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"""),
    re.compile(r"""%_?py%\s+["']?%([A-Za-z_][A-Za-z0-9_]*)%"""),
)
_SHELL_NATIVE_PROBE_RE = re.compile(r"_native\b|NATIVE_IMAGE_MAGIC|\\x7fELF|\\xcf\\xfa\\xed")


@dataclass(frozen=True)
class Finding:
    """One interpreter-mediated settings-home ``bin/`` site."""

    root_label: str
    relpath: str
    lineno: int
    scope: str
    shape: str

    def render(self) -> str:
        return f"  [{self.root_label}] {self.relpath}:{self.lineno} in {self.scope} -- {self.shape}"


# ---------------------------------------------------------------------------
# Expression shapes
# ---------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str:
    """Best-effort dotted text for a Name/Attribute/Call head. Empty when the
    node is not name-shaped -- callers treat that as "no opinion"."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _is_settings_home_rooted(node: ast.AST) -> bool:
    return _SETTINGS_HOME_TOKEN in _dotted(node).lower()


def _const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


def _shell_bin_literal(node: ast.AST) -> bool:
    text = _const_str(node)
    if text is None:
        return False
    if not any(token in text for token in _SHELL_HOME_TOKENS):
        return False
    return "/bin/" in text or text.rstrip().endswith("/bin")


def _div_operands(node: ast.AST) -> List[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_operands(node.left) + _div_operands(node.right)
    return [node]


def _announces_its_own_extension(node: ast.AST) -> bool:
    """True when the path's FINAL component is a literal carrying an
    extension other than ``.exe``.

    The door image occupies exactly two shapes: the EXTENSIONLESS bare name
    on POSIX and ``<name>.exe`` on Windows. A literal that ends in any other
    extension therefore cannot be one, and is not this defect:
    ``<settings-home>/bin/_machine_local.py`` under ``sys.executable`` is
    what ``resolve_launchable`` itself would produce, and
    ``<settings-home>/bin/.coordinator-bin-manifest.json`` read as text is a
    JSON read. The hazard is the bare name, or a name assembled from a
    variable that could be one.
    """
    text = _const_str(node)
    if text is None:
        return False
    tail = text.rstrip("/").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    suffix = os.path.splitext(tail)[1].lower()
    return bool(suffix) and suffix != ".exe"


def _is_composition(node: ast.AST) -> bool:
    """True when ``node`` ITSELF composes a settings-home ``bin/`` path whose
    final component does not announce its own interpreter."""
    if _shell_bin_literal(node) and not _announces_its_own_extension(node):
        return True
    if isinstance(node, ast.Call) and _dotted(node.func).endswith("path.join"):
        args = list(node.args)
        has_bin = any(_const_str(a) == "bin" for a in args)
        has_home = any(_is_settings_home_rooted(a) or _shell_bin_literal(a) for a in args)
        if has_bin and has_home and not (args and _announces_its_own_extension(args[-1])):
            return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        operands = _div_operands(node)
        has_bin = any(_const_str(o) == "bin" for o in operands)
        has_home = any(_is_settings_home_rooted(o) for o in operands)
        if has_bin and has_home and not _announces_its_own_extension(operands[-1]):
            return True
    return False


_MEMO_ATTR = "_ndh_contains_composition"
_INNER_LINK_ATTR = "_ndh_inner_chain_link"


def _mark_inner_chain_links(tree: ast.AST) -> None:
    """Flag the non-final links of a ``a / "bin" / name`` chain.

    ``settings_home() / "bin" / "x.json"`` contains, as its own left child,
    the sub-chain ``settings_home() / "bin"`` -- which reads as a bare
    composition and made every extension-bearing name a finding. Only the
    outermost link is the path actually being composed. One walk per file,
    at parse time, so the question is never asked again.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.BinOp)
            and isinstance(node.left.op, ast.Div)
        ):
            setattr(node.left, _INNER_LINK_ATTR, True)


def _contains_composition(node: ast.AST) -> bool:
    """Memoized ON THE NODE (never on ``id()``, which a freed-and-reused
    address would silently corrupt), and recursive rather than walking.

    The walking spelling -- ``any(_is_composition(sub) for sub in
    ast.walk(node))`` -- re-walks a subtree from scratch for every enclosing
    expression asked about it; memoizing at EVERY level instead makes an
    overlapping second question O(1). Eagerly stamping every node in the
    file was measured slower still (5.1s against 4.0s): most nodes are never
    asked.
    """
    cached = getattr(node, _MEMO_ATTR, None)
    if cached is None:
        cached = not getattr(node, _INNER_LINK_ATTR, False) and _is_composition(node)
        if not cached:
            cached = any(_contains_composition(child) for child in ast.iter_child_nodes(node))
        setattr(node, _MEMO_ATTR, cached)
    return cached


def _is_interpreter_expr(node: ast.AST) -> bool:
    if _dotted(node) == "sys.executable":
        return True
    text = _const_str(node)
    if text is not None and text.lower() in _INTERPRETER_LITERALS:
        return True
    if isinstance(node, ast.Name):
        lowered = node.id.lower()
        return lowered in {"_py", "py_bin", "python_bin", "interpreter"} or lowered.endswith("_python")
    return False


# ---------------------------------------------------------------------------
# Per-module taint
# ---------------------------------------------------------------------------


class _ModuleAnalysis:
    """One parsed module, re-evaluated as the repo-wide producer/parameter
    taint sets grow.

    Each scope's node list and its tainted-name set are computed ONCE PER
    ROUND (``refresh``) and read by every consumer, rather than recomputed
    per query. The naive shape -- an ``ast.walk`` per scope per question --
    measured 60s of profiled time over this tree, because a module scope
    re-walked every nested function once for each question asked of it.
    """

    def __init__(self, relpath: str, tree: ast.Module) -> None:
        self.relpath = relpath
        self.tree = tree
        _mark_inner_chain_links(tree)
        self.scopes: List[Tuple[str, ast.AST, List[ast.stmt]]] = []
        self._collect_scopes("<module>", tree, tree.body)
        self.own: List[List[ast.AST]] = [
            list(_own_nodes(node, body)) for _, node, body in self.scopes
        ]
        self.tainted: List[Set[str]] = [set() for _ in self.scopes]

    def _collect_scopes(self, name: str, node: ast.AST, body: List[ast.stmt]) -> None:
        self.scopes.append((name, node, body))
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_scopes(stmt.name, stmt, stmt.body)
            elif isinstance(stmt, ast.ClassDef):
                self._collect_scopes(name, stmt, stmt.body)

    def refresh(self, producers: Set[str], params: Dict[str, Set[str]]) -> None:
        """Recompute every scope's tainted-name set against the current
        repo-wide producer/parameter sets."""
        for index, (name, node, _body) in enumerate(self.scopes):
            seeded = _param_names_for(node, params.get(name, set()))
            self.tainted[index] = self._tainted_names(index, seeded, producers)

    def _tainted_names(self, index: int, seeded: Set[str], producers: Set[str]) -> Set[str]:
        tainted = set(seeded)
        assignments = [
            stmt
            for stmt in self.own[index]
            if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        ]
        # Two passes, so a name assigned from a name assigned later in the
        # file still resolves. A third pass has never changed an answer on
        # this corpus and is not worth its walk.
        for _ in range(2):
            for stmt in assignments:
                if isinstance(stmt, ast.Assign):
                    targets, value = list(stmt.targets), stmt.value
                elif stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                else:
                    continue
                if not self._is_tainted(value, tainted, producers):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        tainted.add(target.id)
        return tainted

    def _is_tainted(self, node: ast.AST, tainted: Set[str], producers: Set[str]) -> bool:
        if _contains_composition(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in tainted
        if isinstance(node, ast.Call):
            leaf = _dotted(node.func).rsplit(".", 1)[-1]
            if leaf in producers:
                return True
            if leaf in {"str", "Path", "fspath", "abspath", "realpath", "expanduser"}:
                return any(self._is_tainted(a, tainted, producers) for a in node.args)
            return False
        if isinstance(node, (ast.Attribute, ast.Starred)):
            return self._is_tainted(node.value, tainted, producers)
        if isinstance(node, ast.JoinedStr):
            return any(
                self._is_tainted(v.value, tainted, producers)
                for v in node.values
                if isinstance(v, ast.FormattedValue)
            )
        return False

    def producer_names(self, producers: Set[str]) -> Set[str]:
        """Function names whose body returns a settings-home ``bin/`` path.
        Reads the taint computed by the last ``refresh``."""
        found: Set[str] = set()
        for index, (name, node, _body) in enumerate(self.scopes):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for stmt in self.own[index]:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    if self._is_tainted(stmt.value, self.tainted[index], producers):
                        found.add(name)
                        break
        return found

    def tainted_params(self, producers: Set[str]) -> Dict[str, Set[str]]:
        """Callee name -> argument slots this module passes tainted. Reads the
        taint computed by the last ``refresh``."""
        found: Dict[str, Set[str]] = {}
        for index, _scope in enumerate(self.scopes):
            tainted = self.tainted[index]
            for call in self.own[index]:
                if not isinstance(call, ast.Call):
                    continue
                callee = _dotted(call.func).rsplit(".", 1)[-1]
                if not callee:
                    continue
                for position, arg in enumerate(call.args):
                    if self._is_tainted(arg, tainted, producers):
                        found.setdefault(callee, set()).add(f"#{position}")
                for kw in call.keywords:
                    if kw.arg and self._is_tainted(kw.value, tainted, producers):
                        found.setdefault(callee, set()).add(kw.arg)
        return found


class _ParamTaint:
    """Which parameter slots receive a settings-home ``bin/`` path.

    Keyed by the callee's UNQUALIFIED name, because that is all a static
    reader has at a call site. That key is ambiguous across files -- half
    this tree has a local ``_run(...)`` helper -- and propagating a taint
    between two unrelated functions that merely share a name reported seven
    false sites on the first live run. So a slot recorded in file A seeds a
    function in file B only when that name is defined in EXACTLY ONE file
    repo-wide; otherwise the propagation stays inside the file that
    observed it.

    Negative-spec: do not "fix" the ambiguity by widening back to bare-name
    matching. The residual loss is a same-named consumer split across two
    files, which is a shape this defect has never taken; the false-positive
    cost of the wide form is a guard authors learn to ignore.
    """

    def __init__(self, analyses: Dict[str, "_ModuleAnalysis"]) -> None:
        self._local: Dict[Tuple[str, str], Set[str]] = {}
        self._global: Dict[str, Set[str]] = {}
        self._def_files: Dict[str, Set[str]] = {}
        self._memo: Dict[str, Dict[str, Set[str]]] = {}
        self.add_definitions(analyses)

    def add_definitions(self, analyses: Dict[str, "_ModuleAnalysis"]) -> None:
        for relpath, analysis in analyses.items():
            for name, node, _body in analysis.scopes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._def_files.setdefault(name, set()).add(relpath)
        self._memo.clear()

    def record(self, relpath: str, observed: Dict[str, Set[str]]) -> None:
        for callee, slots in observed.items():
            self._local.setdefault((relpath, callee), set()).update(slots)
            self._global.setdefault(callee, set()).update(slots)
        self._memo.clear()

    def for_file(self, relpath: str) -> Dict[str, Set[str]]:
        """Iterates the RECORDED slots, never the repo's whole definition
        table -- the latter shape cost 4.2s of a 7s scan on this tree."""
        cached = self._memo.get(relpath)
        if cached is not None:
            return cached
        resolved: Dict[str, Set[str]] = {}
        for (observed_in, name), slots in self._local.items():
            if observed_in == relpath and slots:
                resolved.setdefault(name, set()).update(slots)
        for name, slots in self._global.items():
            if slots and self._def_files.get(name) == {relpath}:
                resolved.setdefault(name, set()).update(slots)
        self._memo[relpath] = resolved
        return resolved


def _param_names_for(node: ast.AST, slots: Set[str]) -> Set[str]:
    """Map positional/keyword slot markers onto this definition's parameter
    names, so a callee's body can be analysed with its tainted parameters
    already seeded."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    args = node.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    named = {a.arg for a in (*args.args, *args.kwonlyargs)}
    seeded: Set[str] = set()
    for slot in slots:
        if slot.startswith("#"):
            index = int(slot[1:])
            if index < len(positional):
                seeded.add(positional[index])
        elif slot in named:
            seeded.add(slot)
    return seeded


def _own_nodes(scope_node: ast.AST, body: List[ast.stmt]) -> Iterable[ast.AST]:
    """Every node belonging to THIS scope, never descending into a nested
    def/class -- those are their own scopes with their own taint. Without
    this, one site was reported once per enclosing scope, and an exemption
    anywhere in a file cleared the whole file."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    stack: List[ast.AST] = [
        stmt for stmt in body if scope_node is not None and not isinstance(stmt, nested)
    ]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, nested):
                continue
            stack.append(child)


def _is_exempt_among(own: List[ast.AST]) -> bool:
    """Shapes 2 and 3 of the module docstring, evaluated over a caller-chosen
    node set: a magic-byte refusal, or an exec-bit discrimination at call
    time. Either one means the SCOPE PASSED IN already decides from the
    artifact rather than from its name. Split out of `_scope_is_exempt` /
    `_call_site_is_exempt` so both can share the shape logic while differing
    on which nodes they're allowed to look at.
    """
    negated: Set[int] = set()
    for sub in own:
        if isinstance(sub, ast.UnaryOp) and isinstance(sub.op, ast.Not):
            negated.update(id(inner) for inner in ast.walk(sub))
    for sub in own:
        dotted = _dotted(sub)
        if any(token in dotted for token in _MAGIC_REFUSAL_TOKENS):
            return True
        if isinstance(sub, ast.Name) and any(t in sub.id for t in _MAGIC_REFUSAL_TOKENS):
            return True
        if isinstance(sub, ast.Call) and _dotted(sub.func).endswith("os.access"):
            if any(_dotted(a).endswith("X_OK") for a in sub.args):
                return True
        if isinstance(sub, ast.Call) and _dotted(sub.func).endswith("read_bytes"):
            return True
        if _is_header_sniff(sub):
            return True
        if id(sub) not in negated and _is_positive_suffix_test(sub):
            return True
    return False


_EXIT_STMTS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _unconditionally_exits(body: List[ast.stmt]) -> bool:
    """True iff `body`'s last statement is a return/raise/continue/break --
    reaching the code AFTER the `if` that owns this body therefore implies
    the `if`'s condition was false. Simple last-statement check, not a full
    exhaustiveness analysis: good enough for the guard-clause shape the two
    live exempted call sites actually use."""
    return bool(body) and isinstance(body[-1], _EXIT_STMTS)


def _statement_lists(scope_node: ast.AST) -> Iterable[List[ast.stmt]]:
    """Every statement list reachable from `scope_node`'s own body -- the
    body itself, plus every nested `if`/`try`/`while`/`for`/`with` body,
    `orelse`, `finalbody` and exception-handler body -- without crossing
    into a nested def/class scope (that is a different scope's taint)."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def walk(body: List[ast.stmt]) -> Iterable[List[ast.stmt]]:
        yield body
        for stmt in body:
            if isinstance(stmt, nested):
                continue
            for attr in ("body", "orelse", "finalbody"):
                sub = getattr(stmt, attr, None)
                if isinstance(sub, list) and sub:
                    yield from walk(sub)
            for handler in getattr(stmt, "handlers", None) or ():
                yield from walk(handler.body)

    top = getattr(scope_node, "body", [])
    if isinstance(top, list):
        yield from walk(top)


def _index_containing(stmts: List[ast.stmt], target: ast.AST) -> Optional[int]:
    """Index of the statement in `stmts` that IS `target` or contains it,
    without crossing into a nested def/class scope."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for index, stmt in enumerate(stmts):
        stack: List[ast.AST] = [stmt]
        while stack:
            node = stack.pop()
            if node is target:
                return index
            for child in ast.iter_child_nodes(node):
                if isinstance(child, nested):
                    continue
                stack.append(child)
    return None


def _enclosing_gate(scope_node: ast.AST, target: ast.AST) -> Optional[ast.AST]:
    """The `if`/`try` that actually governs whether `target` runs, or
    `None` when nothing does.

    Two shapes recognised, both drawn from the two call sites this guard
    already treats as legitimately exempt:

    1. **Literal nesting** -- `target` sits lexically inside an `if`/`try`
       body (``if cond: ...target...``). The nearest such ancestor is the
       gate.
    2. **Guard-clause / early-return** -- `target` sits AFTER an `if` whose
       body unconditionally exits (``if cond: return X`` then, unindented,
       the code that runs only when `cond` was false). That preceding `if`
       is the gate even though `target` is not nested inside it.

    Code-reviewer Finding 2 (2026-09-02): `_scope_is_exempt` evaluated
    exemption shapes over the WHOLE enclosing function, so an inert mention
    of an exemption token anywhere in it -- a dead branch, an unrelated
    `.py`-suffix test -- cleared a completely unconditional, ungated handoff
    elsewhere in the same function. Tying the exemption to shape (1) or (2)
    above closes that without regressing the two live exempted sites, one of
    which uses shape (2) (`_session_claim_cli_argv`'s `if os.access(...):
    return ...` followed by the unconditional interpreter fallback).
    """
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    parent: Dict[int, ast.AST] = {}
    stack: List[ast.AST] = [scope_node]
    while stack:
        current = stack.pop()
        for child in ast.iter_child_nodes(current):
            if current is not scope_node and isinstance(current, nested):
                continue
            parent[id(child)] = current
            stack.append(child)
    cur: ast.AST = target
    while id(cur) in parent:
        cur = parent[id(cur)]
        if cur is scope_node:
            break
        if isinstance(cur, (ast.If, ast.Try)):
            return cur
        if isinstance(cur, nested):
            return None

    for stmts in _statement_lists(scope_node):
        idx = _index_containing(stmts, target)
        if idx is None:
            continue
        for j in range(idx - 1, -1, -1):
            prior = stmts[j]
            if isinstance(prior, ast.If) and _unconditionally_exits(prior.body):
                return prior
        break
    return None


def _call_site_is_exempt(scope_node: ast.AST, target: ast.AST) -> bool:
    """Exemption check scoped to the flagged call site, not the whole
    function -- see `_enclosing_gate` for what counts as a gate. No gate ->
    nothing ties an exemption to this call, so it is NOT exempt (no
    whole-scope fallback, which is exactly the hole Finding 2 named)."""
    gate = _enclosing_gate(scope_node, target)
    if gate is None:
        return False
    own = list(_own_nodes(gate, list(ast.iter_child_nodes(gate))))
    return _is_exempt_among(own)


def _is_header_sniff(node: ast.AST) -> bool:
    """The scope reads the candidate's own first bytes and decides from them
    -- ``shebang.startswith("#!")``, or a delegation to
    ``launchable.resolve_by_shebang``.

    Same principle as the magic-byte refusal and the same principle the DoE
    plane's drift-script fix landed on: decide from what the file IS, not
    from what its name suggests. A native image has no ``#!`` line, so a
    scope gated on one never hands an image to an interpreter.
    """
    if isinstance(node, ast.Call):
        dotted = _dotted(node.func)
        if dotted.endswith("resolve_by_shebang") or dotted.endswith("resolve_launchable"):
            return True
        if dotted.endswith("startswith"):
            if any((_const_str(a) or "").startswith("#!") for a in node.args):
                return True
    return False


def _is_positive_suffix_test(node: ast.AST) -> bool:
    """Shape 4: the scope interprets only what it has POSITIVELY established
    to be a ``.py`` file (``cli_path.suffix == ".py"``,
    ``path.endswith(".py")``).

    Deciding to interpret because an extension IS ``.py`` is always safe --
    the door image never occupies such a name. Deciding because an extension
    is ABSENT is the defect itself, so the negated form
    (``not path.endswith(".py")``) is deliberately NOT a discriminator here;
    the two live sites that carry it pair it with an exec-bit check, which is
    what actually clears them.
    """
    if isinstance(node, ast.Compare):
        if node.ops and isinstance(node.ops[0], ast.Eq):
            left_is_suffix = _dotted(node.left).endswith("suffix") or (
                isinstance(node.left, ast.Call) and _dotted(node.left.func).endswith("splitext")
            )
            if left_is_suffix and any(_const_str(c) == ".py" for c in node.comparators):
                return True
    if isinstance(node, ast.Call) and _dotted(node.func).endswith("endswith"):
        if any(_const_str(a) == ".py" for a in node.args):
            return True
    return False


# ---------------------------------------------------------------------------
# Suffix-dispatch arm -- the defect `_is_positive_suffix_test` names but no
# other arm detects
# ---------------------------------------------------------------------------
#
# WHY THIS ARM EXISTS, AND WHY IT IS NOT TAINTED. The two taint arms select
# candidate files by co-occurrence: a file must name the settings home AND a
# ``bin`` segment before it is even parsed. A helper that takes the bin
# DIRECTORY AS A PARAMETER names neither, so the one file where the handoff
# physically lives is unselectable by construction -- and stage two cannot
# rescue it either, because stage two pulls in files naming a PRODUCER, and a
# producer can only be discovered inside a stage-one file. The DoE plane's
# ``_forwarder_resolve.py`` was exactly this: 5th consumer of the class, the
# composition in four callers, the interpreter prefix in the callee, zero
# occurrences of either selector token. Measured on a reconstructed pre-fix
# tree (peer session, 2026-09-02): stage one selected 8 candidates in that
# root including all four callers, and the census still read green.
#
# So this arm asks a question that needs no taint and no co-occurrence, and
# is decidable from one function's syntax alone: does this scope decide
# native-vs-script FROM THE PATH'S SUFFIX, and prefix an interpreter on the
# branch where a NATIVE-image suffix test came out FALSE? That is the
# standing assumption the module's negative-spec forbids by name -- "never
# decide from the absence of a file extension" -- stated there since the
# guard landed but, until now, only ever used to REFUSE an exemption, never
# to raise a finding.
#
# THE ASYMMETRY IS THE WHOLE PRECISION STORY, and it is inherited from
# `_is_positive_suffix_test` rather than reinvented: testing that a suffix IS
# ``.py`` and interpreting on the TRUE branch is always safe, because a door
# image never occupies such a name. Testing that a suffix is ``.exe`` and
# interpreting on the FALSE branch is the defect, because on POSIX the image
# is the extensionless bare name and lands in that branch. Only the second
# form fires here. A scope that also sniffs magic bytes, checks an exec bit,
# or delegates to `resolve_launchable` is already exempt by shapes 1-3 and is
# not re-flagged.


def _root_name(node: ast.AST) -> Optional[str]:
    """The leftmost `Name` of an attribute/call/subscript chain --
    ``script_path`` for ``str(script_path.suffix.lower())``."""
    seen = 0
    while seen < 12:
        seen += 1
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call):
            if node.args and not _dotted(node.func).endswith("endswith"):
                node = node.args[0] if isinstance(node.func, ast.Name) else node.func
            else:
                node = node.func
        else:
            return None
    return None


def _reads_a_suffix(node: ast.AST) -> bool:
    dotted = _dotted(node)
    if dotted.endswith("suffix") or ".suffix." in dotted + ".":
        return True
    if isinstance(node, ast.Call):
        func = _dotted(node.func)
        if func.endswith("splitext") or func.endswith("suffix"):
            return True
        return any(_reads_a_suffix(arg) for arg in node.args) or _reads_a_suffix(node.func)
    if isinstance(node, ast.Attribute):
        return _reads_a_suffix(node.value)
    if isinstance(node, ast.Subscript):
        return _reads_a_suffix(node.value)
    return False


def _module_string_groups(tree: ast.Module) -> Dict[str, Set[str]]:
    """Module-level ``NAME = (".exe",)`` constants, so a test written against
    a named tuple resolves to the same literals as an inline one. Module
    scope only -- a value reassigned per call is not a constant."""
    groups: Dict[str, Set[str]] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(stmt.value, (ast.Tuple, ast.List, ast.Set)):
            continue
        literals = {_const_str(elt) for elt in stmt.value.elts}
        if literals and all(lit is not None for lit in literals):
            groups[target.id] = {lit for lit in literals if lit is not None}
    return groups


def _native_suffix_test(node: ast.AST, groups: Dict[str, Set[str]]) -> Optional[str]:
    """``<name>`` if `node` tests whether ``<name>``'s suffix is one of the
    NATIVE-image suffixes, else None. The literal set must be non-empty and
    must contain nothing but native-image suffixes -- a test mentioning
    ``.py`` is the safe positive form and is never claimed here."""

    def _resolve(operand: ast.AST) -> Set[str]:
        literal = _const_str(operand)
        if literal is not None:
            return {literal}
        if isinstance(operand, ast.Name):
            return set(groups.get(operand.id, ()))
        if isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
            out: Set[str] = set()
            for elt in operand.elts:
                out |= _resolve(elt)
            return out
        return set()

    if isinstance(node, ast.Compare) and node.ops:
        if not isinstance(node.ops[0], (ast.In, ast.Eq)):
            return None
        if not _reads_a_suffix(node.left):
            return None
        literals: Set[str] = set()
        for comparator in node.comparators:
            literals |= _resolve(comparator)
        if literals and literals <= _NATIVE_IMAGE_SUFFIXES:
            return _root_name(node.left)
        return None

    if isinstance(node, ast.Call) and _dotted(node.func).endswith("endswith"):
        literals = set()
        for arg in node.args:
            literals |= _resolve(arg)
        if literals and literals <= _NATIVE_IMAGE_SUFFIXES:
            return _root_name(node.func)
        return None

    return None


def _interpreter_argv_over(node: ast.AST, name: str) -> Optional[ast.AST]:
    """The argv literal inside `node` that prefixes an interpreter onto a
    value derived from `name`. Slot 1 only, for the reason `_use_shape`
    gives: a later element is an argument TO the script, not the script."""
    for sub in ast.walk(node):
        if not isinstance(sub, (ast.List, ast.Tuple)) or len(sub.elts) < 2:
            continue
        if not _is_interpreter_expr(sub.elts[0]):
            continue
        if _root_name(sub.elts[1]) == name:
            return sub
    return None


def _suffix_dispatch_sites(analysis: "_ModuleAnalysis") -> List[Tuple[str, ast.AST]]:
    """``(scope name, argv node)`` for every negative-suffix dispatch in this
    module. Independent of producers and params -- that independence is the
    point of the arm."""
    groups = _module_string_groups(analysis.tree) if isinstance(analysis.tree, ast.Module) else {}
    sites: List[Tuple[str, ast.AST]] = []
    for index, (scope_name, scope_node, _body) in enumerate(analysis.scopes):
        if _is_exempt_among(analysis.own[index]):
            continue
        for stmts in _statement_lists(scope_node):
            for position, stmt in enumerate(stmts):
                if not isinstance(stmt, ast.If):
                    continue
                tested = _native_suffix_test(stmt.test, groups)
                if tested is None:
                    continue
                # The false branch is `orelse` when written, and the
                # statements AFTER the `if` when the true branch exits --
                # the bare guard-clause shape the live defect used.
                false_branch: List[ast.stmt] = list(stmt.orelse)
                if not false_branch and _unconditionally_exits(stmt.body):
                    false_branch = list(stmts[position + 1 :])
                for candidate in false_branch:
                    argv = _interpreter_argv_over(candidate, tested)
                    if argv is not None:
                        sites.append((scope_name, argv))
                        break
    return sites


def producers_in_source(text: str) -> Set[str]:
    """Function names in ``text`` that RETURN a settings-home ``bin/`` path.

    Exposed so a test can assert the negative -- that a resolver returning
    ``<settings-home>/bin/_machine_local.py`` is NOT a producer -- without
    reaching into the analysis internals.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    analysis = _ModuleAnalysis("<source>", tree)
    analysis.refresh(set(), {})
    return analysis.producer_names(set())


def classify_python_source(
    relpath: str,
    text: str,
    *,
    root_label: str = "<root>",
    producers: Optional[Set[str]] = None,
    params: Optional[Dict[str, Set[str]]] = None,
) -> List[Finding]:
    """Findings for one Python source file. Pure: no I/O, no repo access.

    ``producers``/``params`` carry the repo-wide taint sets. Called with
    neither, the analysis is file-local -- enough for a planted control,
    never enough for the live census.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    analysis = _ModuleAnalysis(relpath, tree)
    return _findings_for(analysis, root_label, producers or set(), params or {})


def _findings_for(
    analysis: "_ModuleAnalysis",
    root_label: str,
    producers: Set[str],
    params: Dict[str, Set[str]],
) -> List[Finding]:
    findings: List[Finding] = []
    analysis.refresh(producers, params)
    for index, (name, node, _body) in enumerate(analysis.scopes):
        tainted = analysis.tainted[index]
        candidates = [
            (sub, shape)
            for sub, shape in (
                (sub, _use_shape(analysis, sub, tainted, producers))
                for sub in analysis.own[index]
            )
            if shape
        ]
        # The exemption walk runs ONLY for a scope that produced a candidate.
        # Asking it of all 14k scopes cost a quarter of the scan to answer
        # "no" about code with nothing to exempt. Per-candidate, not
        # per-scope (code-reviewer Finding 2, 2026-09-02): an exemption that
        # doesn't gate a given handoff must not clear it just for sharing a
        # function with one that does.
        if not candidates:
            continue
        candidates = [
            (sub, shape) for sub, shape in candidates if not _call_site_is_exempt(node, sub)
        ]
        if not candidates:
            continue
        for sub, shape in candidates:
            findings.append(
                Finding(
                    root_label=root_label,
                    relpath=analysis.relpath,
                    lineno=getattr(sub, "lineno", 0),
                    scope=name,
                    shape=shape,
                )
            )
    seen = {(f.lineno, f.scope) for f in findings}
    for scope_name, argv in _suffix_dispatch_sites(analysis):
        lineno = getattr(argv, "lineno", 0)
        if (lineno, scope_name) in seen:
            continue
        seen.add((lineno, scope_name))
        findings.append(
            Finding(
                root_label=root_label,
                relpath=analysis.relpath,
                lineno=lineno,
                scope=scope_name,
                shape=_SUFFIX_DISPATCH_SHAPE,
            )
        )
    return findings


def _use_shape(
    analysis: "_ModuleAnalysis", node: ast.AST, tainted: Set[str], producers: Set[str]
) -> Optional[str]:
    if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
        # SLOT 1 ONLY. An interpreter's script is argv[1]; a later element is
        # an argument TO that script, and flagging one reported a cache key
        # passed to a same-named function in another file as a launch.
        if _is_interpreter_expr(node.elts[0]) and analysis._is_tainted(
            node.elts[1], tainted, producers
        ):
            return "argv literal prefixes an interpreter onto a settings-home bin/ path"
    if isinstance(node, ast.Call):
        dotted = _dotted(node.func)
        leaf = dotted.rsplit(".", 1)[-1]
        # `re.compile` is not `compile`. Builtins are matched undotted, the
        # module-qualified forms by their own full name.
        builtin_form = dotted in {"exec", "compile"} or dotted in {
            "runpy.run_path",
            "run_path",
            "importlib.util.spec_from_file_location",
            "util.spec_from_file_location",
            "spec_from_file_location",
        }
        if builtin_form and leaf in _SOURCE_EXEC_CALLS and any(
            analysis._is_tainted(a, tainted, producers) for a in node.args
        ):
            return f"`{leaf}` executes a settings-home bin/ path as Python source"
        if leaf == "open" and node.args and analysis._is_tainted(node.args[0], tainted, producers):
            mode = _const_str(node.args[1]) if len(node.args) > 1 else None
            mode = mode or next((_const_str(k.value) for k in node.keywords if k.arg == "mode"), None)
            if mode is None or "b" not in mode:
                return "text-mode read of a settings-home bin/ path (may be a native image)"
        if leaf in _TEXT_READ_ATTRS and isinstance(node.func, ast.Attribute):
            if analysis._is_tainted(node.func.value, tainted, producers):
                return "text-mode read of a settings-home bin/ path (may be a native image)"
    return None


# ---------------------------------------------------------------------------
# Shell arm
# ---------------------------------------------------------------------------


def classify_text_source(relpath: str, text: str, *, root_label: str = "<root>") -> List[Finding]:
    """Findings for the emitted-shell arm. Pure: no I/O, no repo access.

    Follows the variable, not merely the file. A shell variable assigned a
    ``${COORDINATOR_SETTINGS_HOME:-...}/bin/<name>`` rung -- directly, or
    shell-doc-ok: the rung spelling is the detector's own subject matter, not
    a command anyone is meant to paste.
    through a Python name holding that literal, which is how the generated
    hook is built -- and then run through an interpreter is the shape that
    took every ``git commit`` down. A file-level conjunction was tried first
    and reported three prose passages that merely QUOTE the rung, which is
    how a guard earns its way onto an ignore list.

    A native-image header probe anywhere in the emitted body clears the
    file: that is the shell spelling of the magic-byte refusal, and the
    generated hook carries one today.
    """
    if not _SHELL_BIN_RE.search(text) or _SHELL_NATIVE_PROBE_RE.search(text):
        return []

    home_names = {m.group(1) for m in _PY_HOME_ASSIGN_RE.finditer(text)}
    carriers: Set[str] = set()
    for match in _SHELL_ASSIGN_RE.finditer(text):
        name, value = match.group(1), match.group(2)
        if _SHELL_BIN_RE.search(value) or any("{" + n + "}" in value for n in home_names):
            carriers.add(name)
    if not carriers:
        return []

    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in _SHELL_RUN_RES:
            for match in pattern.finditer(line):
                if match.group(1) in carriers:
                    return [
                        Finding(
                            root_label=root_label,
                            relpath=relpath,
                            lineno=lineno,
                            scope="<emitted shell>",
                            shape=(
                                f"emitted shell runs ${match.group(1)} -- a settings-home "
                                "bin/ rung -- through an interpreter with no native-image probe"
                            ),
                        )
                    ]
    return []


# ---------------------------------------------------------------------------
# Root resolution and scanning
# ---------------------------------------------------------------------------


def _git(root: str, args: Sequence[str]) -> List[str]:
    """Lines from one ``git`` invocation, or ``[]``.

    Exit 1 is ``git grep``'s "no match", not a failure. Anything else -- a
    root that is not a repo, a git that is not installed -- collapses to "no
    candidate files" rather than raising: an operator pointing ``--root`` at
    a plain directory gets a clean report, never a traceback.

    Routed through `coordinator_core.git.run.run_git` rather than a private
    `subprocess.run` -- this module had its own git spawn, which is exactly
    the shape `test_shared_git_runner::
    test_no_new_private_git_runner_outside_the_frozen_inventory` exists to
    catch (code-reviewer Finding 2, 2026-09-02).
    """
    result = run_git(["-C", root, *args])
    if result.returncode not in (0, 1):
        return []
    return [line for line in result.stdout.splitlines() if line]


def _excluded(relpath: str) -> bool:
    if relpath.startswith(CONTROL_FIXTURE_DIR):
        return True
    return any(relpath.startswith(prefix) for prefix in _PROSE_DIR_PREFIXES)


def _read(root: str, relpath: str) -> Optional[str]:
    try:
        with open(os.path.join(root, relpath), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def scan_root(root: str, *, root_label: Optional[str] = None) -> List[Finding]:
    """Every interpreter-mediated settings-home ``bin/`` site under ``root``.

    Candidate selection is a fixed set of ``git grep`` invocations: the
    settings-home compositions, the producer names the first stage found,
    the suffix arm's own precondition pair, and the shell arm. No process is
    spawned per file.
    """
    label = root_label or os.path.basename(os.path.abspath(root))

    # Four FIXED-STRING greps, deliberately, not one regex: `--all-match` is
    # file-level, so each demands both halves of a composition in the same
    # file, and `-F` was measured cheaper end-to-end than the two-invocation
    # `-E` form it replaced (7.3s against 8.0s total process time -- `git
    # grep -E` spends more CPU than the extra blob passes cost). Greps 1-2
    # are the `os.path.join(<home>, "bin", ...)` / `<home> / "bin" / ...`
    # forms; greps 3-4 are the shell-literal form, which carries no quoted
    # segment.
    stage_one = [
        p
        for p in _git(
            root,
            ["grep", "-lI", "--all-match", "-F", "-e", _SETTINGS_HOME_TOKEN, "-e", '"bin"',
             "--", "*.py"],
        )
        + _git(
            root,
            ["grep", "-lI", "--all-match", "-F", "-e", _SETTINGS_HOME_TOKEN, "-e", "'bin'",
             "--", "*.py"],
        )
        + _git(
            root,
            ["grep", "-lI", "--all-match", "-F", "-e", _SHELL_HOME_TOKENS[0], "-e", "/bin/",
             "--", "*.py"],
        )
        + _git(
            root,
            ["grep", "-lI", "--all-match", "-F", "-e", _SHELL_HOME_TOKENS[1], "-e", "/bin/",
             "--", "*.py"],
        )
        if not _excluded(p)
    ]

    # Fifth grep: the suffix-dispatch arm's own selector. It shares nothing
    # with the four above ON PURPOSE -- those demand a settings-home token,
    # which is precisely what the parameter-taking helper does not have. A
    # file must name an interpreter AND a native-image suffix literal to
    # carry this shape, so `--all-match` over those two is the whole filter.
    # Two greps intersected, NOT one `--all-match`: `--all-match` is
    # all-of-the-patterns, so a single invocation would demand every
    # interpreter token AND every suffix literal in the same file and select
    # nothing. Each grep here is an OR over its own token set; the AND is the
    # set intersection below.
    def _any_of(tokens: Sequence[str]) -> List[str]:
        args = ["grep", "-lI", "-F"]
        for token in tokens:
            args += ["-e", token]
        args += ["--", "*.py"]
        return _git(root, args)

    # Deliberately NOT merged into `stage_one`. The taint arms' candidate
    # population is a measured precision decision; widening it as a side
    # effect of this arm re-opens it without review. Measured when this arm
    # landed: merging surfaced one finding in `launchable.py:_shebang_
    # launcher`, and that one is FALSE -- it reads a single line with
    # `errors="replace"`, so a Mach-O yields no shebang match and an empty
    # prefix, which is a header sniff wearing a text read's clothes. These
    # files feed `_suffix_dispatch_sites` and nothing else.
    # Two greps intersected on the arm's OWN preconditions, which is why this
    # is exact rather than a heuristic narrowing: the shape needs a
    # native-image suffix literal AND a suffix read, both by construction.
    # Measured here -- `.exe` alone selects 1335 files, a suffix-read token
    # alone 676, the intersection 234. The interpreter half is applied in
    # Python at the parse site instead of as a third grep: its cheapest token
    # is `python`, which matched 2694 files and cost more than the reads it
    # would have saved.
    # `stage_one` files are subtracted because `_findings_for` already runs
    # the suffix arm over every module the taint arms parsed.
    suffix_arm_selected = set(_any_of(_SUFFIX_ARM_FILE_TOKENS)) & set(
        _any_of(_SUFFIX_READ_FILE_TOKENS)
    )
    suffix_arm_files = sorted(
        p for p in suffix_arm_selected - set(stage_one) if not _excluded(p)
    )

    analyses: Dict[str, _ModuleAnalysis] = {}
    for relpath in stage_one:
        text = _read(root, relpath)
        if text is None:
            continue
        try:
            analyses[relpath] = _ModuleAnalysis(relpath, ast.parse(text))
        except SyntaxError:
            continue

    producers: Set[str] = set()
    params = _ParamTaint(analyses)
    for relpath, analysis in analyses.items():
        analysis.refresh(producers, params.for_file(relpath))
        producers |= analysis.producer_names(producers)
    for relpath, analysis in analyses.items():
        analysis.refresh(producers, params.for_file(relpath))
        params.record(relpath, analysis.tainted_params(producers))

    # Stage two: a consumer that only ever sees the path as a parameter need
    # not mention the settings home at all -- `_session_claim_cli_argv(cli_path)`
    # is the live example. Pull in every file naming a producer, in one grep.
    if producers:
        # A consumer file must ALSO name an interpreter to be a defect, so
        # `--all-match` demands both -- one grep instead of parsing every
        # file that merely mentions a resolver.
        args = ["grep", "-lI", "--all-match", "-F"]
        for name in sorted(producers):
            args += ["-e", name]
        for token in _INTERPRETER_FILE_TOKENS:
            args += ["-e", token]
        args += ["--", "*.py"]
        for relpath in _git(root, args):
            if _excluded(relpath) or relpath in analyses:
                continue
            text = _read(root, relpath)
            if text is None:
                continue
            try:
                analyses[relpath] = _ModuleAnalysis(relpath, ast.parse(text))
            except SyntaxError:
                continue
        params.add_definitions(analyses)
        for relpath, analysis in analyses.items():
            analysis.refresh(producers, params.for_file(relpath))
            params.record(relpath, analysis.tainted_params(producers))

    findings: List[Finding] = []
    for relpath, analysis in analyses.items():
        findings += _findings_for(analysis, label, producers, params.for_file(relpath))

    # The suffix arm over its own population. A file already analysed above
    # was covered there; the rest are parsed only for this question.
    for relpath in suffix_arm_files:
        if relpath in analyses:
            continue
        text = _read(root, relpath)
        if text is None:
            continue
        # The interpreter half of the selector, applied here rather than as a
        # second whole-tree grep -- see `suffix_arm_files` for the measurement.
        if not any(token in text for token in _INTERPRETER_FILE_TOKENS):
            continue
        try:
            analysis = _ModuleAnalysis(relpath, ast.parse(text))
        except SyntaxError:
            continue
        for scope_name, argv in _suffix_dispatch_sites(analysis):
            findings.append(
                Finding(
                    root_label=label,
                    relpath=relpath,
                    lineno=getattr(argv, "lineno", 0),
                    scope=scope_name,
                    shape=_SUFFIX_DISPATCH_SHAPE,
                )
            )

    # Pathspec-scoped, and not by taste: unscoped, this grep reads every one
    # of ~39k tracked blobs and measured 6.5s of SYSTEM time on its own. The
    # specs cover exactly the suffixes `_TEXT_ARM_SUFFIXES` admits, plus the
    # extensionless launcher directories, minus the prose corpora.
    for relpath in _git(
        root,
        [
            "grep", "-lI", "-F",
            "-e", _SHELL_HOME_TOKENS[0], "-e", _SHELL_HOME_TOKENS[1],
            "--",
            "*.py", "*.sh", "*.bash", "*.cmd", "*.ps1",
            "bin/*", "coordinator/bin/*", "coordinator/hooks/*", "scripts/*",
            ":!docs", ":!state", ":!archive", ":!tasks", ":!cross-repo",
        ],
    ):
        if _excluded(relpath):
            continue
        if os.path.splitext(relpath)[1] not in _TEXT_ARM_SUFFIXES:
            continue
        text = _read(root, relpath)
        if text is None:
            continue
        findings += classify_text_source(relpath, text, root_label=label)

    return sorted(findings, key=lambda f: (f.relpath, f.lineno))


def resolve_roots(
    extra: Optional[Iterable[str]] = None,
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """``([(label, path), ...], [skip reason, ...])``.

    This repo is always scanned. The DoE-claude plane is scanned when it
    resolves through the engine's OWN resolver
    (``coordinator_core.ops.coordinator_doe_root``, which walks the
    documented rung chain: env override, machine-local ``repos.doe_claude``,
    the ``.doe-root`` pointer, then the marketplace-cache rungs) -- no second
    absolute path is hardcoded here and no resolver is reinvented. A box with
    no DoE clone is a normal box: the skip and its reason are reported, and
    the run stays green on the roots it did scan.
    """
    roots: List[Tuple[str, str]] = []
    skips: List[str] = []

    here = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    roots.append(("claude-klabauter", here))

    for path in extra or ():
        expanded = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(expanded):
            roots.append((os.path.basename(expanded), expanded))
        else:
            skips.append(f"--root {path!r}: not a directory")

    if not any(label == "DoE-claude" for label, _ in roots):
        try:
            from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

            doe = coordinator_doe_root()
        except Exception as exc:  # resolver unavailable is a skip, never a failure
            doe, exc_text = None, str(exc)
            skips.append(f"DoE-claude: resolver unavailable ({exc_text})")
        else:
            if not doe:
                skips.append(
                    "DoE-claude: coordinator_doe_root() resolved nothing "
                    "(no DOE_ROOT/REPO_DOE_CLAUDE, no machine-local repos.doe_claude, "
                    "no .doe-root pointer, no marketplace cache)"
                )
            elif not os.path.isdir(doe):
                skips.append(f"DoE-claude: resolved to {doe}, which is not a directory")
            else:
                roots.append(("DoE-claude", doe))

    return roots, skips


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-native-door-interpreter-handoff",
        description=(
            "Census every site that composes a settings-home bin/ path and hands it "
            "to a Python interpreter. RED on any, in any scanned root."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional repo root to scan. Repeatable. This repo is always scanned; "
        "the DoE-claude plane is added automatically when it resolves.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    roots, skips = resolve_roots(args.root)

    total = 0
    for label, path in roots:
        findings = scan_root(path, root_label=label)
        total += len(findings)
        if findings:
            print(f"FAIL [{label}] {path}: {len(findings)} interpreter-mediated site(s):")
            for finding in findings:
                print(finding.render())
        else:
            print(f"OK [{label}] {path}: no interpreter-mediated settings-home bin/ site.")

    for skip in skips:
        print(f"SKIP {skip}")

    if total:
        print(f"\nFix: {REMEDY}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
