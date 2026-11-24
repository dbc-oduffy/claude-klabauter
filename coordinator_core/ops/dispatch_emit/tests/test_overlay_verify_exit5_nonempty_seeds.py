"""The e78426 shape: seeds holding real tests that the land tier markers deselect. The overlay must classify that
as the tier exclusion it is, and a pytest exit 5 that stays empty over non-empty seeds is a named engine fault, never an
inconclusive landing refusal."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete, pytest.mark.cadence]

_TIERED = "import pytest\n\npytestmark = pytest.mark.{tier}\n\n\ndef test_real_{n}():\n    assert True\n"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _commit(root, files):
    for rel, src in files.items():
        _write(root, rel, src)
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    return _git(root, "rev-parse", "HEAD").strip()


def test_tier_excluded_seeds_are_the_tier_green_not_an_inconclusive_refusal(tmp_path):
    root, _ = _repo(tmp_path)
    seeds = ["t/test_own_run.py", "t/test_guard.py"]
    head = _commit(root, {
        seeds[0]: _TIERED.format(tier="cadence", n=1),
        seeds[1]: _TIERED.format(tier="complete", n=2),
    })

    out = cov.verify(root, "run1", head, seeds)

    assert out.verdict == "green", out
    assert "excluded tier" in out.detail, out


def test_a_seed_that_collects_nothing_is_a_named_engine_fault_not_a_refusal(tmp_path):
    root, _ = _repo(tmp_path)
    seeds = ["t/test_empty_a.py", "t/test_empty_b.py"]
    head = _commit(root, {s: "def helper():\n    return 1\n" for s in seeds})

    out = cov.verify(root, "run1", head, seeds)

    assert out.verdict == cov.ENV_DEFECT and not out.red, out
    assert "engine fault" in out.detail and "collected zero tests" in out.detail, out
    assert out.verdict != "inconclusive"


def test_an_empty_seed_set_keeps_the_inconclusive_verdict(tmp_path):
    root, head = _repo(tmp_path)

    out = cov.verify(root, "run1", head, [])

    assert out.verdict == "inconclusive" and "no test seeds" in out.detail, out
