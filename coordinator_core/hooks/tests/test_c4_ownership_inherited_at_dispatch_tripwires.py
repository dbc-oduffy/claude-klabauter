"""
coordinator_core.hooks.tests.test_c4_ownership_inherited_at_dispatch_tripwires —
standing tripwires for AC5, AC6 and AC11 of
docs/plans/2026-08-04-ownership-inherited-at-dispatch.md.

The plan's central design (a dispatch-time declared-provenance set, C1/C2/C3/C3b)
was REFUSED by cross-repo ruling SC-DR-021
(example-doctrine-repo `coordinator/docs/wiki/scoped-safety-commits.md` @ `bedc7e0e2927`, token
`A-CLAIM-IS-WHAT-YOU-WROTE-NOT-WHAT-YOU-PLANNED`). This chunk (C4) is NOT dead —
it is the standing tripwire that stops the rejected write-time-attribution
design from re-entering silently, and is now MORE load-bearing than before the
refusal (both surviving pins are *negative*: they assert nothing widens).

Three tripwires:

  - AC5 — `coordinator_core.ops.session.scope_report
    .assert_paths_in_session_scope`'s `allow_orphans` parameter does not
    widen: its default stays `False`, it stays KEYWORD_ONLY (asserted on the
    parameter KIND via `inspect.signature`, not on source text), and its
    call-site inventory across `coordinator_core/` — derived mechanically by
    AST walk, each site carrying the `allow_orphans` argument it passes — is
    exactly the enumerated set. Adding or removing a call site fails.

  - AC11 — no write-time attribution anywhere: `coordinator_core.session.claims
    .self_claim` gains no new callers. The caller set is enumerated BY NAME
    below (grepped across `coordinator_core/`, excluding tests/docstrings/the
    definition itself) and asserted exactly — a new caller fails loudly and
    names itself, rather than silently passing a "count didn't change" check.
    Pinned against example-doctrine-repo's `scoped-safety-commits.md` SC-DR-001 negative-spec:
    "The architecture never attributes Bash writes at write time and does not
    need to."

  - AC6 — the DR-258 matcher in `coordinator_core.hooks.track_touched_files
    ._handler` is unchanged: a claim is recorded ONLY for Write/Edit/
    MultiEdit/NotebookEdit. Made BEHAVIOURAL (dispatches a Bash-tool-shaped
    payload and a Write-shaped payload at the real `_handler` and asserts on
    the resulting touched.txt), not source-text-grep-only — a grep alone
    passes on a semantically-equivalent rewrite and fails on a harmless
    comment reflow. The source-text grep is kept as a SECOND layer, per the
    plan's own instruction. Pinned against
    docs/decisions/DR-258-bash-mediated-writes-are-a-named-permanent-limit.md.

Negative-spec: this module asserts nothing about the REFUSED design (AC1-3,
AC7, AC10) — those rows are dead. It asserts only that the two surviving
negative pins hold.

Spec backlink: docs/plans/2026-08-04-ownership-inherited-at-dispatch.md § C4 / AC6 / AC11.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import subprocess
import warnings
from pathlib import Path

import pytest

from coordinator_core.hooks import track_touched_files as ttf
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.session.scope_report import assert_paths_in_session_scope
from coordinator_core.session import scope as touch_scope

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLAIMS_PATH = _REPO_ROOT / "coordinator_core" / "session" / "claims.py"
_TRACK_TOUCHED_FILES_PATH = _REPO_ROOT / "coordinator_core" / "hooks" / "track_touched_files.py"
_DR_258_PATH = (
    _REPO_ROOT
    / "docs"
    / "decisions"
    / "DR-258-bash-mediated-writes-are-a-named-permanent-limit.md"
)

# ---------------------------------------------------------------------------
# AC11 — self_claim's enumerated, exact caller set.
#
# Enumerated by hand via:
#   grep -rn "self_claim(" coordinator_core/ --include="*.py"
# then excluding: the definition itself (claims.py), test files, docstring
# mentions, and the js_bridge_cli CLI subcommand dict (module-level, distinct
# from a call). The three entries below are the only ACTUAL invocations of
# claims.self_claim / the `_self_claim` re-export outside claims.py itself
# and outside tests, verified 2026-08-04 against this repo's HEAD.
# ---------------------------------------------------------------------------
_KNOWN_SELF_CLAIM_CALLERS = frozenset(
    {
        "coordinator_core/snippet_sync/verify.py",
        "coordinator_core/text/refresh_queries.py",
        "coordinator_core/session/js_bridge_cli.py",
    }
)

# Matches a real invocation: `self_claim(` or `_self_claim(` NOT preceded by
# "def " (excludes the definition line) and not inside a docstring/comment
# mention that merely NAMES the function without calling it. This regex
# intentionally over-matches slightly (e.g. a bare mention followed by literal
# "(" in prose) — the point of AC11 is a human-reviewed caller census with a
# machine re-check, not a fully general call-graph analyzer; a false-positive
# widening of the found set only makes this tripwire STRICTER (more files to
# explain), never silently permissive.
_CALL_RE = re.compile(r"(?<!def )\bself_claim\(")


def _find_self_claim_call_files() -> set[str]:
    """Grep coordinator_core/ for files containing a self_claim(...) call,
    excluding claims.py itself (the definition) and any tests/ path.
    Returns POSIX-style repo-relative paths."""
    found: set[str] = set()
    for path in (_REPO_ROOT / "coordinator_core").rglob("*.py"):
        if path == _CLAIMS_PATH:
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if "/tests/" in rel or rel.startswith("coordinator_core/test_") or "/test_" in rel:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _CALL_RE.search(text):
            found.add(rel)
    return found


class TestAC11NoWriteTimeAttributionNewCallers:
    """Pin: self_claim gains no new callers beyond the enumerated set.

    Negative-spec (SC-DR-001, scoped-safety-commits.md): "The architecture
    never attributes Bash writes at write time and does not need to." An op
    self-naming its own write via a new self_claim call site IS write-time
    attribution re-entering — this is exactly the plan's own anti-scope item
    ("Do not add callers to claims.self_claim.").
    """

    def test_self_claim_caller_set_is_exactly_the_enumerated_set(self):
        found = _find_self_claim_call_files()
        assert found == set(_KNOWN_SELF_CLAIM_CALLERS), (
            "self_claim's caller set changed since this tripwire was written "
            f"— found={sorted(found)!r} expected={sorted(_KNOWN_SELF_CLAIM_CALLERS)!r}. "
            "A NEW caller here is write-time attribution re-entering the "
            "architecture (SC-DR-001 negative-spec, docs/plans/"
            "2026-08-04-ownership-inherited-at-dispatch.md anti-scope: 'Do not "
            "add callers to claims.self_claim.') — this is very likely NOT what "
            "you want; if it genuinely is, update _KNOWN_SELF_CLAIM_CALLERS "
            "here deliberately, with a decision record."
        )

    def test_known_caller_files_still_exist_and_still_call_self_claim(self):
        # Guards the census itself against silent staleness in the OTHER
        # direction (a caller removed without updating the enumerated set
        # would otherwise make the exact-set assertion above pass vacuously
        # once compensated by a coincidental new caller elsewhere).
        found = _find_self_claim_call_files()
        for expected in _KNOWN_SELF_CLAIM_CALLERS:
            assert expected in found, (
                f"expected self_claim caller {expected!r} no longer calls "
                "self_claim(...) — update _KNOWN_SELF_CLAIM_CALLERS"
            )


# ---------------------------------------------------------------------------
# AC6 — DR-258 matcher unchanged (behavioural, source-grep as second layer).
# ---------------------------------------------------------------------------


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


class TestAC6DR258MatcherBehavioural:
    """Behavioural pin (PRIMARY layer): dispatch a Bash-tool-shaped payload at
    the real `_handler` and confirm no claim lands; dispatch a Write-shaped
    payload and confirm one does. A source-text grep alone (see
    ``TestAC6DR258MatcherSourceGrepSecondLayer`` below) passes on a
    semantically-equivalent rewrite of the tool-name check and fails on a
    harmless comment reflow — the plan is explicit this must not be the whole
    tripwire.

    Pinned against docs/decisions/DR-258-bash-mediated-writes-are-a-named-
    permanent-limit.md (verified present at that path, § below).
    """

    def test_dr_258_decision_record_exists(self):
        assert _DR_258_PATH.is_file(), (
            f"DR-258 decision record missing at {_DR_258_PATH} — the plan "
            "cites this path as existing; if it moved, update this pin "
            "deliberately, do not silently skip"
        )

    def test_bash_tool_shaped_payload_records_no_claim(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / "bash_written.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)
        params = {
            "session_id": "deadbeefcafe0011",
            "tool_name": "Bash",
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        touched_file = (
            common_dir / "coordinator-sessions" / params["session_id"] / "touched.txt"
        )
        # DR-258: a Bash-tool-shaped call records NO claim at all — the
        # matcher fast-exits before even the session-dir bootstrap runs, so
        # touched.txt is never created.
        assert not touched_file.exists(), (
            "a Bash-tool-shaped payload recorded a claim — the DR-258 matcher "
            "widened to include Bash, which is the doctrine reversal DR-258 "
            "forecloses without a decision record + memo to claude-central-em"
        )

    @pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
    def test_edit_tool_shaped_payload_records_a_claim(self, tmp_path, tool_name):
        repo = _make_repo(tmp_path)
        (repo / "src").mkdir()
        target = repo / "src" / f"{tool_name.lower()}_written.py"
        target.write_text("y")

        common_dir = git_common_dir(repo)
        params = {
            "session_id": "deadbeefcafe0012",
            "tool_name": tool_name,
            "file_path": str(target),
        }
        asyncio.run(ttf._handler(params, repo_root=common_dir))

        touched_file = (
            common_dir / "coordinator-sessions" / params["session_id"] / "touched.txt"
        )
        assert touched_file.exists(), (
            f"a {tool_name}-shaped payload recorded no claim — the DR-258 "
            "matcher narrowed to exclude a tool it must cover"
        )
        lines = [ln for ln in touched_file.read_text(encoding="utf-8").splitlines() if ln]
        entries = [touch_scope.parse_touch_event(ln)[2] for ln in lines]
        assert f"src/{tool_name.lower()}_written.py" in entries


class TestAC6DR258MatcherSourceGrepSecondLayer:
    """Second layer only (per the plan's explicit instruction): a source-text
    grep confirming the matcher's tool-name tuple is unchanged. Kept in
    addition to, never instead of, the behavioural class above."""

    def test_handler_matcher_tuple_unchanged(self):
        text = _TRACK_TOUCHED_FILES_PATH.read_text(encoding="utf-8")
        assert 'if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):' in text, (
            "track_touched_files._handler's DR-258 matcher tuple changed — "
            "widening/narrowing this matcher needs a decision record and a "
            "memo to claude-central-em BEFORE any code (see _handler's own "
            "NAMED LIMIT docstring section)"
        )


# ---------------------------------------------------------------------------
# AC5 — allow_orphans does not widen: signature pin + mechanical call-site
# census.
#
# The census is derived by AST walk over coordinator_core/, NOT by grepping a
# hand-written list: a grep-the-known-names census reports "all known sites
# still present" and stays green when a new one appears, which discharges
# nothing. Names matched are the public name and the `_`-prefixed alias the
# ceremony sink imports it under; `_import_assert_paths_in_session_scope` (the
# lazy import helper in the guard) is a DIFFERENT function and is not matched,
# because matching is on the exact callee name, not a substring.
#
# Test modules are OUT of census scope deliberately. A test calling the helper
# with `allow_orphans=True` is exercising the flag, not widening production
# reach; including tests/ would make this pin fire on every added test case and
# train the next reader to re-baseline it reflexively — the failure mode AC5
# exists to prevent.
# ---------------------------------------------------------------------------

_SCOPE_HELPER_NAMES = frozenset(
    {"assert_paths_in_session_scope", "_assert_paths_in_session_scope"}
)

#: (repo-relative POSIX path, enclosing def qualname, `allow_orphans` argument
#: as source text). "<absent>" means the site passes no `allow_orphans` at all
#: and therefore inherits the `False` default. Verified against HEAD
#: 2026-08-05. Note what is NOT here: `coordinator_core/ops/session/
#: safe_commit_offer.py` names the helper in prose only and neither imports nor
#: calls it.
_EXPECTED_SCOPE_HELPER_CALL_SITES = frozenset(
    {
        (
            "coordinator_core/bash_guards/block_subagent_commit.py",
            "_git_commit_agent_may_commit",
            "False",
        ),
    }
)

#: (repo-relative POSIX path, enclosing def qualname, bound local name). The
#: import seam is censused alongside the calls because an import is the step a
#: prospective new caller takes first — a new importer with no call yet is a
#: widening in progress, and this pin names it while it is still cheap to
#: reverse.
_EXPECTED_SCOPE_HELPER_IMPORT_SITES = frozenset(
    {
        (
            "coordinator_core/bash_guards/block_subagent_commit.py",
            "_import_assert_paths_in_session_scope",
            "assert_paths_in_session_scope",
        ),
    }
)


def _is_census_scope(rel: str) -> bool:
    """True for a production module the census covers — a `.py` under
    coordinator_core/ that is neither a tests package member nor a `test_`
    module."""
    return not ("/tests/" in rel or "/test_" in rel)


def _walk_with_qualname(node, chain):
    """Yield every descendant of `node` paired with the dotted name of its
    enclosing def/class chain ("<module>" at top level)."""
    for child in ast.iter_child_nodes(node):
        yield child, ".".join(chain) if chain else "<module>"
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _walk_with_qualname(child, chain + [child.name])
        else:
            yield from _walk_with_qualname(child, chain)


def _census_scope_helper_sites(root: Path) -> tuple[frozenset, frozenset]:
    """AST-census `assert_paths_in_session_scope` under `root/coordinator_core`.

    Returns (call_sites, import_sites) as the frozensets of triples the two
    `_EXPECTED_*` inventories above are compared against. Derived from parsed
    syntax, so a call reached through a renamed alias is still found and a
    docstring that merely names the helper is not.
    """
    calls: set[tuple[str, str, str]] = set()
    imports: set[tuple[str, str, str]] = set()
    for path in sorted((root / "coordinator_core").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if not _is_census_scope(rel):
            continue
        try:
            # `SyntaxWarning` is muted for the parse itself: several modules
            # under census carry regex string literals that emit invalid-escape
            # warnings at compile time, and surfacing them here would attribute
            # unrelated lint noise to this pin's own test run.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node, qualname in _walk_with_qualname(tree, []):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    callee = func.id
                elif isinstance(func, ast.Attribute):
                    callee = func.attr
                else:
                    continue
                if callee not in _SCOPE_HELPER_NAMES:
                    continue
                passed = "<absent>"
                for keyword in node.keywords:
                    if keyword.arg == "allow_orphans":
                        passed = ast.unparse(keyword.value)
                calls.add((rel, qualname, passed))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _SCOPE_HELPER_NAMES:
                        imports.add((rel, qualname, alias.asname or alias.name))
    return frozenset(calls), frozenset(imports)


class TestAC5AllowOrphansDoesNotWiden:
    """Pin: `allow_orphans` stays opt-in, keyword-only, and reaches no new
    call site.

    This plan's central design (dispatch-time declared provenance) was REFUSED
    by SC-DR-021 (`A-CLAIM-IS-WHAT-YOU-WROTE-NOT-WHAT-YOU-PLANNED`). AC5 is one
    of the four *negative* pins that survived that refusal: its entire value is
    failing when a future change quietly relaxes orphan adoption — flipping the
    default to `True`, making the parameter positional so a stray third
    argument silently enables it, or adding a call site that passes `True`. A
    test that passes because it only re-checks the sites that exist today
    discharges nothing, so the census is set-equality over a mechanically
    derived inventory: an ADDED site and a REMOVED site both fail, and the
    failure names the offending site.

    AC5 explicitly rules out a "byte-unchanged" assertion on the function or
    its callers — nothing here compares source bytes; the signature claim is
    read off `inspect.signature`'s parameter KIND and the inventory off parsed
    syntax, both of which survive a comment reflow and neither of which
    survives a behavioural widening.

    Spec backlink: docs/plans/2026-08-04-ownership-inherited-at-dispatch.md
    § AC5.
    """

    def test_allow_orphans_default_is_false_and_keyword_only(self):
        params = inspect.signature(assert_paths_in_session_scope).parameters
        assert "allow_orphans" in params, (
            "assert_paths_in_session_scope lost its `allow_orphans` parameter — "
            "AC5 pins its shape; a rename/removal needs a decision record"
        )
        param = params["allow_orphans"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            "`allow_orphans` is no longer KEYWORD_ONLY (now "
            f"{param.kind.description!r}) — positional-acceptable means a caller "
            "can enable orphan adoption with a bare third/fourth argument and no "
            "reader of that call site can see it happening (SC-DR-021)"
        )
        assert param.default is False, (
            f"`allow_orphans` default is {param.default!r}, not False — orphan "
            "adoption must stay opt-in per call, never the ambient default"
        )

    def test_call_site_inventory_is_exactly_the_enumerated_set(self):
        calls, _imports = _census_scope_helper_sites(_REPO_ROOT)
        assert calls == set(_EXPECTED_SCOPE_HELPER_CALL_SITES), (
            "assert_paths_in_session_scope's call-site inventory changed — "
            f"found={sorted(calls)!r} expected="
            f"{sorted(_EXPECTED_SCOPE_HELPER_CALL_SITES)!r}. Each triple is "
            "(file, enclosing def, allow_orphans argument). A NEW call site, or "
            "an existing one whose allow_orphans argument changed, is orphan "
            "adoption widening its reach — the thing AC5 exists to catch. If the "
            "change is deliberate, update _EXPECTED_SCOPE_HELPER_CALL_SITES with "
            "a decision record, do not delete this pin."
        )

    def test_import_seam_inventory_is_exactly_the_enumerated_set(self):
        _calls, imports = _census_scope_helper_sites(_REPO_ROOT)
        assert imports == set(_EXPECTED_SCOPE_HELPER_IMPORT_SITES), (
            "assert_paths_in_session_scope's import seam changed — "
            f"found={sorted(imports)!r} expected="
            f"{sorted(_EXPECTED_SCOPE_HELPER_IMPORT_SITES)!r}. Each triple is "
            "(file, enclosing def, bound local name). A new importer is a "
            "prospective new caller."
        )

    def test_census_detects_an_added_and_a_removed_call_site(self, tmp_path):
        """The census's own detection power, on synthetic sources: it must not
        be possible for this pin to be green because the extractor found
        nothing. Runs the same extractor against a throwaway tree containing
        one call, then against the same tree with the call deleted."""
        pkg = tmp_path / "coordinator_core" / "fake_sub"
        pkg.mkdir(parents=True)
        module = pkg / "widener.py"
        module.write_text(
            "from coordinator_core.ops.session.scope_report import (\n"
            "    assert_paths_in_session_scope,\n"
            ")\n"
            "\n"
            "\n"
            "def _widened():\n"
            "    return assert_paths_in_session_scope('s', [], None, allow_orphans=True)\n",
            encoding="utf-8",
        )
        calls, imports = _census_scope_helper_sites(tmp_path)
        added = (
            "coordinator_core/fake_sub/widener.py",
            "_widened",
            "True",
        )
        assert added in calls, "census failed to see an ADDED call site"
        assert (
            "coordinator_core/fake_sub/widener.py",
            "<module>",
            "assert_paths_in_session_scope",
        ) in imports, "census failed to see an ADDED import site"
        assert calls != set(_EXPECTED_SCOPE_HELPER_CALL_SITES)

        module.write_text("x = 1\n", encoding="utf-8")
        calls_after, imports_after = _census_scope_helper_sites(tmp_path)
        assert calls_after == frozenset(), "census failed to see a REMOVED call site"
        assert imports_after == frozenset(), "census failed to see a REMOVED import site"
