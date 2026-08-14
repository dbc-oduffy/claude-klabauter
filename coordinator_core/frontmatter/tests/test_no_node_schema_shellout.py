"""Standing tripwire (C8, absorbs C6 verification; widened 2026-07-22 to cover
all DoE JS oracles; CLOSED to zero known violations 2026-07-22 when
verify_schema_registry_sync.py's `_type_recognised` was ported off its
query-records.js node spawn — see that module's docstring): no DoE-JS-oracle
Node shell-out survives in coordinator_core/ PRODUCTION (non-test) code.

Context: the DR-210 native-tooling-ownership strangler is porting the DoE JS
oracles (DoE-claude coordinator/bin/**/*.js) to Python one at a time. Each
port retires a `subprocess.run(["node", "<oracle>.js", ...])` style shell-out
in favor of an in-process call. This test is the regression gate that keeps a
future edit from silently reintroducing a node shell-out to ANY of those
oracles -- not just the schema pair the gate originally covered.

DoE JS oracle inventory this gate matches (verified against
DoE-claude/coordinator/bin on 2026-07-22):
  - still alive: schema.js, schema-cli.js, query-records.js,
    walk-handoff-dag.js, emit-artifact-shape-contract.js
  - already deleted (a reintroduced call to one of these is worse than a call
    to a live script -- the target doesn't even exist anymore): verify-coverage.js,
    handoff-transition.js, memo-transition.js, stamp-shipped-in.js

Naive detection is useless here: schema_validate.py alone has 72 occurrences
of the substring "schema.js" in doc/comment backlinks to the JS oracle
("Port of schema.js:...", "Mirrors schema.js ..."), and coordinator_core has
many legitimate, unrelated uses of the word "node" (a `node` install
prerequisite probe, a `.js` -> `node` launchable-extension map, a
graph-node `kind="node"` token in the roadmap-DAG emitter, and multiple
parity-test oracles that deliberately still spawn the DoE JS implementation
as ground truth). So the assertion here is narrowly scoped:

    No literal subprocess spawn call (subprocess.run/Popen/check_output/
    call/check_call -- attribute or `from subprocess import ...` form) in a
    NON-TEST coordinator_core/ file passes an argument list/string that
    references BOTH a `node` invocation AND a DoE-JS-oracle script target
    (see _DOE_JS_ORACLE_SUBSTRINGS below).

Implemented via `ast` (not regex) specifically so comments and docstrings
are structurally excluded rather than pattern-excluded -- ast never
materializes a `#` comment as a node, and a bare docstring/string-literal
statement is not a Call argument, so neither can trip the detector no
matter what text they contain.

The one known, live violation this gate used to carry as an explicit, dated
exemption (verify_schema_registry_sync.py's `_type_recognised` spawning
`node query-records.js --type ...`) was ported to an in-process registry
derivation on 2026-07-22 (see `coordinator_core.frontmatter.schema_validate.
build_type_to_glob`) -- _EXEMPT_NODE_ORACLE_CALLS is now empty. The general
gate stays in force with no carve-outs; a future exemption would be added
here the same way (explicit, dated, self-invalidating), not by silently
widening the matcher's blind spot.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

_SPAWN_CALL_NAMES = {"run", "Popen", "check_output", "call", "check_call"}

# Substrings that mean "this argument is invoking the node binary".
_NODE_TOKENS = {"node"}

# Substrings that mean "this argument targets a DoE JS oracle script".
# Covers both still-alive oracles (a live dependency this gate must keep off
# the production hot path) and already-deleted ones (a reintroduced call to
# a deleted script is an even worse regression than one to a live script,
# since the target doesn't even exist). See module docstring for provenance.
_DOE_JS_ORACLE_SUBSTRINGS = (
    # still alive (DoE-claude/coordinator/bin, verified 2026-07-22)
    "schema-cli.js",
    "schema.js",
    "query-records.js",
    "walk-handoff-dag.js",
    "emit-artifact-shape-contract.js",
    # already deleted -- a call to these targets nothing
    "verify-coverage.js",
    "handoff-transition.js",
    "memo-transition.js",
    "stamp-shipped-in.js",
    # ported 2026-07-22 (de-node query/read-layer cutover) -- each had its
    # last production spawn replaced by an in-process call; a reintroduced
    # spawn would resurrect a dependency DoE is clear to delete
    "sentinel-blocks-cli.js",
    "render-handoff-tracker.js",
    "exec-bit.test.js",
)

# Explicit, by-path exemptions for legitimate node uses this gate must not
# flag even if some future edit made them structurally resemble a
# node+oracle-script spawn. Each entry names the file, the reason it's safe,
# and why it can never actually collide with the node+oracle pair the
# scanner looks for. Extending this set requires a comment of the same shape.
_ALLOWLISTED_RELPATHS = {
    # `_run(["node", "--version"])` -- an install-prerequisite probe (is
    # Node.js on PATH at all?). No oracle-script argument is ever present,
    # so it can never satisfy the node+oracle pair this gate flags;
    # listed explicitly anyway per C8 spec so the exemption is auditable.
    "coordinator_core/install/prereq_probe.py",
    # `shutil.which("node")` (env probe) and `_brew_install("node", "node")`
    # (Homebrew package name, not an argv entry to a node invocation) --
    # neither is a subprocess spawn call with an oracle-script argument.
    "coordinator_core/install/first_run.py",
}

# Known, LIVE, outstanding node+oracle violation(s), keyed on the specific
# (file, oracle-script) pair -- NOT a blanket file-level mute. Any OTHER
# node shell-out in the same file still fails the gate; only calls matching
# both the path AND the script substring named here are excused.
#
# Empty by construction (2026-07-22): the one prior entry
# (verify_schema_registry_sync.py's `_type_recognised` node query-records.js
# spawn) was retired the same day by porting the registry-recognition check
# to an in-process derivation (schema_validate.build_type_to_glob). A future
# exemption would be added here the same way this one was: dated, scoped to
# a specific (file, script) pair, with a named fix path -- never a growing
# blanket mute.
_EXEMPT_NODE_ORACLE_CALLS: set[tuple[str, str]] = set()


def _relpath(path: Path) -> str:
    return path.resolve().relative_to(_REPO_ROOT).as_posix()


def _is_excluded_source_path(path: Path) -> bool:
    """Test files are out of scope: the standing parity-test oracles
    (test_queue_parity.py, test_schema_validate.py, test_dag_js_parity.py,
    test_parity_handoff_ops.py, ...) deliberately still spawn node as their
    ground-truth oracle per AC9, and this gate governs production code only.
    """
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

    Handles both the list/tuple argv form (`["node", "schema.js", ...]`) and
    the bare string form (`"node schema.js ..."`, e.g. a shell=True
    command line). Anything else (a Name, a comprehension, an f-string) is
    not a literal and is skipped -- this scanner only ever proves a literal
    node+oracle-script spawn, never a dynamically-built one; false negatives
    on dynamic construction are an accepted tradeoff for zero false
    positives on the doc/comment noise this gate must ignore.
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


def _references_node_invocation(strings: list[str]) -> bool:
    for s in strings:
        if s in _NODE_TOKENS:
            return True
        if any(tok in s.split() for tok in _NODE_TOKENS):
            return True
    return False


def _referenced_oracle_scripts(strings: list[str]) -> set[str]:
    """Return the subset of _DOE_JS_ORACLE_SUBSTRINGS matched by strings."""
    found: set[str] = set()
    for s in strings:
        for sub in _DOE_JS_ORACLE_SUBSTRINGS:
            if sub in s:
                found.add(sub)
    return found


class DoeJsOracleShellout(ast.NodeVisitor):
    """Collects (lineno, argument-strings, matched-oracle-scripts) for every
    node+DoE-JS-oracle subprocess spawn call found in one parsed module."""

    def __init__(self) -> None:
        self.violations: list[tuple[int, list[str], set[str]]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _spawn_call_name(node.func)
        if name in _SPAWN_CALL_NAMES:
            strings = _call_argument_strings(node)
            oracle_scripts = _referenced_oracle_scripts(strings)
            if _references_node_invocation(strings) and oracle_scripts:
                self.violations.append((node.lineno, strings, oracle_scripts))
        self.generic_visit(node)


def find_doe_js_oracle_shellouts(root: Path) -> list[tuple[str, int, list[str], set[str]]]:
    """Walk root for .py files (excluding tests/ and test_*.py) and return
    every (relpath, lineno, argv-strings, matched-oracle-scripts) tuple that
    is a literal node+DoE-JS-oracle subprocess spawn call, skipping
    allowlisted paths and (file, oracle-script)-exempted calls.

    Used both against the real coordinator_core/ tree (the standing gate)
    and against an isolated tmp_path fixture (the gate's own self-test,
    proving it actually detects rather than merely passing by absence).
    """
    violations: list[tuple[str, int, list[str], set[str]]] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded_source_path(path):
            continue
        try:
            relpath = path.resolve().relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            # root is outside the repo (e.g. a tmp_path fixture) -- use a
            # root-relative path for allowlist comparison, which will
            # simply never match any real allowlisted repo path.
            relpath = path.resolve().relative_to(root.resolve()).as_posix()
        if relpath in _ALLOWLISTED_RELPATHS:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        visitor = DoeJsOracleShellout()
        visitor.visit(tree)
        for lineno, strings, oracle_scripts in visitor.violations:
            unexempted_scripts = {
                script
                for script in oracle_scripts
                if (relpath, script) not in _EXEMPT_NODE_ORACLE_CALLS
            }
            if unexempted_scripts:
                violations.append((relpath, lineno, strings, unexempted_scripts))
    return violations


def test_no_doe_js_oracle_node_shellout_in_production_code():
    """Standing gate: coordinator_core/ non-test code must contain zero
    literal node+DoE-JS-oracle subprocess spawns, except the explicit,
    narrow, dated exemption(s) in _EXEMPT_NODE_ORACLE_CALLS. Every ported
    oracle (schema_validate.py/schema_cli.py from schema.js/schema-cli.js,
    and so on) is the sole canonical implementation once ported -- neither
    it nor any other production module should shell out to node to re-derive
    what it already computes (or could compute) natively.
    """
    violations = find_doe_js_oracle_shellouts(_SCAN_ROOT)
    assert violations == [], (
        "Found DoE-JS-oracle node shell-out(s) in coordinator_core/ "
        "production code (should be in-process only, or explicitly "
        f"exempted in _EXEMPT_NODE_ORACLE_CALLS): {violations}"
    )


def test_gate_detects_a_planted_query_records_node_shellout(tmp_path):
    """Proves the widened gate has teeth for a non-schema oracle: without
    this test, C8's widening would merely be passing-by-absence and would
    not catch a future regression that reintroduces a node+query-records.js
    shell-out outside the one narrowly exempted (file, script) pair.
    """
    fixture = tmp_path / "fixture_reintroduced_shellout.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def revalidate(record_path):\n"
        "    return subprocess.run(\n"
        "        [\"node\", \"query-records.js\", \"--type\", \"widget\", record_path],\n"
        "        capture_output=True,\n"
        "    )\n",
        encoding="utf-8",
    )

    violations = find_doe_js_oracle_shellouts(tmp_path)

    assert len(violations) == 1
    relpath, lineno, strings, oracle_scripts = violations[0]
    assert relpath.endswith("fixture_reintroduced_shellout.py")
    assert lineno == 4
    assert "node" in strings
    assert "query-records.js" in strings
    assert oracle_scripts == {"query-records.js"}


def test_gate_detects_a_planted_deleted_oracle_node_shellout(tmp_path):
    """A call targeting an ALREADY-DELETED DoE JS oracle (e.g.
    handoff-transition.js) is worse than one targeting a live oracle -- the
    target doesn't even exist -- and must still be flagged.
    """
    fixture = tmp_path / "fixture_deleted_oracle_shellout.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def transition(handoff_path):\n"
        "    return subprocess.run(\n"
        "        [\"node\", \"handoff-transition.js\", handoff_path],\n"
        "        capture_output=True,\n"
        "    )\n",
        encoding="utf-8",
    )

    violations = find_doe_js_oracle_shellouts(tmp_path)

    assert len(violations) == 1
    relpath, lineno, strings, oracle_scripts = violations[0]
    assert relpath.endswith("fixture_deleted_oracle_shellout.py")
    assert "handoff-transition.js" in strings
    assert oracle_scripts == {"handoff-transition.js"}


def test_gate_ignores_doc_comment_backlinks_and_unrelated_node_uses(tmp_path):
    """Negative control: the exact shape of noise this gate must NOT flag --
    a "Port of schema.js:..." docstring/comment mentioning schema.js, a
    node-unrelated subprocess spawn, and a node spawn with no oracle-script
    argument (the install-probe shape)."""
    fixture = tmp_path / "fixture_benign.py"
    fixture.write_text(
        '"""Port of schema.js: validateRecord, validateFrontmatter."""\n'
        "import subprocess\n"
        "\n"
        "# Mirrors schema.js BLOCK_SCALAR_RE (schema.js:42).\n"
        "def probe_node_present():\n"
        "    return subprocess.run([\"node\", \"--version\"], capture_output=True)\n"
        "\n"
        "def list_files():\n"
        "    return subprocess.run([\"ls\", \"-la\"], capture_output=True)\n",
        encoding="utf-8",
    )

    violations = find_doe_js_oracle_shellouts(tmp_path)

    assert violations == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
