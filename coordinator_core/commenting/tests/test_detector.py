import time

from coordinator_core.commenting import scan_text


def test_changelog_dated_detected():
    text = "# Fixed the retry bug on 2026-09-01"
    assert scan_text(text, "engine/foo.py")


def test_authored_by_detected():
    text = "// added by the Game Dev Reviewer, per the PM"
    assert scan_text(text, "engine/foo.ts")


def test_task_ref_minted_id_detected():
    text = "# Implements pln-commenting-standard-enforced-9c86ed"
    assert scan_text(text, "engine/foo.py")


def test_task_ref_chunk_detected():
    text = "// Chunk C5 wires the detector"
    assert scan_text(text, "engine/foo.ts")


def test_narration_detected():
    text = "# used to retry twice, no longer does"
    assert scan_text(text, "engine/foo.py")


def test_grep_bait_token_detected():
    text = "# GUARD-NOT-A-HOOK-ENTRYPOINT"
    assert scan_text(text, "engine/foo.py")


def test_true_negative_break_class_doctrine():
    text = "# Break-class defects are fixed by default, not deferred."
    assert scan_text(text, "engine/foo.py") == []


def test_true_negative_callers_assume_utc():
    text = "# callers assume UTC"
    assert scan_text(text, "engine/foo.py") == []


def test_true_negative_lowercase_kebab_token():
    text = "# guard-not-a-hook-entrypoint"
    assert scan_text(text, "engine/foo.py") == []


def test_true_negative_noqa():
    text = "# noqa: E402"
    assert scan_text(text, "engine/foo.py") == []


def test_true_negative_iso_date_format_hint():
    text = "# expects YYYY-MM-DD, e.g. 2026-09-01"
    assert scan_text(text, "engine/foo.py") == []


def test_true_negative_non_reference_task_n():
    text = "# Task 3 of the pipeline tuple"
    assert scan_text(text, "engine/foo.py") == []


def test_true_negative_docs_plans_path_exempt():
    text = "# Fixed the retry bug on 2026-09-01"
    assert scan_text(text, "docs/plans/2026-09-26-some-plan.md") == []


def test_crlf_input_scanned_same_as_lf():
    text = "line one\r\n# added by the Game Dev Reviewer, per the PM\r\nline three"
    assert scan_text(text, "engine/foo.py")


def test_windows_separator_exempt_path():
    text = "# Fixed the retry bug on 2026-09-01"
    assert scan_text(text, "state\\handoffs\\2026-09-25-note.md") == []


def test_process_time_bound_under_50ms_for_1000_line_fixture():
    lines = []
    for i in range(1000):
        lines.append(f"# invariant: callers assume UTC for index {i}")
    fixture = "\n".join(lines)
    start = time.process_time()
    scan_text(fixture, "engine/big_module.py")
    elapsed = time.process_time() - start
    assert elapsed < 0.05
