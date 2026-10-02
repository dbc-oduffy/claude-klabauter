"""test_percolate_round_exec_bit_ordering -- pins where `_cmd_round_default`
calls publish.py's `_normalize_dest_exec_bits`: BEFORE the commit pathspec is
partitioned, so a re-moded path is IN the commit rather than staged-and-left.

Why the ordering is load-bearing: `_partition_pathspec_for_commit` freezes the
pathspec the commit and the subject read. A path re-moded after that point
misses both, and the mirror stays mode-dirty exactly as before the normalizer
existed. An earlier draft had precisely that defect.

Behavioural, never a source-order grep: a real dest repo holds a tracked,
unchanged, NON-shebanged file at index mode 100755 under a published dest dir,
and the round runs end to end through its real commit leg (only the publish
spawn and the three sibling-CLI steps are stubbed). The demote direction is
the one the round's own `_stage_shebang_exec_bits` reconcile cannot cover, so
the normalizer call alone decides whether the path reaches HEAD.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_exec_bit_ordering.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_exec_bit_ordering", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()

_STUCK = "bin/plain.md"
_CHANGED = "bin/notes.md"


def _git(repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=True,
    )


def _head_mode(repo: Path, rel: str) -> str:
    return _git(repo, "ls-tree", "HEAD", "--", rel).stdout.split(" ", 1)[0]


@pytest.fixture
def dest_repo(tmp_path):
    repo = tmp_path / "dest"
    (repo / "bin").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "core.fileMode", "false")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / _STUCK).write_text("# not a script\n", encoding="utf-8")
    (repo / _CHANGED).write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "--chmod=+x", "--", _STUCK)
    _git(repo, "add", "--", _CHANGED)
    _git(repo, "commit", "-q", "-m", "seed")
    assert _head_mode(repo, _STUCK) == "100755"
    (repo / _CHANGED).write_text("v2\n", encoding="utf-8")
    return repo


def _run_round(tmp_path: Path, repo: Path, monkeypatch) -> int:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    manifest = _mod._RoundManifest(
        round_id="test",
        added_or_updated=frozenset({_CHANGED}),
        removed=frozenset(),
        declared_payload=frozenset({_CHANGED, _STUCK}),
        published_dest_dirs=frozenset({"bin"}),
    )
    monkeypatch.setattr(
        _mod, "_read_fresh_round_manifest", lambda _root, _not_before: manifest
    )

    real_run = _mod._run

    def _run_without_publish(cmd, **kwargs):
        if str(_mod._PUBLISH) in [str(c) for c in cmd]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    scan_payload = json.dumps(
        {
            "schema": "scan-secrets.v1",
            "counts": {"high": 0, "medium_informational": 0, "medium_gating": 0, "low": 0},
            "render": "Content-leakage scan:\n  (none)\n",
        }
    )
    gate_payload = json.dumps(
        {"gates": {"step3_gate_fires": False}, "judgment_points": []}
    )

    def _fake_step(script, argv):
        sub = argv[0]
        stdout = {
            "scan-secrets": scan_payload,
            "inverse-drift": "anchor_mode: 30day-fallback\n",
            "parse-dryrun": gate_payload,
        }.get(sub)
        if stdout is None:
            raise AssertionError(f"unmodelled step: {sub}")
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(_mod, "_run", _run_without_publish)
    monkeypatch.setattr(_mod, "_run_step", _fake_step)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(repo))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    args = _mod._build_parser().parse_args(
        ["alpha", "--percolate-root", str(percolate_root), "--yes", "--no-publish"]
    )
    with contextlib.redirect_stdout(io.StringIO()):
        return _mod._cmd_round(args)


def test_a_remoded_path_lands_in_the_commit_not_staged_uncommitted(
    tmp_path, dest_repo, monkeypatch
):
    rc = _run_round(tmp_path, dest_repo, monkeypatch)
    assert rc == _mod._EXIT_OK

    committed = _git(dest_repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert _CHANGED in committed
    assert _STUCK in committed
    assert _head_mode(dest_repo, _STUCK) == "100644"
    assert _git(dest_repo, "status", "--porcelain").stdout == ""
