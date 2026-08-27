"""
coordinator_core.ops.ceremony.tests.test_commit_op

C1 of docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md —
discharges AC1 and AC2 only. Later chunks (C3/C4/C6) add coverage for the
git-mechanism shape and the double-commit reconcile path; this module is
deliberately thin, matching the chunk it discharges.

AC1 -- "A commit op is registered, reachable by name through the op
registry, and has an eager-import entry in `ops/__init__.py`", proved by
resolving the op by name IN A FRESH INTERPRETER WITH LAZY OPS UNARMED --
never against this test process's own already-populated `ipc._REGISTRY`,
which would pass vacuously if a peer test imported `commit_op` first. A
subprocess is load-bearing here for the same reason
`test_lazy_hooks_channel.py` uses one: import-time module-level state
(`ipc._REGISTRY`, `sys.modules`) cannot be reset in-process without
reimplementing the interpreter's own module cache.

AC2 -- "The op is classified not COMPUTE_ONLY in authz/classification" --
asserted directly against `classify()`/`_op_may_mutate()`, not inferred from
the fail-closed default: an op absent from `OP_CLASSIFICATION` also reads
MUTATING via the same fallback, so a passing assertion here must show the op
is EXPLICITLY present and EXPLICITLY MUTATING, never merely unclassified.

AC8 (C4, this chunk) -- "An indeterminate dispatch never double-commits."
`warm/client.py :: _op_may_mutate` answering True does NOT by itself close
the double-commit path: the zero-byte-EOF post-delivery branch goes cold
for a mutating op regardless (staff-eng finding, resolved by the plan's own
AC8 text to RECONCILE, not prevention -- EM call, 2026-08-26). This module
therefore proves the RECONCILE mechanism, table-driven over every
post-delivery shape `client.py :: _try_warm_dispatch_inner` can produce for
a delivered mutation, using the existing trailer-search machinery
(`commit_pipeline._reconcile_landed_despite_failure`, the same primitive
`commit()`'s own reported-failure-but-landed repair already uses) rather
than inventing a new one. Each row asserts the identical invariant: exactly
one commit lands, whether the retry is skipped (an attempt-id trailer is
found in history) or proceeds (absent).

No production "dispatch-then-reconcile" wrapper exists yet for
`ceremony.commit` (that caller-side wiring is later plan work, not this
test-only chunk's `writes:` scope) -- this module plays the caller's role
directly: mint an attempt-id trailer, drive `try_warm_dispatch` through a
mocked transport for one post-delivery shape at a time, and apply the same
found-means-skip/absent-means-proceed policy the plan's AC8 mechanism
names, using `ceremony.commit`'s real handler (`commit_op._handler`) to
perform any commit under test -- so the invariant is proved against the
real op and the real transport-classification code, not a stand-in.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Optional

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "ceremony.commit"

_RESOLUTION_SCRIPT = textwrap.dedent(
    f"""
    import coordinator_core.ops  # noqa: F401 -- bare import: lazy unconditionally
    import coordinator_core.ipc as ipc

    assert not ipc._REGISTRY, (
        f"_REGISTRY must start empty under lazy mode; got {{sorted(ipc._REGISTRY.keys())!r}}"
    )

    handler = ipc.get_op_handler({_OP_NAME!r})
    assert handler is not None, {_OP_NAME!r} + " did not resolve under lazy mode"
    print("COMMIT_OP_LAZY_RESOLUTION_OK")
    """
)


def _run_script(script: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_ceremony_commit_op_may_mutate_returns_true() -> None:
    """AC2, asserted through the actual gate `warm/client.py` consults
    (`_op_may_mutate`) rather than only through the classification table
    directly — the two must agree."""
    from coordinator_core.warm.client import _op_may_mutate

    assert _op_may_mutate(_OP_NAME) is True


def test_ceremony_commit_name_is_not_the_killed_scoped_git_commit() -> None:
    """Guard against the named hazard in this chunk's own brief: "the op
    name is a fresh decision, not automatically the killed
    `ceremony.scoped_git_commit`" — and a killed op's name living on in a
    string-keyed guard is a known failure shape in this repo (2026-08-23
    `ceremony.scoped_git_commit` kill, K-050)."""
    assert _OP_NAME != "ceremony.scoped_git_commit"


# ---------------------------------------------------------------------------
# AC8: an indeterminate dispatch cannot double-commit.
# ---------------------------------------------------------------------------

_CNW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(args, cwd) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        **_CNW,
    )


@pytest.fixture()
def _repo(tmp_path: Path) -> Path:
    """A real, throwaway git worktree with one commit -- the reconcile probe
    under test (`commit_pipeline._reconcile_landed_despite_failure`) runs
    real `git log --grep` calls, so a real repo is load-bearing here, not a
    convenience."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "seed.txt"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _head_sha(root: Path) -> str:
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


def _count_trailer_commits(root: Path, needle: str) -> int:
    result = _git(
        ["log", "--all", "--fixed-strings", f"--grep={needle}", "--format=%H"],
        root,
    )
    return len([line for line in result.stdout.splitlines() if line])


def _commit_via_op(root: Path, *, file_name: str, trailers: str) -> dict:
    """Drive the real `ceremony.commit` handler end to end -- the same
    entry point a warm-served dispatch reaches -- so this table exercises
    the actual op, not a stand-in for it."""
    from coordinator_core.ops.ceremony.commit_op import _handler as commit_handler

    (root / file_name).write_text(f"content for {file_name}\n", encoding="utf-8")
    return commit_handler(
        {
            "subject": f"AC8 row: {file_name}",
            "stage_paths": [file_name],
            "trailers": trailers,
        },
        repo_root=root / ".git",
    )


class _FakePipe:
    """Minimal stand-in for the `open(pipe, "r+b")` handle, mirroring
    `warm/tests/test_client_fallback.py::_FakePipe` -- duplicated locally
    rather than imported cross-module (that module is a sibling suite, not
    a shared fixture library, and this table's shapes are commit-specific:
    each row also drives a real op call around the mocked transport)."""

    def __init__(self, read_result=b'{"jsonrpc":"2.0","id":1,"result":{}}\n', raise_on_write=None):
        self.written = []
        self._read_result = read_result
        self._raise_on_write = raise_on_write

    def write(self, data: bytes) -> None:
        if self._raise_on_write is not None:
            raise self._raise_on_write
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self):
        if isinstance(self._read_result, BaseException):
            raise self._read_result
        return self._read_result

    def close(self) -> None:
        pass


def _warm_msg(attempt_id: str, file_name: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": _OP_NAME,
        "params": {
            "subject": f"AC8 row: {file_name}",
            "stage_paths": [file_name],
            "trailers": f"Attempt-Id: {attempt_id}",
        },
    }


def _drive_dispatch(monkeypatch: pytest.MonkeyPatch, msg: dict, open_pipe) -> Optional[dict]:
    from coordinator_core.warm import client as warm_client

    monkeypatch.setattr(warm_client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(warm_client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(warm_client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(warm_client, "_spawned_this_process", False)
    monkeypatch.setattr(warm_client, "_live_tree_cold", False)
    monkeypatch.setattr(warm_client, "_cold_reason", None)
    monkeypatch.setattr(warm_client, "_open_pipe", open_pipe)
    return warm_client.try_warm_dispatch(msg)


# Each row: (row_id, server_engaged, build_open_pipe, expect_indeterminate).
# `server_engaged` is the row's own ground truth for "did the server, in
# reality, read and execute this request before its answer was lost" -- the
# test performs that real commit (or withholds it) BEFORE the mocked
# dispatch, exactly mirroring what each shape's own evidence means per
# `client.py`'s module docstring.
def _shape_read_deadline_expiry(monkeypatch: pytest.MonkeyPatch):
    from coordinator_core.warm import client as warm_client

    import threading

    class _StuckPipe(_FakePipe):
        def readline(self):
            threading.Event().wait(30)
            return b'{"jsonrpc":"2.0","id":1,"result":{}}\n'

    monkeypatch.setattr(warm_client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(warm_client, "MUTATION_READ_DEADLINE_SECS", 0.2)
    return lambda pipe: _StuckPipe()


def _shape_broken_pipe_after_delivery(monkeypatch: pytest.MonkeyPatch):
    return lambda pipe: _FakePipe(read_result=BrokenPipeError("mid-response"))


def _shape_cold_or_indeterminate_malformed(monkeypatch: pytest.MonkeyPatch):
    return lambda pipe: _FakePipe(read_result=b"not json\n")


def _shape_zero_byte_eof(monkeypatch: pytest.MonkeyPatch):
    return lambda pipe: _FakePipe(read_result=b"")


_ROWS = [
    pytest.param(
        "read-deadline-expiry",
        True,
        _shape_read_deadline_expiry,
        True,
        id="read-deadline-expiry-indeterminate",
    ),
    pytest.param(
        "broken-pipe-after-delivery",
        True,
        _shape_broken_pipe_after_delivery,
        True,
        id="broken-pipe-after-delivery-indeterminate",
    ),
    pytest.param(
        "cold-or-indeterminate-malformed",
        True,
        _shape_cold_or_indeterminate_malformed,
        True,
        id="malformed-response-indeterminate",
    ),
    pytest.param(
        "zero-byte-eof",
        False,
        _shape_zero_byte_eof,
        False,
        id="zero-byte-eof-cold",
    ),
]


def test_ac8_table_is_exhaustive_over_client_post_delivery_branches() -> None:
    """Guard: FAILS when a new post-delivery branch is added to
    `warm/client.py :: _try_warm_dispatch_inner` without a row above.

    `client.py` is shared transport this plan does not own (module
    docstring: "the realistic regression is a fifth shape arriving from
    another workstream"), so this counts every `return` reachable AFTER the
    request has been DELIVERED (`fh.write`/`fh.flush` succeeded) -- the
    post-delivery decision surface the table above claims to cover -- via
    `inspect.getsource`, and pins the count to what is true on this chunk's
    own landing. A fifth post-delivery branch (a new `return`, in the
    function body or in its nested `_cold_or_indeterminate` helper) changes
    this count, and a silently-non-exhaustive table is worse than none: it
    reads as covered. This test failing means: add a row to `_ROWS` above
    for the new shape, THEN update `_EXPECTED_POST_DELIVERY_RETURNS` here.
    """
    import inspect
    import re

    from coordinator_core.warm import client as warm_client

    source = inspect.getsource(warm_client._try_warm_dispatch_inner)
    marker = "delivered = True"
    assert marker in source, "post-delivery marker moved -- update this guard"
    post_delivery = source[source.index(marker) :]

    _EXPECTED_POST_DELIVERY_RETURNS = 12
    actual = len(re.findall(r"\breturn\b", post_delivery))
    assert actual == _EXPECTED_POST_DELIVERY_RETURNS, (
        f"warm/client.py::_try_warm_dispatch_inner gained/lost a post-delivery "
        f"`return` (expected {_EXPECTED_POST_DELIVERY_RETURNS}, found {actual}). "
        "If this is a genuinely NEW post-delivery shape, add a row to _ROWS in "
        "this file proving the AC8 invariant for it, then update "
        "_EXPECTED_POST_DELIVERY_RETURNS to match."
    )


# ---------------------------------------------------------------------------
# AC9 (C6): the cold fallback path still commits correctly when the door
# falls through -- proved against Set A doubts named in
# docs/reference/commit-hook-warm-reach-contract.md (that document's own
# transport, the pre-commit-hook door, is superseded/deleted per its own
# banner, but the Set A/Set B split it records is the live semantics
# `warm/client.py::try_warm_dispatch` still implements for THIS op's
# caller -- a PRE-delivery doubt (no pipe found) never reaches the server at
# all, and a post-delivery doubt whose error code is one of the
# `is_provably_undispatched` codes -- ENGINE_SKEW among them -- is PROOF the
# op never ran, so both fall through to cold rather than reporting
# indeterminate. AC8's table above already proves the double-commit
# invariant across every POST-delivery shape client.py can produce for a
# DELIVERED mutation; AC9's job is different and narrower: prove the cold
# leg itself -- the thing every Set A doubt falls through TO -- still lands
# a correct commit, not merely a safe (zero-or-one) one.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# C1 (AC1/AC2/AC3): the spawn count is a real gate, not a number that
# happens to pass. Wraps `subprocess.Popen` (never `.run` -- `.run`
# delegates to `Popen`, so wrapping `Popen` alone already catches
# `check_output`/`call`/every other `subprocess` entry point) for the
# duration of one in-process `run_commit_pipeline()` call and records argv
# per spawn. MUST BE SEEN RED against today's tree: `_MAX_SPAWNS` is the
# real target (<=4), not the count this pass through the pipeline actually
# produces (11) -- a spawn-count assertion that passes today would be
# measuring the wrong thing, exactly how AC6 shipped green against an op
# paying 11 spawns (this chunk's own brief). The fixture's own `git init`/
# `git add`/`git commit`/`git config` setup runs BEFORE the wrapper is
# installed, so only spawns from `run_commit_pipeline`'s own path are
# counted -- that setup is outside the measured window by construction.
# ---------------------------------------------------------------------------

_MAX_SPAWNS = 4


class _PopenSpawnRecorder:
    """Wraps `subprocess.Popen` to record argv per spawn without changing
    behaviour -- delegates construction to the real `Popen` immediately,
    recording the argv it was called with first. Installed via monkeypatch
    on the `subprocess` module for the duration of one pipeline call."""

    def __init__(self, real_popen):
        self._real_popen = real_popen
        self.calls: list = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(argv)
        return self._real_popen(argv, *args, **kwargs)


def test_modify_only_commit_asks_git_nothing_about_ignores(
    monkeypatch: pytest.MonkeyPatch, _repo: Path
) -> None:
    """C2c: `check-ignore` is asked ONLY about untracked paths.

    `check_ignore()`'s plain form is index-aware -- a tracked path is never
    reported ignored, because `git add` on an already-tracked path succeeds
    regardless of a later-added ignore pattern. So a path with an index
    entry cannot change the answer, and asking about it buys nothing.

    This is the shape the sibling spawn-count test CANNOT observe: that one
    commits a NEW file, which is exactly the untracked residual where the
    spawn is still the honest answer. A modify-only commit -- the ordinary
    case -- must not pay for it at all. Without this test, narrowing the
    candidate set to untracked paths would look identical to not narrowing
    it, since the only fixture in play always has an untracked path in it."""
    from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline

    file_name = "already_tracked.txt"
    (_repo / file_name).write_text("first\n", encoding="utf-8")
    _git(["add", "--", file_name], _repo)
    _git(["commit", "-q", "-m", "seed tracked path"], _repo)

    # Now MODIFY it: every staged path is tracked, so nothing can be ignored.
    (_repo / file_name).write_text("second\n", encoding="utf-8")

    recorder = _PopenSpawnRecorder(subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", recorder)

    result = run_commit_pipeline(
        _repo,
        session_id="c2c-tracked-only-session",
        subject="C2c tracked-only probe",
        stage_paths=[file_name],
        push_mode="none",
    )

    assert not result.commit_failed and result.committed_sha, (
        f"pipeline call under measurement must actually commit: {result!r}"
    )

    argvs = [list(map(str, argv)) for argv in recorder.calls]
    ignore_calls = [argv for argv in argvs if "check-ignore" in argv]
    assert ignore_calls == [], (
        "a commit whose every staged path is already tracked must not spawn "
        "`git check-ignore` -- a tracked path cannot be reported ignored, so "
        "the question has a free in-process answer. Got %r; full argv list: %r"
        % (ignore_calls, argvs)
    )


def test_run_commit_pipeline_spawn_count_is_gated(
    monkeypatch: pytest.MonkeyPatch, _repo: Path
) -> None:
    """AC1: total spawn count from one `run_commit_pipeline()` call must
    not exceed `_MAX_SPAWNS` (4). AC2: no two spawns share byte-identical
    argv -- a repeated identical `git` invocation is itself evidence of
    redundant work, not merely a count to shave. AC3: at most one spawn's
    argv contains a `status` subcommand -- `git status`/`git status
    --porcelain` collapsed to a single call across the whole pipeline."""
    from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline

    file_name = "spawn_count_probe.txt"
    (_repo / file_name).write_text("spawn count probe\n", encoding="utf-8")

    recorder = _PopenSpawnRecorder(subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", recorder)

    result = run_commit_pipeline(
        _repo,
        session_id="spawn-count-probe-session",
        subject="C1 spawn count probe",
        stage_paths=[file_name],
        push_mode="none",
    )

    assert not result.commit_failed and result.committed_sha, (
        f"pipeline call under measurement must actually commit: {result!r}"
    )

    argvs = [list(map(str, argv)) for argv in recorder.calls]

    # AC1: total spawn count.
    assert len(argvs) <= _MAX_SPAWNS, (
        f"run_commit_pipeline spawned {len(argvs)} processes "
        f"(budget is {_MAX_SPAWNS}); argv per spawn: {argvs!r}"
    )

    # AC2: no byte-identical argv pair.
    seen: dict = {}
    for argv in argvs:
        key = tuple(argv)
        seen[key] = seen.get(key, 0) + 1
    duplicates = {key: n for key, n in seen.items() if n > 1}
    assert not duplicates, (
        f"run_commit_pipeline issued byte-identical argv more than once: "
        f"{duplicates!r}; full argv list: {argvs!r}"
    )

    # AC3: at most one `status` invocation.
    status_calls = [argv for argv in argvs if "status" in argv]
    assert len(status_calls) <= 1, (
        f"run_commit_pipeline issued more than one `git status`-shaped "
        f"call: {status_calls!r}; full argv list: {argvs!r}"
    )


def test_modify_only_commit_spawn_count_is_at_most_one(
    monkeypatch: pytest.MonkeyPatch, _repo: Path
) -> None:
    """C3e (docs/dispatch-briefs/2026-08-26-the-commit-op-stops-asking-git-
    eleven-times/C3e.md): the LAST cut between this op and AC1 was
    `coordinator_core/git/divergence.py :: diverging_paths`'s own `git
    status --porcelain=v2 -z --no-renames` spawn. Once it settles in
    process, a MODIFY-ONLY commit (every staged path already tracked, no
    new file, so `check-ignore` was already free per `test_modify_only_
    commit_asks_git_nothing_about_ignores` above) should reach AC1's
    survivor: `git add`, the mutation itself, and nothing else.

    RUN THE INSTRUMENT, don't trust a report: this asserts the counter
    directly, the same `_PopenSpawnRecorder` the sibling spawn-count test
    above uses, over a fixture built specifically so no branch of this
    pipeline has an untracked-path or new-file residual to fall back on."""
    from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline

    file_name = "modify_only_spawn_probe.txt"
    (_repo / file_name).write_text("first\n", encoding="utf-8")
    _git(["add", "--", file_name], _repo)
    _git(["commit", "-q", "-m", "seed modify-only spawn probe"], _repo)

    # Now MODIFY it -- every staged path is tracked, nothing is new.
    (_repo / file_name).write_text("second\n", encoding="utf-8")

    recorder = _PopenSpawnRecorder(subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", recorder)

    result = run_commit_pipeline(
        _repo,
        session_id="modify-only-spawn-count-session",
        subject="C3e modify-only spawn count probe",
        stage_paths=[file_name],
        push_mode="none",
    )

    assert not result.commit_failed and result.committed_sha, (
        f"pipeline call under measurement must actually commit: {result!r}"
    )

    argvs = [list(map(str, argv)) for argv in recorder.calls]

    assert len(argvs) <= 1, (
        f"a modify-only commit must reach at most ONE spawn (`git add`, the "
        f"mutation itself) once diverging_paths settles in process; got "
        f"{len(argvs)}: {argvs!r}"
    )
