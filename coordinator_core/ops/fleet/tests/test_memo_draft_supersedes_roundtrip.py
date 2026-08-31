"""
test_memo_draft_supersedes_roundtrip.py — --supersedes on the draft subparser.

Spec backlink: state/dispatch-briefs/2026-08-31-the-memo-channel-s-surviving-three/C1.md

Purpose: `coordinator/bin/cross-repo-memo.py`'s `draft` subparser accepts
`--supersedes MEMO` repeatably (`action="append"`) so a single occurrence
threads the bare string claude-klabauter's `memo.draft` op already validates, and
multiple occurrences thread a list — both shapes `_validate_supersedes_param`
(coordinator_core/ops/fleet/memo_send.py, imported by memo_draft.py) already
accepts. This file verifies two things without a real engine invocation:

  1. Argparse wiring: `--supersedes` parses on `draft` (repeatable,
     default None) and is ABSENT from `send` (see the module's own comment:
     `send` reads the value off staged frontmatter, not a flag).
  2. Threading: `_supersedes_invoke_value` — the function `_cmd_draft` calls
     to reduce argparse's accumulated `--supersedes` list to the op's invoke
     shape — returns a bare string for one occurrence, a list for two-plus,
     and None when the flag is absent.

Review: overengineering-reviewer (2026-08-31) — this file previously also
pinned a round-trip through `_parse_outbox_file`'s nested-`supersedes:`
YAML-sequence reader; that reader had no CLI consumer (both call sites of
`_parse_outbox_file`'s return value read only `to` and `scoped_to_*`) and
was deleted from `cross-repo-memo.py`, so those two tests were dropped with
it rather than left pinning dead code.

Negative-spec: does NOT invoke the real `memo.draft` op or spawn a
subprocess — `_cmd_draft`'s `invoke_params` threading is a pure function of
argparse.Namespace, so this test exercises it directly. No `--supersedes`
value is threaded onto `send_p` — asserting its absence there is part of AC1.
"""

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
    """Import the extensionless cross-repo-memo script as a module.

    Mirrors coordinator/bin/test_cross_repo_memo_draft.py::_load_dispatcher_module
    — loaded under a name other than __main__ so its `if __name__ ==
    "__main__"` guard does not fire.
    """
    # cross-repo-memo.py's own `import lib` bootstrap (coordinator/bin/lib/
    # __init__.py) relies on the CLI's own directory being sys.path[0] — true
    # for a normal script invocation, not for this importlib-by-path load.
    # Mirrors coordinator/bin/test_cross_repo_memo_draft.py's own sys.path
    # insertion (there scoped to coordinator/bin/lib itself, for the same
    # reason) — insert the CLI's directory so `import lib` resolves.
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
    # Review: overengineering-reviewer — rewritten to call the extracted
    # _supersedes_invoke_value helper (the actual threading logic _cmd_draft
    # calls) instead of transcribing its one-liner into the test body, so
    # this test can fail for a change to the real threading path.
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
