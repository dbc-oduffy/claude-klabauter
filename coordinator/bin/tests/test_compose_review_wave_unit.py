"""In-process unit tests for `coordinator/bin/compose-review-wave.py`.

The subprocess/integration half of the suite needs the DoE sandbox policy,
role snippets and catering-resolution schema, so it stays with those files.
Everything here drives the module in-process with `subprocess.run` faked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = Path(__file__).resolve().parents[1] / "compose-review-wave.py"


def _load_composer_module():
    spec = importlib.util.spec_from_file_location("_crw_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_l_hung_attribution_child_degrades_to_not_measurable(monkeypatch) -> None:
    """A covering test that HANGS must not block the review wave.

    The crash path was already covered; a hang is the other half and reaches the
    same requirement by a different route -- an unbounded wait would leave the
    reviewer's whole payload unbuilt rather than degraded. Pins the timeout kwarg
    too: without it subprocess.run never raises TimeoutExpired, so a regression
    that drops the ceiling would silently restore the hang.
    """
    mod = _load_composer_module()

    seen = {}

    def _fake_run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    result = mod._run_waste_attribution(["coordinator/bin/waste-signal.py"], _REPO_ROOT)

    assert result["status"] == "not-measurable"
    assert "exceeded" in (result.get("reason") or "")
    assert seen.get("timeout"), "the attribution child must be spawned with a timeout ceiling"
    assert seen.get("env", {}).get("HOME") not in (None, os.environ.get("HOME")), (
        "the attribution child must run with a scratch HOME, not the operator's"
    )


def test_m_static_section_survives_the_attribution_only_parse_filter(monkeypatch) -> None:
    """`_run_waste_attribution` reads only `parsed.get("attribution")` and builds
    `rendered` from that dict alone -- `waste-signal.py` nests the static
    (duplicate-block) section UNDER `attribution["static"]`, never as a sibling
    top-level key, precisely so it survives this filter. This pins that survival
    behaviourally: a static section with a `missing_index` hint reaches the
    caller-visible dict this composer builds, unmodified.
    """
    mod = _load_composer_module()

    static_hint = (
        "this is NOT a 'no duplicates found' result; nothing was checked -- the "
        "vector store for this scope is absent or the query raised"
    )
    fake_static_section = {
        "status": "not-measurable",
        "reason": "missing-index",
        "hint": static_hint,
        "paths": {},
    }
    fake_stdout = json.dumps(
        {
            "changed_paths": ["coordinator/bin/waste-signal.py"],
            "resolved_tests": ["coordinator/tests/test_waste_signal.py"],
            "report": None,
            "attribution": {
                "status": "not-measurable",
                "reason": "no covering test resolved by naming convention for any changed path",
                "attributable_redundant_opens": 0,
                "attributable_paths": [],
                "elsewhere_in_repo_redundant_opens": 0,
                "out_of_repo_redundant_opens": 0,
                "basis": None,
                "static": fake_static_section,
            },
        }
    )

    calls = []

    class _Proc:
        returncode = 0
        stdout = fake_stdout
        stderr = ""

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    rendered = mod._run_waste_attribution(["coordinator/bin/waste-signal.py"], _REPO_ROOT)

    assert len(calls) == 1, (
        "the static signal is computed inside waste-signal.py's existing child -- "
        "this composer must spawn no additional subprocess for it"
    )
    assert "static" in rendered, "the static section was silently dropped by the parse filter"
    assert rendered["static"] == fake_static_section
    # Byte-for-byte: example-retrieval-repo's own hint text, never paraphrased or summarised
    # on the way from the child's stdout to the value this composer returns.
    assert rendered["static"]["hint"] == static_hint


_PM_BRIEF_FIXTURE_TEXT = (
    "---\ntitle: fixture plan for C10\n---\n\n"
    "## PM brief\n\n> a distinctive sentinel line, quokka-42\n"
)

_NO_BRIEF_FIXTURE_TEXT = "---\ntitle: fixture plan with no brief\n---\n\n# No brief here\n"


def test_k_reviewer_plan_brief_block_unit(tmp_path: Path) -> None:
    """Direct unit coverage of `_reviewer_plan_brief_block`, no subprocess."""
    mod = _load_composer_module()

    assert mod._reviewer_plan_brief_block({}, _REPO_ROOT) == mod._NO_PLAN_BRIEF_LINE
    assert mod._reviewer_plan_brief_block({"plans": []}, _REPO_ROOT) == mod._NO_PLAN_BRIEF_LINE

    plan_path = tmp_path / "unit-fixture-plan.md"
    plan_path.write_text(_PM_BRIEF_FIXTURE_TEXT, encoding="utf-8")
    block = mod._reviewer_plan_brief_block({"plans": [str(plan_path)]}, _REPO_ROOT)
    assert "quokka-42" in block
    assert "## PM intent (verbatim)" in block

    no_brief_path = tmp_path / "unit-fixture-no-brief.md"
    no_brief_path.write_text(_NO_BRIEF_FIXTURE_TEXT, encoding="utf-8")
    with pytest.raises(mod.ComposeError):
        mod._reviewer_plan_brief_block({"plans": [str(no_brief_path)]}, _REPO_ROOT)
