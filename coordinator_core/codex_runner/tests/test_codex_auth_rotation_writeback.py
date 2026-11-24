"""writeback_codex_auth keeps every isolated codex worker home on one single-use refresh-token lineage."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from coordinator_core.codex_runner import codex_spawn_env as cse

ALREADY_USED = "refresh token was already used"


@pytest.fixture(autouse=True)
def _no_pwsh_check(monkeypatch):
    monkeypatch.setattr(cse, "check_pwsh_launchable", lambda env: None)


class FakeTokenServer:
    """In-process token endpoint: each refresh token is valid once and rotates to the next."""

    def __init__(self, first: str) -> None:
        self.valid = first
        self.issued = 0
        self.clock = 1_000_000.0

    def refresh(self, home: Path) -> str:
        auth = home / "auth.json"
        presented = json.loads(auth.read_text(encoding="utf-8"))["refresh_token"]
        if presented != self.valid:
            raise RuntimeError(ALREADY_USED)
        self.issued += 1
        self.valid = f"rt-{self.issued}"
        auth.write_text(json.dumps({"refresh_token": self.valid}), encoding="utf-8")
        self.clock += 10
        os.utime(auth, (self.clock, self.clock))
        return self.valid


def _real_home(tmp_path: Path, token: str, mtime: float) -> Path:
    real = tmp_path / "real-codex"
    real.mkdir()
    auth = real / "auth.json"
    auth.write_text(json.dumps({"refresh_token": token}), encoding="utf-8")
    os.utime(auth, (mtime, mtime))
    return real


def _launch(tmp_path: Path, name: str, real: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=tmp_path / name, environ={"CODEX_HOME": str(real)})
    return env


def _token(home: Path) -> str:
    return json.loads((home / "auth.json").read_text(encoding="utf-8"))["refresh_token"]


def test_child_rotation_in_isolated_home_reaches_real_home(tmp_path):
    real = _real_home(tmp_path, "rt-0", 1_000_000.0)
    env = _launch(tmp_path, "run-a", real)
    FakeTokenServer("rt-0").refresh(Path(env["CODEX_HOME"]))
    assert cse.writeback_codex_auth(env, environ={}) is True
    assert _token(real) == "rt-1"
    assert (real / "auth.json").stat().st_mtime == pytest.approx((Path(env["CODEX_HOME"]) / "auth.json").stat().st_mtime)


def test_two_isolated_homes_refresh_in_sequence_and_real_home_ends_on_latest(tmp_path):
    real = _real_home(tmp_path, "rt-0", 1_000_000.0)
    server = FakeTokenServer("rt-0")
    env_a = _launch(tmp_path, "run-a", real)
    server.refresh(Path(env_a["CODEX_HOME"]))
    cse.writeback_codex_auth(env_a, environ={})
    env_b = _launch(tmp_path, "run-b", real)
    assert server.refresh(Path(env_b["CODEX_HOME"])) == "rt-2"
    cse.writeback_codex_auth(env_b, environ={})
    assert _token(real) == "rt-2"
    assert server.valid == "rt-2"


def test_without_writeback_the_second_home_hits_the_reused_token(tmp_path):
    real = _real_home(tmp_path, "rt-0", 1_000_000.0)
    server = FakeTokenServer("rt-0")
    server.refresh(Path(_launch(tmp_path, "run-a", real)["CODEX_HOME"]))
    env_b = _launch(tmp_path, "run-b", real)
    with pytest.raises(RuntimeError, match=ALREADY_USED):
        server.refresh(Path(env_b["CODEX_HOME"]))


def test_newer_real_home_still_seeds_the_isolated_one(tmp_path):
    real = _real_home(tmp_path, "rt-0", 1_000_000.0)
    env = _launch(tmp_path, "run-a", real)
    auth = real / "auth.json"
    auth.write_text(json.dumps({"refresh_token": "fresh-login"}), encoding="utf-8")
    os.utime(auth, (2_000_000.0, 2_000_000.0))
    env = _launch(tmp_path, "run-a", real)
    assert _token(Path(env["CODEX_HOME"])) == "fresh-login"
    assert cse.writeback_codex_auth(env, environ={}) is False
    assert _token(real) == "fresh-login"


def test_older_or_identical_isolated_copy_does_not_overwrite_real(tmp_path):
    real = _real_home(tmp_path, "rt-0", 1_000_000.0)
    env = _launch(tmp_path, "run-a", real)
    assert cse.writeback_codex_auth(env, environ={}) is False
    home_auth = Path(env["CODEX_HOME"]) / "auth.json"
    home_auth.write_text(json.dumps({"refresh_token": "stale"}), encoding="utf-8")
    os.utime(home_auth, (500_000.0, 500_000.0))
    assert cse.writeback_codex_auth(env, environ={}) is False
    assert _token(real) == "rt-0"


def test_only_auth_json_is_written_back(tmp_path):
    real = _real_home(tmp_path, "rt-0", 1_000_000.0)
    (real / "config.toml").write_text("m = 1", encoding="utf-8")
    env = _launch(tmp_path, "run-a", real)
    home = Path(env["CODEX_HOME"])
    FakeTokenServer("rt-0").refresh(home)
    (home / "AGENTS.md").write_text("doctrine", encoding="utf-8")
    (home / "config.toml").write_text("m = 2", encoding="utf-8")
    (home / "sessions").mkdir()
    (home / "sessions" / "s.jsonl").write_text("x", encoding="utf-8")
    cse.writeback_codex_auth(env, environ={})
    assert sorted(p.name for p in real.iterdir()) == ["auth.json", "config.toml"]
    assert (real / "config.toml").read_text(encoding="utf-8") == "m = 1"


def test_concurrent_writebacks_never_expose_a_partial_auth_json(tmp_path):
    real = _real_home(tmp_path, "rt-0", 1_000_000.0)
    payload = {"refresh_token": "x" * 200_000}
    envs = []
    for i in range(4):
        env = _launch(tmp_path, f"run-{i}", real)
        auth = Path(env["CODEX_HOME"]) / "auth.json"
        auth.write_text(json.dumps({**payload, "n": i}), encoding="utf-8")
        os.utime(auth, (2_000_000.0 + i, 2_000_000.0 + i))
        envs.append(env)
    stop = threading.Event()
    reader_started = threading.Event()
    torn: list[str] = []
    reads = [0]
    barrier = threading.Barrier(len(envs) * 3)
    errors: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads((real / "auth.json").read_text(encoding="utf-8"))
                reads[0] += 1
            except json.JSONDecodeError as e:
                torn.append(str(e))
            except OSError:
                pass
            reader_started.set()

    def writer(env: dict[str, str]) -> None:
        try:
            barrier.wait(timeout=60)
            cse.writeback_codex_auth(env, {})
        except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    t = threading.Thread(target=reader)
    t.start()
    writers = [threading.Thread(target=writer, args=(e,)) for e in envs for _ in range(3)]
    try:
        assert reader_started.wait(timeout=60)
        for w in writers:
            w.start()
        for w in writers:
            w.join(timeout=120)
            assert not w.is_alive()
    finally:
        stop.set()
        t.join(timeout=60)
    assert not t.is_alive()
    assert errors == []
    assert reads[0] >= 1
    assert torn == []
    assert json.loads((real / "auth.json").read_text(encoding="utf-8"))["refresh_token"] == "x" * 200_000
    assert [p.name for p in real.iterdir()] == ["auth.json"]


def test_missing_real_home_or_unshaped_env_is_a_silent_noop(tmp_path):
    assert cse.writeback_codex_auth({}, environ={}) is False
    assert cse.writeback_codex_auth({"CODEX_HOME": str(tmp_path / "nope")}, environ={}) is False
    real = _real_home(tmp_path, "rt-0", 1_000_000.0)
    env = _launch(tmp_path, "run-a", real)
    (Path(env["CODEX_HOME"]) / "auth.json").unlink()
    assert cse.writeback_codex_auth(env, environ={}) is False
    assert _token(real) == "rt-0"
