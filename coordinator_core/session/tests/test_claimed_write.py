"""
coordinator_core.session.tests.test_claimed_write — AC1/AC2/AC7 for the
claiming seam (`coordinator_core.session.claimed_write`, C1,
docs/plans/2026-09-11-state-writers-claim-through-one-seam.md).

Covers, per entry point (`replace_text`, `replace_bytes`, `create_exclusive`,
`append_claimed_line`): a success case declares exactly the path written; a
failure case declares nothing and re-raises; `create_exclusive`'s collision
cases; outside any collection nothing is declared and no session directory
appears; cross-repo containment; Windows `os.replace`-on-open-handle
behaviour; and mode preservation on replace.

Real fixture repos + `session.core.init`/`cli_entry.recording_declared_writes`
are used for the collection/containment tests (not monkeypatched declare
paths) so these exercise the real collection -> recorder -> touch-record
chain, matching AC1/AC7's "each claim assertion is watched failing with the
declare_write call removed" bar.

Spec backlink: docs/plans/2026-09-11-state-writers-claim-through-one-seam.md § C1/AC1/AC2/AC7
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import cli_entry
from coordinator_core.session import claimed_write, core
from coordinator_core.session.declared_writes import active_declarations, collecting
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SESSION_ENV_VAR = "COORDINATOR_SESSION_ID"


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **no_console_creationflags())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, **no_console_creationflags()
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, **no_console_creationflags())
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, **no_console_creationflags())
    return repo


def _touch_record_text(session_dir) -> str:
    from coordinator_core.session import scope

    sink = Path(session_dir) / scope._TOUCH_RECORD_FILENAME
    lines, _degraded = scope._read_touch_record_as_legacy_lines(sink)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AC1: success declares exactly the path written
# ---------------------------------------------------------------------------


def test_replace_text_declares_exactly_the_written_path(tmp_path):
    target = tmp_path / "a.txt"
    with collecting() as declared:
        result = claimed_write.replace_text(target, "hello")
    assert result == target
    assert target.read_text() == "hello"
    assert declared == [str(target)]


def test_replace_bytes_declares_exactly_the_written_path(tmp_path):
    target = tmp_path / "b.bin"
    with collecting() as declared:
        result = claimed_write.replace_bytes(target, b"\x00\x01")
    assert result == target
    assert target.read_bytes() == b"\x00\x01"
    assert declared == [str(target)]


def test_append_claimed_line_declares_the_path(tmp_path):
    target = tmp_path / "log.jsonl"
    with collecting() as declared:
        result = claimed_write.append_claimed_line(target, b'{"a":1}\n')
    assert result == target
    assert target.read_bytes() == b'{"a":1}\n'
    assert declared == [str(target)]


def test_create_exclusive_declares_the_path_on_first_create(tmp_path):
    target = tmp_path / "excl.txt"
    with collecting() as declared:
        result = claimed_write.create_exclusive(target, "one")
    assert result == target
    assert target.read_text() == "one"
    assert declared == [str(target)]


def test_create_exclusive_retry_suffix_declares_the_suffixed_path(tmp_path):
    target = tmp_path / "excl.txt"
    target.write_text("existing")
    with collecting() as declared:
        result = claimed_write.create_exclusive(target, "new", retry_suffix=True)
    expected = tmp_path / "excl-2.txt"
    assert result == expected
    assert expected.read_text() == "new"
    assert declared == [str(expected)], "must claim the path ACTUALLY written, never the requested one"


# ---------------------------------------------------------------------------
# AC1: failure declares nothing and re-raises
# ---------------------------------------------------------------------------


def test_replace_text_failure_declares_nothing_and_reraises(tmp_path, monkeypatch):
    target = tmp_path / "c.txt"

    def _boom(*_a, **_kw):
        raise OSError("os.replace failure injected")

    monkeypatch.setattr(claimed_write, "atomic_write_bytes", _boom)
    with collecting() as declared:
        with pytest.raises(OSError, match="injected"):
            claimed_write.replace_text(target, "nope")
    assert declared == []
    assert not target.exists()


def test_replace_bytes_failure_declares_nothing_and_reraises(tmp_path, monkeypatch):
    target = tmp_path / "d.bin"

    def _boom(*_a, **_kw):
        raise OSError("os.replace failure injected")

    monkeypatch.setattr(claimed_write, "atomic_write_bytes", _boom)
    with collecting() as declared:
        with pytest.raises(OSError, match="injected"):
            claimed_write.replace_bytes(target, b"x")
    assert declared == []


def test_append_claimed_line_failure_declares_nothing_and_reraises(tmp_path, monkeypatch):
    target = tmp_path / "e.jsonl"

    def _boom(*_a, **_kw):
        raise OSError("append failure injected")

    monkeypatch.setattr(claimed_write, "append_line", _boom)
    with collecting() as declared:
        with pytest.raises(OSError, match="injected"):
            claimed_write.append_claimed_line(target, b"x\n")
    assert declared == []


def test_create_exclusive_unwritable_directory_declares_nothing_and_reraises(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"

    def _boom(*_a, **_kw):
        raise PermissionError("unwritable directory")

    monkeypatch.setattr(os, "open", _boom)
    with collecting() as declared:
        with pytest.raises(PermissionError):
            claimed_write.create_exclusive(target, "x")
    assert declared == []


def test_create_exclusive_collision_without_retry_declares_nothing(tmp_path):
    target = tmp_path / "g.txt"
    target.write_text("existing")
    with collecting() as declared:
        with pytest.raises(FileExistsError):
            claimed_write.create_exclusive(target, "new", retry_suffix=False)
    assert declared == []
    assert target.read_text() == "existing", "no bytes were written on the collision path"


# ---------------------------------------------------------------------------
# AC1/AC7: outside any collection, every entry point writes and declares
# nothing, and no session directory is created.
# ---------------------------------------------------------------------------


def test_all_four_entry_points_write_but_declare_nothing_outside_a_collection(tmp_path):
    assert active_declarations() is None
    replaced = tmp_path / "outside-replace.txt"
    appended = tmp_path / "outside-append.jsonl"
    created = tmp_path / "outside-create.txt"

    claimed_write.replace_text(replaced, "x")
    claimed_write.append_claimed_line(appended, b"y\n")
    claimed_write.create_exclusive(created, "z")

    assert replaced.read_text() == "x"
    assert appended.read_bytes() == b"y\n"
    assert created.read_text() == "z"
    assert active_declarations() is None


def test_no_session_directory_is_created_outside_a_collection(tmp_path):
    repo = _make_repo(tmp_path)
    session_root = repo / ".git" / "coordinator-sessions"
    assert not session_root.exists()

    claimed_write.replace_text(repo / "state" / "outside.md", "x")

    assert not session_root.exists(), (
        "a write outside any collection must never mint a session directory "
        "(declare_write is a no-op there per declared_writes.py's negative spec)"
    )


# ---------------------------------------------------------------------------
# AC7: containment -- a write inside a collection whose resolved caller repo
# differs from the target's repo records nothing.
# ---------------------------------------------------------------------------


def test_containment_refuses_a_claim_for_a_path_outside_the_callers_repo(tmp_path, monkeypatch):
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    caller_repo = _make_repo(tmp_path, "caller-repo")
    other_repo = _make_repo(tmp_path, "other-repo")
    sid = "sid-c1-containment"
    core.init(sid, cwd=str(caller_repo))
    monkeypatch.setenv(_SESSION_ENV_VAR, sid)

    outside_target = other_repo / "state" / "foreign.md"
    with cli_entry.recording_declared_writes(cwd=str(caller_repo)):
        claimed_write.replace_text(outside_target, "leaked")

    assert outside_target.read_text() == "leaked", "the write itself still lands"
    content = _touch_record_text(core.session_dir(sid, cwd=str(caller_repo)))
    assert "foreign.md" not in content, (
        "a declared path outside the caller's own resolved repo must not be recorded"
    )


def test_a_write_inside_the_callers_own_repo_is_recorded(tmp_path, monkeypatch):
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    repo = _make_repo(tmp_path)
    sid = "sid-c1-inrepo"
    core.init(sid, cwd=str(repo))
    monkeypatch.setenv(_SESSION_ENV_VAR, sid)

    target = repo / "state" / "mine.md"
    with cli_entry.recording_declared_writes(cwd=str(repo)):
        claimed_write.replace_text(target, "mine")

    content = _touch_record_text(core.session_dir(sid, cwd=str(repo)))
    assert "mine.md" in content


# ---------------------------------------------------------------------------
# D1 Windows note + mode preservation
# ---------------------------------------------------------------------------


def test_replace_over_a_target_another_handle_holds_open_for_read(tmp_path):
    """Assert the ACTUAL platform behaviour (D1): os.replace fails on Windows
    when another handle holds the target open for reading without
    FILE_SHARE_DELETE; POSIX replace succeeds regardless of open readers."""
    target = tmp_path / "held-open.txt"
    target.write_text("before")
    handle = open(target, "rb")
    try:
        if os.name == "nt":
            with pytest.raises(OSError):
                with collecting():
                    claimed_write.replace_text(target, "after")
        else:
            with collecting() as declared:
                claimed_write.replace_text(target, "after")
            assert declared == [str(target)]
            assert target.read_text() == "after"
    finally:
        handle.close()


def test_replace_preserves_an_existing_targets_mode(tmp_path):
    target = tmp_path / "modeful.txt"
    target.write_text("v1")
    os.chmod(target, 0o644)
    with collecting():
        claimed_write.replace_text(target, "v2")
    assert (os.stat(target).st_mode & 0o777) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX mkstemp mode is not meaningful on Windows")
def test_replace_of_a_new_target_is_created_at_mkstemp_mode(tmp_path):
    """Negative spec (D1): a target that does not yet exist is created at
    mkstemp's mode (0600 on POSIX), not the parent directory's default."""
    target = tmp_path / "brand-new.txt"
    with collecting():
        claimed_write.replace_text(target, "v1")
    assert (os.stat(target).st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# AC2: one recorder, one replace primitive
# ---------------------------------------------------------------------------


def test_claimed_write_module_calls_no_recorder_directly():
    import inspect

    src = inspect.getsource(claimed_write)
    import re as _re

    assert _re.search(r"_record_self_reported_touches|touch_written_path|scope\.touch\(", src) is None


def test_claimed_write_module_has_no_replace_primitive_copy():
    import inspect
    import re as _re

    src = inspect.getsource(claimed_write)
    assert _re.search(r"mkstemp|os\.replace|os\.fdopen", src) is None


def test_install_shared_no_longer_defines_atomic_write_bytes():
    import re as _re

    from coordinator_core.install import _shared

    src = inspect_source = __import__("inspect").getsource(_shared)
    assert _re.search(r"^def atomic_write_bytes", src, _re.M) is None


def test_install_shared_reexports_the_same_object():
    from coordinator_core import atomic_replace
    from coordinator_core.install import _shared

    assert _shared.atomic_write_bytes is atomic_replace.atomic_write_bytes


# ---------------------------------------------------------------------------
# Seam import weight (C1 body: no coordinator_core.install pulled in)
# ---------------------------------------------------------------------------


def test_importing_the_seam_does_not_pull_in_install(tmp_path):
    repo_root = Path(__file__).resolve().parents[3]
    script = tmp_path / "check_importtime.py"
    script.write_text(
        "import coordinator_core.session.claimed_write\n", encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    result = subprocess.run(
        [sys.executable, "-X", "importtime", str(script)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=env,
        check=True,
        **no_console_creationflags(),
    )
    assert "coordinator_core.install" not in result.stderr
