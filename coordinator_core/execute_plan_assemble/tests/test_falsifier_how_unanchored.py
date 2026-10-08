"""`falsifier_how_unanchored` flags a bare-word substring decision and stays
silent on anything anchored or undecidable."""
from __future__ import annotations

import pytest

from coordinator_core.execute_plan_assemble.falsifier_shape import falsifier_how_unanchored


@pytest.mark.parametrize(
    "how",
    [
        "python audit.py | grep -c prior",
        "python audit.py | grep prior",
        "grep -n 'prior' out.txt",
        'python -c "import sys; print(any(\'prior\' in line for line in sys.stdin))"',
        "[l for l in lines if 'prior' in line.lower()]",
    ],
)
def test_unanchored_forms_flag(how):
    assert falsifier_how_unanchored(how) == "prior"


@pytest.mark.parametrize(
    "how",
    [
        "python audit.py | grep -x prior",
        "python audit.py | grep -w prior",
        "python audit.py | grep -cw prior",
        "python audit.py | grep -c '^prior:'",
        "python audit.py | grep -c 'prior:'",
        "python audit.py | grep -c '\bprior\b'",
        "python audit.py | grep -E 'a|b'",
        "python audit.py | grep -A 3 context",
        "'prior' in line.split()",
        "line == 'prior'",
        "pytest tests/test_x.py -q",
    ],
)
def test_anchored_or_undecidable_forms_stay_silent(how):
    assert falsifier_how_unanchored(how) is None


def test_non_string_is_silent():
    assert falsifier_how_unanchored(None) is None
