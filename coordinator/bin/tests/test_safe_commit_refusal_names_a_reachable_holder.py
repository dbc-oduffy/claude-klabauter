"""The peer-claim refusal names a holder the caller can actually act on.

`_refuse_contested_pathspec` told the caller to "coordinate with the holder(s)
first" and identified them by `sid[:8]` -- not an address, and stale by
default: a session re-points its id while keeping its name (six re-points
across five of twelve peers in one shift, measured 2026-08-31). A guard whose
remediation nobody can execute teaches the fleet to route around it, which
costs its true positives too (this refusal exists because 40abe011d swept two
peers' hunks; it must survive being inconvenient).

The name is resolved from `harness_registry.snapshot()` at render time. A sid
carried in from a record or a document is the failure this pins against: that
exact mistake produced a three-hop misattribution the same day.

These tests pin the two facts that make the sid actionable -- the holder's
baton title and how long the TOUCH has gone unreleased -- and, separately,
that every part degrades to the bare sid rather than raising. The degrade
case is asserted on its own because a renderer that silently always returns
the bare sid would pass a shape-only test.

Run: python -m pytest coordinator/bin/tests/test_safe_commit_refusal_names_a_reachable_holder.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "safe_commit_holder_context_under_test", _BIN_DIR / "coordinator-safe-commit.py"
)
assert spec is not None and spec.loader is not None
safe_commit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = safe_commit
spec.loader.exec_module(safe_commit)

_SID = "d12e25cf-3a6b-4614-b269-ff941299e19e"


class _Rec:
    def __init__(self, name):
        self.name = name


def _patch_registry(monkeypatch, mapping):
    from coordinator_core.session import harness_registry

    monkeypatch.setattr(harness_registry, "snapshot", lambda: mapping)

def _session_dir(root: Path) -> Path:
    d = root / ".git" / "coordinator-sessions" / _SID
    d.mkdir(parents=True)
    return d


def test_names_the_holders_baton_title(tmp_path):
    d = _session_dir(tmp_path)
    (d / "baton.json").write_text(
        json.dumps({"title": "Execute the percolate contention flip"}), encoding="utf-8"
    )
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert "d12e25cf" in rendered
    assert "Execute the percolate contention flip" in rendered


def test_names_how_long_the_touch_has_gone_unreleased(tmp_path):
    d = _session_dir(tmp_path)
    (d / "touch-record.jsonl").write_text(
        json.dumps(
            {"v": 1, "verb": "T", "ts": time.time() - 4 * 3600, "sid": _SID,
             "path": "some/file.py"}
        )
        + "\n",
        encoding="utf-8",
    )
    assert "held 4.0h" in safe_commit._holder_context(
        str(tmp_path), _SID, "some/file.py"
    )


def test_a_minutes_old_claim_is_not_rendered_as_hours(tmp_path):
    """The age exists to separate live work from residue, so the two ends of
    that judgment must not render alike."""
    d = _session_dir(tmp_path)
    (d / "touch-record.jsonl").write_text(
        json.dumps(
            {"v": 1, "verb": "T", "ts": time.time() - 120, "sid": _SID,
             "path": "some/file.py"}
        )
        + "\n",
        encoding="utf-8",
    )
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert "held 2m" in rendered
    assert "h" not in rendered.split("held ")[1]


def test_a_touch_on_another_path_is_not_this_paths_age(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {})
    d = _session_dir(tmp_path)
    (d / "touch-record.jsonl").write_text(
        json.dumps(
            {"v": 1, "verb": "T", "ts": time.time() - 4 * 3600, "sid": _SID,
             "path": "other/file.py"}
        )
        + "\n",
        encoding="utf-8",
    )
    assert (
        safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
        == "d12e25cf (stale id, no registry entry)"
    )


def test_degrades_to_the_identifier_alone_rather_than_raising(tmp_path, monkeypatch):
    """No session dir at all: unreadable state must never turn a refusal
    into a crash."""
    _patch_registry(monkeypatch, {})
    assert (
        safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
        == "d12e25cf (stale id, no registry entry)"
    )


def test_a_corrupt_baton_does_not_lose_the_age(tmp_path):
    d = _session_dir(tmp_path)
    (d / "baton.json").write_text("{not json", encoding="utf-8")
    (d / "touch-record.jsonl").write_text(
        json.dumps(
            {"v": 1, "verb": "T", "ts": time.time() - 3600, "sid": _SID,
             "path": "some/file.py"}
        )
        + "\n",
        encoding="utf-8",
    )
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert rendered.startswith("d12e25cf") and "held 1.0h" in rendered


def test_resolves_the_sid_to_the_stable_name(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {_SID: _Rec("claude-klabauter-2d")})
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert rendered.startswith("claude-klabauter-2d [d12e25cf]")


def test_an_unregistered_sid_is_marked_stale_not_printed_as_an_address(
    tmp_path, monkeypatch
):
    """A re-pointed session is the common case, not the exceptional one, and
    printing its old id bare invites attribution on a stale identifier."""
    _patch_registry(monkeypatch, {})
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert "stale id" in rendered
    assert "claude-klabauter" not in rendered


def test_a_registry_failure_still_renders_the_holder(tmp_path, monkeypatch):
    from coordinator_core.session import harness_registry

    def _boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(harness_registry, "snapshot", _boom)
    assert "d12e25cf" in safe_commit._holder_context(
        str(tmp_path), _SID, "some/file.py"
    )
