"""reverify_base_measure over a throwaway git repo: base reds, absent ids, head-only reds, the file contract."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import reverify_base_measure as rbm
from coordinator_core.ops.review_reverify import _read_base_export

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


def test_red_at_base_id_is_read_back_by_reverify(repo, monkeypatch):
    root, base, _head = repo
    code, text = _measure(root, ["--sha", base, "--out", "scratch/m.txt", "--", RED_AT_BASE, GREEN], monkeypatch)
    assert code == 0
    assert _read_base_export(root, "scratch/m.txt") == {RED_AT_BASE}
    tail = _tail(text)
    assert tail["export"] == "ok" and tail["runner_exit"] == 1 and tail["passed_at_base"] == [GREEN]
    assert "ModuleNotFoundError" not in text


def test_id_absent_at_base_is_dropped_never_red_never_passed(repo, monkeypatch):
    root, base, _head = repo
    _code, text = _measure(root, ["--sha", base, "--out", "m.txt", "--", ABSENT, RED_AT_BASE], monkeypatch)
    assert _read_base_export(root, "m.txt") == {RED_AT_BASE}
    assert ABSENT not in text
    assert _tail(text)["passed_at_base"] == []


def test_several_absent_ids_are_all_dropped(repo, monkeypatch):
    root, base, _head = repo
    other = "tests/test_c.py::test_also_added"
    _code, text = _measure(root, ["--sha", base, "--out", "m.txt", "--", ABSENT, other, GREEN], monkeypatch)
    assert _read_base_export(root, "m.txt") == set()
    assert _tail(text)["passed_at_base"] == [GREEN]


def test_green_at_base_red_at_head_joins_the_set(repo, monkeypatch):
    root, base, head = repo
    _code, text = _measure(
        root, ["--sha", base, "--head-sha", head, "--out", "m.txt", "--", GREEN, RED_AT_BASE], monkeypatch)
    assert _read_base_export(root, "m.txt") == {GREEN, RED_AT_BASE}
    assert _tail(text)["passed_at_base"] == [GREEN]


def test_without_head_sha_a_green_id_stays_out(repo, monkeypatch):
    root, base, _head = repo
    _measure(root, ["--sha", base, "--out", "m.txt", "--", GREEN], monkeypatch)
    assert _read_base_export(root, "m.txt") == set()


def test_unresolvable_sha_writes_a_failed_export_reverify_treats_as_unmeasured(repo, monkeypatch):
    root, _base, _head = repo
    _code, text = _measure(root, ["--sha", "0" * 40, "--out", "m.txt", "--", GREEN], monkeypatch)
    assert _tail(text)["export"] == "failed"
    assert _read_base_export(root, "m.txt") is None


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


def test_an_id_list_over_32k_chars_reaches_the_runner_through_a_file_not_argv(tmp_path, monkeypatch):
    root = tmp_path / "many"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "tests" / "test_many.py").write_text(
        "".join(f"def test_ok_{i:04d}_{'x' * 40}():\n    assert True\n" for i in range(500)), encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    pad = [f"tests/test_many.py::test_ok_{i:04d}_{'x' * 40}" for i in range(500)]
    argvs: list[list[str]] = []
    real = subprocess.run

    def watch(argv, *a, **kw):
        argvs.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(rbm.subprocess, "run", watch)
    result = rbm.measure(base, None, pad, str(root))
    assert sum(len(i) for i in pad) > 32 * 1024
    assert result["export"] == "ok" and result["red"] == [] and result["passed_at_base"] == pad
    assert all(sum(len(a) for a in argv) < 32 * 1024 for argv in argvs)


def test_a_spawn_oserror_propagates_for_the_leg_to_name(monkeypatch):
    def too_long(argv, *a, **kw):
        raise OSError(206, "command line too long")

    monkeypatch.setattr(rbm.subprocess, "run", too_long)
    with pytest.raises(OSError, match="too long"):
        rbm.measure("0" * 40, None, [GREEN], ".")


def test_write_export_is_byte_identical_to_main(repo, monkeypatch, tmp_path):
    root, base, _head = repo
    _code, text = _measure(root, ["--sha", base, "--out", "m.txt", "--", RED_AT_BASE, GREEN], monkeypatch)
    result = rbm.measure(base, None, [RED_AT_BASE, GREEN], str(root))
    target = tmp_path / "nested" / "w.txt"
    rbm.write_export(result, target)
    assert target.read_bytes() == (root / "m.txt").read_bytes() and text == target.read_text(encoding="utf-8")


def test_head_run_exit_one_keeps_the_export_ok(monkeypatch):
    def fake_run(sha, ids, cwd):
        if sha == "base":
            return "ok", 0, [], []
        return "ok", 1, [f"FAILED {GREEN} - assert"], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    result = rbm.measure("base", "head", [GREEN], ".")
    assert result["export"] == "ok" and result["red"] == [GREEN]
