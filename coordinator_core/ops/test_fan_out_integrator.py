"""
Tests for coordinator_core.ops.fan_out_integrator (7 assertions: happy-path,
per-block structure, overlap, missing-sidecar, non-git-repo, empty-spec,
malformed-row). Each test below mirrors one bats test by name/number.

Also covers two Python-port-specific edge cases the bash oracle's `read -ra`
comma-split behavior has that a naive `str.split(",")` port would silently get
wrong (see fan_out_integrator._parse_file_lists's inline comment): a trailing
comma in the file-list column must NOT produce a spurious empty-path-entry
error, matching bash's `IFS=',' read -ra` one-trailing-empty-field drop.

Port of: test-fan-out-integrator.sh (coordinator-claude 432e3285, 2026-07-22)
"""
from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest

from coordinator_core.ops import fan_out_integrator as mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "root.txt").write_text("root\n")
    subprocess.run(["git", "add", "root.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture()
def plugin_root(tmp_path, monkeypatch):
    root = tmp_path / "plugin"
    snippets = root / "snippets"
    snippets.mkdir(parents=True)
    (snippets / "peer-scope-block.md").write_text(
        "<!-- header comment -->\n"
        "<!-- more header -->\n"
        "\n"
        "## Out-of-scope — peer work, do NOT touch\n"
        "\n"
        "{{peer_chunks}}\n"
        "\n"
        "If a peer's expected output appears missing on disk, assume a peer is on it.\n"
    )
    (snippets / "text-only-recovery-preamble.md").write_text(
        "### Disk-first verification\nVerify on disk before replying DONE.\n"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    return root


def make_sidecar(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"findings": [], "verdict": "PASS"}\n')


def run_main(spec_text, monkeypatch, capsys, spec_file=None):
    argv = ["--spec", spec_file] if spec_file else []
    monkeypatch.setattr(sys, "stdin", io.StringIO(spec_text))
    rc = mod.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# T1: happy path — 3 slices, no overlap, all sidecars present
# ---------------------------------------------------------------------------


def test_happy_path_three_slices(git_repo, plugin_root, monkeypatch, capsys):
    sidecars = git_repo / "sidecars"
    make_sidecar(sidecars / "slice-A.json")
    make_sidecar(sidecars / "slice-B.json")
    make_sidecar(sidecars / "slice-C.json")

    spec = (
        f"slice-A\t{sidecars}/slice-A.json\tauth.py,auth_test.py\n"
        f"slice-B\t{sidecars}/slice-B.json\tmodels.py,models_test.py\n"
        f"slice-C\t{sidecars}/slice-C.json\tviews.py,views_test.py"
    )
    rc, out, err = run_main(spec, monkeypatch, capsys)

    assert rc == 0
    assert out.count("INTEGRATOR DISPATCH BLOCK:") == 3
    assert out.count("END BLOCK:") == 3
    for sid in ("slice-A", "slice-B", "slice-C"):
        assert sid in out
    assert "EM REMINDERS" not in out
    assert "EM REMINDERS" in err


# ---------------------------------------------------------------------------
# T2: per-block structure
# ---------------------------------------------------------------------------


def test_per_block_structure(git_repo, plugin_root, monkeypatch, capsys):
    sidecars = git_repo / "sidecars"
    make_sidecar(sidecars / "slice-A.json")
    make_sidecar(sidecars / "slice-B.json")

    spec = f"slice-A\t{sidecars}/slice-A.json\tauth.py\nslice-B\t{sidecars}/slice-B.json\tmodels.py"
    rc, out, err = run_main(spec, monkeypatch, capsys)

    assert rc == 0

    def block(slice_id):
        start = out.index(f"DISPATCH BLOCK: {slice_id}")
        end = out.index(f"END BLOCK: {slice_id}")
        return out[start:end]

    slice_a_block = block("slice-A")
    brief_idx = slice_a_block.index("### Brief")
    next_heading_idx = slice_a_block.index("###", brief_idx + len("### Brief"))
    brief_section = slice_a_block[brief_idx:next_heading_idx]
    assert "sidecars/slice-A.json" in brief_section
    assert "Slice: slice-A" in slice_a_block

    assert "RIGHT NOW in parallel" in out
    assert out.count("Destructive-action prohibition") >= 2
    assert "do NOT collate" in out

    assert "slice-B" in block("slice-A")
    assert "slice-A" in block("slice-B")


# ---------------------------------------------------------------------------
# T3: overlap detection
# ---------------------------------------------------------------------------


def test_overlap_detection(git_repo, plugin_root, monkeypatch, capsys):
    sidecars = git_repo / "sidecars"
    make_sidecar(sidecars / "slice-A.json")
    make_sidecar(sidecars / "slice-B.json")

    spec = (
        f"slice-A\t{sidecars}/slice-A.json\tauth.py,shared.py\n"
        f"slice-B\t{sidecars}/slice-B.json\tmodels.py,shared.py"
    )
    rc, out, err = run_main(spec, monkeypatch, capsys)

    assert rc == 1
    assert out == ""
    assert "overlap" in err
    assert "shared.py" in err


# ---------------------------------------------------------------------------
# T4: missing sidecar
# ---------------------------------------------------------------------------


def test_missing_sidecar(git_repo, plugin_root, monkeypatch, capsys):
    sidecars = git_repo / "sidecars"
    make_sidecar(sidecars / "slice-A.json")

    spec = (
        f"slice-A\t{sidecars}/slice-A.json\tauth.py\n"
        f"slice-B\t{sidecars}/slice-B-MISSING.json\tmodels.py"
    )
    rc, out, err = run_main(spec, monkeypatch, capsys)

    assert rc == 1
    assert out == ""
    assert "slice-B-MISSING.json" in err


# ---------------------------------------------------------------------------
# T5: non-git-repo cwd
# ---------------------------------------------------------------------------


def test_non_git_repo(tmp_path, plugin_root, monkeypatch, capsys):
    non_git_dir = tmp_path / "non-git"
    non_git_dir.mkdir()
    monkeypatch.chdir(non_git_dir)

    rc, out, err = run_main("slice-A\t/some/path.json\tfile.py\n", monkeypatch, capsys)

    assert rc == 2
    assert out == ""
    assert "Remediation" in err
    assert "git repository" in err


# ---------------------------------------------------------------------------
# T6: empty spec
# ---------------------------------------------------------------------------


def test_empty_spec(git_repo, plugin_root, monkeypatch, capsys):
    rc, out, err = run_main("", monkeypatch, capsys)

    assert rc == 2
    assert out == ""
    assert "empty spec" in err


# ---------------------------------------------------------------------------
# T7: malformed row
# ---------------------------------------------------------------------------


def test_malformed_row(git_repo, plugin_root, monkeypatch, capsys):
    sidecars = git_repo / "sidecars"
    make_sidecar(sidecars / "slice-A.json")

    spec = f"slice-A\t{sidecars}/slice-A.json"
    rc, out, err = run_main(spec, monkeypatch, capsys)

    assert rc == 1
    assert out == ""
    assert "row 1" in err
    assert "field" in err


# ---------------------------------------------------------------------------
# Port-specific: trailing comma in file-list column (bash read -ra parity)
# ---------------------------------------------------------------------------


def test_trailing_comma_does_not_spuriously_error(git_repo, plugin_root, monkeypatch, capsys):
    sidecars = git_repo / "sidecars"
    make_sidecar(sidecars / "slice-A.json")

    # Trailing comma after the last real path — bash's `IFS=',' read -ra` drops
    # exactly one trailing empty field here (verified empirically against the
    # bash oracle's shape), so this must NOT trip the "empty path entry" error.
    spec = f"slice-A\t{sidecars}/slice-A.json\tauth.py,auth_test.py,"
    rc, out, err = run_main(spec, monkeypatch, capsys)

    assert rc == 0, err
    assert "empty path entry" not in err
    assert "auth.py" in out
    assert "auth_test.py" in out


# ---------------------------------------------------------------------------
# Missing snippet file — exit-2 "environment" bucket (module docstring contract)
# ---------------------------------------------------------------------------


def test_missing_snippet_is_exit_2(git_repo, monkeypatch, capsys, tmp_path):
    empty_plugin_root = tmp_path / "empty-plugin"
    empty_plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(empty_plugin_root))

    sidecars = git_repo / "sidecars"
    make_sidecar(sidecars / "slice-A.json")
    spec = f"slice-A\t{sidecars}/slice-A.json\tauth.py"

    rc, out, err = run_main(spec, monkeypatch, capsys)

    assert rc == 2
    assert out == ""
    assert "peer-scope snippet not found" in err
