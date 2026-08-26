"""`ensure_meta` — the write path stops depending on who created the dir.

A session directory has no single owner: `init` is the only writer of
`meta.json`, but many bookkeeping writers create the directory and drop
their own file in it. `update_meta_field` returns False on an absent
`meta.json` by contract, so every field written that way silently no-opped
in such a session. `goal` is the field that made it visible -- `meta.json`
is its only source, so a claim in a record-less session rendered to peers as
`holder_goal_state: undeclared` (cross-repo/inbox/2026-08-20-doe-claude-em-
cmd-forwarder-eats-json-and-two-smaller-seams.md, item 3).

Negative-spec:
    - Does NOT assert that record-less directories stop being created. That
      is the constructor fix, sized as its own plan; these tests pin the
      write path's self-sufficiency, which is what the reported symptom was.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
        ["commit", "-q", "--allow-empty", "-m", "seed"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    _init_repo(r)
    core.reset_sessions_dir_cache()
    yield r
    core.reset_sessions_dir_cache()


@pytest.mark.spawns_process
@pytest.mark.cadence
class TestEnsureMeta:
    def test_creates_the_record_when_a_bookkeeping_writer_made_the_dir(self, repo):
        """The reported shape: the directory exists because something else
        made it, and no meta.json was ever written."""
        sid = "sess-recordless"
        sdir = Path(core.session_dir(sid, str(repo)))
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "touched.txt").touch()
        assert not (sdir / "meta.json").is_file()
        assert core.update_meta_field(str(sdir), "goal", "before") is False

        resolved = core.ensure_meta(sid, str(repo))

        assert Path(resolved) == sdir
        assert (sdir / "meta.json").is_file()
        assert core.update_meta_field(str(sdir), "goal", "after") is True
        assert json.loads((sdir / "meta.json").read_text(encoding="utf-8"))["goal"] == "after"

    def test_leaves_an_existing_record_untouched(self, repo):
        """Idempotent, and never a read-modify-write of a record that is
        already there -- a concurrent writer's fields must survive."""
        sid = "sess-existing"
        core.init(sid, cwd=str(repo))
        sdir = Path(core.session_dir(sid, str(repo)))
        core.update_meta_field(str(sdir), "goal", "peer-goal")
        before = (sdir / "meta.json").read_text(encoding="utf-8")

        core.ensure_meta(sid, str(repo))

        assert (sdir / "meta.json").read_text(encoding="utf-8") == before

    def test_returns_the_dir_even_when_the_record_cannot_be_created(self, repo, monkeypatch):
        """A failed create must not raise: the caller's existing
        no-op-and-warn branch stays reachable rather than becoming a new
        failure mode on a diagnostic field."""
        sid = "sess-init-fails"

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated init failure")

        monkeypatch.setattr(core, "init", _boom)

        resolved = core.ensure_meta(sid, str(repo))

        assert resolved == core.session_dir(sid, str(repo))
        assert not (Path(resolved) / "meta.json").is_file()

    def test_empty_when_there_is_no_sessions_dir(self, tmp_path):
        core.reset_sessions_dir_cache()
        assert core.ensure_meta("sess-x", str(tmp_path / "not-a-repo")) == ""
