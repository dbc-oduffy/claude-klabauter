"""
coordinator_core.ops.tests.test_group_em_admission_post_op -- tests for "groupem.admission_post"
(registration, dispatch append, fail-open, invalid params).
"""

from __future__ import annotations

import json
import os

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import admission_post, nomination
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_admission_post as op


def test_registration_quad():
    assert ipc._REGISTRY.get("groupem.admission_post") is not None
    assert (
        _registry_map.OP_MODULE_MAP["groupem.admission_post"]
        == "coordinator_core.ops.group_em_admission_post"
    )
    assert OP_KEY_SCOPE["groupem.admission_post"] == "none"
    assert OP_CLASSIFICATION["groupem.admission_post"] is OpClass.MUTATING


def test_dispatch_appends_line(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(
        nomination, "box_holder", lambda directory=None, now=None: {"session_id": "gem", "live": True}
    )
    out = ipc._REGISTRY["groupem.admission_post"](
        {"phase": "start", "session_id": "s1", "pids": [os.getpid()], "run": "probe"}
    )
    assert out["posted"] is True and out["pids"] == [os.getpid()]
    lines = admission_post.admission_path().read_text(encoding="utf-8").splitlines()
    (rec,) = [json.loads(x) for x in lines]
    assert rec["phase"] == "START" and rec["session_id"] == "s1" and rec["run"] == "probe"


def test_no_holder_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(nomination, "box_holder", lambda directory=None, now=None: None)
    out = op._groupem_admission_post({"phase": "END", "session_id": "s1", "pids": [1]})
    assert out == {"posted": False, "reason": "no_box_holder"}
    assert not admission_post.admission_path().exists()


def test_invalid_params_return_error(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert "error" in op._groupem_admission_post({"phase": "START"})
    assert "error" in op._groupem_admission_post({"phase": "START", "session_id": "s", "pids": 5})
    assert "error" in op._groupem_admission_post(
        {"phase": "START", "session_id": "s", "note": 3}
    )
