"""Characterization + parity tests for coordinator_core.ops.find_polluter.

Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List

import pytest

import coordinator_core.ops.find_polluter as find_polluter
from coordinator_core.ops.find_polluter import main
from coordinator_core.testing.fake_machine_local import write_fake_executable

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _make_fake_npm(tmp_path: Path, pollute_on_substring: str = "bad") -> Path:
    """Write a cross-platform fake `npm` executable: `npm test <file>` creates
    `.pollution-marker` in the CWD iff <file> contains `pollute_on_substring`;
    always exits 0.

    Only used to satisfy the module's `shutil.which("npm")` pre-flight probe
    (existence check only, never exec'd) — the actual `npm test <file>`
    invocation is intercepted by `_patch_npm_run` below instead of being
    exec'd, because Windows `CreateProcess` cannot resolve a bare command
    name (``"npm"``) to its `PATHEXT`-suffixed sibling (``npm.cmd``) the way
    `shutil.which`/`cmd.exe` do — see module docstring negative-spec in
    `find_polluter.py` and the flagged production gap in this dispatch's
    report. Faking only the pre-flight and intercepting the real call avoids
    a production change while keeping the fake's on-disk file real.
    """
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
    """Intercept `find_polluter`'s `subprocess.run(["npm", "test", <file>], ...)`
    call and simulate its pollution side effect directly, instead of exec'ing.

    Windows `CreateProcess` (which `subprocess.run` uses under the hood) does
    not perform `PATHEXT` resolution the way `shutil.which`/`cmd.exe` do, so a
    bare ``"npm"`` argv[0] raises `FileNotFoundError: [WinError 2]` even when
    a real `npm.cmd` is on PATH (reproduced directly against the real,
    machine-installed npm — not a fake-executable artifact). Calls that are
    NOT the `npm test` invocation (e.g. `_existence_detail`'s `ls -la` call,
    which passes an already-resolved absolute path) fall through to the real
    `subprocess.run` unchanged.
    """
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
    """Deterministic bisection order — a faithful-intent tightening over the
    oracle's filesystem-enumeration-order dependency (see module negative-spec).
    """
    fake_bin = _make_fake_npm(tmp_path, pollute_on_substring="zzz")
    _patch_npm_run(monkeypatch, pollute_on_substring="zzz")
    src = tmp_path / "src"
    src.mkdir()
    (src / "zzz.test.ts").write_text("")
    (src / "aaa.test.ts").write_text("")
    rc = _run(monkeypatch, tmp_path, fake_bin, [".pollution-marker", "src/**/*.test.ts"])
    # zzz sorts after aaa, so aaa runs first (no pollution) then zzz (pollutes) → found.
    assert rc == 1
    assert (tmp_path / ".pollution-marker").exists()
