"""The memo-rows leaf loads none of spine_read's heavy modules; spine_read re-exports its names."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import memo_rows, spine_read
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MOVED = (
    "_is_memo_send_row",
    "memo_row_slug",
    "memo_receipt_exists",
    "memo_receipt_slug",
    "inbound_close_path",
    "_is_receiver_inbox_surface",
    "_RECEIVER_INBOX_SEGMENT",
    "memo_row_dispatchable",
    "memo_send_step_block",
)

_ROW = {"kind": "memo-send", "receipt": "slug-a", "memo_to": "coordinator-content-repo-em"}


def test_prose_mention_does_not_classify():
    assert not memo_rows._is_memo_send_row({"body": "then run cross-repo-memo send x or memo.send"})
    assert not hasattr(memo_rows, "_brief_names_memo_send")
    assert not hasattr(memo_rows, "_MEMO_SEND_BRIEF_RE")


def test_dispatchable_base_row():
    assert memo_rows.memo_row_dispatchable(dict(_ROW))
    assert memo_rows.memo_row_dispatchable({**_ROW, "disposition": "open"})


@pytest.mark.parametrize(
    "patch",
    [
        {"kind": "script-edit"},
        {"disposition": "coded"},
        {"deferred": True},
        {"deferred_until": "2027-01-01"},
        {"performer": "em"},
        {"memo_to": ""},
        {"memo_to": None},
        {"receipt": None},
        {"surface": "state/cross-repo/inbox/foo.md"},
    ],
)
def test_not_dispatchable_branches(patch):
    row = {**_ROW, **patch}
    if patch.get("receipt", 1) is None:
        del row["receipt"]
    assert not memo_rows.memo_row_dispatchable(row)


def test_step_block_names_draft_send_and_receipt():
    from coordinator_core.session.machinery_paths import MEMO_OUTBOX_RELDIR

    block = memo_rows.memo_send_step_block(
        "C9", {"slug": "slug-a", "to": "coordinator-content-repo-em", "title": "A title", "kind": "note"}
    )
    assert "cross-repo-memo draft slug-a --to coordinator-content-repo-em --kind note --title 'A title'" in block
    assert "cross-repo-memo send slug-a" in block
    assert f"{MEMO_OUTBOX_RELDIR}/sent/slug-a.md" in block
    assert "REFUSED" in block and "git commit" in block


@pytest.mark.parametrize("name", _MOVED)
def test_spine_read_reexports_leaf_object(name):
    assert getattr(spine_read, name) is getattr(memo_rows, name)


def test_leaf_import_set_excludes_spine_read_and_heavy_modules():
    code = (
        "import sys\n"
        "import coordinator_core.ops.dispatch_emit.memo_rows\n"
        "bad = [m for m in ('yaml', 'coordinator_core.ops.dispatch_emit.spine_read',"
        " 'coordinator_core.frontmatter.schema_validate', 'coordinator_core.ops.plan_tasks_render')"
        " if m in sys.modules]\n"
        "print('LOADED=' + ','.join(bad))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "LOADED="
