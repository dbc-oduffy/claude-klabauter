"""Tests for suite_run_record: rotation, field set, classifier, failed_ids parse."""

import pytest

from coordinator_core.testing import suite_run_record as srr
from pathlib import Path


def _fields(**over):
    base = {
        "repo_root": "r", "collection": "default", "command": ["pytest"], "head_sha": "a" * 40,
        "started_at": "t0", "ended_at": "t1", "dirty_paths": [], "duration_s": 1.0,
        "exit_code": 0, "outcome": "pass", "serialized": True, "log_truncated": False,
    }
    base.update(over)
    return base


def test_two_writes_leave_current_and_previous(tmp_path: Path) -> None:
    rec = srr.record_dir(tmp_path, "default")
    for n in (1, 2):
        log = tmp_path / f"run{n}.log"
        log.write_text(f"run {n}\n")
        srr.write_run(rec, _fields(exit_code=n), log)
    runs = srr.read_runs(rec)
    assert runs["current"]["exit_code"] == 2 and runs["previous"]["exit_code"] == 1
    for slot in ("current", "previous"):
        assert set(srr.NAMED_FIELDS) <= set(runs[slot])
        assert runs[slot]["schema"] == srr.SCHEMA
    assert (rec / "current.log").read_text() == "run 2\n"
    assert (rec / "previous.log").read_text() == "run 1\n"
    assert runs["current"]["log"] == "current.log"
    assert runs["previous"]["log"] == "previous.log"


def test_write_run_sweeps_only_dead_writer_tmp_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.session import core

    monkeypatch.setattr(core, "pid_alive", lambda pid: pid == 222)
    rec = srr.record_dir(tmp_path, "default")
    rec.mkdir(parents=True)
    dead, live, junk = rec / "run-111.log.tmp", rec / "run-222.log.tmp", rec / "run-x.log.tmp"
    for p in (dead, live, junk):
        p.write_text("x")
    srr.write_run(rec, _fields(), None)
    assert not dead.exists() and live.exists() and junk.exists()


def test_missing_field_raises_before_rotation(tmp_path: Path) -> None:
    rec = srr.record_dir(tmp_path, "c")
    srr.write_run(rec, _fields(), None)
    bad = _fields()
    del bad["outcome"]
    with pytest.raises(KeyError):
        srr.write_run(rec, bad, None)
    assert srr.read_runs(rec)["previous"] is None


def test_read_runs_empty(tmp_path: Path) -> None:
    assert srr.read_runs(tmp_path / "none") == {"current": None, "previous": None}


def test_main_slot_only_when_head_on_main(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    head = tmp_path / ".git" / "HEAD"
    rec = tmp_path / "rec"
    head.write_text("ref: refs/heads/feature\n")
    srr.write_run(rec, _fields(repo_root=str(tmp_path), head_sha="f" * 40), None)
    assert srr.read_main(rec) is None and not (rec / "main.json").exists()
    head.write_text("ref: refs/heads/main\n")
    srr.write_run(rec, _fields(repo_root=str(tmp_path), head_sha="a" * 40), None)
    head.write_text("ref: refs/heads/feature\n")
    srr.write_run(rec, _fields(repo_root=str(tmp_path), head_sha="b" * 40), None)
    assert srr.read_main(rec)["head_sha"] == "a" * 40
    assert srr.read_main(rec)["log"] == "main.log" and (rec / "main.log").exists()
    assert srr.read_runs(rec)["current"]["head_sha"] == "b" * 40


def test_classify_failures_both_sides() -> None:
    prev = {"head_sha": "p", "failed_ids": ["a", "b"]}
    main = {"head_sha": "m", "failed_ids": ["b", "c"]}
    got = srr.classify_failures(["a", "b", "c", "d"], prev, main)
    assert got["previous_sha"] == "p" and got["main_sha"] == "m"
    assert {k: v["red_at"] for k, v in got["ids"].items()} == {
        "a": ["previous"], "b": ["previous", "main"], "c": ["main"], "d": []}
    assert got["ids"]["d"]["status"] == "new" and got["ids"]["a"]["status"] == "pre-existing"
    assert srr.classify_failures(["a"], None, None)["ids"]["a"]["status"] == "new"


def test_classify_failures_main_is_current_is_no_baseline() -> None:
    cur = {"head_sha": "m", "started_at": "t1", "failed_ids": ["a"]}
    same = dict(cur)
    got = srr.classify_failures(["a"], None, same, cur)
    assert got["ids"]["a"]["status"] == "new" and got["main_sha"] is None
    older = {"head_sha": "m", "started_at": "t0", "failed_ids": ["a"]}
    assert srr.classify_failures(["a"], None, older, cur)["ids"]["a"]["red_at"] == ["main"]


def test_repo_root_for_dir_and_file_git(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / ".git").write_text("gitdir: x")
    assert srr.repo_root_for(tmp_path / "a" / "b") == (tmp_path / "a").resolve()


def test_collection_slug_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        srr.record_dir(tmp_path, "Bad Name")
    assert srr.record_dir(tmp_path, "x.1-y").name == "x.1-y"


@pytest.mark.parametrize("paths", [
    ["src/b.py"], ["docs"], ["docs/a.md", "src/b.py"], ["mod.py"], ["tests/a.py"],
])
def test_classifier_fails_toward_source(paths) -> None:
    docs_only, source = srr.classify_changed(paths)
    assert not docs_only and source


def test_classifier_docs_only() -> None:
    assert srr.classify_changed(["docs/a/b.md", "archive/x.md"]) == (True, [])
    assert srr.classify_changed(
        ["README.md", "state/x.yaml", "docs/x.py", "docs/tests/x.md", "tests/a.md"]
    ) == (True, [])
    assert srr.classify_changed([]) == (True, [])


def test_failed_ids_parse() -> None:
    text = "FAILED a/t.py::test_x - boom\nPASSED b\nFAILED a/t.py::test_y[1]\n"
    assert srr.parse_failed_ids(text) == ["a/t.py::test_x", "a/t.py::test_y[1]"]
    assert srr.parse_failed_ids("all good\n") is None


def test_failed_ids_null_in_record(tmp_path: Path) -> None:
    rec = srr.record_dir(tmp_path, "d")
    srr.write_run(rec, _fields(), None)
    assert srr.read_runs(rec)["current"]["failed_ids"] is None


def _last(tmp_path, name: str = "default"):
    out = tmp_path / "state" / "test-runs"
    return out / f"{name}-last.txt", out / f"{name}-last.json"


def test_last_pair_strips_ansi_and_summarizes(tmp_path: Path) -> None:
    import json

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/feat/x\n")
    log = tmp_path / "run.log"
    log.write_text(
        "\x1b[31mFAILED a/t.py::test_x\x1b[0m - boom\r\n"
        "\x1b[31m== 1 failed, 4 passed, 2 skipped in 1.5s ==\x1b[0m\n"
    )
    srr.write_run(srr.record_dir(tmp_path, "default"), _fields(exit_code=1, outcome="fail",
                  failed_ids=["a/t.py::test_x"]), log)
    txt, js = _last(tmp_path)
    assert "\x1b" not in txt.read_text() and "\r" not in txt.read_text()
    assert "FAILED a/t.py::test_x - boom" in txt.read_text()
    body = json.loads(js.read_text())
    assert body == {
        "sha": "a" * 40, "branch": "feat/x", "started_at": "t0", "duration_s": 1.0, "exit": 1,
        "counts": {"failed": 1, "passed": 4, "skipped": 2}, "failing": ["a/t.py::test_x"],
    }


def test_last_pair_overwritten_per_run(tmp_path: Path) -> None:
    rec = srr.record_dir(tmp_path, "default")
    for n in (1, 2):
        log = tmp_path / f"r{n}.log"
        log.write_text(f"run {n}\n")
        srr.write_run(rec, _fields(exit_code=n), log)
    txt, js = _last(tmp_path)
    assert txt.read_text() == "run 2\n" and '"exit": 2' in js.read_text()
    assert sorted(p.name for p in txt.parent.iterdir()) == ["default-last.json", "default-last.txt"]


def test_last_pair_failure_never_fails_the_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "state").write_text("a file where the directory should be")
    cur = srr.write_run(srr.record_dir(tmp_path, "default"), _fields(), None)
    assert cur.is_file()
    assert "last-run pair not written" in capsys.readouterr().err


def test_branch_none_on_detached_head(tmp_path: Path) -> None:
    import json

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("b" * 40 + "\n")
    assert srr.read_branch(tmp_path) is None
    srr.write_run(srr.record_dir(tmp_path, "default"), _fields(), None)
    body = json.loads(_last(tmp_path)[1].read_text())
    assert body["branch"] is None and body["counts"] is None and body["failing"] == []


@pytest.mark.parametrize("line,counts", [
    ("===== 3 passed in 0.12s =====", {"passed": 3}),
    ("1 failed, 2 errors, 1 warning in 3.00s", {"failed": 1, "errors": 2, "warnings": 1}),
    ("no summary here", None),
])
def test_parse_counts(line, counts) -> None:
    assert srr.parse_counts(line + "\n") == counts


def test_crash_between_demote_steps_leaves_current_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = srr.record_dir(tmp_path, "default")
    first = tmp_path / "run1.log"
    first.write_text("run 1\n")
    srr.write_run(rec, _fields(exit_code=1), first)

    real_replace, demotes = srr.os.replace, []

    def crash_on_json_demote(src, dst):
        if str(dst).endswith("previous.json"):
            demotes.append(dst)
            raise OSError("simulated crash between demote steps")
        return real_replace(src, dst)

    monkeypatch.setattr(srr.os, "replace", crash_on_json_demote)
    second = tmp_path / "run2.log"
    second.write_text("run 2\n")
    with pytest.raises(OSError):
        srr.write_run(rec, _fields(exit_code=2), second)

    assert demotes
    assert (rec / "current.log").is_file() and (rec / "current.json").is_file()
    current = srr.read_runs(rec)["current"]
    assert current is not None and current["exit_code"] in (1, 2)
    assert (rec / "current.log").read_text() in ("run 1\n", "run 2\n")
