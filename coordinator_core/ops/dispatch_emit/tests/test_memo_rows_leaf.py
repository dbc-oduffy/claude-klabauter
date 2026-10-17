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
    "_MEMO_SEND_BRIEF_RE",
    "_RECEIVER_INBOX_SEGMENT",
)


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
