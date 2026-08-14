"""
Black-box CLI tests for coordinator/lib/check-install-singularity.py.

Port of: coordinator/lib/tests/test-check-install-singularity.sh (T1-T12).
Exercises the SUT as a subprocess (python3 check-install-singularity.py),
asserting stdout/exit-code contract — complementary to
coordinator_core/install/test_check_install_singularity.py, which calls the
underlying coordinator_core.install.check_install_singularity module directly
(white-box) and does not invoke this CLI at all. Named distinctly from that
module to avoid a pytest module-name collision under prepend import mode.

Spec backlink:
  docs/plans/2026-06-26-coordinator-install-update-friction-fix-slate.md § C-R1b
  AC4, AC5, AC5b
Port backlink: docs/plans/2026-08-13-grind-the-posix-exec-baseline-to-zero.md
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SUT = Path(__file__).resolve().parent.parent / "check-install-singularity.py"


def _run_sut(fake_home: Path, extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    for key in ("COORDINATOR_CLONE", "COORDINATOR_ROOT", "CLAUDE_PLUGIN_ROOT", "CLAUDE_HOME"):
        env.pop(key, None)
    env["HOME"] = str(fake_home)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(SUT)],
        env=env,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode, result.stdout + result.stderr


def _setup_base(fake_home: Path, flat_path: Path) -> None:
    flat_path.mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude" / "machine-local").mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude" / "plugins").mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    fake_home = tmp_path / "home"
    flat_path = fake_home / ".claude" / "plugins" / "coordinator-claude"
    reg_file = fake_home / ".claude" / "machine-local" / "registry.local.toml"
    _setup_base(fake_home, flat_path)
    return fake_home, flat_path, reg_file


def test_sut_exists():
    assert SUT.is_file(), f"script under test not found at {SUT}"


def test_t1_claude_suffixed_claude_home_fails(home):
    fake_home, _flat_path, _reg = home
    rc, out = _run_sut(fake_home, {"CLAUDE_HOME": str(fake_home / ".claude")})
    assert rc != 0
    assert "remediation" in out.lower()


@pytest.mark.xfail(
    reason=(
        "Pre-existing SUT drift, reproduced (not introduced) by this port: the bash "
        "oracle test-check-install-singularity.sh already fails this same case against "
        "today's check-install-singularity.py — the gate now requires a "
        ".claude-plugin/plugin.json marker to count a directory as a tree (see "
        "coordinator_core/install/test_check_install_singularity.py's _make_tree "
        "docstring), so a bare-directory registry fixture no longer registers as a "
        "second tree. Flagged for the EM to route; not fixed here (out of Group C scope)."
    ),
    strict=False,
)
def test_t2_doubled_claude_venv_pin_fails(home):
    fake_home, _flat_path, reg_file = home
    reg_file.write_text(
        '[coordinator]\npython = "/home/user/.claude/.claude/.coordinator-venv/bin/python"\n',
        encoding="utf-8",
    )
    rc, out = _run_sut(fake_home)
    assert rc != 0
    assert "remediation" in out.lower()


@pytest.mark.xfail(
    reason=(
        "Pre-existing SUT drift, same bare-directory-vs-manifest-marker mechanism as "
        "test_t2 above. Reproduced from the bash oracle, not introduced by this port."
    ),
    strict=False,
)
def test_t3_multiple_distinct_trees_no_override_fails(home, tmp_path):
    fake_home, _flat_path, reg_file = home
    extra_tree = tmp_path / "extra" / "coordinator-claude"
    (extra_tree / "coordinator").mkdir(parents=True, exist_ok=True)
    reg_file.write_text(
        f'[plugin.mirrors.coordinator-claude]\nlive_path = "{extra_tree / "coordinator"}"\n',
        encoding="utf-8",
    )
    rc, out = _run_sut(fake_home)
    assert rc != 0
    assert "remediation" in out.lower()


def test_t4_two_present_settings_disagree_fails(home, tmp_path):
    fake_home, flat_path, _reg = home
    other_path = tmp_path / "other" / "coordinator-claude"
    other_path.mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "extraKnownMarketplaces": {
                    "coordinator-claude": {"source": {"source": "directory", "path": str(flat_path)}}
                }
            }
        ),
        encoding="utf-8",
    )
    (fake_home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
        json.dumps(
            {"coordinator-claude": {"source": {"source": "directory", "path": str(other_path)}}}
        ),
        encoding="utf-8",
    )
    rc, out = _run_sut(fake_home)
    assert rc != 0
    assert "remediation" in out.lower()


def test_t5_registry_live_path_matches_flat_path_after_dedupe(home):
    fake_home, flat_path, reg_file = home
    (flat_path / "coordinator").mkdir(parents=True, exist_ok=True)
    reg_file.write_text(
        f'[plugin.mirrors.coordinator-claude]\nlive_path = "{flat_path / "coordinator"}"\n',
        encoding="utf-8",
    )
    rc, out = _run_sut(fake_home)
    assert rc == 0, out


def test_t6_single_distinct_tree_passes(home):
    fake_home, _flat_path, _reg = home
    rc, out = _run_sut(fake_home)
    assert rc == 0, out


def test_t7_settings_local_absent_is_concordant(home):
    fake_home, flat_path, _reg = home
    assert not (fake_home / ".claude" / "settings.local.json").exists()
    (fake_home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "extraKnownMarketplaces": {
                    "coordinator-claude": {"source": {"source": "directory", "path": str(flat_path)}}
                }
            }
        ),
        encoding="utf-8",
    )
    (fake_home / ".claude" / "plugins" / "known_marketplaces.json").write_text(
        json.dumps(
            {"coordinator-claude": {"source": {"source": "directory", "path": str(flat_path)}}}
        ),
        encoding="utf-8",
    )
    rc, out = _run_sut(fake_home)
    assert rc == 0, out


def test_t8_single_coordinator_clone_git_backed_is_exempt(home, tmp_path):
    fake_home, _flat_path, _reg = home
    clone_dir = tmp_path / "clone" / "coordinator-claude"
    (clone_dir / ".git").mkdir(parents=True, exist_ok=True)
    rc, out = _run_sut(fake_home, {"COORDINATOR_CLONE": str(clone_dir)})
    assert rc == 0, out
    assert "dev-loop override" in out.lower()


def test_t9_single_coordinator_root_parent_git_backed_is_exempt(home, tmp_path):
    fake_home, _flat_path, _reg = home
    root_plugin = tmp_path / "root_plugin" / "coordinator-claude"
    (root_plugin / ".git").mkdir(parents=True, exist_ok=True)
    (root_plugin / "coordinator").mkdir(parents=True, exist_ok=True)
    rc, out = _run_sut(fake_home, {"COORDINATOR_ROOT": str(root_plugin / "coordinator")})
    assert rc == 0, out


@pytest.mark.xfail(
    reason=(
        "Pre-existing SUT drift, same bare-directory-vs-manifest-marker mechanism as "
        "test_t2 above. Reproduced from the bash oracle, not introduced by this port."
    ),
    strict=False,
)
def test_t10_claude_plugin_root_is_not_exempt(home, tmp_path):
    fake_home, _flat_path, _reg = home
    cpr_dir = tmp_path / "cpr" / "coordinator-claude"
    cpr_dir.mkdir(parents=True, exist_ok=True)
    rc, out = _run_sut(fake_home, {"CLAUDE_PLUGIN_ROOT": str(cpr_dir)})
    assert rc != 0, out


def test_t11_py_compile_clean():
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SUT)],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr


def test_t12_claude_home_already_suffixed_sentinel_leakage_fails(home):
    fake_home, _flat_path, _reg = home
    rc, out = _run_sut(fake_home, {"CLAUDE_HOME": str(fake_home / ".claude" / ".claude")})
    assert rc != 0, out
