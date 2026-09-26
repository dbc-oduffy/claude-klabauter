from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List

import pytest

import coordinator_core.ops.find_polluter as find_polluter
from coordinator_core.ops.find_polluter import main
from coordinator_core.testing.fake_machine_local import write_fake_executable

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _make_fake_npm(tmp_path: Path, pollute_on_substring: str = "bad") -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    pollute_on_substring_repr = repr(pollute_on_substring)
    python_body = (
        "import sys\n"
        "from pathlib import Path\n"
        f"pollute_on_substring = {pollute_on_substring_repr}\n"
        "args = sys.argv[1:]\n"
        "if len(args) >= 2 and args[0] == 'test' and pollute_on_substring in args[1]:\n"
        "    Path('.pollution-marker').write_text('', encoding='utf-8')\n"
    )
    write_fake_executable(bin_dir, "npm", python_body)
    return bin_dir


def _patch_npm_run(monkeypatch, pollute_on_substring: str = "bad") -> None:
    real_run = subprocess.run

    def _fake_run(args, **kwargs):
        if args and args[0] == "npm":
            if len(args) >= 3 and args[1] == "test" and pollute_on_substring in args[2]:
                Path(".pollution-marker").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        return real_run(args, **kwargs)

    monkeypatch.setattr(find_polluter.subprocess, "run", _fake_run)


def _run(monkeypatch, tmp_path, fake_bin: Path, argv: List[str], chdir: Path = None) -> int:
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.chdir(chdir if chdir is not None else tmp_path)
    return main(argv)


def _make_corpus(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "good1.test.ts").write_text("")
    (src / "bad.test.ts").write_text("")
    (src / "good2.test.ts").write_text("")
    return src


def test_finds_polluter(monkeypatch, tmp_path):
    fake_bin = _make_fake_npm(tmp_path)
    _patch_npm_run(monkeypatch)
    _make_corpus(tmp_path)
    rc = _run(monkeypatch, tmp_path, fake_bin, [".pollution-marker", "src/**/*.test.ts"])
    assert rc == 1
    assert (tmp_path / ".pollution-marker").exists()


def test_no_polluter_all_clean(monkeypatch, tmp_path):
    fake_bin = _make_fake_npm(tmp_path)
    _patch_npm_run(monkeypatch)
    _make_corpus(tmp_path)
    rc = _run(monkeypatch, tmp_path, fake_bin, [".pollution-marker", "src/**/*good*.test.ts"])
    assert rc == 0
    assert not (tmp_path / ".pollution-marker").exists()


def test_npm_missing_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (tmp_path / "empty-bin").mkdir()
    rc = main([".pollution-marker", "src/**/*.test.ts"])
    assert rc == 1


def test_wrong_arg_count(monkeypatch, tmp_path):
    fake_bin = _make_fake_npm(tmp_path)
    rc = _run(monkeypatch, tmp_path, fake_bin, ["onlyonearg"])
    assert rc == 1


def test_no_matching_pattern(monkeypatch, tmp_path):
    fake_bin = _make_fake_npm(tmp_path)
    _make_corpus(tmp_path)
    rc = _run(monkeypatch, tmp_path, fake_bin, [".pollution-marker", "src/**/*.nomatch.ts"])
    assert rc == 1


def test_preexisting_pollution_fails_loud(monkeypatch, tmp_path):
    fake_bin = _make_fake_npm(tmp_path)
    _make_corpus(tmp_path)
    (tmp_path / ".pollution-marker").write_text("stale")
    rc = _run(monkeypatch, tmp_path, fake_bin, [".pollution-marker", "src/**/*.test.ts"])
    assert rc == 1


def test_glob_order_is_sorted(monkeypatch, tmp_path):
    fake_bin = _make_fake_npm(tmp_path, pollute_on_substring="zzz")
    _patch_npm_run(monkeypatch, pollute_on_substring="zzz")
    src = tmp_path / "src"
    src.mkdir()
    (src / "zzz.test.ts").write_text("")
    (src / "aaa.test.ts").write_text("")
    rc = _run(monkeypatch, tmp_path, fake_bin, [".pollution-marker", "src/**/*.test.ts"])
    assert rc == 1
    assert (tmp_path / ".pollution-marker").exists()
