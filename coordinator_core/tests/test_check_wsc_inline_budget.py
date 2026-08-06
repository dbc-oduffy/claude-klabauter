"""Characterization tests for coordinator_core.ops.check_wsc_inline_budget.

Ported from the bash oracle's four-case harness
(Port of: test-inline-budget-check.sh, example-doctrine-repo 432e3285, 2026-07-22) plus a
fatal-path case (SKILL.md not found) and a missing-argv case not covered by
the bash harness (bash argv there is always well-formed via env-var
defaulting; this module's argv contract is stricter).

Spec backlink: wsc-asic task (2026-06-30)
Port of: check-wsc-inline-budget.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.check_wsc_inline_budget import check, count_bash_fences, main

_FAKE_SKILL = """# Fake workstream-complete SKILL (test fixture)

Some preamble text.

```bash
echo "inline block one"
```

Narrative text between blocks.

```bash
echo "inline block two"
```

More narrative.

```bash
echo "inline block three"
```

End of fixture.
"""


def _write_skill(tmp_path: Path) -> Path:
    p = tmp_path / "SKILL.md"
    p.write_text(_FAKE_SKILL, encoding="utf-8")
    return p


def test_count_bash_fences_counts_three() -> None:
    assert count_bash_fences(_FAKE_SKILL) == 3


def test_count_bash_fences_zero_on_no_matches() -> None:
    assert count_bash_fences("# no bash blocks here\n```python\nprint(1)\n```\n") == 0


def test_case_a_no_baseline_file_exits_0(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    baseline = tmp_path / "baseline-nonexistent"
    message, rc = check(str(skill), str(baseline))
    assert rc == 0
    assert "no baseline set yet" in message
    assert "3" in message


def test_case_b_baseline_lower_than_count_exits_1_warn(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    baseline = tmp_path / "baseline-b"
    baseline.write_text("1\n", encoding="utf-8")
    message, rc = check(str(skill), str(baseline))
    assert rc == 1
    assert "WARN:" in message
    assert "exceeds baseline" in message


def test_case_c_baseline_equal_to_count_exits_0_ok(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    baseline = tmp_path / "baseline-c"
    baseline.write_text("3\n", encoding="utf-8")
    message, rc = check(str(skill), str(baseline))
    assert rc == 0
    assert "OK:" in message
    assert "within baseline" in message


def test_case_d_baseline_higher_than_count_exits_0_ok(tmp_path: Path) -> None:
    skill = _write_skill(tmp_path)
    baseline = tmp_path / "baseline-d"
    baseline.write_text("8\n", encoding="utf-8")
    message, rc = check(str(skill), str(baseline))
    assert rc == 0
    assert "OK:" in message
    assert "within baseline" in message


def test_skill_not_found_exits_2(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"
    baseline = tmp_path / "baseline-nonexistent"
    message, rc = check(str(missing), str(baseline))
    assert rc == 2
    assert "ERROR" in message
    assert "not found" in message


def test_zero_blocks_no_baseline(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# no bash blocks\n```python\nprint(1)\n```\n", encoding="utf-8")
    baseline = tmp_path / "baseline-nonexistent"
    message, rc = check(str(skill), str(baseline))
    assert rc == 0
    assert "no baseline set yet" in message
    assert "0" in message


def test_main_missing_argv_returns_2(capsys) -> None:
    rc = main([])
    assert rc == 2
    captured = capsys.readouterr()
    assert "missing required" in captured.err


def test_main_prints_and_returns_rc(tmp_path: Path, capsys) -> None:
    skill = _write_skill(tmp_path)
    baseline = tmp_path / "baseline-b"
    baseline.write_text("1\n", encoding="utf-8")
    rc = main([str(skill), str(baseline)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "WARN:" in captured.out
