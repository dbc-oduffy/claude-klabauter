"""`machine-local get` answers for repos.content_root and its legacy name
repos.content_root from either key, matching machine_resolver.registry_get.

Spec backlink: docs/research/2026-10-16-unclear-claude-author-registry-key.md
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"


@pytest.fixture
def ml():
    spec = importlib.util.spec_from_file_location("_machine_local_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registry(tmp_path: Path, monkeypatch, key: str) -> Path:
    target = tmp_path / "content"
    target.mkdir()
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "registry.local.toml").write_text(
        f'[repos]\n{key} = "{target.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)
    return target


@pytest.mark.parametrize(
    "stored,asked",
    [
        ("content_root", "repos.content_root"),
        ("content_root", "repos.content_root"),
        ("content_root", "repos.content_root"),
        ("content_root", "repos.content_root"),
    ],
)
def test_get_falls_back_across_the_key_pair(ml, tmp_path, monkeypatch, stored, asked):
    target = _registry(tmp_path, monkeypatch, stored)
    rc, val = ml.resolve_one(asked)
    assert rc == ml.EXIT_OK
    assert Path(val) == target


def test_unrelated_repo_key_does_not_fall_back(ml, tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, "content_root")
    rc, _ = ml.resolve_one("repos.no_such_repo_xyz")
    assert rc == ml.EXIT_NOT_FOUND
