"""Wave-0 gate (C0a plan's chunk C0c) for PM-binding constraint (2): no
bash / POSIX-coreutils / `jq` / `comm` / process-substitution dependency in
any op this plan builds. Direct sibling of
``coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py`` (C8)
per DEC-4 — same AST-walk machinery, same by-(file, call)-tuple exemption
granularity, same plant-a-violation self-test discipline, copied rather than
reinvented.

Spec backlink: `pln-coordinator-ops-buildout-from--903224`
§ PM-binding constraints (2), § DEC-4, chunk C0c, AC3.

Constraint (2), restated: every op this plan builds must be first-class on
macOS AND Windows. A literal spawn of `bash`, `sh`, `jq`, `comm`, `awk`,
`sed`, `grep`, `find`, `ls`, `stat`, `date`, `xargs`, `cut`, `sort`, `uniq`,
`tr`, `head`, or `tail` as the invoked program, a `shell=True` kwarg, or
`<(...)` process-substitution text inside any command string, all encode a
POSIX-shell assumption that silently breaks on a bash-less Windows machine.

Detection is AST-based (`ast`, not regex) over literal string constants
reachable from a `subprocess.run`/`Popen`/`check_output`/`call`/`check_call`
call's arguments — same scope and same accepted tradeoff as the C8 sibling:
a dynamically-built argv (`subprocess.run(cmd)` where `cmd` was assembled
across earlier statements, or `subprocess.run([tool_path, ...])` where
`tool_path` came from `shutil.which(...)`) is a literal string gate's
structural blind spot, not a bug in this gate — false negatives on dynamic
construction are the documented, accepted cost of zero false positives on
the doc/comment noise a substring scan would otherwise flag. `<CLAUDE.md
carve-out (a)/(c)/(e)>`'s own sanctioned sites resolve their bash/sh path
through `shutil.which` in two of five cases specifically because that
dynamic lookup makes them structurally invisible to a literal scan in the
first place (see `_SANCTIONED_BASH_CARVEOUT_SITES` below for which of the
five carve-out sites this gate can even see).

Exemption sets — three, kept structurally and semantically separate so a
reader (and a future executor) can never mistake one kind for another:

1. ``_SANCTIONED_BASH_CARVEOUT_SITES`` — the CLOSED, PM-ratified carve-out
   `CLAUDE.md` § Runtime conventions enumerates by named site (classes
   a-e). Of the five named sites, only two are literal-constant spawns this
   gate can see at all (the other two resolve their executable via
   `shutil.which(...)` into a variable and are invisible by construction,
   same as the C8 sibling's node install-probe): `_install_homebrew`
   (class a), `_fnm_step` (class a), and `_tier1b_mirror_and_cold_tier`
   (class e). Class (c)'s only named site, `verify_registry_consistency.py`
   `_bash_n`, was retired 2026-07-22 with the four-script leg it
   parse-checked (see `CLAUDE.md` § Runtime conventions carve-out (c) —
   the class's site list resolved to zero); it is no longer in this set.
   Extending this set requires a new PM ruling per `CLAUDE.md` — never a
   local judgment call, never inferred from a class's rationale.

2. ``_KNOWN_PREEXISTING_BASH_DEPENDENCY_DEBT`` — live, NON-carve-out
   violations discovered while building this gate (2026-07-22), predating
   this plan, in modules that are not among the plan's 64 in-scope ops.
   NOT a carve-out (no CLAUDE.md class covers them) and NOT this plan's
   modules (see AC3) — a third, honestly-separate bucket, structured
   exactly like the C8 sibling's ``_EXEMPT_NODE_ORACLE_CALLS``: dated,
   scoped to a specific (file, function, tool) triple, with a named fix
   path, never a growing blanket mute. Flagged to the EM for a follow-up
   fix chunk; out of this chunk's write scope (disjoint-write discipline
   for this wave) to fix in-place. Three entries; the first two are a shell-out-with-
   graceful-Python-fallback shape that predates the naked-Python mandate:
   ``ops/sync_plugin_wiki.py::_stat_mtime`` (shells to `stat` for a
   byte-identical mtime string; fix path: drop the `stat` shell-out
   entirely and always take the `os.stat().st_mtime` branch — the
   byte-parity-with-a-retired-bash-oracle rationale that motivated
   shelling out no longer has a live oracle to stay parity with) and
   ``ops/discover_working_repos.py::_sort_unique`` (shells to `sort -u`
   for locale-collated byte parity; fix path: same shape — the
   Python-`sorted()` fallback already exists in the same function, so
   the fix is deleting the shell-out branch, not writing a new one).
   The third entry is a DIFFERENT shape, dated 2026-07-24 (regression from
   safety-commit 9e721eeb, a concurrent session's in-flight reverse-drift
   work): ``ops/workweek_reverse_drift_gate.py::run_gate`` runs an arbitrary
   user-registered ``reverse_drift_cmd`` via ``bash -euo pipefail -c`` — no
   Python fallback exists, so the fix is a design change owned by the
   reverse-drift feature (register the command as an arg-list / windows-safe
   invocation contract and run it shell-free), not a shell-out deletion.
   See the inline comment on that frozenset entry below.

3. ``_PLAN_MODULE_BASH_EXEMPTIONS`` — AC3's exemption set "for this plan's
   modules." Empty by construction and asserted empty by
   ``test_plan_module_bash_exemptions_is_empty`` below, so a future Wave
   1-3 executor cannot quietly buy an exemption into existence instead of
   porting the op natively. A real addition here is a plan amendment, not
   an executor call — same discipline DEC-4 states for C0b.

Scope: production (non-test) code under `coordinator_core/`, matching the
C8 sibling's own scope exactly. Test files are out of this gate's scope for
the same reason the sibling states: this gate governs a runtime-dependency
prohibition on op *implementations*, not on how a test proves an op's
behavior.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

_SPAWN_CALL_NAMES = {"run", "Popen", "check_output", "call", "check_call"}

# The banned-executable set for PM-binding constraint (2). Curl is
# deliberately absent: it is the transport half of carve-out (a)'s
# sanctioned upstream-installer invocations and is not itself a
# bash/coreutils/jq/comm dependency.
_BANNED_TOOLS = frozenset(
    {
        "bash",
        "sh",
        "jq",
        "comm",
        "awk",
        "sed",
        "grep",
        "find",
        "ls",
        "stat",
        "date",
        "xargs",
        "cut",
        "sort",
        "uniq",
        "tr",
        "head",
        "tail",
    }
)

_PROCESS_SUBSTITUTION_MARK = "<("

# ---------------------------------------------------------------------------
# Exemption set 1 — the CLOSED CLAUDE.md carve-out (classes a/c/e; b/d are
# structurally invisible to this literal-only scan, see module docstring).
# Keyed (relpath, enclosing-function, matched-tool) per DEC-4's by-(file,
# call)-tuple granularity. Extend ONLY on a new PM ruling.
# ---------------------------------------------------------------------------
_SANCTIONED_BASH_CARVEOUT_SITES: frozenset[tuple[str, str, str]] = frozenset(
    {
        # carve-out (a) — 3rd-party upstream installer invoked as-published.
        ("coordinator_core/install/first_run.py", "_install_homebrew", "bash"),
        ("coordinator_core/install/substrate.py", "_fnm_step", "bash"),
        # carve-out (c) — bash-as-required-parser (`bash -n`) — RETIRED
        # 2026-07-22: its only named site, `verify_registry_consistency.py`
        # `_bash_n`, was deleted with the four-script leg it parse-checked.
        # No entry remains for this class.
        # carve-out (e) — live-shell-environment artifact under test (a
        # claude-klabauter-generated shell shim sourced solely to assert its runtime
        # effect on a live shell, inside install-verification harness code).
        (
            "coordinator_core/install/sandbox_check.py",
            "_tier1b_mirror_and_cold_tier",
            "bash",
        ),
    }
)

# ---------------------------------------------------------------------------
# Exemption set 2 — known, LIVE, pre-existing (non-carve-out) violations
# discovered while building this gate. See module docstring for the fix
# path each carries. Dated 2026-07-22; not this plan's modules; flagged to
# the EM, not fixed here (this chunk's write target is this file alone).
# ---------------------------------------------------------------------------
_KNOWN_PREEXISTING_BASH_DEPENDENCY_DEBT: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("coordinator_core/ops/sync_plugin_wiki.py", "_stat_mtime", "stat"),
        ("coordinator_core/ops/discover_working_repos.py", "_sort_unique", "sort"),
        # 2026-07-24 (introduced by safety-commit 9e721eeb, a concurrent
        # session's in-flight reverse-drift work — NOT a 2026-07-22 discovery
        # and NOT the shell-out-with-Python-fallback shape the other two are).
        # `run_gate` executes an arbitrary user-registered `reverse_drift_cmd`
        # via `bash -euo pipefail -c` for fail-fast pipeline semantics, so
        # there is no Python fallback branch to fall back to — the fix is a
        # DESIGN change owned by the reverse-drift feature: register
        # `reverse_drift_cmd` as an arg-list (or a windows-safe invocation
        # contract) so it runs via `subprocess.run(arglist, cwd=...)` with no
        # shell, cross-platform. Tracked here (not blanket-muted) pending that
        # owner's port; windows-hostile until then.
        ("coordinator_core/ops/workweek_reverse_drift_gate.py", "run_gate", "bash"),
    }
)

# ---------------------------------------------------------------------------
# Exemption set 3 — AC3's "for this plan's modules" exemption set. Must
# stay empty (test_plan_module_bash_exemptions_is_empty asserts this). A
# real entry here is a plan amendment, never an executor's local call.
# ---------------------------------------------------------------------------
_PLAN_MODULE_BASH_EXEMPTIONS: set[tuple[str, str, str]] = set()

_MODULE_SCOPE = "<module>"


def _relpath(path: Path) -> str:
    return path.resolve().relative_to(_REPO_ROOT).as_posix()


def _is_excluded_source_path(path: Path) -> bool:
    """Test files are out of scope: this gate governs op *implementations*,
    not how a test exercises or documents an op's behavior."""
    if path.name.startswith("test_"):
        return True
    return "tests" in path.parts


def _spawn_call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _string_constants(node: ast.expr) -> list[str]:
    """Collect literal string constants reachable from a call argument.

    Handles both the list/tuple argv form (`["bash", "-c", ...]`) and the
    bare string form (`"bash -c ..."`, e.g. a shell=True command line).
    Anything else (a Name, a Starred variable, an f-string) is not a
    literal and is skipped -- false negatives on dynamic construction are
    an accepted tradeoff, same as the C8 sibling gate.
    """
    strings: list[str] = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                strings.append(elt.value)
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        strings.append(node.value)
    return strings


def _call_argument_strings(call: ast.Call) -> list[str]:
    strings: list[str] = []
    for arg in call.args:
        strings.extend(_string_constants(arg))
    for kw in call.keywords:
        if kw.arg == "args" and kw.value is not None:
            strings.extend(_string_constants(kw.value))
    return strings


def _basename(token: str) -> str:
    """Last path component of `token`, splitting on both `/` and `\\` so a
    literal like `/bin/bash` or (hypothetically) `C:\\Windows\\bash` still
    resolves to the bare executable name `bash` for banned-tool matching."""
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _string_tokens(s: str) -> list[str]:
    """`s` itself, plus each of its whitespace-split words -- covers both
    an already-split argv element (`"bash"`) and a bare multi-word command
    line (`"bash -c 'cmd1 | jq .x'"`, the shell=True embedded-string
    shape)."""
    return [s, *s.split()]


def _matched_banned_tools(strings: list[str]) -> set[str]:
    found: set[str] = set()
    for s in strings:
        for candidate in _string_tokens(s):
            base = _basename(candidate)
            if base in _BANNED_TOOLS:
                found.add(base)
    return found


def _has_process_substitution(strings: list[str]) -> bool:
    return any(_PROCESS_SUBSTITUTION_MARK in s for s in strings)


def _has_shell_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


class BashDependencyVisitor(ast.NodeVisitor):
    """Collects (lineno, argument-strings, enclosing-function, reasons) for
    every bash/coreutils/jq/comm/shell=True/process-substitution subprocess
    spawn found in one parsed module. `reasons` is the set of matched
    banned-tool tokens, plus "shell=True" and/or "process-substitution"
    when those shapes are present -- a single call can carry more than one
    reason at once.
    """

    def __init__(self) -> None:
        self.violations: list[tuple[int, list[str], str, set[str]]] = []
        self._function_stack: list[str] = []

    def _current_scope(self) -> str:
        return self._function_stack[-1] if self._function_stack else _MODULE_SCOPE

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_stack.append(node.name)
        self.generic_visit(node)
        self._function_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _spawn_call_name(node.func)
        if name in _SPAWN_CALL_NAMES:
            strings = _call_argument_strings(node)
            reasons: set[str] = set(_matched_banned_tools(strings))
            if _has_process_substitution(strings):
                reasons.add("process-substitution")
            if _has_shell_true(node):
                reasons.add("shell=True")
            if reasons:
                self.violations.append((node.lineno, strings, self._current_scope(), reasons))
        self.generic_visit(node)


def find_bash_dependency_violations(
    root: Path,
) -> list[tuple[str, int, str, list[str], set[str]]]:
    """Walk `root` for .py files (excluding tests/ and test_*.py) and return
    every (relpath, lineno, enclosing-scope, argv-strings, matched-reasons)
    tuple that is a literal bash/coreutils/jq/comm/shell=True/process-
    substitution subprocess spawn, minus whatever's covered by the three
    exemption sets above.

    Used both against the real coordinator_core/ tree (the standing gate)
    and against an isolated tmp_path fixture (this gate's own self-test,
    proving it actually detects rather than merely passing by absence).
    """
    violations: list[tuple[str, int, str, list[str], set[str]]] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded_source_path(path):
            continue
        try:
            relpath = path.resolve().relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            # root is outside the repo (a tmp_path fixture) -- use a
            # root-relative path, which will never match a real exemption.
            relpath = path.resolve().relative_to(root.resolve()).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        visitor = BashDependencyVisitor()
        visitor.visit(tree)
        for lineno, strings, scope, reasons in visitor.violations:
            unexempted = {
                reason
                for reason in reasons
                if (relpath, scope, reason) not in _SANCTIONED_BASH_CARVEOUT_SITES
                and (relpath, scope, reason) not in _KNOWN_PREEXISTING_BASH_DEPENDENCY_DEBT
                and (relpath, scope, reason) not in _PLAN_MODULE_BASH_EXEMPTIONS
            }
            if unexempted:
                violations.append((relpath, lineno, scope, strings, unexempted))
    return violations


def test_no_bash_dependency_in_production_code():
    """Standing gate: coordinator_core/ non-test code must contain zero
    literal bash/coreutils/jq/comm/shell=True/process-substitution
    dependencies, except the three named, dated exemption sets above.
    Must land green against the current tree -- and does, because both
    real pre-existing violations found while building this gate are
    tracked in _KNOWN_PREEXISTING_BASH_DEPENDENCY_DEBT with a fix path,
    not silently ignored."""
    violations = find_bash_dependency_violations(_SCAN_ROOT)
    assert violations == [], (
        "Found bash/coreutils/jq/comm/shell=True/process-substitution "
        "dependency in coordinator_core/ production code (should be "
        "in-process only, or explicitly exempted): "
        f"{violations}"
    )


def test_gate_detects_planted_bash_jq_coreutils_and_shell_true_violations(tmp_path):
    """Proves the gate has teeth for all four of PM-binding constraint (2)'s
    named shapes at once -- a matcher that catches only the first (e.g. only
    literal `bash`) is the hollow-gate failure mode DEC-4 names."""
    fixture = tmp_path / "fixture_reintroduced_bash_dependency.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def run_bash():\n"
        "    return subprocess.run(['bash', '-c', 'echo hi'], capture_output=True)\n"
        "\n"
        "def run_jq(record_path):\n"
        "    return subprocess.run(['jq', '.field', record_path], capture_output=True)\n"
        "\n"
        "def run_coreutils(lines):\n"
        "    return subprocess.run(['sort', '-u'], input=lines, capture_output=True, text=True)\n"
        "\n"
        "def run_shell_true(cmd):\n"
        "    return subprocess.run(cmd, shell=True, capture_output=True)\n",
        encoding="utf-8",
    )

    violations = find_bash_dependency_violations(tmp_path)

    by_scope = {scope: reasons for _, _, scope, _, reasons in violations}
    assert len(violations) == 4, violations
    assert by_scope["run_bash"] == {"bash"}
    assert by_scope["run_jq"] == {"jq"}
    assert by_scope["run_coreutils"] == {"sort"}
    assert by_scope["run_shell_true"] == {"shell=True"}


def test_gate_detects_a_planted_process_substitution(tmp_path):
    """Isolated proof that `<(...)` process-substitution text is flagged on
    its own, independent of any banned-tool token match (the executable
    here, `customdiff`, is not itself a banned tool)."""
    fixture = tmp_path / "fixture_process_substitution.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def diff_two_sorted_files(a, b):\n"
        "    return subprocess.run(\n"
        "        ['customdiff', '<(sort a.txt)', '<(sort b.txt)'],\n"
        "        capture_output=True,\n"
        "    )\n",
        encoding="utf-8",
    )

    violations = find_bash_dependency_violations(tmp_path)

    assert len(violations) == 1
    relpath, lineno, scope, strings, reasons = violations[0]
    assert relpath.endswith("fixture_process_substitution.py")
    assert lineno == 4
    assert scope == "diff_two_sorted_files"
    assert reasons == {"process-substitution"}


def test_gate_ignores_benign_and_dynamically_constructed_calls(tmp_path):
    """Negative control: the exact shape of noise/tradeoff this gate must
    NOT flag -- a benign non-banned-tool spawn, a comment/docstring
    mentioning banned-tool names, a `shutil.which` presence probe (not a
    spawn at all), and a dynamically-built argv (the accepted
    false-negative tradeoff documented in the module docstring)."""
    fixture = tmp_path / "fixture_benign.py"
    fixture.write_text(
        '"""Mirrors the retired bash script'"'"'s `grep`/`sort` pipeline in prose only."""\n'
        "import shutil\n"
        "import subprocess\n"
        "\n"
        "def run_git_status():\n"
        "    return subprocess.run(['git', 'status'], capture_output=True)\n"
        "\n"
        "def probe_bash_present():\n"
        "    return shutil.which('bash')\n"
        "\n"
        "def run_dynamic_argv():\n"
        "    cmd = ['bash', '-c']\n"
        "    cmd.append('echo hi')\n"
        "    return subprocess.run(cmd, capture_output=True)\n"
        "\n"
        "def run_dynamic_executable():\n"
        "    bash_path = shutil.which('bash')\n"
        "    return subprocess.run([bash_path, '--version'], capture_output=True)\n",
        encoding="utf-8",
    )

    violations = find_bash_dependency_violations(tmp_path)

    assert violations == []


def test_plan_module_bash_exemptions_is_empty():
    """AC3: 'carve-out exemption set for this plan's modules is empty.' A
    future Wave 1-3 executor cannot quietly grow this set to buy a bash
    dependency into an op instead of porting it -- a real addition is a
    plan amendment, never a local call, same as DEC-4 mandates for C0b."""
    assert _PLAN_MODULE_BASH_EXEMPTIONS == set()


def test_sanctioned_carveout_set_matches_claude_md_enumeration():
    """Pins the carve-out allowlist to exactly the CLAUDE.md-named sites
    this literal-only scan can actually see (classes a/e -- class c's only
    site retired 2026-07-22) -- proves the set hasn't silently grown beyond
    what's ratified, and documents that it hasn't silently shrunk either."""
    assert _SANCTIONED_BASH_CARVEOUT_SITES == {
        ("coordinator_core/install/first_run.py", "_install_homebrew", "bash"),
        ("coordinator_core/install/substrate.py", "_fnm_step", "bash"),
        (
            "coordinator_core/install/sandbox_check.py",
            "_tier1b_mirror_and_cold_tier",
            "bash",
        ),
    }


def test_known_preexisting_debt_disjoint_from_sanctioned_carveout_and_plan_exemptions():
    """The three exemption sets must never overlap -- a site belongs to
    exactly one bucket (CLOSED CLAUDE.md carve-out, known pre-existing
    non-carve-out debt, or this-plan's-modules), never two, or the
    boundary DEC-4 draws between them has blurred."""
    assert not (_KNOWN_PREEXISTING_BASH_DEPENDENCY_DEBT & _SANCTIONED_BASH_CARVEOUT_SITES)
    assert not (_KNOWN_PREEXISTING_BASH_DEPENDENCY_DEBT & _PLAN_MODULE_BASH_EXEMPTIONS)
    assert not (_SANCTIONED_BASH_CARVEOUT_SITES & _PLAN_MODULE_BASH_EXEMPTIONS)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
