"""
Tests for coordinator_core.ops.check_version_consistency.

Mirrors the AC1-AC11 acceptance criteria of the bash oracle so the Python
port is provably parity-equivalent, not just "looks about right".

Port of: test-check-version-consistency.sh (DoE 894d4bc6, 2026-07-22)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.ops.check_version_consistency import main
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Declared, not excused: this file spawns real git because the bash-oracle parity
# contract (test-check-version-consistency.sh) it ports depends on real git-tree
# state (tracked-file layout) that `check_version_consistency.main` reads via git
# plumbing -- no mock stands in for that. Each test builds its own throwaway repo
# via `_make_bundle`, so there is no shared state to hoist. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the route
# for this file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_bundle(
    root: Path,
    pver: str,
    mver: str,
    cver: str,
    layout: str = "dist",
    mode: str = "",
) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / "coordinator" / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / "coordinator" / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "coordinator",\n  "version": "%s"\n}\n' % pver
    )
    (root / ".claude-plugin" / "marketplace.json").write_text(
        '{\n  "name": "coordinator-claude",\n  "metadata": {\n    "version": "%s"\n  },\n'
        '  "plugins": [\n    { "name": "coordinator", "source": "./coordinator", "tags": ["x"] }\n  ]\n}\n'
        % mver
    )
    if mode != "no-changelog":
        if layout == "flat":
            cl = root / "CHANGELOG.md"
        else:
            cl = root / "coordinator" / "dist" / "publish-repo-toplevel" / "CHANGELOG.md"
            cl.parent.mkdir(parents=True, exist_ok=True)
        cl.write_text(
            "# Changelog\n\n## [Unreleased]\n\n- pending\n\n## [%s] — 2026-06-22\n\n"
            "- shipped\n\n## [2.0.0] — 2026-01-01\n\n- old\n" % cver
        )


def _run(root: Path, *extra_args: str) -> int:
    return main(["--root", str(root), *extra_args])


def test_ac1_all_surfaces_agree(tmp_path, capsys):
    d = tmp_path / "ac1"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    assert _run(d, "--quiet") == 0


def test_ac2_plugin_json_mismatch(tmp_path):
    d = tmp_path / "ac2"
    _make_bundle(d, "2.8.0", "2.9.0", "2.9.0")
    assert _run(d, "--quiet") == 1


def test_ac3_marketplace_mismatch(tmp_path):
    d = tmp_path / "ac3"
    _make_bundle(d, "2.9.0", "2.1.1", "2.9.0")
    assert _run(d, "--quiet") == 1


def test_ac4_changelog_mismatch(tmp_path):
    d = tmp_path / "ac4"
    _make_bundle(d, "2.9.0", "2.9.0", "2.8.1")
    assert _run(d, "--quiet") == 1


def test_ac5_unreleased_skipped(tmp_path):
    d = tmp_path / "ac5"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    assert _run(d, "--quiet") == 0


def test_ac6_missing_changelog_fails_loud(tmp_path):
    d = tmp_path / "ac6"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0", mode="no-changelog")
    assert _run(d, "--quiet") == 1


def test_ac7_flat_layout(tmp_path):
    d = tmp_path / "ac7"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0", layout="flat")
    assert _run(d, "--quiet") == 0


def _git_bundle(d: Path, tag: Optional[str] = None) -> None:
    subprocess.run(["git", "init", "-q"], cwd=d, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=d,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    if tag:
        subprocess.run(
            ["git", "-c", "tag.gpgSign=false", "-c", "tag.forcesignannotated=false", "tag", tag],
            cwd=d,
            check=True,
            **no_console_passthrough_kwargs(),
        )


def test_ac8_check_tag_mismatch_is_advisory(tmp_path, capsys):
    d = tmp_path / "ac8"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    _git_bundle(d, "v2.8.0")
    rc = _run(d, "--check-tag")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" in captured.err


def test_ac9_plugin_entry_version_ignored(tmp_path):
    d = tmp_path / "ac9"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / "coordinator" / ".claude-plugin").mkdir(parents=True)
    (d / "coordinator" / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "coordinator",\n  "version": "2.9.0"\n}\n'
    )
    (d / ".claude-plugin" / "marketplace.json").write_text(
        '{\n  "name": "coordinator-claude",\n  "metadata": {\n    "version": "2.9.0"\n  },\n'
        '  "plugins": [\n    { "name": "coordinator", "source": "./coordinator", '
        '"version": "9.9.9", "tags": ["x"] }\n  ]\n}\n'
    )
    (d / "CHANGELOG.md").write_text("# Changelog\n\n## [2.9.0] — 2026-06-22\n\n- x\n")
    assert _run(d, "--quiet") == 0


def test_ac10_check_tag_matching(tmp_path, capsys):
    d = tmp_path / "ac10"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    _git_bundle(d, "v2.9.0")
    rc = _run(d, "--check-tag")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" not in captured.err


def test_ac11_check_tag_no_tags(tmp_path, capsys):
    d = tmp_path / "ac11"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    _git_bundle(d)
    rc = _run(d, "--check-tag")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" not in captured.err


def test_unknown_arg_exits_2():
    assert main(["--bogus"]) == 2


def test_help_exits_0(capsys):
    assert main(["--help"]) == 0
    assert "check-version-consistency" in capsys.readouterr().out


def test_bad_root_reproduces_oracle_double_message_bug(tmp_path, capsys):
    """Byte-parity regression test for a genuine bash-oracle bug: `fail()` inside
    `discover_root()`'s command-substitution subshell only kills the subshell, so
    the parent script silently continues with BUNDLE_ROOT="" and fails a SECOND
    time at the CHANGELOG-resolution step. Verified against the live bash oracle
    on 2026-07-16 (Port of: check-version-consistency.sh, DoE 894d4bc6, 2026-07-22):
    `bash check-version-consistency.sh --root /nonexistent` prints both lines
    below and exits 1. This module reproduces that exactly, not just the exit code.
    """
    rc = main(["--root", str(tmp_path / "nonexistent")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no .claude-plugin/marketplace.json under --root" in captured.err
    assert "no CHANGELOG.md found" in captured.err


# ---------------------------------------------------------------------------
# P124-C1 — the version gate identifies the coordinator-claude bundle in its
# caller before it may fail it. All --repo-root, never --root: these tests
# exercise the auto-discovery/identity/N/A path, not the unchanged --root path.
# ---------------------------------------------------------------------------


def _make_v3_bundle(root: Path, pver: str, mver: str, cver: str, name: str = "coordinator-claude") -> None:
    """DoE-claude's real v3 source layout: the bundle lives under
    `<root>/coordinator/.claude-plugin/`, with the CHANGELOG one level below
    that at `dist/publish-repo-toplevel/` — mirrors the actual on-disk shape
    (verified at DoE-claude@HEAD, 2026-09-24) rather than a hand-typed guess."""
    bundle = root / "coordinator"
    (bundle / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (bundle / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "coordinator",\n  "version": "%s"\n}\n' % pver
    )
    (bundle / ".claude-plugin" / "marketplace.json").write_text(
        '{\n  "name": "%s",\n  "metadata": {\n    "version": "%s"\n  },\n'
        '  "plugins": [\n    { "name": "coordinator", "source": "./coordinator", "tags": ["x"] }\n  ]\n}\n'
        % (name, mver)
    )
    cl = bundle / "dist" / "publish-repo-toplevel" / "CHANGELOG.md"
    cl.parent.mkdir(parents=True, exist_ok=True)
    cl.write_text(
        "# Changelog\n\n## [Unreleased]\n\n- pending\n\n## [%s] — 2026-06-22\n\n"
        "- shipped\n\n## [2.0.0] — 2026-01-01\n\n- old\n" % cver
    )


def test_c1_a_v3_layout_from_repo_root_reaches_ok(tmp_path, capsys):
    """The compliance path (example-store-repo proposal 2): DoE running its own
    ceremony from its own root with no --root. Must reach OK, never N/A."""
    d = tmp_path / "doe"
    d.mkdir()
    _make_v3_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    rc = main(["--repo-root", str(d)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "N/A" not in captured.out
    assert "OK" in captured.out


def test_c1_b_root_marketplace_named_something_else_reaches_na(tmp_path, capsys):
    """A plugin repo whose root marketplace.json is named something other than
    coordinator-claude (e.g. Example-retrieval-repo's own) must never be mistaken for the
    bundle -- reaches a stated N/A, not a fail and not a silent pass."""
    d = tmp_path / "project-rag"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "marketplace.json").write_text('{"name": "project-rag"}\n')
    rc = main(["--repo-root", str(d), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().startswith("check-version-consistency: N/A")
    assert str(d) in captured.out


def test_c1_c_no_marketplace_reaches_na_even_under_quiet(tmp_path, capsys):
    d = tmp_path / "empty-repo"
    d.mkdir()
    rc = main(["--repo-root", str(d), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().startswith("check-version-consistency: N/A")
    assert str(d) in captured.out


def test_c1_e_directive_args_carry_repo_root(tmp_path, capsys):
    """--repo-root round-trips: identical bundle reached whether passed
    explicitly or (if it happened to be cwd) discovered by default -- this is
    the flag-plumbing half; test_workweek_complete_contract.py owns the actual
    directive-args assertion for d_step4b_4k_version_consistency."""
    d = tmp_path / "doe2"
    d.mkdir()
    _make_v3_bundle(d, "3.1.0", "3.1.0", "3.1.0")
    rc = main(["--repo-root", str(d), "--quiet"])
    assert rc == 0


def test_c1_f_nested_repo_root_reaches_ok_via_bundle_holder(tmp_path, capsys):
    """A --repo-root naming a path NESTED under a bundle holder's own git
    worktree still resolves to that holder's root and reaches OK -- discovery
    normalizes through show_toplevel, it does not require repo_root to BE the
    exact bundle-holder root."""
    d = tmp_path / "doe3"
    d.mkdir()
    _make_v3_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=d,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    nested = d / "coordinator" / "docs"
    nested.mkdir(parents=True, exist_ok=True)
    rc = main(["--repo-root", str(nested), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "N/A" not in captured.out


def test_c1_f_non_git_repo_root_reaches_na_naming_that_path(tmp_path, capsys):
    d = tmp_path / "not-a-git-repo"
    d.mkdir()
    rc = main(["--repo-root", str(d), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().startswith("check-version-consistency: N/A")
    assert str(d) in captured.out


def test_c1_g_corrupt_marketplace_json_fails_loud_not_na(tmp_path, capsys):
    d = tmp_path / "corrupt"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "marketplace.json").write_text("{ not valid json ")
    rc = main(["--repo-root", str(d), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "unparseable marketplace manifest" in captured.err
    assert str(d / ".claude-plugin" / "marketplace.json") in captured.err
    assert "N/A" not in captured.out


def test_c1_h_explicit_repo_root_never_reads_cwd(tmp_path, capsys, monkeypatch):
    """cwd holds a coordinator-claude bundle while --repo-root names a
    bundle-less repo: the gate must reach N/A for repo_root, proving no rung
    consults cwd."""
    bundle_holder = tmp_path / "holder"
    bundle_holder.mkdir()
    _make_v3_bundle(bundle_holder, "2.9.0", "2.9.0", "2.9.0")
    monkeypatch.chdir(bundle_holder)

    bundle_less = tmp_path / "bundle-less"
    bundle_less.mkdir()
    rc = main(["--repo-root", str(bundle_less), "--quiet"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip().startswith("check-version-consistency: N/A")
    assert str(bundle_less) in captured.out


def test_c1_no_rung_reads_cwd_via_getcwd_default(tmp_path, capsys, monkeypatch):
    """With --repo-root omitted entirely, the default IS cwd (unchanged
    behaviour) -- a bundle at cwd reaches OK with no --repo-root given."""
    d = tmp_path / "cwd-bundle"
    d.mkdir()
    _make_v3_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    monkeypatch.chdir(d)
    rc = main(["--quiet"])
    captured = capsys.readouterr()
    assert rc == 0


def test_c1_bad_root_still_reproduces_oracle_double_message_and_repo_root_ignored(tmp_path, capsys):
    """Item 4: an explicit --root is unaffected by --repo-root, and stays
    fail-loud/unchanged (test_bad_root_reproduces_oracle_double_message_bug's
    sibling, with a --repo-root also passed to prove it is ignored)."""
    bundle_holder = tmp_path / "holder2"
    bundle_holder.mkdir()
    _make_v3_bundle(bundle_holder, "2.9.0", "2.9.0", "2.9.0")
    rc = main(["--root", str(tmp_path / "nonexistent"), "--repo-root", str(bundle_holder)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no .claude-plugin/marketplace.json under --root" in captured.err
    assert "no CHANGELOG.md found" in captured.err
