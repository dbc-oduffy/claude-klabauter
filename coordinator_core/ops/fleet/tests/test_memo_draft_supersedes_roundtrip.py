
from __future__ import annotations

import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

import pytest

pytestmark = [pytest.mark.cadence]


def _script_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(
        os.path.join(here, "..", "..", "..", "..", "coordinator", "bin", "cross-repo-memo.py")
    )


def _load_dispatcher_module():
    bin_dir = os.path.dirname(_script_path())
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    loader = SourceFileLoader("cross_repo_memo_supersedes_rt", _script_path())
    spec = importlib.util.spec_from_loader("cross_repo_memo_supersedes_rt", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_dispatcher_module()


def test_supersedes_flag_present_on_draft_repeatable(mod):
    parser = mod._build_parser()
    ns = parser.parse_args(
        ["draft", "my-topic", "--to", "some-em", "--title", "t", "--kind", "fyi",
         "--supersedes", "2026-08-01-first.md"]
    )
    assert ns.supersedes == ["2026-08-01-first.md"]

    ns2 = parser.parse_args(
        ["draft", "my-topic", "--to", "some-em", "--title", "t", "--kind", "fyi",
         "--supersedes", "2026-08-01-first.md", "--supersedes", "2026-08-02-second.md"]
    )
    assert ns2.supersedes == ["2026-08-01-first.md", "2026-08-02-second.md"]

    ns3 = parser.parse_args(
        ["draft", "my-topic", "--to", "some-em", "--title", "t", "--kind", "fyi"]
    )
    assert ns3.supersedes is None


def test_supersedes_absent_from_send_subparser(mod):
    parser = mod._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["send", "my-topic", "--supersedes", "2026-08-01-first.md"])


def test_cmd_draft_threads_bare_string_for_single_supersedes(mod):
    ns = mod._build_parser().parse_args(
        ["draft", "my-topic", "--to", "some-em", "--title", "t", "--kind", "fyi",
         "--supersedes", "2026-08-01-first.md"]
    )
    assert mod._supersedes_invoke_value(ns.supersedes) == "2026-08-01-first.md"


def test_cmd_draft_threads_list_for_multiple_supersedes(mod):
    ns = mod._build_parser().parse_args(
        ["draft", "my-topic", "--to", "some-em", "--title", "t", "--kind", "fyi",
         "--supersedes", "2026-08-01-first.md", "--supersedes", "2026-08-02-second.md"]
    )
    assert mod._supersedes_invoke_value(ns.supersedes) == [
        "2026-08-01-first.md", "2026-08-02-second.md",
    ]


def test_supersedes_invoke_value_absent_returns_none(mod):
    ns = mod._build_parser().parse_args(
        ["draft", "my-topic", "--to", "some-em", "--title", "t", "--kind", "fyi"]
    )
    assert mod._supersedes_invoke_value(ns.supersedes) is None
