"""
coordinator_core.hooks.tests.test_c4_ownership_inherited_at_dispatch_tripwires —
standing tripwires for AC5, AC6 and AC11 of
docs/plans/2026-08-04-ownership-inherited-at-dispatch.md.

The plan's central design (a dispatch-time declared-provenance set, C1/C2/C3/C3b)
was REFUSED by cross-repo ruling SC-DR-021
(DoE `coordinator/docs/wiki/scoped-safety-commits.md` @ `bedc7e0e2927`, token
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
    Pinned against DoE's `scoped-safety-commits.md` SC-DR-001 negative-spec:
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

Spec backlink: pln-ownership-inherited-at-dispatc-2a211f § C4 / AC6 / AC11.
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
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Real git is load-bearing for the AC6 behavioural tripwire: it dispatches
# real payloads at track_touched_files._handler against a real repo's
# `git_common_dir`, asserting claims land under the actual
# `.git/coordinator-sessions/<sid>/touch-record.jsonl` sink -- a mocked
# common_dir would not exercise the real session-dir bootstrap this pin
# covers.
#
# That sink was `touched.txt` until `pln-the-legacy-touched-txt-record-44ce48`
# § C7 retired the legacy record for the self-describing T-event log. Both
# tripwires below still named the retired file, which cost the Bash one its
# teeth entirely: `assert not touched.txt.exists()` holds for every tool name
# once nothing writes that file, so the DR-258 widening it exists to catch
# would have gone green.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

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
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
    return tmp_path


def _touch_record_entries(common_dir: Path, session_id: str) -> list[str]:
    """Paths recorded as `T` events for `session_id`, or `[]` when the hook
    recorded nothing. Reads through `touch_record.decode_line` rather than
    splitting the line by hand -- the encoding is that module's contract and
    a hand-rolled parser here would drift from it exactly as the retired
    `touched.txt` path did."""
    sink = common_dir / "coordinator-sessions" / session_id / "touch-record.jsonl"
    entries: list[str] = []
    # `discover_family` resolves the base sink PLUS any rotated siblings --
    # the same read contract `session/scope.py` and `session/claims.py` use.
    # Reading the literal path alone would pass these single-write fixtures
    # while silently missing a rotated record, which is the shape that let
    # the retired touched.txt assertion go vacuous.
    for member in touch_record.discover_family(sink):
        for line in member.read_text(encoding="utf-8").splitlines():
            if line:
                entries.append(touch_record.decode_line(line).path)
    return entries


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

        # DR-258: a Bash-tool-shaped call records NO claim at all — the
        # matcher fast-exits before even the session-dir bootstrap runs, so
        # the sink is never created.
        assert _touch_record_entries(common_dir, params["session_id"]) == [], (
            "a Bash-tool-shaped payload recorded a claim — the DR-258 matcher "
            "widened to include Bash, which is the doctrine reversal DR-258 "
            "forecloses without a decision record + memo to DoE-claude"
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

        entries = _touch_record_entries(common_dir, params["session_id"])
        assert entries, (
            f"a {tool_name}-shaped payload recorded no claim — the DR-258 "
            "matcher narrowed to exclude a tool it must cover"
        )
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
            "memo to DoE-claude BEFORE any code (see _handler's own "
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
#: RE-BASELINED 2026-08-31, and the reason is the whole record: the enclosing
#: def changed from `_git_commit_agent_may_commit` to
#: `_git_commit_agent_pathspec_permitted` at f864d4c716, whose own docstring
#: declares the move "PURE MOTION, no logic change" -- LEG 3's predicate
#: extracted so a caller already holding a resolved `(paths, include_orphans)`
#: pair can consult it without re-parsing a command string. Still ONE call
#: site, same file, same `allow_orphans=False`. That is a move, not the
#: widening AC5 exists to catch: no new caller gained reach and no site's
#: argument changed. The pin had been red since that extraction landed, which
#: is its own small lesson -- a tripwire nobody re-baselines is a tripwire that
#: reports nothing about the NEXT change.
_EXPECTED_SCOPE_HELPER_CALL_SITES = frozenset(
    {
        (
            "coordinator_core/bash_guards/block_subagent_commit.py",
            "_git_commit_agent_pathspec_permitted",
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

    Spec backlink: pln-ownership-inherited-at-dispatc-2a211f
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
        # This census legitimately holds at ONE caller, not two. It used to
        # pin block_subagent_commit.py (C4b, guard-side) AND
        # scoped_git_commit.py (C4c, sink-side) — the 2026-08-08 excision
        # (b56f3f3dd) dropped the C4c entry because C4c itself was removed.
        # C2 (docs/plans/2026-08-08-claim-index-the-commit-gate-never-had.md,
        # landed this session at e90cdfb217f0) replaces C4c's sink-side
        # enforcement, but it does NOT call `assert_paths_in_session_scope` —
        # it composes `claim_index.lookup()` instead (see the second census
        # below, `TestClaimIndexLookupCallSitesDoNotWiden`). A future reader
        # seeing this census at one caller after having been two must NOT
        # "fix" it by re-adding a scoped_git_commit.py entry: that would
        # re-introduce the exact predicate C2 deliberately avoided reusing.
        # The enclosing def is `_git_commit_agent_pathspec_permitted` since
        # f864d4c716's PURE-MOTION extraction — see the re-baseline note on
        # `_EXPECTED_SCOPE_HELPER_CALL_SITES`. Still ONE caller, which is what
        # the comment above is about; the move did not make it two.
        assert set(_EXPECTED_SCOPE_HELPER_CALL_SITES) == {
            (
                "coordinator_core/bash_guards/block_subagent_commit.py",
                "_git_commit_agent_pathspec_permitted",
                "False",
            ),
        }, "sanity check on the pinned set itself failed — see comment above"

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


# ---------------------------------------------------------------------------
# claim_index.lookup call-site census — the fast path C2 introduced in place
# of the excised `assert_paths_in_session_scope` sink-side call (see the
# comment inside `test_call_site_inventory_is_exactly_the_enumerated_set`
# above). The thing whose widening nobody would otherwise notice moved from
# `assert_paths_in_session_scope` to `claim_index.lookup()` — a new caller of
# `lookup()` is a new O(pathspec) ownership-gate-shaped check appearing
# somewhere else in the tree, which is exactly the kind of silent widening
# AC5's census exists to catch for the older predicate.
#
# Test files are excluded from this census deliberately, same call as AC5's
# own census docstring makes above: `test_claim_index.py` exercises `lookup`
# directly as the unit under test (dozens of calls, churns every time a test
# case is added or removed) and `test_scoped_git_commit_ownership.py` only
# NAMES `claim_index.lookup` to save/monkeypatch it, never calls it. Counting
# either would make this pin fire on ordinary test-authoring noise rather
# than on a genuine new production call site — noise trains the next reader
# to re-baseline reflexively, which is the failure mode this pin exists to
# avoid. A real new caller added under `coordinator_core/session/` itself
# (i.e. NOT `claim_index.py`'s own definition) would also be missed by this
# tests-only exclusion if it were added without a corresponding production
# module elsewhere; grep verification was run against the whole tree
# (`grep -rn "claim_index" --include='*.py' .`, verified 2026-08-08 against
# this repo's HEAD) and found exactly one non-test, non-definition call.
# ---------------------------------------------------------------------------

_CLAIM_INDEX_PATH = _REPO_ROOT / "coordinator_core" / "session" / "claim_index.py"

#: (repo-relative POSIX path, enclosing def qualname). Re-verified 2026-08-13
#: against this repo's HEAD via `grep -rn "claim_index\.lookup(" --include=
#: '*.py' coordinator_core/`, after two changes landed by
#: docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-that-
#: rejects-it.md: chunk C1 DELETED `_check_claim_conflicts` (the enforcing
#: sink-side gate this pin used to name -- path-touch claims are now
#: advisory swimlane guidance, not an enforcement primitive, PM ruling) and
#: chunk C1d ADDED `_warn_recent_edits` (a WARN-only, never-gating read of
#: `.edit_ts`) in its place.
#:
#: NARROWED 2026-08-19 to a single entry: `_warn_recent_edits` was removed
#: outright on latency grounds (its `claim_index.lookup()` cost ~50ms per
#: invocation of the engine's hottest op to produce a log line no response
#: envelope carried and no consumer read -- the same counterfactual
#: `state/kill-ledger.md` K-008 applied to `_disclose_peer_claims` and
#: `Absorbed-From:` days earlier). Its absence is pinned positively, not
#: merely by omission here, in `coordinator_core/ops/ceremony/tests/
#: test_scoped_git_commit_recent_edit_warn.py`. The sole surviving
#: `claim_index.lookup(...)` invocation outside `claim_index.py` itself and
#: outside any tests/ path is therefore `claims._clear_path_claim_if_dead`
#: (the dead-holder release path for the PATH-TOUCH claim plane, landed per
#: cross-repo/archive/2026-08-11-doe-claude-em-dead-claim-on-a-non-plan-
#: artifact-has-no-clear-path.md) -- the decision record this pin's own
#: docstring asks for before widening. A new entry re-added to the commit
#: path needs K-008's Returns-when discharged, not just this set edited.
#: Every production `claim_index.lookup(...)` call site, each entry carrying
#: why it is NOT the second ownership GATE this census exists to catch.
#: `lookup` returns raw claimants; a gate is a caller that turns that into a
#: refusal. The gate itself answers through `commit_set`/`classify_paths` and
#: is pinned separately by `_EXPECTED_CLAIM_INDEX_ANSWER_CALL_SITES`.
_EXPECTED_CLAIM_INDEX_LOOKUP_CALL_SITES = frozenset(
    {
        # Reaper: drops a dead session's claim. Writes, never refuses.
        (
            "coordinator_core/session/claims.py",
            "_clear_path_claim_if_dead._claimants",
        ),
        # Neighbour discovery: who else holds paths near mine. Fail-soft by
        # its own contract (a raising lookup degrades every path to
        # `unanswerable_paths`), and its product is an advisory list.
        (
            "coordinator_core/session/claim_neighbours.py",
            "find_neighbours_for_paths",
        ),
        # Holder-liveness evidence: does a claimed scope overlap a named
        # holder. Ternary by construction -- an unanswerable path yields
        # `None` (evidence gap), never a refusal.
        (
            "coordinator_core/session/holder_evidence.py",
            "_claim_scope_overlap",
        ),
    }
)



def _census_claim_index_call_sites(root: Path, attr: str) -> frozenset:
    """AST-census `claim_index.<attr>(...)` call sites under
    `root/coordinator_core`, excluding `claim_index.py` itself (the
    definition) and any tests/test_ path. Matches only the attribute-access
    form `claim_index.lookup(...)` — the form every known caller uses — via
    parsed syntax, not a source-text grep."""
    found: set[tuple[str, str]] = set()
    for path in sorted((root / "coordinator_core").rglob("*.py")):
        if path == _CLAIM_INDEX_PATH:
            continue
        rel = path.relative_to(root).as_posix()
        if not _is_census_scope(rel):
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node, qualname in _walk_with_qualname(tree, []):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == attr
                and isinstance(func.value, ast.Name)
                and func.value.id == "claim_index"
            ):
                found.add((rel, qualname))
    return frozenset(found)


def _census_claim_index_lookup_call_sites(root: Path) -> frozenset:
    return _census_claim_index_call_sites(root, "lookup")


#: The SECOND half of the same pin, added 2026-08-21 when the ownership leg
#: was rebuilt. `lookup` is no longer the only way to ask claim_index an
#: ownership question: `commit_set` ("what is mine to commit?") and
#: `classify_paths` ("is THIS path mine?") are two more, and a census that
#: watched only `lookup` would have let the rebuilt gate -- an
#: ownership-gate-shaped check reached from a different sink, which is
#: EXACTLY what the original pin exists to catch -- appear silently. Widened
#: deliberately rather than routed around; the entry below IS the rebuilt
#: leg, and a further one needs the same deliberate edit.
_EXPECTED_CLAIM_INDEX_ANSWER_CALL_SITES = frozenset(
    {
        (
            "classify_paths",
            "coordinator_core/ops/session/scope_report.py",
            "assert_paths_in_session_scope",
        ),
        (
            "commit_set",
            "coordinator_core/ops/session/safe_commit_offer.py",
            "compute_offer",
        ),
        (
            "commit_set",
            "coordinator_core/ops/session/safe_commit_offer.py",
            "full_ownership_map",
        ),
    }
)


class TestClaimIndexLookupCallSitesDoNotWiden:
    """Pin: `claim_index.lookup()`'s production call-site set is exactly the
    enumerated set. C2's fast path is the new thing whose silent widening
    nobody would otherwise notice — this census exists so a second caller
    appearing anywhere in `coordinator_core/` (a second ownership-gate-shaped
    check, reached from a different sink) fails loudly and names itself.

    The gate this was written against, `ops/ceremony/scoped_git_commit.py`,
    no longer exists — DR-344's kill bar deleted it, and its successor gate
    (`ops/session/scope_report.py`) answers through `commit_set`/
    `classify_paths`, so it is the sibling ANSWER pin below that guards the
    gate now. This pin's own subject narrowed accordingly: it enumerates the
    raw-claimant readers and catches a NEW one, each entry justified at
    `_EXPECTED_CLAIM_INDEX_LOOKUP_CALL_SITES`. A third test here asserted
    that the killed module still called `lookup`; it was deleted with its
    subject rather than repointed, because kill means kill (CLAUDE.md
    § brightline) and there is no file left for it to read.

    Spec backlink: pln-the-claim-index-the-commit-gat-5d33ee
    § C7.
    """

    def test_lookup_call_site_inventory_is_exactly_the_enumerated_set(self):
        found = _census_claim_index_lookup_call_sites(_REPO_ROOT)
        assert found == set(_EXPECTED_CLAIM_INDEX_LOOKUP_CALL_SITES), (
            "claim_index.lookup()'s call-site inventory changed — "
            f"found={sorted(found)!r} expected="
            f"{sorted(_EXPECTED_CLAIM_INDEX_LOOKUP_CALL_SITES)!r}. Each pair "
            "is (file, enclosing def). A NEW call site reads raw claimants "
            "from a sink none of the enumerated readers use; if it turns "
            "them into a refusal it is a second ownership gate, which needs "
            "a decision record. Update "
            "_EXPECTED_CLAIM_INDEX_LOOKUP_CALL_SITES deliberately, with the "
            "same per-entry justification the others carry; do not delete "
            "this pin."
        )

    def test_answer_surface_call_site_inventory_is_exactly_the_enumerated_set(self):
        found = set()
        for attr in ("commit_set", "classify_paths"):
            for rel, qualname in _census_claim_index_call_sites(_REPO_ROOT, attr):
                found.add((attr, rel, qualname))
        assert found == set(_EXPECTED_CLAIM_INDEX_ANSWER_CALL_SITES), (
            "claim_index's ownership-ANSWER call-site inventory changed — "
            f"found={sorted(found)!r} expected="
            f"{sorted(_EXPECTED_CLAIM_INDEX_ANSWER_CALL_SITES)!r}. Each triple "
            "is (function, file, enclosing def). `commit_set` and "
            "`classify_paths` answer the same ownership question `lookup` "
            "does, so a new caller of either is the same silent widening the "
            "sibling pin above catches — update "
            "_EXPECTED_CLAIM_INDEX_ANSWER_CALL_SITES deliberately if that is "
            "genuinely intended, with a decision record; do not delete this "
            "pin."
        )

    # test_source_grep_confirms_the_single_production_caller lived here. It
    # read the ceremony scoped-commit module and asserted that file still
    # called claim_index.lookup(...). DR-344's kill bar deleted that module
    # outright, so the test raised FileNotFoundError on every run and no edit
    # to it could pass. Deleted rather than repointed: its subject is gone,
    # and the successor gate answers through commit_set/classify_paths,
    # already pinned by the sibling test above.
