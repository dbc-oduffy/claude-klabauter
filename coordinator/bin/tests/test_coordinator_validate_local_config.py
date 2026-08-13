"""Tests for coordinator/bin/coordinator-validate-local-config.py.

Spec backlink: pln-shell-spawn-regrowth-gate-cens-097e21 § C12
"""

from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_CLI_PATH = Path(__file__).resolve().parents[1] / "coordinator-validate-local-config.py"


def _load_cli():
    """Load the extensionless CLI script as a module. Mirrors
    coordinator/bin/test_spawn_census.py's `_load_module` — SourceFileLoader
    is required here (not spec_from_file_location) because the target file
    has no `.py` suffix for Python's import machinery to key off of."""
    loader = SourceFileLoader("coordinator_validate_local_config_under_test", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return _load_cli()


def _write_local_md(tmp_path: Path, frontmatter_body: str) -> Path:
    content = "---\n" + frontmatter_body + "\n---\n"
    path = tmp_path / "coordinator.local.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_conformant_repo_exits_zero(tmp_path, cli):
    _write_local_md(
        tmp_path,
        'fast_test_cmd: "python3 -m pytest -m \'not slow\'"\n'
        "post_command: pnpm publish:fleet",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--repo", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["unconformant_count"] == 0
    keys = {row["key"] for row in payload["rows"]}
    assert keys == {"fast_test_cmd", "post_command"}


def test_w1_violation_from_memo_exits_nonzero(tmp_path, cli):
    _write_local_md(
        tmp_path,
        "workday_complete_post_command: COCKPIT_STATE_SOURCE=firestore pnpm publish:fleet",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--repo", str(tmp_path), "--json"])
    assert rc == 1
    payload = json.loads(buf.getvalue())
    assert payload["unconformant_count"] == 1
    assert payload["rows"][0]["rule"] == "W1-assignment-prefix"


def test_example_store_repo_live_value_not_flagged(tmp_path, cli):
    _write_local_md(
        tmp_path,
        "install_hook_command: python3 bin/guard_store_plaintext.py --install-hook --quiet",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--repo", str(tmp_path), "--json"])
    assert rc == 0


def test_exempt_marker_skips_check(tmp_path, cli):
    _write_local_md(
        tmp_path,
        "legacy_migration_command: echo hi && echo bye\n"
        "argv_only_exempt: [legacy_migration_command]",
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--repo", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    row = payload["rows"][0]
    assert row["key"] == "legacy_migration_command"
    assert row["conformant"] is True
    assert "exempt" in row["detail"]


def test_non_command_keys_are_ignored(tmp_path, cli):
    _write_local_md(tmp_path, "fast_test_cmd: pytest\nsome_other_key: not a command")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--repo", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert {row["key"] for row in payload["rows"]} == {"fast_test_cmd"}


def test_missing_local_md_exits_zero(tmp_path, cli):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--repo", str(tmp_path)])
    assert rc == 0
    assert "no *_command/*_cmd keys found" in buf.getvalue()


def test_human_output_shows_fail_marker(tmp_path, cli):
    _write_local_md(tmp_path, "post_command: echo hi && echo bye")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--repo", str(tmp_path)])
    assert rc == 1
    out = buf.getvalue()
    assert "FAIL" in out
    assert "W2-shell-metachar" in out
