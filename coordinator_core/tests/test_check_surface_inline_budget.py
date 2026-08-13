"""Tests for coordinator_core.ops.check_surface_inline_budget.

Covers each of the four inline-mechanism signals independently, the
summed budget-comparison contract (WARN over-budget / OK within-budget /
INFO no-baseline / ERROR no-file-matched), and glob-based multi-file
surfaces.

Spec backlink: canonical-resolution-engine plan, chunk W1-A3a (2026-07-24)
  — AC-11 anti-rebound inline-mechanism budget gate.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.check_surface_inline_budget import (
    check,
    count_inline_mechanism_signals,
    main,
)

_CLEAN_SURFACE = """# Fake converted skill (test fixture)

This surface links to a named entrypoint instead of narrating a procedure.

Run `"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/some-op"`.

That's it.
"""

_OVER_BUDGET_SURFACE = """# Fake reverted skill (test fixture)

Some preamble text.

```bash
_cc_trusted=0
echo "inline block one"
```

1. Stage the change: `git add -- foo && git commit -m "x"`
2. Then push: `git push`

More narrative referencing `git log && git status` inline.

```sh
echo "inline block two"
```

End of fixture.
"""


def test_count_inline_mechanism_signals_clean_surface_all_zero() -> None:
    signals = count_inline_mechanism_signals(_CLEAN_SURFACE)
    assert signals == {
        "bash_fence": 0,
        "cc_trusted": 0,
        "narrated_step": 0,
        "inline_payload": 0,
        "total": 0,
    }


def test_count_inline_mechanism_signals_bash_fence() -> None:
    signals = count_inline_mechanism_signals(_OVER_BUDGET_SURFACE)
    assert signals["bash_fence"] == 2  # ```bash and ```sh


def test_count_inline_mechanism_signals_cc_trusted_anchored() -> None:
    # Whole-line anchor: `_cc_trusted=0` on its own line counts once; a
    # compound line carrying the same substring does not match (documented
    # blind spot, mirrors the coordinator-claude ancestor's own anchoring rationale).
    text = "_cc_trusted=0\n_cc_trusted=0  # reset\n"
    signals = count_inline_mechanism_signals(text)
    assert signals["cc_trusted"] == 1


def test_count_inline_mechanism_signals_narrated_step() -> None:
    signals = count_inline_mechanism_signals(_OVER_BUDGET_SURFACE)
    assert signals["narrated_step"] == 2  # the two numbered steps with inline code


def test_count_inline_mechanism_signals_inline_payload() -> None:
    signals = count_inline_mechanism_signals(_OVER_BUDGET_SURFACE)
    # The narrated-step line's own inline span (`git add -- foo && git
    # commit -m "x"`) plus the standalone `git log && git status` mention.
    assert signals["inline_payload"] == 2


def test_check_warns_when_over_budget(tmp_path: Path) -> None:
    surface = tmp_path / "SKILL.md"
    surface.write_text(_OVER_BUDGET_SURFACE, encoding="utf-8")
    baseline = tmp_path / "baseline"
    baseline.write_text("0\n", encoding="utf-8")

    message, rc = check(str(surface), str(baseline))

    assert rc == 1
    assert "WARN:" in message
    assert "exceeds baseline" in message


def test_check_passes_clean_fixture(tmp_path: Path) -> None:
    surface = tmp_path / "SKILL.md"
    surface.write_text(_CLEAN_SURFACE, encoding="utf-8")
    baseline = tmp_path / "baseline"
    baseline.write_text("0\n", encoding="utf-8")

    message, rc = check(str(surface), str(baseline))

    assert rc == 0
    assert "OK:" in message
    assert "within baseline" in message


def test_check_no_baseline_returns_info(tmp_path: Path) -> None:
    surface = tmp_path / "SKILL.md"
    surface.write_text(_OVER_BUDGET_SURFACE, encoding="utf-8")
    baseline = tmp_path / "baseline-nonexistent"

    message, rc = check(str(surface), str(baseline))

    assert rc == 0
    assert "no baseline set yet" in message


def test_check_no_file_matched_returns_error(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline-nonexistent"
    message, rc = check(str(tmp_path / "*.does-not-exist"), str(baseline))

    assert rc == 2
    assert "ERROR" in message


def test_check_glob_sums_across_files(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(_OVER_BUDGET_SURFACE, encoding="utf-8")
    (tmp_path / "b.md").write_text(_OVER_BUDGET_SURFACE, encoding="utf-8")
    baseline = tmp_path / "baseline"
    over_budget_total = count_inline_mechanism_signals(_OVER_BUDGET_SURFACE)["total"]
    baseline.write_text(str(over_budget_total * 2), encoding="utf-8")

    message, rc = check(str(tmp_path / "*.md"), str(baseline))

    assert rc == 0
    assert "OK:" in message


def test_main_prints_and_returns_rc(tmp_path: Path, capsys) -> None:
    surface = tmp_path / "SKILL.md"
    surface.write_text(_OVER_BUDGET_SURFACE, encoding="utf-8")
    baseline = tmp_path / "baseline"
    baseline.write_text("0\n", encoding="utf-8")

    rc = main([str(surface), str(baseline)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "WARN:" in captured.out


def test_main_missing_argv_returns_2(capsys) -> None:
    rc = main([])
    assert rc == 2
    captured = capsys.readouterr()
    assert "missing required" in captured.err
