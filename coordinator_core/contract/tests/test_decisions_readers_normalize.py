"""Pins that every decisions-file reader in coordinator_core reads entries through the shared normalization.

A new reader that takes a raw `disposition` key off a decisions entry fails here, not in a later run.
Left alone on purpose: `pickup_assemble/apply.py` `jp.get("disposition")` reads a persisted
judgment-point record, not a decisions entry.

Limit of `test_no_raw_disposition_read_on_a_decisions_entry`: its pattern needs a
subscript or `.get(...)` on a `decision*` name before the `disposition` read, so it
does not see `<decision_var>.get("disposition")` on a local bound earlier (the
baton_assemble shape). The per-module normalizer-reference test is the backstop there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.workday_complete.apply import _closed_by_resolved_jp

_ROOT = Path(__file__).resolve().parents[2]

_NORMALIZER_NAMES = ("normalize_decisions", "chosen_disposition", "validate_decisions_shape")

_READER_MODULES = (
    "contract/apply_base.py",
    "ceremony_common/apply_halt.py",
    "ceremony_common/run/envelope.py",
    "ceremony_common/run/decide.py",
    "baton_assemble/__init__.py",
    "workstream_complete/__init__.py",
    "workstream_complete/apply.py",
    "workday_complete/apply.py",
    "pickup_brief.py",
    "pickup_assemble/__init__.py",
    "pickup_assemble/apply.py",
)

_RAW_DECISIONS_READ = re.compile(
    r"decision\w*(?:\[[^\]]*\]|\.get\([^)]*\))\s*"
    r"(?:\.get\(\s*[\"']disposition[\"']|\[\s*[\"']disposition[\"']\s*\])"
)


def _code_lines(path: Path):
    in_doc = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        quotes = stripped.count('"""') + stripped.count("'''")
        if in_doc or quotes:
            if quotes % 2 == 1:
                in_doc = not in_doc
            continue
        if stripped.startswith("#"):
            continue
        yield lineno, line


@pytest.mark.parametrize("rel", _READER_MODULES)
def test_reader_module_references_a_normalizer(rel):
    text = "\n".join(line for _, line in _code_lines(_ROOT / rel))
    assert any(name in text for name in _NORMALIZER_NAMES), (
        f"{rel} reads decisions entries but references none of {_NORMALIZER_NAMES}"
    )


def test_no_raw_disposition_read_on_a_decisions_entry():
    offenders = []
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT).as_posix()
        if "/tests/" in f"/{rel}" or Path(rel).name.startswith("test_"):
            continue
        for lineno, line in _code_lines(path):
            if _RAW_DECISIONS_READ.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "raw `disposition` read on a decisions entry; route it through "
        "apply_base.normalize_decisions or apply_halt.chosen_disposition:\n" + "\n".join(offenders)
    )


_JP = {"j1": {"id": "j1"}}
_DIRECTIVE = {"id": "d1", "depends_on": ["j1"]}


@pytest.mark.parametrize("entry", [{"disposition": "skip"}, {"value": "skip"}])
def test_workday_closed_by_resolved_jp_accepts_disposition_and_value(entry):
    assert _closed_by_resolved_jp(_DIRECTIVE, _JP, {"j1": entry}) is True


@pytest.mark.parametrize("decisions", [{}, {"j1": {}}, {"j1": {"note": "x"}}])
def test_workday_closed_by_resolved_jp_refuses_unanswered_point(decisions):
    assert _closed_by_resolved_jp(_DIRECTIVE, _JP, decisions) is False
