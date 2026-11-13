"""reap_green: batched member-test run, in-place close of all-green tf rows, HEAD-stamped record."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.bug_blitz import reap_green, targeted_run
from coordinator_core.bug_blitz.contract import REAP_CLOSED_BY, ReapRecord, reap_record_rel
from coordinator_core.git import git_state

pytestmark = pytest.mark.complete

SHA = "a" * 40


@pytest.fixture(autouse=True)
def _settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


@pytest.fixture
def spawns(monkeypatch):
    calls: list[list[str]] = []
    real = targeted_run._spawn_pytest_capped

    def counting(resolved, cwd, timeout):
        calls.append(list(resolved))
        return real(resolved, cwd, timeout)

    monkeypatch.setattr(targeted_run, "_spawn_pytest_capped", counting)
    return calls


def _row(repo: Path, slug: str, body: str, *, tags=("tf",), status="open") -> str:
    path = repo / "state" / "bug-backlog" / f"2026-11-12-{slug}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_lines = "".join(f"  - {t}\n" for t in tags)
    indented = "\n".join("  " + line for line in body.splitlines())
    path.write_text(
        f"created: 2026-11-12\ntitle: \"TF: {slug}\"\nbody: |-\n{indented}\nstatus: {status}\n"
        f"surface: tests/{slug}.py\nseverity: P1\nfrom_repo: x\ntags:\n{tag_lines}"
        "evidence: scratch/old.txt\n",
        encoding="utf-8",
    )
    return path.stem


def _fm(repo: Path, row_id: str) -> dict:
    return yaml.safe_load((repo / "state" / "bug-backlog" / f"{row_id}.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".git" / "refs" / "heads" / "main").write_text(SHA + "\n")
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (root / "tests" / "test_good.py").write_text("def test_ok():\n    assert True\n")
    (root / "tests" / "test_bad.py").write_text(
        "def test_nope():\n    assert False\n\ndef test_fine():\n    assert True\n"
    )
    return root


def test_member_nodeids_parses_mint_body_shapes():
    body = (
        "Failing tests and first message line:\n"
        "- tests/test_a.py::test_x :: assert 1 == 2\n"
        "- tests/test_a.py::TestC::test_y\n"
        "- tests/test_b.py :: boom\n"
    )
    assert reap_green.member_nodeids(body) == [
        ("tests/test_a.py", "tests/test_a.py::test_x"),
        ("tests/test_a.py", "tests/test_a.py::TestC::test_y"),
        ("tests/test_b.py", "tests/test_b.py"),
    ]


def test_reap_closes_green_row_and_keeps_red_row_at_default_args(repo, spawns):
    green = _row(repo, "good", "Failing tests:\n- tests/test_good.py::test_ok")
    red = _row(repo, "bad", "Failing tests:\n- tests/test_bad.py::test_nope")

    record = reap_green.reap(repo)

    assert record.head_sha == git_state.head_sha(repo) == SHA
    assert record.green == (green,)
    assert record.red == (red,)
    closed = _fm(repo, green)
    assert closed["status"] == "closed"
    assert closed["closed_by"] == REAP_CLOSED_BY
    assert closed["evidence"] == reap_record_rel()
    assert str(closed["closed_at"])
    assert _fm(repo, red)["status"] == "open"
    on_disk = ReapRecord.from_json(json.loads((repo / reap_record_rel()).read_text()))
    assert on_disk == record


def test_reap_spawns_once_per_chunk_not_per_row(repo, spawns):
    for i in range(4):
        (repo / "tests" / f"test_g{i}.py").write_text("def test_ok():\n    assert True\n")
        _row(repo, f"g{i}", f"Failing tests:\n- tests/test_g{i}.py::test_ok")
    _row(repo, "bad", "Failing tests:\n- tests/test_bad.py::test_nope")

    record = reap_green.reap(repo, chunk_files=3)

    assert len(record.green) == 4 and len(record.red) == 1
    assert len(spawns) == 2  # five files in chunks of three


def test_file_only_member_is_red_when_any_test_in_file_fails(repo, spawns):
    row = _row(repo, "wholefile", "Failing tests:\n- tests/test_bad.py")
    record = reap_green.reap(repo)
    assert record.red == (row,)


def test_member_green_when_only_a_sibling_test_in_its_file_fails(repo, spawns):
    row = _row(repo, "sibling", "Failing tests:\n- tests/test_bad.py::test_fine")
    record = reap_green.reap(repo)
    assert record.green == (row,)


def test_member_node_that_no_longer_exists_is_unrun_not_green(repo, spawns):
    gone = _row(repo, "renamed", "Failing tests:\n- tests/test_good.py::test_renamed_away")
    ok = _row(repo, "ok", "Failing tests:\n- tests/test_good.py::test_ok")
    record = reap_green.reap(repo)
    assert record.unrun == {gone: "member not collected"}
    assert record.green == (ok,)
    assert _fm(repo, gone)["status"] == "open"


def test_collection_error_row_stays_open_and_unrun(repo, spawns):
    (repo / "tests" / "test_broken.py").write_text("import no_such_module_xyz\n")
    row = _row(repo, "broken", "Failing tests:\n- tests/test_broken.py::test_a")
    good = _row(repo, "good", "Failing tests:\n- tests/test_good.py::test_ok")

    record = reap_green.reap(repo)

    assert record.unrun == {row: "collection error"}
    assert record.green == (good,)
    assert _fm(repo, row)["status"] == "open"


def test_refused_chunk_is_split_then_unrun_never_closed(repo, spawns, monkeypatch):
    monkeypatch.setattr(targeted_run, "admit", lambda argv, root: False)
    row = _row(repo, "good", "Failing tests:\n- tests/test_good.py::test_ok")
    record = reap_green.reap(repo)
    assert not spawns
    assert record.green == () and record.unrun == {row: "chunk refused by the Tier-T classifier"}
    assert _fm(repo, row)["status"] == "open"


def test_timeout_halves_chunk_then_marks_unrun(repo, monkeypatch):
    calls = []

    def timed_out(resolved, cwd, timeout):
        calls.append(resolved)
        return targeted_run._TIMEOUT_EXIT, ""

    monkeypatch.setattr(targeted_run, "_spawn_pytest_capped", timed_out)
    a = _row(repo, "good", "Failing tests:\n- tests/test_good.py::test_ok")
    b = _row(repo, "bad", "Failing tests:\n- tests/test_bad.py::test_nope")
    record = reap_green.reap(repo, timeout=7)
    assert len(calls) == 3  # the pair, then each file alone
    assert set(record.unrun) == {a, b} and "timed out after 7s" in record.unrun[a]


def test_rows_without_runnable_members_are_unrun(repo, spawns):
    no_members = _row(repo, "prose", "A prose report with no test list")
    js = _row(repo, "js", "Failing tests:\n- web/widget.test.ts::renders")
    gone = _row(repo, "gone", "Failing tests:\n- tests/test_missing.py::test_a")
    non_tf = _row(repo, "other", "Failing tests:\n- tests/test_good.py::test_ok", tags=("misc",))

    record = reap_green.reap(repo)

    assert set(record.unrun) == {no_members, js, gone}
    assert non_tf not in record.unrun and not record.green and not spawns
    assert _fm(repo, non_tf)["status"] == "open"


def test_closed_rows_are_not_reaped_again(repo, spawns):
    _row(repo, "done", "Failing tests:\n- tests/test_good.py::test_ok", status="closed")
    record = reap_green.reap(repo)
    assert record.green == record.red == () and not spawns


def _load_cli():
    import importlib.util

    path = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "bug-backlog-reap.py"
    spec = importlib.util.spec_from_file_location("bug_backlog_reap_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_help_exits_zero_and_missing_repo_is_usage_error(capsys):
    cli = _load_cli()
    assert cli.main(["--help"]) == 0
    assert "bug-backlog-reap" in capsys.readouterr().out
    assert cli.main([]) == 2
    assert cli.main(["--bogus", "x"]) == 2


@pytest.mark.parametrize("argv", [["--repo", "R"], ["--repo=R"]])
def test_cli_delegates_repo_to_reap_main(monkeypatch, argv):
    cli = _load_cli()
    seen = []
    monkeypatch.setattr(cli, "_import_reap", lambda: lambda a: seen.append(a) or 0)
    assert cli.main(argv) == 0 and seen == [["R"]]


def test_cli_failure_is_exit_one_and_engine_unavailable_is_exit_one(monkeypatch):
    cli = _load_cli()

    def boom(_args):
        raise ValueError("no record")

    monkeypatch.setattr(cli, "_import_reap", lambda: boom)
    assert cli.main(["--repo", "R"]) == 1

    def unavailable():
        raise RuntimeError("engine off path")

    monkeypatch.setattr(cli, "_import_reap", unavailable)
    assert cli.main(["--repo", "R"]) == 1
