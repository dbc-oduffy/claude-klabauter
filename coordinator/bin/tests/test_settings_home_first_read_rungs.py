"""test_settings_home_first_read_rungs.py — regression coverage for the two
unconditional `~/.claude/bin` mirror read rungs the audit named as violating
DR-210 Amendment ("resolves nothing through ~/.claude/bin"):
  - coordinator/bin/lib/coordinator_registry.py's split-repo manifest
    fallback (subprocess call onto `machine_local_bin_candidates()`)
  - coordinator/bin/resolve-repo-path.py's `_machine_local_path_candidates()`

Both sites now delegate ordering to
`coordinator/bin/lib/machine_local_impl_resolve.py` (already covered for its
own internals by test_machine_local_impl_resolve.py); these tests instead
pin that each CALL SITE actually surfaces settings-home-first behavior, not
just the shared helper in isolation.

Spec backlink: state/audits/2026-07-25-claude-bin-mirror-read-rungs.md § 2/3.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

# ---------------------------------------------------------------------------
# resolve-repo-path.py — _machine_local_path_candidates() ordering.
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(_BIN_DIR))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import importlib.util  # noqa: E402

_RRP_PATH = os.path.join(_BIN_DIR, "resolve-repo-path.py")
_rrp_spec = importlib.util.spec_from_file_location("resolve_repo_path_module", _RRP_PATH)
_rrp = importlib.util.module_from_spec(_rrp_spec)
_rrp_spec.loader.exec_module(_rrp)  # type: ignore[union-attr]


@pytest.fixture(scope="module", autouse=True)
def _restore_sys_path():
    # Review: coordinator:code-reviewer-05a3e212 — module-level sys.path
    # mutation above (needed before the exec_module import-time load) would
    # otherwise persist for the rest of the pytest session and could shadow
    # same-named modules in files collected afterward. Undo it once every
    # test in this module has run.
    yield
    if _LIB_DIR in sys.path:
        sys.path.remove(_LIB_DIR)
    if _REPO_ROOT in sys.path:
        sys.path.remove(_REPO_ROOT)


def test_resolve_repo_path_candidates_settings_home_before_mirror(monkeypatch, tmp_path):
    settings_home = tmp_path / "settings-home"
    claude_home = tmp_path / "claude-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    # Review: coordinator:code-reviewer-05a3e212 — machine_local_impl_resolve
    # .claude_home() consults CLAUDE_CONFIG_DIR before CLAUDE_HOME; leaving it
    # unpinned would let an ambient dev-box/CI value silently override the
    # fixture and resolve against a real path instead of tmp_path.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    candidates = _rrp._machine_local_path_candidates()

    settings_idx = next(
        i for i, c in enumerate(candidates) if str(settings_home) in c
    )
    mirror_idx = next(i for i, c in enumerate(candidates) if str(claude_home) in c)
    assert settings_idx < mirror_idx, (
        f"expected settings-home candidate before mirror candidate; got {candidates!r}"
    )


def test_resolve_repo_path_mirror_still_reachable_when_settings_home_absent(monkeypatch, tmp_path):
    """The mirror candidate must still be offered (never removed) even though
    it now ranks last — DR-210 Amendment retires primacy, not existence."""
    settings_home = tmp_path / "settings-home"
    claude_home = tmp_path / "claude-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    candidates = _rrp._machine_local_path_candidates()
    assert any(str(claude_home) in c for c in candidates), (
        f"expected a mirror candidate to still be present; got {candidates!r}"
    )


def test_resolve_repo_path_fails_open_with_breadcrumb_when_no_candidate_exists(monkeypatch, tmp_path, capsys):
    """Neither settings-home nor mirror has a machine-local binary on disk:
    _resolve_registry_value degrades to empty stdout (fail-open, not
    fail-loud) with a stderr breadcrumb — the pre-existing contract this
    ordering fix must not change."""
    settings_home = tmp_path / "settings-home"
    claude_home = tmp_path / "claude-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    result = _rrp._resolve_registry_value("repos.doe_claude")

    assert result == ""
    captured = capsys.readouterr()
    assert "machine-local CLI not found" in captured.err


# ---------------------------------------------------------------------------
# coordinator_registry.py — split-repo manifest fallback: settings-home
# candidate must be tried before the mirror candidate. Exercised via a real
# subprocess import (import-time behavior) with fake machine-local
# executables planted at each candidate location so which one "wins" is
# externally observable from the resulting _MANIFEST_PATH.
# ---------------------------------------------------------------------------

_FAKE_ML_SCRIPT = """#!/usr/bin/env python3
import sys
print({doe_root!r})
sys.exit(0)
"""


def _plant_fake_machine_local(base_dir: str, doe_root: str) -> None:
    bin_dir = os.path.join(base_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    script_path = os.path.join(bin_dir, "machine-local")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(_FAKE_ML_SCRIPT.format(doe_root=doe_root))
    os.chmod(script_path, 0o755)
    if os.name == "nt":
        # coordinator_registry.py's split-repo fallback subprocess.run's the
        # candidate path directly as argv[0] (`[_ml_cand, "get", key]`) — on
        # Windows that requires a real executable, so also plant a .cmd twin
        # that shells back into this same interpreter running the sibling
        # .py file (CreateProcess does not consult PATHEXT for a bare path).
        py_twin = script_path + ".py"
        with open(py_twin, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_ML_SCRIPT.format(doe_root=doe_root))
        cmd_path = script_path + ".cmd"
        with open(cmd_path, "w", encoding="utf-8") as fh:
            fh.write(f'@"{sys.executable}" "{py_twin}" %*\n')


def _build_doe_fixture(root: str, tag: str) -> str:
    """A fake DoE root carrying a real manifest at
    <root>/coordinator/schemas/coordinator-registry.manifest.json."""
    doe_root = os.path.join(root, f"doe-{tag}")
    manifest_dir = os.path.join(doe_root, "coordinator", "schemas")
    os.makedirs(manifest_dir)
    with open(
        os.path.join(manifest_dir, "coordinator-registry.manifest.json"), "w", encoding="utf-8"
    ) as fh:
        fh.write(
            '{"docTypes": [], "queueTypes": [], '
            '"identity": {"repoAliases": [], "centralReceiverIds": ["doe-claude-em"]}}'
        )
    return doe_root


def _run_registry_import_subprocess(env: dict) -> "subprocess.CompletedProcess[str]":
    snippet = (
        "import sys; "
        f"sys.path.insert(0, {_LIB_DIR!r}); "
        "import coordinator_registry; "
        "print(coordinator_registry._MANIFEST_PATH)"
    )
    return subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _base_env(empty_home: str) -> dict:
    env = {
        "HOME": empty_home,
        "USERPROFILE": empty_home,
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    return env


def test_coordinator_registry_split_repo_fallback_settings_home_wins_over_mirror():
    with tempfile.TemporaryDirectory() as tmp:
        empty_home = os.path.join(tmp, "empty-home")
        os.makedirs(empty_home)
        settings_home = os.path.join(tmp, "settings-home")
        claude_home = os.path.join(tmp, "claude-home")

        settings_doe = _build_doe_fixture(tmp, "settings")
        mirror_doe = _build_doe_fixture(tmp, "mirror")
        _plant_fake_machine_local(settings_home, settings_doe)
        _plant_fake_machine_local(claude_home, mirror_doe)

        env = _base_env(empty_home)
        env["COORDINATOR_SETTINGS_HOME"] = settings_home
        env["CLAUDE_HOME"] = claude_home
        # Review: coordinator:code-reviewer-05a3e212 — no .pop() here: env is
        # a from-scratch dict built by _base_env(), which never populates
        # DOE_ROOT/REPO_DOE_CLAUDE in the first place (unlike os.environ.copy()).

        result = _run_registry_import_subprocess(env)
        assert result.returncode == 0, f"stderr:\n{result.stderr}"
        manifest_path = result.stdout.strip()
        assert manifest_path.startswith(settings_doe), (
            f"expected the settings-home fake machine-local's DoE root to win; "
            f"got _MANIFEST_PATH={manifest_path!r}, stderr:\n{result.stderr}"
        )


def test_coordinator_registry_split_repo_fallback_mirror_reachable_when_settings_home_absent():
    with tempfile.TemporaryDirectory() as tmp:
        empty_home = os.path.join(tmp, "empty-home")
        os.makedirs(empty_home)
        settings_home = os.path.join(tmp, "settings-home")  # never planted with a binary
        claude_home = os.path.join(tmp, "claude-home")

        mirror_doe = _build_doe_fixture(tmp, "mirror-only")
        _plant_fake_machine_local(claude_home, mirror_doe)

        env = _base_env(empty_home)
        env["COORDINATOR_SETTINGS_HOME"] = settings_home
        env["CLAUDE_HOME"] = claude_home
        # Review: coordinator:code-reviewer-05a3e212 — no .pop() here; see
        # the sibling test above for rationale.

        result = _run_registry_import_subprocess(env)
        assert result.returncode == 0, f"stderr:\n{result.stderr}"
        manifest_path = result.stdout.strip()
        assert manifest_path.startswith(mirror_doe), (
            f"expected the mirror rung to still be reachable when settings-home "
            f"has no binary; got _MANIFEST_PATH={manifest_path!r}, stderr:\n{result.stderr}"
        )
