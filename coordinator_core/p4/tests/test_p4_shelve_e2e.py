"""
test_p4_shelve_e2e.py -- C4's positive-leg end-to-end coverage.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
§ C4, § falsifier.

The falsifier's positive leg, made permanent: a throwaway p4d 2026.1, a git
repo co-located in the client root with `vcs_mirror: p4`, a submitted seed of
a.txt/b.txt/c.txt/keep.txt. The test edits a and b, deletes c, commits via
`ceremony.commit_v2`, and runs `push.outstanding` against a local bare-repo
remote (never the network).

AUTH NOTE: a from-scratch p4d 2026.1 (this box's build) enforces
authentication from its very first connection -- `p4 -Ztag info` reports
`password enabled` before any user or protections table exists, and neither
an anonymous `configure set security=0` nor an unauthenticated write is
accepted (measured against this box's p4d; the falsifier's own
"security=0" phrasing describes the fixture's INTENT -- a server nobody
needs a real credential to drive -- not a literal startup flag this build
still honors). This fixture instead bootstraps ONE throwaway password for
the one throwaway user (`bob`), logs in once per test to a scratch
`P4TICKETS` file, and exports that file on the environment for the whole
test via `monkeypatch.setenv` -- `coordinator_core.p4.runner.run`'s
`subprocess.Popen` inherits the parent environment (it sets no `env=`
kwarg of its own), so every real p4 spawn made by the code under test rides
the same cached ticket the fixture logged in, with no code-under-test
change and no ambient-env reliance beyond this one test-only ticket file.
It then reads the server with `p4 -ztag changes -s shelved -c <client>` and
`p4 -ztag describe -S -s <CL>` and asserts exactly one shelved CL whose
paths equal the pushed git range (c.txt shelved as a delete).

Also covers: resume (a second commit+push reuses the same CL rather than
minting a second one) and a closed-CL re-mint (the session's CL submitted
out from under it by a human/cockpit -- the next push must not shelve into a
CL p4 has already closed).

Skipped entirely when `p4d`/`p4` are not on PATH -- this is the one test in
this plan's surface that talks to a real Perforce server, and CI/dev boxes
without the binary must not fail on its absence.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_P4D_MISSING = shutil.which("p4d") is None or shutil.which("p4") is None
pytestmark.append(
    pytest.mark.skipif(_P4D_MISSING, reason="p4d/p4 not on PATH -- e2e leg skipped")
)

_SEED_FILES = {"a.txt": "a-seed\n", "b.txt": "b-seed\n", "c.txt": "c-seed\n", "keep.txt": "keep\n"}
_NO_WINDOW = no_console_creationflags()


def _run(args, cwd=None, check=True, timeout=30):
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        **_NO_WINDOW,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ThrowawayP4d:
    """Stands up a throwaway `p4d` on a free localhost port, in a scratch
    server root, and tears it down on exit. Never touches any real p4
    server -- the port is freshly bound and the root is a fresh tmp_path."""

    def __init__(self, server_root: Path):
        self.server_root = server_root
        self.port = _free_port()
        self.p4port = f"localhost:{self.port}"
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        self.server_root.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            ["p4d", "-p", self.p4port, "-r", str(self.server_root), "-L", "log"],
            cwd=str(self.server_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_NO_WINDOW,
        )
        deadline = time.time() + 20.0
        last_error = None
        while time.time() < deadline:
            try:
                _run(["p4", "-p", self.p4port, "info"], timeout=5)
                return
            except Exception as exc:  # noqa: BLE001 -- retry until deadline
                last_error = exc
                time.sleep(0.25)
        raise RuntimeError(f"p4d never became reachable on {self.p4port}: {last_error!r}")

    def login(self, monkeypatch, user: str, tickets_path: Path, password: str = "Passw0rd1!") -> None:
        """Bootstraps ONE throwaway password for `user`, logs in once, and
        points `P4TICKETS` at a scratch file for the rest of THIS test --
        see the module docstring's AUTH NOTE for why this is needed at all
        against this box's p4d build."""
        monkeypatch.setenv("P4TICKETS", str(tickets_path))
        proc = subprocess.run(
            ["p4", "-p", self.p4port, "-u", user, "passwd"],
            input=f"{password}\n{password}\n",
            capture_output=True,
            text=True,
            timeout=15,
            **_NO_WINDOW,
        )
        assert proc.returncode == 0, proc.stderr
        proc = subprocess.run(
            ["p4", "-p", self.p4port, "-u", user, "login"],
            input=f"{password}\n",
            capture_output=True,
            text=True,
            timeout=15,
            **_NO_WINDOW,
        )
        assert proc.returncode == 0, proc.stderr

    def stop(self) -> None:
        if self._proc is None:
            return
        # `admin stop` needs `super` access, which this fixture's throwaway
        # user is never granted (no protections table is ever authored) --
        # a direct process kill is the correct teardown for a scratch
        # server nothing else on the box can be talking to.
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self._proc.kill()
            self._proc.wait(timeout=5)


@pytest.fixture
def p4d(tmp_path, monkeypatch):
    server = _ThrowawayP4d(tmp_path / "p4d-root")
    server.start()
    server.login(monkeypatch, "bob", tmp_path / "p4tickets")
    try:
        yield server
    finally:
        server.stop()


def _p4(p4d_fixture: _ThrowawayP4d, user: str, client: str, args, cwd=None, **kw):
    # `-d <dir>` names p4's own working directory explicitly rather than
    # relying on the spawned child's OS-level cwd -- this box's `p4.exe`
    # resolves relative paths against an inherited `$PWD`-style value when
    # run from this shell, not the `subprocess.Popen(cwd=...)` argument,
    # which silently resolved every relative seed path against the test
    # PROCESS's own cwd instead of the throwaway client root.
    prefix = ["p4", "-p", p4d_fixture.p4port]
    if cwd is not None:
        prefix += ["-d", str(cwd)]
    return _run([*prefix, "-u", user, "-c", client, *args], **kw)


def _git(args, cwd):
    _run(["git", *args], cwd=cwd)


def _make_writable(path: Path) -> None:
    """`p4 submit` leaves a synced/added file read-only (`noallwrite`, this
    fixture's own client option, matching D5's read-only invariant) --
    production flips this via the D5 checkout-before-edit guard's own
    `p4 edit` spawn before a git worktree write ever lands. This fixture
    exercises the D4 shelve leg in isolation from D5's own guard (a
    separate row, separately tested), so it stands in for "the guard
    already ran" with a direct chmod rather than pulling the guard itself
    into this test's scope."""
    path.chmod(0o644)


def _init_client_and_seed(p4d_fixture: _ThrowawayP4d, client_root: Path, client: str, user: str):
    client_root.mkdir(parents=True, exist_ok=True)
    spec = (
        f"Client: {client}\n"
        f"Owner: {user}\n"
        f"Root: {client_root}\n"
        "Options: noallwrite clobber nocompress unlocked nomodtime normdir\n"
        "LineEnd: local\n"
        f"View:\n\t//depot/... //{client}/...\n"
    )
    proc = subprocess.run(
        ["p4", "-p", p4d_fixture.p4port, "-u", user, "-c", client, "client", "-i"],
        input=spec,
        cwd=str(client_root),
        capture_output=True,
        text=True,
        timeout=30,
        **_NO_WINDOW,
    )
    assert proc.returncode == 0, proc.stderr

    for name, content in _SEED_FILES.items():
        (client_root / name).write_text(content, encoding="utf-8")
    _p4(p4d_fixture, user, client, ["add", *_SEED_FILES.keys()], cwd=client_root).check_returncode()
    _p4(p4d_fixture, user, client, ["submit", "-d", "seed"], cwd=client_root).check_returncode()


def _current_branch(repo_root: Path) -> str:
    proc = _run(["git", "branch", "--show-current"], cwd=repo_root)
    return proc.stdout.strip()


def _init_git_repo(client_root: Path) -> None:
    _git(["init", "-q"], client_root)
    _git(["config", "user.email", "t@t.example"], client_root)
    _git(["config", "user.name", "t"], client_root)
    _git(["add", "--", *_SEED_FILES.keys()], client_root)
    _git(["commit", "-q", "-m", "seed"], client_root)
    _git(["branch", "-m", "work/p4-shelve-e2e"], client_root)


def _add_bare_remote(client_root: Path, tmp_path: Path) -> None:
    """A local bare repo standing in for the git remote -- push lands for
    real (a real `git push`), never a network call (D3's own budget note:
    the remote leg is what makes this op's spawn budget expensive; this
    fixture pays that cost once, locally, to exercise it honestly)."""
    bare = tmp_path / "origin.git"
    _run(["git", "init", "-q", "--bare", str(bare)])
    _git(["remote", "add", "origin", str(bare)], client_root)
    # Establish tracking with an upfront publish of the seed commit --
    # `push_outstanding`'s own no-upstream-ref auto-publish
    # (`ops/ceremony/push.py::publish_day_branch`) is scoped to CANONICAL
    # day branches only, never an arbitrary `work/*` branch, so a fixture
    # branch outside that naming scheme needs its upstream set explicitly
    # once, the same way a genuine first push on a brand-new feature branch
    # would.
    _git(["push", "-u", "origin", _current_branch(client_root)], client_root)


def _write_local_md_p4(repo_root: Path, repo_key: str) -> None:
    (repo_root / "coordinator.local.md").write_text(
        f"---\nvcs_mirror: p4\np4_repo_key: {repo_key}\n---\n", encoding="utf-8"
    )


def _register_identity(monkeypatch, tmp_path, repo_key, p4d_fixture, client, user, client_root, repo_root):
    """Registers the machine-local `p4.<repo_key>.*` row via a scratch
    registry directory (`MACHINE_LOCAL_REGISTRY_DIR`) -- never the real
    machine's own registry."""
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    from coordinator_core.machine_resolver import registry_set

    registry_set(f"p4.{repo_key}.port", p4d_fixture.p4port)
    registry_set(f"p4.{repo_key}.user", user)
    registry_set(f"p4.{repo_key}.client", client)
    registry_set(f"p4.{repo_key}.client_root", str(client_root))
    registry_set(f"p4.{repo_key}.repo_root", str(repo_root))


def _prime_session_cl(repo_root: Path, sid: str) -> int:
    """Mints the session CL (and records `p4_base_sha` off HEAD) BEFORE any
    edit lands -- production reaches this through D5's checkout-before-edit
    guard on the session's first touched file, which always fires ahead of
    the commit that follows it. This e2e test exercises D4's shelve leg in
    isolation from D5's own guard (a separate row, separately tested), so
    it stands in for "the guard already primed the session CL" the same way
    `_make_writable` stands in for "the guard already checked the file
    out" -- calling `ensure_session_change` here, not inside
    `push_outstanding`'s own lazy first-use mint, is what keeps
    `p4_base_sha` anchored at the PRE-edit HEAD rather than the post-commit
    one D4's path-set derivation (`shelve.py::_path_set`) depends on.

    `update_meta_fields` (D3's writer) is a documented no-op when
    `meta.json` is absent -- ordinary session lifecycle always creates it at
    session start, ahead of any op reaching this module, which this
    fixture stands in for by creating an empty one itself (the same
    `d.mkdir(); (d / "meta.json").write_text("{}")` shape every other
    module in this package's own test fixtures use, e.g.
    `test_shelve.py::sdir`)."""
    from coordinator_core.p4.session_change import ensure_session_change
    from coordinator_core.session.core import session_dir

    sdir = Path(session_dir(sid, cwd=str(repo_root)))
    sdir.mkdir(parents=True, exist_ok=True)
    meta_path = sdir / "meta.json"
    if not meta_path.exists():
        meta_path.write_text("{}", encoding="utf-8")

    return ensure_session_change(str(repo_root), sid)


def _commit_and_push(monkeypatch, repo_root: Path, sid: str, paths, deleted_paths, message):
    from coordinator_core.ops.ceremony import commit_v2
    from coordinator_core.ops import push_outstanding as push_outstanding_mod

    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    commit_result = commit_v2._handler(
        {"paths": paths, "deleted_paths": deleted_paths, "message": message},
        repo_root=repo_root / ".git",
    )
    assert commit_result["committed"] is True, commit_result

    outcome = push_outstanding_mod.push_outstanding(repo_root, session_id=sid)
    assert outcome.exit_code == 0, outcome
    assert "push:no-remote" not in outcome.skipped, outcome.skipped
    return commit_result, outcome


def _shelved_changes(p4d_fixture, user, client):
    proc = _p4(p4d_fixture, user, client, ["-ztag", "changes", "-s", "shelved", "-c", client])
    proc.check_returncode()
    changes = []
    current = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            if current:
                changes.append(current)
                current = {}
            continue
        if line.startswith("... "):
            key, _, value = line[4:].partition(" ")
            current[key] = value
    if current:
        changes.append(current)
    return changes


def _shelved_paths(p4d_fixture, user, client, cl: str):
    proc = _p4(p4d_fixture, user, client, ["-ztag", "describe", "-S", "-s", cl])
    proc.check_returncode()
    paths = []
    for line in proc.stdout.splitlines():
        if line.startswith("... depotFile"):
            depot_path = line.split(" ", 2)[2]
            paths.append(depot_path.rsplit("/", 1)[-1])
    return sorted(paths)


class TestPositiveLegShelvesExactlyThePushedPaths:
    def test_edit_edit_delete_shelves_exactly_the_pushed_paths(self, monkeypatch, tmp_path, p4d):
        client = "e2e-client"
        user = "bob"
        repo_key = "p4-studio/e2e-shelve"
        client_root = tmp_path / "workspace"

        _init_client_and_seed(p4d, client_root, client, user)
        _init_git_repo(client_root)
        _add_bare_remote(client_root, tmp_path)
        _write_local_md_p4(client_root, repo_key)
        _register_identity(monkeypatch, tmp_path, repo_key, p4d, client, user, client_root, client_root)

        sid = str(uuid.uuid4())
        _prime_session_cl(client_root, sid)

        _make_writable(client_root / "a.txt")
        _make_writable(client_root / "b.txt")
        _make_writable(client_root / "c.txt")
        (client_root / "a.txt").write_text("a-changed\n", encoding="utf-8")
        (client_root / "b.txt").write_text("b-changed\n", encoding="utf-8")
        (client_root / "c.txt").unlink()

        _commit_and_push(
            monkeypatch,
            client_root,
            sid,
            paths=["a.txt", "b.txt"],
            deleted_paths=["c.txt"],
            message="edit a/b, delete c",
        )

        changes = _shelved_changes(p4d, user, client)
        assert len(changes) == 1, changes
        cl = changes[0]["change"]
        assert _shelved_paths(p4d, user, client, cl) == ["a.txt", "b.txt", "c.txt"]


class TestResumeReusesTheSameCL:
    def test_a_second_commit_and_push_reuses_the_session_cl(self, monkeypatch, tmp_path, p4d):
        client = "e2e-resume-client"
        user = "bob"
        repo_key = "p4-studio/e2e-resume"
        client_root = tmp_path / "workspace"

        _init_client_and_seed(p4d, client_root, client, user)
        _init_git_repo(client_root)
        _add_bare_remote(client_root, tmp_path)
        _write_local_md_p4(client_root, repo_key)
        _register_identity(monkeypatch, tmp_path, repo_key, p4d, client, user, client_root, client_root)

        sid = str(uuid.uuid4())
        _prime_session_cl(client_root, sid)

        _make_writable(client_root / "a.txt")
        (client_root / "a.txt").write_text("a-changed\n", encoding="utf-8")
        _, outcome_1 = _commit_and_push(
            monkeypatch, client_root, sid, paths=["a.txt"], deleted_paths=[], message="edit a"
        )

        _make_writable(client_root / "b.txt")
        (client_root / "b.txt").write_text("b-changed\n", encoding="utf-8")
        _, outcome_2 = _commit_and_push(
            monkeypatch, client_root, sid, paths=["b.txt"], deleted_paths=[], message="edit b"
        )

        changes = _shelved_changes(p4d, user, client)
        assert len(changes) == 1, changes  # resume -- never a second CL
        cl = changes[0]["change"]
        assert _shelved_paths(p4d, user, client, cl) == ["a.txt", "b.txt"]


class TestClosedCLReMintsRatherThanFailing:
    def test_session_cl_submitted_out_from_under_it_remints(self, monkeypatch, tmp_path, p4d):
        client = "e2e-remint-client"
        user = "bob"
        repo_key = "p4-studio/e2e-remint"
        client_root = tmp_path / "workspace"

        _init_client_and_seed(p4d, client_root, client, user)
        _init_git_repo(client_root)
        _add_bare_remote(client_root, tmp_path)
        _write_local_md_p4(client_root, repo_key)
        _register_identity(monkeypatch, tmp_path, repo_key, p4d, client, user, client_root, client_root)

        sid = str(uuid.uuid4())
        _prime_session_cl(client_root, sid)

        _make_writable(client_root / "a.txt")
        (client_root / "a.txt").write_text("a-changed\n", encoding="utf-8")
        _, _ = _commit_and_push(
            monkeypatch, client_root, sid, paths=["a.txt"], deleted_paths=[], message="edit a"
        )

        changes_before = _shelved_changes(p4d, user, client)
        assert len(changes_before) == 1
        session_cl = changes_before[0]["change"]

        # A human/cockpit submits the session's shelved CL out from under
        # it (D4's re-mint trigger is a classified refusal against the
        # RECORDED base sha becoming unreachable, not a live CL probe --
        # so this test drives the actual git-side condition that trips it:
        # the recorded base sha is rewritten out of history, exactly what a
        # submit-then-reset workflow produces).
        _p4(p4d, user, client, ["unshelve", "-s", session_cl, "-c", session_cl]).check_returncode()
        # p4 refuses to submit a CL that still carries a shelf ("Change N has
        # shelved files -- cannot submit."), so the shelf is deleted first.
        # This is not a test convenience: it is the real sequence a human or
        # cockpit performs to submit the session's CL, which is the event
        # this test exists to simulate.
        _p4(p4d, user, client, ["shelve", "-d", "-c", session_cl]).check_returncode()
        # `-c <cl>` and `-d <description>` are MUTUALLY EXCLUSIVE submit
        # grammars (`p4 help submit`: `submit [...] -c changelist#` is its
        # own form). The CL already carries the description session_change
        # minted it with, so submitting it by number needs no `-d`.
        _p4(p4d, user, client, ["submit", "-c", session_cl]).check_returncode()

        # Rewrite the recorded base sha unreachable, forcing D4's loud
        # re-mint fallback rather than a silent empty-path-set read.
        from coordinator_core.session.core import session_dir, update_meta_fields

        monkeypatch.setattr(
            "coordinator_core.ops.push_outstanding.session_dir",
            lambda s, cwd=None: str(tmp_path / "sdir"),
        )
        sdir = tmp_path / "sdir"
        sdir.mkdir(exist_ok=True)
        # `update_meta_fields` is a documented no-op when `meta.json` is
        # absent (`session/core.py::update_meta_fields`) -- this is a FRESH
        # scratch sdir the fixture just created, so without seeding an empty
        # object first, the `p4_base_sha` write below silently does nothing,
        # `_p4_leg_execute` falls back to `head_sha(root)` (the POST-push
        # HEAD) as the base, `_base_reachable` trivially succeeds against
        # it, and D4's re-mint branch this test exists to exercise never
        # fires at all -- measured: `state["p4_base_sha"]` read back as
        # `None`, not the written all-zero sha.
        (sdir / "meta.json").write_text("{}", encoding="utf-8")
        update_meta_fields(str(sdir), {"p4_base_sha": "0" * 40})

        _make_writable(client_root / "b.txt")
        (client_root / "b.txt").write_text("b-changed\n", encoding="utf-8")
        from coordinator_core.ops.ceremony import commit_v2
        from coordinator_core.ops import push_outstanding as push_outstanding_mod

        monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
        commit_result = commit_v2._handler(
            {"paths": ["b.txt"], "message": "edit b after external submit"},
            repo_root=client_root / ".git",
        )
        assert commit_result["committed"] is True, commit_result

        outcome = push_outstanding_mod.push_outstanding(client_root, session_id=sid)
        assert outcome.exit_code == 0, outcome

        changes_after = _shelved_changes(p4d, user, client)
        assert len(changes_after) == 1, changes_after
        assert changes_after[0]["change"] != session_cl  # re-minted, not reused


class TestRegisterWorkspaceAgainstRealServer:
    """`p4.register_workspace` through the real `runner.run` (`-s` tagged
    output) -- every other e2e leg here seeds the registry by hand and so
    never exercised the client-spec parse."""

    def test_registers_a_real_client(self, monkeypatch, tmp_path, p4d):
        user, client = "bob", "bob-register-ws"
        client_root = tmp_path / "ws"
        _init_client_and_seed(p4d, client_root, client, user)
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

        from coordinator_core.machine_resolver import registry_get
        from coordinator_core.p4 import register

        result = register._register_workspace(
            {
                "repo_key": "p4-studio/game-main",
                "repo_root": str(client_root),
                "port": p4d.p4port,
                "user": user,
                "client": client,
            }
        )

        assert result["ok"] is True, result
        assert Path(registry_get("p4.p4-studio/game-main.client_root")).resolve() == client_root.resolve()
