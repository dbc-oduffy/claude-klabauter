"""foreign_hunks against a throwaway git repo with two fake run manifests."""
import json
import subprocess

import pytest

from coordinator_core.landing_audit.foreign_hunks import audit, landings, run_manifest_writes

pytestmark = pytest.mark.spawns_process

RUN_A = "ask-20261029T010101-aaaaaa"
RUN_B = "ask-20261029T020202-bbbbbb"


def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
                   capture_output=True)


def _manifest(repo, run, writes, docs=()):
    d = repo / "state" / "warp" / run
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"rows": [{"writes": writes}], "doc_writes": list(docs)}))


def _put(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _put(tmp_path, "seed.txt", "x\n")
    _manifest(tmp_path, RUN_A, ["a/one.py"])
    _manifest(tmp_path, RUN_B, ["b/two.py"], docs=["docs/b.md"])
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def test_declared_path_is_not_foreign_and_undeclared_takes_owner_from_other_manifest(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "b/two.py", "1\n2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"landing for {RUN_B}\n\nLands-Chunks: C1")
    _put(repo, "a/one.py", "1\n")
    _put(repo, "b/two.py", "1\n2\n3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"landing for {RUN_A}\n\nLands-Chunks: C1")
    res = audit(repo, "2000-01-01")
    top = res["landings"][0]
    assert top["run_id"] == RUN_A
    assert [f["path"] for f in top["foreign"]] == ["b/two.py"]
    assert top["foreign"][0]["likely_owner"] == RUN_B
    assert top["foreign"][0]["hunks"] == [[3, 1]]
    assert res["landings"][1]["foreign"] == []


def test_unowned_foreign_path_is_unknown(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "a/one.py", "1\n")
    _put(repo, "stray.txt", "s\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"landing for {RUN_A}\n\nLands-Chunks: C1")
    foreign = audit(repo, "2000-01-01")["landings"][0]["foreign"]
    assert [(f["path"], f["likely_owner"]) for f in foreign] == [("stray.txt", "unknown")]


def test_landing_without_run_record_lists_null_run_and_every_path_foreign(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "a/one.py", "1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "landing\n\nLands-Chunks: C1")
    top = audit(repo, "2000-01-01")["landings"][0]
    assert top["run_id"] is None
    assert [(f["path"], f["likely_owner"]) for f in top["foreign"]] == [("a/one.py", "unknown")]


def test_run_manifest_writes_and_row_unlanded_id_is_not_owner(tmp_path):
    repo = _repo(tmp_path)
    assert run_manifest_writes(repo, RUN_B) == {"b/two.py", "docs/b.md"}
    assert run_manifest_writes(repo, "ask-20261029T030303-cccccc") == set()
    _put(repo, "a/one.py", "1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"landing\n\nRow-Unlanded: C3 x withheld by {RUN_B}\nLands-Chunks: C1")
    ld = landings(repo, "2000-01-01")
    assert ld[0].run_id is None
    assert ld[0].hunks == {"a/one.py": [(1, 1)]}


def test_audit_is_read_only(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "a/one.py", "1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"landing for {RUN_A}\n\nLands-Chunks: C1")
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    audit(repo, "2000-01-01")
    after = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert before == after


def test_owner_with_no_older_cover_falls_back_to_nearest_newer_run(tmp_path):
    repo = _repo(tmp_path)
    run_c = "ask-20261029T030303-cccccc"
    _manifest(repo, run_c, ["shared.txt"])
    _put(repo, "shared.txt", "s\n")
    _put(repo, "a/one.py", "1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed2\n\nLands-Chunks: C0")
    _manifest_extra = json.loads((repo / "state/warp" / RUN_A / "manifest.json").read_text())
    _manifest_extra["rows"][0]["writes"].append("shared.txt")
    (repo / "state/warp" / RUN_A / "manifest.json").write_text(json.dumps(_manifest_extra))
    _put(repo, "shared.txt", "s\nt\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"landing for {RUN_B}\n\nLands-Chunks: C1")
    _put(repo, "a/one.py", "2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"landing for {RUN_A}\n\nLands-Chunks: C1")
    _put(repo, "c.txt", "c\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"landing for {run_c}\n\nLands-Chunks: C1")
    res = audit(repo, "2000-01-01")
    by_run = {e["run_id"]: e for e in res["landings"]}
    b_foreign = {f["path"]: f["likely_owner"] for f in by_run[RUN_B]["foreign"]}
    assert b_foreign["shared.txt"] == RUN_A
