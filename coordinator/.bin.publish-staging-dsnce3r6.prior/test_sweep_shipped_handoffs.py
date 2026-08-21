"""test_sweep_shipped_handoffs.py — self-contained test suite for sweep-shipped-handoffs.py.

Port of: test-sweep-shipped-handoffs.sh (DoE f703efad, 2026-07-21). Retargets the bash
oracle's contract assertions onto the Python trampoline: frontmatter-driven selection (shipped /
abandoned / superseded), the shipped-subclass SHA-resolvability gate, the mtime staleness
WARNING, and dispatch shape via cc_invoke.route.

Highest-value coverage in this file is test_repo_root_derived_from_state_root_not_git_root
below — the Staff Engineer's coupling-regression finding. The bash oracle's invariant
`repo_root="${_ssh_state_root%/state}"` is NOT reproducible via an independent
`git rev-parse --show-toplevel` call once the meta-repo central-state redirect is live
(coordinator_state_root(), which resolves to the state dir under the engine root,
diverges from the git repo root). This
test simulates that divergence directly and asserts the trampoline still derives repo_root
from state_root, not from git_repo_root — the exact silent-never-archive bug the review
comment at sweep-shipped-handoffs.py:175-185 guards against.

Runs bash-free: `python3 test_sweep_shipped_handoffs.py` (or via the coordinator test runner).
Exit 0 = all tests pass; non-zero = at least one failure.

Spec backlink: pln-shipped-handoff-archive-sweep-d61d01
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md § Wave F1 (facade collapse)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import time

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.win_portability import no_console_creationflags

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_module():
    """Import sweep-shipped-handoffs.py as a fresh module object each call."""
    path = os.path.join(SCRIPT_DIR, "sweep-shipped-handoffs.py")
    spec = importlib.util.spec_from_file_location("sweep_shipped_handoffs_under_test", path)
    assert spec is not None, f"spec_from_file_location returned None for {path}"
    assert spec.loader is not None, f"spec {spec!r} has no loader"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv=None, fake_route=None):
    """Run mod.main(argv or []) with stdout/stderr captured; optionally fake
    cc_invoke.route for the duration of the call.

    Negative spec: `mod.cc_invoke` is the REAL, process-shared
    `coordinator/bin/lib/cc_invoke.py` module object — `_load_module()` gives a
    fresh subject per call but not a fresh `cc_invoke`. A bare
    `mod.cc_invoke.route = fake_route` at a test's call site therefore outlives
    the test and every later test in the same worker resolves `route` to the
    stale fake (mirrors `test_sweep_actioned_memos.py`'s save-restore). Patch
    only through here.
    """
    orig_route = mod.cc_invoke.route
    if fake_route is not None:
        mod.cc_invoke.route = fake_route
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(argv if argv is not None else [])
    finally:
        mod.cc_invoke.route = orig_route
    return rc, out.getvalue(), err.getvalue()


def _write_handoff(handoffs_dir: str, name: str, deployment_state: str | None, shipped_in: str | None = None) -> str:
    """Write a minimal frontmatter-only handoff fixture; returns the full path."""
    lines = ["---", 'title: "test handoff"']
    if deployment_state is not None:
        lines.append(f"deployment_state: {deployment_state}")
    if shipped_in is not None:
        lines.append(f"shipped_in: {shipped_in}")
    lines.append("---")
    lines.append("body")
    path = os.path.join(handoffs_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# ===========================================================================
# the Staff Engineer's coupling regression: repo_root MUST derive from state_root, not
# from an independently-resolved git repo root.
# ===========================================================================
def test_repo_root_derived_from_state_root_not_git_root():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        # Simulate the meta-repo central-state redirect: state_root lives under
        # a CLAUDE-KLABAUTER-style root that is DIFFERENT from the git checkout root.
        claude_klabauter_root = os.path.join(tmp, "central-claude-klabauter-root")
        state_root = os.path.join(claude_klabauter_root, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)

        git_repo_root = os.path.join(tmp, "git-checkout-root")
        os.makedirs(git_repo_root)

        # A shipped handoff with a resolvable-per-test-double SHA, plus an
        # abandoned one (no SHA-gate applies) so candidates is non-empty.
        _write_handoff(handoffs_dir, "a-shipped.md", "shipped", "deadbeef")
        _write_handoff(handoffs_dir, "b-abandoned.md", "abandoned")

        setattr(mod, "_resolve_repo_root", lambda: git_repo_root)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)
        setattr(mod, "_batch_resolve_shipped_shas", lambda shas, cwd: {s: True for s in shas})  # every SHA resolves

        seen = {}

        def fake_route(op, params, repo_root, legacy_fn):
            seen["op"] = op
            seen["repo_root"] = repo_root
            seen["candidate_ids"] = params.get("candidate_ids")
            return {"acted": params.get("candidate_ids", [])}

        rc, out, _err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 0, f"coupling regression: exit 0, got rc={rc}"

        expected_repo_root = os.path.dirname(state_root)  # state_root minus "/state"
        assert seen.get("repo_root") == expected_repo_root, (
            f"coupling regression: repo_root derived from state_root: expected "
            f"{expected_repo_root!r}, got {seen.get('repo_root')!r} (git_repo_root "
            f"was {git_repo_root!r})"
        )

        candidate_ids = seen.get("candidate_ids") or []
        # Always forward-slash — wire_paths.rel_id()'s convention, not os.sep
        # (2026-08-18 Windows-separator fix; see rel_candidates in main()).
        expected_rel = {
            "state/handoffs/a-shipped.md",
            "state/handoffs/b-abandoned.md",
        }
        assert set(candidate_ids) == expected_rel, (
            "coupling regression: candidate_ids relativized against "
            f"state_root-derived repo_root: expected {expected_rel!r}, got "
            f"{candidate_ids!r}"
        )

        assert seen.get("op") == "fleet.archive_completed_handoffs", (
            f"dispatch: op == fleet.archive_completed_handoffs, got {seen.get('op')!r}"
        )

        assert "2 terminal handoffs archived" in out, (
            f"coupling regression: both candidates reported archived: stdout: {out!r}"
        )


# ===========================================================================
# Frontmatter selection: deployment_state classes, SHA-resolvability gate,
# mtime staleness threshold — all in one mixed-batch sweep.
# ===========================================================================
def test_frontmatter_selection_and_staleness():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        state_root = os.path.join(tmp, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)

        # a: shipped, resolvable -> candidate
        _write_handoff(handoffs_dir, "a-shipped-ok.md", "shipped", "goodsha1")
        # b: shipped, unresolvable, mtime recent -> retained, NOT stale
        _write_handoff(handoffs_dir, "b-shipped-unresolvable-fresh.md", "shipped", "badsha1")
        # c: shipped, unresolvable, mtime old -> retained AND stale
        c_path = _write_handoff(handoffs_dir, "c-shipped-unresolvable-stale.md", "shipped", "badsha2")
        old_time = time.time() - (20 * 86400)
        os.utime(c_path, (old_time, old_time))
        # d: abandoned -> candidate (no SHA-gate)
        _write_handoff(handoffs_dir, "d-abandoned.md", "abandoned")
        # e: closed -> candidate (no SHA-gate). NOT "superseded" -- that lives
        # on the `status` axis (HANDOFF_TERMINAL_STATUS), deliberately absent
        # from the `deployment_state`-keyed HANDOFF_TERMINAL_DEPLOYMENT
        # selector this sweep reads (see this file's module docstring); a
        # `deployment_state: superseded` fixture here would assert a shape
        # the product code intentionally does not treat as a candidate.
        _write_handoff(handoffs_dir, "e-closed.md", "closed")
        # f: active -> skipped entirely, never a candidate
        _write_handoff(handoffs_dir, "f-active.md", "active")

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)

        def fake_batch_resolve(shas, cwd):
            return {sha: sha == "goodsha1" for sha in shas}

        setattr(mod, "_batch_resolve_shipped_shas", fake_batch_resolve)

        seen = {}

        def fake_route(op, params, repo_root, legacy_fn):
            seen["candidate_ids"] = params.get("candidate_ids")
            return {"acted": params.get("candidate_ids", [])}

        rc, out, _err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 0, f"frontmatter selection: exit 0, got rc={rc}"

        candidate_ids = set(seen.get("candidate_ids") or [])
        # Always forward-slash — wire_paths.rel_id()'s convention, not os.sep
        # (2026-08-18 Windows-separator fix; see rel_candidates in main()).
        expected_candidates = {
            "state/handoffs/a-shipped-ok.md",
            "state/handoffs/d-abandoned.md",
            "state/handoffs/e-closed.md",
        }
        assert candidate_ids == expected_candidates, (
            "frontmatter selection: shipped-resolvable + abandoned + closed "
            f"selected: expected {expected_candidates!r}, got {candidate_ids!r}"
        )

        assert "3 terminal handoffs archived" in out, (
            f"frontmatter selection: '3 terminal handoffs archived': stdout: {out!r}"
        )

        assert "2 shipped handoffs retained (unresolvable SHA)" in out, (
            f"frontmatter selection: 2 unresolvable shipped retained: stdout: {out!r}"
        )

        assert "WARNING: 1 shipped handoffs retained" in out, (
            f"frontmatter selection: WARNING for the 1 stale-unresolvable: stdout: {out!r}"
        )


# ===========================================================================
# Empty tree: no candidates -> route() never called, "no terminal handoffs archived"
# ===========================================================================
def test_empty_tree_no_dispatch():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        state_root = os.path.join(tmp, "state")
        os.makedirs(os.path.join(state_root, "handoffs"))

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)

        called = {"n": 0}

        def fake_route(op, params, repo_root, legacy_fn):
            called["n"] += 1
            return {"acted": []}

        rc, out, _err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 0, f"empty tree: exit 0, got rc={rc}"

        assert called["n"] == 0, f"empty tree: route() never called, called {called['n']} times"

        assert "no terminal handoffs archived" in out, (
            f"empty tree: 'no terminal handoffs archived': stdout: {out!r}"
        )


# ===========================================================================
# AC13 (§ S12 site (a), docs/plans/2026-07-28-handoff-close-path-fail-loud.md
# chunk C4): route() raises RuntimeError (transport failure) -> candidates
# retained, WARN on stderr, exit 1 — a dispatch FAILURE, not a normal
# best-effort outcome. Renamed from the pre-C4 "exit 0 (best-effort
# ceremony)" framing this test's own name and comment still carried; that
# rc==0 assertion described exactly the "refusal indistinguishable from
# success at the caller's rc" defect this chunk exists to close. Real
# `assert`s (not the file's own non-raising _pass/_fail counters) so a
# regression here actually fails the pytest run.
# ===========================================================================
def test_route_runtime_error_is_a_dispatch_failure_not_success():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        state_root = os.path.join(tmp, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)
        _write_handoff(handoffs_dir, "a-abandoned.md", "abandoned")

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)

        def fake_route(op, params, repo_root, legacy_fn):
            raise RuntimeError("simulated transport failure")

        rc, out, err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 1, f"a caught dispatch RuntimeError must return non-zero (AC13), got rc={rc}"
        assert "no terminal handoffs archived" in out, out
        assert "candidates retained" in err, err


# ===========================================================================
# AC13: route() returns exit_code=2 (op-level partial refusal) -> true acted
# count printed + WARN on stderr + non-zero exit (not a raise; route() only
# raises on transport failure — op-level refusals live INSIDE the returned
# dict, but a partial dispatch is still a dispatch FAILURE at this script's
# own rc, not a normal outcome). Renamed from test_route_partial_exit_code_
# warns, whose rc==0 assertion pre-dated the C4/AC13 fix.
# ===========================================================================
def test_route_partial_exit_code_is_a_dispatch_failure_not_success():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        state_root = os.path.join(tmp, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)
        _write_handoff(handoffs_dir, "a-abandoned.md", "abandoned")
        _write_handoff(handoffs_dir, "b-superseded.md", "superseded")

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)

        def fake_route(op, params, repo_root, legacy_fn):
            candidate_ids = params.get("candidate_ids", [])
            return {"exit_code": 2, "acted": candidate_ids[:1], "failed": candidate_ids[1:]}

        rc, out, err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 1, f"an op-reported exit_code==2 (partial) must return non-zero (AC13), got rc={rc}"
        assert "1 terminal handoffs archived" in out, out
        assert "WARN" in err and "partial" in err, err


# ===========================================================================
# AC13 companion: the three LEGITIMATE-zero shapes must stay 0 after the
# above fix — a patch that turns every non-full-success into non-zero would
# swap a silent-failure bug for a noisy-success bug on the retention path.
# empty-tree is already covered by test_empty_tree_no_dispatch above;
# stale-unresolvable is covered by test_frontmatter_selection_and_staleness
# (mixed candidates + retention, dispatch succeeds). This test isolates the
# all-retained shape on its own: every candidate is gated out before
# route() is ever called, so there is nothing to dispatch and nothing to
# fail — a legitimate 0, not a masked failure.
# ===========================================================================
def test_all_retained_no_candidates_still_returns_zero():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        state_root = os.path.join(tmp, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)
        # Both shipped, both unresolvable, neither stale — every candidate
        # is retained before candidates ever reaches route(), so route()
        # must never be called.
        _write_handoff(handoffs_dir, "a-shipped-unresolvable.md", "shipped", "badsha1")
        _write_handoff(handoffs_dir, "b-shipped-unresolvable.md", "shipped", "badsha2")

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)
        setattr(mod, "_batch_resolve_shipped_shas", lambda shas, cwd: {s: False for s in shas})  # nothing resolves

        called = {"n": 0}

        def fake_route(op, params, repo_root, legacy_fn):
            called["n"] += 1
            return {"acted": []}

        rc, out, _err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 0, f"all-retained (no dispatch-eligible candidates) must stay 0, got rc={rc}"
        assert called["n"] == 0, "route() must never be called when every candidate is retained"
        assert "2 shipped handoffs retained (unresolvable SHA)" in out, out


# ===========================================================================
# C9/AC15 — unresolvable-shipped_in escape hatch (Route B, corrected shape).
#
# Real `assert`s below (not the file's own _pass/_fail counters, which pytest
# never inspects) so a regression here actually fails the pytest run — see
# docs/plans/2026-07-28-handoff-close-path-fail-loud.md § C9 for the shape
# this pins: shipped_in_kind is NEVER rewritten by the escape; the dead SHA
# is recorded in the separate, non-discriminant shipped_in_unresolvable_sha
# field instead.
# ===========================================================================


def _read_field(path: str, key: str) -> str | None:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    split = split_frontmatter(text)
    assert split is not None, f"no parseable frontmatter in {path}"
    return read_fm_field_unquoted(split.fm_text, key)


def test_c9_unresolvable_escape_leaves_shipped_in_kind_untouched():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["git", "init", "-q", tmp],
            check=True,
            **no_console_creationflags(),
        )

        state_root = os.path.join(tmp, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)

        dead_sha = "deadbee1"
        path = os.path.join(handoffs_dir, "shipped-dead.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "---\n"
                'title: "test handoff"\n'
                "deployment_state: shipped\n"
                f"shipped_in: {dead_sha}\n"
                "shipped_in_kind: ship-commit\n"
                "---\n"
                "body\n"
            )
        old_time = time.time() - (20 * 86400)  # past the default 14-day threshold
        os.utime(path, (old_time, old_time))

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)
        # _batch_resolve_shipped_shas runs for real here (tmp is a real,
        # commit-less repo, so `deadbee1` never resolves) -- exercising the
        # actual batch resolvability gate, not a stand-in.

        seen = {}

        def fake_route(op, params, repo_root, legacy_fn):
            seen["candidate_ids"] = params.get("candidate_ids")
            return {"acted": params.get("candidate_ids", [])}

        rc, out, _err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 0, f"expected exit 0, got {rc}"
        assert "1 shipped handoffs escaped via unresolvable-shipped_in marker" in out, out
        candidate_ids = seen.get("candidate_ids") or []
        # Always forward-slash — wire_paths.rel_id()'s convention, not os.sep
        # (2026-08-18 Windows-separator fix; see rel_candidates in main()).
        assert "state/handoffs/shipped-dead.md" in candidate_ids, candidate_ids
        assert "retained" not in out, (
            f"escaped handoff must not also be reported as retained: {out!r}"
        )

        assert _read_field(path, "shipped_in_kind") == "ship-commit", (
            "shipped_in_kind must be UNCHANGED by the escape -- never rewritten to no-commit"
        )
        assert _read_field(path, "shipped_in") == dead_sha, (
            "shipped_in itself is untouched by the escape path"
        )
        assert _read_field(path, "shipped_in_unresolvable_sha") == dead_sha, (
            "the dead SHA must land in the separate non-discriminant field"
        )

        # Idempotency: re-stamping the same dead_sha is a byte-identical no-op.
        before = open(path, "r", encoding="utf-8").read()
        applied = mod._stamp_unresolvable_escape(path, tmp, dead_sha)
        after = open(path, "r", encoding="utf-8").read()
        assert applied is True
        assert before == after, "re-stamping the same dead SHA must not rewrite the file"


# ===========================================================================
# C11 — sweep-shipped-handoffs adopts _batch_check_hex_tokens for the
# shipped-subclass SHA-resolvability gate instead of one `cat-file -e` spawn
# per handoff.
# ===========================================================================
def test_batch_resolve_shipped_shas_makes_one_batch_check_call_for_many_shas():
    """N distinct shipped_in SHAs must resolve via exactly ONE
    `_batch_check_hex_tokens` call, not N -- the C11 amplification fix."""
    mod = _load_module()

    calls = []

    def fake_batch_check(tokens, cwd):
        calls.append(list(tokens))
        # every peeled token "resolves" except the one for "deadsha"
        return {t: (None if t.startswith("deadsha") else "f" * 40) for t in tokens}

    import coordinator_core.coverage as coverage_mod

    orig = coverage_mod._batch_check_hex_tokens
    coverage_mod._batch_check_hex_tokens = fake_batch_check
    try:
        shas = {"goodsha1", "goodsha2", "goodsha3", "deadsha1"}
        resolved = mod._batch_resolve_shipped_shas(shas, "/some/repo")
    finally:
        coverage_mod._batch_check_hex_tokens = orig

    assert len(calls) == 1, f"expected exactly one batch-check call, got {len(calls)}: {calls}"
    assert len(calls[0]) == 4, f"all four distinct SHAs fed into the single call: {calls[0]}"
    assert all(tok.endswith("^{commit}") for tok in calls[0]), (
        f"each token must be peeled to commit ('<sha>^{{commit}}'): {calls[0]}"
    )
    assert resolved == {
        "goodsha1": True,
        "goodsha2": True,
        "goodsha3": True,
        "deadsha1": False,
    }, resolved


def test_batch_resolve_shipped_shas_treats_absent_token_as_unresolved():
    """§ anti-scope 25: a token silently DROPPED from the batch-check return
    (never explicitly mapped to None) must be reconciled as unresolved --
    fail-closed, absence is never read as a resolved answer."""
    mod = _load_module()

    import coordinator_core.coverage as coverage_mod

    def fake_batch_check_drops_one(tokens, cwd):
        # Simulate a dropped/omitted entry: return a dict missing one of the
        # requested tokens entirely (not even mapped to None).
        return {tok: "f" * 40 for tok in tokens if not tok.startswith("droppedsha")}

    orig = coverage_mod._batch_check_hex_tokens
    coverage_mod._batch_check_hex_tokens = fake_batch_check_drops_one
    try:
        resolved = mod._batch_resolve_shipped_shas({"presentsha", "droppedsha"}, "/some/repo")
    finally:
        coverage_mod._batch_check_hex_tokens = orig

    assert resolved["presentsha"] is True
    assert resolved["droppedsha"] is False, (
        "a token absent from the batch-check return must reconcile to unresolved, "
        f"got {resolved!r}"
    )


def test_batch_resolve_shipped_shas_empty_input_makes_no_call():
    mod = _load_module()

    import coordinator_core.coverage as coverage_mod

    calls = []

    def fake_batch_check(tokens, cwd):
        calls.append(tokens)
        return {}

    orig = coverage_mod._batch_check_hex_tokens
    coverage_mod._batch_check_hex_tokens = fake_batch_check
    try:
        resolved = mod._batch_resolve_shipped_shas(set(), "/some/repo")
    finally:
        coverage_mod._batch_check_hex_tokens = orig

    assert resolved == {}
    assert calls == [], "an empty SHA set must not spawn a batch-check call at all"


def test_c9_unresolvable_escape_stamp_failure_falls_back_to_retained():
    """A stamp failure (here: not inside a git repo) must fall back to the
    pre-C9 retained/stale-warning behavior, never crash the sweep or silently
    drop the handoff from either count."""
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        # Deliberately NOT a git repo -- locked_rmw's git_common_dir()
        # resolution fails, forcing _stamp_unresolvable_escape to return False.
        state_root = os.path.join(tmp, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)

        dead_sha = "deadbee2"
        path = _write_handoff(handoffs_dir, "shipped-dead-nogit.md", "shipped", dead_sha)
        old_time = time.time() - (20 * 86400)
        os.utime(path, (old_time, old_time))

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)

        seen = {}

        def fake_route(op, params, repo_root, legacy_fn):
            seen["candidate_ids"] = params.get("candidate_ids")
            return {"acted": params.get("candidate_ids", [])}

        rc, out, _err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 0, f"expected exit 0 (guard retention, not a dispatch failure), got {rc}"
        assert "1 shipped handoffs retained (unresolvable SHA)" in out, out
        assert "WARNING: 1 shipped handoffs retained" in out, out
        assert "escaped" not in out
        assert (seen.get("candidate_ids") or []) == []


# ===========================================================================
# Windows-separator portability tripwire (dispatch brief, 2026-08-18):
# rel_candidates built at main()'s dispatch site must always carry forward
# slashes, matching wire_paths.rel_id()'s ALWAYS-forward-slash convention
# that fleet.archive_completed_handoffs' live_handoffs map is keyed by.
# handoffs_dir/candidate paths here are built via the real os.path.join, so
# on a Windows CI/dev host this exercises the genuine os.sep == "\\" shape
# natively (no ntpath-monkeypatching needed for that host) — the assertion
# itself has no host-OS branch, so a POSIX host merely finds nothing to
# normalize and still passes; only Windows exercises the actual regression.
# ===========================================================================
def test_rel_candidates_are_always_forward_slash():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        state_root = os.path.join(tmp, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)
        _write_handoff(handoffs_dir, "a-abandoned.md", "abandoned")

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)

        seen = {}

        def fake_route(op, params, repo_root, legacy_fn):
            seen["candidate_ids"] = params.get("candidate_ids")
            return {"acted": params.get("candidate_ids", [])}

        rc, _out, _err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 0, f"expected exit 0, got {rc}"
        candidate_ids = seen.get("candidate_ids") or []
        assert candidate_ids, "expected exactly one dispatched candidate_id"
        for cid in candidate_ids:
            assert "\\" not in cid, f"candidate_id must be forward-slash only, got {cid!r}"
            assert "/" in cid, f"candidate_id must be repo-relative with a separator, got {cid!r}"


# ===========================================================================
# Companion to the above: a dispatch where candidates went in non-empty but
# acted came back empty (every skip reason "already-archived") must WARN on
# stderr, not print an indistinguishable "no terminal handoffs archived" —
# this is the observable symptom the Windows-separator bug produced for
# days before the boundary/op-side fixes above.
# ===========================================================================
def test_nonempty_candidates_all_already_archived_warns_not_silent():
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        state_root = os.path.join(tmp, "state")
        handoffs_dir = os.path.join(state_root, "handoffs")
        os.makedirs(handoffs_dir)
        _write_handoff(handoffs_dir, "a-abandoned.md", "abandoned")

        setattr(mod, "_resolve_repo_root", lambda: tmp)
        setattr(mod, "_resolve_state_root", lambda repo_root: state_root)

        def fake_route(op, params, repo_root, legacy_fn):
            candidate_ids = params.get("candidate_ids", [])
            return {
                "acted": [],
                "skipped": [{"id": cid, "reason": "already-archived"} for cid in candidate_ids],
            }

        rc, out, err = _run_main_capturing(mod, fake_route=fake_route)

        assert rc == 0, out
        assert "no terminal handoffs archived" in out, out
        assert "WARN" in err and "already-archived" in err, err




# ===========================================================================
# sedge-17 — advisory referent dangle report over realized_by/gate_cleared_by.
# Report-only by contract: no write, no stamp, no exit-code influence, and it
# rides the SAME single batch-check spawn as the shipped_in gate.
# ===========================================================================
def test_extract_referent_tokens_reads_scalar_and_inline_flow():
    """The research corpus's own counter handled block-sequence but not
    inline-flow YAML, which is exactly the shape `gate_cleared_by` carries in
    this repo -- both forms must yield their tokens here."""
    mod = _load_module()

    assert mod._extract_referent_tokens("abc1234") == ["abc1234"]
    assert mod._extract_referent_tokens("'abc1234'") == ["abc1234"]
    assert mod._extract_referent_tokens("['abc1234', 'def5678']") == ["abc1234", "def5678"]
    assert mod._extract_referent_tokens("") == []
    assert mod._extract_referent_tokens("null") == []
    assert mod._extract_referent_tokens("[]") == []


def test_referent_fields_are_opt_in_never_hex_pattern_matched():
    """Field-opt-in by construction: a hex-shaped value in a NON-member field
    (execution_authorized_sha is a `git hash-object` without -w, never in the
    ODB) must never be picked up as a commit referent."""
    mod = _load_module()

    assert mod._REFERENT_ADVISORY_FIELDS == ("realized_by", "gate_cleared_by")
    assert "execution_authorized_sha" not in mod._REFERENT_ADVISORY_FIELDS
    assert "shipped_in" not in mod._REFERENT_ADVISORY_FIELDS

    fields = {"execution_authorized_sha": "dead1234", "realized_by": "beef5678"}
    dangling = mod._report_dangling_referents([("h.md", fields)], {"beef5678": True})
    assert dangling == 0, "a resolving realized_by must not report, and the non-member field is invisible"


def test_dangling_referent_is_reported_but_nothing_is_stamped(capsys):
    """AC1: report-only. A dead realized_by prints an advisory and leaves the
    file byte-identical -- unlike the shipped_in path, which stamps an escape."""
    mod = _load_module()

    with tempfile.TemporaryDirectory() as tmp:
        handoffs_dir = os.path.join(tmp, "handoffs")
        os.makedirs(handoffs_dir)
        path = os.path.join(handoffs_dir, "h.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: t\nrealized_by: dead1234\n---\nbody\n")
        before = open(path, "rb").read()

        fields = mod._parse_frontmatter(path)
        dangling = mod._report_dangling_referents([(path, fields)], {"dead1234": False})

        assert dangling == 1
        out = capsys.readouterr().out
        assert "advisory" in out and "dead1234" in out and "realized_by" in out, out
        assert open(path, "rb").read() == before, "report-only: the file must not be rewritten"
