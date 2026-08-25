"""Cross-cutting pin tests for the branch-creation-seam guard trio
(`block_noncanonical_branch_creation` / C1, `guard_branch_set_precedence` /
C5, `guard_longlived_branch_naming` / C7).

This file does not exercise any one guard's full substrate table (each
guard's own `test_*.py` owns that) -- it pins the plan's hardest
CROSS-GUARD negative guarantees: the retired inline-override hatch stays
dead (AC3), ceremony-side creation traffic never trips these guards (AC5),
C1 alone never compares branch DATES to decide a verdict (AC9), rename is
untouched by all three while `git branch <name>` create is C1-only (AC10/
AC14), all three gate on `_is_hazard_repo` before any predicate (AC13), and
C5's recency filter is a genuine `daily_branch` reuse, not a re-derived
constant (AC16).

Spec: docs/plans/2026-08-01-branch-creation-seam-guards.md, chunk C6.
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from coordinator_core.bash_guards import block_noncanonical_branch_creation as c1
from coordinator_core.bash_guards import guard_branch_set_precedence as c5
from coordinator_core.bash_guards import guard_longlived_branch_naming as c7
from coordinator_core import daily_branch

_GUARDS = (c1, c5, c7)


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


# ---------------------------------------------------------------------------
# AC3 helpers -- see TestAC3NoHatch.test_override_key_absent_from_guard_
# module_source for why these two checks replace a single raw substring
# scan (that scan tripped on a REQUIRED negative-spec docstring paragraph;
# see docstring on the test method itself).
# ---------------------------------------------------------------------------

#: A hatch-shaped env-var key: COORDINATOR_(ALLOW|OVERRIDE|DISABLE)_<rest>.
#: Matches the retired COORDINATOR_OVERRIDE_BRANCH and any future sibling
#: of the same shape.
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

    Review: coordinator:code-reviewer Finding 2 -- widened to close the
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

    Review: coordinator:code-reviewer Finding 2 -- widened to also inspect
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
        if isinstance(slice_node, ast.Index):  # py<3.9 compat
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


# ---------------------------------------------------------------------------
# AC3 -- the no-hatch pin.
# ---------------------------------------------------------------------------


class TestAC3NoHatch:
    # C1 flipped CONFINEMENT_DENY -> ADVISORY_REWRITE in 2ac049c5b (C14b,
    # per DR-277 "guards are advisory by default"); these two tests still
    # pin AC3's real guarantee -- the retired env-prefix hatch does not let
    # a caller escape the guard's notice -- now expressed against the
    # advisory envelope instead of a deny.
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
        # C1 advisory (post-2ac049c5b flip, see class-level note above).
        c1_ctx = _advisory_ctx(c1.check(_payload("git checkout -b fix/some-topic")))
        assert "COORDINATOR_OVERRIDE_BRANCH" not in c1_ctx

        # C7 advisory (sanctioned longlived prefix).
        c7_ctx = _advisory_ctx(c7.check(_payload("git checkout -b migration/topic-x")))
        assert "COORDINATOR_OVERRIDE_BRANCH" not in c7_ctx

        # C5 advisory (deterministic firing via injected provider).
        monkeypatch.setattr(c5, "_ahead_of_main", lambda branch, cwd=None: 3)
        monkeypatch.setattr(c5, "should_prompt_rename", lambda *a, **k: False)
        provider = lambda: [("work/delphipro/2026-08-01", c5._now() - 60)]
        c5_ctx = _advisory_ctx(
            c5.check(_payload("git checkout -b work/delphipro/2026-08-03"), branch_set_provider=provider)
        )
        assert "COORDINATOR_OVERRIDE_BRANCH" not in c5_ctx


# ---------------------------------------------------------------------------
# AC5 -- ceremony non-regression (unit-level shape checks, not e2e runs).
#
# Ceremony-side creation (session_ensure_branch.py, workday-start-step0.py,
# merge-recovery-and-tag-cut.py) mints these exact command shapes via
# in-process subprocess.run(argv-list) and NEVER as a Bash-tool call -- so
# the first, load-bearing leg of each case below is that none of these
# guards even sees a non-Bash-tool invocation. The second leg is defense in
# depth: even if one of these shapes WERE somehow observed at the Bash seam,
# the behavior is the one already ratified elsewhere (bare today-branch
# creation allows; -N collision suffixes are C1's own documented, deliberate
# incoherence per daily_branch.py's module docstring; rename is untouched).
# ---------------------------------------------------------------------------


class TestAC5CeremonyNonRegression:
    def test_non_bash_tool_shapes_never_match_any_guard(self):
        # session_ensure_branch.py / merge-recovery-and-tag-cut.py invoke
        # subprocess.run directly -- never surfaced through the PreToolUse
        # Bash tool at all. A payload whose tool_name isn't "Bash" is the
        # closest unit-level stand-in for "this op never transits the seam".
        shapes = [
            "git checkout -b work/delphipro/2026-08-01",
            "git checkout -b work/delphipro/2026-08-01-2",
            "git checkout -b work/delphipro/2026-08-01-9",
            "git branch -m work/delphipro/2026-07-30to01 work/delphipro/2026-08-01",
            "git branch -m work/delphipro/2026-08-01 work/delphipro/2026-07-30to01",
        ]
        for cmd in shapes:
            payload = _payload(cmd, tool_name=None)
            for g in _GUARDS:
                assert g.check(payload) is None

    def test_bare_today_branch_checkout_allows(self):
        # session_ensure_branch's fresh-cut default shape, and merge-
        # recovery-and-tag-cut's recovery-branch default shape.
        assert c1.check(_payload("git checkout -b work/delphipro/2026-08-01")) is None

    def test_collision_suffix_shapes_are_c1s_own_documented_incoherence(self):
        # daily_branch.py's own module docstring: -N collision suffixes are
        # NOT accepted by is_canonical_branch and never will be, because the
        # guard never has to judge them at the seam (they never arrive as
        # Bash). This is the accepted, non-regressed state -- not something
        # this pin asks C1 to change. C1 fires (now advisory, not deny --
        # see TestAC3NoHatch's class-level note on the 2ac049c5b flip)
        # rather than staying silent on them.
        for n in range(2, 10):
            out = c1.check(_payload("git checkout -b work/delphipro/2026-08-01-%d" % n))
            _advisory_ctx(out)

    def test_workday_start_step0_rename_and_rollback_untouched(self):
        rename = "git branch -m work/delphipro/2026-07-30to01 work/delphipro/2026-08-01"
        rollback = "git branch -m work/delphipro/2026-08-01 work/delphipro/2026-07-30to01"
        for cmd in (rename, rollback):
            for g in _GUARDS:
                assert g.check(_payload(cmd)) is None

    def test_recovery_branch_cut_shape_allows(self):
        # merge-recovery-and-tag-cut.py's cmd_recovery_branch: `git checkout
        # -b work/{host}/{today}` -- same canonical shape as the daily cut.
        assert c1.check(_payload("git checkout -b work/some-host/2026-08-01")) is None


# ---------------------------------------------------------------------------
# AC9 -- no branch-DATE-vs-current-date COMPARISON in C1 specifically.
# ---------------------------------------------------------------------------


class TestAC9NoDateComparisonInC1:
    # Post-2ac049c5b (C14b), C1's remediation-text helper is named
    # `_advisory_reason` (was `_deny_reason`) -- see DR-277 and
    # TestAC3NoHatch's class-level note above. The AC9 guarantee itself
    # (local_day never feeds a comparison, only remediation text) is
    # unchanged by the flip.
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
        out_a = c1.check(_payload("git checkout -b work/delphipro/2026-08-01"))
        monkeypatch.setattr(c1, "local_day", lambda: "2030-12-31")
        out_b = c1.check(_payload("git checkout -b work/delphipro/2026-08-01"))
        assert out_a is None
        assert out_b is None


# ---------------------------------------------------------------------------
# AC10/AC14 -- rename untouched by all three; `git branch <name>` create is
# C1-only.
# ---------------------------------------------------------------------------


class TestAC10AC14RenameVsCreate:
    @pytest.mark.parametrize("flag", ["-m", "-M"])
    def test_branch_rename_untouched_by_all_three(self, flag):
        cmd = "git branch %s old-name new-name" % flag
        for g in _GUARDS:
            assert g.check(_payload(cmd)) is None

    def test_branch_create_advised_by_c1_only(self):
        # C1 fires (advisory, post-2ac049c5b flip -- see TestAC3NoHatch's
        # class-level note above); C5/C7 do not even inspect `git branch`
        # -- see each module's own docstring ("WHAT THIS DOES"/"Injection
        # seam").
        cmd = "git branch some-noncanonical-name"
        _advisory_ctx(c1.check(_payload(cmd)))
        assert c5.check(_payload(cmd)) is None
        assert c7.check(_payload(cmd)) is None


# ---------------------------------------------------------------------------
# AC13 -- all three gate on _is_hazard_repo BEFORE evaluating any predicate.
# ---------------------------------------------------------------------------


class TestAC13HazardRepoGateFirst:
    def test_all_three_pass_silently_when_not_a_hazard_repo(self, monkeypatch):
        for g in _GUARDS:
            monkeypatch.setattr(g, "_is_hazard_repo", lambda git_root: False)

        shapes = [
            "git checkout -b fix/some-topic",  # C1 would otherwise deny
            "git checkout -b migration/some-topic",  # C7 would otherwise advise
            "git branch fix/some-topic",  # C1 would otherwise deny
        ]
        for cmd in shapes:
            for g in _GUARDS:
                assert g.check(_payload(cmd)) is None

    def test_c5_passes_silently_and_spends_no_enumeration_when_not_a_hazard_repo(self, monkeypatch):
        monkeypatch.setattr(c5, "_is_hazard_repo", lambda git_root: False)
        calls = []
        monkeypatch.setattr(c5, "_other_canonical_branches", lambda cwd=None: calls.append(1) or [])
        out = c5.check(_payload("git checkout -b work/delphipro/2026-08-01"))
        assert out is None
        assert calls == []


# ---------------------------------------------------------------------------
# AC16 -- C5's recency filter reuses daily_branch.should_prompt_rename and
# _HOURS_48_SECONDS by IMPORT/CALL, not a re-derived constant.
# ---------------------------------------------------------------------------


class TestAC16RecencyFilterIsRealReuse:
    def test_identity_reuse_not_a_local_reimplementation(self):
        assert c5.should_prompt_rename is daily_branch.should_prompt_rename
        assert c5._HOURS_48_SECONDS == daily_branch._HOURS_48_SECONDS
        assert c5._HOURS_48_SECONDS is daily_branch._HOURS_48_SECONDS

    def test_real_should_prompt_rename_excludes_day_roll_candidate(self, monkeypatch):
        """Behavioural, injected epochs, REAL (unmocked) should_prompt_rename:
        a same-shape candidate whose span does not yet cover "today" and
        whose last commit is fresh is the day-roll case Step 0's rename
        path owns -- C5 must not also offer it as a resume target."""
        fixed_now = 1722700000.0
        fixed_today = "2026-08-03"
        monkeypatch.setattr(c5, "resolve_git_root", lambda cwd=None: "/repo")
        monkeypatch.setattr(c5, "_is_hazard_repo", lambda git_root: True)
        monkeypatch.setattr(c5, "_now", lambda: fixed_now)
        monkeypatch.setattr(c5, "_today", lambda: fixed_today)
        monkeypatch.setattr(c5, "_ahead_of_main", lambda branch, cwd=None: 5)

        recent_epoch = fixed_now - 3600  # 1h old, well within 48h
        provider = lambda: [("work/delphipro/2026-08-01", recent_epoch)]  # span end != today

        out = c5.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )
        assert out is None, "real should_prompt_rename should have excluded this day-roll candidate"

    def test_real_should_prompt_rename_survives_when_span_already_covers_today(self, monkeypatch):
        """Contrast case: a same-shape candidate whose span END already
        equals today survives the real should_prompt_rename leg (False --
        no rename needed), proving the exclusion above is genuinely driven
        by daily_branch's own logic, not an always-exclude stub."""
        fixed_now = 1722700000.0
        fixed_today = "2026-08-03"
        monkeypatch.setattr(c5, "resolve_git_root", lambda cwd=None: "/repo")
        monkeypatch.setattr(c5, "_is_hazard_repo", lambda git_root: True)
        monkeypatch.setattr(c5, "_now", lambda: fixed_now)
        monkeypatch.setattr(c5, "_today", lambda: fixed_today)
        monkeypatch.setattr(c5, "_ahead_of_main", lambda branch, cwd=None: 5)

        recent_epoch = fixed_now - 3600
        provider = lambda: [("work/delphipro/2026-08-03", recent_epoch)]  # span end == today

        out = c5.check(
            _payload("git checkout -b work/other-machine/2026-08-03"),
            branch_set_provider=provider,
        )
        _advisory_ctx(out)

    def test_real_age_leg_excludes_stale_candidate(self, monkeypatch):
        fixed_now = 1722700000.0
        fixed_today = "2026-08-03"
        monkeypatch.setattr(c5, "resolve_git_root", lambda cwd=None: "/repo")
        monkeypatch.setattr(c5, "_is_hazard_repo", lambda git_root: True)
        monkeypatch.setattr(c5, "_now", lambda: fixed_now)
        monkeypatch.setattr(c5, "_today", lambda: fixed_today)
        monkeypatch.setattr(c5, "_ahead_of_main", lambda branch, cwd=None: 5)

        stale_epoch = fixed_now - (daily_branch._HOURS_48_SECONDS + 3600)
        provider = lambda: [("work/delphipro/2026-07-20", stale_epoch)]

        out = c5.check(
            _payload("git checkout -b work/delphipro/2026-08-03"),
            branch_set_provider=provider,
        )
        assert out is None
