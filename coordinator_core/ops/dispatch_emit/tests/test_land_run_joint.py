"""land_run joint mode: one commit over a co-tenant cycle's union, one test leg, a Joint-Land trailer per run."""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, halted_run_landed, halted_withheld_index
from coordinator_core.ops.dispatch_emit import land_run_joint as joint
from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

A = "ask-20261121T100000-aaaaaa"
B = "ask-20261121T101000-bbbbbb"
SHARED = "bin/shared.py"
BASE = "".join(f"line{i}\n" for i in range(1, 9))


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, input=stdin, **no_console_creationflags(),
    )
    return done.stdout.decode("utf-8")


def _blob(repo: Path, text: str) -> str:
    return _git(repo, "hash-object", "-w", "--stdin", stdin=text.encode("utf-8")).strip()


def _edit(text: str, line: int, to: str) -> str:
    rows = text.splitlines(keepends=True)
    rows[line - 1] = to + "\n"
    return "".join(rows)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@t")
    monkeypatch.setattr(halted_run_landed, "_launch", lambda root, run_id: True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "bin").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    (root / SHARED).write_text(BASE, encoding="utf-8", newline="\n")
    (root / "a.py").write_text("a0\n", encoding="utf-8", newline="\n")
    (root / "b.py").write_text("b0\n", encoding="utf-8", newline="\n")
    (root / "bin/tests").mkdir()
    (root / "bin/tests/test_shared.py").write_text("def test_ok():\n    pass\n", encoding="utf-8", newline="\n")
    _git(root, "add", "--", SHARED, "a.py", "b.py", "bin/tests/test_shared.py")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _halt(root: Path, run_id: str, own: dict, own_shared: str, kept_shared: str | None = None) -> None:
    """A halted co_tenant_wait run withholding SHARED plus its own file, with its own-delta image of SHARED."""
    rd = root / "state/warp" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    head = _git(root, "rev-parse", "HEAD").strip()
    halted_run.write_halt_marker(rd, halted="co_tenant_wait", sizing_rel=None, run_base_sha=head)
    withheld = [SHARED, *own]
    halted_run.record_withheld(rd, {p: "hunks" for p in withheld})
    kept = {SHARED: _blob(root, kept_shared or own_shared), **{p: _blob(root, t) for p, t in own.items()}}
    (rd / "kept.json").write_text(json.dumps({"chunks": {"X1": {"paths": kept}}, "plan_gate": None}), encoding="utf-8")
    (rd / "manifest.json").write_text(
        json.dumps({"write_baseline": {SHARED: _blob(root, BASE), **{p: _blob(root, _git(root, "show", f"HEAD:{p}")) for p in own}}}),
        encoding="utf-8",
    )
    deltas = {SHARED: {"blob": _blob(root, own_shared)}, **{p: {"blob": _blob(root, t)} for p, t in own.items()}}
    (rd / WITHHELD_OWN_DELTA_FILE).write_text(json.dumps({"paths": deltas}), encoding="utf-8")
    halted_withheld_index.add_run(root, run_id, withheld)


def _pair(root: Path, a_text: str, b_text: str, worktree: str, kept_a: str | None = None, kept_b: str | None = None) -> None:
    _halt(root, A, {"a.py": "a1\n"}, a_text, kept_a)
    _halt(root, B, {"b.py": "b1\n"}, b_text, kept_b)
    (root / SHARED).write_text(worktree, encoding="utf-8", newline="\n")
    (root / "a.py").write_text("a1\n", encoding="utf-8", newline="\n")
    (root / "b.py").write_text("b1\n", encoding="utf-8", newline="\n")


@pytest.fixture
def leg(monkeypatch):
    calls: list = []

    def fake(root, run_id, union, seeds):
        calls.append((run_id, list(union), list(seeds)))
        return calls_verdict[0], "detail"

    calls_verdict = ["green"]
    monkeypatch.setattr(joint, "run_leg", fake)
    fake.calls, fake.verdict = calls, calls_verdict
    return fake


def _land(root: Path, ids: list) -> dict:
    return joint.land(root, root, {"joint": ids})


def test_cycle_lands_as_one_commit_with_a_trailer_per_run_and_composes_the_shared_path(repo, leg):
    a_text, b_text = _edit(BASE, 1, "A-ONE"), _edit(BASE, 8, "B-EIGHT")
    both = _edit(a_text, 8, "B-EIGHT")
    _pair(repo, a_text, b_text, both)
    reply = _land(repo, [B, A])
    assert reply["committed"] is True and not reply["partial"], reply
    assert {r["run_id"] for r in reply["landed"]} == {A, B}
    assert _git(repo, "log", "--format=%H", f"--grep=Joint-Land: {A}").split() == [reply["sha"]]
    sha = reply["sha"]
    message = _git(repo, "log", "-1", "--format=%B", sha)
    assert f"Joint-Land: {A}" in message and f"Joint-Land: {B}" in message
    assert _git(repo, "show", f"{sha}:{SHARED}").replace("\r\n", "\n") == both
    assert _git(repo, "show", f"{sha}:a.py").strip() == "a1" and _git(repo, "show", f"{sha}:b.py").strip() == "b1"
    assert len(leg.calls) == 1 and leg.calls[0][0] == f"{A}+{B}"
    assert {SHARED, "a.py", "b.py"} <= set(leg.calls[0][1]) and leg.calls[0][2] == ["bin/tests/test_shared.py"]
    assert halted_run._read(ask_contract.record_dir(repo, A))["status"] == "landed"
    assert halted_run._read(ask_contract.record_dir(repo, B))["status"] == "landed"


def test_composed_blob_is_head_plus_own_deltas_never_a_foreign_worktree_hunk(repo, leg):
    a_text, b_text = _edit(BASE, 1, "A-ONE"), _edit(BASE, 8, "B-EIGHT")
    foreign = _edit(_edit(_edit(BASE, 1, "A-ONE"), 8, "B-EIGHT"), 4, "FOREIGN")
    _pair(repo, a_text, b_text, foreign, kept_a=foreign, kept_b=foreign)
    reply = _land(repo, [A, B])
    assert reply["committed"] is True, reply
    committed = _git(repo, "show", f"HEAD:{SHARED}").replace("\r\n", "\n")
    assert "FOREIGN" not in committed and "A-ONE" in committed and "B-EIGHT" in committed
    assert SHARED in reply["composed"]


def test_overlapping_hunk_refuses_typed_before_any_export_or_commit(repo, leg):
    a_text, b_text = _edit(BASE, 3, "A-THREE"), _edit(BASE, 3, "B-THREE")
    _pair(repo, a_text, b_text, a_text)
    head = _git(repo, "rev-parse", "HEAD").strip()
    reply = _land(repo, [A, B])
    assert reply["committed"] is False and reply["refused"] == joint.REFUSED_OVERLAP
    assert SHARED in reply["error"] and B in reply["error"] and A in reply["error"]
    assert leg.calls == [] and _git(repo, "rev-parse", "HEAD").strip() == head
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""


def test_runs_that_are_not_a_cycle_are_refused_typed(repo, leg):
    _pair(repo, _edit(BASE, 1, "A"), _edit(BASE, 8, "B"), BASE)
    (repo / "state/warp" / B / "halted.json").unlink()
    halted_withheld_index.remove_run(repo, B)
    reply = _land(repo, [A, B])
    assert reply["refused"] == joint.REFUSED_NOT_CYCLE and reply["committed"] is False
    assert leg.calls == []


def test_three_runs_sharing_one_path_confirm_as_one_cycle(repo, leg):
    c = "ask-20261121T102000-cccccc"
    _pair(repo, _edit(BASE, 1, "A"), _edit(BASE, 8, "B"), BASE)
    _halt(repo, c, {"a.py": "a1\n"}, _edit(BASE, 5, "C"))
    assert joint.confirm_cycle(repo, [A, B, c]) is True
    assert joint.confirm_cycle(repo, [A, B, c, "ask-20261121T103000-dddddd"]) is False


@pytest.mark.parametrize("ids", [None, "x", [], [A], [A, A], [A, "../escape"]])
def test_malformed_ids_are_refused_typed(repo, leg, ids):
    reply = joint.land(repo, repo, {"joint": ids})
    assert reply["refused"] == joint.REFUSED_IDS and reply["committed"] is False


def test_red_leg_refuses_and_commits_nothing(repo, leg):
    _pair(repo, _edit(BASE, 1, "A"), _edit(BASE, 8, "B"), _edit(_edit(BASE, 1, "A"), 8, "B"))
    leg.verdict[0] = "red"
    head = _git(repo, "rev-parse", "HEAD").strip()
    reply = _land(repo, [A, B])
    assert reply["refused"] == joint.REFUSED_LEG_RED and reply["committed"] is False
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""


def test_pre_existing_leg_lands_with_an_advisory(repo, leg):
    _pair(repo, _edit(BASE, 1, "A"), _edit(BASE, 8, "B"), _edit(_edit(BASE, 1, "A"), 8, "B"))
    leg.verdict[0] = "pre_existing"
    reply = _land(repo, [A, B])
    assert reply["committed"] is True and reply["advisory"]


def test_a_run_left_partial_fails_the_landing(repo, leg, monkeypatch):
    _pair(repo, _edit(BASE, 1, "A"), _edit(BASE, 8, "B"), _edit(_edit(BASE, 1, "A"), 8, "B"))
    from coordinator_core.ops.dispatch_emit import combined_landing

    monkeypatch.setattr(combined_landing, "_unkept_at_head", lambda root, run_id: ["x.py"] if run_id == B else [])
    monkeypatch.setattr(combined_landing, "_classify", lambda root, run_id, declared: (None, ["x.py"] if run_id == B else []))
    reply = _land(repo, [A, B])
    assert reply["committed"] is True and [p["run_id"] for p in reply["partial"]] == [B]
    assert "partial" in reply["error"] and reply["incomplete_reasons"]


def test_peer_staged_path_outside_the_union_refuses_without_staging_anything(repo, leg):
    _pair(repo, _edit(BASE, 1, "A"), _edit(BASE, 8, "B"), _edit(_edit(BASE, 1, "A"), 8, "B"))
    (repo / "peer.py").write_text("p\n", encoding="utf-8")
    _git(repo, "add", "--", "peer.py")
    reply = _land(repo, [A, B])
    assert reply["refused"] == joint.REFUSED_INDEX
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["peer.py"]


class _Proc:
    def __init__(self, code: int, out: str = ""):
        self.returncode, self.stdout, self.stderr = code, out, ""


def _stub_export(monkeypatch, outcomes: list):
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    seen: list = []

    @contextlib.contextmanager
    def export(root, run_id, paths, leg=None, argv=()):
        seen.append((run_id, tuple(paths), tuple(argv)))
        yield Path(root)

    monkeypatch.setattr(cov, "clean_export", export)
    monkeypatch.setattr(joint, "_pytest_in", lambda root, dest, slug, seeds: outcomes.pop(0))
    return seen


def test_run_leg_one_export_over_the_union_and_green(monkeypatch, tmp_path):
    seen = _stub_export(monkeypatch, [_Proc(0, "1 passed")])
    assert joint.run_leg(tmp_path, "A+B", ["a.py", "b.py"], ["tests/test_x.py"])[0] == "green"
    assert [s[:2] for s in seen] == [("A+B", ("a.py", "b.py"))]


def test_run_leg_red_whose_ids_all_fail_at_head_is_pre_existing(monkeypatch, tmp_path):
    fail = "FAILED tests/test_x.py::test_a - boom\n1 failed"
    seen = _stub_export(monkeypatch, [_Proc(1, fail), _Proc(1, fail)])
    assert joint.run_leg(tmp_path, "A+B", ["a.py"], ["tests/test_x.py"])[0] == "pre_existing"
    assert len(seen) == 2 and seen[1][1] == ()


def test_run_leg_red_with_an_id_that_passes_at_head_stays_red(monkeypatch, tmp_path):
    fail = "FAILED tests/test_x.py::test_a - boom\n1 failed"
    _stub_export(monkeypatch, [_Proc(1, fail), _Proc(0, "1 passed")])
    assert joint.run_leg(tmp_path, "A+B", ["a.py"], ["tests/test_x.py"])[0] == "red"
