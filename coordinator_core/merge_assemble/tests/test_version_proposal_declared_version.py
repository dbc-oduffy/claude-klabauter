"""The release-tag proposal names the declared package version and prefers it
when it is ahead of the latest tag."""
from __future__ import annotations

from types import SimpleNamespace

from coordinator_core import merge_assemble


def _stub_git(monkeypatch, local: str, remote_refs: str = "") -> None:
    def _fake(args, cwd, **_kw):
        if args[0] == "tag":
            return SimpleNamespace(returncode=0, stdout=local, stderr="")
        return SimpleNamespace(returncode=0, stdout=remote_refs, stderr="")

    monkeypatch.setattr(merge_assemble, "_run_git", _fake)


def _pyproject(tmp_path, version: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')


def test_declared_ahead_of_tag_is_proposed(tmp_path, monkeypatch):
    _stub_git(monkeypatch, "v0.14.1\n")
    _pyproject(tmp_path, "0.15.0")
    assert merge_assemble.compute_version_bump_proposal(tmp_path) == {
        "current": "v0.14.1",
        "declared": "0.15.0",
        "proposed": "v0.15.0",
        "bump": "declared",
    }


def test_declared_behind_tag_keeps_patch_bump_and_names_both(tmp_path, monkeypatch):
    _stub_git(monkeypatch, "v0.14.1\n")
    _pyproject(tmp_path, "0.1.0")
    assert merge_assemble.compute_version_bump_proposal(tmp_path) == {
        "current": "v0.14.1",
        "declared": "0.1.0",
        "proposed": "v0.14.2",
        "bump": "patch",
    }


def test_declared_tag_taken_on_origin_falls_back_to_patch(tmp_path, monkeypatch):
    _stub_git(monkeypatch, "v0.14.1\n", "a\trefs/tags/v0.15.0\n")
    _pyproject(tmp_path, "0.15.0")
    result = merge_assemble.compute_version_bump_proposal(tmp_path)
    assert (result["proposed"], result["bump"]) == ("v0.14.2", "patch")


def test_package_json_is_the_fallback_source(tmp_path, monkeypatch):
    _stub_git(monkeypatch, "v1.0.0\n")
    (tmp_path / "package.json").write_text('{"version": "1.2.0"}')
    result = merge_assemble.compute_version_bump_proposal(tmp_path)
    assert (result["proposed"], result["declared"]) == ("v1.2.0", "1.2.0")


def test_unparseable_declared_version_is_ignored(tmp_path, monkeypatch):
    _stub_git(monkeypatch, "v1.0.0\n")
    _pyproject(tmp_path, "1.2.0rc1")
    assert merge_assemble.compute_version_bump_proposal(tmp_path) == {
        "current": "v1.0.0",
        "proposed": "v1.0.1",
        "bump": "patch",
    }
