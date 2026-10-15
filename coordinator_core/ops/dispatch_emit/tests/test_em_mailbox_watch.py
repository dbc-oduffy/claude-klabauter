"""The EM mailbox watch is a one-shot background command, not a Monitor tail."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.op import _em_mailbox
from coordinator_core.workflow_watch.mailbox_line import wait_for_line


def test_reply_names_no_monitor_and_no_tail(tmp_path):
    reply = _em_mailbox(tmp_path, "scratch")
    for key in ("em_watch", "em_rewatch"):
        text = reply[key]
        assert "Monitor(" not in text and "persistent" not in text
        assert not text.startswith("tail") and "-F" not in text
        assert "workflow_watch.mailbox_line" in text


def test_default_start_is_the_end_and_from_start_replays(tmp_path):
    box = tmp_path / "_em.jsonl"
    box.write_text("old\n", encoding="utf-8")

    def append():
        time.sleep(0.2)
        with box.open("a", encoding="utf-8") as fh:
            fh.write("new\n")

    t = threading.Thread(target=append)
    t.start()
    assert wait_for_line(str(box), poll_seconds=0.05) == "new"
    t.join()
    assert wait_for_line(str(box), from_start=True, poll_seconds=0.05) == "old" + chr(10) + "new"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_process_prints_the_line_and_exits_zero(tmp_path):
    box = tmp_path / "_em.jsonl"
    box.write_text("escalation\n", encoding="utf-8")
    root = Path(__file__).resolve().parents[4]
    proc = subprocess.Popen(
        [sys.executable, "-m", "coordinator_core.workflow_watch.mailbox_line", "--from-start", str(box)],
        cwd=root, stdout=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0 and out.strip() == "escalation"
