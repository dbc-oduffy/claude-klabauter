"""Shared two-root fixture for the ``fleet.scratch_hygiene`` tests.

Everything lives under the caller's ``tmp_path``; ``tempfile.gettempdir`` is
monkeypatched to a child of it, so no real Temp or repo path is touched.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from coordinator_core.install.junction import JunctionUnsupported, create_junction
from coordinator_core.temp_layout import coordinator_temp_root

OLD_AGE_SECONDS = 10 * 24 * 3600
REPO_NAME = "fixture-repo"


@dataclass
class ScratchHygieneFixture:
    repo_root: Path
    scratch: Path
    temp_root: Path
    hold: Path
    registry_dir: Path
    outside: Path
    copy_dest: Path
    expected: dict[Path, str] = field(default_factory=dict)
    expected_reason: dict[Path, str] = field(default_factory=dict)
    nested_link: Path | None = None
    top_link: Path | None = None
    verify_copy: dict[str, str] = field(default_factory=dict)
    hold_readme: Path | None = None
    hold_missing: Path | None = None
    _handle: object = None

    def would_delete(self) -> set[Path]:
        return {p for p, a in self.expected.items() if a == "would-delete"}

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()


def age(path: Path, seconds: float = OLD_AGE_SECONDS) -> None:
    """Backdate ``path`` and everything under it without following links."""
    stamp = time.time() - seconds
    targets = [path]
    if path.is_dir() and not path.is_symlink():
        for dirpath, dirnames, filenames in os.walk(path):
            targets += [Path(dirpath) / n for n in dirnames + filenames]
    for t in targets:
        try:
            os.utime(t, (stamp, stamp), follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass


def _link(link: Path, target: Path) -> None:
    try:
        create_junction(link, target)
    except (OSError, JunctionUnsupported) as exc:
        pytest.skip(f"cannot create junction/symlink here: {exc}")


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return root


def build_two_root_fixture(tmp_path: Path, monkeypatch) -> ScratchHygieneFixture:
    """Build the exit-criterion fixture; the caller must ``close()`` it (it holds a file open)."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))
    repo = tmp_path / REPO_NAME
    scratch = repo / "scratch"
    hold = repo / "scratch-hold"
    temp_root = coordinator_temp_root(repo)
    registry = tmp_path / "registry"
    outside = tmp_path / "outside-target"
    copy_dest = tmp_path / "copy-dest"
    for d in (scratch, hold, temp_root, registry):
        d.mkdir(parents=True)
    _tree(outside, {"keep.txt": b"must survive", "sub/deep.bin": b"\x00\x01\x02"})

    fx = ScratchHygieneFixture(repo, scratch, temp_root, hold, registry, outside, copy_dest)
    expected = fx.expected

    plain = _tree(scratch / "old-plain", {"a.txt": b"aaaa", "d/b.txt": b"bb"})
    age(plain)
    expected[plain] = "would-delete"

    nested_parent = _tree(scratch / "old-with-nested-link", {"x.txt": b"x"})
    fx.nested_link = nested_parent / "nested-link"
    _link(fx.nested_link, outside)
    age(nested_parent)
    expected[nested_parent] = "would-delete"

    fx.top_link = scratch / "top-link"
    _link(fx.top_link, outside)
    expected[fx.top_link] = "skipped-link"

    young = _tree(scratch / "young-entry", {"fresh.txt": b"fresh"})
    expected[young] = "skipped-young"

    live = _tree(scratch / "live-cwd", {"w/work.txt": b"in use"})
    age(live)
    (registry / "live.json").write_text(
        json.dumps({"sessionId": "live-sid", "pid": os.getpid(), "cwd": str(live / "w"), "status": "busy"}),
        encoding="utf-8",
    )
    expected[live] = "skipped-live"
    fx.expected_reason[live] = "live-session-cwd:live-sid"

    held_open = _tree(scratch / "open-handle", {"log.txt": b"being written"})
    age(held_open)
    fx._handle = open(held_open / "log.txt", "r+b")
    expected[held_open] = "skipped-live"
    fx.expected_reason[held_open] = "open-handle"

    src = _tree(scratch / "copy-source", {"one.bin": b"12345678", "two.bin": b"abcd"})
    age(src)
    _tree(copy_dest, {"one.bin": b"12345678", "two.bin": b"ab"})
    fx.verify_copy = {"source": str(src), "dest": str(copy_dest)}
    expected[src] = "skipped-unverified"

    old_temp = _tree(temp_root / "old-probe", {"probe.log": b"log"})
    age(old_temp)
    expected[old_temp] = "would-delete"

    old_run = _tree(temp_root / "pytest" / "run-old", {"t/x.txt": b"x"})
    age(old_run)
    age(old_run.parent)
    expected[old_run] = "would-delete"

    ok = _tree(hold / "held-with-readme", {"data.bin": b"1234", "README": b"\nwhat | plan-x | after merge\n"})
    age(ok, 400 * 24 * 3600)
    fx.hold_readme = ok
    missing = _tree(hold / "held-no-readme", {"data.bin": b"123456"})
    age(missing, 400 * 24 * 3600)
    fx.hold_missing = missing
    return fx
