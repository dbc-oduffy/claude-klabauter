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
header. All are fixed; this module exists so the population stays at zero
without anyone remembering the rule.

WHAT IS SCANNED. Tracked source in one or more repo roots -- this repo by
default, plus the DoE-claude plane when it resolves (see ``resolve_roots``).
Candidate files are selected with a handful of batched ``git grep``
invocations per root; nothing spawns a process per file.

THE THREE ARMS, and the incident each one answers.

  - **Python arm** (``classify_python_source``): a FILE-LOCAL taint. A
    *composition* is ``os.path.join(<settings-home>, "bin", ...)``, a
    ``<settings-home> / "bin" / ...`` pathlib chain, or a string literal
    naming ``${COORDINATOR_SETTINGS_HOME...}/bin/`` /
    ``.coordinator-claude-settings/bin/``. A function that RETURNS one is a
    *producer*; a call to a producer taints, and an argument passed tainted
    taints that parameter in a callee DEFINED IN THE SAME FILE. The defect
    is a tainted value reaching an interpreter: ``[sys.executable, path]``,
    ``["python3", path]``, ``runpy.run_path``, ``spec_from_file_location``,
    ``exec``/``compile``, or a TEXT read (``open()`` in text mode,
    ``read_text()``) of a path that may be a binary image. This is the arm
    the two live exempted sites exercise; both compose and hand off inside
    one file, which is why the taint stops there (see the negative-spec).
  - **Shell arm** (``classify_text_source``): the same defect in emitted
    shell -- a rung that resolves ``${COORDINATOR_SETTINGS_HOME:-...}/bin/
    <name>`` and then runs it as ``"$_PY" "$_T"``.
    shell-doc-ok: those two spellings ARE the defect this arm detects; a
    docstring that cannot name the shape cannot document the detector.
    Applied to the raw text of shell/launcher files AND of ``.py`` files,
    because the hook that broke every commit was a shell body built from
    Python string literals: an AST-only guard cannot see it.
  - **Suffix-dispatch arm** (``_suffix_dispatch_sites``): does a scope decide
    native-vs-script FROM THE PATH'S SUFFIX and prefix an interpreter on the
    branch where a NATIVE-image suffix test came out FALSE? Needs no taint
    and no co-occurrence, so it reaches the file the other arms cannot see:
    a helper taking the bin DIRECTORY as a parameter names no settings-home
    token at all. The DoE plane's ``_forwarder_resolve.forwarder_argv`` was
    exactly that, and is the ONE live defect this census has ever found.
    The asymmetry is the whole precision story: testing that a suffix IS
    ``.py`` and interpreting on the TRUE branch is safe, because a door
    image never occupies such a name; testing that a suffix is ``.exe`` and
    interpreting on the FALSE branch is the defect, because on POSIX the
    image IS the extensionless bare name and lands in that branch.

THE REMEDY, AND WHY IT IS ONE NAME. ``coordinator_core.launchable.
resolve_launchable`` already returns a bare path for a native image and
prefixes an interpreter only for a non-executable ``.py``. That is precisely
why roughly 370 other cut-over names survived the same cutover untouched.
Delegating is the fix; a second hand-rolled answer to the same question is
how this class recurs.

THE THREE SHAPES THAT ARE NOT DEFECTS, and are recognised as such:

  1. **Delegation** -- ``[*resolve_launchable(path), *args]``. Produces no
     finding structurally (the interpreter is never named at the call site).
  2. **Magic-byte refusal** -- the enclosing gate reads the bytes and
     compares against ``door_install.NATIVE_IMAGE_MAGIC`` before deciding
     (``coordinator_core.ops.install_health_run``'s launch-chain leg). The
     shell arm's equivalent is a ``_native``-style header probe, which the
     generated hook body carries.
  3. **Exec-bit discrimination at call time** -- ``os.access(path, os.X_OK)``
     gating a bare-path return, with the interpreter as the fallback
     (``wsc-session-disposition._session_claim_cli_argv``,
     ``workday-complete-reconcile._cruft_sweep_argv``). Not the preferred
     shape -- it is a second answer to the question shape 1 already answers,
     and is drift-prone -- but it is CORRECT, it is what the fixed sites
     landed, and REDing already-fixed code would train authors to route
     around this guard.

An exemption is read at the flagged call site's own GATE, never over the
enclosing function: see ``_enclosing_gate``.

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
  - **The taint is FILE-LOCAL, and widening it back is not an improvement.**
    A repo-wide producer/parameter registry keyed by unqualified callee name
    was built first and then measured: it never produced a finding in either
    root, while its own ambiguity (half this tree has a local ``_run(...)``)
    reported seven false sites until a "defined in exactly one file
    repo-wide" rule was added to suppress them. Every site this census has
    ever had to reason about -- the two live exempted ones, the planted
    control, the one real DoE defect -- composes and hands off within one
    file, or carries no composition at all and belongs to the suffix arm.
    A cross-file registry is therefore an unexercised capability whose whole
    cost is the machinery that makes its false positives tolerable.
  - **The absence of a finding is not the absence of the hazard.** This
    guard decides statically over a taint that stops at the first
    indirection it cannot follow -- a path routed through a dict, a class
    attribute, or a subprocess argv assembled in another file is invisible
    to it. It is a ratchet on the shapes that have actually occurred, not a
    proof. Widen it when a new shape occurs; do not read green as
    "impossible".
  - **This guard never blocks on a second root being absent.** A box with no
    DoE clone is a normal box, not a failure. It reports the skip and its
    reason and stays green on the roots it did scan.
  - **No baseline, ever.** The population is a handful of sites and was
    driven to zero before this file existed. A baseline here would be the
    grandfather slot that lets the next one in silently.
  - **No per-file subprocess.** Candidate selection is a fixed number of
    ``git grep`` calls per root. A loop of ``git`` calls over 39k tracked
    files is a brightline violation, not an implementation detail.
  - **This is a whole-tree census run at CADENCE gates**, never on a session,
    commit or ceremony hot path -- the two slow tests carry
    ``@pytest.mark.cadence``. The shape that would put it under the
    brightline is an INCREMENTAL scan over the staged diff, not a faster
    whole-tree walk. Build that if a hot-path caller ever needs it, and do
    not mistake the whole-tree run for one.

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

_INTERPRETER_FILE_TOKENS = ("sys.executable", "python", "run_path", "read_text")

# Nor can a file carry a suffix DISPATCH without naming one of these: every
_SUFFIX_READ_FILE_TOKENS = (".suffix", "splitext", "endswith")

_SOURCE_EXEC_CALLS = frozenset({"run_path", "spec_from_file_location", "exec", "compile"})

_MAGIC_REFUSAL_TOKENS = ("NATIVE_IMAGE_MAGIC", "native_image", "is_native_image")

_NATIVE_IMAGE_SUFFIXES = frozenset({".exe", ".com"})
_SUFFIX_ARM_FILE_TOKENS = tuple(sorted(_NATIVE_IMAGE_SUFFIXES))

_SUFFIX_DISPATCH_SHAPE = (
    "interpreter prefixed on the branch where a native-image suffix test was FALSE"
)

_TEXT_ARM_SUFFIXES = frozenset({"", ".sh", ".bash", ".cmd", ".ps1", ".py"})

_PROSE_DIR_PREFIXES = (
    "docs/",
    "state/",
    "archive/",
    "tasks/",
    "cross-repo/",
    ".structural-index/",
)

CONTROL_FIXTURE_DIR = "coordinator_core/ops/tests/fixtures/native_door_handoff/"


@dataclass(frozen=True)
class Finding:

    root_label: str
    relpath: str
    lineno: int
    scope: str
    shape: str

    def render(self) -> str:
        return f"  [{self.root_label}] {self.relpath}:{self.lineno} in {self.scope} -- {self.shape}"


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


def _is_settings_home_rooted(node: ast.AST) -> bool:
    return _SETTINGS_HOME_TOKEN in _dotted(node).lower()


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
    JSON read. Dropping this rule reported 18 correct call sites on the first
    live run. The hazard is the bare name, or a name assembled from a
    variable that could be one.
    """
    text = _const_str(node)
    if text is None:
        return False
    tail = text.rstrip("/").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    suffix = os.path.splitext(tail)[1].lower()
    return bool(suffix) and suffix != ".exe"


def _is_composition(node: ast.AST) -> bool:
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
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.BinOp)
            and isinstance(node.left.op, ast.Div)
        ):
            setattr(node.left, _INNER_LINK_ATTR, True)


def _contains_composition(node: ast.AST) -> bool:
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


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _own_nodes(body: Sequence[ast.AST]) -> Iterable[ast.AST]:
    stack: List[ast.AST] = [stmt for stmt in body if not isinstance(stmt, _NESTED_SCOPES)]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _NESTED_SCOPES):
                stack.append(child)


def _statement_lists(scope_node: ast.AST) -> Iterable[List[ast.stmt]]:

    def walk(body: List[ast.stmt]) -> Iterable[List[ast.stmt]]:
        yield body
        for stmt in body:
            if isinstance(stmt, _NESTED_SCOPES):
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


class _FileAnalysis:

    def __init__(self, relpath: str, tree: ast.Module) -> None:
        self.relpath = relpath
        self.tree = tree
        _mark_inner_chain_links(tree)
        self.scopes: List[Tuple[str, ast.AST, List[ast.stmt]]] = []
        self._collect_scopes("<module>", tree, tree.body)
        self.own: List[List[ast.AST]] = [list(_own_nodes(body)) for _, _, body in self.scopes]
        self.producers: Set[str] = set()
        self.params: Dict[str, Set[str]] = {}
        self.tainted: List[Set[str]] = [set() for _ in self.scopes]
        self._settle()

    def _collect_scopes(self, name: str, node: ast.AST, body: List[ast.stmt]) -> None:
        self.scopes.append((name, node, body))
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_scopes(stmt.name, stmt, stmt.body)
            elif isinstance(stmt, ast.ClassDef):
                self._collect_scopes(name, stmt, stmt.body)

    def _settle(self) -> None:
        for _ in range(2):
            self._recompute()
            self.producers |= self._producer_names()
            self._recompute()
            for callee, slots in self._tainted_params().items():
                self.params.setdefault(callee, set()).update(slots)
        self._recompute()

    def _recompute(self) -> None:
        for index, (name, node, _body) in enumerate(self.scopes):
            seeded = _param_names_for(node, self.params.get(name, set()))
            self.tainted[index] = self._tainted_names(index, seeded)

    def _tainted_names(self, index: int, seeded: Set[str]) -> Set[str]:
        tainted = set(seeded)
        assignments = [
            stmt
            for stmt in self.own[index]
            if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        ]
        for _ in range(2):
            for stmt in assignments:
                if isinstance(stmt, ast.Assign):
                    targets, value = list(stmt.targets), stmt.value
                elif stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                else:
                    continue
                if not self.is_tainted(value, tainted):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        tainted.add(target.id)
        return tainted

    def is_tainted(self, node: ast.AST, tainted: Set[str]) -> bool:
        if _contains_composition(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in tainted
        if isinstance(node, ast.Call):
            leaf = _dotted(node.func).rsplit(".", 1)[-1]
            if leaf in self.producers:
                return True
            if leaf in {"str", "Path", "fspath", "abspath", "realpath", "expanduser"}:
                return any(self.is_tainted(a, tainted) for a in node.args)
            return False
        if isinstance(node, (ast.Attribute, ast.Starred)):
            return self.is_tainted(node.value, tainted)
        if isinstance(node, ast.JoinedStr):
            return any(
                self.is_tainted(v.value, tainted)
                for v in node.values
                if isinstance(v, ast.FormattedValue)
            )
        return False

    def _producer_names(self) -> Set[str]:
        found: Set[str] = set()
        for index, (name, node, _body) in enumerate(self.scopes):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for stmt in self.own[index]:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    if self.is_tainted(stmt.value, self.tainted[index]):
                        found.add(name)
                        break
        return found

    def _tainted_params(self) -> Dict[str, Set[str]]:
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
                    if self.is_tainted(arg, tainted):
                        found.setdefault(callee, set()).add(f"#{position}")
                for kw in call.keywords:
                    if kw.arg and self.is_tainted(kw.value, tainted):
                        found.setdefault(callee, set()).add(kw.arg)
        return found


def _param_names_for(node: ast.AST, slots: Set[str]) -> Set[str]:
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


def _is_header_sniff(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        dotted = _dotted(node.func)
        if dotted.endswith("resolve_launchable"):
            return True
        if dotted.endswith("startswith"):
            if any((_const_str(a) or "").startswith("#!") for a in node.args):
                return True
    return False


def _is_positive_suffix_test(node: ast.AST) -> bool:
    """The gate interprets only what it has POSITIVELY established to be a
    ``.py`` file (``cli_path.suffix == ".py"``, ``path.endswith(".py")``).

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


def _is_exempt_among(own: List[ast.AST]) -> bool:
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
    return bool(body) and isinstance(body[-1], _EXIT_STMTS)


def _index_containing(stmts: List[ast.stmt], target: ast.AST) -> Optional[int]:
    for index, stmt in enumerate(stmts):
        stack: List[ast.AST] = [stmt]
        while stack:
            node = stack.pop()
            if node is target:
                return index
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, _NESTED_SCOPES):
                    stack.append(child)
    return None


def _enclosing_gate(scope_node: ast.AST, target: ast.AST) -> Optional[ast.AST]:
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
    gate = _enclosing_gate(scope_node, target)
    if gate is None:
        return False
    return _is_exempt_among(list(_own_nodes(list(ast.iter_child_nodes(gate)))))


def _root_name(node: ast.AST) -> Optional[str]:
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
    if isinstance(node, (ast.Attribute, ast.Subscript)):
        return _reads_a_suffix(node.value)
    return False


def _module_string_groups(tree: ast.Module) -> Dict[str, Set[str]]:
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
    for sub in ast.walk(node):
        if not isinstance(sub, (ast.List, ast.Tuple)) or len(sub.elts) < 2:
            continue
        if not _is_interpreter_expr(sub.elts[0]):
            continue
        if _root_name(sub.elts[1]) == name:
            return sub
    return None


def _suffix_dispatch_sites(analysis: "_FileAnalysis") -> List[Tuple[str, ast.AST]]:
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
                false_branch: List[ast.stmt] = list(stmt.orelse)
                if not false_branch and _unconditionally_exits(stmt.body):
                    false_branch = list(stmts[position + 1 :])
                for candidate in false_branch:
                    argv = _interpreter_argv_over(candidate, tested)
                    if argv is not None:
                        sites.append((scope_name, argv))
                        break
    return sites


def _use_shape(analysis: "_FileAnalysis", node: ast.AST, tainted: Set[str]) -> Optional[str]:
    if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
        if _is_interpreter_expr(node.elts[0]) and analysis.is_tainted(node.elts[1], tainted):
            return "argv literal prefixes an interpreter onto a settings-home bin/ path"
    if isinstance(node, ast.Call):
        dotted = _dotted(node.func)
        leaf = dotted.rsplit(".", 1)[-1]
        builtin_form = dotted in {
            "exec",
            "compile",
            "runpy.run_path",
            "run_path",
            "importlib.util.spec_from_file_location",
            "util.spec_from_file_location",
            "spec_from_file_location",
        }
        if (
            builtin_form
            and leaf in _SOURCE_EXEC_CALLS
            and any(analysis.is_tainted(a, tainted) for a in node.args)
        ):
            return f"`{leaf}` executes a settings-home bin/ path as Python source"
        if leaf == "open" and node.args and analysis.is_tainted(node.args[0], tainted):
            mode = _const_str(node.args[1]) if len(node.args) > 1 else None
            mode = mode or next((_const_str(k.value) for k in node.keywords if k.arg == "mode"), None)
            if mode is None or "b" not in mode:
                return "text-mode read of a settings-home bin/ path (may be a native image)"
        if leaf == "read_text" and isinstance(node.func, ast.Attribute):
            if analysis.is_tainted(node.func.value, tainted):
                return "text-mode read of a settings-home bin/ path (may be a native image)"
    return None


def _findings_for(analysis: "_FileAnalysis", root_label: str) -> List[Finding]:
    findings: List[Finding] = []
    for index, (name, node, _body) in enumerate(analysis.scopes):
        tainted = analysis.tainted[index]
        candidates = [
            (sub, shape)
            for sub, shape in (
                (sub, _use_shape(analysis, sub, tainted)) for sub in analysis.own[index]
            )
            if shape and not _call_site_is_exempt(node, sub)
        ]
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


def _analyse(relpath: str, text: str) -> Optional["_FileAnalysis"]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    return _FileAnalysis(relpath, tree)


def classify_python_source(relpath: str, text: str, *, root_label: str = "<root>") -> List[Finding]:
    analysis = _analyse(relpath, text)
    return [] if analysis is None else _findings_for(analysis, root_label)


def producers_in_source(text: str) -> Set[str]:
    analysis = _analyse("<source>", text)
    return set() if analysis is None else set(analysis.producers)


_SHELL_BIN_RE = re.compile(
    r"(?:COORDINATOR_SETTINGS_HOME|coordinator-claude-settings)[^\n]{0,160}?/bin/"
)
_SHELL_ASSIGN_RE = re.compile(r"""(?m)^[^\n]*?\b([A-Za-z_][A-Za-z0-9_]*)=["']?([^"'\n]*)""")
_PY_HOME_ASSIGN_RE = re.compile(
    r"""(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[\s\S]{0,300}?"""
    r"""(?:COORDINATOR_SETTINGS_HOME|coordinator-claude-settings)"""
)
_SHELL_RUN_RES = (
    re.compile(r"""["']?\$\{?_?[Pp][Yy][A-Za-z0-9_]*\}?["']?\s+["']?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"""),
    re.compile(r"""\bpython3?\b\s+["']?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"""),
    re.compile(r"""%_?py%\s+["']?%([A-Za-z_][A-Za-z0-9_]*)%"""),
)
_SHELL_NATIVE_PROBE_RE = re.compile(r"_native\b|NATIVE_IMAGE_MAGIC|\\x7fELF|\\xcf\\xfa\\xed")


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


def _git(root: str, args: Sequence[str]) -> List[str]:
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


def _taint_arm_files(root: str) -> List[str]:
    """Files that could carry a composition: four FIXED-STRING greps, not one
    regex. ``--all-match`` is file-level, so each demands both halves of a
    composition in the same file, and ``-F`` measured cheaper end-to-end than
    the ``-E`` form it replaced. Greps 1-2 are the ``os.path.join(<home>,
    "bin", ...)`` / ``<home> / "bin" / ...`` forms; greps 3-4 are the
    shell-literal form, which carries no quoted segment."""
    selected: Set[str] = set()
    for first, second in (
        (_SETTINGS_HOME_TOKEN, '"bin"'),
        (_SETTINGS_HOME_TOKEN, "'bin'"),
        (_SHELL_HOME_TOKENS[0], "/bin/"),
        (_SHELL_HOME_TOKENS[1], "/bin/"),
    ):
        selected |= set(
            _git(root, ["grep", "-lI", "--all-match", "-F", "-e", first, "-e", second, "--", "*.py"])
        )
    return sorted(p for p in selected if not _excluded(p))


def _suffix_arm_files(root: str) -> List[str]:
    """Files that could carry a negative-suffix dispatch. Shares nothing with
    the taint arm's selector ON PURPOSE -- that one demands a settings-home
    token, which is precisely what the parameter-taking helper does not have.

    Two greps intersected on the arm's OWN preconditions, which is why this
    is exact rather than a heuristic narrowing: the shape needs a
    native-image suffix literal AND a suffix read, both by construction. Not
    one ``--all-match`` -- that is all-of-the-patterns, so a single
    invocation would demand every suffix literal in the same file and select
    nothing. Each grep is an OR over its own token set; the AND is the set
    intersection.

    Measured (claude-klabauter-7f, 2026-09-02, this repo): ``.exe``/``.com``
    alone selects 1335 files, a suffix-read token alone 676, the intersection
    234. The INTERPRETER half is deliberately NOT a third grep and is applied
    in Python at the parse site instead -- its cheapest token is ``python``,
    which matched 2694 files and cost more than the reads it would have
    saved.

    Deliberately NOT merged into the taint arm's population, whose precision
    is a measured decision: merging surfaced one finding in
    ``launchable._shebang_launcher``, and that one is FALSE -- it reads a
    single line with ``errors="replace"``, so a Mach-O yields no shebang
    match and an empty prefix, which is a header sniff wearing a text read's
    clothes.
    """

    def _any_of(tokens: Sequence[str]) -> Set[str]:
        args = ["grep", "-lI", "-F"]
        for token in tokens:
            args += ["-e", token]
        args += ["--", "*.py"]
        return set(_git(root, args))

    both = _any_of(_SUFFIX_ARM_FILE_TOKENS) & _any_of(_SUFFIX_READ_FILE_TOKENS)
    return sorted(p for p in both if not _excluded(p))


def _text_arm_files(root: str) -> List[str]:
    """Files whose raw text could carry an emitted settings-home rung.

    Pathspec-scoped, and not by taste: unscoped, this grep reads every one of
    ~39k tracked blobs and measured 6.5s of SYSTEM time on its own. The specs
    cover exactly the suffixes ``_TEXT_ARM_SUFFIXES`` admits, plus the
    extensionless launcher directories, minus the prose corpora.
    """
    lines = _git(
        root,
        [
            "grep", "-lI", "-F",
            "-e", _SHELL_HOME_TOKENS[0], "-e", _SHELL_HOME_TOKENS[1],
            "--",
            "*.py", "*.sh", "*.bash", "*.cmd", "*.ps1",
            "bin/*", "coordinator/bin/*", "coordinator/hooks/*", "scripts/*",
            ":!docs", ":!state", ":!archive", ":!tasks", ":!cross-repo",
        ],
    )
    return [
        p
        for p in lines
        if not _excluded(p) and os.path.splitext(p)[1] in _TEXT_ARM_SUFFIXES
    ]


def scan_root(root: str, *, root_label: Optional[str] = None) -> List[Finding]:
    label = root_label or os.path.basename(os.path.abspath(root))
    findings: List[Finding] = []
    analysed: Set[str] = set()

    for relpath in _taint_arm_files(root):
        text = _read(root, relpath)
        if text is None:
            continue
        analysis = _analyse(relpath, text)
        if analysis is None:
            continue
        analysed.add(relpath)
        findings += _findings_for(analysis, label)

    for relpath in _suffix_arm_files(root):
        if relpath in analysed:
            continue
        text = _read(root, relpath)
        if text is None:
            continue
        if not any(token in text for token in _INTERPRETER_FILE_TOKENS):
            continue
        analysis = _analyse(relpath, text)
        if analysis is None:
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

    for relpath in _text_arm_files(root):
        text = _read(root, relpath)
        if text is not None:
            findings += classify_text_source(relpath, text, root_label=label)

    return sorted(findings, key=lambda f: (f.relpath, f.lineno))


def resolve_roots(
    extra: Optional[Iterable[str]] = None,
) -> Tuple[List[Tuple[str, str]], List[str]]:
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
        except Exception as exc:
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
