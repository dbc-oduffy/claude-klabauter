"""dispatch.land_combined_index: verified-index combined commit crediting halted runs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
from coordinator_core.ops.dispatch_emit import combined_landing as op
from coordinator_core.ops.dispatch_emit import halted_run_landed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PROCESS_BUDGET_SECS = 0.5


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@t")
    monkeypatch.setattr(halted_run_landed, "_launch", lambda root, run_id: True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "tag.gpgsign", "false")
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "a", "b", "c")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _halt(root: Path, run_id: str, withheld: list, kept: dict | None = None, base: str | None = "HEAD") -> None:
    rd = root / "state/warp" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    base_sha = _git(root, "rev-parse", base).strip() if base else base
    halted_run.write_halt_marker(rd, halted="x", sizing_rel=None, run_base_sha=base_sha)
    halted_run.record_withheld(rd, {p: "hunks" for p in withheld})
    blobs = {}
    for path in withheld:
        text = (kept or {}).get(path, f"staged {path}\n")
        done = subprocess.run(
            # Bytes, not text=True: text-mode stdin writes CRLF on Windows, so the kept blob would never
            # equal the LF blob the fixture commits.
            ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=text.encode("utf-8"), check=True,
            capture_output=True, **no_console_creationflags(),
        )
        blobs[path] = done.stdout.decode("ascii").strip()
    (rd / "kept.json").write_text(json.dumps({"chunks": {"X1": {"paths": blobs}}, "plan_gate": None}), encoding="utf-8")


def _marker(root: Path, run_id: str) -> dict:
    return halted_run._read(ask_contract.record_dir(root, run_id))


def _stage(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", "--", name)


def _call(root, **params):
    return op._handler(params, root)


def _cpu() -> float:
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def test_commits_index_blobs_keeps_peer_hunk_and_splits_full_from_partial(repo):
    _halt(repo, "R-full", ["a", "b"])
    _halt(repo, "R-part", ["a", "c"])
    _stage(repo, "a", "staged a\n")
    _stage(repo, "b", "staged b\n")
    (repo / "a").write_text("staged a\npeer hunk\n", encoding="utf-8")
    before = _cpu()
    out = _call(repo, paths=["a", "b"], run_ids=["R-full", "R-part"], message="combined")
    spent = _cpu() - before
    assert out["ok"], out
    assert spent < PROCESS_BUDGET_SECS
    assert _git(repo, "show", "HEAD:a") == "staged a\n"
    assert (repo / "a").read_text(encoding="utf-8") == "staged a\npeer hunk\n"
    assert _git(repo, "rev-parse", "HEAD").strip() == out["sha"]
    assert [r["run_id"] for r in out["landed"]] == ["R-full"]
    assert out["partial"] == [{"run_id": "R-part", "missing": ["c"]}]
    assert _marker(repo, "R-full")["status"] == halted_run.STATUS_LANDED
    assert _marker(repo, "R-full")["sha"] == out["sha"]
    assert _marker(repo, "R-part")["status"] == halted_run.STATUS_HALTED
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""


def test_blob_override_that_drops_the_runs_kept_hunk_does_not_credit_it(repo):
    _halt(repo, "R", ["a"], kept={"a": "kept hunk\n"})
    _stage(repo, "a", "override without the hunk\n")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert out["ok"], out
    assert out["landed"] == []
    assert out["partial"] == [{"run_id": "R", "missing": ["a"]}]
    assert _marker(repo, "R")["status"] == halted_run.STATUS_HALTED


def test_refuses_staged_set_differing_from_declared(repo):
    _halt(repo, "R", ["a"])
    _stage(repo, "a", "x\n")
    _stage(repo, "b", "y\n")
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "staged-not-declared ['b']" in out["error"]
    out = _call(repo, paths=["a", "b", "c"], run_ids=["R"], message="m")
    assert not out["ok"] and "declared-not-staged ['c']" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _marker(repo, "R")["status"] == halted_run.STATUS_HALTED


def test_refuses_declared_path_a_peer_run_holds(repo):
    _halt(repo, "R", ["a"])
    _halt(repo, "peer", ["a"])
    _stage(repo, "a", "x\n")
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "peer" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_refuses_run_that_is_not_halted_before_committing(repo):
    _halt(repo, "R", ["a"])
    _stage(repo, "a", "x\n")
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["a"], run_ids=["R", "ghost"], message="m")
    assert not out["ok"] and "ghost" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _marker(repo, "R")["status"] == halted_run.STATUS_HALTED


@pytest.mark.parametrize("params", [{}, {"paths": [], "run_ids": ["R"], "message": "m"}, {"paths": ["a"], "run_ids": ["R"], "message": " "}])
def test_refuses_malformed_params(repo, params):
    assert not _call(repo, **params)["ok"]


L5 = "l1\nl2\nl3\nl4\nl5\n"
L5_KEPT = "l1\nl2\nl3\nR\nl4\nl5\n"
L5_RESIDUE = "l1\nl2\nl3\nR\nl4\nl5-peer\n"


def _union_fixture(repo, committed=L5_KEPT, work=L5_RESIDUE, base="HEAD"):
    (repo / "a").write_text(L5, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "a base")
    _halt(repo, "R", ["a"], kept={"a": L5_KEPT}, base=base)
    _halt(repo, "peer", ["a"])
    _stage(repo, "a", committed)
    (repo / "a").write_text(work, encoding="utf-8")


def test_union_lands_over_halted_holder_whose_residue_stays_in_the_working_tree(repo):
    _union_fixture(repo)
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert out["ok"], out
    assert _git(repo, "show", "HEAD:a") == L5_KEPT
    assert (repo / "a").read_text(encoding="utf-8") == L5_RESIDUE
    assert _marker(repo, "peer")["status"] == halted_run.STATUS_HALTED


def test_refuses_when_committed_blob_carries_a_non_declared_hunk(repo):
    _union_fixture(repo, committed=L5_KEPT + "foreign hunk\n", work=L5_RESIDUE + "foreign hunk\n")
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "peer" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_refuses_when_residue_no_longer_applies_onto_committed_blob(repo):
    _union_fixture(repo, work="l1\nl2\nl3\nR2\nl4\nl5\n")
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "peer" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_holder_record_with_withheld_prints_still_refuses_whole(repo):
    from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE

    _union_fixture(repo)
    (ask_contract.record_dir(repo, "peer") / WITHHELD_PRINTS_FILE).write_text("{}", encoding="utf-8")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "peer" in out["error"]


def _multi(repo, base, kept, committed, work, kept_base="HEAD"):
    (repo / "m").write_bytes(base)
    _git(repo, "add", "--", "m")
    _git(repo, "commit", "-q", "-m", "m base")
    _halt(repo, "R", ["m"], kept={"m": kept.decode()}, base=kept_base)
    _halt(repo, "peer", ["m"])
    (repo / "m").write_bytes(committed)
    _git(repo, "add", "--", "m")
    (repo / "m").write_bytes(work)


def _refused(repo):
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["m"], run_ids=["R"], message="m")
    assert not out["ok"] and "peer" in out["error"], out
    assert _git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("work", [
    b"l1\nl2\nl3\nR\nl4-peer\nl5\n",
    b"l1\nl2\nl3-peer\nR\nl4\nl5\n",
    b"l1\nl2\nl3\nR\npeer\nl4\nl5\n",
])
def test_refuses_residue_edit_adjacent_to_the_committed_added_block(repo, work):
    _multi(repo, L5.encode(), L5_KEPT.encode(), L5_KEPT.encode(), work)
    _refused(repo)


@pytest.mark.parametrize("line", [b"foo\n", b"}\n", b"\n"])
def test_refuses_foreign_hunk_duplicating_a_credited_line(repo, line):
    base = b"top\nx\nbottom\n"
    kept = b"top\nx\n" + line + b"bottom\n"
    _multi(repo, base, kept, kept, b"top\nx\n" + line + line + b"bottom\n")
    _refused(repo)


def test_refuses_line_ending_only_change(repo):
    # Pinned off: under a box-wide autocrlf=true a CRLF working tree over an LF blob is no change at all.
    _git(repo, "config", "core.autocrlf", "false")
    kept = b"a\nb\nR\n"
    _multi(repo, b"a\nb\n", kept, kept, kept.replace(b"\n", b"\r\n"))
    _refused(repo)


def test_refuses_trailing_newline_change(repo):
    kept = b"a\nb\nR\n"
    _multi(repo, b"a\nb\n", kept, kept, b"a\nb\nR")
    _refused(repo)


def test_missing_run_base_sha_holds_even_when_committed_blob_equals_kept(repo):
    _union_fixture(repo, base=None)
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "peer" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_empty_string_run_base_sha_holds_even_when_committed_blob_equals_kept(repo):
    _union_fixture(repo, base="")
    head = _git(repo, "rev-parse", "HEAD")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "peer" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_nonzero_apply_exit_holds_every_candidate_not_only_the_named_conflict(repo, monkeypatch):
    from types import SimpleNamespace

    from coordinator_core.git import run as git_run

    for name in ("a", "b"):
        (repo / name).write_text("changed\n", encoding="utf-8")
    real = git_run.run_git

    def fake(args, **kw):
        if "apply" in args:
            return SimpleNamespace(
                ok=False, returncode=1,
                stderr="Applied patch to 'a' with conflicts.\nerror: b: patch does not apply\n",
            )
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", fake)
    assert op._residue_conflicts(repo, ["a", "b"]) == {"a", "b"}


def test_zero_exit_apply_holds_only_the_named_conflict(repo, monkeypatch):
    from types import SimpleNamespace

    from coordinator_core.git import run as git_run

    for name in ("a", "b"):
        (repo / name).write_text("changed\n", encoding="utf-8")
    real = git_run.run_git

    def fake(args, **kw):
        if "apply" in args:
            return SimpleNamespace(ok=True, returncode=0, stderr="Applied patch to 'a' with conflicts.\n")
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", fake)
    assert op._residue_conflicts(repo, ["a", "b"]) == {"a"}


def test_autocrlf_true_working_tree_equal_to_head_is_an_empty_diff_and_passes(repo):
    _union_fixture(repo, work=L5)
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "a").write_bytes(L5.replace("\n", "\r\n").encode())
    assert _git(repo, "diff", "HEAD", "--", "a") == ""
    assert op._residue_conflicts(repo, ["a"]) == set()
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert out["ok"], out


def test_foreign_commit_advancing_head_on_the_path_holds(repo):
    _union_fixture(repo)
    (repo / "a").write_text("foreign\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "foreign")
    _stage(repo, "a", L5_KEPT)
    (repo / "a").write_text(L5_RESIDUE, encoding="utf-8")
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "peer" in out["error"]


def test_conflict_detection_runs_under_the_c_locale_and_never_reads_translated_text(repo, monkeypatch):
    from coordinator_core.git import run as git_run

    seen = []
    real = git_run.run_git

    def spy(args, **kw):
        if "apply" in args:
            seen.append(kw.get("env"))
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", spy)
    monkeypatch.setenv("LANGUAGE", "de")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    _union_fixture(repo)
    assert _call(repo, paths=["a"], run_ids=["R"], message="m")["ok"]
    assert len(seen) == 1 and seen[0]["LC_ALL"] == "C" and "LANGUAGE" not in seen[0]


@pytest.mark.parametrize("work", [L5_KEPT, L5])
def test_no_residue_passes_whether_or_not_the_working_tree_equals_head(repo, work):
    _union_fixture(repo, work=work)
    assert _call(repo, paths=["a"], run_ids=["R"], message="m")["ok"]


def test_head_moved_after_verification_refuses_without_committing(repo, monkeypatch):
    from coordinator_core.git import git_state

    _halt(repo, "R", ["a"])
    _stage(repo, "a", "x\n")
    head = _git(repo, "rev-parse", "HEAD")
    reads = iter([head.strip(), "0" * 40])
    monkeypatch.setattr(git_state, "head_sha", lambda root, fresh=False: next(reads))
    out = _call(repo, paths=["a"], run_ids=["R"], message="m")
    assert not out["ok"] and "HEAD moved" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _marker(repo, "R")["status"] == halted_run.STATUS_HALTED
