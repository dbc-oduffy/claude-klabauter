"""The review candidate carries a run's latest own bytes for every path it owns, never HEAD or a stale kept blob."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, in_run_widen, kept_work, own_hunk_candidate as ohc
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import resume_gate, review_edits, run_write_prints
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-28-fixture-cdffc5-20261128T051056-f97c57"
MOD = "pkg/mod.py"
ADDED = "pkg/added.py"
NEWMOD = "pkg/newmod.py"
PEER_NEW = "pkg/peer_new.py"
PAD = "".join(f"# pad {i:02d}\n" for i in range(30))
HEAD_MOD = "def old():\n    return 1\n" + PAD
KEPT_MOD = "A = 1\n" + HEAD_MOD
LATEST_MOD = KEPT_MOD + "B = 2\n"
PEER_MOD = LATEST_MOD.replace("# pad 15\n", "# pad 15\nPEER = 3\n")
HEAD_ADDED = "def stable():\n    return 0\n" + PAD
KEPT_ADDED = "K = 1\n" + HEAD_ADDED
LATEST_ADDED = KEPT_ADDED + "LATEST = 2\n"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = seed_repo(tmp_path / "repo")
    _put(root, ".gitignore", "state/\nscratch/\n.coordinator-local/\n")
    _put(root, "pkg/__init__.py", "")
    _put(root, MOD, HEAD_MOD)
    _put(root, ADDED, HEAD_ADDED)
    _git(["add", ".gitignore", "pkg"], root)
    _git(["commit", "-q", "-m", "pkg"], root)
    ask_stage.record_dir(root, RUN).mkdir(parents=True, exist_ok=True)
    return root


def _rec(root: Path) -> Path:
    return ask_stage.record_dir(root, RUN)


def _print(root: Path, rel: str, text: str) -> str:
    """Write `text` at `rel`, print it as the run's write and return its blob sha (object stored)."""
    _put(root, rel, text)
    run_write_prints.record(root, RUN, [rel])
    return run_write_prints.read(root, RUN)[rel]


def _candidate(root: Path, rel: str, head: str, tree: str, **kw) -> dict:
    return ohc.candidate(root, RUN, rel, head.encode(), tree.encode(), **kw)


@pytest.fixture
def no_peer(monkeypatch):
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])


def test_a_kept_blob_from_an_earlier_leg_does_not_hide_the_latest_own_print(repo):
    pinned = _print(repo, MOD, KEPT_MOD)
    kept_work.write(_rec(repo), {"c1": {"paths": {MOD: pinned}}}, None)
    _print(repo, MOD, LATEST_MOD)

    res = _candidate(repo, MOD, HEAD_MOD, LATEST_MOD, declared=False)

    assert res.get("bytes") == LATEST_MOD.encode() and res.get("excluded") == [], res


def test_without_a_latest_print_the_hunk_the_kept_blob_lacks_stays_excluded(repo):
    pinned = _print(repo, MOD, KEPT_MOD)
    kept_work.write(_rec(repo), {"c1": {"paths": {MOD: pinned}}}, None)
    run_write_prints.drop(repo, RUN, MOD)
    _put(repo, MOD, LATEST_MOD)

    res = _candidate(repo, MOD, HEAD_MOD, LATEST_MOD, declared=False)

    assert res.get("bytes") == KEPT_MOD.encode() and len(res.get("excluded", ())) == 1, res


def test_a_peer_hunk_beside_the_latest_own_print_is_still_excluded(repo):
    pinned = _print(repo, MOD, KEPT_MOD)
    kept_work.write(_rec(repo), {"c1": {"paths": {MOD: pinned}}}, None)
    _print(repo, MOD, LATEST_MOD)
    _put(repo, MOD, PEER_MOD)

    res = _candidate(repo, MOD, HEAD_MOD, PEER_MOD, declared=False)

    assert res.get("bytes") == LATEST_MOD.encode() and len(res.get("excluded", ())) == 1, res


def test_a_path_added_by_add_paths_reaches_the_candidate_at_its_latest_own_bytes(repo):
    _put(repo, "state/warp/placeholder.txt", "x")
    manifest = {"review_declared_paths": [MOD], "rows": [{"id": "X1", "writes": [MOD], "writes_under": []}]}
    (_rec(repo) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    assert in_run_widen.record_widens(_rec(repo), [{"row": "X1", "path": ADDED}]) == 1
    pinned = _print(repo, ADDED, KEPT_ADDED)
    kept_work.write(_rec(repo), {"c1": {"paths": {ADDED: pinned}}}, None)
    _print(repo, ADDED, LATEST_ADDED)

    res = _candidate(repo, ADDED, HEAD_ADDED, LATEST_ADDED)

    assert res.get("bytes") == LATEST_ADDED.encode() and res.get("excluded") == [], res


def test_a_path_added_by_add_paths_reaches_the_export_mirror_at_its_latest_own_bytes(repo, no_peer):
    manifest = {"review_declared_paths": [MOD], "rows": [{"id": "X1", "writes": [MOD], "writes_under": []}]}
    (_rec(repo) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    in_run_widen.record_widens(_rec(repo), [{"row": "X1", "path": ADDED}])
    pinned = _print(repo, ADDED, KEPT_ADDED)
    kept_work.write(_rec(repo), {"c1": {"paths": {ADDED: pinned}}}, None)
    _print(repo, ADDED, LATEST_ADDED)
    probe = "t = open('pkg/added.py', encoding='utf-8').read();raise SystemExit(0 if 'LATEST = 2' in t else 3)"

    paths, overrides = cov.own_delta(repo, RUN, [MOD])
    ran = cov.run_in_export(repo, RUN, paths, [sys.executable, "-c", probe], overrides)

    assert ADDED in paths, paths
    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_a_taken_over_path_reaches_the_candidate_at_the_dead_sessions_bytes(repo):
    head_fp = kept_work.fingerprint(repo, MOD)
    (_rec(repo) / "manifest.json").write_text(
        json.dumps({"write_baseline": {MOD: head_fp}, "foreign_at_baseline": {}}), encoding="utf-8", newline="\n"
    )
    resume_gate.record_takeover(_rec(repo), "dead-session", "live-session", "driver died")
    _put(repo, MOD, LATEST_MOD)

    res = _candidate(repo, MOD, HEAD_MOD, LATEST_MOD)

    assert res.get("bytes") == LATEST_MOD.encode() and res.get("excluded") == [], res


def test_a_new_untracked_module_the_run_printed_is_named_as_an_own_untracked_write(repo):
    _print(repo, NEWMOD, "VALUE = 1\n")
    _put(repo, PEER_NEW, "PEER = 1\n")
    _print(repo, MOD, KEPT_MOD)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    named = cov.own_untracked_writes(repo, RUN, head)

    assert named == [NEWMOD], named


def test_a_new_untracked_module_reaches_the_export_mirror_at_its_latest_own_bytes(repo, no_peer):
    _print(repo, NEWMOD, "VALUE = 1\n")
    _print(repo, NEWMOD, "VALUE = 1\nLATEST = 2\n")
    probe = "t = open('pkg/newmod.py', encoding='utf-8').read();raise SystemExit(0 if 'LATEST = 2' in t else 3)"

    paths, overrides = cov.own_delta(repo, RUN, [MOD])
    ran = cov.run_in_export(repo, RUN, paths, [sys.executable, "-c", probe], overrides)

    assert NEWMOD in paths, paths
    assert ran.returncode == 0, ran.stdout + ran.stderr


HALTED = "warp-2026-11-28-fixture-halted-aaaaaa-20261128T040000-bbbbbb"


def _halted_peer(root: Path, rel: str, peer_text: str) -> None:
    """A halted run whose record keeps `peer_text` for `rel` and whose withheld index names `rel`."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    rdir = ask_stage.record_dir(root, HALTED)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "halted": "ledger-check", "run_artifacts": {"withheld": {rel: "kept"}}}),
        encoding="utf-8",
    )
    _put(root, rel, peer_text)
    blob = _git(["hash-object", "-w", rel], root).strip()
    kept_work.write(rdir, {"c1": {"paths": {rel: blob}}}, None)
    index = root / halted_withheld_index.INDEX_DIR
    index.mkdir(parents=True, exist_ok=True)
    (index / f"{HALTED}.json").write_text(json.dumps({"paths": [rel], "declared": [rel], "at_ns": 1}), encoding="utf-8")


def test_a_halted_peers_hunk_is_excluded_not_owned_even_beyond_the_recent_run_scan(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import stranded_sibling

    peer_only = HEAD_MOD.replace("# pad 15\n", "# pad 15\nPEER = 3\n")
    both = KEPT_MOD.replace("# pad 15\n", "# pad 15\nPEER = 3\n")
    _halted_peer(repo, MOD, peer_only)
    _put(repo, MOD, both)
    monkeypatch.setattr(stranded_sibling, "recent_run_dirs", lambda *_a, **_k: [])

    res = _candidate(repo, MOD, HEAD_MOD, both, declared=True)

    assert res.get("bytes") == KEPT_MOD.encode(), res
    assert [e["owner"] for e in res.get("excluded", ())] == [HALTED], res


def test_a_halted_peers_hunk_stays_excluded_from_the_withheld_print_while_the_own_print_rides(repo):
    from coordinator_core.ops.dispatch_emit.halted_record_ledger import _co_tenant_print

    _print(repo, MOD, LATEST_MOD)
    _halted_peer(repo, MOD, PEER_MOD)
    _put(repo, MOD, PEER_MOD)

    printed = _co_tenant_print(repo, RUN, MOD)

    assert isinstance(printed, str), printed
    assert _git(["cat-file", "blob", printed], repo) == LATEST_MOD


def test_the_withheld_print_falls_back_to_the_latest_own_print_when_the_hunks_cannot_be_split(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_hunk_claims
    from coordinator_core.ops.dispatch_emit.halted_record_ledger import _co_tenant_print

    own = _print(repo, MOD, LATEST_MOD)
    _halted_peer(repo, MOD, PEER_MOD)
    _put(repo, MOD, PEER_MOD)
    monkeypatch.setattr(run_hunk_claims, "own_hunks", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("x")))

    assert _co_tenant_print(repo, RUN, MOD) == own


def test_a_path_the_run_never_wrote_stays_a_typed_entry_for_the_halted_peer(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_hunk_claims
    from coordinator_core.ops.dispatch_emit.halted_record_ledger import _co_tenant_print

    _halted_peer(repo, MOD, PEER_MOD)
    _put(repo, MOD, PEER_MOD)
    monkeypatch.setattr(run_hunk_claims, "own_hunks", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("x")))

    assert _co_tenant_print(repo, RUN, MOD) == {"kind": "co_tenant", "owner": HALTED}
