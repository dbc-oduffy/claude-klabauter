"""
coordinator_core.ops.fleet.tests.test_memo_send_spawn_budget

C2 (docs/plans/2026-08-21-memo-send-stops-asking-git-what-it-already-knows.md):
the spawn-count ratchet for `_commit_delivered_memo` (delivery leg) and
`_commit_sender_ledger` (sender-ledger leg), so C1's cut from six spawns to
three does not silently regrow.

Modelled on `coordinator_core/ops/ceremony/tests/test_commit_e2e_spawn_budget.py`
— read that file before touching this one, per that plan's own C2 task body.
This module does NOT reuse that file's `_count_op_spawns_both_ways` machinery
verbatim: `memo_send.py`'s delivery leg shells out via
`asyncio.create_subprocess_exec` (never `git_native._git`/`subprocess.run`),
so the ceremony seam simply is not reachable here. The counting mechanism
instead mirrors `state/audits/2026-08-21-memo-send-spawn-count-probe.py`
(the working harness this plan's Problem section cites as the source of the
measured 6->3 figure): substitute the `asyncio.create_subprocess_exec`
MODULE ATTRIBUTE for the delivery leg, and `subprocess.run` for the
sender-ledger leg (which is sync, `git_native._git`-routed, wrapped in
`asyncio.to_thread` — see `_commit_sender_ledger`'s own docstring).

WHY ARGV LISTS, NOT JUST COUNTS. A count-only assertion passes when one read
spawn is swapped for a different read spawn (e.g. `git log` regressing back
to a blind `git rev-parse HEAD`) — the exact failure C1's own anti-scope
entry warns against (a blind HEAD read silently regresses `committed_sha`'s
concurrent-sibling guarantee, AC2). Every assertion below pins the actual
argv, not merely `len(...)`.

DELIVERY LEG (`_commit_delivered_memo`), AC1a: exactly 2 spawns on the
success path — `git add` and `git commit` — and zero read spawns, plus at
most one pathspec-scoped fallback spawn (`git log -1 --format=%H --
<path>`) on the concurrent-sibling race path (AC2).

SENDER-LEDGER LEG (`_commit_sender_ledger`), AC1b/C3: `git rev-parse HEAD`
and `git ls-tree HEAD -- <path>` are NOT eliminated here — both live inside
`ops/ceremony/git_native.py :: commit_authored_content`, which is in the
EXECUTING scope of docs/plans/2026-08-21-the-commit-path-reads-git-state-
without-spawning-git.md (spine row C3 of THIS plan, disposition
`spun_off`). This module therefore asserts the ledger leg's total spawn
count as an UPPER BOUND (`<=`, never `==`) — measured at 12 real `git`
invocations for this leg's own fixture shape (rev-parse, ls-tree,
hash-object, read-tree, update-index x2, write-tree, interpret-trailers,
commit-tree, update-ref, plus auto_push's post-commit-replay `show`/
`status` bookkeeping) — so a future session landing C3 and dropping this
leg's spawn count is a PASS here, never a break, while a genuinely new
spawn anywhere on the leg still fails loud.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.fleet.memo_send as memo_send_module
from coordinator_core.ops.fleet.memo_send import (
    _commit_delivered_memo,
    _commit_sender_ledger,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


# ---------------------------------------------------------------------------
# Fixture helpers — same shape as test_memo_send.py's own `_git`/
# `_make_receiver_git_repo` (not imported cross-module; this file's HARD
# FILE SCOPE forbids editing that module to export them, and duplicating a
# five-line git-init helper is cheaper than a shared-fixture refactor here).
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        check=check,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _make_receiver_repo(tmp_path: Path, *, branch: str = "budget-test") -> Path:
    """A minimal receiver repo on an explicit non-`main`/`master` branch —
    the ordinary case AC1a's exact-count assertions are measured against.
    `branch` is a plain feature-branch name so `_commit_delivered_memo`'s
    own `main`/`master` warning stays OUT of these tests unless a test
    deliberately opts into it (see `test_main_branch_warning_preserved`
    below)."""
    root = tmp_path / "receiver-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", branch)
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init receiver")
    return root


def _write_memo(receiver_repo: Path, name: str) -> str:
    """Write an undelivered memo file into the receiver's inbox and return
    its repo-relative, posix-separated path (the shape `_commit_delivered_
    memo`'s `memo_relpath` parameter expects)."""
    memo_path = receiver_repo / "cross-repo" / "inbox" / name
    memo_path.write_text("---\ntitle: probe\n---\n\nbody\n", encoding="utf-8")
    return "cross-repo/inbox/" + name


def _make_sender_repo(tmp_path: Path) -> Path:
    """A minimal sender repo, seeded with a committed
    `state/memo-outbox/sent-ledger.jsonl` — `commit_authored_content`'s own
    containment guard (git_native.py) requires the target path to already
    exist in HEAD (in-place mutation only, never file creation)."""
    root = tmp_path / "sender-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    ledger_dir = root / "state" / "memo-outbox"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "sent-ledger.jsonl").write_text('{"seed": true}\n', encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed ledger")
    return root


# ---------------------------------------------------------------------------
# Delivery-leg counter — substitutes asyncio.create_subprocess_exec, the
# ONLY spawn mechanism `_commit_delivered_memo`/`_resolve_committed_sha` use
# (mirrors state/audits/2026-08-21-memo-send-spawn-count-probe.py exactly).
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


async def _count_delivery_spawns(coro_factory):
    """Run one `_commit_delivered_memo` coroutine under an
    `asyncio.create_subprocess_exec` counter and return `(argv_list, result)`.

    Patches the MODULE ATTRIBUTE on `asyncio` itself, not a name imported
    into `memo_send`'s namespace — `memo_send.py` does `import asyncio` and
    calls `asyncio.create_subprocess_exec(...)` through that shared module
    reference at call time, so this is the same seam the probe script
    patches, not a second dialect."""
    argv_list: list[list[str]] = []
    orig_exec = asyncio.create_subprocess_exec

    async def _counting_exec(*args, **kw):
        argv_list.append(list(args))
        return await orig_exec(*args, **kw)

    asyncio.create_subprocess_exec = _counting_exec
    try:
        result = await coro_factory()
    finally:
        asyncio.create_subprocess_exec = orig_exec
    return argv_list, result


def _normalize_commit_argv(argv: list[str]) -> list[str]:
    """Replace the dynamic `core.hooksPath=<tempdir>` element with a fixed
    placeholder so the commit spawn's argv can be compared by equality —
    `_commit_delivered_memo` creates a fresh `tempfile.TemporaryDirectory()`
    per call (the all-hooks-off mechanism its own docstring RATIONALE
    describes), so that one element is the only non-deterministic part of
    an otherwise-fixed argv shape."""
    return [
        "core.hooksPath=<TMP>" if a.startswith("core.hooksPath=") else a
        for a in argv
    ]


# ---------------------------------------------------------------------------
# AC1a — success path: exactly 2 spawns (add, commit), zero reads.
# ---------------------------------------------------------------------------


def test_delivery_leg_success_path_argv_and_zero_reads(tmp_path):
    receiver_repo = _make_receiver_repo(tmp_path)
    memo_relpath = _write_memo(receiver_repo, "success-path.md")

    async def _call():
        return await _commit_delivered_memo(receiver_repo, memo_relpath, "sender-em", "probe")

    argv_list, outcome = _run(_count_delivery_spawns(_call))

    assert outcome.committed is True, "fixture did not land a commit: %r" % (outcome,)
    assert len(argv_list) == 2, (
        "delivery leg spawned %d processes on the success path, AC1a budgets "
        "exactly 2 (git add, git commit) — argv lists: %r" % (len(argv_list), argv_list)
    )

    add_argv, commit_argv = argv_list
    assert add_argv == [
        "git", "-C", str(receiver_repo), "add", "--", memo_relpath,
    ], "unexpected `git add` argv: %r" % (add_argv,)

    normalized_commit = _normalize_commit_argv(commit_argv)
    assert normalized_commit == [
        "git", "-C", str(receiver_repo),
        "-c", "core.hooksPath=<TMP>",
        "-c", "commit.gpgsign=false",
        "commit", "-m", normalized_commit[9],
        "--", memo_relpath,
    ], "unexpected `git commit` argv (hooksPath normalized): %r" % (normalized_commit,)
    assert normalized_commit[9].startswith("cross-repo: deliver probe memo from sender-em"), (
        "commit message does not match the expected subject: %r" % (normalized_commit[9],)
    )

    # Zero read spawns — no symbolic-ref, log, rev-parse, or ls-tree spawn
    # anywhere in the argv list. AC1a's own "zero read spawns" clause.
    read_verbs = {"symbolic-ref", "log", "rev-parse", "ls-tree"}
    for argv in argv_list:
        assert not (set(argv) & read_verbs), (
            "delivery leg issued a READ spawn (%r) — AC1a budgets zero reads "
            "on the success path" % (argv,)
        )

    # committed_sha resolved in-process (zero-spawn common path) and full width.
    assert outcome.committed_sha is not None, "committed_sha unexpectedly absent: %r" % (outcome,)
    assert _FULL_SHA_RE.fullmatch(outcome.committed_sha), (
        "committed_sha is not a full 40-char sha: %r" % (outcome.committed_sha,)
    )


# ---------------------------------------------------------------------------
# AC2 — concurrent-sibling race: the fallback spawn still fires, and
# committed_sha is still THIS call's own sha, never the (deliberately
# wrong) monkeypatched HEAD value.
# ---------------------------------------------------------------------------


def test_sibling_race_fallback_spawn_fires_and_sha_is_ours(tmp_path, monkeypatch):
    """Forces `_resolve_committed_sha`'s in-process prefix-check to fail on
    every call (a `git_state.head_sha` that never starts with the real
    commit's abbreviated sha — indistinguishable, from that check's own
    point of view, from a real sibling's commit having moved HEAD in the
    narrow post-commit window) — reusing C1's own verification technique
    (monkeypatch `git_state.head_sha` to a wrong value) rather than trying
    to engineer a real concurrent writer in a single-process test."""
    receiver_repo = _make_receiver_repo(tmp_path)
    memo_relpath = _write_memo(receiver_repo, "sibling-race.md")

    wrong_sha = "f" * 40
    monkeypatch.setattr(
        memo_send_module.git_state, "head_sha", lambda repo: wrong_sha,
    )

    async def _call():
        return await _commit_delivered_memo(receiver_repo, memo_relpath, "sender-em", "probe")

    argv_list, outcome = _run(_count_delivery_spawns(_call))

    assert outcome.committed is True, "fixture did not land a commit: %r" % (outcome,)
    assert len(argv_list) == 3, (
        "sibling-race path spawned %d processes, AC2 budgets add+commit plus "
        "exactly one pathspec-scoped fallback spawn — argv lists: %r"
        % (len(argv_list), argv_list)
    )

    fallback_argv = argv_list[2]
    assert fallback_argv == [
        "git", "-C", str(receiver_repo),
        "log", "-1", "--format=%H", "--", memo_relpath,
    ], "unexpected fallback-spawn argv: %r" % (fallback_argv,)

    # The resolved sha must be OURS, not the monkeypatched wrong_sha — the
    # exact regression AC2 and the plan's anti-scope entry both name.
    assert outcome.committed_sha is not None, "committed_sha unexpectedly absent: %r" % (outcome,)
    assert outcome.committed_sha != wrong_sha, (
        "committed_sha equals the monkeypatched WRONG head_sha — the "
        "sibling-race fallback did not override a stale/foreign HEAD read"
    )
    assert _FULL_SHA_RE.fullmatch(outcome.committed_sha), (
        "committed_sha is not a full 40-char sha: %r" % (outcome.committed_sha,)
    )
    real_head = _git(receiver_repo, "rev-parse", "HEAD").stdout.decode().strip()
    assert outcome.committed_sha == real_head, (
        "committed_sha %r does not match the receiver's real HEAD %r after "
        "the fallback spawn" % (outcome.committed_sha, real_head)
    )


# ---------------------------------------------------------------------------
# Preserved behaviours — detached HEAD skip, main/master warning. Neither
# is an AC1a/AC2 assertion on its own; both are named in the plan's task
# body as behaviour the in-process HEAD read must not change, so both get
# a spawn-count check here too (0 spawns on the skip; the ordinary 2 on
# main/master, same as the success-path shape).
# ---------------------------------------------------------------------------


def test_detached_head_skips_with_zero_spawns(tmp_path):
    receiver_repo = _make_receiver_repo(tmp_path)
    memo_relpath = _write_memo(receiver_repo, "detached-head.md")
    head_sha = _git(receiver_repo, "rev-parse", "HEAD").stdout.decode().strip()
    _git(receiver_repo, "checkout", "-q", head_sha)  # detach

    async def _call():
        return await _commit_delivered_memo(receiver_repo, memo_relpath, "sender-em", "probe")

    argv_list, outcome = _run(_count_delivery_spawns(_call))

    assert outcome.committed is False, "detached HEAD must skip the commit: %r" % (outcome,)
    assert outcome.branch is None
    assert argv_list == [], (
        "detached-HEAD skip must issue zero spawns (in-process HEAD/HEAD-file "
        "read only) — argv lists: %r" % (argv_list,)
    )


def test_main_branch_warning_preserved_still_two_spawns(tmp_path, caplog):
    receiver_repo = _make_receiver_repo(tmp_path, branch="main")
    memo_relpath = _write_memo(receiver_repo, "main-branch.md")

    async def _call():
        return await _commit_delivered_memo(receiver_repo, memo_relpath, "sender-em", "probe")

    with caplog.at_level("WARNING"):
        argv_list, outcome = _run(_count_delivery_spawns(_call))

    assert outcome.committed is True, "fixture did not land a commit: %r" % (outcome,)
    assert outcome.branch == "main"
    assert len(argv_list) == 2, (
        "main-branch commit must still cost exactly 2 spawns (the warning "
        "does not change the commit path) — argv lists: %r" % (argv_list,)
    )
    assert any(
        "main" in rec.message and "committing the delivered memo there anyway" in rec.message
        for rec in caplog.records
    ), "expected the main/master WARNING to still fire — preserved behaviour, not touched by this plan"


# ---------------------------------------------------------------------------
# C7 (docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md) — TOTAL
# end-to-end spawn count for a full `_memo_send` call, warm, in-process,
# through the registered handler itself (`_memo_send` IS the
# `@register_op("memo.send")` target, not a wrapper around it). AC1a/AC1b
# above pin the delivery leg and ledger leg SEPARATELY; this pins the
# whole op's total so a spawn moving between legs, or a new spawn
# appearing anywhere in `_memo_send`'s own body outside either dedicated
# leg (`_git_check_ignore`, auto_push's post-commit `show`/`status`
# bookkeeping), is still caught. Measured at
# `state/audits/2026-08-22-memo-send-end-to-end-reinstatement-probe.py`
# Section 1 (n=15, INVARIANT at 9 every run, argv sample recorded there).
#
# NOT a timing ratchet. The same audit's Section 2 (n=15, cold subprocess
# per sample, lazy channel armed, `_memo_send` resolved directly since
# `memo.send` is administratively SUSPENDED at the dispatch layer and
# cannot be reached via `coordinator_core.invoke`'s ordinary cold CLI path)
# measured p50=2875.0ms / p90=11890.6ms COLD process time — far OVER both
# the PM's 400ms cold bar and the 2000ms `SUSPENSION_BAR_MS` this op is
# currently held under. Per the C7 dispatch brief: a COLD p90 over 400ms is
# not reported green and the bar is not relaxed, so no timing ratchet is
# added here. `op_budget_suspension.py`'s `SUSPENDED_OPS["memo.send"]`
# entry is NOT edited by this chunk — "that table is a live baton's
# surface" (C7 dispatch brief, out of scope) — these cold figures are
# handed to its holder, not landed here.
# ---------------------------------------------------------------------------

_END_TO_END_TOTAL_SPAWN_COUNT = 9


def _make_end_to_end_receiver(tmp_path: Path) -> Path:
    root = tmp_path / "e2e-receiver"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init receiver")
    return root


def _make_end_to_end_claude_home(tmp_path: Path, receiver_repo: Path) -> Path:
    claude_home = tmp_path / "e2e-claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    toml_val = str(receiver_repo).replace("\\", "\\\\").replace('"', '\\"')
    (machine_local / "registry.local.toml").write_text(
        f'"repos.project_rag" = "{toml_val}"\n', encoding="utf-8",
    )
    return claude_home


def _make_end_to_end_sender(tmp_path: Path) -> Path:
    root = tmp_path / "e2e-sender"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    ledger_dir = root / "state" / "memo-outbox"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "sent-ledger.jsonl").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed ledger")
    return root


def test_end_to_end_memo_send_total_spawn_count_is_invariant(tmp_path, monkeypatch):
    """Total spawn count across a full, successful `_memo_send` call, pinned
    at the measured-invariant figure (9). See the block comment above this
    test for what this is and is not a ratchet on."""
    receiver = _make_end_to_end_receiver(tmp_path)
    claude_home = _make_end_to_end_claude_home(tmp_path, receiver)
    sender = _make_end_to_end_sender(tmp_path)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

    params = {
        "dry_run": False,
        "topic": "spawn-budget-e2e",
        "to": "project-rag-em",
        "title": "Test Memo",
        "body": "This is a test memo body.",
        "kind": "fyi",
        "summary": "Test summary.",
    }

    spawns: list[list[str]] = []
    orig_exec = asyncio.create_subprocess_exec
    orig_run = subprocess.run

    async def _counting_exec(*args, **kw):
        spawns.append([str(a) for a in args])
        return await orig_exec(*args, **kw)

    def _counting_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and "git" in str(cmd[0]):
            spawns.append([str(x) for x in cmd])
        return orig_run(cmd, *a, **kw)

    asyncio.create_subprocess_exec = _counting_exec
    subprocess.run = _counting_run
    try:
        result = _run(memo_send_module._memo_send(params, repo_root=sender))
    finally:
        asyncio.create_subprocess_exec = orig_exec
        subprocess.run = orig_run

    assert result.get("exit_code") == 0, (
        "end-to-end memo.send did not succeed -- refusing to trust a spawn "
        "count from a failed/refused run: %r" % (result,)
    )
    assert len(spawns) == _END_TO_END_TOTAL_SPAWN_COUNT, (
        "end-to-end memo.send spawned %d processes, ratchet pins exactly %d "
        "-- a NEW spawn appeared (or one vanished: lower the ratchet and "
        "cite the new measurement) -- argv lists: %r"
        % (len(spawns), _END_TO_END_TOTAL_SPAWN_COUNT, spawns)
    )


# ---------------------------------------------------------------------------
# AC1b/C3 — sender-ledger leg: UPPER BOUND, not exact equality. See module
# docstring for why `<=` is the right shape here and `==` is not.
# ---------------------------------------------------------------------------

# Measured 2026-08-21 against this file's own `_make_sender_repo` fixture
# shape (see module docstring for the full spawn list). NOT the plan's own
# 6-spawn PROBLEM-TABLE figure — that table counts memo_send.py's TOTAL
# across both legs; this is the ledger leg's own total via
# commit_authored_content, which was never in scope for the 6->3 cut this
# plan's C1 made (see C3's disposition_detail: those two reads belong to a
# different, executing peer plan).
_LEDGER_LEG_SPAWN_UPPER_BOUND = 12


def test_ledger_leg_spawn_count_is_upper_bound_not_exact(tmp_path):
    sender_repo = _make_sender_repo(tmp_path)
    spawns: list[list[str]] = []
    orig_run = subprocess.run

    def _counting_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and "git" in str(cmd[0]):
            spawns.append(list(cmd))
        return orig_run(cmd, *a, **kw)

    subprocess.run = _counting_run
    try:
        sha = _run(_commit_sender_ledger(sender_repo, '{"seed": true, "sent": 1}\n'))
    finally:
        subprocess.run = orig_run

    assert sha is not None, "fixture did not land a ledger commit"
    assert len(spawns) <= _LEDGER_LEG_SPAWN_UPPER_BOUND, (
        "sender-ledger leg spawned %d processes, upper bound is %d — a "
        "NEW spawn was added on this leg (a decrease, e.g. from spine row "
        "C3 landing, is fine and expected; a growth beyond the bound is "
        "the regression this test exists to catch). argv lists: %r"
        % (len(spawns), _LEDGER_LEG_SPAWN_UPPER_BOUND, spawns)
    )
    # The two reads C3 owns must still be present-or-absent as a PAIR, never
    # partially swapped for something else — if exactly one of them vanishes
    # while the other stays, that is a routing change this ratchet should
    # see, not a silent partial win.
    has_rev_parse_head = any(a[:3] == ["git", "rev-parse", "HEAD"] for a in spawns)
    has_ls_tree_head = any(a[:2] == ["git", "ls-tree"] and "HEAD" in a for a in spawns)
    assert has_rev_parse_head == has_ls_tree_head, (
        "expected `git rev-parse HEAD` and `git ls-tree HEAD` to be present "
        "or absent TOGETHER (both are C3's to remove as a pair) — got "
        "rev-parse=%r ls-tree=%r in %r" % (has_rev_parse_head, has_ls_tree_head, spawns)
    )
