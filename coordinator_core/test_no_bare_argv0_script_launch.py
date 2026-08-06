"""Repo-wide guard: no `coordinator_core` subprocess launch passes a bare
``.js``/``.sh``/``.cjs``/``.mjs``/``.bash`` script path as ``argv[0]``.

The defect this guards: three ported ops (``query_completions.py``,
``render_template_tree.py``, ``verify_no_powershell_flash.py``) launched an
external script by handing its bare path straight to ``subprocess.run([...])``
as ``argv[0]``, relying on the POSIX exec loader to honour the script's ``#!``
line. Windows ``CreateProcess`` has no such loader and refuses the file with
``OSError: [WinError 193] %1 is not a valid Win32 application`` -- a 100%
failure rate on that platform. All three were fixed (``0a356897``,
``5c921625``) by routing through ``coordinator_core.launchable.resolve_launchable``.

``coordinator_core.plugin_health.tests.test_sentinel`` used to carry a guard
for this exact bug class scoped to four specific sentinel probes (P-9/P-11/
P-13/P-18); those four have since been repointed to call their ported
sibling modules' ``main()`` in-process (no bash spawn, no ``_sh_argv`` at
all -- see ``sentinel.py``'s module docstring), so that scoped guard no
longer applies and the three ported ops above were never in its denominator
either way, so nothing prevented (and nothing would have caught) a fourth
site reintroducing the pattern. This module widens the guard from four
instances to the class: it statically walks the AST of every
production module under ``coordinator_core`` and asserts that any
``subprocess.{run,Popen,call,check_call,check_output}([...])`` call whose
``argv[0]`` traces back to a string literal ending in a script extension is
wrapped through ``resolve_launchable()`` (directly, or via a traceable local
assignment) rather than passed bare.

Negative-spec -- scope of this guard, read before extending it:
    - This test is a STATIC scan, not an execution harness. It does not import
      or run any of the scanned modules; it only parses their source with
      ``ast``. Do NOT widen it into an execution-based check -- the ask this
      guards against was explicit that a static/AST scan is the right shape
      here ("do not attempt to execute every subprocess call site").
    - Test files (``test_*.py`` / ``*_test.py`` / anything under a ``tests/``
      dir) are excluded from the scan. They routinely monkeypatch
      ``subprocess.run`` with fakes and pass literal fixture strings that look
      like script paths but launch nothing real -- scanning them would produce
      noise, not signal.
    - **Extension-literal detection only -- the extensionless case is
      deliberately OUT OF SCOPE for this test.** An extensionless bare path
      (e.g. the ``machine-local`` binary, resolved via ``shutil.which`` or a
      raw ``~/.claude/bin/machine-local`` join in a couple dozen call sites
      across this tree) cannot be distinguished, at the pure-syntax level,
      from a legitimate system-executable literal (``git``, ``bash``, ``node``,
      ``rg``, ...) without a hand-built allowlist of every command name ever
      spawned in this codebase -- a maintenance trap that degrades into "the
      test passed because it stopped looking." The ``machine-local`` /
      ``resolve-coordinator-clone`` extensionless-binary-resolution convention
      is a separate, much wider, pre-existing pattern that deserves its own
      dedicated audit; conflating it with this test's mechanically-decidable
      extension-literal check would either bury real positives under a pile
      of allowlist noise or silently narrow the pattern until it stopped
      finding anything. This test's contract is narrower and unambiguous: literal
      ``.js``/``.cjs``/``.mjs``/``.sh``/``.bash`` extensions used bare as
      ``argv[0]``.
    - Known PRE-EXISTING violations of the in-scope (extension-literal) check,
      discovered while authoring this test on 2026-07-21, are named in
      ``_KNOWN_PRE_EXISTING_VIOLATIONS`` below with an explicit comment. These
      are real, unfixed instances of the same bug class -- NOT legitimate
      design exceptions -- and are surfaced verbatim in the executor's report
      as break-class findings for prompt follow-up. The allowlist exists so
      this guard can land now (and stop the bleeding on *new* sites) without
      being blocked on a separate, wider fix; it is intentionally an exact-set
      match (see ``test_no_pre_existing_violation_list_has_gone_stale``) so a
      future fix silently going unrecorded here is itself caught.

Spec backlink: ``cross-repo/inbox/2026-07-20-claude-central-em-shebang-launch-bug-class-three-sites.md``;
``coordinator_core.launchable`` (the shared resolution seam); the guard this
generalizes, ``coordinator_core/plugin_health/tests/test_sentinel.py::test_every_bash_sibling_probe_routes_through_sh_argv``.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_ROOT = Path(__file__).resolve().parent

_SUBPROCESS_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}
_SCRIPT_EXTS = (".js", ".cjs", ".mjs", ".sh", ".bash")

# (relative-path-from-coordinator_core, enclosing-function-name,
#  callee ("subprocess.<method>"), call_signature, ordinal) -> reason.
#
# Keyed on the enclosing function rather than a line number -- see
# `_find_enclosing_function_name` below for the resolution rule, including
# the `_MODULE_LEVEL` sentinel. Identity is the call's own text
# (`call_signature`, an `ast.unparse` of the call node), NOT a
# position-derived ordinal -- this guard's sibling,
# ``session/tests/test_no_untracked_relocation.py``, carries the full
# rationale for why a positional ordinal silently re-maps an approved entry
# onto an unrelated call site when a same-callee violation is inserted
# between two existing ones; both guards share this convention on purpose
# and were fixed together. `ordinal` is now a narrow residual that only
# disambiguates two-or-more BYTE-IDENTICAL calls (same `call_signature`)
# inside one function -- genuinely interchangeable by construction, so
# sharing one entry across them is acceptable. A pure line shift above a
# guarded call site does not change this key; a function rename, or a change
# to the call's own arguments, does, on purpose.
#
# Each entry is a REAL, currently-unfixed instance of the exact bug class this
# test guards against (bare-launched .sh/.js script, no resolve_launchable),
# discovered by this test's own scan while it was being authored. They are
# NOT legitimate design exceptions -- they are break-class defects out of
# scope for the test-authorship dispatch that added this guard. Each should
# be fixed the same way the three original sites were (wrap in
# ``resolve_launchable``) and its entry removed here as part of that fix.
_KNOWN_PRE_EXISTING_VIOLATIONS: Dict[Tuple[str, str, str, str, int], str] = {}


def _is_excluded(path: Path) -> bool:
    base = path.name
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    if "tests" in path.relative_to(_SCAN_ROOT).parts[:-1]:
        return True
    return False


def _iter_py_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def _is_subprocess_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return isinstance(f, ast.Attribute) and f.attr in _SUBPROCESS_ATTRS


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        # Best-effort human-readable text for a reason/diagnostic string only
        # -- never affects the safe/violation/unknown verdict itself, so a
        # placeholder is sufficient and a per-call warning would just be
        # test-output noise.
        return "<unparseable>"


_MODULE_LEVEL = "<module>"
"""Sentinel enclosing-function name for a call that sits outside any
`def`/`async def` -- a module-level allow-list key never crashes on
``None``/empty-string, and stays visually distinct from any real function
name. Shared convention with the sibling guard,
``session/tests/test_no_untracked_relocation.py``."""


def _find_enclosing_scope(tree: ast.AST, lineno: int) -> List[ast.stmt]:
    """Statement list of the innermost function containing `lineno`, else module body."""
    best = None

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            nonlocal best
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                best = node.body
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    _Visitor().visit(tree)
    return best if best is not None else list(getattr(tree, "body", []))


def _find_enclosing_function_name(tree: ast.AST, lineno: int) -> str:
    """Name of the innermost function containing `lineno`, or `_MODULE_LEVEL`
    if `lineno` sits outside every `def`/`async def`.

    This is the allow-list key's function component -- a line shift
    elsewhere in the file leaves it untouched; a function *rename* changes
    it, which is meant to fail `test_no_pre_existing_violation_list_has_gone_stale`
    (a rename is a real semantic change worth re-ratifying).
    """
    best = None

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            nonlocal best
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                best = node.name
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    _Visitor().visit(tree)
    return best if best is not None else _MODULE_LEVEL


def _find_assignment(scope_stmts: List[ast.stmt], name: str, before_lineno: int) -> Optional[ast.stmt]:
    """Nearest preceding simple `name = ...` / `name: T = ...` in `scope_stmts`."""
    candidates = []
    wrapper = ast.Module(body=scope_stmts, type_ignores=[])
    for stmt in ast.walk(wrapper):
        if isinstance(stmt, ast.Assign) and stmt.lineno < before_lineno:
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    candidates.append(stmt)
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == name
            and stmt.lineno < before_lineno
        ):
            candidates.append(stmt)
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.lineno)
    return candidates[-1]


def _find_top_level_func(tree: ast.AST, name: str) -> Optional[ast.FunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _last_literal_segment(node: ast.AST) -> Optional[str]:
    """Final literal path-segment for `os.path.join(...)` calls and pathlib `/` chains."""
    if isinstance(node, ast.Call):
        f = node.func
        fname = f.attr if isinstance(f, ast.Attribute) else None
        if fname == "join" and node.args:
            last = node.args[-1]
            if isinstance(last, ast.Constant) and isinstance(last.value, str):
                return last.value
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            return right.value
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ends_in_script_ext(segment: str) -> bool:
    low = segment.lower()
    return any(low.endswith(ext) for ext in _SCRIPT_EXTS)


def _classify(node: ast.AST, tree: ast.AST, call_lineno: int, depth: int = 0) -> Tuple[str, str]:
    """Return ("safe" | "violation" | "unknown", reason) for the argv[0] expression."""
    if depth > 5:
        return "unknown", "recursion-depth-exceeded"

    if isinstance(node, ast.Starred):
        if "resolve_launchable(" in _unparse(node.value):
            return "safe", "starred resolve_launchable() unpack"
        return _classify(node.value, tree, call_lineno, depth)

    if isinstance(node, ast.IfExp):
        return _classify(node.body, tree, call_lineno, depth + 1)

    if isinstance(node, ast.Call):
        f = node.func
        fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if fname == "resolve_launchable":
            return "safe", "resolve_launchable() call"
        if fname == "which":
            return "safe", "shutil.which() call"
        if fname in ("str", "fspath") and node.args:
            return _classify(node.args[0], tree, call_lineno, depth + 1)

        seg = _last_literal_segment(node)
        if seg is not None:
            if _ends_in_script_ext(seg):
                return "violation", f"bare literal '{seg}' via os.path.join(...)"
            return "safe", f"extensionless join tail '{seg}' (out of scope, see module docstring)"

        if fname:
            fn_def = _find_top_level_func(tree, fname)
            if fn_def is not None:
                for stmt in ast.walk(fn_def):
                    if isinstance(stmt, ast.Return) and stmt.value is not None:
                        cls, reason = _classify(stmt.value, tree, stmt.lineno, depth + 1)
                        if cls == "violation":
                            return cls, f"via {fname}(): {reason}"
        return "unknown", f"opaque call: {_unparse(node)}"

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        seg = _last_literal_segment(node)
        if seg is not None:
            if _ends_in_script_ext(seg):
                return "violation", f"bare literal '{seg}' via pathlib '/' join"
            return "safe", f"extensionless '/' join tail '{seg}' (out of scope, see module docstring)"
        return "unknown", f"opaque binop: {_unparse(node)}"

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if _ends_in_script_ext(node.value):
            return "violation", f"bare literal '{node.value}'"
        return "safe", f"extensionless/non-script literal '{node.value}' (out of scope, see module docstring)"

    if isinstance(node, ast.Attribute):
        txt = _unparse(node)
        if txt == "sys.executable":
            return "safe", "sys.executable"
        return "unknown", f"opaque attribute: {txt}"

    if isinstance(node, ast.Name):
        scope = _find_enclosing_scope(tree, call_lineno)
        assign = _find_assignment(scope, node.id, call_lineno)
        if assign is None or assign.value is None:
            return "unknown", f"untraceable name '{node.id}' (no local assignment found)"
        return _classify(assign.value, tree, assign.lineno, depth + 1)

    return "unknown", f"unhandled node shape: {_unparse(node)}"


def _scan_violations_raw() -> List[Tuple[str, str, str, int, int, str, str]]:
    """Return `(relpath, enclosing_function, callee, lineno, col_offset,
    reason, call_signature)` for every argv[0] that traces to a bare
    script-extension literal, un-ordinalled -- `_scan_violations` groups
    these by `(relpath, enclosing_function, callee, call_signature)` and
    assigns the residual ordinal. `call_signature` is `ast.unparse` of the
    whole `subprocess.<method>(...)` call node -- the call's own text, not
    its position, is the identity a same-callee insertion must not disturb."""
    findings: List[Tuple[str, str, str, int, int, str, str]] = []
    for path in _iter_py_files(_SCAN_ROOT):
        if _is_excluded(path):
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as exc:
            # A production module that fails to parse can't be scanned for
            # this guard's violation class -- surface it (stderr, not a test
            # failure) so an unparseable-and-therefore-unguarded file is
            # discoverable rather than silently dropped from the scan.
            print(f"test_no_bare_argv0_script_launch: skipping unparseable {path}: {exc}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not _is_subprocess_call(node):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
                continue
            elt0 = first.elts[0]
            cls, reason = _classify(elt0, tree, node.lineno)
            if cls == "violation":
                rel = str(path.relative_to(_SCAN_ROOT))
                func_name = _find_enclosing_function_name(tree, node.lineno)
                callee = f"subprocess.{node.func.attr}"
                sig = _unparse(node)
                findings.append((rel, func_name, callee, node.lineno, node.col_offset, reason, sig))
    return findings


def _scan_violations() -> Dict[Tuple[str, str, str, str, int], str]:
    """Return `{(relpath, enclosing_function, callee, call_signature,
    ordinal): reason}` for every argv[0] that traces to a bare
    script-extension literal.

    Identity is the call's own text (`call_signature`), NOT a
    position-derived ordinal -- same convention as the sibling guard,
    ``session/tests/test_no_untracked_relocation.py``, which carries the
    full rationale for why a positional-only ordinal silently re-maps an
    approved entry when a same-callee violation is inserted between two
    existing ones. `ordinal` is a narrow residual disambiguating two-or-more
    BYTE-IDENTICAL violating calls (same `call_signature`) in one
    `(relpath, enclosing_function, callee)` group.
    """
    raw = _scan_violations_raw()
    per_group: Dict[Tuple[str, str, str], List[Tuple[int, int, str, str]]] = {}
    for rel, func_name, callee, lineno, col, reason, sig in raw:
        per_group.setdefault((rel, func_name, callee), []).append((lineno, col, reason, sig))

    found: Dict[Tuple[str, str, str, str, int], str] = {}
    for (rel, func_name, callee), entries in per_group.items():
        sig_rank: Dict[str, int] = {}
        for _lineno, _col, reason, sig in sorted(entries, key=lambda e: (e[0], e[1])):
            sig_rank[sig] = sig_rank.get(sig, 0) + 1
            found[(rel, func_name, callee, sig, sig_rank[sig])] = reason
    return found


def _group_key(entry_key: Tuple[str, str, str, str, int]) -> Tuple[str, str, str]:
    """The `(relpath, function, callee)` group a full 5-tuple key belongs
    to -- drops `call_signature`/`ordinal`, the two components that vary
    within one group. Used by the group-membership guard below."""
    rel, func, callee, _sig, _ordinal = entry_key
    return (rel, func, callee)


def test_no_new_bare_script_argv0_beyond_known_pre_existing_violations():
    """The class-level guard: every bare-script-extension argv[0] found by the
    scan must already be named in `_KNOWN_PRE_EXISTING_VIOLATIONS` (a tracked,
    reported gap) -- any OTHER hit is a new (or reintroduced) instance of the
    exact bug class the 2026-07-20 fix addressed, and fails the build.
    """
    found = _scan_violations()
    found_keys = set(found.keys())
    known_keys = set(_KNOWN_PRE_EXISTING_VIOLATIONS.keys())

    unexpected = found_keys - known_keys
    assert not unexpected, (
        "New bare-script-argv[0] launch(es) detected -- route through "
        "coordinator_core.launchable.resolve_launchable() instead:\n"
        + "\n".join(
            f"  {rel} in {func}() [{callee}, {sig!r}#{ordinal}] -- {found[(rel, func, callee, sig, ordinal)]}"
            for rel, func, callee, sig, ordinal in sorted(unexpected)
        )
    )


def test_group_membership_change_forces_full_group_re_review():
    """Belt-and-braces on top of the call-signature key: if the SET of
    violating calls in any `(relpath, function, callee)` group differs at
    all from what `_KNOWN_PRE_EXISTING_VIOLATIONS` records for that group,
    every entry in that group must fail -- not just the ones whose own key
    changed. See the sibling guard's identically-named test
    (``session/tests/test_no_untracked_relocation.py``) for the full
    rationale: a false re-ratification costs one reading, a silent exemption
    is permanent and invisible. Fail loud toward re-ratification, always."""
    found = _scan_violations()
    found_groups: Dict[Tuple[str, str, str], set] = {}
    for key in found:
        found_groups.setdefault(_group_key(key), set()).add(key)
    known_groups: Dict[Tuple[str, str, str], set] = {}
    for key in _KNOWN_PRE_EXISTING_VIOLATIONS:
        known_groups.setdefault(_group_key(key), set()).add(key)

    mismatched_groups = {
        group
        for group in set(found_groups) | set(known_groups)
        if found_groups.get(group, set()) != known_groups.get(group, set())
    }
    affected_entries = {
        key for key in _KNOWN_PRE_EXISTING_VIOLATIONS if _group_key(key) in mismatched_groups
    }
    assert not affected_entries, (
        "The set of violating call sites in these (relpath, function, "
        "callee) groups no longer matches _KNOWN_PRE_EXISTING_VIOLATIONS "
        "exactly -- every entry in an affected group must be re-reviewed and "
        "re-keyed:\n"
        + "\n".join(f"  group {group}" for group in sorted(mismatched_groups))
    )


def test_no_pre_existing_violation_list_has_gone_stale():
    """If a known pre-existing violation gets fixed, its allowlist entry must
    be removed in the same change -- otherwise the allowlist silently drifts
    from what's actually still broken, and the next real regression at that
    same line would be masked by a stale entry."""
    found_keys = set(_scan_violations().keys())
    known_keys = set(_KNOWN_PRE_EXISTING_VIOLATIONS.keys())

    stale = known_keys - found_keys
    assert not stale, (
        "Allowlist entries no longer match a real violation -- remove them "
        "from _KNOWN_PRE_EXISTING_VIOLATIONS (the underlying bug appears fixed, "
        "its enclosing function renamed, or its own arguments changed):\n"
        + "\n".join(
            f"  {rel} in {func}() [{callee}, {sig!r}#{ordinal}]"
            for rel, func, callee, sig, ordinal in sorted(stale)
        )
    )


def test_ordinal_insertion_does_not_silently_reassign_an_approved_entry():
    """Regression test mirroring the sibling guard's reproduction of the
    reviewer-confirmed defect: two violating calls in one function, a third
    same-callee violation inserted between them, and assert the guard does
    NOT silently accept the new call under either original entry's key.

    Keying on the call's own `ast.unparse` text instead of position means
    the inserted call changes nothing about the two pre-existing calls' keys
    -- this test proves that directly against this module's own scan
    helpers, without touching `_KNOWN_PRE_EXISTING_VIOLATIONS` or the real
    scan root.
    """
    src_before = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['first.sh', '--a'])\n"
        "    subprocess.run(['second.sh', '--b'])\n"
    )
    src_after_insertion = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['first.sh', '--a'])\n"
        "    subprocess.run(['inserted.sh', '--c'])\n"
        "    subprocess.run(['second.sh', '--b'])\n"
    )

    def _scan_signatures(src: str) -> Dict[Tuple[str, int], str]:
        tree = ast.parse(src)
        out: Dict[Tuple[str, int], str] = {}
        rank: Dict[str, int] = {}
        for node in ast.walk(tree):
            if not _is_subprocess_call(node) or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
                continue
            cls, reason = _classify(first.elts[0], tree, node.lineno)
            if cls == "violation":
                sig = _unparse(node)
                rank[sig] = rank.get(sig, 0) + 1
                out[(sig, rank[sig])] = reason
        return out

    before = _scan_signatures(src_before)
    after = _scan_signatures(src_after_insertion)

    for key in before:
        assert key in after, f"insertion silently removed/re-keyed {key}"
        assert after[key] == before[key]

    inserted_keys = set(after) - set(before)
    assert inserted_keys == {("subprocess.run(['inserted.sh', '--c'])", 1)}


# ---------------------------------------------------------------------------
# Self-tests for the scanner itself -- prove the detector actually detects.
# ---------------------------------------------------------------------------


def test_scanner_flags_a_bare_sh_launch_literal(tmp_path):
    src = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['sibling-guard.sh', '--check'])\n"
    )
    mod = tmp_path / "probe_mod.py"
    mod.write_text(src)
    tree = ast.parse(src, filename=str(mod))
    call = next(n for n in ast.walk(tree) if _is_subprocess_call(n))
    elt0 = call.args[0].elts[0]
    cls, _reason = _classify(elt0, tree, call.lineno)
    assert cls == "violation"


def test_scanner_flags_a_bare_js_launch_via_variable(tmp_path):
    src = (
        "import subprocess\n"
        "def f():\n"
        "    script = 'query-records.js'\n"
        "    subprocess.run([script, '--type', 'completion'])\n"
    )
    mod = tmp_path / "probe_mod2.py"
    mod.write_text(src)
    tree = ast.parse(src, filename=str(mod))
    call = next(n for n in ast.walk(tree) if _is_subprocess_call(n))
    elt0 = call.args[0].elts[0]
    cls, _reason = _classify(elt0, tree, call.lineno)
    assert cls == "violation"


def test_scanner_accepts_resolve_launchable_unpack(tmp_path):
    src = (
        "import subprocess\n"
        "from coordinator_core.launchable import resolve_launchable\n"
        "def f():\n"
        "    subprocess.run([*resolve_launchable('query-records.js'), '--type', 'completion'])\n"
    )
    mod = tmp_path / "probe_mod3.py"
    mod.write_text(src)
    tree = ast.parse(src, filename=str(mod))
    call = next(n for n in ast.walk(tree) if _is_subprocess_call(n))
    elt0 = call.args[0].elts[0]
    cls, _reason = _classify(elt0, tree, call.lineno)
    assert cls == "safe"


def test_scanner_accepts_explicit_interpreter_prefix(tmp_path):
    src = (
        "import subprocess\n"
        "def f():\n"
        "    subprocess.run(['bash', 'guard.sh', '--check'])\n"
    )
    mod = tmp_path / "probe_mod4.py"
    mod.write_text(src)
    tree = ast.parse(src, filename=str(mod))
    call = next(n for n in ast.walk(tree) if _is_subprocess_call(n))
    elt0 = call.args[0].elts[0]
    cls, _reason = _classify(elt0, tree, call.lineno)
    assert cls == "safe"
