"""foreign_hunks names a holder run's hunks on a path with zero subprocess spawns."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import foreign_hunks as fh
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

PATH = "src/x.py"
HOLDER = "ask-20261026T000000-aaaaaa"
BASE_LINES = [f"line {i}\n" for i in range(1, 41)]


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _write(root, lines):
    p = root / PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes("".join(lines).encode())


def _edit(lines, at_line, n=1):
    out = list(lines)
    for k in range(n):
        out[at_line - 1 + 4 * k] = f"edited {at_line + 4 * k}\n"
    return out


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, BASE_LINES)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _holder(root, kept_lines, *, kept_blob=None):
    base = _git(root, "rev-parse", "HEAD")
    kept_file = root / "scratch_kept.txt"
    kept_file.write_bytes("".join(kept_lines).encode())
    blob = kept_blob or _git(root, "hash-object", "-w", str(kept_file))
    kept_file.unlink()
    rdir = root / "state" / "warp" / HOLDER
    rdir.mkdir(parents=True)
    (rdir / "halted.json").write_text(json.dumps({"status": "halted", "run_base_sha": base}), encoding="utf-8")
    (rdir / "kept.json").write_text(
        json.dumps({"chunks": {"C": {"paths": {PATH: blob}}}, "plan_gate": None}), encoding="utf-8"
    )


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_attributed_names_the_holders_edit_range(repo, monkeypatch):
    _holder(repo, _edit(BASE_LINES, 5))
    _write(repo, _edit(_edit(BASE_LINES, 5), 30))
    _no_spawn(monkeypatch)

    [(path, holder, headers, attributed)] = fh.foreign_hunks(repo, [(PATH, HOLDER)])

    assert (path, holder, attributed) == (PATH, HOLDER, True)
    assert headers == ["@@ -5,1 +5,1 @@"]


def test_unattributed_fallback_reads_head_against_worktree(repo, monkeypatch):
    _write(repo, _edit(BASE_LINES, 12))
    _no_spawn(monkeypatch)

    [(_, holder, headers, attributed)] = fh.foreign_hunks(repo, [(PATH, "ask-20261026T000000-bbbbbb")])

    assert attributed is False
    assert holder == "ask-20261026T000000-bbbbbb"
    assert headers == ["@@ -12,1 +12,1 @@"]


def test_unreadable_kept_blob_falls_back_without_raising(repo):
    _holder(repo, BASE_LINES, kept_blob="0" * 40)
    _write(repo, _edit(BASE_LINES, 9))

    [(_, _, headers, attributed)] = fh.foreign_hunks(repo, [(PATH, HOLDER)])

    assert attributed is False
    assert headers == ["@@ -9,1 +9,1 @@"]


def test_clean_path_and_missing_file_yield_nothing(repo):
    assert fh.foreign_hunks(repo, [(PATH, HOLDER), ("absent.py", HOLDER)]) == []


def test_cap_limits_headers_and_total_keeps_the_count(repo):
    kept = _edit(BASE_LINES, 2, n=6)
    _holder(repo, kept)

    [(_, _, headers, attributed)] = fh.foreign_hunks(repo, [(PATH, HOLDER)], cap=3)

    assert attributed is True
    assert len(headers) == 3
    assert headers.total == 6
    assert headers[0] == "@@ -2,1 +2,1 @@"


def _crlf(lines):
    return "".join(lines).replace("\n", "\r\n").encode()


def _autocrlf_repo(root, value):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", value)
    p = root / PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes("".join(BASE_LINES[:20]).encode())
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _raw_holder(root, raw):
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import write_object

    blob = write_object(resolve_git_common_dir(root), b"blob", raw)
    rdir = root / "state" / "warp" / HOLDER
    rdir.mkdir(parents=True)
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_base_sha": _git(root, "rev-parse", "HEAD")}), encoding="utf-8"
    )
    (rdir / "kept.json").write_text(
        json.dumps({"chunks": {"C": {"paths": {PATH: blob}}}, "plan_gate": None}), encoding="utf-8"
    )


def test_crlf_kept_blob_against_lf_base_names_only_the_changed_line(tmp_path):
    root = _autocrlf_repo(tmp_path, "true")
    _raw_holder(root, _crlf(_edit(BASE_LINES[:20], 5)))

    [(_, _, headers, attributed)] = fh.foreign_hunks(root, [(PATH, HOLDER)])

    assert attributed is True
    assert headers == ["@@ -5,1 +5,1 @@"]


def test_crlf_worktree_against_lf_head_names_only_the_changed_line(tmp_path):
    root = _autocrlf_repo(tmp_path, "true")
    (root / PATH).write_bytes(_crlf(_edit(BASE_LINES[:20], 5)))

    [(_, _, headers, attributed)] = fh.foreign_hunks(root, [(PATH, HOLDER)])

    assert attributed is False
    assert headers == ["@@ -5,1 +5,1 @@"]


def test_refused_lift_contributes_nothing_and_does_not_raise(tmp_path):
    root = _autocrlf_repo(tmp_path, "false")
    raw = _crlf(_edit(BASE_LINES[:20], 5))
    _raw_holder(root, raw)
    (root / PATH).write_bytes(raw)

    assert fh.foreign_hunks(root, [(PATH, HOLDER)]) == []


@pytest.mark.parametrize("n", [1, 10])
def test_zero_spawns_whatever_the_hunk_count(repo, monkeypatch, n):
    _holder(repo, _edit(BASE_LINES, 1, n=n))
    _write(repo, _edit(BASE_LINES, 1, n=n))
    _no_spawn(monkeypatch)

    [(_, _, headers, attributed)] = fh.foreign_hunks(repo, [(PATH, HOLDER)], cap=50)

    assert attributed is True
    assert headers.total == n
