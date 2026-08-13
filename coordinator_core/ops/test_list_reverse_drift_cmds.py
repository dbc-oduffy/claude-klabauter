"""
test_list_reverse_drift_cmds.py — pytest unit tests for
coordinator_core.ops.list_reverse_drift_cmds.

Reauthored here as direct in-process calls against the ported Python module's
main()/_run(), same fixtures/assertions/expected exit codes as the bash
oracle's 13 subprocess tests. Parity verified against the bash oracle on
matching fixtures during this port's parity gate.

Port of: test-list-reverse-drift-scoping.sh (coordinator-claude 290997c7, 2026-07-22)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Spec backlink: cross-repo/inbox/2026-06-01-reverse-drift-gate-per-repo-scoping.md

Isolation: every test writes a fresh registry.local.toml under tmp_path and
sets MACHINE_LOCAL_REGISTRY_DIR / HOME env overrides — never touches the
operator's real ~/.claude/machine-local.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from coordinator_core.ops.list_reverse_drift_cmds import _norm_path, main


def _run_lister(
    monkeypatch: pytest.MonkeyPatch,
    reg_dir: Path,
    home,
    argv: List[str],
    ostype: Optional[str] = None,
) -> Tuple[str, str, int]:
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setenv("HOME", str(home))
    if ostype is not None:
        monkeypatch.setenv("OSTYPE", ostype)
    else:
        monkeypatch.delenv("OSTYPE", raising=False)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return out.getvalue().rstrip("\n"), err.getvalue(), rc


def _example_game_repo_registry(reg_dir: Path, *, backslash: bool = False) -> None:
    reg_dir.mkdir(parents=True, exist_ok=True)
    source_path = r"X:\\example-game-workbench-repo" if backslash else "X:/example-game-workbench-repo"
    (reg_dir / "registry.local.toml").write_text(
        "[plugin.mirrors.example-game-repo]\n"
        'propagation_mode = "copy_install"\n'
        f'source_path = "{source_path}"\n'
        'reverse_drift_cmd = "bash bin/check-reverse-drift.sh"\n',
        encoding="utf-8",
    )


def _example_game_repo_registry_cmdless(reg_dir: Path) -> None:
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "registry.local.toml").write_text(
        "[plugin.mirrors.example-game-repo]\n"
        'propagation_mode = "copy_install"\n'
        'source_path = "X:/example-game-workbench-repo"\n',
        encoding="utf-8",
    )


# 1. No --scope-repo -> legacy behavior: emit ALL copy_install rows.
def test_no_scope_emits_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, [])
    assert rc == 0
    assert "example-game-repo|X:/example-game-workbench-repo|" in out


# 2. Consumer repo (not the source of any copy_install plugin) -> clean no-op.
def test_consumer_repo_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo", "X:/example-retrieval-repo"])
    assert rc == 0
    assert out == ""


# 3. The plugin's own source repo releasing itself -> matches its own row.
def test_own_source_repo_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo", "X:/example-game-workbench-repo"])
    assert rc == 0
    assert "example-game-repo|X:/example-game-workbench-repo|" in out


# 4. Meta-repo (.claude under the home directory) gets check-all even though it
#    sources none of them.
def test_meta_repo_checks_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo", str(tmp_path / ".claude")])
    assert rc == 0
    assert "example-game-repo|X:/example-game-workbench-repo|" in out


# 5. Path normalization — meta-repo via Windows drive form vs $HOME /c/ form.
#    _norm_path folds MSYS /c/ -> C:/ ONLY under msys/cygwin OSTYPE.
def test_meta_repo_windows_drive_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    out, _err, rc = _run_lister(
        monkeypatch, reg_dir, "/c/Users/operator", ["--scope-repo", "C:/Users/operator/.claude"], ostype="msys"
    )
    assert rc == 0
    assert "example-game-repo|X:/example-game-workbench-repo|" in out


# 6. Path normalization — own source repo via MSYS /x/ form matches registry X:/ form.
#    Cross-platform: _norm_path converts the registry's X:/ form to /x/ form on
#    ALL platforms, so this passes regardless of OSTYPE.
def test_own_repo_msys_form_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo", "/x/example-game-workbench-repo"])
    assert rc == 0
    assert "example-game-repo|X:/example-game-workbench-repo|" in out


# 7. Scope-aware misconfig: a consumer that scopes OUT a cmd-less plugin -> rc=0, NOT rc=3.
def test_consumer_scopes_out_cmdless_plugin_no_misconfig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry_cmdless(reg_dir)
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo", "X:/example-retrieval-repo"])
    assert rc == 0


# 8. Scope-aware misconfig: meta-repo seeing a cmd-less copy_install plugin still fails loud.
def test_meta_repo_cmdless_plugin_misconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry_cmdless(reg_dir)
    out, err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo", str(tmp_path / ".claude")])
    assert rc == 3
    assert "reverse-drift gate cannot run" in err


# 9. --scope-repo with no path argument -> rc=2 (invocation error).
def test_scope_repo_missing_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    _out, err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo"])
    assert rc == 2
    assert "requires a path argument" in err


# 10. Unknown argument -> rc=2.
def test_unknown_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    _out, err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--bogus"])
    assert rc == 2
    assert "unknown argument" in err


# 11. Production meta-repo topology — HOME on /c/, scope-repo as C:/ drive form,
#     registry source_path on a DIFFERENT drive (X:). Meta-repo check-all must
#     still emit the cross-drive row.
def test_production_metarepo_cross_drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    out, _err, rc = _run_lister(
        monkeypatch, reg_dir, "/c/Users/alice", ["--scope-repo", "C:/Users/alice/.claude"], ostype="msys"
    )
    assert rc == 0
    assert "example-game-repo|X:/example-game-workbench-repo|" in out


# 12. Backslash-form registry source_path (X:\...) matched by a forward-slash scope.
def test_backslash_source_path_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir, backslash=True)
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo", "X:/example-game-workbench-repo"])
    assert rc == 0
    # Output preserves the registry's stored (backslash) source_path verbatim;
    # the match is on the normalized form, not the emitted text.
    assert "example-game-repo|" in out


# 13. Trailing-backslash scope (X:\...\) must still match (F4 path).
def test_trailing_backslash_scope_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    _example_game_repo_registry(reg_dir)
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, ["--scope-repo", "X:\\example-game-workbench-repo\\"])
    assert rc == 0
    assert "example-game-repo|X:/example-game-workbench-repo|" in out


# ---------------------------------------------------------------------------
# Additional coverage beyond the bash oracle's 13 cases.
# ---------------------------------------------------------------------------


# 14. No registry file at all -> N/A, rc=0, no output.
def test_no_registry_file_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "empty-regdir"
    reg_dir.mkdir()
    out, err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, [])
    assert rc == 0
    assert out == ""
    assert err == ""


# 15. Non-copy_install plugin (editable_sibling_venv) is excluded regardless of scope.
def test_non_copy_install_plugin_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    reg_dir.mkdir()
    (reg_dir / "registry.local.toml").write_text(
        "[plugin.mirrors.example-retrieval-repo]\n"
        'propagation_mode = "editable_sibling_venv"\n'
        'source_path = "/src/example-retrieval-repo"\n'
        'reverse_drift_cmd = "bash bin/check-reverse-drift.sh"\n',
        encoding="utf-8",
    )
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, [])
    assert rc == 0
    assert out == ""


# 16. Double-quote in reverse_drift_cmd -> advisory WARNING on stderr, row still emitted.
def test_double_quote_in_cmd_warns_but_emits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    reg_dir.mkdir()
    (reg_dir / "registry.local.toml").write_text(
        "[plugin.mirrors.example-game-repo]\n"
        'propagation_mode = "copy_install"\n'
        'source_path = "X:/example-game-workbench-repo"\n'
        "reverse_drift_cmd = 'bash -c \"echo hi\"'\n",
        encoding="utf-8",
    )
    out, err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, [])
    assert rc == 0
    assert "example-game-repo|" in out
    assert "single-quote the value" in err


# ---------------------------------------------------------------------------
# _norm_path unit coverage (the fiddly Windows/MSYS/POSIX fold, ported
# verbatim from the bash oracle's F1-F4 review-fix comments).
# ---------------------------------------------------------------------------


def test_norm_path_windows_drive_lowercases() -> None:
    assert _norm_path("X:/Claude-Unreal-Example-Game-Repo") == "/x/example-game-workbench-repo"


def test_norm_path_windows_backslash_to_forward() -> None:
    assert _norm_path(r"X:\example-game-workbench-repo") == "/x/example-game-workbench-repo"


def test_norm_path_drive_relative() -> None:
    assert _norm_path("X:foo") == "/x/foo"


def test_norm_path_msys_form_off_windows_not_folded() -> None:
    # POSIX single-letter top-level dir must NOT fold when ostype is unset
    # (i.e. this session's actual macOS/Linux host).
    assert _norm_path("/a/Foo") == "/a/Foo"


def test_norm_path_msys_form_folds_under_msys_ostype() -> None:
    assert _norm_path("/x/Claude-Unreal-Example-Game-Repo", ostype="msys") == "/x/example-game-workbench-repo"


def test_norm_path_trailing_separator_stripped() -> None:
    assert _norm_path("X:/example-game-workbench-repo/") == "/x/example-game-workbench-repo"
    assert _norm_path("X:\\example-game-workbench-repo\\") == "/x/example-game-workbench-repo"


def test_norm_path_posix_case_preserved() -> None:
    assert _norm_path("/Users/alice/X/example-retrieval-repo") == "/Users/alice/X/example-retrieval-repo"


# 14. Per-key fallthrough: a copy_install row registered only in the tracked
#     registry.toml is emitted even when a registry.local.toml exists
#     (previously first-FILE-wins made it invisible).
def test_row_only_in_tracked_registry_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "regdir"
    reg_dir.mkdir(parents=True)
    (reg_dir / "registry.local.toml").write_text("schema = 1\n", encoding="utf-8")
    (reg_dir / "registry.toml").write_text(
        "[plugin.mirrors.example-game-repo]\n"
        'propagation_mode = "copy_install"\n'
        'source_path = "X:/example-game-workbench-repo"\n'
        'reverse_drift_cmd = "bash bin/check-reverse-drift.sh"\n',
        encoding="utf-8",
    )
    out, _err, rc = _run_lister(monkeypatch, reg_dir, tmp_path, [])
    assert rc == 0
    assert "example-game-repo|X:/example-game-workbench-repo|" in out
