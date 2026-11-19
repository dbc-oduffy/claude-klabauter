"""Falsifier: the two 2026-11-19 engine lands that committed a peer's hunks calling a name only that peer's dirt
defines (`record_dir`, `handler_finished`), driven through the default `land_run._handler`. A peer hunk present at
the run's write baseline stays out of the commit (own-only blob, the run's other recorded hunk lands too); a peer
hunk that arrives after the measure refuses the land with its remedy, HEAD unmoved and nothing staged."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_unclaimed_peer_dirt_own_hunks import (  # noqa: F401
    DLV,
    PLAN,
    REPORT,
    RUN,
    SELF,
    SIZING,
    VERDICT,
    _author,
    _git,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

KEPT = "pkg/kept_work.py"
EMIT = "pkg/emit.py"
STAT = "pkg/run_dir_stat_cache.py"
IPC = "pkg/ipc.py"
ABANDON = "pkg/dispatch_abandon.py"


def _body(prefix: str, count: int = 14) -> str:
    return "".join(f"{prefix}_{i} = {i}\n" for i in range(1, count + 1))


KEPT_HEAD = "def keep():\n    return 1\n\n\n" + _body("kept")
KEPT_OWN = KEPT_HEAD.replace("def keep():\n    return 1\n", "def keep():\n    return 2\n")
PEER_CALLER = "\n\ndef note():\n    from pkg import run_dir_stat_cache\n\n    return run_dir_stat_cache.record_dir('d')\n"
KEPT_BOTH = KEPT_OWN + PEER_CALLER
KEPT_PEER = KEPT_HEAD + PEER_CALLER

STAT_HEAD = "def stat_dir():\n    return 0\n"
STAT_PEER = STAT_HEAD + "\n\ndef record_dir(path):\n    return path\n"

EMIT_HEAD = _body("emit")
EMIT_OWN = EMIT_HEAD.replace("emit_14 = 14\n", "emit_14 = 'own'\n")

IPC_HEAD = _body("ipc", 10) + "\n\ndef serve():\n    return 1\n"
IPC_OWN = IPC_HEAD.replace("ipc_1 = 1\n", "ipc_1 = 'own'\n")
IPC_CALLER = "\n\ndef finish():\n    from pkg import dispatch_abandon\n\n    return dispatch_abandon.handler_finished('r')\n"
IPC_BOTH = IPC_OWN + IPC_CALLER
IPC_PEER = IPC_HEAD + IPC_CALLER

ABANDON_HEAD = "def abandon():\n    return 0\n"
ABANDON_PEER = ABANDON_HEAD + "\n\ndef handler_finished(run_id):\n    return run_id\n"

ROW_SUMMARY = "carries foreign stat-cache hunks"
_REPORT_TEXT = "PARTIAL: X1\nNot done: nothing edited, files written by the run\n"


def _seed(root, files: dict) -> None:
    _write(root, "pkg/__init__.py", "")
    for rel, text in files.items():
        _write(root, rel, text)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed pkg")


def _declare(root, landing: list, baseline: dict) -> str:
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=tuple(landing), report=REPORT),), deliverable_id=DLV, plan_path=PLAN
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", write_baseline=baseline,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RUN}.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    return run_token.mint(root, RUN, f"{RUN}.mjs", session_id=SELF)


def _row(root, path: str, severity: str = "major") -> None:
    """An unresolved non-P0 reviewer row naming `path`, integrated into the run's review wave record."""
    share = root / ".coordinator-local" / "subagent-share" / SELF
    share.mkdir(parents=True, exist_ok=True)
    (share / f"{RUN}.review-wave-bookkeeping.md").write_text(
        '---\nintegrated_from: ["rev-1"]\n---\n\nbody\n', encoding="utf-8"
    )
    row = {"id": "f1", "file": path, "summary": ROW_SUMMARY, "severity": severity}
    (share / "rev-1.md").write_text(
        f"---\nagent_type: reviewer\n---\n\n## Findings Ledger\n\n```json\n{json.dumps([row])}\n```\n", encoding="utf-8"
    )


def _land(root, token: str, landing: list) -> dict:
    review = {
        **reviewed(),
        "prep": {"run_base_sha": None, "product_files": landing, "foreign_claims": [], "slice_files": landing},
        "delivery": {"verdict": "PASS"},
        "integration": {"unresolved": [], "confinement_violations": []},
    }
    return land_run._handler(
        {
            "script_path": f"{RUN}.mjs", "incomplete_chunks": ["X1"], "inline_review": review, "session_id": SELF,
            "run_id": RUN, "run_token": token, "ledger_verdict": VERDICT,
        },
        repo_root=root / ".git",
    )


def _stage_kept(root, *, peer_before_baseline: bool) -> tuple:
    """Run records a kept_work.py hunk and an emit.py hunk but declares only kept_work.py; a peer's dirt (a
    `record_dir` caller in kept_work.py, `record_dir` itself in run_dir_stat_cache.py) lands either before the
    run's write baseline or after the run's measure."""
    _seed(root, {KEPT: KEPT_HEAD, EMIT: EMIT_HEAD, STAT: STAT_HEAD})
    if peer_before_baseline:
        _write(root, KEPT, KEPT_PEER)
        _write(root, STAT, STAT_PEER)
    baseline = wb.capture(root, [KEPT, EMIT])
    _write(root, REPORT, _REPORT_TEXT)
    _author(root, KEPT, KEPT_BOTH if peer_before_baseline else KEPT_OWN)
    _author(root, EMIT, EMIT_OWN)
    token = _declare(root, [KEPT], baseline)
    mr.measured_rev(RUN, [KEPT, EMIT, REPORT], repo_root=root)
    if not peer_before_baseline:
        _write(root, KEPT, KEPT_BOTH)
        _write(root, STAT, STAT_PEER)
    return token, [KEPT]


def _stage_ipc(root, *, peer_before_baseline: bool) -> tuple:
    """The run edits ipc.py; a peer's dirt adds a `handler_finished` caller there and the def in
    dispatch_abandon.py, either before the run's write baseline or after the run's measure."""
    _seed(root, {IPC: IPC_HEAD, ABANDON: ABANDON_HEAD})
    if peer_before_baseline:
        _write(root, IPC, IPC_PEER)
        _write(root, ABANDON, ABANDON_PEER)
    baseline = wb.capture(root, [IPC])
    _write(root, REPORT, _REPORT_TEXT)
    _author(root, IPC, IPC_BOTH if peer_before_baseline else IPC_OWN)
    token = _declare(root, [IPC], baseline)
    mr.measured_rev(RUN, [IPC, REPORT], repo_root=root)
    if not peer_before_baseline:
        _write(root, IPC, IPC_BOTH)
        _write(root, ABANDON, ABANDON_PEER)
    return token, [IPC]


def _head_has(root, rel: str, needle: str) -> bool:
    return needle in _git(root, "show", f"HEAD:{rel}")


def _staged(root) -> list:
    return _git(root, "diff", "--cached", "--name-only").split()


def _assert_refused_clean(root, head: str, out: dict, path: str) -> None:
    """Refused with the named remedy, HEAD unmoved, nothing staged, the peer's dirt still in the worktree."""
    assert out.get("committed") is False, out
    assert out.get("refused") == land_run.COMMIT_SET_NOT_RECORDED_HUNKS, out
    assert path in out["paths"] and "carries unrecorded hunk" in out["error"], out
    assert "Remedy:" in out["error"] and "peer's hunk never rides this commit" in out["error"], out
    assert _git(root, "rev-parse", "HEAD").strip() == head
    assert _staged(root) == [], _staged(root)


def _assert_no_foreign_hunk_at_head(root) -> None:
    """The committed image holds no caller of a name only the peer's uncommitted dirt defines."""
    for rel, needle in ((KEPT, "record_dir"), (IPC, "handler_finished"), (STAT, "record_dir"), (ABANDON, "handler_finished")):
        if _git(root, "ls-tree", "HEAD", "--", rel).strip():
            assert not _head_has(root, rel, needle), (rel, needle)


@pytest.mark.parametrize("with_row", [False, True])
def test_instance_1_peer_stat_cache_hunks_beside_the_run_never_reach_head_and_the_emit_hunk_lands(repo, with_row) -> None:  # noqa: F811
    token, landing = _stage_kept(repo, peer_before_baseline=True)
    if with_row:
        _row(repo, KEPT)
    head = _git(repo, "rev-parse", "HEAD").strip()

    out = _land(repo, token, landing)

    assert out.get("committed") is True and not out.get("refused"), out
    assert _git(repo, "rev-parse", "HEAD").strip() != head
    assert _git(repo, "show", f"{out['sha']}:{KEPT}") == KEPT_OWN
    assert _git(repo, "show", f"{out['sha']}:{EMIT}") == EMIT_OWN
    assert _git(repo, "show", f"{out['sha']}:{STAT}") == STAT_HEAD
    _assert_no_foreign_hunk_at_head(repo)
    assert [e for e in out.get("provenance", []) if e["reason"] == "import-closure-untracked"] == []
    assert (ROW_SUMMARY in json.dumps(out.get("provenance", []))) is with_row, out
    assert (repo / KEPT).read_text(encoding="utf-8") == KEPT_BOTH
    assert (repo / STAT).read_text(encoding="utf-8") == STAT_PEER


@pytest.mark.parametrize("with_row", [False, True])
def test_instance_1_a_peer_stat_cache_hunk_added_after_the_measure_refuses_the_land(repo, with_row) -> None:  # noqa: F811
    token, landing = _stage_kept(repo, peer_before_baseline=False)
    if with_row:
        _row(repo, KEPT)
    head = _git(repo, "rev-parse", "HEAD").strip()

    out = _land(repo, token, landing)

    _assert_refused_clean(repo, head, out, KEPT)
    _assert_no_foreign_hunk_at_head(repo)
    assert (repo / KEPT).read_text(encoding="utf-8") == KEPT_BOTH
    assert (repo / EMIT).read_text(encoding="utf-8") == EMIT_OWN


def test_instance_2_a_peers_handler_finished_caller_beside_the_run_never_reaches_head(repo) -> None:  # noqa: F811
    token, landing = _stage_ipc(repo, peer_before_baseline=True)
    head = _git(repo, "rev-parse", "HEAD").strip()

    out = _land(repo, token, landing)

    assert out.get("committed") is True and not out.get("refused"), out
    assert _git(repo, "rev-parse", "HEAD").strip() != head
    assert _git(repo, "show", f"{out['sha']}:{IPC}") == IPC_OWN
    assert _git(repo, "show", f"{out['sha']}:{ABANDON}") == ABANDON_HEAD
    _assert_no_foreign_hunk_at_head(repo)
    assert [e for e in out.get("provenance", []) if e["reason"] == "import-closure-untracked"] == []
    assert (repo / IPC).read_text(encoding="utf-8") == IPC_BOTH


@pytest.mark.parametrize("with_row", [False, True])
def test_instance_2_a_peers_handler_finished_hunks_overlaid_after_the_measure_refuse_the_land(repo, with_row) -> None:  # noqa: F811
    token, landing = _stage_ipc(repo, peer_before_baseline=False)
    if with_row:
        _row(repo, IPC)
    head = _git(repo, "rev-parse", "HEAD").strip()

    out = _land(repo, token, landing)

    _assert_refused_clean(repo, head, out, IPC)
    _assert_no_foreign_hunk_at_head(repo)
    assert (repo / IPC).read_text(encoding="utf-8") == IPC_BOTH
    assert (repo / ABANDON).read_text(encoding="utf-8") == ABANDON_PEER
