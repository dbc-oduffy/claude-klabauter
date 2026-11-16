"""The land_blobs parity harness on a recorded fixture: a source repo whose ledger holds landing commits and whose
`scratch/warp/<run>/` holds each run's record is replayed through the registered op, and the harness reports a
match, a miss with its reason, or a named skip."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_blobs_parity as parity
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

RUN_1 = "ask-20261114T100000-aaaaaa"
RUN_2 = "ask-20261114T100100-bbbbbb"
RUN_3 = "ask-20261114T100200-cccccc"
#: `main` calls `declare_benchmark_origin`, which writes this variable; the CLI tests pin it first so teardown restores it.
ORIGIN_ENV = "COORDINATOR_INVOCATION_ORIGIN"


def _git(args, cwd: Path, stdin: bytes = None) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), input=stdin, capture_output=True, check=True,
                          **no_console_creationflags()).stdout.decode().strip()


@pytest.fixture
def src(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "src"
    root.mkdir()
    _git(["init", "-q"], root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("core.autocrlf", "false"),
                       ("commit.gpgsign", "false")):
        _git(["config", key, value], root)
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "pkg" / "gone.py").write_text("g = 1\n", encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _land(root: Path, run_id: str, declared: list, *, record: bool = True, extra_commit: list = (),
          trailers: bool = True) -> str:
    """Commit the current worktree state of `declared` (+ `extra_commit`) as a landing of `run_id`; write the run's
    record. Returns the commit sha."""
    for rel in [*declared, *extra_commit]:
        if (root / rel).exists():
            _git(["add", "--", rel], root)
        else:
            _git(["rm", "-q", "--cached", "--", rel], root)
    message = f"C1: landing of {run_id}\n"
    if trailers:
        message += (f"\nLands-Chunks: C1\nInline-Review: applies {run_id}.review-wave-bookkeeping -- "
                    "execute-review: 1 slices, 0 fixes\n")
    _git(["commit", "-q", "-m", message], root)
    if record:
        rec = root / "scratch" / "warp" / run_id
        rec.mkdir(parents=True, exist_ok=True)
        request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=tuple(declared)),))
        (rec / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    return _git(["rev-parse", "HEAD"], root)


def _run(src: Path, tmp_path: Path, last: int) -> parity.Report:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    return parity.run_parity(src, last, workdir)


def _landings(src: Path) -> None:
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    (src / "pkg" / "new.py").write_text("n = 1\n", encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py", "pkg/new.py"])
    (src / "pkg" / "gone.py").unlink()
    _land(src, RUN_2, ["pkg/gone.py"])


def test_each_recorded_landing_replays_to_the_recorded_tree_sha(src, tmp_path):
    _landings(src)

    report = _run(src, tmp_path, 2)

    assert report.summary() == "2/2" and report.ok, parity.render(report)
    assert [r.run_id for r in report.results] == [RUN_2, RUN_1]
    for r in report.results:
        assert r.recorded["tree"] == r.replayed["tree"] != ""
    assert not (tmp_path / "work" / f"fx-{report.results[0].sha[:10]}").exists()


def test_a_replay_leaves_the_source_repo_head_and_worktree_alone(src, tmp_path):
    _landings(src)
    head = _git(["rev-parse", "HEAD"], src)

    _run(src, tmp_path, 2)

    assert _git(["rev-parse", "HEAD"], src) == head
    assert _git(["status", "--porcelain", "--untracked-files=no"], src) == ""


def test_a_landing_whose_commit_also_carries_an_undeclared_path_passes_in_the_bookkeeping_column(src, tmp_path):
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    (src / "bookkeeping.txt").write_text("receipt\n", encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py"], extra_commit=["bookkeeping.txt"])

    report = _run(src, tmp_path, 1)

    [hit] = report.results
    assert report.ok and report.matched == 1, parity.render(report)
    assert report.breakdown() == {"strict": 0, "bookkeeping_only": 1, "improved": 0, "miss": 0}
    assert hit.status == "match-bookkeeping" and hit.differing_paths == ["bookkeeping.txt"]
    assert hit.bookkeeping == ["bookkeeping.txt"] and hit.declared_match is True
    assert "tree differs" in hit.reason and "product set identical" in hit.reason
    assert "bookkeeping: bookkeeping.txt" in parity.render(report)


def _record_foreign_blob(src: Path) -> str:
    """Record, in RUN_1's `kept.json`, a blob for `pkg/a.py` other than the one the landing carries."""
    other = _git(["hash-object", "-w", "--stdin"], src, stdin=b"a = 'someone else'\n")
    kept = src / "state" / "warp" / RUN_1
    kept.mkdir(parents=True)
    (kept / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": {"pkg/a.py": other}}}}), encoding="utf-8")
    return other


def _declare_as_other_run(src: Path, run_id: str, paths: list) -> None:
    rec = src / "scratch" / "warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=tuple(paths)),))
    (rec / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")


def test_a_foreign_hunk_refusal_no_other_run_declares_is_a_miss_naming_the_paths(src, tmp_path):
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    sha = _land(src, RUN_1, ["pkg/a.py"])
    _record_foreign_blob(src)

    report = _run(src, tmp_path, 1)

    [miss] = report.results
    assert miss.status == "miss" and miss.sha == sha and miss.foreign_mapping == {}
    assert miss.replayed == {"kind": "refused", "refused": "foreign-hunk"}
    assert "op refused (foreign-hunk)" in miss.reason and "no other run declares: pkg/a.py" in miss.reason
    assert report.matched == 0 and not report.ok


def test_a_foreign_hunk_refusal_another_run_declares_is_improved_legacy_foreign_hunk(src, tmp_path):
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py"])
    _record_foreign_blob(src)
    _declare_as_other_run(src, RUN_2, ["pkg/a.py", "pkg/other.py"])

    report = _run(src, tmp_path, 1)

    [hit] = report.results
    assert report.ok, parity.render(report)
    assert report.breakdown() == {"strict": 0, "bookkeeping_only": 0, "improved": 1, "miss": 0}
    assert hit.status == "improved" and hit.improved_class == "legacy-foreign-hunk"
    assert hit.foreign_mapping == {"pkg/a.py": [RUN_2]}
    assert hit.replayed == {"kind": "refused", "refused": "foreign-hunk"}
    assert "legacy-foreign-hunk" in hit.reason and RUN_2 in hit.reason


def test_a_run_declaring_a_different_path_does_not_explain_the_foreign_refusal(src, tmp_path):
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py"])
    _record_foreign_blob(src)
    _declare_as_other_run(src, RUN_2, ["pkg/other.py"])
    _declare_as_other_run(src, RUN_1, ["pkg/a.py"])

    report = _run(src, tmp_path, 1)

    assert report.results[0].status == "miss" and not report.ok


def test_a_landing_whose_recorded_blob_is_the_landed_blob_replays_through_the_guard(src, tmp_path):
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py"])
    own = _git(["rev-parse", "HEAD:pkg/a.py"], src)
    kept = src / "state" / "warp" / RUN_1
    kept.mkdir(parents=True)
    (kept / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": {"pkg/a.py": own}}}}), encoding="utf-8")

    report = _run(src, tmp_path, 1)

    assert report.ok, parity.render(report)


def test_a_landing_with_no_run_record_is_skipped_by_name_and_the_scan_goes_past_it(src, tmp_path):
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py"])
    (src / "pkg" / "a.py").write_text("a = 3\n", encoding="utf-8")
    unrecorded = _land(src, RUN_2, ["pkg/a.py"], record=False)

    report = _run(src, tmp_path, 1)

    assert report.ok and [r.run_id for r in report.results] == [RUN_1]
    assert [(s.run_id, s.sha) for s in report.skipped] == [(RUN_2, unrecorded)]
    assert "commit-request.txt" in report.skipped[0].reason


def test_fewer_replayable_landings_than_asked_is_not_ok(src, tmp_path):
    _landings(src)

    report = _run(src, tmp_path, 5)

    assert report.matched == 2 and not report.ok and report.summary() == "2/5"


def test_a_commit_without_the_landing_trailer_is_not_a_landing(src, tmp_path):
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    _land(src, RUN_3, ["pkg/a.py"], trailers=False)

    assert parity.select_landings(src, 10) == []


def test_the_run_is_named_by_the_inline_review_stem_and_the_base_by_the_closed_trailer(src):
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    _git(["add", "."], src)
    base = _git(["rev-parse", "HEAD"], src)
    _git(["commit", "-q", "-m", f"C1: x\n\nLands-Chunks: C1\nCheckpoint-Run-Closed: {base}\nInline-Review: applies "
          f"{RUN_1}.review-wave-bookkeeping -- execute-review: 3 slices, 2 fixes\n"], src)

    [landing] = parity.select_landings(src, 10)

    assert (landing.run_id, landing.run_base, landing.slices, landing.fixes) == (RUN_1, base, 3, 2)
    assert landing.parent == base


@pytest.mark.parametrize("recorded,replayed,match", [
    ({"kind": "landed", "tree": "t1"}, {"kind": "landed", "tree": "t1"}, True),
    ({"kind": "landed", "tree": "t1"}, {"kind": "landed", "tree": "t2"}, False),
    ({"kind": "refused", "refused": "secret-in-diff"}, {"kind": "refused", "refused": "secret-in-diff"}, True),
    ({"kind": "refused", "refused": "secret-in-diff"}, {"kind": "refused", "refused": "foreign-hunk"}, False),
    ({"kind": "landed", "tree": "t1"}, {"kind": "refused", "refused": "foreign-hunk"}, False),
    ({"kind": "refused", "refused": "unreviewed"}, {"kind": "landed", "tree": "t1"}, False),
    ({"kind": "landed", "tree": "t1"}, {"kind": "nothing-to-commit"}, False),
    ({"kind": "landed", "tree": "t1"}, {"kind": "error", "error": "boom"}, False),
])
def test_compare_matches_only_the_same_tree_or_the_same_refusal_kind(recorded, replayed, match):
    assert parity.compare(recorded, replayed)[0] is match


def test_a_secret_in_the_diff_refuses_in_replay_and_compares_as_the_recorded_refusal(src, tmp_path):
    key = "-----BEGIN " + "RSA PRIVATE KEY-----"
    (src / "pkg" / "a.py").write_text(f'KEY = """{key}"""\n', encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py"])

    report = _run(src, tmp_path, 1)

    [miss] = report.results
    assert miss.replayed == {"kind": "refused", "refused": "secret-in-diff"}
    assert miss.status == "miss" and "recorded landing committed" in miss.reason
    assert parity.compare({"kind": "refused", "refused": "secret-in-diff"}, miss.replayed)[0] is True


def test_the_cli_reports_json_and_exits_zero_on_full_parity(src, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(ORIGIN_ENV, "benchmark")
    _landings(src)

    code = parity.main(["--last", "2", "--source-repo", str(src), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["summary"] == "2/2" and out["ok"] is True and out["skipped"] == []
    assert not list((src / "scratch").glob("land-blobs-parity-*"))


def test_the_cli_exits_two_when_a_landing_misses(src, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(ORIGIN_ENV, "benchmark")
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py"])
    _record_foreign_blob(src)

    code = parity.main(["--last", "1", "--source-repo", str(src)])

    assert code == 2
    assert "miss" in capsys.readouterr().out


def test_the_cli_exits_zero_when_every_landing_is_bookkeeping_only_or_improved(src, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(ORIGIN_ENV, "benchmark")
    (src / "pkg" / "a.py").write_text("a = 2\n", encoding="utf-8")
    (src / "extra.txt").write_text("e\n", encoding="utf-8")
    _land(src, RUN_1, ["pkg/a.py"], extra_commit=["extra.txt"])

    code = parity.main(["--last", "1", "--source-repo", str(src), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["breakdown"]["bookkeeping_only"] == 1 and out["results"][0]["bookkeeping"] == ["extra.txt"]


# --- the worktree-sync case ------------------------------------------------------------------------------

BIG = "pkg/big.py"


def _big(**edits: str) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, 121)]
    for line, text in edits.items():
        rows[int(line[1:]) - 1] = text
    return "\n".join(rows) + "\n"


def _big_landing(src: Path) -> None:
    (src / BIG).write_bytes(_big().encode())
    _git(["add", "."], src)
    _git(["commit", "-q", "-m", "big seed"], src)
    (src / BIG).write_bytes(_big(l115="x_115 = 'OWN'").encode())
    _land(src, RUN_1, [BIG])


def _peer_edits_replay_worktree(monkeypatch, text: str) -> None:
    """A peer edit of the replay worktree in the window after the ref moves and before the sync."""
    from coordinator_core.ops.dispatch_emit import land_blobs

    real = land_blobs._publish

    def publish(root, *args, **kwargs):
        out = real(root, *args, **kwargs)
        (Path(root) / BIG).write_bytes(text.encode())
        return out

    monkeypatch.setattr(land_blobs, "_publish", publish)


def test_a_clean_replay_reports_no_worktree_sync_dirt(src, tmp_path):
    _big_landing(src)

    report = _run(src, tmp_path, 1)

    [hit] = report.results
    assert report.ok, parity.render(report)
    assert (hit.worktree_rebased, hit.worktree_sync_refused, hit.worktree_gap) == ([], [], [])


def test_peer_dirt_rebased_onto_the_landed_blob_is_still_a_match_and_is_named(src, tmp_path, monkeypatch):
    _big_landing(src)
    _peer_edits_replay_worktree(monkeypatch, _big(l5="x_5 = 'PEER'"))

    report = _run(src, tmp_path, 1)

    [hit] = report.results
    assert report.ok, parity.render(report)
    assert hit.worktree_rebased == [BIG] and hit.worktree_sync_refused == [] and hit.worktree_gap == []


def test_a_conflicting_worktree_is_a_miss_even_though_the_tree_sha_matches(src, tmp_path, monkeypatch):
    _big_landing(src)
    _peer_edits_replay_worktree(monkeypatch, _big(l115="x_115 = 'PEER'"))

    report = _run(src, tmp_path, 1)

    [miss] = report.results
    assert miss.status == "miss" and not report.ok
    assert miss.recorded["tree"] == miss.replayed["tree"]
    assert miss.worktree_sync_refused == [BIG]
    assert "landed but worktree sync refused" in miss.reason and BIG in miss.reason


def test_a_landed_path_that_differs_from_head_with_no_report_is_a_gap(src):
    (src / BIG).write_bytes(_big().encode())
    _git(["add", "."], src)
    _git(["commit", "-q", "-m", "big"], src)
    (src / BIG).write_bytes(_big(l5="x_5 = 'DIRT'").encode())
    reply = {"committed": True, "changed_paths": [BIG]}

    state = parity.worktree_sync_state(src, reply)

    assert state == {"rebased": [], "refused": [], "gap": [BIG]}
    assert "unreported" in parity.worktree_miss_reason(state)
    assert parity.worktree_sync_state(src, {**reply, "worktree_rebased": [BIG]})["gap"] == []
    assert parity.worktree_miss_reason(parity.worktree_sync_state(src, {**reply, "worktree_rebased": [BIG]})) == ""
    assert parity.worktree_sync_state(src, {"committed": False}) == {"rebased": [], "refused": [], "gap": []}
