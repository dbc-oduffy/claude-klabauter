"""
coordinator_core.plugin_health.tests.test_release_currency

Pytest port of example-doctrine-repo coordinator/lib/release-currency.sh's
`release_currency_probe` (bash oracle, retired on cutover — see git log) and of
sentinel.py's `probe_p19` result->ProbeNote rewire. Exercises every status string
(source_is_live/current/behind/behind-clone/differs/offline), the three surviving
source_is_live triggers (env override, contributor-clone-registry live_path match,
no-version.txt-and-not-a-worktree fallback — the 4th "script inside install_root"
trigger has no native analog, see release_currency.py's module docstring), the
The Staff Engineer-F4-pinned numeric-prefix-only highest-tag selection (NOT PEP440), the
annotated-tag ^{} peel, merge-base ancestry, the tag allowlist gate, the
RELEASE_CURRENCY_FORCE_OFFLINE shim, and the ImportError/unexpected-exception ->
_absent ProbeNote remap (the Staff Engineer F6).

Spec backlink: docs/plans/2026-07-17-retire-doe-bash-bridges-native-python.md § Port C.
Port of: release-currency.sh (example-doctrine-repo 5aa75f3b, 2026-07-21)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional

import pytest

from coordinator_core.plugin_health import release_currency as rc
from coordinator_core.plugin_health.sentinel import probe_p19


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


def _no_subprocess_allowed(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - assertion path only
        raise AssertionError(f"unexpected subprocess.run call: {args!r}")

    monkeypatch.setattr(rc.subprocess, "run", _boom)


def _write_registry(tmp_path: Path, live_path: str) -> Path:
    ml = tmp_path / "machine-local"
    ml.mkdir(parents=True, exist_ok=True)
    (ml / "registry.local.toml").write_text(
        '[plugin.mirrors.coordinator-claude]\n'
        f'live_path = "{live_path}"\n',
        encoding="utf-8",
    )
    return ml


# ---------------------------------------------------------------------------
# source_is_live — the three surviving triggers
# ---------------------------------------------------------------------------


def test_source_is_live_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", "1")
    _no_subprocess_allowed(monkeypatch)
    result = rc.release_currency_probe("coordinator", "o/r", tmp_path)
    assert result == "source_is_live"


def test_source_is_live_registry_live_path_match(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", raising=False)
    install_root = tmp_path / "install"
    install_root.mkdir()
    ml = _write_registry(tmp_path, str(install_root))
    monkeypatch.setattr(rc, "machine_local_dir", lambda: ml)
    _no_subprocess_allowed(monkeypatch)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "source_is_live"


def test_source_is_live_registry_trailing_slash_normalized(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", raising=False)
    install_root = tmp_path / "install"
    install_root.mkdir()
    ml = _write_registry(tmp_path, str(install_root) + "/")
    monkeypatch.setattr(rc, "machine_local_dir", lambda: ml)
    _no_subprocess_allowed(monkeypatch)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "source_is_live"


def test_no_version_txt_not_a_worktree_falls_back_to_source_is_live(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", raising=False)
    monkeypatch.setattr(rc, "machine_local_dir", lambda: tmp_path / "no-such-machine-local")
    install_root = tmp_path / "plain"
    install_root.mkdir()

    def _fake_run(cmd, timeout):
        assert cmd[:4] == ["git", "-C", str(install_root), "rev-parse"]
        return _FakeProc(returncode=128, stdout="")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "source_is_live"


# ---------------------------------------------------------------------------
# behind-clone / current (git-worktree, no version.txt)
# ---------------------------------------------------------------------------


def test_behind_clone_positive_count(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", raising=False)
    monkeypatch.setattr(rc, "machine_local_dir", lambda: tmp_path / "no-such-machine-local")
    install_root = tmp_path / "clone"
    install_root.mkdir()

    def _fake_run(cmd, timeout):
        if cmd[-1] == "--is-inside-work-tree":
            return _FakeProc(0, "true")
        if cmd[3] == "fetch":
            return _FakeProc(0, "")
        if cmd[3:6] == ["rev-list", "--count", "HEAD..@{u}"]:
            return _FakeProc(0, "3\n")
        if cmd[3:6] == ["rev-parse", "--abbrev-ref", "@{u}"]:
            return _FakeProc(0, "origin/main\n")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "behind-clone 3 origin/main"


def test_behind_clone_zero_count_is_current(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", raising=False)
    monkeypatch.setattr(rc, "machine_local_dir", lambda: tmp_path / "no-such-machine-local")
    install_root = tmp_path / "clone"
    install_root.mkdir()

    def _fake_run(cmd, timeout):
        if cmd[-1] == "--is-inside-work-tree":
            return _FakeProc(0, "true")
        if cmd[3] == "fetch":
            return _FakeProc(0, "")
        if cmd[3:6] == ["rev-list", "--count", "HEAD..@{u}"]:
            return _FakeProc(0, "0\n")
        if cmd[3:6] == ["rev-parse", "--abbrev-ref", "@{u}"]:
            return _FakeProc(0, "origin/main\n")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "current"


def test_worktree_fetch_failure_is_offline(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", raising=False)
    monkeypatch.setattr(rc, "machine_local_dir", lambda: tmp_path / "no-such-machine-local")
    install_root = tmp_path / "clone"
    install_root.mkdir()

    def _fake_run(cmd, timeout):
        if cmd[-1] == "--is-inside-work-tree":
            return _FakeProc(0, "true")
        if cmd[3] == "fetch":
            return _FakeProc(1, "")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "offline"


# ---------------------------------------------------------------------------
# version.txt present -> current / behind / differs / offline
# ---------------------------------------------------------------------------


_SHA_LOCAL = "a" * 40
_SHA_TAG = "b" * 40


def _write_version_txt(install_root: Path, sha: str) -> None:
    install_root.mkdir(parents=True, exist_ok=True)
    (install_root / "version.txt").write_text(sha + "\n", encoding="utf-8")


def test_current_when_local_sha_matches_tag_sha(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    _write_version_txt(install_root, _SHA_LOCAL)

    def _fake_run(cmd, timeout):
        if cmd[:2] == ["git", "ls-remote"] and "--tags" in cmd:
            return _FakeProc(0, f"{_SHA_LOCAL}\trefs/tags/v1.0.0\n")
        if cmd[:2] == ["git", "ls-remote"] and cmd[-1].endswith("^{}"):
            return _FakeProc(0, f"{_SHA_LOCAL}\trefs/tags/v1.0.0^{{}}\n")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "current"


def test_behind_when_ancestor(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    _write_version_txt(install_root, _SHA_LOCAL)
    (install_root / ".git").mkdir()

    def _fake_run(cmd, timeout):
        if cmd[:2] == ["git", "ls-remote"] and "--tags" in cmd:
            return _FakeProc(0, f"{_SHA_TAG}\trefs/tags/v2.0.0\n")
        if cmd[:2] == ["git", "ls-remote"] and cmd[-1].endswith("^{}"):
            return _FakeProc(0, f"{_SHA_TAG}\trefs/tags/v2.0.0^{{}}\n")
        if cmd[3:6] == ["merge-base", "--is-ancestor", _SHA_LOCAL]:
            return _FakeProc(0, "")
        if cmd[3:6] == ["describe", "--tags", "--exact-match"]:
            return _FakeProc(1, "")
        if cmd[3:5] == ["describe", "--tags"]:
            return _FakeProc(0, "v1.9.0-3-gabcdef1\n")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "behind v1.9.0-3-gabcdef1 v2.0.0"


def test_behind_from_label_falls_back_to_short_sha_when_describe_fails(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    _write_version_txt(install_root, _SHA_LOCAL)
    (install_root / ".git").mkdir()

    def _fake_run(cmd, timeout):
        if cmd[:2] == ["git", "ls-remote"] and "--tags" in cmd:
            return _FakeProc(0, f"{_SHA_TAG}\trefs/tags/v2.0.0\n")
        if cmd[:2] == ["git", "ls-remote"] and cmd[-1].endswith("^{}"):
            return _FakeProc(0, f"{_SHA_TAG}\trefs/tags/v2.0.0^{{}}\n")
        if cmd[3:6] == ["merge-base", "--is-ancestor", _SHA_LOCAL]:
            return _FakeProc(0, "")
        if cmd[3:6] == ["describe", "--tags", "--exact-match"]:
            return _FakeProc(1, "")
        if cmd[3:5] == ["describe", "--tags"]:
            return _FakeProc(1, "")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == f"behind {_SHA_LOCAL[:12]} v2.0.0"


def test_differs_when_not_ancestor(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    _write_version_txt(install_root, _SHA_LOCAL)
    (install_root / ".git").mkdir()

    def _fake_run(cmd, timeout):
        if cmd[:2] == ["git", "ls-remote"] and "--tags" in cmd:
            return _FakeProc(0, f"{_SHA_TAG}\trefs/tags/v2.0.0\n")
        if cmd[:2] == ["git", "ls-remote"] and cmd[-1].endswith("^{}"):
            return _FakeProc(0, f"{_SHA_TAG}\trefs/tags/v2.0.0^{{}}\n")
        if cmd[3:6] == ["merge-base", "--is-ancestor", _SHA_LOCAL]:
            return _FakeProc(1, "")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "differs v2.0.0"


def test_offline_when_ls_remote_returns_nothing(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    _write_version_txt(install_root, _SHA_LOCAL)

    def _fake_run(cmd, timeout):
        if cmd[:2] == ["git", "ls-remote"]:
            return _FakeProc(0, "")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "offline"


def test_offline_when_tag_fails_allowlist(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    _write_version_txt(install_root, _SHA_LOCAL)
    monkeypatch.setattr(rc, "_fetch_latest_release_tag", lambda owner_repo, force_offline: "bad tag!")
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "offline"


def test_offline_when_tag_sha_unresolvable(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    _write_version_txt(install_root, _SHA_LOCAL)

    def _fake_run(cmd, timeout):
        if cmd[:2] == ["git", "ls-remote"] and "--tags" in cmd:
            return _FakeProc(0, f"{_SHA_TAG}\trefs/tags/v2.0.0\n")
        if cmd[:2] == ["git", "ls-remote"]:
            return _FakeProc(0, "")
        raise AssertionError(f"unexpected cmd {cmd!r}")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "offline"


def test_force_offline_shim_short_circuits(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    _write_version_txt(install_root, _SHA_LOCAL)
    monkeypatch.setenv("RELEASE_CURRENCY_FORCE_OFFLINE", "1")
    _no_subprocess_allowed(monkeypatch)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "offline"


# ---------------------------------------------------------------------------
# Highest-tag selection (the Staff Engineer F4 — numeric-prefix-only, NOT PEP440)
# ---------------------------------------------------------------------------


def test_highest_tag_picks_numeric_major_minor_patch():
    assert rc._select_highest_tag(["v1.2.0", "v1.10.0", "v1.9.0"]) == "v1.10.0"


def test_highest_tag_prerelease_co_present_with_release_locks_lexical_tiebreak():
    # the Staff Engineer F4: numeric key ties (2,14,0) for both tags; the bash pipeline's
    # trailing `sort | tail -1` then breaks the tie LEXICALLY on the raw tag
    # string, and "v2.14.0-rc1" (longer, shares the "v2.14.0" prefix) sorts
    # after "v2.14.0" — so the prerelease wins the tie. PEP440 would order the
    # release above its prerelease; this native port must NOT do that.
    result = rc._select_highest_tag(["v2.14.0-rc1", "v2.14.0"])
    assert result == "v2.14.0-rc1"


def test_highest_tag_ignores_build_metadata_suffix():
    # Numeric key ties (1,0,0) for both tags; the lexical tie-break then picks
    # the longer string (mirrors the prerelease tie-break's reasoning above) --
    # "v1.0.0+build.5" sorts after its "v1.0.0" prefix.
    assert rc._select_highest_tag(["v1.0.0+build.5", "v1.0.0"]) == "v1.0.0+build.5"


def test_highest_tag_empty_list_returns_none():
    assert rc._select_highest_tag([]) is None


# ---------------------------------------------------------------------------
# Annotated-tag ^{} peel
# ---------------------------------------------------------------------------


def test_resolve_tag_sha_prefers_peeled_annotated_sha(monkeypatch):
    calls = []

    def _fake_run(cmd, timeout):
        calls.append(cmd)
        if cmd[-1].endswith("^{}"):
            return _FakeProc(0, f"{_SHA_TAG}\trefs/tags/v2.0.0^{{}}\n")
        raise AssertionError("bare-ref query should not run when peel succeeds")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc._resolve_tag_sha("o/r", "v2.0.0", force_offline=False)
    assert result == _SHA_TAG
    assert len(calls) == 1


def test_resolve_tag_sha_falls_back_to_lightweight_ref(monkeypatch):
    def _fake_run(cmd, timeout):
        if cmd[-1].endswith("^{}"):
            return _FakeProc(0, "")
        return _FakeProc(0, f"{_SHA_LOCAL}\trefs/tags/v2.0.0\n")

    monkeypatch.setattr(rc, "_run", _fake_run)
    result = rc._resolve_tag_sha("o/r", "v2.0.0", force_offline=False)
    assert result == _SHA_LOCAL


# ---------------------------------------------------------------------------
# Merge-base ancestry / tag allowlist gate — direct unit coverage
# ---------------------------------------------------------------------------


def test_check_ancestry_false_without_git_dir(tmp_path):
    assert rc._check_ancestry(str(tmp_path), _SHA_LOCAL, _SHA_TAG) is False


def test_check_ancestry_true(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(rc, "_run", lambda cmd, timeout: _FakeProc(0, ""))
    assert rc._check_ancestry(str(tmp_path), _SHA_LOCAL, _SHA_TAG) is True


@pytest.mark.parametrize(
    "tag,ok",
    [
        ("v2.0.0", True),
        ("v2.0.0-rc1", True),
        ("-evil", False),
        ("v2.0.0; rm -rf /", False),
        ("", False),
    ],
)
def test_tag_allowlist_gate(tag, ok):
    assert bool(rc._TAG_ALLOWLIST_RE.match(tag)) is ok


# ---------------------------------------------------------------------------
# probe_p19 — result -> ProbeNote mapping + _absent remap (the Staff Engineer F6)
# ---------------------------------------------------------------------------


def _sentinel_module():
    import coordinator_core.plugin_health.sentinel as sentinel_mod

    return sentinel_mod


def test_probe_p19_lib_dir_none_is_absent(tmp_path):
    notes = probe_p19(None, tmp_path)
    assert len(notes) == 1
    assert notes[0].id == "P-19"
    assert "skipped" in notes[0].message


def test_probe_p19_maps_current_to_empty(tmp_path, monkeypatch):
    sentinel_mod = _sentinel_module()
    monkeypatch.setattr(
        "coordinator_core.plugin_health.release_currency.release_currency_probe",
        lambda plugin, repo, root: "current",
    )
    notes = sentinel_mod.probe_p19(tmp_path, tmp_path)
    assert notes == []


def test_probe_p19_maps_behind_clone(tmp_path, monkeypatch):
    sentinel_mod = _sentinel_module()
    monkeypatch.setattr(
        "coordinator_core.plugin_health.release_currency.release_currency_probe",
        lambda plugin, repo, root: "behind-clone 4 origin/main",
    )
    notes = sentinel_mod.probe_p19(tmp_path, tmp_path)
    assert len(notes) == 1
    assert "4 commits behind origin/main" in notes[0].message


def test_probe_p19_maps_behind(tmp_path, monkeypatch):
    sentinel_mod = _sentinel_module()
    monkeypatch.setattr(
        "coordinator_core.plugin_health.release_currency.release_currency_probe",
        lambda plugin, repo, root: "behind v1.9.0 v2.0.0",
    )
    notes = sentinel_mod.probe_p19(tmp_path, tmp_path)
    assert len(notes) == 1
    assert "v1.9.0→v2.0.0 behind latest release" in notes[0].message


def test_probe_p19_maps_differs(tmp_path, monkeypatch):
    sentinel_mod = _sentinel_module()
    monkeypatch.setattr(
        "coordinator_core.plugin_health.release_currency.release_currency_probe",
        lambda plugin, repo, root: "differs v2.0.0",
    )
    notes = sentinel_mod.probe_p19(tmp_path, tmp_path)
    assert len(notes) == 1
    assert "differs from latest release (v2.0.0)" in notes[0].message


def test_probe_p19_unexpected_exception_maps_to_absent(tmp_path, monkeypatch):
    sentinel_mod = _sentinel_module()

    def _boom(plugin, repo, root):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "coordinator_core.plugin_health.release_currency.release_currency_probe", _boom
    )
    notes = sentinel_mod.probe_p19(tmp_path, tmp_path)
    assert len(notes) == 1
    assert "skipped" in notes[0].message


def test_probe_p19_import_error_maps_to_absent(tmp_path, monkeypatch):
    import sys

    sentinel_mod = _sentinel_module()
    monkeypatch.setitem(sys.modules, "coordinator_core.plugin_health.release_currency", None)
    notes = sentinel_mod.probe_p19(tmp_path, tmp_path)
    assert len(notes) == 1
    assert "skipped" in notes[0].message


def test_source_is_live_live_path_only_in_tracked_registry(tmp_path, monkeypatch):
    """Per-key fallthrough: live_path declared only in the tracked
    registry.toml is honored even when a registry.local.toml exists without
    the key (previously .local-only -- the tracked declaration was
    invisible)."""
    monkeypatch.delenv("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", raising=False)
    install_root = tmp_path / "install"
    install_root.mkdir()
    ml = tmp_path / "machine-local"
    ml.mkdir(parents=True, exist_ok=True)
    (ml / "registry.local.toml").write_text("schema = 1\n", encoding="utf-8")
    (ml / "registry.toml").write_text(
        '[plugin.mirrors.coordinator-claude]\n'
        f'live_path = "{install_root}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rc, "machine_local_dir", lambda: ml)
    _no_subprocess_allowed(monkeypatch)
    result = rc.release_currency_probe("coordinator", "o/r", install_root)
    assert result == "source_is_live"
