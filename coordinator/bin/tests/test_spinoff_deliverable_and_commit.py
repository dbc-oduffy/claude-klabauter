"""test_spinoff_deliverable_and_commit — pytest coverage for
spinoff-deliverable-and-commit.py.

Purpose: exercises the three concerns ported out of DoE-claude
coordinator/skills/spinoff/SKILL.md into coordinator/bin/spinoff-deliverable-and-commit.py:
  1. resolve-deliverable — D1 carry-not-remint cascade (single parent artifact,
     fail-loud empty-slug guard).
  2. resolve-origin-handoff-id — C2 ID-companion, same-file resolve.
  3. commit-scope — scope: block extraction (awk port) + fail-loud-on-empty +
     scoped commit including the handoff file.

Spec backlink: coordinator/skills/spinoff/SKILL.md § "Deliverable-spine threading
               (D1 carry-not-remint)" (C3d block), § "origin_handoff_id:" (C2 block),
               § "Step 4: Commit" — DoE-claude
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

# Declared, not excused: the commit-scope CLI tests below spawn real
# `git`/CLI-subprocess processes because the property under test is the
# real end-to-end CLI contract (exit codes, real `git add`/`commit` on a
# scratch repo) -- no mock stands in for that. `_init_repo` is called
# per-test, not hoisted to module scope, since each test drives its own
# commit/scope scenario against a fresh repo. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not
# the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLI = os.path.normpath(os.path.join(_HERE, "..", "spinoff-deliverable-and-commit.py"))
_LIB_DIR = os.path.normpath(os.path.join(_HERE, "..", "lib"))

if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

_spec = importlib.util.spec_from_file_location("spinoff_deliverable_and_commit", _CLI)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)  # type: ignore[union-attr]

from cc_invoke import _resolve_claude_klabauter_root, child_env  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402


def _write_frontmatter(path, **fields):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# body")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _load_ops():
    claude_klabauter_root = _resolve_claude_klabauter_root()
    if claude_klabauter_root not in sys.path:
        sys.path.insert(0, claude_klabauter_root)
    from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field
    from coordinator_core.ops.mint_deliverable_id import mint

    return read_frontmatter_field, mint


# ---------------------------------------------------------------------------
# resolve_deliverable_and_initiative (C3d)
# ---------------------------------------------------------------------------


def test_carries_deliverable_id_from_parent_artifact(tmp_path):
    read_frontmatter_field, mint = _load_ops()
    parent = tmp_path / "roadmap-stub.md"
    _write_frontmatter(parent, deliverable_id="dlv-parent-thing-abc123", initiative="init-foo")

    dlvr_id, initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(parent), "some-slug"
    )

    assert dlvr_id == "dlv-parent-thing-abc123"
    assert initiative_id == "init-foo"


def test_mints_fresh_from_slug_when_no_parent_artifact(tmp_path):
    read_frontmatter_field, mint = _load_ops()

    dlvr_id, initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, None, "my-spinoff-slug"
    )

    assert dlvr_id.startswith("dlv-my-spinoff-slug-")
    assert initiative_id == ""


def test_mints_fresh_from_slug_when_parent_lacks_deliverable_id(tmp_path):
    read_frontmatter_field, mint = _load_ops()
    parent = tmp_path / "plan.md"
    _write_frontmatter(parent)  # no deliverable_id field at all

    dlvr_id, initiative_id = _module.resolve_deliverable_and_initiative(
        read_frontmatter_field, mint, str(parent), "fallback-slug"
    )

    assert dlvr_id.startswith("dlv-fallback-slug-")
    assert initiative_id == ""


def test_fail_loud_guard_raises_when_no_parent_and_empty_slug(tmp_path):
    read_frontmatter_field, mint = _load_ops()

    try:
        _module.resolve_deliverable_and_initiative(read_frontmatter_field, mint, None, "")
        assert False, "expected ValueError for empty slug"
    except ValueError as exc:
        assert "slug is empty" in str(exc)


def test_cli_resolve_deliverable_carry_path_emits_shell_assignments(tmp_path):
    parent = tmp_path / "plan.md"
    _write_frontmatter(parent, deliverable_id="dlv-cli-check-111222", initiative="init-cli")

    proc = subprocess.run(
        [sys.executable, _CLI, "resolve-deliverable", "--parent-artifact", str(parent), "--slug", "unused"],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, proc.stderr
    stdout_lines = proc.stdout.strip().splitlines()
    assert stdout_lines == [
        "DLVR_ID=dlv-cli-check-111222",
        "INITIATIVE_ID=init-cli",
    ]
    assert "carry path" in proc.stderr


def test_cli_resolve_deliverable_empty_slug_guard_exits_nonzero_fail_loud():
    proc = subprocess.run(
        [sys.executable, _CLI, "resolve-deliverable", "--slug", ""],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 1
    assert "slug is empty" in proc.stderr


# ---------------------------------------------------------------------------
# resolve_origin_handoff_id (C2)
# ---------------------------------------------------------------------------


def test_resolve_origin_handoff_id_reads_handoff_id_from_named_file(tmp_path):
    read_frontmatter_field, _mint = _load_ops()
    origin = tmp_path / "origin-handoff.md"
    _write_frontmatter(origin, handoff_id="hnd-origin-xyz")

    result = _module.resolve_origin_handoff_id(read_frontmatter_field, str(origin))

    assert result == "hnd-origin-xyz"


def test_resolve_origin_handoff_id_empty_when_unset():
    read_frontmatter_field, _mint = _load_ops()

    assert _module.resolve_origin_handoff_id(read_frontmatter_field, None) == ""
    assert _module.resolve_origin_handoff_id(read_frontmatter_field, "") == ""


def test_resolve_origin_handoff_id_empty_when_file_missing(tmp_path):
    read_frontmatter_field, _mint = _load_ops()
    missing = tmp_path / "does-not-exist.md"

    assert _module.resolve_origin_handoff_id(read_frontmatter_field, str(missing)) == ""


def test_cli_resolve_origin_handoff_id_emits_shell_assignment(tmp_path):
    origin = tmp_path / "origin-handoff.md"
    _write_frontmatter(origin, handoff_id="hnd-cli-check")

    proc = subprocess.run(
        [sys.executable, _CLI, "resolve-origin-handoff-id", "--origin-handoff", str(origin)],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ORIGIN_HANDOFF_ID=hnd-cli-check"


# ---------------------------------------------------------------------------
# extract_scope_paths (Step 4 awk port)
# ---------------------------------------------------------------------------


def test_extract_scope_paths_basic_block():
    text = "\n".join(
        [
            "---",
            "title: Foo",
            "scope:",
            "  - path/one.md",
            "  - path/two.py",
            "workstream: bar",
            "---",
            "# body",
        ]
    )
    assert _module.extract_scope_paths(text) == ["path/one.md", "path/two.py"]


def test_extract_scope_paths_stops_at_next_top_level_key():
    text = "\n".join(
        [
            "scope:",
            "  - only-one.md",
            "kind: spinoff",
            "  - not-a-scope-entry-anymore.md",
        ]
    )
    assert _module.extract_scope_paths(text) == ["only-one.md"]


def test_extract_scope_paths_missing_block_returns_empty():
    text = "\n".join(["---", "title: Foo", "workstream: bar", "---"])
    assert _module.extract_scope_paths(text) == []


def test_extract_scope_paths_empty_block_returns_empty():
    text = "\n".join(["scope:", "workstream: bar"])
    assert _module.extract_scope_paths(text) == []


# ---------------------------------------------------------------------------
# commit-scope CLI (Step 4 fail-loud + scoped git add/commit)
# ---------------------------------------------------------------------------


def _init_repo(repo_dir):
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)


def test_cli_commit_scope_fails_loud_on_missing_scope_block(tmp_path):
    handoff = tmp_path / "state" / "handoffs" / "spinoff.md"
    handoff.parent.mkdir(parents=True)
    _write_frontmatter(handoff, title="No Scope")

    proc = subprocess.run(
        [sys.executable, _CLI, "commit-scope", "--handoff", str(handoff), "--slug", "my-slug", "--cwd", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 1
    assert "scope: block missing or empty" in proc.stderr


def test_cli_commit_scope_dry_run_prints_git_commands(tmp_path):
    handoff = tmp_path / "state" / "handoffs" / "spinoff.md"
    handoff.parent.mkdir(parents=True)
    scoped_file = tmp_path / "coordinator" / "example.py"
    scoped_file.parent.mkdir(parents=True)
    scoped_file.write_text("# example\n", encoding="utf-8")
    _write_frontmatter(
        handoff,
        title="Has Scope",
        **{"scope": "\n  - coordinator/example.py"},
    )

    proc = subprocess.run(
        [
            sys.executable,
            _CLI,
            "commit-scope",
            "--handoff",
            str(handoff),
            "--slug",
            "my-slug",
            "--cwd",
            str(tmp_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, proc.stderr
    assert "git add -- coordinator/example.py" in proc.stdout
    assert "git commit -m" in proc.stdout
    assert "chore(spinoff): my-slug [authored mid-session]" in proc.stdout


def test_cli_commit_scope_actually_commits_scope_and_handoff(tmp_path):
    _init_repo(tmp_path)

    scoped_file = tmp_path / "coordinator" / "example.py"
    scoped_file.parent.mkdir(parents=True)
    scoped_file.write_text("# example\n", encoding="utf-8")

    handoff = tmp_path / "state" / "handoffs" / "spinoff.md"
    handoff.parent.mkdir(parents=True)
    _write_frontmatter(
        handoff,
        title="Has Scope",
        **{"scope": "\n  - coordinator/example.py"},
    )

    proc = subprocess.run(
        [
            sys.executable,
            _CLI,
            "commit-scope",
            "--handoff",
            "state/handoffs/spinoff.md",
            "--slug",
            "my-slug",
            "--cwd",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, proc.stderr

    log = subprocess.run(
        ["git", "log", "-1", "--name-only", "--pretty=format:%s"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    assert "chore(spinoff): my-slug [authored mid-session]" in log.stdout
    assert "coordinator/example.py" in log.stdout
    assert "state/handoffs/spinoff.md" in log.stdout


def test_cli_missing_subcommand_exits_nonzero():
    proc = subprocess.run(
        [sys.executable, _CLI],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(),
        **no_console_creationflags(),
    )
    assert proc.returncode != 0
