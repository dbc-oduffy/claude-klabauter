"""One host-issued Codex submit. This receiver is private to the supervising launcher.

The transport authenticates the invoking command before entry. The allocation, caller
environment and receipt directory belong to the host and are never taken from the wire.
"""

import hashlib
import json
import os
from pathlib import Path

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops.engine_ask_submit import _engine_ask_submit
from coordinator_core.ops.workflow_fire.headless_em_grant import registered_launch_observation
from coordinator_core.session.caller_env import bind_caller_env
from coordinator_core.session.core import session_identity_override


class HostSubmit:
    version = 1

    def __init__(self, *, authorized_params, workspace, receipt_directory, launcher_session_id):
        self.workspace = Path(workspace).resolve(strict=True)
        self.launcher_session_id = launcher_session_id
        params = dict(authorized_params)
        if (not isinstance(params.get("ask"), str) or not params["ask"].strip()
                or params.get("ask_source") not in ("pm", "relayed")
                or params.get("backend", "codex") != "codex"
                or params.get("executor", "headless") != "headless"
                or params.get("fire", True) is not True
                or params.get("cwd", str(self.workspace)) != str(self.workspace)
                or params.get("session_id", launcher_session_id) != launcher_session_id):
            raise ValueError("expected an issued default-fire Codex ask for this workspace")
        self.snapshot = self._encode(params)
        directory = Path(receipt_directory).resolve(strict=True)
        if directory.is_relative_to(self.workspace):
            raise ValueError("submit receipt directory must be outside the worker workspace")
        info = directory.stat()
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PermissionError("submit receipt directory must be private to the launcher")
        self.receipt = directory / "host-submit.json"

    @staticmethod
    def _encode(params):
        return json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    def receive(self, params, *, caller_env, native_pid, native_birth):
        """Transport supplies the authenticated launch identity separately from request fields."""
        if self._encode(params) != self.snapshot:
            return {"refused": "issued-request-mismatch"}
        sid = self.launcher_session_id
        observed = registered_launch_observation(caller_env, sid, str(self.workspace))
        if not observed or (observed["pid"], observed["start_epoch"]) != (native_pid, native_birth):
            return {"refused": "launch-identity-mismatch"}
        binding = dict(observed, request_sha256=hashlib.sha256(self.snapshot).hexdigest())
        try:
            with self.receipt.open("x", encoding="utf-8") as stream:
                json.dump({"binding": binding}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            try:
                record = json.loads(self.receipt.read_bytes())
            except (OSError, ValueError):
                record = {}
            if record.get("binding") == binding and "result" in record:
                return record["result"]
            return {"refused": "reconcile-existing-submit"}
        if os.name == "posix":
            fd = os.open(self.receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        # Once claimed, an uncertain receiver outcome must never trigger another mint.
        request = json.loads(self.snapshot)
        request.setdefault("cwd", str(self.workspace))
        with bind_caller_env(caller_env), session_identity_override(observed["launcher_session_id"]):
            result = _engine_ask_submit(request, self.workspace)
        atomic_write_bytes(self.receipt, self._encode({"binding": binding, "result": result}))
        return result
