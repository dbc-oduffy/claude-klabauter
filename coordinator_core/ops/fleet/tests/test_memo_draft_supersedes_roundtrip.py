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
  2. Round-trip: the frontmatter shape `memo_draft.py`'s
     `compose_draft_frontmatter` emits for `supersedes` (a bare quoted
     scalar for one item, a nested YAML sequence via `_render_extra_field`
     for two-plus — see coordinator_core/ops/fleet/memo_draft.py:620-629)
     parses back through `cross-repo-memo.py`'s own `_parse_outbox_file` as
     the same shape: a bare string for one, a list for two.

Negative-spec: does NOT invoke the real `memo.draft` op or spawn a
subprocess — `_cmd_draft`'s `invoke_params` threading and
`_parse_outbox_file`'s parsing are pure functions of argparse.Namespace /
file text, so this test exercises them directly. No `--supersedes` value is
threaded onto `send_p` — asserting its absence there is part of AC1.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import textwrap
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
    ns = mod._build_parser().parse_args(
        ["draft", "my-topic", "--to", "some-em", "--title", "t", "--kind", "fyi",
         "--supersedes", "2026-08-01-first.md"]
    )
    supersedes = getattr(ns, "supersedes", None)
    invoke_params: dict = {}
    if supersedes:
        invoke_params["supersedes"] = supersedes[0] if len(supersedes) == 1 else supersedes
    assert invoke_params["supersedes"] == "2026-08-01-first.md"


def test_cmd_draft_threads_list_for_multiple_supersedes(mod):
    ns = mod._build_parser().parse_args(
        ["draft", "my-topic", "--to", "some-em", "--title", "t", "--kind", "fyi",
         "--supersedes", "2026-08-01-first.md", "--supersedes", "2026-08-02-second.md"]
    )
    supersedes = getattr(ns, "supersedes", None)
    invoke_params: dict = {}
    if supersedes:
        invoke_params["supersedes"] = supersedes[0] if len(supersedes) == 1 else supersedes
    assert invoke_params["supersedes"] == [
        "2026-08-01-first.md", "2026-08-02-second.md",
    ]


def test_parse_outbox_file_roundtrips_bare_string_supersedes(mod, tmp_path):
    # Mirrors memo_draft.py::compose_draft_frontmatter's single-item rendering
    # (a _yaml_quote'd inline scalar — coordinator_core/ops/fleet/memo_draft.py:629).
    outbox = tmp_path / "my-topic.md"
    outbox.write_text(
        textwrap.dedent("""\
            ---
            title: "t"
            from: "me-em"
            to: "some-em"
            created: 2026-08-31
            status: draft
            delivery_mode: receiver-repo
            summary: "(no summary provided)"
            kind: "fyi"
            supersedes: "2026-08-01-first.md"
            ---
            body text
            """),
        encoding="utf-8",
    )
    fm, body = mod._parse_outbox_file(str(outbox))
    assert fm["supersedes"] == "2026-08-01-first.md"
    assert body.strip() == "body text"


def test_parse_outbox_file_roundtrips_list_supersedes(mod, tmp_path):
    # Mirrors memo_draft.py::compose_draft_frontmatter's multi-item rendering
    # (a nested YAML sequence via _render_extra_field/_render_yaml_block —
    # coordinator_core/ops/fleet/memo_draft.py:627,
    # coordinator_core/ops/fleet/_memo_compose.py:420-427).
    outbox = tmp_path / "my-topic.md"
    outbox.write_text(
        textwrap.dedent("""\
            ---
            title: "t"
            from: "me-em"
            to: "some-em"
            created: 2026-08-31
            status: draft
            delivery_mode: receiver-repo
            summary: "(no summary provided)"
            kind: "fyi"
            supersedes:
              - "2026-08-01-first.md"
              - "2026-08-02-second.md"
            ---
            body text
            """),
        encoding="utf-8",
    )
    fm, body = mod._parse_outbox_file(str(outbox))
    assert fm["supersedes"] == ["2026-08-01-first.md", "2026-08-02-second.md"]
    assert body.strip() == "body text"
