"""The relaxed overlay keying still refuses when a path the verdict rests on moved."""
from __future__ import annotations

import subprocess

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov


def _git_names(monkeypatch, names):
    out = subprocess.CompletedProcess([], 0, stdout="\0".join(names) + "\0", stderr="")
    monkeypatch.setattr(cov, "run_git", lambda *a, **k: out)


def test_base_advance_touching_a_watched_path_is_not_clear(monkeypatch, tmp_path):
    _git_names(monkeypatch, ["a.py", "t/test_x.py"])
    assert not cov._base_advance_clear(tmp_path, "b1", "b2", {"t/test_x.py"})


def test_base_advance_touching_nothing_watched_is_clear(monkeypatch, tmp_path):
    _git_names(monkeypatch, ["other.py"])
    assert cov._base_advance_clear(tmp_path, "b1", "b2", {"t/test_x.py"})


def test_unreadable_base_advance_is_not_clear(monkeypatch, tmp_path):
    monkeypatch.setattr(cov, "run_git", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="x"))
    assert not cov._base_advance_clear(tmp_path, "b1", "b2", set())


def test_peer_hunk_path_churn_leaves_the_key_stable():
    base = {"base_sha": "b", "blobs": {"p": "w1"}, "peer_hunks": {"p": [1]}}
    assert cov.landing_key(base) == cov.landing_key({**base, "blobs": {"p": "w2"}, "sha": "z"})
    assert cov.landing_key({"base_sha": "b", "blobs": {"p": "w1"}}) != cov.landing_key({"base_sha": "b", "blobs": {"p": "w2"}})
