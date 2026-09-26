from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

# `_BASELINE` is shrink-only pre-existing residue and is explicitly not
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLI = os.path.normpath(os.path.join(_HERE, "..", "spinoff-deliverable-and-commit.py"))
_LIB_DIR = os.path.normpath(os.path.join(_HERE, "..", "lib"))

if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

_spec = importlib.util.spec_from_file_location("spinoff_deliverable_and_commit", _CLI)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)  # type: ignore[union-attr]

from cc_invoke import require_dispatch_engine_on_path, child_env  # noqa: E402
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
    claude_klabauter_root = require_dispatch_engine_on_path()
    from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

    return read_frontmatter_field


def test_resolve_origin_handoff_id_reads_handoff_id_from_named_file(tmp_path):
    read_frontmatter_field = _load_ops()
    origin = tmp_path / "origin-handoff.md"
    _write_frontmatter(origin, handoff_id="hnd-origin-xyz")

    result = _module.resolve_origin_handoff_id(read_frontmatter_field, str(origin))

    assert result == "hnd-origin-xyz"


def test_resolve_origin_handoff_id_empty_when_unset():
    read_frontmatter_field = _load_ops()

    assert _module.resolve_origin_handoff_id(read_frontmatter_field, None) == ""
    assert _module.resolve_origin_handoff_id(read_frontmatter_field, "") == ""


def test_resolve_origin_handoff_id_empty_when_file_missing(tmp_path):
    read_frontmatter_field = _load_ops()
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
