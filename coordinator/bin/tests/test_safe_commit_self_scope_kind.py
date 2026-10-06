"""The banner's own-set reads the self-write projection: a read-only hold leaves it."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

from coordinator_core.session import core as cs_core
from coordinator_core.session.touch_record import VERB_TOUCH, append_event

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent
_SRC = _BIN_DIR / "coordinator-safe-commit.py"


def _load():
    loader = importlib.machinery.SourceFileLoader("coordinator_safe_commit", str(_SRC))
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _put(sink, path, kind, ts):
    append_event(sink, session_id="s1", agent_id=None, verb=VERB_TOUCH, path=path,
                 kind=kind, timestamp=ts, name=None)


def test_banner_own_set_drops_read_hold_keeps_write_hold(tmp_path, monkeypatch):
    sink = tmp_path / "s1" / "touch-record.jsonl"
    _put(sink, "read_only.py", "r", 1.0)
    _put(sink, "written.py", "w", 2.0)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "s1")
    monkeypatch.setattr(cs_core, "sessions_dir", lambda: str(tmp_path))
    own, reason = _load()._own_touched_paths_for_banner()
    assert reason == "ok"
    assert own == {"written.py"}


def test_blanket_and_scoped_read_the_projection_not_the_legacy_sink():
    src = _SRC.read_text(encoding="utf-8")
    assert 'project_self_scope(' not in src
    assert src.count("project_self_write_scope(") >= 3
    assert '"touched.txt")\n            if os.path.isfile(touched_path):\n                try:\n                    own_lines' not in src
