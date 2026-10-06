"""`baton-assemble apply handoff` is the door the rendered entry-brief apply line
runs. The hook that renders that line cannot know the title, so the engine is
the backstop: the CLI refuses an untitled handoff before any directive fires.
"""

from __future__ import annotations

import pytest

import coordinator_core.baton_assemble.apply as ba_apply

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def apply_spy(monkeypatch):
    calls: list[tuple] = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return ba_apply.APPLY_EXIT_OK, {}

    monkeypatch.setattr(ba_apply, "apply", _spy)
    return calls


@pytest.mark.parametrize(
    "title_argv",
    [[], ["--title", ""], ["--title", "   "], ["--title", "PLACEHOLDER title"]],
)
def test_untitled_handoff_is_refused_before_apply_runs(apply_spy, capsys, title_argv):
    rc = ba_apply.main_apply(["handoff", *title_argv])

    assert rc == ba_apply.APPLY_EXIT_TRANSPORT_FAIL
    assert apply_spy == []
    assert '--title "<one line naming the work>"' in capsys.readouterr().err


def test_titled_handoff_reaches_apply(apply_spy):
    ba_apply.main_apply(["handoff", "--title", "Wire the thing"])

    assert len(apply_spy) == 1
    assert apply_spy[0][1]["title"] == "Wire the thing"
