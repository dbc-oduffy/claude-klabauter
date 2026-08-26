"""test_reap_orphaned_in_flight_handoffs.py — path-scoped tests for
reap-orphaned-in-flight-handoffs.py's C13 batching fix.

No path-scoped test existed for this script at HEAD (verified: no
`coordinator/bin/tests/test_reap_orphaned_in_flight_handoffs.py` and no
`coordinator/bin/test_reap_orphaned_in_flight_handoffs.py` prior to this
chunk) -- this file is authored fresh, per the plan's own `yaml plan-tasks`
row for C13.

Scope: `_shipped_orphan_sha`'s P4 leg (terminal-SHA committer-timestamp
selection) used to spawn one `git show -s --format=%ct <sha>` PER COMMIT PER
ORPHAN inside `main()`'s per-file loop -- an N-plus-one git-spawn site. This
chunk splits P4 into three primitives:

  - `_shipped_orphan_candidate` (P2+P3, zero git spawns) -- returns the raw
    `commits[]` candidate list, or None on any P2/P3 failure.
  - `_batch_commit_timestamps` (the ONE git-spawning leg) -- resolves
    committer timestamps for a batch of candidate SHAs in a single
    `git log --no-walk --ignore-missing` call, mirroring
    `emit/sections/handoffs._resolve_shipped_in_dates`'s reconciliation
    shape exactly (prefix-match against a `matched` set -- an absent SHA is
    never read as a resolved answer).
  - `_best_shipped_sha` (P4 proper) -- pure in-memory MAX-committer-timestamp
    selection against the batched map.

`main()` now runs two passes: pass 1 walks the corpus and collects every
orphan's raw candidate SHA list (zero git spawns beyond the process-wide
`git rev-parse --show-toplevel`); ONE `_batch_commit_timestamps` call
resolves every candidate SHA across the WHOLE in-flight set; pass 2 applies
P4 + the unchanged ship/release disposition per orphan.

Anti-scope 25 (batching correctness): `--ignore-missing` silently DROPS an
unresolvable SHA from `git log`'s stdout (exit 0). This is an OBJECT
question (commit metadata at a caller-supplied SHA), never a RANGE
question, so it batches unconditionally and safely -- but a dropped SHA
must still be reconciled as unresolved, not silently treated as absent
therefore ineligible-by-default (which happens to be the correct fail-closed
disposition here, but only because `_best_shipped_sha` explicitly checks
`sha_ct.get(sha) is None` -- see `test_dropped_sha_is_reconciled_as_unresolved_not_shipped`
and `test_main_batches_across_multiple_orphans_in_one_git_log_call` below,
which assert this explicitly rather than assuming it).

Runs bash-free: `python3 -m pytest coordinator/bin/tests/test_reap_orphaned_in_flight_handoffs.py`
(or `python3 test_reap_orphaned_in_flight_handoffs.py` directly -- every
test function is a bare `assert`-based pytest-collectible function).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    """Import reap-orphaned-in-flight-handoffs.py as a fresh module object each call."""
    path = os.path.join(SCRIPT_DIR, "reap-orphaned-in-flight-handoffs.py")
    spec = importlib.util.spec_from_file_location("reap_orphaned_in_flight_handoffs_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write_handoff(handoffs_dir, name, *, status, deployment_state, consumed_by=None, kind=None,
                    deliverable_id=None, handoff_id=None):
    lines = ["---", 'title: "test handoff"', f"status: {status}", f"deployment_state: {deployment_state}"]
    if consumed_by is not None:
        lines.append(f"consumed_by: {consumed_by}")
    if kind is not None:
        lines.append(f"kind: {kind}")
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    if handoff_id is not None:
        lines.append(f"handoff_id: {handoff_id}")
    lines.append("---")
    lines.append("body")
    path = os.path.join(handoffs_dir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# ===========================================================================
# _batch_commit_timestamps -- the ONE git-spawning leg.
# ===========================================================================
def test_batch_commit_timestamps_makes_one_call_for_many_shas(monkeypatch):
    mod = _load_module()

    class FakeResult:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult(0, "sha1full 1000\nsha2full 2000\n")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    result = mod._batch_commit_timestamps(["sha1", "sha2"], "/some/repo")

    assert len(calls) == 1, f"expected exactly one git-log call, got {len(calls)}: {calls}"
    cmd = calls[0]
    assert cmd[:3] == ["git", "-C", "/some/repo"]
    assert "log" in cmd
    assert "--no-walk=unsorted" in cmd
    assert "--ignore-missing" in cmd
    assert "--format=%H %ct" in cmd
    # Object-batching, not range-batching: each SHA is fed as an independent
    # positional argv token, never joined into an A..B range expression.
    assert "sha1" in cmd and "sha2" in cmd
    assert not any(".." in tok for tok in cmd), f"no range syntax expected: {cmd}"

    assert result == {"sha1": 1000, "sha2": 2000}


def test_batch_commit_timestamps_empty_input_makes_no_call(monkeypatch):
    mod = _load_module()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise AssertionError("must not spawn for an empty SHA list")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    result = mod._batch_commit_timestamps([], "/some/repo")
    assert result == {}
    assert calls == []


def test_batch_commit_timestamps_dropped_sha_absent_from_result(monkeypatch):
    """§ anti-scope 25: --ignore-missing silently drops an unresolvable SHA
    from stdout. The dropped SHA must be reconciled as ABSENT from the
    returned map -- never defaulted to a resolved answer."""
    mod = _load_module()

    class FakeResult:
        returncode = 0
        stdout = "presentshafull 5000\n"  # "droppedsha" never appears in stdout

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: FakeResult())

    result = mod._batch_commit_timestamps(["presentsha", "droppedsha"], "/some/repo")
    assert result.get("presentsha") == 5000
    assert "droppedsha" not in result, (
        f"a SHA dropped by --ignore-missing must be absent from the map, not defaulted: {result!r}"
    )


def test_batch_commit_timestamps_git_failure_returns_empty_map(monkeypatch):
    mod = _load_module()

    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: FakeResult())
    assert mod._batch_commit_timestamps(["sha1"], "/some/repo") == {}


# ===========================================================================
# _best_shipped_sha -- pure P4 selection against the batched map.
# ===========================================================================
def test_best_shipped_sha_picks_max_committer_timestamp():
    mod = _load_module()
    sha_ct = {"a": 100, "b": 300, "c": 200}
    assert mod._best_shipped_sha(["a", "b", "c"], sha_ct) == "b"


def test_dropped_sha_is_reconciled_as_unresolved_not_shipped():
    """A candidate sha absent from sha_ct (dropped by --ignore-missing) must
    never be treated as resolved -- if it's the ONLY candidate, the result
    is "" (fail-closed), not a spurious match."""
    mod = _load_module()
    assert mod._best_shipped_sha(["droppedsha"], {}) == ""
    # Mixed: one resolved, one dropped -- the dropped one must never win by
    # virtue of being "found" in some other sense; only sha_ct entries count.
    assert mod._best_shipped_sha(["droppedsha", "presentsha"], {"presentsha": 42}) == "presentsha"


def test_best_shipped_sha_no_resolvable_sha_fails_closed():
    mod = _load_module()
    assert mod._best_shipped_sha([], {}) == ""
    assert mod._best_shipped_sha(["x", "y"], {}) == ""


# ===========================================================================
# _shipped_orphan_candidate -- P2+P3, zero git spawns.
# ===========================================================================
def test_shipped_orphan_candidate_p2_ambiguous_returns_none(tmp_path, monkeypatch=None):
    mod = _load_module()
    handoffs_dir = tmp_path / "handoffs"
    handoffs_dir.mkdir()
    h1 = _write_handoff(str(handoffs_dir), "a.md", status="consumed", deployment_state="in_flight", consumed_by="s1")
    h2 = _write_handoff(str(handoffs_dir), "b.md", status="consumed", deployment_state="in_flight", consumed_by="s1")

    mod._claim_holder = lambda path, repo_root="": "s1"

    result = mod._shipped_orphan_candidate("s1", h1, str(handoffs_dir), str(tmp_path))
    assert result is None, "P2 (more than one match) must fail closed to None"


def test_shipped_orphan_candidate_p3_zero_completions_returns_none(tmp_path):
    mod = _load_module()
    handoffs_dir = tmp_path / "handoffs"
    handoffs_dir.mkdir()
    h1 = _write_handoff(str(handoffs_dir), "a.md", status="consumed", deployment_state="in_flight", consumed_by="s1")

    mod._claim_holder = lambda path, repo_root="": "s1"
    # Empty completion index -> zero entries authored by this session.
    result = mod._shipped_orphan_candidate("s1", h1, str(handoffs_dir), str(tmp_path), {})
    assert result is None, "P3 (zero completion entries) must fail closed to None"


def test_shipped_orphan_candidate_success_returns_commits_list(tmp_path):
    mod = _load_module()
    handoffs_dir = tmp_path / "handoffs"
    handoffs_dir.mkdir()
    h1 = _write_handoff(str(handoffs_dir), "a.md", status="consumed", deployment_state="in_flight", consumed_by="s1")

    mod._claim_holder = lambda path, repo_root="": "s1"

    index = {"s1": [{"frontmatter": {"commits": ["sha1", "sha2"]}}]}

    result = mod._shipped_orphan_candidate("s1", h1, str(handoffs_dir), str(tmp_path), index)
    assert result == ["sha1", "sha2"]


# ===========================================================================
# main() -- the actual C13 fix: ONE batched git-log call across the WHOLE
# in-flight set, not one per orphan.
# ===========================================================================
def _fake_stamp_shipped_in(path, sha):
    """Mimic archive-stamp-cli's stamp-shipped-in verb well enough for the
    module's own real _fm_field reader to see it land (the TOCTOU/landed
    assertion in main() reads the file back through _fm_field, not through
    any test double)."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    out = []
    in_fm = False
    inserted = False
    for line in lines:
        if line.strip() == "---":
            if in_fm and not inserted:
                out.append(f"shipped_in: {sha}")
                inserted = True
                in_fm = False
                out.append(line)
                continue
            in_fm = not in_fm
        out.append(line)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def _patch_completion_index(mod, index):
    """Wire the one-shot completion-log index (P3's oracle) to a literal dict.

    Replaces the retired `_run_query_cli` monkeypatch: P3 no longer spawns
    `query-completions.py` twice per orphan -- it reads a single index built
    once per run, keyed by `authored_by`. Tests supply that index directly."""
    mod._resolve_completion_index = lambda: (lambda repo_root: dict(index))


def _patch_common(mod, *, repo_root, session_live_map=None, claim_holder_map=None,
                  implemented_plan_index=None):
    """Wire the module's resolver-trampoline functions to fakes so main()
    runs without touching the engine root or spawning any non-git subprocess.

    `resolve_checked_repo_root` (module-scope import from repo_identity,
    wired in by bc0d2bde5 "C3: sweep and reap scripts ride the checked
    resolver") is main()'s ONLY source of repo_root -- it is called with
    explicit_root=None and resolves off cwd's real git toplevel, not off
    this `repo_root` kwarg. Prior to bc0d2bde5 that kwarg was sufficient on
    its own; post-C3 it must also be wired onto the checked-resolver stand-in
    below, or main() silently walks pytest's OWN cwd (this repo's real
    state/handoffs/) instead of the tmp_path fixture tree, mis-attributing
    every handoff in this corpus as claim-holder-less.
    """
    session_live_map = session_live_map or {}
    claim_holder_map = claim_holder_map or {}

    mod.resolve_checked_repo_root = lambda explicit_root=None: (
        repo_root,
        {
            "verdict": "EXPLICIT",
            "session_root": None,
            "resolved_root": repo_root,
            "sid": None,
            "message": "test stand-in: resolve_checked_repo_root patched by _patch_common",
        },
    )
    mod._resolve_session_live = lambda: (lambda holder, cwd=None: session_live_map.get(holder, False))
    mod._resolve_canonical_kind = lambda: (lambda kind: kind)
    # Default: empty completion index -> every ship-check fails closed at P3,
    # which is exactly what each pre-existing release-path case below assumed
    # when it stubbed `_run_query_cli` to return "".
    _patch_completion_index(mod, {})
    # Governed-plan pre-check: default empty index -> no orphan is governed by
    # an implemented plan, so every pre-existing case below keeps its original
    # release/ship disposition unchanged.
    mod._resolve_implemented_plan_index = lambda: (
        lambda worktree: dict(implemented_plan_index or {})
    )
    mod._claim_holder = lambda path, repo_root="": claim_holder_map.get(path, "")
    mod._handoff_id_archived_twin = lambda handoff_id, repo_root: ""
    # childless -> proceed to release. `main()` resolves the whole fall-through
    # set through the batched form (ONE corpus pass); the singular wrapper
    # delegates to it and stays stubbed for direct-call tests.
    mod._has_live_children_exit_code = lambda path, repo_root=None: 1
    mod._has_live_children_exit_codes = lambda paths, repo_root=None: {p: 1 for p in paths}
    mod._run_archive_stamp_cli = lambda args: (True, "")


def test_main_batches_across_multiple_orphans_in_one_git_log_call(tmp_path, monkeypatch):
    """The C13 target: two dead-holder orphans, each with their own
    completed-ceremony commits[], must resolve committer timestamps via
    EXACTLY ONE git-log call across the whole in-flight set -- not one
    `git show` per commit per orphan (the pre-fix shape)."""
    mod = _load_module()

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    h1 = _write_handoff(str(handoffs_dir), "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")
    h2 = _write_handoff(str(handoffs_dir), "b.md", status="consumed", deployment_state="in_flight", consumed_by="dead2")

    _patch_common(
        mod,
        repo_root=str(tmp_path),
        session_live_map={"dead1": False, "dead2": False},
        claim_holder_map={h1: "dead1", h2: "dead2"},
    )

    _patch_completion_index(mod, {
        "dead1": [{"frontmatter": {"commits": ["sha-a1"]}}],
        "dead2": [{"frontmatter": {"commits": ["sha-b1", "sha-b2"]}}],
    })

    git_log_calls = []
    other_git_calls = []

    class FakeResult:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeResult(0, str(tmp_path) + "\n")
        if "log" in cmd and "--no-walk=unsorted" in cmd:
            git_log_calls.append(cmd)
            return FakeResult(
                0,
                "sha-a1-full 1000\n"
                "sha-b1-full 3000\n"
                "sha-b2-full 2000\n",
            )
        other_git_calls.append(cmd)
        return FakeResult(1, "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    stamped = []

    def fake_stamp(args):
        stamped.append(args)
        if args[0] == "stamp-shipped-in":
            _fake_stamp_shipped_in(args[1], args[3])
        return True, ""

    mod._run_archive_stamp_cli = fake_stamp

    rc = mod.main([])
    assert rc == 0

    assert len(git_log_calls) == 1, (
        f"expected exactly ONE batched git-log call across both orphans, got {len(git_log_calls)}: "
        f"{git_log_calls}"
    )
    # Every candidate SHA across BOTH orphans landed in the single call's argv.
    call = git_log_calls[0]
    for sha in ("sha-a1", "sha-b1", "sha-b2"):
        assert sha in call, f"{sha} missing from the batched call argv: {call}"

    # Zero per-orphan/per-commit git spawns for the timestamp leg.
    assert not any("show" in c and "-s" in c for c in other_git_calls), (
        f"no per-commit 'git show -s' spawns expected post-fix: {other_git_calls}"
    )

    # Both orphans reached the ship path (stamp-shipped-in + ship-handoff = 2
    # archive-stamp-cli calls each -> 4 total).
    stamp_verbs = [args[0] for args in stamped]
    assert stamp_verbs.count("stamp-shipped-in") == 2, stamp_verbs
    assert stamp_verbs.count("ship-handoff") == 2, stamp_verbs

    # dead2's best sha must be the MAX-committer-timestamp one (sha-b1 @
    # 3000), not positional-first (sha-b1 is first in commits[] here too, so
    # assert on the actual --sha argv value to pin the selection, not the
    # list order).
    b_stamp = next(args for args in stamped if args[0] == "stamp-shipped-in" and args[1] == h2)
    assert b_stamp[2] == "--sha" and b_stamp[3] == "sha-b1", b_stamp


def test_main_dropped_candidate_sha_falls_through_to_release_not_ship(tmp_path, monkeypatch):
    """§ anti-scope 25, end-to-end: an orphan whose ONLY candidate commit sha
    is silently dropped by --ignore-missing (absent from git-log stdout) must
    fall through to claim-release, never be treated as shipped."""
    mod = _load_module()

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(str(handoffs_dir), "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")

    _patch_common(
        mod,
        repo_root=str(tmp_path),
        session_live_map={"dead1": False},
        claim_holder_map={h1: "dead1"},
    )

    _patch_completion_index(mod, {"dead1": [{"frontmatter": {"commits": ["sha-vanished"]}}]})

    class FakeResult:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeResult(0, str(tmp_path) + "\n")
        if "log" in cmd and "--no-walk=unsorted" in cmd:
            # sha-vanished never appears in stdout -- --ignore-missing dropped it.
            return FakeResult(0, "")
        return FakeResult(1, "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    stamped = []
    mod._run_archive_stamp_cli = lambda args: (stamped.append(args) or (True, ""))

    rc = mod.main([])
    assert rc == 0
    stamp_verbs = [args[0] for args in stamped]
    assert "stamp-shipped-in" not in stamp_verbs, (
        f"dropped-SHA orphan must never be shipped: {stamped!r}"
    )
    assert stamp_verbs == ["unconsume-handoff"], stamp_verbs


def test_main_no_orphans_makes_zero_git_log_batch_calls(tmp_path, monkeypatch):
    """No in-flight orphans -> pending is empty -> _batch_commit_timestamps
    must not spawn at all."""
    mod = _load_module()

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(str(handoffs_dir), "active.md", status="active", deployment_state="ready_to_fire")

    _patch_common(mod, repo_root=str(tmp_path))

    git_log_calls = []

    class FakeResult:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeResult(0, str(tmp_path) + "\n")
        if "log" in cmd and "--no-walk=unsorted" in cmd:
            git_log_calls.append(cmd)
            return FakeResult(0, "")
        return FakeResult(1, "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod.main([])
    assert rc == 0
    assert git_log_calls == [], f"no orphans -> zero batch calls, got {git_log_calls}"


# ===========================================================================
# _has_live_children_exit_code -- amplification burn-down (W2/C2): now an
# in-process op call (coordinator_core.ops.handoff_children.
# _handoff_has_live_children), not a subprocess spawn of
# handoff-has-live-children.py (which itself spawned a second subprocess via
# cc_invoke.route()). Asserts zero `subprocess.run` calls and correct
# exit-code passthrough/fail-closed mapping.
# ===========================================================================
def test_has_live_children_exit_code_calls_op_in_process_zero_spawns(monkeypatch):
    mod = _load_module()

    calls = []

    async def fake_handler(candidates, repo_root=None):
        calls.append((list(candidates), repo_root))
        return {c: 0 for c in candidates}

    def fake_git_common_dir(cwd=None):
        return "/fake/common/.git"

    mod._resolve_handoff_has_live_children = lambda: (fake_handler, fake_git_common_dir)

    spawned = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: spawned.append((a, kw)))

    rc = mod._has_live_children_exit_code("/fake/state/handoffs/x.md", "/fake/worktree")

    assert rc == 0
    assert spawned == [], f"expected zero subprocess.run calls, got {spawned}"
    # ONE call for the whole set, whatever its size -- the singular wrapper is a
    # one-element case of the batched form, not a separate route.
    assert len(calls) == 1
    candidates, repo_root = calls[0]
    assert candidates == ["/fake/state/handoffs/x.md"]
    assert Path(repo_root) == Path("/fake/common/.git")


def test_has_live_children_exit_codes_batches_every_candidate_into_one_call(monkeypatch):
    """The whole fall-through set is answered by ONE corpus pass -- asking per
    orphan is what cost 36,638 _read_meta calls."""
    mod = _load_module()

    calls = []

    async def fake_handler(candidates, repo_root=None):
        calls.append(list(candidates))
        return {c: 1 for c in candidates}

    mod._resolve_handoff_has_live_children = lambda: (fake_handler, lambda cwd=None: "/fake/common/.git")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not spawn")))

    paths = [f"/fake/state/handoffs/{n}.md" for n in "abcde"]
    codes = mod._has_live_children_exit_codes(paths, "/fake/worktree")

    assert codes == {p: 1 for p in paths}
    assert len(calls) == 1, f"expected one batched call, got {len(calls)}: {calls}"
    assert calls[0] == paths


def test_has_live_children_exit_codes_fails_closed_on_handler_error(monkeypatch):
    """Any failure yields 2 (indeterminate) for every path -- never 1, which
    would read as safe-to-release."""
    mod = _load_module()

    async def boom(candidates, repo_root=None):
        raise RuntimeError("op exploded")

    mod._resolve_handoff_has_live_children = lambda: (boom, lambda cwd=None: "/fake/common/.git")

    paths = ["/fake/state/handoffs/a.md", "/fake/state/handoffs/b.md"]
    assert mod._has_live_children_exit_codes(paths, "/fake/worktree") == {p: 2 for p in paths}


def test_has_live_children_exit_code_fails_closed_on_unresolvable_common_dir():
    mod = _load_module()

    mod._resolve_handoff_has_live_children = lambda: (None, lambda cwd=None: None)

    rc = mod._has_live_children_exit_code("/fake/state/handoffs/x.md", "/fake/worktree")

    assert rc == 2


def test_has_live_children_exit_code_fails_closed_on_op_exception():
    mod = _load_module()

    async def raising_handler(params, repo_root=None):
        raise RuntimeError("boom")

    mod._resolve_handoff_has_live_children = lambda: (raising_handler, lambda cwd=None: "/fake/common/.git")

    rc = mod._has_live_children_exit_code("/fake/state/handoffs/x.md", "/fake/worktree")

    assert rc == 2


if __name__ == "__main__":
    failures = 0
    tests = [
        (name, obj) for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for name, fn in tests:
        try:
            if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                import tempfile
                from pathlib import Path
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)


# ===========================================================================
# Governed-plan pre-check -- release is the wrong verb for an orphan whose
# governing plan is already stamped `implemented`. See the module docstring's
# "Governed-plan guard" section.
# ===========================================================================
def _release_only_env(mod, tmp_path, monkeypatch):
    """Wire the git/query doubles so the ship-check (P1-P4) always fails
    closed, leaving the release fall-through as the only live path -- which
    is exactly where the governed-plan pre-check sits."""
    class FakeResult:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeResult(0, str(tmp_path) + "\n")
        return FakeResult(1, "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    _patch_completion_index(mod, {})


def test_main_skips_release_when_governing_plan_is_implemented(tmp_path, monkeypatch):
    """An orphan joining an implemented plan is SKIPPED, not released --
    releasing it would re-advertise shipped work as a live pickup target,
    and `_unclaim` refuses the write anyway."""
    mod = _load_module()

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(
        str(handoffs_dir), "shipped-plan.md", status="consumed",
        deployment_state="in_flight", consumed_by="dead1", deliverable_id="dlv-abc",
    )

    _patch_common(
        mod,
        repo_root=str(tmp_path),
        session_live_map={"dead1": False},
        claim_holder_map={h1: "dead1"},
        implemented_plan_index={
            "dlv-abc": {"path": "docs/plans/p.md", "title": "A shipped plan"}
        },
    )
    _release_only_env(mod, tmp_path, monkeypatch)

    stamped = []
    mod._run_archive_stamp_cli = lambda args: (stamped.append(args) or (True, ""))

    assert mod.main([]) == 0
    assert stamped == [], (
        f"a handoff under an implemented plan must not be written at all: {stamped!r}"
    )


def test_main_releases_when_governing_plan_is_not_implemented(tmp_path, monkeypatch):
    """Unchanged behaviour: no implemented plan carries this deliverable_id,
    so the orphan still returns to the pool."""
    mod = _load_module()

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(
        str(handoffs_dir), "live-plan.md", status="consumed",
        deployment_state="in_flight", consumed_by="dead1", deliverable_id="dlv-other",
    )

    _patch_common(
        mod,
        repo_root=str(tmp_path),
        session_live_map={"dead1": False},
        claim_holder_map={h1: "dead1"},
        implemented_plan_index={
            "dlv-abc": {"path": "docs/plans/p.md", "title": "A shipped plan"}
        },
    )
    _release_only_env(mod, tmp_path, monkeypatch)

    stamped = []
    mod._run_archive_stamp_cli = lambda args: (stamped.append(args) or (True, ""))

    assert mod.main([]) == 0
    assert [args[0] for args in stamped] == ["unconsume-handoff"], stamped


def test_main_spinoff_kind_is_exempt_from_the_governed_plan_precheck(tmp_path, monkeypatch):
    """Mirrors `_unclaim`'s own kind exemption: a `kind: spinoff` record's
    deliverable_id is an inherited id, not a true join onto the plan it
    matches, so the pre-check must not fire and the release proceeds."""
    mod = _load_module()

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(
        str(handoffs_dir), "spinoff.md", status="consumed",
        deployment_state="in_flight", consumed_by="dead1", kind="spinoff",
        deliverable_id="dlv-abc",
    )

    _patch_common(
        mod,
        repo_root=str(tmp_path),
        session_live_map={"dead1": False},
        claim_holder_map={h1: "dead1"},
        implemented_plan_index={
            "dlv-abc": {"path": "docs/plans/p.md", "title": "A shipped plan"}
        },
    )
    _release_only_env(mod, tmp_path, monkeypatch)

    stamped = []
    mod._run_archive_stamp_cli = lambda args: (stamped.append(args) or (True, ""))

    assert mod.main([]) == 0
    assert [args[0] for args in stamped] == ["unconsume-handoff"], stamped


def test_main_builds_no_corpus_indexes_when_there_are_no_orphans(tmp_path, monkeypatch):
    """Pay-for-use: the completion index (109ms) and implemented-plan index
    (78ms) each read a whole corpus, and neither is consulted unless an orphan
    reaches its gate. A clean corpus -- the steady state once the backlog is
    drained -- must not pay 187ms to answer nothing."""
    mod = _load_module()

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(str(handoffs_dir), "active.md", status="active", deployment_state="ready_to_fire")

    _patch_common(mod, repo_root=str(tmp_path))

    built = []
    mod._resolve_completion_index = lambda: (lambda repo_root: built.append("completion") or {})
    mod._resolve_implemented_plan_index = lambda: (lambda worktree: built.append("plans") or {})

    class FakeResult:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: FakeResult(0, str(tmp_path) + "\n"))

    assert mod.main([]) == 0
    assert built == [], f"no orphans -> neither corpus index should be built, got {built}"
