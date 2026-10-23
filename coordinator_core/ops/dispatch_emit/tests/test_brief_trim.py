"""brief_trim: constants, section accounting, raw-dump detection, pinned pre-trim brief bytes."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import brief_trim
from coordinator_core.ops.dispatch_emit.ask_stage import PM_INTENT_HEADING, stage
from coordinator_core.ops.dispatch_emit.ask_contract import RELAYED_ASK_HEADING
from coordinator_core.ops.dispatch_emit.executor_economy import EXCERPT_HEADING

FIXTURE_PLAN = Path(__file__).parent / "fixtures" / "brief_trim" / "plan.md"
PLAN_REL = "docs/plans/2026-10-01-brief-trim-fixture.md"
# row prefix -> file count; each file carries <prefix>_<n> (named by the row body) and an unnamed def.
FIXTURE_SOURCES = {"a": 2, "b": 5, "c": 8}


def write_fixture_sources(root: Path) -> None:
    """Deterministic sources under root/pkg so span rendering has real top-level defs to find."""
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    for prefix, count in FIXTURE_SOURCES.items():
        for n in range(1, count + 1):
            src = (
                f"def {'alpha' if prefix == 'a' else 'beta' if prefix == 'b' else 'gamma'}_{n}(x):\n"
                f"    return x + {n}\n\n\n"
                f"def unrelated_{prefix}{n}(y):\n"
                f"    return y * {n}\n"
            )
            (root / "pkg" / f"{prefix}{n}.py").write_text(src, encoding="utf-8", newline="\n")


def stage_fixture(root: Path):
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "state" / "sizings").mkdir(parents=True)
    (root / PLAN_REL).write_text(FIXTURE_PLAN.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    write_fixture_sources(root)
    return stage(root, run_id="r1", plan_rel=PLAN_REL)


def measure_executor_briefs(root: Path) -> tuple[int, dict[str, int]]:
    """Summed UTF-8 bytes of every executor brief and the per-section split summed across them."""
    m = stage_fixture(root)
    total = 0
    split: dict[str, int] = {}
    for mr in m.rows:
        text = (root / mr.brief_path).read_text(encoding="utf-8")
        total += len(text.encode("utf-8"))
        for k, v in brief_trim.section_bytes(text).items():
            split[k] = split.get(k, 0) + v
    return total, split


def test_constants_pinned():
    assert brief_trim.PM_INTENT_HEADING_TEXT == "## PM intent (verbatim)" == PM_INTENT_HEADING
    assert brief_trim.FILES_BLOCK_HEADING == "## Files you may write"
    assert brief_trim.PROTECTED_HEADINGS == (
        "## PM intent (verbatim)",
        RELAYED_ASK_HEADING,
        EXCERPT_HEADING,
        "## Files you may write",
    )
    assert brief_trim.RAW_DUMP_MAX_FENCE_BYTES == 2000


def test_section_bytes_two_sections_with_preface():
    text = "pre\n## A\nxy\n## B\nz\n"
    got = brief_trim.section_bytes(text)
    assert got == {"": 4, "## A": len("## A\nxy\n"), "## B": len("## B\nz\n")}
    assert sum(got.values()) == len(text.encode("utf-8"))


def test_section_bytes_ignores_heading_inside_fence():
    text = "## A\n```\n## not a heading\n```\n"
    assert list(brief_trim.section_bytes(text)) == ["## A"]


def _fence(n: int) -> str:
    return "```\n" + "x" * n + "\n```\n"


def test_raw_dump_flags_2001_and_passes_1999():
    assert len(brief_trim.raw_dump_findings(_fence(2001))) == 1
    assert brief_trim.raw_dump_findings(_fence(1999)) == []
    assert brief_trim.raw_dump_findings(_fence(2000)) == []


def test_raw_dump_one_finding_per_block():
    assert len(brief_trim.raw_dump_findings(_fence(2500) + "\ntext\n" + _fence(3000) + _fence(10))) == 2


def test_module_imports_nothing_else_from_ops():
    tree = ast.parse(Path(brief_trim.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            if node.module == "coordinator_core.ops.dispatch_emit":
                mods |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
    ops = {m for m in mods if m.startswith("coordinator_core")}
    assert ops <= {
        "coordinator_core.ops.dispatch_emit",
        "coordinator_core.ops.dispatch_emit.ask_contract",
        "coordinator_core.ops.dispatch_emit.executor_economy",
    }


def test_fixture_stages_three_rows_over_real_sources(tmp_path):
    m = stage_fixture(tmp_path)
    assert sorted(r.id for r in m.rows) == ["C1", "C2", "C3"]
    assert len(list((tmp_path / "pkg").glob("*.py"))) == 15
    assert all(len(re.findall(r"^def ", p.read_text(encoding="utf-8"), re.M)) == 2 for p in (tmp_path / "pkg").glob("*.py"))


def test_pre_trim_brief_bytes_baseline_is_pinned(tmp_path):
    total, split = measure_executor_briefs(tmp_path)
    assert total == brief_trim.BRIEF_BYTES_BASELINE, (total, split)
