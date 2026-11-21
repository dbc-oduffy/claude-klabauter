"""The overlay red verdict names the first `ERROR at setup of` traceback head even when the output tail cuts it."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401

_OUTPUT = """\
==================================== ERRORS ====================================
_____________ ERROR at setup of test_alpha _____________

request = <FixtureRequest for <Function test_alpha>>

    def one(request):
>       return request.getfixturevalue("absent_dep")
E       fixture 'absent_dep' not found

t/test_x.py:5: FixtureLookupError
_____________ ERROR at setup of test_beta _____________
E       second error
=========================== short test summary info ============================
ERROR t/test_x.py::test_alpha - fixture 'absent_dep' not found
"""


def test_first_setup_error_names_header_and_cause_of_the_first_only():
    head = cov.first_setup_error(_OUTPUT)

    assert head.startswith("first setup ERROR: ")
    assert "ERROR at setup of test_alpha" in head
    assert "fixture 'absent_dep' not found" in head
    assert "second error" not in head


def test_first_collection_error_head_carries_the_import_cause():
    out = (
        "_ ERROR collecting t/test_land_run.py __\r\nImportError while importing test module\r\nTraceback:\r\n"
        "t/test_land_run.py:11: in <module>\r\nE   ImportError: cannot import name 'x' from 'y'\r\n"
        "_ ERROR collecting t/test_other.py __\r\nE   other\r\n"
    )

    head = cov.first_setup_error(out)

    assert "ERROR collecting t/test_land_run.py" in head and "cannot import name 'x'" in head
    assert "other" not in head


def test_no_setup_error_yields_empty():
    assert cov.first_setup_error("FAILED t/test_x.py::test_a - assert 0\n1 failed in 0.1s\n") == ""
