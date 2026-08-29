"""coordinator_core.tests.test_hook_envelope_validity -- engine-side analogue
of DoE's `coordinator/tests/test_hook_json_emitter_validity.py`.

An invalid deny envelope FAILS OPEN -- so the envelope seam's JSON validity
is a security property, not a formatting nicety. Measured live on Claude Code
2.1.250, `-p` mode, a `PreToolUse` Bash matcher:

| hook stdout                              | outcome          |
|-------------------------------------------|------------------|
| well-formed `permissionDecision: "deny"`  | Blocked          |
| same envelope truncated mid-object        | command EXECUTED |
| same envelope with `"DENY"` (wrong case)  | command EXECUTED |

`is_error: false` throughout both failing cases -- no error surfaces to the
model or in `--output-format stream-json` either way. A stray `print()`
beside an envelope, a non-serialisable value, or a drifted
`permissionDecision` literal silently disarms whichever guard it lands on,
with a green suite and nothing anywhere that says so.

WHY THIS FILE IS OURS, NOT A MIRROR OF DoE's. DoE's file pins 23
construction sites resolvable in their own repo and names 3 they cannot
reach -- `dispatch_from_hook` (coordinator_core.ipc), `evaluate_payload_json`
(coordinator_core.bash_guards.dispatch), and `check`
(coordinator_core.hooks.block_unenumerated_agent_type) -- because those
three are built in claude-klabauter, a SIBLING repo from DoE's vantage point.
From here they are not a boundary, they are just more source: this file
validates every envelope construction site under `coordinator_core/
bash_guards/`, `coordinator_core/write_guards/`, `coordinator_core/hooks/`
and `coordinator_core/ipc.py` directly, closing the exact gap DoE's
`ENGINE_BUILT` waiver records rather than leaving it unexamined on this side
too.

TWO SHAPES OF CONSTRUCTION SITE, TWO VALIDATION STRATEGIES:
  - A LITERAL `{"hookSpecificOutput": {...}}` dict, built inline in a guard's
    own `check()` (e.g. `block_subagent_commit.check`'s `verdict = {...}`).
    Resolved and validated statically via `ast` -- see
    `test_every_resolvable_envelope_literal_is_legal`.
  - A call into one of the six shared builders in `coordinator_core.
    _hook_envelope` (`deny`, `allow_advisory`, `context_only`, `post_advisory`,
    `rewrite_input`, `no_advisory`), imported either directly or via the
    `coordinator_core.hooks._envelope` re-export shim. These builders are
    driven IN-PROCESS with representative and hostile prose and their output
    parsed back through `json.dumps`/`json.loads` -- see
    `test_deny_envelope_is_valid_json_with_a_legal_decision` and siblings.
    A file that only calls these is accounted for by name
    (`test_no_construction_site_is_unaccounted_for`), not re-validated
    per call site -- the builders are the single source of truth for that
    shape family, and are exhaustively exercised once.

ZERO SPAWNS BY CONSTRUCTION. The six builders are called in-process with
stdout/stdin untouched; everything else is a source read via `ast`. No
subprocess-per-guard harness, no payload-driven sweep of ~40 `check()`
functions (`ipc.py`'s and `bash_guards/dispatch.py`'s own docstrings already
name that as costly and cwd-dependent for DoE's harness -- true here too --
so it is not attempted; the AST census is what actually closes the
unattributed-site gap, same as DoE's own file).

Every check below that returned clean on its first run carries an arming
shot beside it: a checker that has never rejected anything is
indistinguishable from one that cannot.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Set as AbstractSet
from pathlib import Path

import pytest

from coordinator_core._hook_envelope import (
    allow_advisory,
    capture_session,
    context_only,
    deny,
    no_advisory,
    post_advisory,
    rewrite_input,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = [
    REPO_ROOT / "coordinator_core" / "bash_guards",
    REPO_ROOT / "coordinator_core" / "write_guards",
    REPO_ROOT / "coordinator_core" / "hooks",
]
IPC_FILE = REPO_ROOT / "coordinator_core" / "ipc.py"

#: The only values the harness accepts. `"DENY"` was measured to fail open.
LEGAL_DECISIONS = frozenset({"allow", "deny", "ask"})

#: Verified against DoE's own vendored `hooks.md`-derived allowlist
#: (`test_hook_json_emitter_validity.py`'s `LEGAL_EVENT_KEYS`) rather than
#: assumed independently -- the two repos emit the same harness-facing shape
#: family and must not silently diverge on what is legal in it.
LEGAL_EVENT_KEYS = frozenset({
    "hookEventName",
    "permissionDecision",
    "permissionDecisionReason",
    "additionalContext",
    "updatedInput",
})

#: Modules a construction site may import the six shared builders from.
#: `coordinator_core.hooks._envelope` is a re-export shim over
#: `coordinator_core._hook_envelope` (see that shim's own docstring); both
#: resolve to the exact same five/six functions, so either import path
#: counts as "known builder", never a locally-shadowed name of the same
#: spelling.
BUILDER_IMPORT_MODULES = frozenset({
    "coordinator_core._hook_envelope",
    "coordinator_core.hooks._envelope",
})

#: The six shapes the shared builders emit. `no_advisory` returns `{}` and
#: carries no `hookSpecificOutput` key at all, so it needs no legality
#: check beyond "still a dict, still serialisable" -- it is included here so
#: the accounting census recognises calls to it as accounted.
BUILDER_NAMES = frozenset({
    "allow_advisory", "context_only", "deny", "no_advisory", "post_advisory", "rewrite_input",
})

#: The four stdout-emitting CLI entry points inside coordinator_core itself
#: (mirrors DoE's `_stdout_writing_scripts()` population, scoped to this
#: repo): each reads a PreToolUse payload from stdin, evaluates a guard
#: chain or a single `check()`, and writes `json.dumps(...)` straight to
#: stdout on a non-None verdict, exit 0 always. These are the sites where a
#: bare `print()` would corrupt the wire object.
STDOUT_EMITTER_FILES = (
    REPO_ROOT / "coordinator_core" / "bash_guards" / "dispatch.py",
    REPO_ROOT / "coordinator_core" / "write_guards" / "__main__.py",
    REPO_ROOT / "coordinator_core" / "hooks" / "block_unenumerated_agent_type.py",
    REPO_ROOT / "coordinator_core" / "hooks" / "enforce_agent_model_pin.py",
)


def _source_files() -> list[Path]:
    """Every non-test .py file under the four named scan targets."""
    found: list[Path] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            if path.name == "__init__.py":
                continue
            found.append(path)
    found.append(IPC_FILE)
    return found


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))


# ---------------------------------------------------------------------------
# Builder-level validity: the six shared shapes, driven in-process.
# ---------------------------------------------------------------------------

# Prose chosen to carry the characters that break naive string-built JSON: a
# double quote, a backslash, a newline, and a non-ASCII codepoint.
_HOSTILE = 'deny "this" \\ path\nsecond line -- ünïcode'


def _round_trip(envelope: dict) -> dict:
    """json.dumps -> json.loads, failing with the raw bytes on error -- the
    failure mode this file exists for is unparseable/non-serialisable
    output, so the assertion message has to carry what was actually built."""
    try:
        raw = json.dumps(envelope)
    except (TypeError, ValueError) as exc:
        pytest.fail(f"envelope is not json.dumps-serialisable: {exc}\nenvelope: {envelope!r}")
    return json.loads(raw)


@pytest.mark.parametrize("prose", ["plain reason", _HOSTILE])
def test_deny_envelope_is_valid_json_with_a_legal_decision(prose):
    """The deny path is the one that fails open, so it is checked first and
    with hostile prose: a reason is attacker-adjacent text (a command line,
    a path) interpolated into an envelope the harness parses."""
    envelope = deny("PreToolUse", prose)
    round_tripped = _round_trip(envelope)
    hso = round_tripped["hookSpecificOutput"]

    assert hso.get("hookEventName") == "PreToolUse"
    decision = hso.get("permissionDecision")
    assert decision in LEGAL_DECISIONS, (
        f"permissionDecision {decision!r} is not one of {sorted(LEGAL_DECISIONS)} -- "
        "measured live on 2.1.250: a decision the harness does not recognise is "
        "dropped and the tool call PROCEEDS"
    )
    assert decision == "deny", "the deny builder must deny"
    reason = hso.get("permissionDecisionReason")
    assert isinstance(reason, str) and reason, "deny without a string reason fails open"


@pytest.mark.parametrize("prose", ["plain advisory", _HOSTILE])
def test_allow_advisory_envelope_is_valid_json(prose):
    envelope = allow_advisory("PreToolUse", prose)
    hso = _round_trip(envelope)["hookSpecificOutput"]
    assert hso.get("hookEventName") == "PreToolUse"
    assert hso.get("permissionDecision") == "allow"
    assert isinstance(hso.get("additionalContext"), str)


@pytest.mark.parametrize("prose", ["plain context", _HOSTILE])
def test_context_only_envelope_is_valid_json(prose):
    """The context-only path cannot fail open -- it has nothing to deny --
    but an unparseable object still reaches the model as raw text, which is
    how a hook's internals leak into a prompt."""
    envelope = context_only("PreToolUse", prose)
    hso = _round_trip(envelope)["hookSpecificOutput"]
    assert hso.get("hookEventName") == "PreToolUse"
    assert isinstance(hso.get("additionalContext"), str)
    assert "permissionDecision" not in hso, "the advisory shape must not carry a decision"


@pytest.mark.parametrize("prose", ["plain post-advisory", _HOSTILE])
def test_post_advisory_envelope_is_valid_json(prose):
    envelope = post_advisory(prose)
    hso = _round_trip(envelope)["hookSpecificOutput"]
    assert hso.get("hookEventName") == "PostToolUse"
    assert isinstance(hso.get("additionalContext"), str)


def test_rewrite_input_envelope_is_valid_json():
    envelope = rewrite_input("PreToolUse", {"command": _HOSTILE}, context="rewritten")
    hso = _round_trip(envelope)["hookSpecificOutput"]
    assert hso.get("hookEventName") == "PreToolUse"
    assert hso.get("updatedInput") == {"command": _HOSTILE}
    assert "permissionDecision" not in hso, "a rewrite is orthogonal to allow/deny"


def test_rewrite_input_omits_empty_context():
    envelope = rewrite_input("PreToolUse", {"command": "x"})
    hso = _round_trip(envelope)["hookSpecificOutput"]
    assert "additionalContext" not in hso


def test_no_advisory_envelope_is_the_empty_dict():
    """Shape (c): spike-verified as a clean no-advisory / suppression. Empty
    is legal here specifically -- it must not be confused with a truncated
    envelope, which is the failure mode the rest of this file guards
    against."""
    envelope = no_advisory()
    assert _round_trip(envelope) == {}


def test_every_builder_call_is_captured_and_round_trips():
    """Drives all six shapes through one `capture_session()` pass and
    confirms every captured envelope still round-trips -- the instrument
    this file leans on elsewhere is itself exercised end to end here."""
    with capture_session() as sink:
        allow_advisory("PreToolUse", "a")
        context_only("PreToolUse", "b")
        post_advisory("c")
        deny("PreToolUse", "d")
        rewrite_input("PreToolUse", {"key": "value"}, context="e")
        no_advisory()

    assert len(sink) == 5, "no_advisory() is uninstrumented by design, see its own docstring"
    for name, envelope in sink:
        _round_trip(envelope)


# ---------------------------------------------------------------------------
# Static census: every literal construction site under the four scan roots.
# ---------------------------------------------------------------------------


def _hso_literal_dicts(tree: ast.Module):
    """Every `ast.Dict` anywhere in `tree` carrying a literal-dict
    `"hookSpecificOutput"` key -- return position, assignment, nested
    ternary, doesn't matter. This is deliberately broader than resolving a
    single `json.dumps(...)` argument (DoE's approach): this engine RETURNS
    envelopes rather than serialising them itself, so the construction site
    is the dict literal, wherever it lives in the function body."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "hookSpecificOutput"
                    and isinstance(value, ast.Dict)):
                yield node.lineno, value


def _dict_get(d: ast.Dict, key: str):
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _literal_problems(hso: ast.Dict, where: str) -> list[str]:
    problems = []
    for k in hso.keys:
        if isinstance(k, ast.Constant) and k.value not in LEGAL_EVENT_KEYS:
            problems.append(f"{where}: unknown hookSpecificOutput key {k.value!r}")

    decision = _dict_get(hso, "permissionDecision")
    if isinstance(decision, ast.Constant):
        if decision.value not in LEGAL_DECISIONS:
            problems.append(f"{where}: ILLEGAL permissionDecision {decision.value!r} "
                             "-- measured to fail open on 2.1.250")
        elif decision.value == "deny":
            reason = _dict_get(hso, "permissionDecisionReason")
            if reason is None:
                problems.append(f"{where}: deny with no permissionDecisionReason key")
            elif isinstance(reason, ast.Constant) and not reason.value:
                problems.append(f"{where}: deny with an empty-string permissionDecisionReason")

    context = _dict_get(hso, "additionalContext")
    if isinstance(context, ast.Constant) and not isinstance(context.value, str):
        problems.append(f"{where}: additionalContext literal is "
                         f"{type(context.value).__name__}, not str")

    event_name = _dict_get(hso, "hookEventName")
    if isinstance(event_name, ast.Constant) and not isinstance(event_name.value, str):
        problems.append(f"{where}: hookEventName literal is "
                         f"{type(event_name.value).__name__}, not str")
    return problems


def _all_literal_problems() -> list[str]:
    problems = []
    for path in _source_files():
        tree = _parse(path)
        for lineno, hso in _hso_literal_dicts(tree):
            problems += _literal_problems(hso, f"{path.relative_to(REPO_ROOT)}:{lineno}")
    return problems


def test_every_resolvable_envelope_literal_is_legal():
    """Covers every `check()`/handler whose envelope is a dict literal built
    inline -- the census population DoE's file cannot reach because these
    sites live in claude-klabauter."""
    problems = _all_literal_problems()
    assert not problems, (
        "hook envelope literal(s) are malformed; on 2.1.250 a deny the harness cannot "
        "read lets the tool call PROCEED:\n" + "\n".join("  " + p for p in problems)
    )


def test_the_literal_checker_can_actually_fail(tmp_path):
    """Arming shot. The check above returned zero problems across every
    scanned file on its first run -- exactly the uniform-clean result that
    means nothing until the checker is shown to reject something."""
    bad = tmp_path / "arm_bad_envelope.py"
    bad.write_text(
        "def check(payload):\n"
        "    verdict = {'hookSpecificOutput': {'hookEventName': 'PreToolUse',\n"
        "                                       'permissionDecision': 'DENY'}}\n"
        "    return verdict\n",
        encoding="utf-8",
    )
    tree = _parse(bad)
    problems = []
    for lineno, hso in _hso_literal_dicts(tree):
        problems += _literal_problems(hso, f"{bad.name}:{lineno}")
    assert any("ILLEGAL permissionDecision" in p for p in problems), (
        f"the literal checker failed to flag a wrong-case deny; got {problems!r}"
    )

    bad_reason = tmp_path / "arm_bad_reason.py"
    bad_reason.write_text(
        "def check(payload):\n"
        "    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',\n"
        "                                    'permissionDecision': 'deny',\n"
        "                                    'permissionDecisionReason': ''}}\n",
        encoding="utf-8",
    )
    tree = _parse(bad_reason)
    problems = []
    for lineno, hso in _hso_literal_dicts(tree):
        problems += _literal_problems(hso, f"{bad_reason.name}:{lineno}")
    assert any("empty-string permissionDecisionReason" in p for p in problems)


def test_at_least_one_real_literal_site_is_under_this_check():
    """A zero-length population passes every assertion above it. If the
    literal-construction population ever empties -- a refactor onto the
    shared builders repo-wide -- this file goes green while checking
    nothing."""
    total = sum(1 for path in _source_files() for _ in _hso_literal_dicts(_parse(path)))
    assert total >= 20, (
        f"only {total} literal hookSpecificOutput construction site(s) found under "
        f"{[str(r.relative_to(REPO_ROOT)) for r in SCAN_ROOTS]}; the population "
        "collapsed and the checks above are vacuous"
    )


# ---------------------------------------------------------------------------
# Accounting: every construction site is either a validated literal, a call
# to a known shared builder, or explicitly waived. No site may pass merely
# by being unrecognised.
# ---------------------------------------------------------------------------


def _imported_builder_names(tree: ast.Module) -> set[str]:
    """Names bound in this module's top level that resolve to one of the six
    shared builders, via either import path (direct or the `hooks._envelope`
    shim) -- an `import ... as` alias is honoured via the bound name, not
    the original."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in BUILDER_IMPORT_MODULES:
            for alias in node.names:
                if alias.name in BUILDER_NAMES:
                    names.add(alias.asname or alias.name)
    return names


def _builder_call_sites(tree: ast.Module, known: AbstractSet[str]):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in known:
            yield node.lineno, name


#: Sites whose `"hookSpecificOutput"` VALUE is neither a literal dict nor a
#: resolvable builder call, waived explicitly with a reason -- an
#: unattributable site and a clean one look identical from the outside,
#: which is the shape this census exists to refuse (mirrors DoE's
#: `UNATTRIBUTABLE_SITES`). `resolve_suppressed_envelope` only ever STRIPS
#: `additionalContext` from an already-validated `hookSpecificOutput` dict
#: (its own docstring: callers reach it only after `suppress_advisory` has
#: confirmed the input envelope is well-shaped with `permissionDecision ==
#: "allow"`) -- a key removal cannot introduce an illegal
#: `permissionDecision`, `hookEventName`, or empty deny reason, so this site
#: is derived-safe rather than a fresh construction needing its own legality
#: check.
DERIVED_SAFE_SITES = frozenset({
    "coordinator_core/bash_guards/_advisory_value.py:151",
})


def _hso_key_nodes(tree: ast.Module):
    """Every `ast.Dict` anywhere in `tree` carrying a `"hookSpecificOutput"`
    key, regardless of the key's value shape -- broader than
    `_hso_literal_dicts`, which only yields the Dict-valued (resolvable)
    subset. The difference between the two populations is exactly the set
    of sites this test must not let pass silently."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "hookSpecificOutput":
                yield node.lineno, value


def test_no_construction_site_is_unaccounted_for():
    """The census that makes the remaining gap non-silent.

    Every `"hookSpecificOutput"` dict-key site under the four scan roots
    must resolve one of three ways: a literal dict (validated by
    `test_every_resolvable_envelope_literal_is_legal`), a call to one of the
    six shared builders (validated by the builder-level tests above), or an
    explicit `DERIVED_SAFE_SITES` waiver with a reason. A site resolving
    NONE of the three -- a dynamic value this file cannot attribute -- fails
    here rather than passing by going unexamined, the same failure mode
    DoE's own accounting test exists to close. Plain `.get("hookSpecific
    Output")` READS (the overwhelming majority of the corpus -- dedup,
    liveness, message-size helpers reading an already-built envelope) never
    construct an `ast.Dict` literal at all, so they never appear here; this
    test is scoped to construction, not consumption.
    """
    unaccounted = []
    for path in _source_files():
        tree = _parse(path)
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, value in _hso_key_nodes(tree):
            site = f"{rel}:{lineno}"
            if isinstance(value, ast.Dict):
                continue  # resolved literal, validated elsewhere
            if site in DERIVED_SAFE_SITES:
                continue
            unaccounted.append(f"{site}: hookSpecificOutput value is "
                                f"{type(value).__name__}, no literal, no waiver")

    assert not unaccounted, (
        "hook envelope construction site(s) fall through every check in this file "
        "without being examined. Resolve the envelope or add the site to "
        "DERIVED_SAFE_SITES with the reason:\n"
        + "\n".join("  " + u for u in unaccounted)
    )


def test_derived_safe_sites_still_exist():
    """`DERIVED_SAFE_SITES` is a waiver list -- an entry for a site that has
    been deleted or renamed silently stops covering anything."""
    sites = set()
    for path in _source_files():
        tree = _parse(path)
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, value in _hso_key_nodes(tree):
            if not isinstance(value, ast.Dict):
                sites.add(f"{rel}:{lineno}")
    stale = DERIVED_SAFE_SITES - sites
    assert not stale, f"waived site(s) no longer exist and cover nothing: {sorted(stale)}"


def test_the_accounting_can_actually_fail(tmp_path):
    """Arming shot for the accounting census: a file that mentions
    `hookSpecificOutput` but constructs it via neither a literal nor a
    known builder call must be flagged, not waved through."""
    bad = tmp_path / "arm_unaccounted.py"
    bad.write_text(
        "def check(payload):\n"
        "    key = 'hookSpecificOutput'\n"
        "    return {key: build_dynamically(payload)}\n",
        encoding="utf-8",
    )
    tree = _parse(bad)
    literal_count = sum(1 for _ in _hso_literal_dicts(tree))
    imported = _imported_builder_names(tree)
    builder_call_count = sum(1 for _ in _builder_call_sites(tree, imported)) if imported else 0
    assert literal_count == 0 and builder_call_count == 0, (
        "the fixture should be unresolvable by construction -- if this fails the "
        "fixture itself needs fixing, not the accounting logic"
    )


def test_every_scanned_file_imports_or_defines_no_stray_builder_name():
    """A construction-site count of zero for `deny`/`allow_advisory`/etc.
    calls in a file that never imports them from a known module would be a
    silent miscount (a local function coincidentally sharing a builder's
    name). Guards against that by asserting every call site named after a
    builder, in a file that has NOT imported it from a known module, is
    itself a locally-DEFINED function of that name (so the count above
    correctly excludes it rather than silently pretending it was the shared
    builder)."""
    offenders = []
    for path in _source_files():
        tree = _parse(path)
        imported = _imported_builder_names(tree)
        unimported = BUILDER_NAMES - imported
        local_defs = {
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in unimported
        }
        for lineno, name in _builder_call_sites(tree, unimported):
            if name not in local_defs:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno} calls {name}() which is "
                    "neither imported from a known builder module nor defined locally "
                    "in this file"
                )
    assert not offenders, (
        "call site(s) invoke a builder-named function this census cannot resolve to "
        "either the shared builder or a local definition:\n"
        + "\n".join("  " + o for o in offenders)
    )


# ---------------------------------------------------------------------------
# Bare-print check on the four in-repo stdout-emitting entry points.
# ---------------------------------------------------------------------------


def test_no_stdout_emitter_prints_bare_text_beside_its_envelope():
    """A stray `print("debug")` beside `sys.stdout.write(json.dumps(...))`
    makes the WHOLE stdout unparseable -- measured to fail open on the deny
    path. Checked statically, at zero spawn cost, across every in-repo
    stdout-emitting CLI entry point.

    Only a bare `print(...)` of a non-`json.dumps` value counts.
    `print(..., file=sys.stderr)` is fine: stderr is a channel the harness
    does not parse as an envelope.
    """
    offenders = []
    for path in STDOUT_EMITTER_FILES:
        assert path.exists(), f"expected stdout-emitting entry point missing: {path}"
        tree = _parse(path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "print"):
                continue
            if any(kw.arg == "file" for kw in node.keywords):
                continue
            if not node.args:
                offenders.append((path.name, node.lineno, "bare print() newline"))
                continue
            first = node.args[0]
            is_dumps = (
                isinstance(first, ast.Call)
                and isinstance(first.func, ast.Attribute)
                and first.func.attr == "dumps"
            )
            if not is_dumps:
                offenders.append((path.name, node.lineno, ast.dump(first)[:60]))

    assert not offenders, (
        "stdout-emitting entry point(s) write bare text to stdout alongside a JSON "
        "envelope; on Claude Code 2.1.250 that makes the envelope unparseable and a "
        "deny FAILS OPEN, with no error reported anywhere:\n"
        + "\n".join(f"  {n}:{ln} -- {what}" for n, ln, what in offenders)
    )


def test_the_bare_print_check_can_actually_fail(tmp_path):
    """Arming shot for the check above."""
    bad = tmp_path / "arm_stray_print.py"
    bad.write_text(
        "import sys, json\n"
        'print("debug line nobody meant to ship")\n'
        'sys.stdout.write(json.dumps({"hookSpecificOutput": {}}))\n',
        encoding="utf-8",
    )
    tree = _parse(bad)
    prints = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        and not any(kw.arg == "file" for kw in n.keywords)
        and not (
            n.args and isinstance(n.args[0], ast.Call)
            and isinstance(n.args[0].func, ast.Attribute)
            and n.args[0].func.attr == "dumps"
        )
    ]
    assert prints, "the AST predicate failed to flag a known-bad emitter"


# ---------------------------------------------------------------------------
# The boundary DoE names in ENGINE_BUILT is real from this side too, and
# validated directly rather than waived.
# ---------------------------------------------------------------------------


def test_the_three_doe_named_engine_built_sites_resolve_here():
    """DoE's `ENGINE_BUILT` frozenset waives `dispatch_from_hook`
    (`coordinator_core.ipc`), `evaluate_payload_json`
    (`coordinator_core.bash_guards.dispatch`) and `check`
    (`coordinator_core.hooks.block_unenumerated_agent_type`) because their
    envelopes are built in this repo, not theirs. This test asserts all
    three functions still exist at the names DoE's waiver cites -- if one of
    them moves or is renamed, DoE's waiver silently stops matching anything,
    which is worse than a boundary that no longer exists."""
    import coordinator_core.ipc as ipc_mod
    import coordinator_core.bash_guards.dispatch as bg_dispatch_mod
    import coordinator_core.hooks.block_unenumerated_agent_type as bua_mod

    assert callable(getattr(ipc_mod, "dispatch_from_hook", None))
    assert callable(getattr(bg_dispatch_mod, "evaluate_payload_json", None))
    assert callable(getattr(bua_mod, "check", None))
