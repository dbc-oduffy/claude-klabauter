"""Cross-cutting pin tests for the branch-creation-seam guard
(`block_noncanonical_branch_creation` / C1).

This file does not exercise C1's full substrate table (its own `test_*.py`
owns that) -- it pins the plan's hardest guarantees for the guard that
survives this trio: the retired inline-override hatch stays dead (AC3),
ceremony-side creation traffic never trips this guard (AC5), C1 alone never
compares branch DATES to decide a verdict (AC9), rename is untouched while
`git branch <name>` create is advised (AC10/AC14), and the guard gates on
`_is_hazard_repo` before any predicate (AC13).

Narrowed 2026-09-19 (docs/plans/2026-08-21-the-advisory-band-gets-smaller-
cheaper-and-honest.md, C6): `guard_branch_set_precedence` (C5) and
`guard_longlived_branch_naming` (C7) -- the other two members of the
original trio -- were deleted (zero fires in 14 days, mild/no harm on
noncompliance). Every assertion that exercised either guard specifically
(AC16's recency-filter pin, C5's no-enumeration-when-not-hazard pin, and
every explicit c5/c7 call inline in a shared test) is gone with them;
`block_noncanonical_branch_creation` is deliberately KEPT (see the plan's
own C6 body) and every assertion that pins IT survives unchanged.

Spec: docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C6.
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from coordinator_core.bash_guards import block_noncanonical_branch_creation as c1

_GUARDS = (c1,)


def _payload(command, cwd="/repo", tool_name="Bash"):
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": cwd,
    }


def _advisory_ctx(out):
    assert out is not None, "expected an advisory envelope, got no-op"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    return hso["additionalContext"]


# scan (that scan tripped on a REQUIRED negative-spec docstring paragraph;

#: A hatch-shaped env-var key: COORDINATOR_(ALLOW|OVERRIDE|DISABLE)_<rest>.
#: Matches the retired COORDINATOR_OVERRIDE_BRANCH and any future sibling
_HATCH_KEY_RE = re.compile(r"COORDINATOR_(?:ALLOW|OVERRIDE|DISABLE)_[A-Z0-9_]+")


def _docstring_node_ids(tree):
    """Return `id()` of every AST string-constant node that is a MODULE,
    CLASS, or FUNCTION docstring (the first statement of that scope's
    body, when it is a bare string expression). Mirrors
    `test_no_handwritten_override_clauses.py`'s `_docstring_node_ids` --
    same exemption, same reasoning: developer-facing explanation text is
    never emitted to a caller and must be free to name a retired key
    while explaining why it does not work."""
    exempt = set()
    scopes = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(node)
    for scope in scopes:
        body = getattr(scope, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            exempt.add(id(first.value))
    return exempt


def _os_environ_getenv_aliases(tree):
    """Return `(environ_names, getenv_names)`: local names bound via
    `from os import environ[ as x]` / `from os import getenv[ as x]`, so a
    bare-`Name` access after importer aliasing (`from os import environ`
    then `environ.get(...)`) is still recognized by `_environ_key_node`
    -- the plain `ast.Attribute` shape check (`os.environ`) is blind to
    this form on its own.

    Widened to close the
    importer-aliasing bypass.
    """
    environ_names = set()
    getenv_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "environ":
                    environ_names.add(bound)
                elif alias.name == "getenv":
                    getenv_names.add(bound)
    return environ_names, getenv_names


def _environ_key_node(node, environ_names, getenv_names):
    """If `node` is an `os.environ.get(...)`, `os.getenv(...)`, or
    `os.environ[...]` read -- including a bare-`Name` form after
    `from os import environ`/`getenv` aliasing (`environ_names`/
    `getenv_names`), and including the key passed as the `key=` keyword
    rather than positionally -- return its key-argument AST node
    (positional arg, keyword-arg value, or the subscript index);
    otherwise `None`. The returned node may be a non-`ast.Constant`
    expression (e.g. a `BinOp` concatenation or an f-string); the caller
    treats that as a hard failure rather than silently skipping it -- see
    the docstring on `test_override_key_absent_from_guard_module_source`.

    Widened to also inspect
    `node.keywords` (keyword-arg key form) and bare-`Name` environ/getenv
    aliases, neither of which the original `ast.Attribute`/`node.args`-only
    shape check recognized.
    """

    def _is_environ_expr(value_node):
        if isinstance(value_node, ast.Attribute) and value_node.attr == "environ":
            return True
        if isinstance(value_node, ast.Name) and value_node.id in environ_names:
            return True
        return False

    def _key_from_call(call_node):
        if call_node.args:
            return call_node.args[0]
        for kw in call_node.keywords:
            if kw.arg == "key":
                return kw.value
        return None

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "getenv":
            return _key_from_call(node)
        if isinstance(func, ast.Name) and func.id in getenv_names:
            return _key_from_call(node)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and _is_environ_expr(func.value)
        ):
            return _key_from_call(node)
    if isinstance(node, ast.Subscript) and _is_environ_expr(node.value):
        slice_node = node.slice
        if isinstance(slice_node, ast.Index):
            slice_node = slice_node.value
        return slice_node
    return None


def _strip_docstring_spans(src, tree):
    """Return `src` with every module/class/function docstring's line
    span blanked out (comments and non-docstring string literals are left
    intact) -- the source-text half of the no-hatch check must still see
    a comment or an executable-code string literal naming the key, and
    only exempt genuine docstring prose."""
    exempt_ids = _docstring_node_ids(tree)
    lines = src.splitlines(keepends=True)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) in exempt_ids:
            spans.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    for start, end in spans:
        for i in range(start, end + 1):
            lines[i - 1] = "\n" if lines[i - 1].endswith("\n") else ""
    return "".join(lines)


@pytest.fixture(autouse=True)
def _hazard_repo_by_default(monkeypatch):
    """Every test in this file runs "inside" a hazard repo by default --
    AC13's own test overrides this locally, per guard."""
    for g in _GUARDS:
        monkeypatch.setattr(g, "resolve_git_root", lambda cwd=None: "/repo")
        monkeypatch.setattr(g, "_is_hazard_repo", lambda git_root: True)


class TestAC3NoHatch:
    # C1 flipped CONFINEMENT_DENY -> ADVISORY_REWRITE in 2ac049c5b (C14b,
    def test_env_prefix_override_still_advises(self):
        out = c1.check(_payload('COORDINATOR_OVERRIDE_BRANCH=1 git checkout -b bad-name'))
        _advisory_ctx(out)

    def test_env_prefix_override_still_advises_git_branch_form(self):
        out = c1.check(_payload("COORDINATOR_OVERRIDE_BRANCH=1 git branch bad-name"))
        _advisory_ctx(out)

    def test_override_key_absent_from_guard_module_source(self):
        """Deliberately NOT a raw substring scan of the whole module
        source (the original form of this test) -- a required negative-
        spec docstring paragraph explaining WHY there is no override
        (this fleet's RAG-bait doctrine mandates negative-spec blocks,
        not optional prose) legitimately NAMES the retired key while
        explaining it does not work, and a bare substring ban forces a
        doctrine violation just to pass this pin. Docstrings are never
        emitted to a caller -- `_alternative_liveness._OVERRIDE_RE` scans
        EMITTED MESSAGE TEXT only, and AC3's real protection is that scan
        (see `test_override_key_absent_from_every_emitted_message`,
        unchanged, still strict). Precedent for the docstring carve-out:
        `test_no_handwritten_override_clauses.py` exempts module/class/
        function docstrings from its own override scan for the same
        reason.

        Replaced with two assertions STRICTLY STRONGER than the old
        substring check, not merely more permissive:

        (1) AST: no guard module reads a COORDINATOR_(ALLOW|OVERRIDE|
            DISABLE)_* key from the environment at all, across the
            `os.environ.get(...)` / `os.getenv(...)` / `os.environ[...]`
            forms, including the key passed as a `key=` keyword rather
            than positionally, and including `from os import environ`/
            `getenv` importer-aliased bare-`Name` access -- these are the
            three bypasses `_environ_key_node`/`_os_environ_getenv_aliases`
            close (Review: coordinator:code-reviewer Finding 2; the
            original version only recognized `os.environ`/`os.getenv`
            attribute access with a literal positional key argument).
            A key expression that is not an `ast.Constant` string (a
            `BinOp` concatenation, an f-string, or any other computed
            expression) is a HARD FAILURE here, not a silent skip: this
            is the exact opposite fail-direction from the guards
            themselves, which fail OPEN (allow) on a name they cannot
            statically resolve, because a guard denying a developer must
            never brick an unrelated command on a static-analysis miss.
            This test verifies a security property instead of enforcing
            one at runtime, so it fails CLOSED -- "I cannot statically
            read this key" must read as a hazard, not a pass, because a
            computed/concatenated key IS the hatch shape this pin exists
            to forbid.
        (2) Source text with docstrings stripped: a comment or a string
            literal in EXECUTABLE code still fails this check -- only
            genuine docstring prose is exempt.

        Do not "restore" the broader whole-source ban -- it is
        incompatible with the doctrine that requires this negative-spec
        paragraph to exist in the first place.
        """
        for g in _GUARDS:
            src = inspect.getsource(g)
            tree = ast.parse(src)
            environ_names, getenv_names = _os_environ_getenv_aliases(tree)

            offenders = []
            unresolvable = []
            for node in ast.walk(tree):
                key_node = _environ_key_node(node, environ_names, getenv_names)
                if key_node is None:
                    continue
                if isinstance(key_node, ast.Constant) and isinstance(
                    key_node.value, str
                ):
                    if _HATCH_KEY_RE.search(key_node.value):
                        offenders.append(key_node.value)
                else:
                    unresolvable.append(ast.dump(key_node))
            assert offenders == [], (
                "%s reads a hatch-shaped env key from the environment: %r"
                % (g.__name__, offenders)
            )
            assert unresolvable == [], (
                "%s reads os.environ/getenv with a non-literal key "
                "expression (%r) -- this pin fails CLOSED on an "
                "unresolvable key (see method docstring): a computed/"
                "concatenated key IS the hatch shape this check exists "
                "to forbid." % (g.__name__, unresolvable)
            )

            remaining_src = _strip_docstring_spans(src, tree)
            assert not _HATCH_KEY_RE.search(remaining_src), (
                "%s names a hatch-shaped key outside a docstring "
                "(comment or executable-code string literal)" % g.__name__
            )

    def test_override_key_absent_from_every_emitted_message(self, monkeypatch):
        c1_ctx = _advisory_ctx(c1.check(_payload("git checkout -b fix/some-topic")))
        assert "COORDINATOR_OVERRIDE_BRANCH" not in c1_ctx


class TestAC5CeremonyNonRegression:
    def test_non_bash_tool_shapes_never_match_any_guard(self):
        shapes = [
            "git checkout -b work/machine-b/2026-08-01",
            "git checkout -b work/machine-b/2026-08-01-2",
            "git checkout -b work/machine-b/2026-08-01-9",
            "git branch -m work/machine-b/2026-07-30to01 work/machine-b/2026-08-01",
            "git branch -m work/machine-b/2026-08-01 work/machine-b/2026-07-30to01",
        ]
        for cmd in shapes:
            payload = _payload(cmd, tool_name=None)
            for g in _GUARDS:
                assert g.check(payload) is None

    def test_bare_today_branch_checkout_allows(self):
        assert c1.check(_payload("git checkout -b work/machine-b/2026-08-01")) is None

    def test_collision_suffix_shapes_are_c1s_own_documented_incoherence(self):
        for n in range(2, 10):
            out = c1.check(_payload("git checkout -b work/machine-b/2026-08-01-%d" % n))
            _advisory_ctx(out)

    def test_workday_start_step0_rename_and_rollback_untouched(self):
        rename = "git branch -m work/machine-b/2026-07-30to01 work/machine-b/2026-08-01"
        rollback = "git branch -m work/machine-b/2026-08-01 work/machine-b/2026-07-30to01"
        for cmd in (rename, rollback):
            for g in _GUARDS:
                assert g.check(_payload(cmd)) is None

    def test_recovery_branch_cut_shape_allows(self):
        assert c1.check(_payload("git checkout -b work/some-host/2026-08-01")) is None


# AC9 -- no branch-DATE-vs-current-date COMPARISON in C1 specifically.


class TestAC9NoDateComparisonInC1:
    def test_local_day_only_referenced_inside_advisory_reason(self):
        """Structural (AST) assertion: `local_day` is named nowhere in C1's
        module except inside `_advisory_reason` (remediation text only)."""
        src = inspect.getsource(c1)
        tree = ast.parse(src)
        offending = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name != "_advisory_reason":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and sub.id == "local_day":
                        offending.append(node.name)
        assert offending == [], "local_day referenced outside _advisory_reason in: %r" % offending

    def test_no_compare_node_involving_local_day_anywhere(self):
        """No ast.Compare node in C1's module has `local_day` (call or
        name) as either operand -- a stronger structural guarantee than the
        function-scoping check above: even inside `_advisory_reason` itself,
        `local_day()`'s return value is only ever interpolated into text,
        never compared."""
        src = inspect.getsource(c1)
        tree = ast.parse(src)

        def _names_local_day(node):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == "local_day":
                    return True
            return False

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                operands = [node.left] + node.comparators
                assert not any(_names_local_day(op) for op in operands), (
                    "found a Compare node involving local_day"
                )

    def test_verdict_identical_across_different_todays(self, monkeypatch):
        """Behavioural: the deny/allow verdict for a fixed noncanonical name
        does not change when `local_day()` (only used for remediation text)
        returns a different date."""
        monkeypatch.setattr(c1, "local_day", lambda: "2020-01-01")
        out_a = c1.check(_payload("git checkout -b fix/some-topic"))
        monkeypatch.setattr(c1, "local_day", lambda: "2030-12-31")
        out_b = c1.check(_payload("git checkout -b fix/some-topic"))
        assert out_a is not None and out_b is not None
        assert out_a["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert out_b["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_verdict_identical_across_different_todays_for_canonical_name(self, monkeypatch):
        monkeypatch.setattr(c1, "local_day", lambda: "2020-01-01")
        out_a = c1.check(_payload("git checkout -b work/machine-b/2026-08-01"))
        monkeypatch.setattr(c1, "local_day", lambda: "2030-12-31")
        out_b = c1.check(_payload("git checkout -b work/machine-b/2026-08-01"))
        assert out_a is None
        assert out_b is None


class TestAC10AC14RenameVsCreate:
    @pytest.mark.parametrize("flag", ["-m", "-M"])
    def test_branch_rename_untouched(self, flag):
        cmd = "git branch %s old-name new-name" % flag
        for g in _GUARDS:
            assert g.check(_payload(cmd)) is None

    def test_branch_create_advised_by_c1(self):
        cmd = "git branch some-noncanonical-name"
        _advisory_ctx(c1.check(_payload(cmd)))


class TestAC13HazardRepoGateFirst:
    def test_passes_silently_when_not_a_hazard_repo(self, monkeypatch):
        for g in _GUARDS:
            monkeypatch.setattr(g, "_is_hazard_repo", lambda git_root: False)

        shapes = [
            "git checkout -b fix/some-topic",
            "git branch fix/some-topic",
        ]
        for cmd in shapes:
            for g in _GUARDS:
                assert g.check(_payload(cmd)) is None
