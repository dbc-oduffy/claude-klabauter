"""
coordinator_core.distill.tests.test_log_append

Unit tests for coordinator_core.distill.log_append — the canonical distillation-log
WRITER (C7). Every distill-run append goes THROUGH this module so the on-disk format
can never drift from what `_common.parse_distillation_log` consumes.

Coverage:
  render_row:
    (a) renders the exact canonical row shape with ASCII "->"
    (b) raises ValueError on an invalid disposition
    (c) raises ValueError on empty path / fate / run_id
  append_row:
    (d) creates a new log file with a fresh "## Run <id>" header + row when the file
        does not exist
    (e) appends a row under an EXISTING "## Run <id>" header without duplicating it,
        when the file already has other runs before/after
    (f) opens a new "## Run <id>" header at EOF when the file exists but has no
        header for this run_id yet
    (g) multiple appends to the same run accumulate rows under one header, in order
    (h) round-trip: append via the writer, then parse back via _common's
        parse_distillation_log, and recover path/disposition/fate/run_id exactly
    (i) an invalid disposition passed to append_row raises before any write occurs
        (file is left untouched / not created)
    (j) round-trip: a fate string containing parentheses (a plausible free-text
        parenthetical aside) still round-trips exactly — regression test for the
        workflow-review P1 finding (2026-07-12): the writer previously permitted
        parenthesized fate text that _common's fate-capture group silently failed
        to parse back, dropping a legitimately DISTILLED row with no error
  append_rows (bulk, all-or-nothing):
    (k) happy path — many rows, mixed run_ids, one invocation, parse back exactly
    (l) one invalid row rejects the WHOLE batch with nothing written (fresh file
        never created; existing file byte-identical)
    (m) empty batch raises ValueError (a no-op batch is a caller bug)
  CLI (bin/distill-log-append.py):
    (n) --batch happy path over a JSON Lines file and stdin ('-'), with --run-id
        as the default for rows omitting "run_id"
    (o) --batch with one bad row exits 1 with a JSON error and writes nothing
    (p) single-row invocation shape unchanged (backward compat)

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C7
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.distill._common import parse_distillation_log
from coordinator_core.distill.log_append import append_row, append_rows, render_row

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# render_row
# ---------------------------------------------------------------------------


def test_render_row_exact_canonical_shape():
    row = render_row("archive/specs/foo.md", "DISTILLED", "distilled into wiki/foo.md", "run-1")
    assert row == "- archive/specs/foo.md -> DISTILLED, distilled into wiki/foo.md (run: run-1)"
    assert "->" in row
    assert "→" not in row  # never a unicode arrow


@pytest.mark.parametrize("bad_disposition", ["HARVESTED", "distilled", "", "PENDING"])
def test_render_row_rejects_invalid_disposition(bad_disposition):
    with pytest.raises(ValueError):
        render_row("archive/specs/foo.md", bad_disposition, "some fate", "run-1")


@pytest.mark.parametrize(
    "path,disposition,fate,run_id",
    [
        ("", "DISTILLED", "fate", "run-1"),
        ("archive/specs/foo.md", "DISTILLED", "", "run-1"),
        ("archive/specs/foo.md", "DISTILLED", "fate", ""),
    ],
)
def test_render_row_rejects_empty_fields(path, disposition, fate, run_id):
    with pytest.raises(ValueError):
        render_row(path, disposition, fate, run_id)


# ---------------------------------------------------------------------------
# append_row
# ---------------------------------------------------------------------------


def test_append_row_creates_new_file_with_header(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    result = append_row(log_path, "archive/specs/a.md", "DISTILLED", "into wiki/a.md", "run-1")

    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert text == "## Run run-1\n- archive/specs/a.md -> DISTILLED, into wiki/a.md (run: run-1)\n"
    assert result.header_opened is True


def test_append_row_joins_existing_header_without_duplicating(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(
        "## Run run-0\n"
        "- archive/specs/old.md -> DISTILLED, prior run (run: run-0)\n"
        "## Run run-1\n"
        "- archive/specs/a.md -> DISTILLED, into wiki/a.md (run: run-1)\n"
        "## Run run-2\n"
        "- archive/specs/later.md -> SKIP, not relevant (run: run-2)\n",
        encoding="utf-8",
    )

    result = append_row(log_path, "archive/specs/b.md", "PROMOTE", "into wiki/b.md", "run-1")

    text = log_path.read_text(encoding="utf-8")
    assert text.count("## Run run-1") == 1
    lines = text.splitlines()
    run1_idx = lines.index("## Run run-1")
    run2_idx = lines.index("## Run run-2")
    # the new row must land between the run-1 header and the run-2 header
    assert any(
        line == "- archive/specs/b.md -> PROMOTE, into wiki/b.md (run: run-1)"
        for line in lines[run1_idx:run2_idx]
    )
    assert result.header_opened is False


def test_append_row_opens_new_header_when_run_id_absent(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(
        "## Run run-0\n- archive/specs/old.md -> DISTILLED, prior run (run: run-0)\n",
        encoding="utf-8",
    )

    result = append_row(log_path, "archive/specs/new.md", "EPHEMERAL", "not distilled", "run-9")

    text = log_path.read_text(encoding="utf-8")
    assert "## Run run-9" in text
    assert text.count("## Run run-9") == 1
    assert "- archive/specs/new.md -> EPHEMERAL, not distilled (run: run-9)" in text
    assert result.header_opened is True


def test_append_row_accumulates_multiple_rows_in_order(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    append_row(log_path, "archive/specs/a.md", "DISTILLED", "fate a", "run-1")
    append_row(log_path, "archive/specs/b.md", "PROMOTE", "fate b", "run-1")
    append_row(log_path, "archive/specs/c.md", "SKIP", "fate c", "run-1")

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert [r.path for r in rows] == [
        "archive/specs/a.md",
        "archive/specs/b.md",
        "archive/specs/c.md",
    ]
    assert all(r.run_id == "run-1" for r in rows)


@pytest.mark.parametrize(
    "path,disposition,fate,run_id",
    [
        ("archive/specs/foo.md", "DISTILLED", "distilled into wiki/foo.md", "run-1"),
        ("archive/specs/bar.md", "PROMOTE", "promoted to decision", "run-42"),
        ("archive/specs/baz.md", "EPHEMERAL", "ephemeral scratch, no capture", "run-7"),
        ("archive/specs/qux.md", "SKIP", "not yet reviewed", "run-a1b2"),
        ("archive/specs/quux.md", "PRESERVE", "kept as-is, active reference", "run-z"),
    ],
)
def test_append_then_parse_round_trip(tmp_path, path, disposition, fate, run_id):
    log_path = tmp_path / "distillation-log.md"
    append_row(log_path, path, disposition, fate, run_id)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    row = rows[0]
    assert row.path == path
    assert row.disposition == disposition
    assert row.fate == fate
    assert row.run_id == run_id


def test_append_then_parse_round_trip_fate_with_parens(tmp_path):
    # Review: workflow-review — writer/parser round-trip regression guard for a
    # fate string containing parentheses (previously silently dropped by the
    # parser's parenthesis-excluding fate capture group).
    log_path = tmp_path / "distillation-log.md"
    fate = "distilled into wiki (agent-hierarchy)"
    append_row(log_path, "archive/specs/foo.md", "DISTILLED", fate, "run-1")

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    row = rows[0]
    assert row.path == "archive/specs/foo.md"
    assert row.disposition == "DISTILLED"
    assert row.fate == fate
    assert row.run_id == "run-1"


def test_append_row_invalid_disposition_does_not_write(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    with pytest.raises(ValueError):
        append_row(log_path, "archive/specs/foo.md", "HARVESTED", "fate", "run-1")
    assert not log_path.exists()


def test_append_row_invalid_disposition_leaves_existing_file_untouched(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    original = "## Run run-1\n- archive/specs/a.md -> DISTILLED, fate a (run: run-1)\n"
    log_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError):
        append_row(log_path, "archive/specs/bad.md", "NOT_A_REAL_DISPOSITION", "fate", "run-1")

    assert log_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# append_rows (bulk, all-or-nothing)
# ---------------------------------------------------------------------------


def test_append_rows_happy_path_mixed_run_ids(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    rows = [
        {"path": "archive/specs/a.md", "disposition": "DISTILLED", "fate": "fate a", "run_id": "run-1"},
        {"path": "archive/specs/b.md", "disposition": "PROMOTE", "fate": "fate b", "run_id": "run-1"},
        {"path": "archive/specs/c.md", "disposition": "SKIP", "fate": "fate c", "run_id": "run-2"},
    ]
    results = append_rows(log_path, rows)

    assert len(results) == 3
    text = log_path.read_text(encoding="utf-8")
    assert text.count("## Run run-1") == 1
    assert text.count("## Run run-2") == 1

    parsed = parse_distillation_log(text)
    assert [(r.path, r.disposition, r.fate, r.run_id) for r in parsed] == [
        ("archive/specs/a.md", "DISTILLED", "fate a", "run-1"),
        ("archive/specs/b.md", "PROMOTE", "fate b", "run-1"),
        ("archive/specs/c.md", "SKIP", "fate c", "run-2"),
    ]


def test_append_rows_joins_existing_run_block(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    append_row(log_path, "archive/specs/old.md", "DISTILLED", "prior", "run-1")

    append_rows(
        log_path,
        [
            {"path": "archive/specs/a.md", "disposition": "PRESERVE", "fate": "kept", "run_id": "run-1"},
            {"path": "archive/specs/b.md", "disposition": "EPHEMERAL", "fate": "scratch", "run_id": "run-1"},
        ],
    )

    text = log_path.read_text(encoding="utf-8")
    assert text.count("## Run run-1") == 1
    parsed = parse_distillation_log(text)
    assert [r.path for r in parsed] == [
        "archive/specs/old.md",
        "archive/specs/a.md",
        "archive/specs/b.md",
    ]


def test_append_rows_one_bad_row_rejects_batch_fresh_file(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    rows = [
        {"path": "archive/specs/a.md", "disposition": "DISTILLED", "fate": "fate a", "run_id": "run-1"},
        {"path": "archive/specs/b.md", "disposition": "HARVESTED", "fate": "fate b", "run_id": "run-1"},
        {"path": "archive/specs/c.md", "disposition": "SKIP", "fate": "fate c", "run_id": "run-1"},
    ]
    with pytest.raises(ValueError, match="row 1"):
        append_rows(log_path, rows)
    assert not log_path.exists()


def test_append_rows_one_bad_row_leaves_existing_file_untouched(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    original = "## Run run-0\n- archive/specs/old.md -> DISTILLED, prior (run: run-0)\n"
    log_path.write_text(original, encoding="utf-8")

    rows = [
        {"path": "archive/specs/a.md", "disposition": "DISTILLED", "fate": "fate a", "run_id": "run-1"},
        {"path": "archive/specs/b.md", "disposition": "DISTILLED", "fate": "", "run_id": "run-1"},
    ]
    with pytest.raises(ValueError, match="row 1"):
        append_rows(log_path, rows)
    assert log_path.read_text(encoding="utf-8") == original


def test_append_rows_missing_key_rejects_batch(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    rows = [
        {"path": "archive/specs/a.md", "disposition": "DISTILLED", "fate": "fate a", "run_id": "run-1"},
        {"path": "archive/specs/b.md", "disposition": "DISTILLED", "fate": "fate b"},
    ]
    with pytest.raises(ValueError, match="row 1.*run_id"):
        append_rows(log_path, rows)
    assert not log_path.exists()


def test_append_rows_empty_batch_raises(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    with pytest.raises(ValueError, match="empty batch"):
        append_rows(log_path, [])
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# CLI (bin/distill-log-append.py)
# ---------------------------------------------------------------------------

_CLI = Path(__file__).resolve().parents[3] / "bin" / "distill-log-append.py"


def _run_cli(args: list[str], stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_cli_batch_file_happy_path(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    batch = tmp_path / "rows.jsonl"
    batch.write_text(
        json.dumps({"path": "archive/specs/a.md", "disposition": "DISTILLED", "fate": "fate a", "run_id": "run-1"})
        + "\n"
        + json.dumps({"path": "archive/specs/b.md", "disposition": "SKIP", "fate": "fate b", "run_id": "run-1"})
        + "\n",
        encoding="utf-8",
    )

    proc = _run_cli(["--log-path", str(log_path), "--batch", str(batch)])

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["rows_appended"] == 2
    parsed = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert [r.path for r in parsed] == ["archive/specs/a.md", "archive/specs/b.md"]


def test_cli_batch_stdin_with_default_run_id(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    stdin_text = (
        json.dumps({"path": "archive/specs/a.md", "disposition": "DISTILLED", "fate": "fate a"})
        + "\n"
        + json.dumps({"path": "archive/specs/b.md", "disposition": "PRESERVE", "fate": "fate b", "run_id": "run-9"})
        + "\n"
    )

    proc = _run_cli(
        ["--log-path", str(log_path), "--batch", "-", "--run-id", "run-1"],
        stdin_text=stdin_text,
    )

    assert proc.returncode == 0, proc.stderr
    parsed = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert [(r.path, r.run_id) for r in parsed] == [
        ("archive/specs/a.md", "run-1"),
        ("archive/specs/b.md", "run-9"),
    ]


def test_cli_batch_bad_row_exits_1_writes_nothing(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    batch = tmp_path / "rows.jsonl"
    batch.write_text(
        json.dumps({"path": "archive/specs/a.md", "disposition": "DISTILLED", "fate": "fate a", "run_id": "run-1"})
        + "\n"
        + json.dumps({"path": "archive/specs/b.md", "disposition": "HARVESTED", "fate": "fate b", "run_id": "run-1"})
        + "\n",
        encoding="utf-8",
    )

    proc = _run_cli(["--log-path", str(log_path), "--batch", str(batch)])

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "error" in payload
    assert not log_path.exists()


def test_cli_batch_mutually_exclusive_with_single_row_flags(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    proc = _run_cli(
        [
            "--log-path", str(log_path),
            "--batch", "-",
            "--path", "archive/specs/a.md",
        ]
    )
    assert proc.returncode == 2  # argparse usage error
    assert not log_path.exists()


def test_cli_single_row_shape_unchanged(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    proc = _run_cli(
        [
            "--log-path", str(log_path),
            "--path", "archive/specs/a.md",
            "--disposition", "DISTILLED",
            "--fate", "into wiki/a.md",
            "--run-id", "run-1",
        ]
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["row"] == "- archive/specs/a.md -> DISTILLED, into wiki/a.md (run: run-1)"
    assert payload["header_opened"] is True
    assert log_path.read_text(encoding="utf-8") == (
        "## Run run-1\n- archive/specs/a.md -> DISTILLED, into wiki/a.md (run: run-1)\n"
    )
