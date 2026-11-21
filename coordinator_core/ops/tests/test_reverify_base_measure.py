"""reverify_base_measure over a throwaway git repo: base reds, absent ids, head-only reds, the file contract."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import reverify_base_measure as rbm


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RED_AT_BASE = "tests/test_a.py::test_red_at_base"
GREEN = "tests/test_b.py::test_green"
ABSENT = "tests/test_c.py::test_added_later"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(
        "from pkg import VALUE\n\ndef test_red_at_base():\n    assert VALUE == 2\n", encoding="utf-8")
    (root / "tests" / "test_b.py").write_text(
        "from pkg import VALUE\n\ndef test_green():\n    assert VALUE == 1\n", encoding="utf-8")
    _git(root.parent / "repo", "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "pkg" / "__init__.py").write_text("VALUE = 3\n", encoding="utf-8")
    (root / "tests" / "test_c.py").write_text("def test_added_later():\n    assert True\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    return root, base, _git(root, "rev-parse", "HEAD")


def _measure(root: Path, argv: list[str], monkeypatch) -> tuple[int, str]:
    monkeypatch.chdir(root)
    code = rbm.main(argv)
    out = argv[argv.index("--out") + 1]
    return code, (root / out).read_text(encoding="utf-8")


def _tail(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])["baseline_export"]


@pytest.mark.parametrize("argv", [
    ["--sha", "abc", "--out", "m.txt"],
    ["--sha", "abc", "--", "x::y"],
    ["--out", "m.txt", "--", "x::y"],
    ["--sha", "abc", "--sha", "def", "--out", "m.txt", "--", "x::y"],
])
def test_malformed_argv_is_a_usage_error(argv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert rbm.main(argv) == rbm.EXIT_USAGE
    assert not (tmp_path / "m.txt").exists()


def test_head_run_collection_error_fails_the_export(monkeypatch):
    calls = []

    def fake_run(sha, ids, cwd):
        calls.append(sha)
        if sha == "base":
            return "ok", 0, [], []
        return "ok", 4, [], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    result = rbm.measure("base", "head", [GREEN], ".")
    assert calls == ["base", "head"]
    assert result["export"] == "failed"
    assert result["passed_at_base"] == [GREEN]


def test_head_pass_covers_ids_absent_at_base_and_runs_when_every_id_is_absent(monkeypatch):
    seen = {}

    def fake_run(sha, ids, cwd):
        seen[sha] = list(ids)
        if sha == "base":
            return "ok", 4, [f"{rbm._DROPPED}{i}" for i in ids], []
        return "ok", 1, [f"FAILED {ABSENT} - assert"], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    result = rbm.measure("base", "head", [ABSENT, GREEN], ".")
    assert seen["head"] == [ABSENT, GREEN]
    assert result["export"] == "ok" and result["red"] == [ABSENT]
    assert result["passed_at_base"] == []


def test_head_pass_skips_ids_red_at_base_and_still_runs_for_absent_ones(monkeypatch):
    seen = {}

    def fake_run(sha, ids, cwd):
        seen[sha] = list(ids)
        if sha == "base":
            return "ok", 1, [f"FAILED {RED_AT_BASE} - assert", f"{rbm._DROPPED}{ABSENT}"], []
        return "ok", 0, [], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    result = rbm.measure("base", "head", [RED_AT_BASE, ABSENT, GREEN], ".")
    assert seen["head"] == [ABSENT, GREEN]
    assert result["red"] == [RED_AT_BASE]


def test_head_sha_none_never_runs_head_so_head_only_reds_stay_out(monkeypatch):
    calls = []

    def fake_run(sha, ids, cwd):
        calls.append(sha)
        return "ok", 0, [], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    result = rbm.measure("base", None, [GREEN], ".")
    assert calls == ["base"] and result["red"] == []


def test_timeout_s_is_one_deadline_and_expiry_reports_timeout(monkeypatch):
    def slow(argv, *a, **kw):
        assert kw["timeout"] is not None
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(rbm.subprocess, "run", slow)
    result = rbm.measure("0" * 40, None, [GREEN], ".", timeout_s=5.0)
    assert result["export"] == "timeout" and result["red"] == [] and result["passed_at_base"] == []


def test_without_timeout_s_the_run_is_unbounded(monkeypatch):
    seen = {}

    def fake(argv, *a, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(argv, 0)

    monkeypatch.setattr(rbm.subprocess, "run", fake)
    rbm.measure("0" * 40, None, [GREEN], ".")
    assert seen["timeout"] is None


def test_a_spawn_oserror_propagates_for_the_leg_to_name(monkeypatch):
    def too_long(argv, *a, **kw):
        raise OSError(206, "command line too long")

    monkeypatch.setattr(rbm.subprocess, "run", too_long)
    with pytest.raises(OSError, match="too long"):
        rbm.measure("0" * 40, None, [GREEN], ".")


@pytest.mark.parametrize("sep", ["\\", "/"])
def test_a_backslash_printed_red_id_is_red_not_passed_at_base(monkeypatch, sep):
    printed = RED_AT_BASE.replace("/", sep)

    def fake_run(sha, ids, cwd):
        return "ok", 1, [f"FAILED {printed} - assert"], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    result = rbm.measure("base", None, [RED_AT_BASE, GREEN], ".")
    assert result["red"] == [RED_AT_BASE]
    assert result["passed_at_base"] == [GREEN]


def test_a_backslash_red_id_is_written_forward_slash(monkeypatch, tmp_path):
    printed = RED_AT_BASE.replace("/", "\\")
    monkeypatch.setattr(rbm, "_run", lambda sha, ids, cwd: ("ok", 1, [f"FAILED {printed} - assert"], []))
    target = tmp_path / "m.txt"
    rbm.write_export(rbm.measure("base", None, [RED_AT_BASE], "."), target)
    assert target.read_text(encoding="utf-8").splitlines()[0] == f"FAILED {RED_AT_BASE}"


def test_head_run_exit_one_keeps_the_export_ok(monkeypatch):
    def fake_run(sha, ids, cwd):
        if sha == "base":
            return "ok", 0, [], []
        return "ok", 1, [f"FAILED {GREEN} - assert"], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    result = rbm.measure("base", "head", [GREEN], ".")
    assert result["export"] == "ok" and result["red"] == [GREEN]


def test_base_measure_pytest_carries_a_per_run_basetemp_removed_after_the_run(monkeypatch, tmp_path):
    from coordinator_core.testing import scoped_pytest

    seen: dict = {}

    def fake_run(argv, *a, **kw):
        runner_args = argv[argv.index(rbm._RETRY_RUNNER) + 1:]
        seen["basetemp"] = Path(runner_args[1])
        seen["existed"] = seen["basetemp"].is_dir()
        seen["temp"] = kw["env"]["TEMP"]
        return subprocess.CompletedProcess(argv, 0, stdout='{"baseline_export": {"export": "ok", "runner_exit": 0}}\n',
                                           stderr="")

    monkeypatch.setattr(rbm.subprocess, "run", fake_run)
    monkeypatch.setattr(rbm, "_resolve", lambda sha, cwd: sha)
    rbm._run("a" * 40, ["tests/test_a.py::test_x"], str(tmp_path))
    root = scoped_pytest.per_run_root(str(tmp_path))
    assert seen["existed"] and root in seen["basetemp"].parents
    assert Path(seen["temp"]).parent == seen["basetemp"].parent
    assert "--basetemp" in rbm._RETRY_RUNNER and "sys.argv[2]" in rbm._RETRY_RUNNER
    assert not seen["basetemp"].parent.exists()
