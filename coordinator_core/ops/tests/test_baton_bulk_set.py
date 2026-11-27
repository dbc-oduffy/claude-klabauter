"""End-to-end tests for the baton.bulk_set handler on a tmp_path git repo, plus the 809-baton budget.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_baton_bulk_set.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops import baton_bulk_set as op
from coordinator_core.ops.baton_bulk_set_contract import OP_KEY, RESULT_KEYS
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_TEMPLATE: list[Path] = []


@pytest.fixture(scope="module", autouse=True)
def _repo_template(tmp_path_factory):
    tpl = tmp_path_factory.mktemp("bulk-tpl") / "repo"
    tpl.mkdir()
    _git(tpl, "init")
    _git(tpl, "config", "commit.gpgsign", "false")
    (tpl / "README.md").write_text("init\n", encoding="utf-8")
    _git(tpl, "add", "README.md")
    _git(tpl, "commit", "-m", "init")
    _TEMPLATE.append(tpl)
    yield
    _TEMPLATE.clear()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_TEMPLATE[0], repo, dirs_exist_ok=True)


_H = "state/handoffs/"
_THREAD_CAPS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")


def _baton(deliverable_id: str | None, *, extra: str = "") -> str:
    lines = [
        "title: t",
        "created: 2026-10-01",
        "branch: work/test/2026-10-01",
        "predecessor: none",
        "category: infra",
        "summary: fixture baton",
        "status: open",
        "deployment_state: ready_to_fire",
    ]
    if deliverable_id:
        lines.append(f"deliverable_id: {deliverable_id}")
    return "---\n" + "\n".join(lines) + "\n" + extra + "---\n\nbody\n"


def _write(repo: Path, rel: str, text: str, newline: str = "\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", newline).encode("utf-8"))


def _fm(repo: Path, rel: str, key: str):
    split = split_frontmatter((repo / rel).read_bytes().decode("utf-8"))
    assert split is not None
    return read_fm_field_unquoted(split.fm_text, key)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _fixture(repo: Path) -> dict[str, str]:
    _init_repo(repo)
    _write(repo, _H + "a.md", _baton("dlv-a-000001"))
    _write(repo, _H + "b.md", _baton("dlv-b-000002"))
    _write(repo, _H + "crlf.md", _baton("dlv-c-000003"), newline="\r\n")
    _write(repo, _H + "nodlv.md", _baton(None))
    _write(repo, _H + "preset.md", _baton("dlv-p-000004", extra="owner_session: old\neta: 2026-01-01\n"))
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-m", "seed").returncode == 0
    return {p.stem: f"{_H}{p.name}" for p in (repo / "state/handoffs").glob("*.md")}


def _item(rel: str, owner: str = "sess-1", eta: str = "2026-12-01", **extra) -> dict:
    return {"path": rel, "owner_session": owner, "eta": eta, **extra}


def _call(repo: Path, items: list, **extra) -> dict:
    return asyncio.run(op._handler({"items": items, **extra}, repo))


def _reasons(res: dict) -> dict[str, str]:
    return {r["handoff_path"]: r["reason"] for r in res["refused"]}


def test_stamps_owner_and_eta_in_one_commit_preserving_other_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    before = {k: (repo / v).read_bytes() for k, v in paths.items()}
    head = _head(repo)

    res = _call(repo, [_item(paths["a"]), _item(paths["b"]), _item(paths["crlf"])])

    assert tuple(res) == RESULT_KEYS and res["op"] == OP_KEY and res["exit_code"] == 0
    assert len(res["applied"]) == 3 and res["unchanged"] == [] and res["refused"] == []
    assert res["dry_run"] is False
    assert res["commit_sha"] == _head(repo) != head
    assert _git(repo, "rev-parse", "HEAD~1").stdout.strip() == head
    assert res["pathspec"] == sorted([paths["a"], paths["b"], paths["crlf"]])
    for key in ("a", "b", "crlf"):
        assert _fm(repo, paths[key], "owner_session") == "sess-1"
        assert _fm(repo, paths[key], "eta") == "2026-12-01"
    a_after = (repo / paths["a"]).read_bytes().decode("utf-8")
    assert a_after.replace("owner_session: sess-1\neta: 2026-12-01\n", "") == before["a"].decode("utf-8")
    crlf_after = (repo / paths["crlf"]).read_bytes()
    assert crlf_after.count(b"\r\n") == crlf_after.count(b"\n")
    assert crlf_after.replace(b"owner_session: sess-1\r\neta: 2026-12-01\r\n", b"") == before["crlf"]
    dirty = {line[3:].strip() for line in _git(repo, "status", "--porcelain").stdout.splitlines()}
    assert paths["a"] not in dirty and paths["b"] not in dirty


def test_existing_owner_and_eta_are_replaced_in_place(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    before = (repo / paths["preset"]).read_text(encoding="utf-8")

    res = _call(repo, [_item(paths["preset"], owner="new-owner", eta="2027-02-03")])

    assert len(res["applied"]) == 1
    after = (repo / paths["preset"]).read_text(encoding="utf-8")
    assert after == before.replace("owner_session: old", "owner_session: new-owner").replace(
        "eta: 2026-01-01", "eta: 2027-02-03"
    )


def test_dry_run_lists_changes_and_writes_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    before = {k: (repo / v).read_bytes() for k, v in paths.items()}
    head = _head(repo)

    res = _call(repo, [_item(paths["a"]), _item(paths["b"])], dry_run=True)

    assert res["dry_run"] is True and res["commit_sha"] is None
    assert [a["handoff_path"] for a in res["applied"]] == [paths["a"], paths["b"]]
    assert _head(repo) == head
    assert {k: (repo / v).read_bytes() for k, v in paths.items()} == before


def test_repeat_call_is_a_noop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    items = [_item(paths["a"]), _item(paths["b"])]
    assert len(_call(repo, items)["applied"]) == 2
    head = _head(repo)
    snap = {k: (repo / v).read_bytes() for k, v in paths.items()}

    res = _call(repo, items)

    assert res["applied"] == [] and len(res["unchanged"]) == 2 and res["commit_sha"] is None
    assert res["pathspec"] == []
    assert _head(repo) == head
    assert {k: (repo / v).read_bytes() for k, v in paths.items()} == snap


def test_close_flips_terminal_in_same_commit_and_repeat_is_noop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    evidence = _head(repo)
    close = {"verdict": "shipped", "reason": "all acs ticked", "evidence": evidence}

    res = _call(repo, [_item(paths["a"], close=close), _item(paths["b"])])

    assert res["refused"] == [] and res["commit_sha"] == _head(repo)
    closed = {a["handoff_path"]: a["closed"] for a in res["applied"]}
    assert closed == {paths["a"]: True, paths["b"]: False}
    assert _fm(repo, paths["a"], "deployment_state") == "shipped"
    assert _fm(repo, paths["a"], "shipped_in") == evidence
    assert _fm(repo, paths["a"], "owner_session") == "sess-1"
    assert _fm(repo, paths["b"], "deployment_state") == "ready_to_fire"
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert sorted(committed) == sorted([paths["a"], paths["b"]])

    head = _head(repo)
    again = _call(repo, [_item(paths["a"], close=close), _item(paths["b"])])
    assert again["applied"] == [] and len(again["unchanged"]) == 2 and again["commit_sha"] is None
    assert _head(repo) == head


def test_close_with_same_owner_still_closes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    assert len(_call(repo, [_item(paths["a"])])["applied"]) == 1
    close = {"verdict": "shipped", "reason": "done", "evidence": _head(repo)}

    res = _call(repo, [_item(paths["a"], close=close)])

    assert [(a["handoff_path"], a["closed"]) for a in res["applied"]] == [(paths["a"], True)]
    assert _fm(repo, paths["a"], "deployment_state") == "shipped"
    assert res["commit_sha"] == _head(repo)


def test_dry_run_close_reports_would_close_and_writes_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    head = _head(repo)
    before = (repo / paths["a"]).read_bytes()
    close = {"verdict": "shipped", "reason": "r", "evidence": head}

    res = _call(repo, [_item(paths["a"], close=close)], dry_run=True)

    assert [(a["handoff_path"], a["closed"]) for a in res["applied"]] == [(paths["a"], True)]
    assert (repo / paths["a"]).read_bytes() == before and _head(repo) == head


def test_per_item_refusals_do_not_block_the_rest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    head = _head(repo)
    good = {"verdict": "shipped", "reason": "r", "evidence": head}
    items = [
        _item(paths["a"]),
        _item(paths["b"], close={"verdict": "shipped", "reason": "r"}),
        _item(paths["crlf"], close={**good, "evidence": "abc123"}),
        _item(paths["preset"], close={**good, "evidence": "f" * 40}),
        _item(paths["nodlv"], close=good),
        _item(_H + "missing.md"),
        _item(paths["a"].replace("a.md", "x.md"), eta="not-a-date"),
    ]

    res = _call(repo, items)

    reasons = _reasons(res)
    assert reasons[paths["b"]] == "close_without_evidence"
    assert reasons[paths["crlf"]] == "bad_evidence"
    assert reasons[paths["preset"]] == "bad_evidence"
    assert reasons[paths["nodlv"]] == "missing_deliverable_id"
    assert reasons[_H + "missing.md"] == "not_found"
    assert reasons[_H + "x.md"] == "bad_eta"
    assert [a["handoff_path"] for a in res["applied"]] == [paths["a"]]
    assert res["pathspec"] == [paths["a"]]
    assert _fm(repo, paths["b"], "owner_session") is None
    assert _fm(repo, paths["preset"], "owner_session") == "old"


def test_path_outside_the_repo_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _fixture(repo)
    outside = tmp_path / "outside.md"
    outside.write_text(_baton("dlv-o-000009"), encoding="utf-8")

    res = _call(repo, [_item("../outside.md")])

    assert res["applied"] == [] and list(_reasons(res).values()) == ["not_found"]
    assert "owner_session" not in outside.read_text(encoding="utf-8")


def test_blank_owner_is_refused_bad_owner_and_others_proceed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    before = (repo / paths["b"]).read_bytes()

    res = _call(repo, [_item(paths["a"]), _item(paths["b"], owner="  ")])

    assert _reasons(res) == {paths["b"]: "bad_owner"}
    assert [a["handoff_path"] for a in res["applied"]] == [paths["a"]]
    assert (repo / paths["b"]).read_bytes() == before


def test_trailing_comment_and_empty_value_survive_the_line_edit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    _write(repo, _H + "c.md", _baton("dlv-c-000005", extra="owner_session:\neta: 2026-01-01  # soon\n"))
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-m", "more").returncode == 0

    res = _call(repo, [_item(_H + "c.md", owner="o-2", eta="2026-03-04")])

    assert len(res["applied"]) == 1 and res["refused"] == []
    text = (repo / (_H + "c.md")).read_text(encoding="utf-8")
    assert "owner_session: o-2\n" in text and "eta: 2026-03-04  # soon\n" in text


def test_file_changed_between_read_and_write_is_refused_and_left_alone(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    racing = repo / paths["b"]
    real_span = op._frontmatter_span
    mutated: list[bytes] = []

    def span(data: bytes):
        if b"dlv-b-000002" in data and not mutated:
            racing.write_bytes(data + b"peer edit\n")
            mutated.append(racing.read_bytes())
        return real_span(data)

    monkeypatch.setattr(op, "_frontmatter_span", span)
    res = _call(repo, [_item(paths["a"]), _item(paths["b"])])

    assert _reasons(res) == {paths["b"]: "write_failed"}
    assert "changed since read" in res["refused"][0]["detail"]
    assert [a["handoff_path"] for a in res["applied"]] == [paths["a"]]
    assert racing.read_bytes() == mutated[0]
    assert res["pathspec"] == [paths["a"]]


_CHILD = r"""
import asyncio, json, subprocess, sys, time
from pathlib import Path
from coordinator_core.ops import baton_bulk_set as op

base, count = Path(sys.argv[1]), int(sys.argv[2])
H = "state/handoffs/"
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]

def seed(repo, n):
    (repo / H).mkdir(parents=True)
    subprocess.run(GIT + ["init", "-q"], cwd=repo, check=True)
    rels = []
    for i in range(n):
        rel = f"{H}b{i}.md"
        rels.append(rel)
        (repo / rel).write_bytes(
            f"---\ntitle: t\ncreated: 2026-10-01\nstatus: open\ndeployment_state: ready_to_fire\n"
            f"deliverable_id: dlv-{i:06d}\n---\n\nbody\n".encode()
        )
    subprocess.run(GIT + ["add", "-A"], cwd=repo, check=True)
    subprocess.run(GIT + ["commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return rels

repo, warm = base / "repo", base / "warm"
spare = [base / "spare1", base / "spare2"]
for d in [repo, warm, *spare]:
    d.mkdir()
rels = seed(repo, count)
for d in spare:
    seed(d, count)
warm_rels = seed(warm, 1)
asyncio.run(op._handler({"items": [{"path": warm_rels[0], "owner_session": "w", "eta": "2026-12-01"}]}, warm))
items = [{"path": rel, "owner_session": "sess-1", "eta": "2026-12-01"} for rel in rels]
spawns = []
real = subprocess.Popen
def counting(*a, **k):
    spawns.append(1)
    return real(*a, **k)
subprocess.Popen = counting
runs = []
for target in (*spare, repo):
    t0 = time.process_time()
    res = asyncio.run(op._handler({"items": items}, target))
    runs.append(time.process_time() - t0)
print(json.dumps({"process_s": min(runs), "spawns": len(spawns), "exit_code": res["exit_code"],
                  "applied": len(res["applied"]), "refused": len(res["refused"]), "sha": res["commit_sha"]}))
"""


def test_809_batons_stamp_in_one_commit_under_the_brightline_with_zero_spawns() -> None:
    tmp_path = Path(tempfile.mkdtemp(prefix="bulk809-", dir=os.environ.get("TEMP") or None))
    try:
        _check_809(tmp_path)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _check_809(tmp_path: Path) -> None:
    count = 809
    root = Path(__file__).resolve().parents[3]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MACHINE_LOCAL_COORDINATOR_", "COORDINATOR_"))}
    env.update({"PYTHONPATH": str(root), **{k: "1" for k in _THREAD_CAPS}})
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")

    # trap: the child builds its own fixture (files the pytest process wrote itself opened several
    # times slower for a later opener) and reports the best of three runs (process time swings ~30% on a loaded box).
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path), str(count)],
        capture_output=True, text=True, env=env, cwd=str(root),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    repo = tmp_path / "repo"
    assert got["exit_code"] == 0 and got["applied"] == count and got["refused"] == 0
    assert got["spawns"] == 0
    assert got["process_s"] < 0.5, f"{got['process_s']:.3f}s process time for {count} batons"
    assert got["sha"] == _head(repo)
    seed_sha = _git(repo, "rev-parse", "HEAD~1").stdout.strip()
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "2" and seed_sha
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert sorted(committed) == sorted(f"{_H}b{i}.md" for i in range(count))
    assert _fm(repo, f"{_H}b{count - 1}.md", "owner_session") == "sess-1"
    assert _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip() == ""


def test_index_splice_does_not_rehash_the_files_the_op_just_wrote(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.git import index_write

    repo = tmp_path / "repo"
    paths = _fixture(repo)
    rehashed: list[str] = []
    real = index_write._worktree_matches_blob
    monkeypatch.setattr(index_write, "_worktree_matches_blob", lambda f, *a: rehashed.append(f.name) or real(f, *a))

    res = _call(repo, [_item(paths["a"]), _item(paths["b"])])

    assert res["exit_code"] == 0 and len(res["applied"]) == 2
    assert rehashed == []
    monkeypatch.undo()
    assert _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip() == ""


def test_splice_still_rehashes_a_path_whose_stat_moved_after_the_op_hashed_it(tmp_path: Path) -> None:
    from coordinator_core.git import index_write

    repo = tmp_path / "repo"
    paths = _fixture(repo)
    rel = paths["a"]
    st = (repo / rel).stat()
    sha = _git(repo, "rev-parse", f"HEAD:{rel}").stdout.strip()
    calls: list[str] = []
    real = index_write._worktree_matches_blob

    def spy(f, *a):
        calls.append(Path(f).name)
        return real(f, *a)

    index_write._worktree_matches_blob = spy
    try:
        index_write.prepare_splice(
            repo, {rel: (0o100644, sha)}, trusted_stats={rel: (st.st_size, st.st_mtime_ns + 1)}
        ).abort()
        assert calls == ["a.md"]
        index_write.prepare_splice(
            repo, {rel: (0o100644, sha)}, trusted_stats={rel: (st.st_size, st.st_mtime_ns)}
        ).abort()
        assert calls == ["a.md"]
    finally:
        index_write._worktree_matches_blob = real


def test_empty_items_is_an_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _fixture(repo)
    assert _call(repo, [])["exit_code"] == 1
