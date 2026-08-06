"""test_stitch_observer_sidecar.py — regression suite for
stitch-observer-sidecar.py, the /workday-complete Step 4d helper that folds
the strategic observer's transient sidecar into the day's daily summary.

Covers the leak the inline bash Step 4d body (`cat $OBSERVER_SIDECAR >>
$DAILY_SUMMARY; rm $OBSERVER_SIDECAR`) could produce with no existence check,
no idempotency, and no failure surface: three backfilled days (2026-07-19..21)
were found on disk 2026-07-23 with an orphaned `*.observer.md` sidecar and
zero `## Strategic Review` heading in the main summary. Also covers the
standalone `--scan` sweep, which detects that same shape of leak independently
of any single ceremony invocation.

Spec backlink: coordinator/bin/stitch-observer-sidecar.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "stitch-observer-sidecar.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("stitch_observer_sidecar", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def test_sidecar_absent_is_a_noop(mod, tmp_path):
    main = tmp_path / "2026-01-01-testmachine.md"
    main.write_text("# Daily Summary\n\nsome content\n", encoding="utf-8")
    sidecar = tmp_path / "2026-01-01-testmachine.observer.md"

    rc = mod.stitch(str(main), str(sidecar))

    assert rc == 0
    assert main.read_text(encoding="utf-8") == "# Daily Summary\n\nsome content\n"
    assert not sidecar.exists()


def test_happy_path_stitches_and_deletes_sidecar(mod, tmp_path):
    main = tmp_path / "2026-01-01-testmachine.md"
    main.write_text("# Daily Summary\n\nsome content\n", encoding="utf-8")
    sidecar = tmp_path / "2026-01-01-testmachine.observer.md"
    sidecar.write_text("## Strategic Review (Sonnet daily observer)\n\nfindings here\n", encoding="utf-8")

    rc = mod.stitch(str(main), str(sidecar))

    assert rc == 0
    assert not sidecar.exists()
    stitched = main.read_text(encoding="utf-8")
    assert stitched.count("## Strategic Review") == 1
    assert "findings here" in stitched
    # Verbatim: the sidecar's own content is untouched substring of the result.
    assert "## Strategic Review (Sonnet daily observer)\n\nfindings here\n" in stitched


def test_already_stitched_is_idempotent_and_removes_redundant_sidecar(mod, tmp_path):
    main = tmp_path / "2026-01-01-testmachine.md"
    main.write_text(
        "# Daily Summary\n\nsome content\n\n"
        "## Strategic Review (Sonnet daily observer)\n\nalready here\n",
        encoding="utf-8",
    )
    original = main.read_text(encoding="utf-8")
    sidecar = tmp_path / "2026-01-01-testmachine.observer.md"
    sidecar.write_text("## Strategic Review (Sonnet daily observer)\n\nduplicate copy\n", encoding="utf-8")

    rc = mod.stitch(str(main), str(sidecar))

    assert rc == 0
    assert not sidecar.exists()
    # Main summary content is untouched — no second heading appended.
    assert main.read_text(encoding="utf-8") == original
    assert main.read_text(encoding="utf-8").count("## Strategic Review") == 1


def test_missing_main_summary_fails_loud_and_preserves_sidecar(mod, tmp_path):
    main = tmp_path / "2026-01-01-testmachine.md"  # deliberately never created
    sidecar = tmp_path / "2026-01-01-testmachine.observer.md"
    sidecar.write_text("## Strategic Review (Sonnet daily observer)\n\nfindings here\n", encoding="utf-8")

    rc = mod.stitch(str(main), str(sidecar))

    assert rc == 1
    assert not main.exists()
    # The leak condition: sidecar must survive so the content isn't lost.
    assert sidecar.exists()
    assert sidecar.read_text(encoding="utf-8") == "## Strategic Review (Sonnet daily observer)\n\nfindings here\n"


def test_scan_clean_directory_reports_nothing_and_exits_zero(mod, tmp_path):
    (tmp_path / "2026-01-01-testmachine.md").write_text("# Daily Summary\n", encoding="utf-8")

    assert mod.scan(str(tmp_path)) == 0


def test_scan_finds_never_stitched_orphan(mod, tmp_path):
    (tmp_path / "2026-01-01-testmachine.md").write_text("# Daily Summary\n\nno review here\n", encoding="utf-8")
    (tmp_path / "2026-01-01-testmachine.observer.md").write_text(
        "## Strategic Review (Sonnet daily observer)\n\nstranded findings\n", encoding="utf-8",
    )

    assert mod.scan(str(tmp_path)) == 1


def test_scan_finds_already_stitched_uncleaned_sidecar(mod, tmp_path):
    (tmp_path / "2026-01-01-testmachine.md").write_text(
        "# Daily Summary\n\n## Strategic Review (Sonnet daily observer)\n\nalready folded in\n",
        encoding="utf-8",
    )
    (tmp_path / "2026-01-01-testmachine.observer.md").write_text(
        "## Strategic Review (Sonnet daily observer)\n\nduplicate copy\n", encoding="utf-8",
    )

    assert mod.scan(str(tmp_path)) == 1


def test_scan_finds_sidecar_with_no_main_summary(mod, tmp_path):
    (tmp_path / "2026-01-01-testmachine.observer.md").write_text(
        "## Strategic Review (Sonnet daily observer)\n\nfindings here\n", encoding="utf-8",
    )

    assert mod.scan(str(tmp_path)) == 1


def test_scan_nonexistent_directory_is_a_usage_error(mod, tmp_path):
    assert mod.scan(str(tmp_path / "does-not-exist")) == 2
