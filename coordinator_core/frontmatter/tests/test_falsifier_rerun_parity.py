"""Parity oracle: scan_plans() over the live docs/plans corpus must equal a
full-parse reference built in the same run from the module's private helpers.
"""

from __future__ import annotations

import re
from collections import defaultdict

from coordinator_core.frontmatter import falsifier_rerun_report as frr
from coordinator_core.frontmatter.schema_validate import parse_frontmatter

_TIME_LINE = re.compile(r"^process_time_seconds=.*$", re.MULTILINE)


def _strip_time(rendered: str) -> str:
    return _TIME_LINE.sub("", rendered)


def _reference() -> frr.FalsifierRerunReport:
    scanned = 0
    with_falsifier = 0
    terminal: list[frr.PlanFalsifierRecord] = []
    key_to_files: dict[str, list[str]] = defaultdict(list)

    for path in sorted(frr._DEFAULT_PLANS_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        scanned += 1

        frontmatter = parse_frontmatter(text).get("frontmatter")
        if not isinstance(frontmatter, dict):
            continue

        try:
            repo_rel = str(path.relative_to(frr._REPO_ROOT))
        except ValueError:
            repo_rel = str(path)

        for key in frr._close_out_adhoc_keys(frontmatter):
            key_to_files[key].append(repo_rel)

        if frr._falsifier_dict(frontmatter) is None:
            continue
        with_falsifier += 1

        status = frontmatter.get("status")
        status = status if isinstance(status, str) else ""
        if status not in frr.TERMINAL_STATUSES:
            continue
        recorded, verdict = frr._rerun_recorded(frontmatter)
        terminal.append(
            frr.PlanFalsifierRecord(
                repo_rel_path=repo_rel,
                status=status,
                rerun_recorded=recorded,
                falsifier_verdict=verdict,
            )
        )

    attempts = tuple(
        frr.CloseOutKeyAttempt(key=k, count=len(f), repo_rel_paths=tuple(f))
        for k, f in sorted(key_to_files.items())
    )
    return frr.FalsifierRerunReport(
        scanned_files=scanned,
        with_falsifier=with_falsifier,
        terminal_with_falsifier=tuple(terminal),
        close_out_key_attempts=attempts,
        process_time_seconds=0.0,
    )


def test_scan_plans_matches_full_parse_reference_on_live_corpus() -> None:
    ref = _reference()
    got = frr.scan_plans()
    assert got.scanned_files == ref.scanned_files
    assert got.with_falsifier == ref.with_falsifier
    assert got.terminal_with_falsifier == ref.terminal_with_falsifier
    assert got.close_out_key_attempts == ref.close_out_key_attempts
    assert _strip_time(got.render()) == _strip_time(ref.render())
