"""
bin.tests.test_dr084_migrate_handoff_vocabulary -- Unit + subprocess tests for
bin/dr084-migrate-handoff-vocabulary.py, hardened ahead of a sibling repo
(claude-central-em) pointing it at a corpus ~4x claude-klabauter's own.

Covers the three hardening asks: real argparse with --dry-run (replacing the
sys.argv[1].startswith("-") footgun that silently migrated cwd on any flag),
atomic same-directory temp-file + os.replace writes, and non-fatal WARN
surfacing of frontmatter values that fail the bare-token status/deployment_state
regex instead of migrating them silently-not-at-all.

Spec: cross-repo DR-084 handoff-lifecycle vocabulary overhaul (open/claimed,
continued/closed); this tool's own module docstring carries the full contract.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_SCRIPT = _REPO_ROOT / "bin" / "dr084-migrate-handoff-vocabulary.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("dr084_migrate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module", autouse=True)
def _require_script() -> None:
    if not _SCRIPT.exists():
        pytest.skip("bin/dr084-migrate-handoff-vocabulary.py not on disk")


@pytest.fixture(scope="module")
def _mod():
    return _load_module()


_ACTIVE_RECORD = """---
handoff_id: "h-active-001"
status: active
predecessor: null
---

body untouched
"""

_CONSUMED_RECORD = """---
handoff_id: "h-consumed-001"
status: consumed
consumed_at: 2026-07-20T10:00:00Z
consumed_by: someone
---

body
"""

_ABANDONED_NO_SUCCESSOR = """---
handoff_id: "h-abandoned-001"
status: superseded
deployment_state: abandoned
---

body
"""

_UNPARSEABLE_STATUS_RECORD = """---
handoff_id: "h-unparseable-001"
status: "needs review"
---

body
"""


def _write(dir_path: Path, name: str, content: str) -> Path:
    p = dir_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / "archive" / "handoffs").mkdir(parents=True)
    _write(repo / "state" / "handoffs", "active.md", _ACTIVE_RECORD)
    _write(repo / "state" / "handoffs", "consumed.md", _CONSUMED_RECORD)
    _write(repo / "archive" / "handoffs", "abandoned.md", _ABANDONED_NO_SUCCESSOR)
    _write(repo / "state" / "handoffs", "unparseable.md", _UNPARSEABLE_STATUS_RECORD)
    return repo


def _run(args, cwd=None, env=None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env.pop("DR084_REPO_ROOT", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(_SCRIPT)] + args,
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _snapshot(repo: Path) -> dict:
    return {
        str(p): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(repo.rglob("*.md"))
    }


def test_dry_run_writes_nothing_but_reports_same_actions(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = _snapshot(repo)

    result = _run([str(repo), "--dry-run"])

    assert result.returncode == 0
    assert _snapshot(repo) == before, "dry-run must not touch any file on disk"
    assert "DRY RUN" in result.stdout
    assert "changed: state/handoffs/active.md -- status:active->open" in result.stdout
    assert "changed: state/handoffs/consumed.md" in result.stdout
    assert "changed: archive/handoffs/abandoned.md" in result.stdout


def test_real_run_migrates_then_second_pass_is_idempotent(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    first = _run([str(repo)])
    assert first.returncode == 0
    assert "changed: state/handoffs/active.md" in first.stdout

    active_text = (repo / "state" / "handoffs" / "active.md").read_text(encoding="utf-8")
    assert "status: open" in active_text
    assert "status: active" not in active_text

    abandoned_text = (repo / "archive" / "handoffs" / "abandoned.md").read_text(encoding="utf-8")
    assert "deployment_state: closed" in abandoned_text
    assert "closed_reason: stale" in abandoned_text

    after_first = _snapshot(repo)
    second = _run([str(repo)])
    assert second.returncode == 0
    assert "files changed: 0 /" in second.stdout
    assert _snapshot(repo) == after_first, "second pass must be a no-op, byte-for-byte"


def test_unrecognized_flag_exits_nonzero_and_does_not_migrate_cwd(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = _snapshot(repo)

    result = _run(["--bogus-flag"], cwd=str(repo))

    assert result.returncode != 0
    assert _snapshot(repo) == before, (
        "an unrecognized flag must hard-error via argparse, never silently "
        "fall through to migrating the cwd"
    )


def test_env_var_takes_precedence_over_positional_arg(tmp_path: Path) -> None:
    real_repo = _make_repo(tmp_path)
    decoy_repo = tmp_path / "decoy"
    (decoy_repo / "state" / "handoffs").mkdir(parents=True)

    result = _run(
        [str(decoy_repo), "--dry-run"],
        env={"DR084_REPO_ROOT": str(real_repo)},
    )

    assert result.returncode == 0
    assert "changed: state/handoffs/active.md" in result.stdout


def test_byte_preservation_of_untouched_lines(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before_bytes = (repo / "state" / "handoffs" / "active.md").read_bytes()
    before_predecessor_line = before_bytes.split(b"\n")[3]
    assert before_predecessor_line == b"predecessor: null"

    _run([str(repo)])

    after_bytes = (repo / "state" / "handoffs" / "active.md").read_bytes()
    after_lines = after_bytes.split(b"\n")
    assert after_lines[3] == b"predecessor: null"
    assert after_bytes.split(b"\n\n", 1)[1] == b"body untouched\n"


def test_unparseable_status_value_warns_and_is_not_migrated(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    result = _run([str(repo)])

    assert result.returncode == 0
    assert "WARN: state/handoffs/unparseable.md: unparseable status value -- skipped" in result.stdout
    assert "unparseable status: 1" in result.stdout
    text = (repo / "state" / "handoffs" / "unparseable.md").read_text(encoding="utf-8")
    assert 'status: "needs review"' in text


def test_resolve_repo_root_env_wins_over_positional(_mod) -> None:
    class _Args:
        repo_root = "/from/positional"

    old = os.environ.get("DR084_REPO_ROOT")
    try:
        os.environ["DR084_REPO_ROOT"] = "/from/env"
        assert _mod.resolve_repo_root(_Args()) == "/from/env"
        os.environ["DR084_REPO_ROOT"] = ""
        assert _mod.resolve_repo_root(_Args()) == "/from/positional"
        del os.environ["DR084_REPO_ROOT"]
        assert _mod.resolve_repo_root(_Args()) == "/from/positional"
    finally:
        if old is None:
            os.environ.pop("DR084_REPO_ROOT", None)
        else:
            os.environ["DR084_REPO_ROOT"] = old


def test_parse_record_collects_unparseable_warning(_mod, tmp_path: Path) -> None:
    path = _write(tmp_path, "rec.md", _UNPARSEABLE_STATUS_RECORD)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        lines = fh.readlines()
    bounds = _mod.frontmatter_bounds(lines)
    record = _mod.parse_record(str(path), lines, bounds)
    assert record["warnings"] == ["status"]


_LEADING_COMMENT_RECORD = """<!--
  provenance: migrated from legacy tool
  multi-line comment block
-->
---
handoff_id: "h-comment-001"
status: active
predecessor: null
---

body untouched
"""

_LEADING_BLANKS_RECORD = """

---
handoff_id: "h-blanks-001"
status: active
predecessor: null
---

body untouched
"""

_NO_FRONTMATTER_HR_BODY = """Some intro text.

---

More body after a horizontal rule.
"""

_UNTERMINATED_COMMENT_RECORD = """<!--
never closed
status: active
---

body
"""


def test_frontmatter_bounds_leading_html_comment_then_frontmatter(_mod) -> None:
    lines = _LEADING_COMMENT_RECORD.splitlines(keepends=True)
    bounds = _mod.frontmatter_bounds(lines)
    assert bounds is not None
    opener, closer = bounds
    assert lines[opener].rstrip("\r\n") == "---"
    assert lines[closer].rstrip("\r\n") == "---"
    assert opener < closer


def test_migrate_file_preserves_leading_comment_and_migrates_frontmatter(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = _write(repo / "state" / "handoffs", "with-comment.md", _LEADING_COMMENT_RECORD)
    before_comment_block = "\n".join(_LEADING_COMMENT_RECORD.splitlines()[:4])

    result = _run([str(repo)])

    assert result.returncode == 0
    assert "changed: state/handoffs/with-comment.md" in result.stdout
    after_text = path.read_text(encoding="utf-8")
    assert after_text.startswith(before_comment_block)
    assert "status: open" in after_text
    assert "status: active" not in after_text
    assert "body untouched" in after_text


def test_frontmatter_bounds_leading_blank_lines_then_frontmatter(_mod) -> None:
    lines = _LEADING_BLANKS_RECORD.splitlines(keepends=True)
    bounds = _mod.frontmatter_bounds(lines)
    assert bounds is not None
    opener, closer = bounds
    assert lines[opener].rstrip("\r\n") == "---"
    assert lines[closer].rstrip("\r\n") == "---"
    assert opener < closer


def test_frontmatter_bounds_none_when_no_frontmatter_but_body_has_hr(_mod) -> None:
    lines = _NO_FRONTMATTER_HR_BODY.splitlines(keepends=True)
    assert _mod.frontmatter_bounds(lines) is None


def test_frontmatter_bounds_none_on_unterminated_comment(_mod) -> None:
    lines = _UNTERMINATED_COMMENT_RECORD.splitlines(keepends=True)
    assert _mod.frontmatter_bounds(lines) is None


def test_frontmatter_bounds_plain_dashes_on_line_zero_unchanged(_mod) -> None:
    lines = _ACTIVE_RECORD.splitlines(keepends=True)
    bounds = _mod.frontmatter_bounds(lines)
    assert bounds == (0, 4)


def test_atomic_write_leaves_no_temp_file_behind(_mod, tmp_path: Path) -> None:
    target = _write(tmp_path, "target.md", "line one\nline two\n")
    _mod._atomic_write(str(target), ["replaced\n"])
    assert target.read_text(encoding="utf-8") == "replaced\n"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "target.md"]
    assert leftovers == []


def test_atomic_write_preserves_original_file_mode(_mod, tmp_path: Path) -> None:
    target = _write(tmp_path, "target.md", "line one\nline two\n")
    os.chmod(target, 0o644)
    _mod._atomic_write(str(target), ["replaced\n"])
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644


def test_real_migration_preserves_file_mode(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    active_path = repo / "state" / "handoffs" / "active.md"
    os.chmod(active_path, 0o644)

    result = _run([str(repo)])

    assert result.returncode == 0
    assert stat.S_IMODE(os.stat(active_path).st_mode) == 0o644
