"""migrate_hub: legacy .git/coordinator-sessions carry-over."""

import os
from pathlib import Path

from coordinator_core.session import migrate_hub as mh
from coordinator_core.session_hub_path import hub_for_common


def _seed(tmp_path: Path):
    common = tmp_path / ".git"
    legacy = common / "coordinator-sessions"
    (legacy / "s1").mkdir(parents=True)
    (legacy / "s1" / "meta.json").write_text("legacy-meta")
    (legacy / "s1" / "touch-record.jsonl").write_text('{"a":1}\n{"b":2}\n')
    return common, legacy


def test_merges_into_resolver_hub_and_keeps_legacy(tmp_path):
    common, legacy = _seed(tmp_path)
    c = mh.migrate_common(common)
    hub = hub_for_common(common)
    assert c["copied"] == 2
    assert (hub / "s1" / "meta.json").read_text() == "legacy-meta"
    assert (legacy / "s1" / "meta.json").exists()


def test_second_run_is_noop(tmp_path):
    common, _ = _seed(tmp_path)
    mh.migrate_common(common)
    c = mh.migrate_common(common)
    assert c == {"copied": 0, "merged": 0, "skipped": 2}


def test_newer_destination_wins_and_older_is_replaced(tmp_path):
    common, legacy = _seed(tmp_path)
    hub = hub_for_common(common)
    (hub / "s1").mkdir(parents=True)
    dst = hub / "s1" / "meta.json"
    dst.write_text("new-dest")
    os.utime(legacy / "s1" / "meta.json", (1000, 1000))
    mh.migrate_common(common)
    assert dst.read_text() == "new-dest"
    os.utime(dst, (500, 500))
    mh.migrate_common(common)
    assert dst.read_text() == "legacy-meta"


def test_jsonl_unions_even_when_destination_is_newer(tmp_path):
    common, legacy = _seed(tmp_path)
    hub = hub_for_common(common)
    (hub / "s1").mkdir(parents=True)
    dst = hub / "s1" / "touch-record.jsonl"
    dst.write_text('{"b":2}\n{"c":3}')
    os.utime(legacy / "s1" / "touch-record.jsonl", (1000, 1000))
    c = mh.migrate_common(common)
    assert c["merged"] == 1
    assert dst.read_text().splitlines() == ['{"b":2}', '{"c":3}', '{"a":1}']
    assert mh.migrate_common(common)["merged"] == 0


def test_bare_repo_is_noop(tmp_path):
    bare = tmp_path / "repo.git"
    (bare / "coordinator-sessions" / "s1").mkdir(parents=True)
    (bare / "coordinator-sessions" / "s1" / "x").write_text("x")
    assert mh.migrate_common(bare) == {"copied": 0, "merged": 0, "skipped": 0}


def test_main_runs_against_repo(tmp_path, monkeypatch, capsys):
    common, _ = _seed(tmp_path)
    import coordinator_core.git.repo_root as rr

    monkeypatch.setattr(rr, "git_common_dir", lambda cwd=None: str(common))
    assert mh.main(["--repo", str(tmp_path)]) == 0
    assert "copied=2" in capsys.readouterr().out


def test_interrupted_copy_leaves_no_destination_so_a_rerun_copies(tmp_path, monkeypatch):
    common, _ = _seed(tmp_path)
    real = mh.shutil.copy2

    def _truncating_copy(src, dst):
        Path(dst).write_text("trunc")
        os.utime(dst, (Path(src).stat().st_mtime,) * 2)
        raise OSError("killed mid-copy")

    monkeypatch.setattr(mh.shutil, "copy2", _truncating_copy)
    try:
        mh.migrate_common(common)
    except OSError:
        pass
    hub = hub_for_common(common)
    assert not list((hub / "s1").iterdir())
    monkeypatch.setattr(mh.shutil, "copy2", real)
    mh.migrate_common(common)
    assert (hub / "s1" / "meta.json").read_text() == "legacy-meta"
