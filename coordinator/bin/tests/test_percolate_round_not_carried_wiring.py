"""test_percolate_round_not_carried_wiring -- pins that `_cmd_round_default`'s
call site hands the classifier's benign buckets (identical-to-HEAD, and
gitignored + absent) to the commit subject and body, not just that those
helpers behave when hand-fed.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_not_carried_wiring.py -q
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
        "percolate_round_not_carried_wiring", _BIN_DIR / "percolate-round.py"
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


def test_call_site_passes_benign_buckets_to_subject_and_body(
    tmp_path, dest_repo, monkeypatch
):
    buckets = {
        "identical_to_head": ["same.py"],
        "gitignored": ["x.pyc"],
        "absent": ["gone.py"],
        "unaccounted": [],
    }
    monkeypatch.setattr(_mod, "_classify_dropped_paths", lambda *a, **k: buckets)
    seen = {}
    real_subject = _mod._build_commit_subject
    real_prose = _mod._not_carried_prose

    def _subject(*a, **k):
        seen["subject"] = k
        return real_subject(*a, **k)

    def _prose(dropped, unchanged_paths=(), explained_paths=()):
        seen["prose"] = (list(unchanged_paths), list(explained_paths))
        return real_prose(dropped, unchanged_paths, explained_paths)

    monkeypatch.setattr(_mod, "_build_commit_subject", _subject)
    monkeypatch.setattr(_mod, "_not_carried_prose", _prose)

    assert _run_round(tmp_path, dest_repo, monkeypatch) == _mod._EXIT_OK

    assert list(seen["subject"]["unchanged_paths"]) == ["same.py"]
    assert sorted(seen["subject"]["explained_paths"]) == ["gone.py", "x.pyc"]
    assert seen["prose"][0] == ["same.py"]
    assert sorted(seen["prose"][1]) == ["gone.py", "x.pyc"]
