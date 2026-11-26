"""A pending agy leg started without AGY_LEG_LOG keeps its log and spool under settings-home unimatrix/agy-legs, never the engine tree."""
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
CONV = "2a49865c-2678-4d7f-967e-cc9f86f19b99"


@pytest.fixture
def leg(tmp_path, monkeypatch):
    monkeypatch.delenv("AGY_LEG_LOG", raising=False)
    monkeypatch.delenv("AGY_LEG_SPOOL", raising=False)
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", CONV)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    spec = importlib.util.spec_from_file_location("agy_leg_log_home", BIN / "unimatrix-agy-leg.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def legs_dir(tmp_path):
    return tmp_path / "settings" / "unimatrix" / "agy-legs"


def test_default_log_is_the_conversation_log_under_settings_home(leg, tmp_path):
    assert Path(leg.log_path()) == legs_dir(tmp_path) / f"{CONV}.log"


def test_default_log_without_a_conversation_id_is_the_generic_name_under_settings_home(leg, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID")
    assert Path(leg.log_path()) == legs_dir(tmp_path) / "unimatrix-agy-leg.log"


def test_default_spool_is_the_conversation_queue_beside_the_default_log(leg, tmp_path):
    assert Path(leg.spool_path()) == legs_dir(tmp_path) / f"{CONV}.queue.jsonl"


def test_default_log_creates_its_directory_so_the_first_write_lands(leg, tmp_path):
    path = Path(leg.log_path())
    assert path.parent == legs_dir(tmp_path)  # asserted before any write: a wrong default must not be written to
    leg.log("first-line")
    assert "first-line" in path.read_text(encoding="utf-8")


def test_default_spool_is_writable_without_any_override(leg, tmp_path):
    spool = Path(leg.spool_path())
    assert spool.parent == legs_dir(tmp_path)  # asserted before any write: a wrong default must not be written to
    leg.log("make-the-dir")
    msg_id = leg.enqueue("queued-under-settings-home")
    assert msg_id and "queued-under-settings-home" in spool.read_text(encoding="utf-8")


def test_unset_settings_home_refuses_naming_the_key(leg, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME")
    with pytest.raises(RuntimeError, match="COORDINATOR_SETTINGS_HOME"):
        leg.log_path()


def test_nothing_defaults_into_the_engine_tree(leg, tmp_path):
    assert BIN not in Path(leg.log_path()).parents
    assert BIN not in Path(leg.spool_path()).parents


def test_explicit_log_override_still_wins_and_the_spool_follows_it(leg, tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_LEG_LOG", str(tmp_path / "elsewhere" / "late.log"))
    assert leg.log_path() == str(tmp_path / "elsewhere" / "late.log")
    assert Path(leg.spool_path()) == tmp_path / "elsewhere" / f"{CONV}.queue.jsonl"


def test_explicit_spool_override_still_wins(leg, tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_LEG_SPOOL", str(tmp_path / "custom.queue.jsonl"))
    assert leg.spool_path() == str(tmp_path / "custom.queue.jsonl")
