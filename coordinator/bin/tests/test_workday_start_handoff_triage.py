"""test_workday_start_handoff_triage.py — regression suite for
workday-start-handoff-triage.py.

Covers the four subcommands ported from example-doctrine-repo's
coordinator/commands/workday-start.md § Step 0.8 / § Step 1.1 / § Step 1.2:
  - stale-plans   (status:executing + age>threshold frontmatter/git scan)
  - trim-notes    (tasks/orphan-sweep-notes.md tail/head rotation)
  - ready         (records_query.query_records passthrough, ready_to_fire)
  - awaiting-gate (records_query.query_records passthrough, awaiting_gate
                   full listing + >6d stale subset)

Spec backlink: example-doctrine-repo docs/plans (extirpation-of-structural-bash chunk
WDS-2). Port source: example-doctrine-repo coordinator/commands/workday-start.md
§ Step 0.8, § Step 1.1, § Step 1.2 (read-only source; not modified by this
chunk — the example-doctrine-repo-side repoint is a later wave).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import types

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True,
    text=True,
    check=True,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workday-start-handoff-triage.py")
_HANDOFF_SCHEMA_PATH = os.path.join(
    _REPO_ROOT, "coordinator_core", "frontmatter", "schemas", "handoff.schema.json"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("workday_start_handoff_triage", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# --------------------------------------------------------------------------
# stale-plans
# --------------------------------------------------------------------------


def _write_plan(tmp_path, name, status_line, body="Some plan body.\n"):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plans_dir / name
    plan_path.write_text(f"---\n{status_line}\n---\n{body}")
    return plan_path


def test_stale_plans_flags_old_executing_plan(mod, tmp_path, monkeypatch):
    plan_path = _write_plan(tmp_path, "2026-01-01-example.md", "status: executing")
    monkeypatch.setattr(mod, "_git_last_commit_epoch", lambda p: 1000.0)
    results = mod.find_stale_executing_plans(
        str(tmp_path / "docs" / "plans"), threshold_days=3, now=1000.0 + 4 * 86400
    )
    assert len(results) == 1
    assert str(plan_path) in results[0]
    assert "status: executing" in results[0]
    assert "untouched 4d" in results[0]


def test_stale_plans_skips_non_executing_status(mod, tmp_path, monkeypatch):
    _write_plan(tmp_path, "2026-01-01-example.md", "status: implemented")
    monkeypatch.setattr(mod, "_git_last_commit_epoch", lambda p: 1000.0)
    results = mod.find_stale_executing_plans(
        str(tmp_path / "docs" / "plans"), threshold_days=3, now=1000.0 + 30 * 86400
    )
    assert results == []


def test_stale_plans_skips_under_threshold(mod, tmp_path, monkeypatch):
    _write_plan(tmp_path, "2026-01-01-example.md", "status: executing")
    monkeypatch.setattr(mod, "_git_last_commit_epoch", lambda p: 1000.0)
    results = mod.find_stale_executing_plans(
        str(tmp_path / "docs" / "plans"), threshold_days=3, now=1000.0 + 1 * 86400
    )
    assert results == []


def test_stale_plans_skips_plan_with_no_git_history(mod, tmp_path, monkeypatch):
    _write_plan(tmp_path, "2026-01-01-example.md", "status: executing")
    monkeypatch.setattr(mod, "_git_last_commit_epoch", lambda p: None)
    results = mod.find_stale_executing_plans(str(tmp_path / "docs" / "plans"), threshold_days=3)
    assert results == []


def test_stale_plans_missing_directory_returns_empty(mod, tmp_path):
    results = mod.find_stale_executing_plans(str(tmp_path / "does-not-exist"))
    assert results == []


def test_frontmatter_status_prefix_match_no_end_anchor(mod):
    # Preserves the bash original's un-anchored `grep -qE` behavior verbatim
    # (see the docstring on _frontmatter_status_is_executing).
    assert mod._frontmatter_status_is_executing(["status: executing-ish"]) is True
    assert mod._frontmatter_status_is_executing(["status:executing"]) is True
    assert mod._frontmatter_status_is_executing(["status: draft"]) is False
    assert mod._frontmatter_status_is_executing([]) is False


def test_read_frontmatter_lines_only_between_first_two_delimiters(mod, tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("---\nstatus: executing\nkey: value\n---\nBody line\n---\nnot-frontmatter\n")
    lines = mod._read_frontmatter_lines(plan_path)
    assert lines == ["status: executing", "key: value"]


# --------------------------------------------------------------------------
# trim-notes
# --------------------------------------------------------------------------


def test_trim_notes_rotates_and_returns_body(mod, tmp_path):
    notes_path = tmp_path / "orphan-sweep-notes.md"
    notes_path.write_text(
        "# Orphan sweep notes\n"
        "# Header line 2\n"
        "# Header line 3\n"
        "# Header line 4\n"
        "2026-07-20 orphan archived: state/handoffs/foo.md\n"
        "2026-07-21 orphan archived: state/handoffs/bar.md\n"
    )
    body = mod.trim_orphan_sweep_notes(str(notes_path), header_lines=4)
    assert body is not None
    assert "foo.md" in body
    assert "bar.md" in body
    # File rotated back down to just the header.
    remaining = notes_path.read_text().splitlines()
    assert remaining == [
        "# Orphan sweep notes",
        "# Header line 2",
        "# Header line 3",
        "# Header line 4",
    ]


def test_trim_notes_noop_when_at_or_under_header_lines(mod, tmp_path):
    notes_path = tmp_path / "orphan-sweep-notes.md"
    original = "# H1\n# H2\n# H3\n# H4\n"
    notes_path.write_text(original)
    body = mod.trim_orphan_sweep_notes(str(notes_path), header_lines=4)
    assert body is None
    assert notes_path.read_text() == original


def test_trim_notes_missing_file_returns_none(mod, tmp_path):
    body = mod.trim_orphan_sweep_notes(str(tmp_path / "absent.md"), header_lines=4)
    assert body is None


# --------------------------------------------------------------------------
# ready / awaiting-gate — records_query passthrough
# --------------------------------------------------------------------------


class _FakeArgs:
    pass


def _install_fake_records_query(monkeypatch, fn):
    fake_mod = types.ModuleType("records_query")
    fake_mod.query_records = fn
    monkeypatch.setitem(sys.modules, "records_query", fake_mod)


def _status_values_from_where(where: str) -> list[str]:
    """Extract every `status=<value>` predicate value out of a `records_query`
    WHERE-clause string (the equality-AND grammar `query_records()` accepts)."""
    return re.findall(r"(?:^|\bAND\s+)status=(\S+)", where)


def test_ready_and_awaiting_gate_status_predicate_matches_schema_enum(mod):
    """Regression for the DR-084-narrow break: `ready`/`awaiting-gate` build
    a `status=<value>` predicate that MUST be a member of the live handoff
    schema's `status` enum, not a stale/retired vocabulary token (e.g. the
    pre-DR-084 `active`/`consumed` values, dropped 2026-07-22). Deliberately
    reads the enum from the schema file rather than hard-coding the expected
    token, so a future schema-side enum change is caught here too, instead
    of silently re-encoding whatever the predicate happens to say today."""
    schema = json.loads(open(_HANDOFF_SCHEMA_PATH, encoding="utf-8").read())
    status_enum = set(schema["properties"]["status"]["enum"])

    for where in (mod._READY_WHERE, mod._AWAITING_GATE_WHERE):
        status_values = _status_values_from_where(where)
        assert status_values, f"no status=<value> predicate found in WHERE clause: {where!r}"
        for value in status_values:
            assert value in status_enum, (
                f"WHERE clause {where!r} predicates on status={value!r}, which is not in "
                f"the handoff schema's status enum {sorted(status_enum)} — a handoff record "
                "can never satisfy this predicate, so the query returns an empty set "
                "unconditionally."
            )


def test_cmd_ready_passes_expected_query_shape(mod, monkeypatch, capsys):
    calls = []

    def fake_query_records(record_type, where, format_, limit, *, sort=None, older_than=None):
        calls.append((record_type, where, format_, limit, sort, older_than))
        return "- [a handoff](path) — active\n"

    _install_fake_records_query(monkeypatch, fake_query_records)
    rc = mod._cmd_ready(_FakeArgs())
    assert rc == 0
    assert calls == [
        ("handoff", "deployment_state=ready_to_fire AND status=open", "markdown-list", 0, "-created", None)
    ]
    out = capsys.readouterr().out
    assert "a handoff" in out


def test_cmd_awaiting_gate_runs_both_queries(mod, monkeypatch, capsys):
    calls = []

    def fake_query_records(record_type, where, format_, limit, *, sort=None, older_than=None):
        calls.append((record_type, where, format_, limit, sort, older_than))
        if older_than:
            return "- [stale handoff](path2) — active\n"
        return "- [gated handoff](path1) — active\n"

    _install_fake_records_query(monkeypatch, fake_query_records)
    rc = mod._cmd_awaiting_gate(_FakeArgs())
    assert rc == 0
    assert calls == [
        ("handoff", "deployment_state=awaiting_gate AND status=open", "markdown-list", 0, "-created", None),
        ("handoff", "deployment_state=awaiting_gate AND status=open", "markdown-list", 0, None, "6d"),
    ]
    out = capsys.readouterr().out
    assert "gated handoff" in out
    assert "stale handoff" in out


def test_cmd_awaiting_gate_omits_stale_section_when_empty(mod, monkeypatch, capsys):
    def fake_query_records(record_type, where, format_, limit, *, sort=None, older_than=None):
        if older_than:
            return ""
        return "- [gated handoff](path1) — active\n"

    _install_fake_records_query(monkeypatch, fake_query_records)
    rc = mod._cmd_awaiting_gate(_FakeArgs())
    assert rc == 0
    out = capsys.readouterr().out
    assert "gated handoff" in out
    assert "older than 6d" not in out


def test_cmd_ready_transport_failure_exits_setup_error(mod, monkeypatch, capsys):
    def fake_query_records(*args, **kwargs):
        raise RuntimeError("engine unreachable")

    _install_fake_records_query(monkeypatch, fake_query_records)
    rc = mod._cmd_ready(_FakeArgs())
    assert rc == mod._SETUP_ERROR
    err = capsys.readouterr().err
    assert "engine unreachable" in err


# --------------------------------------------------------------------------
# CLI wiring smoke test
# --------------------------------------------------------------------------


def test_cli_end_to_end_stale_plans(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    (plans_dir / "old.md").write_text("---\nstatus: executing\n---\nbody\n")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add stale plan"],
        cwd=tmp_path,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    proc = subprocess.run(
        [sys.executable, _TARGET, "stale-plans", str(plans_dir), "--threshold-days", "-1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0
    assert "old.md" in proc.stdout
    assert "status: executing" in proc.stdout
