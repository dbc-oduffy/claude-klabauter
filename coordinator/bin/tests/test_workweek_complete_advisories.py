# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""bin/tests/test_workweek_complete_advisories.py

Purpose: unit tests for coordinator/bin/workweek-complete-advisories.py — the
M3 chunk WWC-1 port of four genuine bash-logic fences out of example-doctrine-repo's
`coordinator/commands/workweek-complete.md` (tripwire fire-log summarization,
improvement-queue depth counting, cruft-sweep last-run parsing, and the
ubt-unresolved CLI dispatch over the already-ported
`coordinator_core.ops.scan_unresolved_ubt_records`).

Coverage:
  test_tripwire_absent_file_returns_none
  test_tripwire_summary_counts_and_recurring_agents
  test_improvement_queue_depth_absent_dir
  test_improvement_queue_depth_counts_and_oldest
  test_cruft_sweep_last_run_absent
  test_cruft_sweep_last_run_parses_pipe_delimited_log
  test_ubt_unresolved_cli_lists_only_unpaired_markers
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    """Load workweek-complete-advisories.py by file path (hyphenated name bypass)."""
    spec = importlib.util.spec_from_file_location(
        "workweek_complete_advisories",
        _BIN_DIR / "workweek-complete-advisories.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# tripwire-summary — Step 3.5 oracle fence (workweek-complete.md:608-617)
# ---------------------------------------------------------------------------


def test_tripwire_absent_file_returns_none(tmp_path: Path) -> None:
    missing = tmp_path / "runtime-tripwire-fire-log.tsv"
    assert _mod.summarize_tripwire_fire_log(missing) is None


def test_tripwire_summary_counts_and_recurring_agents(tmp_path: Path) -> None:
    fire_log = tmp_path / "runtime-tripwire-fire-log.tsv"
    header = "timestamp\tagentId\tmodel\telapsed_min\tfire_type"
    rows = [
        "t1\tagent-a\tsonnet\t5\tem-side",
        "t2\tagent-a\tsonnet\t6\tem-side",
        "t3\tagent-a\tsonnet\t7\tagent-side",
        "t4\tagent-b\topus\t3\tem-side",
    ]
    fire_log.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")

    summary = _mod.summarize_tripwire_fire_log(fire_log)
    assert summary is not None
    assert summary["total_rows"] == 4
    assert summary["fire_type_counts"] == {"em-side": 3, "agent-side": 1}
    # agent-a fired 3 times (>=3 threshold); agent-b fired once (excluded).
    assert summary["recurring_agents"] == [("agent-a", 3)]


def test_tripwire_cmd_prints_absent_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "runtime-tripwire-fire-log.tsv"
    rc = _mod.main(["tripwire-summary", str(missing)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "absent — skipping." in out


# ---------------------------------------------------------------------------
# improvement-queue-depth — Step 4 oracle fence (workweek-complete.md:746-755)
# ---------------------------------------------------------------------------


def test_improvement_queue_depth_absent_dir(tmp_path: Path) -> None:
    absent = tmp_path / "improvement-queue"
    count, oldest = _mod.improvement_queue_depth(absent)
    assert (count, oldest) == (0, None)


def test_improvement_queue_depth_counts_and_oldest(tmp_path: Path) -> None:
    queue_dir = tmp_path / "improvement-queue"
    queue_dir.mkdir()
    (queue_dir / "2026-07-20-first.yaml").write_text("title: first\n", encoding="utf-8")
    (queue_dir / "2026-07-22-second.yaml").write_text("title: second\n", encoding="utf-8")
    (queue_dir / "2026-07-21-third.yaml").write_text("title: third\n", encoding="utf-8")
    # Non-yaml file must not be counted.
    (queue_dir / "README.md").write_text("not an entry\n", encoding="utf-8")

    count, oldest = _mod.improvement_queue_depth(queue_dir)
    assert count == 3
    assert oldest == "2026-07-20-first.yaml"


def test_improvement_queue_cmd_reports_absent_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    absent = tmp_path / "improvement-queue"
    rc = _mod.main(["improvement-queue-depth", str(absent)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dir absent" in out
    assert "0 entries." in out


# ---------------------------------------------------------------------------
# cruft-sweep-last-run — cruft-sweep verification oracle fence
# (workweek-complete.md:1037-1041, Review: code-reviewer Slice C F5)
# ---------------------------------------------------------------------------


def test_cruft_sweep_last_run_absent(tmp_path: Path) -> None:
    missing = tmp_path / "cruft-sweep-log.md"
    assert _mod.cruft_sweep_last_run(missing) is None


def test_cruft_sweep_last_run_parses_pipe_delimited_log(tmp_path: Path) -> None:
    log_path = tmp_path / "cruft-sweep-log.md"
    # Pipe-delimited: <class> | <timestamp> | <reclaimed>. A naive whitespace
    # split (awk '{print $1}') would return the literal "|" separator, not the
    # timestamp — this regression guard is the entire point of F5.
    log_path.write_text(
        "all | 2026-07-10T09:00:00Z | 12MB\n"
        "all | 2026-07-23T14:30:00Z | 4MB\n",
        encoding="utf-8",
    )
    assert _mod.cruft_sweep_last_run(log_path) == "2026-07-23T14:30:00Z"


def test_cruft_sweep_cmd_reports_never_when_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "cruft-sweep-log.md"
    rc = _mod.main(["cruft-sweep-last-run", str(missing)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cruft-sweep last run: never" in out


# ---------------------------------------------------------------------------
# ubt-unresolved — Step 4c oracle fence (workweek-complete.md:1331-1333)
# ---------------------------------------------------------------------------


def test_ubt_unresolved_cli_lists_only_unpaired_markers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review_trail = tmp_path / "state" / "review-trail"
    review_trail.mkdir(parents=True)

    # Paired: pending + resolved sibling -> must NOT appear in output.
    paired_pending = review_trail / "abc123.ubt-compile.pending.json"
    paired_pending.write_text(json.dumps({"sha_range": "abc..def"}), encoding="utf-8")
    (review_trail / "abc123.resolved.json").write_text("{}", encoding="utf-8")

    # Unpaired: pending only -> must appear.
    unpaired_pending = review_trail / "ghi789.ubt-compile.pending.json"
    unpaired_pending.write_text(json.dumps({"sha_range": "ghi..jkl"}), encoding="utf-8")

    rc = _mod.main(["ubt-unresolved", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(unpaired_pending) in out
    assert str(paired_pending) not in out


def test_ubt_unresolved_cli_absent_dir_is_clean_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _mod.main(["ubt-unresolved", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
