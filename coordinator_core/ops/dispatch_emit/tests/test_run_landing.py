"""`run_landing.land_run` engine behaviour the parity suite does not pin: the in-process writer's
shapes (check-in form, modes, deletions, fallbacks) and its zero-spawn contract."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_landing, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_landing_parity import (
    _fire,
    _git,
    _head,
    _repo,
    _review,
)
from pathlib import Path

from coordinator_core.ops.dispatch_emit.tests import test_register_gate as trg
from coordinator_core.ops.dispatch_emit.tests.test_register_gate import wb_repo  # noqa: F401 -- fixture

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _repo(tmp_path, monkeypatch)


def _blob(root, rel: str) -> bytes:
    return subprocess.run(["git", "cat-file", "blob", f"HEAD:{rel}"], cwd=str(root), capture_output=True,
                          check=True).stdout


def _fsck_clean(root) -> None:
    assert subprocess.run(["git", "fsck", "--no-dangling", "--no-progress"], cwd=str(root),
                          capture_output=True).returncode == 0
    assert _git(["status", "--porcelain", "--untracked-files=no"], root) == ""


def test_a_plain_landing_spawns_no_git_process(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    real = subprocess.Popen

    def no_spawn(*args, **kwargs):
        raise AssertionError(f"land_run spawned {args[0]!r}")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    try:
        out = _fire("new", repo, {"C1": ["a.py"]})
    finally:
        monkeypatch.setattr(subprocess, "Popen", real)

    assert out["committed"] is True, out
    _fsck_clean(repo)


def test_a_crlf_file_under_autocrlf_lands_its_lf_blob_and_stays_clean(repo) -> None:
    _git(["config", "core.autocrlf", "true"], repo)
    (repo / "w.py").write_bytes(b"a = 1\r\nb = 2\r\n")

    out = _fire("new", repo, {"C1": ["w.py"]})

    assert out["committed"] is True, out
    assert _blob(repo, "w.py") == b"a = 1\nb = 2\n"
    _fsck_clean(repo)


def test_a_text_unset_attribute_keeps_the_crs_the_file_carries(repo) -> None:
    _git(["config", "core.autocrlf", "true"], repo)
    (repo / ".gitattributes").write_text("*.dat -text\n", encoding="utf-8")
    (repo / "x.dat").write_bytes(b"a\r\nb\r\n")

    out = _fire("new", repo, {"C1": ["x.dat", ".gitattributes"]})

    assert out["committed"] is True, out
    assert _blob(repo, "x.dat") == b"a\r\nb\r\n"


def test_a_name_specific_attribute_line_is_resolved_per_path(repo) -> None:
    _git(["config", "core.autocrlf", "true"], repo)
    (repo / ".gitattributes").write_text("special.txt -text\n", encoding="utf-8")
    (repo / "special.txt").write_bytes(b"a\r\n")
    (repo / "plain.txt").write_bytes(b"a\r\n")

    out = _fire("new", repo, {"C1": ["special.txt", "plain.txt", ".gitattributes"]})

    assert out["committed"] is True, out
    assert _blob(repo, "special.txt") == b"a\r\n"
    assert _blob(repo, "plain.txt") == b"a\n"


def test_a_clean_filter_the_process_cannot_run_refuses_and_commits_nothing(repo) -> None:
    (repo / ".gitattributes").write_text("*.lfs filter=lfs\n", encoding="utf-8")
    _git(["add", ".gitattributes"], repo)
    _git(["commit", "-q", "-m", "attrs"], repo)
    (repo / "big.lfs").write_text("payload\n", encoding="utf-8")
    head = _head(repo)

    out = _fire("new", repo, {"C1": ["big.lfs"]})

    assert out["committed"] is False
    assert _head(repo) == head


def test_a_deletion_that_empties_a_directory_drops_the_directory_from_the_tree(repo) -> None:
    (repo / "d").mkdir()
    (repo / "d" / "only.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "add d"], repo)
    (repo / "d" / "only.py").unlink()

    out = _fire("new", repo, {"C1": ["d/only.py"]})

    assert out["committed"] is True, out
    assert _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split() == ["seed.txt"]
    _fsck_clean(repo)


def test_a_tracked_executable_keeps_its_mode_when_edited(repo) -> None:
    (repo / "run.sh").write_text("echo 1\n", encoding="utf-8")
    _git(["add", "run.sh"], repo)
    _git(["update-index", "--chmod=+x", "run.sh"], repo)
    _git(["commit", "-q", "-m", "exec"], repo)
    (repo / "run.sh").write_text("echo 2\n", encoding="utf-8")

    out = _fire("new", repo, {"C1": ["run.sh"]})

    assert out["committed"] is True, out
    assert _git(["ls-tree", "HEAD", "run.sh"], repo).startswith("100755")


def test_a_second_call_with_a_spent_token_answers_already_landed(repo) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    run_id = "ask-20261114T000000-aaaaaa"
    token = run_token.mint(repo, run_id, "run.mjs", session_id="5e1f0000-0000-4000-8000-0000000000f1")
    extra = {"run_id": run_id, "run_token": token if isinstance(token, str) else token["token"]}

    first = _fire("new", repo, {"C1": ["a.py"]}, **extra)
    second = _fire("new", repo, {"C1": ["a.py"]}, **extra)

    assert first["committed"] is True, first
    assert second.get("already_landed") is True or second.get("sha") == first["sha"], second
    assert _git(["rev-list", "--count", "HEAD"], repo) == "2"


def test_a_path_in_no_reviewed_slice_lands_with_unreviewed_provenance(repo) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    run_id = "ask-20261114T000000-bbbbbb"
    token = run_token.mint(repo, run_id, "run.mjs", session_id="5e1f0000-0000-4000-8000-0000000000f1")
    extra = {"run_id": run_id, "run_token": token if isinstance(token, str) else token["token"]}

    landed = _fire("new", repo, {"C1": ["a.py"]}, inline_review=_review(["other.py"]), **extra)

    assert landed["committed"] is True, landed
    assert "refused" not in landed
    assert [(e["reason"], e["path"]) for e in landed["provenance"] if e["reason"] == "unreviewed"] == [("unreviewed", "a.py")]


def test_an_own_kept_blob_for_a_reverted_path_lands_and_converges_the_worktree(repo) -> None:
    run_id = "ask-20261114T000001-cccccc"
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(repo), input=b"k = 2\n",
                          capture_output=True, check=True).stdout.decode().strip()
    (repo / "k.py").write_bytes(b"k = 1\n")
    _git(["add", "k.py"], repo)
    _git(["commit", "-q", "-m", "k"], repo)
    kept = repo / RECORD_DIR_ROOT / run_id
    kept.mkdir(parents=True)
    (kept / "kept.json").write_text('{"chunks": {"C1": {"paths": {"k.py": "%s"}}}}' % blob, encoding="utf-8")

    out = _fire("new", repo, {"C1": ["k.py"]}, run_id=run_id)

    assert out["committed"] is True, out
    assert _blob(repo, "k.py") == b"k = 2\n", out
    assert out.get("converged") == ["k.py"], out
    assert (repo / "k.py").read_bytes() == b"k = 2\n"


def test_the_reply_keeps_the_keys_callers_branch_on(repo) -> None:
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    out = _fire("new", repo, {"C1": ["a.py"]})

    for key in ("committed", "sha", "stranded", "undeclared_dirty", "chunks_committed", "run_artifacts",
                "residue", "ok"):
        assert key in out, key
    assert out["chunks_committed"] == ["C1"]


def test_a_mode_param_is_not_a_product_landing(repo) -> None:
    out = run_landing.land_run({"halted_by": {"x": 1}}, repo / ".git")

    assert out["committed"] is False
    assert out["refused"] == "mode-ambiguous"


def test_landing_stamps_an_unclaimed_ledger_met_row_and_names_the_rest_unreconciled(wb_repo: Path) -> None:
    review = trg._wb_review([trg._wb_judged("r1"), trg._wb_judged("r3")])
    out = run_landing._write_register(wb_repo, trg._PLAN, review, "abc1234")

    assert out["register_sizing"] == trg._SIZING
    r1, r2, r3 = trg._wb_register(wb_repo)["rows"]
    assert (r1["status"], r3["status"], r3["wired"]) == ("met", "met", True)
    assert r3["met_by"] == [f"{trg._PLAN}@abc1234"]
    assert r2["status"] == "open"
    assert [o["id"] for o in out["register_unreconciled"]] == ["r2"]


def test_landing_with_every_row_met_reports_no_unreconciled(wb_repo: Path) -> None:
    review = trg._wb_review([trg._wb_judged(r) for r in ("r1", "r2", "r3")])
    out = run_landing._write_register(wb_repo, trg._PLAN, review, "abc1234")

    assert out == {"register_sizing": trg._SIZING}
