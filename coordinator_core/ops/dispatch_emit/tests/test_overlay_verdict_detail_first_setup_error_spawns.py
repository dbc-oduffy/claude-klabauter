"""The overlay red verdict names the first `ERROR at setup of` traceback head even when the output tail cuts it."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


@pytest.mark.spawns_process
@pytest.mark.complete
def test_red_verdict_detail_carries_setup_error_head_beyond_the_tail(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    monkeypatch.setattr(cov, "_OUTPUT_TAIL", 120)
    root, _ = _repo(tmp_path)
    src = (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef dep():\n    raise RuntimeError('setup-cause-marker')\n\n\n"
        "def test_a(dep):\n    assert True\n"
        + "".join(f"\n\ndef test_pad_{i}():\n    assert True\n" for i in range(40))
    )
    _write(root, "t/test_setup_red.py", src)
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    head = _git(root, "rev-parse", "HEAD").strip()

    out = cov.verify(root, "run1", head, ["t/test_setup_red.py"])

    assert out.verdict == "red", out
    assert "first setup ERROR: " in out.detail
    assert "setup-cause-marker" in out.detail
