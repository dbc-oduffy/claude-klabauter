"""Tests for `comment_only_verifier`: declaration reading and the single-spawn, fail-closed
`verify_pairs`. Fixture verifier is a Python script under tmp_path; HEAD is faked by patching
`head_blobs` except in one real-repo test."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import comment_only_verifier as cov
from coordinator_core.ops.dispatch_emit.comment_only_verifier import (
    VerifierDecl,
    declared_verifier,
    verify_pairs,
)

_VERIFIER = (
    "import sys, json, time\n"
    "d = json.load(sys.stdin)\n"
    "if any('SLEEP' in p['after'] for p in d['pairs']): time.sleep(5)\n"
    "sys.exit(1 if any('BAD' in p['after'] for p in d['pairs']) else 0)\n"
)
_PAIRS = [("a.ts", b"x\n", b"x // c\n"), ("b.ts", b"y\n", b"y // c\n"), ("c.ts", b"z\n", b"z\n")]


def _sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def _write_md(root: Path, argv: str, suffixes: str) -> None:
    (root / "coordinator.local.md").write_text(
        f"---\ncomment_only_verifier: {argv}\ncomment_only_verifier_suffixes: {suffixes}\n---\n",
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "v.py").write_text(_VERIFIER, encoding="utf-8")
    decl = VerifierDecl(argv=(sys.executable, "v.py"), suffixes=frozenset({".ts"}))

    def fake_head(root, paths):
        out = {}
        for p in paths:
            f = Path(root) / p
            if f.is_file():
                out[p] = (0o100644, _sha(f.read_bytes()))
        return out

    monkeypatch.setattr("coordinator_core.git.git_state.head_blobs", fake_head)
    return tmp_path, decl


def test_no_file_is_none(tmp_path):
    assert declared_verifier(tmp_path) is None


def test_missing_key_is_none(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        '---\ncomment_only_verifier: ["node", "x.mjs"]\n---\n', encoding="utf-8"
    )
    assert declared_verifier(tmp_path) is None


@pytest.mark.parametrize(
    "argv,suffixes",
    [
        ('["node"]', "[]"),
        ("[]", '[".ts"]'),
        ("node x", '[".ts"]'),
        ('["node"]', '["ts"]'),
        ('["node"]', '["."]'),
        ('["", "x"]', '[".ts"]'),
    ],
)
def test_malformed_is_none(tmp_path, argv, suffixes):
    _write_md(tmp_path, argv, suffixes)
    assert declared_verifier(tmp_path) is None


def test_well_formed_declaration(tmp_path):
    _write_md(tmp_path, '["node", "scripts/x.mjs", "verify-pairs"]', '[".TS", ".tsx"]')
    decl = declared_verifier(tmp_path)
    assert decl == VerifierDecl(
        argv=("node", "scripts/x.mjs", "verify-pairs"), suffixes=frozenset({".ts", ".tsx"})
    )
    assert cov.covers(decl, "src/A.TS")
    assert not cov.covers(decl, "src/a.js")


def test_does_not_walk_up(tmp_path):
    _write_md(tmp_path, '["node"]', '[".ts"]')
    child = tmp_path / "sub"
    child.mkdir()
    assert declared_verifier(child) is None


def test_exit_zero_true(repo):
    root, decl = repo
    assert verify_pairs(root, decl, _PAIRS, ()) is True


def test_exit_one_false(repo):
    root, decl = repo
    assert verify_pairs(root, decl, [("a.ts", b"x", b"BAD")], ()) is False


def test_timeout_false(repo):
    root, decl = repo
    assert verify_pairs(root, decl, [("a.ts", b"x", b"SLEEP")], ()) is False


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(cov.subprocess, "run", boom)


def test_empty_pairs_false_no_spawn(repo, monkeypatch):
    root, decl = repo
    _no_spawn(monkeypatch)
    assert verify_pairs(root, decl, [], ()) is False


def test_argv0_not_found_false_no_spawn(repo, monkeypatch):
    root, _ = repo
    _no_spawn(monkeypatch)
    decl = VerifierDecl(argv=("no-such-exe-xyz", "v.py"), suffixes=frozenset({".ts"}))
    assert verify_pairs(root, decl, _PAIRS, ()) is False


def test_verifier_in_untrusted_false_no_spawn(repo, monkeypatch):
    root, decl = repo
    _no_spawn(monkeypatch)
    assert verify_pairs(root, decl, _PAIRS, ["v.py"]) is False


def test_local_md_in_untrusted_false_no_spawn(repo, monkeypatch):
    root, decl = repo
    _no_spawn(monkeypatch)
    assert verify_pairs(root, decl, _PAIRS, ["coordinator.local.md"]) is False


def test_backslashed_untrusted_matches(repo, monkeypatch):
    root, _ = repo
    (root / "tools").mkdir()
    (root / "tools" / "v.py").write_text(_VERIFIER, encoding="utf-8")
    decl = VerifierDecl(argv=(sys.executable, "tools/v.py"), suffixes=frozenset({".ts"}))
    _no_spawn(monkeypatch)
    assert verify_pairs(root, decl, _PAIRS, ["tools\\v.py"]) is False


def test_cmd_suffix_false_no_spawn(repo, monkeypatch):
    root, _ = repo
    (root / "v.cmd").write_text("@exit 0\r\n", encoding="utf-8")
    _no_spawn(monkeypatch)
    decl = VerifierDecl(argv=("v.cmd",), suffixes=frozenset({".ts"}))
    assert verify_pairs(root, decl, _PAIRS, ()) is False


def test_one_spawn_for_three_pairs(repo, monkeypatch):
    root, decl = repo
    calls = []
    real = subprocess.run

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(cov.subprocess, "run", counting)
    assert verify_pairs(root, decl, _PAIRS, ()) is True
    assert len(calls) == 1


def test_undecodable_bytes_false_no_spawn(repo, monkeypatch):
    root, decl = repo
    _no_spawn(monkeypatch)
    assert verify_pairs(root, decl, [("a.ts", b"\xff", b"x")], ()) is False


def test_crlf_folded_before_send(repo):
    root, decl = repo
    (root / "v.py").write_text(
        "import sys, json\nd = json.load(sys.stdin)\n"
        "sys.exit(1 if any('\\r' in p['before'] + p['after'] for p in d['pairs']) else 0)\n",
        encoding="utf-8",
    )
    assert verify_pairs(root, decl, [("a.ts", b"x\r\n", b"x\r\ny\r\n")], ()) is True


def test_verifier_edited_vs_head_false_no_spawn(repo, monkeypatch):
    root, decl = repo
    monkeypatch.setattr(
        "coordinator_core.git.git_state.head_blobs",
        lambda r, paths: {p: (0o100644, _sha(b"old")) for p in paths},
    )
    _no_spawn(monkeypatch)
    assert verify_pairs(root, decl, _PAIRS, ()) is False


def test_verifier_not_in_head_false_no_spawn(repo, monkeypatch):
    root, decl = repo
    monkeypatch.setattr("coordinator_core.git.git_state.head_blobs", lambda r, paths: {})
    _no_spawn(monkeypatch)
    assert verify_pairs(root, decl, _PAIRS, ()) is False


def test_untracked_local_md_allowed_edited_tracked_refused(repo, monkeypatch):
    root, decl = repo
    _write_md(root, '["x"]', '[".ts"]')

    def head(r, paths):
        return {p: (0o100644, _sha((Path(r) / p).read_bytes())) for p in paths if p == "v.py"}

    monkeypatch.setattr("coordinator_core.git.git_state.head_blobs", head)
    assert verify_pairs(root, decl, _PAIRS, ()) is True

    def head_stale_md(r, paths):
        out = head(r, paths)
        if "coordinator.local.md" in paths:
            out["coordinator.local.md"] = (0o100644, _sha(b"old"))
        return out

    monkeypatch.setattr("coordinator_core.git.git_state.head_blobs", head_stale_md)
    _no_spawn(monkeypatch)
    assert verify_pairs(root, decl, _PAIRS, ()) is False


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_real_repo_head_check(tmp_path):
    (tmp_path / "v.py").write_text(_VERIFIER, encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
        "HOME": str(tmp_path),
    }
    for cmd in (["init", "-q"], ["add", "v.py"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *cmd], cwd=tmp_path, env=env, check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    decl = VerifierDecl(argv=(sys.executable, "v.py"), suffixes=frozenset({".ts"}))
    assert verify_pairs(tmp_path, decl, _PAIRS, ()) is True
    (tmp_path / "v.py").write_text(_VERIFIER + "# edited\n", encoding="utf-8")
    assert verify_pairs(tmp_path, decl, _PAIRS, ()) is False
